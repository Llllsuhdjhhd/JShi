"""Bounded independent recognition after acoustic boundaries, never a duration cutoff."""
import asyncio
from collections import deque
from dataclasses import replace
from time import monotonic
from uuid import uuid4
from .timing import async_lock, report_safely


async def recognize_span(config, http, cloud, pcm, offset_ms, *, report=None):
    from .streaming_asr import open_stream
    started = monotonic()
    request_id = uuid4().hex
    def trace(stage, **values):
        report_safely(report, 'recovery_asr_timing', {'request_id': request_id,
            'start_ms':offset_ms, 'end_ms':offset_ms+len(pcm)//32, 'stage':stage,
            'elapsed_ms':round((monotonic()-started)*1000,3), **values})
    trace('connecting')
    try:
        session = await open_stream(replace(config, identity_mode='diarization'), http, cloud)
    except BaseException as exc:
        trace('connect_failed', error_type=type(exc).__name__)
        raise
    trace('connected')
    async def sender():
        for offset in range(0, len(pcm), 6400):
            await session.send_audio(pcm[offset:offset+6400])
        await session.finish()
        trace('audio_sent')
    task = asyncio.create_task(sender())
    results = {}
    try:
        async with asyncio.timeout(15):
            first = True
            async for rows, final in session.results():
                if first:
                    trace('first_result', rows=len(rows), stream_final=final)
                    first = False
                for row in rows:
                    if row.final:
                        trace('final_text', text_chars=len(row.text))
                        results[row.start_ms, row.end_ms] = replace(row,
                            start_ms=row.start_ms+offset_ms, end_ms=row.end_ms+offset_ms,
                            words=tuple(replace(w, start_ms=w.start_ms+offset_ms,
                                               end_ms=w.end_ms+offset_ms) for w in row.words), replaces=())
                if final:
                    trace('stream_final')
                    break
            await task
        trace('finished', result_count=len(results))
        return tuple(results[k] for k in sorted(results))
    except BaseException as exc:
        trace('failed', error_type=type(exc).__name__)
        raise
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session.close()
        trace('closed')


class RecoveryIntake:
    ready_gap_ms = 300  # silence after a segment before it counts as ended
    # Each extra recognition is a new billed provider stream; partial words cost nothing.
    max_recognitions_per_minute = 20

    def __init__(self, audio, recognize, deliver, report, *, identity=None):
        self.audio, self.recognize, self.deliver, self.report = audio, recognize, deliver, report
        self.identity = identity
        self.covered = deque(maxlen=256)
        self.preliminary_spans = set()
        self.events = {}  # stable early input -> acoustic scope and later source packets
        self.attempted = deque(maxlen=256)
        self.failed_attempts = {}
        self.interim = deque(maxlen=8)
        self.recognized_at = deque()
        self.pending = deque()
        self.pending_at = {}
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.closed = False
        self.task = asyncio.create_task(self._run())

    def covered_span(self, start, end):
        cursor = start
        for a,b in sorted(self.covered):
            if a > cursor+100:
                break
            if b > cursor:
                cursor = b
        return end > start and cursor >= end-min(20, (end-start)*.1)

    def covered_fraction(self, start, end):
        if end <= start:
            return 0.0
        merged = []
        for a, b in sorted(self.covered):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return sum(max(0, min(b, end)-max(a, start)) for a, b in merged) / (end-start)

    def note_interim(self, transcript):
        """Keep the latest word-timed partial text from the main stream."""
        if transcript.final or not transcript.words:
            return
        for old in list(self.interim):
            if old.start_ms == transcript.start_ms:
                self.interim.remove(old)
        self.interim.append(transcript)

    def from_interim(self, segment):
        """Words the main stream already returned for this span; no extra recognition call."""
        span = segment.end_ms - segment.start_ms
        for row in reversed(self.interim):
            words = [w for w in row.words if segment.start_ms <= (w.start_ms+w.end_ms)/2 <= segment.end_ms]
            text = ''.join(w.text for w in words)
            if (not words or not any(ch.isalnum() for ch in text)
                    or words[-1].end_ms - words[0].start_ms < .6*span):
                continue
            return (replace(row, text=text, start_ms=words[0].start_ms, end_ms=words[-1].end_ms,
                            words=tuple(words), final=True, replaces=()),)
        return ()

    def _boundary_age_ms(self, segment):
        from time import time
        receipt = (self.audio.receipt_timing(segment.start_ms, segment.end_ms)
                   if hasattr(self.audio, 'receipt_timing') else {})
        at = receipt.get('audio_end_received_at_ms')
        return round(time()*1000 - at) if at is not None else None

    async def offer(self, segments, start, end):
        if self.closed:
            return
        for s in segments:
            # Overlap detection cannot recover a second transcript without separation.
            if s.overlap or s.end_ms-s.start_ms < 160:
                continue
            # A rolling window is not a new utterance. Remove already submitted
            # acoustic scopes, including their uncertain word-time edges.
            scopes = sorted([*self.covered, *(e['bounds'] for e in self.events.values())])
            cursor = s.start_ms
            gaps = []
            for a, b in scopes:
                if b <= cursor or a >= s.end_ms:
                    continue
                if a > cursor:
                    gaps.append((cursor, min(a, s.end_ms)))
                cursor = max(cursor, b)
            if cursor < s.end_ms:
                gaps.append((cursor, s.end_ms))
            for a, b in gaps:
                await self._offer_uncovered(replace(s, start_ms=a, end_ms=b,
                    samples=s.samples[(a-s.start_ms)*16:(b-s.start_ms)*16]), end)

    async def _offer_uncovered(self, s, end):
        if s.end_ms-s.start_ms < 160:
            return
        # Six seconds only prewarms identity. Ongoing speech does not create
        # an ASR request or a formal JEV input merely because time has elapsed.
        ready = end-s.end_ms >= self.ready_gap_ms
        if not ready or self.covered_span(s.start_ms, s.end_ms):
            return
        if any(max(0,min(s.end_ms,b)-max(s.start_ms,a)) >= .7*(s.end_ms-s.start_ms)
               for a,b in self.attempted):
            return
        # Slightly jittered endpoints are the same failed acoustic attempt,
        # not permission for a fresh cloud request every second. A substantially
        # shorter, newly closed segment is different evidence and may proceed.
        failures = [v for (a,b),v in self.failed_attempts.items()
                    if abs(a-s.start_ms) <= 300 and abs(b-s.end_ms) <= 300]
        if failures and (sum(v[1] for v in failures) >= 2
                         or any(monotonic()-v[0] < (v[2] if len(v)>2 else 2) for v in failures)):
            return
        if len(self.pending) >= 8:
            self.report('recovery_backlog', {'start_ms': s.start_ms, 'end_ms': s.end_ms})
            return
        self.attempted.append((s.start_ms, s.end_ms))
        self.pending_at[s.start_ms,s.end_ms] = monotonic()
        report_safely(self.report, 'recovery_work_timing', {'stage':'queued',
            'start_ms':s.start_ms,'end_ms':s.end_ms,'pending_ahead':len(self.pending),
            'boundary_age_ms':self._boundary_age_ms(s)})
        self.pending.append(s)
        self.wake.set()

    def _reconcile_source(self, source, rows):
        """Use the original final packet before its speaker/word fragments.

        Only the same acoustic event can revise an early input. Equal words in
        later audio never suffice. Final prefix packets are retained until the
        whole early event is covered, rather than emitted as fresh utterances.
        """
        if not source.words or source.overlap:
            return rows
        candidates = [(iid, e) for iid, e in self.events.items()
                      if e['bounds'][0] < source.end_ms and e['bounds'][1] > source.start_ms]
        # Alignment has already preserved the provider's sentence. Reconciliation
        # must not split it again at individual word midpoints. The tolerance is
        # only for associating inputs, never for confirming a person's identity.
        import json
        merged, remaining, covered_words = [], [], {}
        for whole in rows:
            try:
                note = json.loads(whole.identity_note or '{}')
            except (ValueError, TypeError):
                note = {}
            matches = [(iid,e) for iid,e in candidates
                       if e['bounds'][0] < whole.end_ms and e['bounds'][1] > whole.start_ms]
            voices = {e['row'].speaker_cluster_id or e['row'].track_id for _,e in matches}
            preserve = isinstance(note,dict) and note.get('preserve_utterance')
            sound = whole.speaker_cluster_id or whole.track_id
            aligned_same_voice = bool(sound and voices == {sound} and not whole.overlap)
            if (matches and whole.words and len(voices)==1
                    and (preserve or aligned_same_voice and (len(matches)>1 or matches[0][1].get('whole_signature')))
                    and all(whole.start_ms-150 <= e['row'].start_ms
                            and e['row'].end_ms <= whole.end_ms+150 for _,e in matches)):
                matches.sort(key=lambda pair: pair[1]['bounds'][0])
                iid, first = matches[0]
                signature = (whole.text, whole.words)
                for w in whole.words:
                    covered_words[(w.start_ms,w.end_ms,w.text)] = iid
                matched_ids = {key for key,_ in matches}
                candidates = [(key,e) for key,e in candidates if key not in matched_ids]
                if first.get('whole_signature') == signature:
                    continue
                revision = replace(whole, input_id=iid,
                    replaces=tuple((e['row'].start_ms,e['row'].end_ms) for _,e in matches),
                    replaces_input_ids=tuple(key for key,_ in matches))
                for key,_ in matches:
                    self.events.pop(key, None)
                self.events[iid] = {'row':revision, 'bounds':(whole.start_ms,whole.end_ms),
                                    'finals':{}, 'whole_signature':signature}
                self.report('recovery_whole_revision', {'input_id':iid,
                    'replaced_inputs':len(matches), 'source_start_ms':source.start_ms,
                    'source_end_ms':source.end_ms, 'basis':'alignment_preserved_utterance' if preserve else 'same_acoustic_speaker'})
                merged.append(revision)
            else:
                remaining.append(whole)
        rows = tuple(remaining)
        owned = dict(covered_words)
        for word in source.words:
            middle = (word.start_ms+word.end_ms)/2
            matches = [(iid, e) for iid, e in candidates if e['bounds'][0] <= middle < e['bounds'][1]]
            if len(matches) == 1:
                owned[(word.start_ms, word.end_ms, word.text)] = matches[0][0]
        if not owned:
            return rows
        ready = list(merged)
        for iid, event in candidates:
            words = tuple(w for w in source.words if owned.get((w.start_ms,w.end_ms,w.text)) == iid)
            if not words:
                continue
            a, b = event['bounds']
            # Preserve punctuation when the whole source belongs to this event.
            text = source.text if len(words) == len(source.words) else ''.join(w.text for w in words)
            packet = replace(source, text=text, words=words,
                             start_ms=words[0].start_ms, end_ms=words[-1].end_ms)
            key = (max(a,source.start_ms), min(b,source.end_ms))
            for old in list(event['finals']):
                if key[0] <= old[0] and key[1] >= old[1]:
                    del event['finals'][old]
            event['finals'][key] = packet
            spans = sorted(event['finals'])
            gaps = sum(max(0, right[0]-left[1]) for left,right in zip(spans,spans[1:]))
            if spans[0][0] > a+80 or spans[-1][1] < b-80 or gaps > min(1200,(b-a)*.2):
                continue
            finals = [event['finals'][span] for span in spans]
            final_words = tuple(w for p in finals for w in p.words)
            final_text = ''.join(p.text for p in finals)
            signature = (final_text, final_words)
            if event.get('signature') == signature:
                continue
            event['signature'] = signature
            previous = event['row']
            if previous.text == final_text and previous.words == final_words:
                continue
            # Keep the event's prior identity. A later packet's different voice
            # label is evidence to review, not authority to rename the speaker.
            revision = replace(previous, text=final_text, words=final_words,
                start_ms=min(p.start_ms for p in finals), end_ms=max(p.end_ms for p in finals),
                final=True, replaces=((previous.start_ms,previous.end_ms),),
                replaces_input_ids=(iid,))
            event['row'] = revision
            ready.append(revision)
            self.report('recovery_event_revision', {'input_id':iid,
                'source_start_ms':source.start_ms,'source_end_ms':source.end_ms,
                'final_packets':len(finals),'text_changed':previous.text != final_text})
        # Preserve genuinely new words from the same packet (for example B after A).
        for row in rows:
            if not row.words:
                if not any(a <= row.start_ms and b >= row.end_ms for _,e in candidates for a,b in [e['bounds']]):
                    ready.append(row)
                continue
            fresh = []
            for word in (*row.words, None):
                if word is not None and (word.start_ms,word.end_ms,word.text) not in owned:
                    fresh.append(word)
                elif fresh:
                    ready.append(replace(row, text=''.join(w.text for w in fresh), words=tuple(fresh),
                        start_ms=fresh[0].start_ms,end_ms=fresh[-1].end_ms))
                    fresh=[]
        return tuple(ready)

    async def publish(self, rows, *, preliminary=False, source=None, source_span=None, **arrival):
        """Late full-stream packets reconcile against already delivered audio."""
        async with async_lock(self.lock, self.report, 'reconcile_lock_wait', preliminary=preliminary):
            if not preliminary and source is not None:
                rows = self._reconcile_source(source, rows)
            previous_coverage = tuple(self.covered)
            ready = []
            for row in rows:
                if row.replaces_input_ids:
                    ready.append(row)
                    continue
                overlapping = [(a,b) for a,b in self.covered
                               if min(b,row.end_ms) > max(a,row.start_ms)]
                if preliminary and overlapping:
                    # A second short-context decode must not overwrite or crop a
                    # transcript already delivered from this audio.
                    continue
                contained = [(a,b) for a,b in overlapping
                             if row.start_ms <= a+80 and row.end_ms >= b-80]
                if (not preliminary and overlapping and len(contained) == len(overlapping)
                        and all(span in self.preliminary_spans for span in overlapping)):
                    # Final text owns the entire old span: replace text as a unit.
                    # Independent recognizers do not share identical word clocks.
                    ready.append(replace(row, replaces=tuple(contained)))
                    continue
                matches = [(a,b) for a,b in self.covered
                           if max(0,min(b,row.end_ms)-max(a,row.start_ms)) >= .8*(b-a)]
                if self.covered_span(row.start_ms, row.end_ms):
                    if matches:
                        ready.append(replace(row, replaces=tuple(matches)))
                    continue
                if overlapping and row.words:
                    # Keep novel words only; covered material is already in the same scene.
                    groups, fresh = [], []
                    for word in row.words:
                        # Recognizers disagree on word edges; a mostly covered word was already said.
                        if self.covered_fraction(word.start_ms,word.end_ms) >= .5:
                            if fresh:
                                groups.append(fresh)
                                fresh = []
                        else:
                            fresh.append(word)
                    if fresh:
                        groups.append(fresh)
                    for group in groups:
                        part = replace(row, text=''.join(w.text for w in group),
                            start_ms=group[0].start_ms,end_ms=group[-1].end_ms,words=tuple(group))
                        ready.append(part)
                        self.covered.append((part.start_ms,part.end_ms))
                    continue
                elif overlapping:
                    self.report('recovery_unaligned_revision', {'start_ms':row.start_ms,
                        'end_ms':row.end_ms, 'text':row.text})
                    # Preserve novel content without pretending it belongs to A or B.
                    # This is explicit revision context, not a second occurrence of A.
                    from .cloud_alignment import unknown
                    ready.append(replace(unknown(row,
                        '整体转写包含此前已处理音频及新增内容，缺少字词时间；作为待对齐补充，不当作再次说过'),
                        audio_kind='revision_context'))
                    continue
                ready.append(row)
                self.covered.append((row.start_ms,row.end_ms))
            if ready:
                from .utterance_organizer import coalesce_rows
                ready = coalesce_rows(ready)
                ready = [replace(r,input_id=r.input_id or uuid4().hex) for r in ready]
                self.covered = deque(previous_coverage, maxlen=256)
                for part in ready:
                    for old in part.replaces:
                        if old in self.covered:
                            self.covered.remove(old)
                        self.preliminary_spans.discard(old)
                    self.covered.append((part.start_ms,part.end_ms))
                    if preliminary:
                        self.preliminary_spans.add((part.start_ms,part.end_ms))
                        self.events[part.input_id] = {'row':part,
                            'bounds':source_span or (part.start_ms,part.end_ms),'finals':{}}
                    elif not part.replaces and not part.replaces_input_ids:
                        # Fresh final parts also own their audio. A repeated final
                        # packet must not re-emit the other speaker beside a merge.
                        self.events[part.input_id] = {'row':part,
                            'bounds':(part.start_ms,part.end_ms),'finals':{}}
                while len(self.events) > 128:
                    self.events.pop(next(iter(self.events)))
                self.preliminary_spans.intersection_update(self.covered)
                await self.deliver(tuple(ready), **arrival)

    async def _run(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.pending:
                s = self.pending.popleft()
                started = monotonic()
                empty_success = False
                queued = self.pending_at.pop((s.start_ms,s.end_ms),started)
                report_safely(self.report, 'recovery_work_timing', {'stage':'started',
                    'start_ms':s.start_ms,'end_ms':s.end_ms,
                    'queue_ms':round((started-queued)*1000,3),'boundary_age_ms':self._boundary_age_ms(s)})
                if self.covered_span(s.start_ms, s.end_ms):
                    continue
                pcm = self.audio.segment(s.start_ms,s.end_ms)
                if pcm is None:
                    self.report('recovery_audio_missing', {'start_ms':s.start_ms,'end_ms':s.end_ms})
                    continue
                try:
                    rows = self.from_interim(s)
                    source = 'recovery_interim'
                    if not rows:
                        now = monotonic()
                        while self.recognized_at and now - self.recognized_at[0] > 60:
                            self.recognized_at.popleft()
                        if len(self.recognized_at) >= self.max_recognitions_per_minute:
                            self.report('recovery_rate_limited', {'start_ms': s.start_ms, 'end_ms': s.end_ms})
                            continue
                        self.recognized_at.append(now)
                        rows = await self.recognize(pcm,s.start_ms)
                        source = 'recovery_recognized'
                    if rows:
                        self.report(source, {'start_ms': s.start_ms, 'end_ms': s.end_ms,
                            'boundary_to_ready_ms': self._boundary_age_ms(s),
                            'queue_ms':round((started-queued)*1000,3),
                            'work_ms':round((monotonic()-started)*1000,3)})
                    # Use the same job 06 already started. No ASR final and no
                    # successful name match are required to deliver this input.
                    empty_success = not rows
                    identified = (await asyncio.to_thread(self.identity.identify, s)
                                  if rows and self.identity is not None else s)
                    rows = tuple(replace(r, track_id=identified.track_id,
                        voiceprint_id=identified.voiceprint_id, confidence=identified.confidence,
                        speaker_cluster_id=s.speaker_cluster_id,
                        identity_note=identified.identity_note,
                        identity_tentative=not bool(identified.voiceprint_id)) for r in rows)
                    # Independent ASR can emit several packets for this one
                    # selected acoustic scope. They share one stable input.
                    if len(rows) > 1:
                        rows = (replace(rows[0], text=''.join(r.text for r in rows),
                            start_ms=min(r.start_ms for r in rows), end_ms=max(r.end_ms for r in rows),
                            words=tuple(w for r in rows for w in r.words)),)
                    await self.publish(rows, preliminary=True, source_span=(s.start_ms,s.end_ms))
                    report_safely(self.report, 'recovery_work_timing', {'stage':'delivered',
                        'start_ms':s.start_ms,'end_ms':s.end_ms,'source':source,'row_count':len(rows),
                        'work_ms':round((monotonic()-started)*1000,3)})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.report('recovery_failed', {'start_ms':s.start_ms,'end_ms':s.end_ms,
                        'error_type':type(exc).__name__})
                finally:
                    # An empty long checkpoint must not permanently block the
                    # later, correctly closed short segment inside its range.
                    scope = (s.start_ms, s.end_ms)
                    if not self.covered_span(*scope):
                        if scope in self.attempted:
                            self.attempted.remove(scope)
                        old = self.failed_attempts.get(scope, (0, 0))
                        self.failed_attempts[scope] = (monotonic(), old[1]+1, .5 if empty_success else 2)
                        while len(self.failed_attempts) > 256:
                            self.failed_attempts.pop(next(iter(self.failed_attempts)))

    async def close(self):
        self.closed = True
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
