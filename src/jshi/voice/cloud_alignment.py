"""Align timed cloud words to audio-only speaker segments, independent of provider."""
from dataclasses import replace
import json
import unicodedata
import asyncio
from time import monotonic, time
from uuid import uuid4
from .timing import collect_phases, phase, report_safely, stage


async def complete_alignment(alignment, row, executor, budget_s, on_slow, *, report=None, trace=None):
    """The identity latency budget is a progress threshold, not a discard deadline.

    Keep intake ordered until the audio result is available. In particular, never
    deliver a whole-span fallback while its real split is still being computed.
    Cancellation still propagates when the voice session closes.
    """
    submitted = monotonic()
    base = {**(trace or {}), 'alignment_id': uuid4().hex,
            'source_start_ms': getattr(row, 'start_ms', None),
            'source_end_ms': getattr(row, 'end_ms', None),
            'word_count': len(getattr(row, 'words', ()))}
    marks = {}
    def emit(state, **data):
        report_safely(report, 'cloud_alignment_timing', {**base, 'state': state, **data})
    def work():
        began = monotonic()
        emit('worker_started', executor_queue_ms=round((began-submitted)*1000, 3))
        phases = {}
        status = 'completed'
        try:
            with collect_phases(phases):
                return alignment.process(row)
        except BaseException as exc:
            status = type(exc).__name__
            raise
        finally:
            ended = monotonic()
            marks['worker_finished'] = ended
            emit('worker_finished', status=status,
                 executor_queue_ms=round((began-submitted)*1000, 3),
                 worker_ms=round((ended-began)*1000, 3),
                 worker_finished_at_ms=round(time()*1000, 3),
                 phases_ms={k: round(v, 3) for k,v in phases.items()})
    emit('submitted', budget_ms=round(budget_s*1000, 3))
    job = asyncio.get_running_loop().run_in_executor(executor, work)
    job.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
    wait_status = 'completed'
    try:
        try:
            return await asyncio.wait_for(asyncio.shield(job), budget_s)
        except asyncio.TimeoutError:
            with stage(report, 'alignment_slow_notice', **base):
                await on_slow()
            return await asyncio.shield(job)
    except BaseException as exc:
        wait_status = type(exc).__name__
        raise
    finally:
        now = monotonic()
        finished = marks.get('worker_finished')
        emit('wait_finished', status=wait_status, total_wait_ms=round((now-submitted)*1000, 3),
             worker_done=job.done(),
             loop_resume_ms=round((now-finished)*1000, 3) if finished is not None else None)


def unknown(row, reason):
    return replace(row, track_id=f'unidentified-{row.start_ms}', voiceprint_id='', confidence=None,
                   speaker_cluster_id='', identity_uncertain=True, identity_tentative=False,
                   identity_note=json.dumps({'reason': reason, 'person_scope': 'unaligned_cloud_span'}, ensure_ascii=False))


def _attributed(row, segment, text=None, start=None, end=None, words=None):
    return replace(row, text=row.text if text is None else text,
                   start_ms=row.start_ms if start is None else start,
                   end_ms=row.end_ms if end is None else end,
                   words=row.words if words is None else tuple(words),
                   track_id=segment.track_id, overlap=segment.overlap,
                   voiceprint_id=segment.voiceprint_id, confidence=segment.confidence,
                   speaker_cluster_id=segment.speaker_cluster_id,
                   identity_tentative=segment.identity_tentative,
                   identity_uncertain=segment.overlap, identity_note=segment.identity_note)


def _only_separators(text):
    return all(ch.isspace() or unicodedata.category(ch).startswith('P') for ch in text)


def align_words(row, segments):
    segments = tuple(segments)
    if not segments:
        return (unknown(row, '没有有效声音分段，不按整段确认单一人物'),)
    if len(segments) == 1 and not segments[0].overlap:
        segment = segments[0]
        covered = max(0, min(segment.end_ms, row.end_ms) - max(segment.start_ms, row.start_ms))
        if covered >= .8 * (row.end_ms - row.start_ms):
            return (_attributed(row, segment),)
    if not row.words:
        return (unknown(row, '存在多个声音段但缺少可靠字词时间戳，不能将整段归给一个人'),)
    # Locate provider tokens in the actual transcript, preserving all text and spaces.
    positioned = []
    cursor = 0
    for word in row.words:
        token = word.text.strip()
        index = row.text.find(token, cursor) if token else -1
        if index < 0 or not _only_separators(row.text[cursor:index]):
            return (unknown(row, '字词与正文无法可靠对齐，不按整段确认人物'),)
        end = index + len(token)
        positioned.append((word, row.text[cursor:end]))
        cursor = end
    if not _only_separators(row.text[cursor:]):
        return (unknown(row, '字词没有覆盖完整原话，不按整段确认人物'),)
    groups = []
    for word, text in positioned:
        overlaps = [(max(0, min(word.end_ms, s.end_ms) - max(word.start_ms, s.start_ms)), i)
                    for i, s in enumerate(segments)]
        overlaps.sort(reverse=True)
        duration = word.end_ms - word.start_ms
        best, choice = overlaps[0]
        if (best < .65 * duration or segments[choice].overlap
                or len(overlaps) > 1 and overlaps[1][0] > .2 * duration):
            choice = None
        # Uncertain word boundaries are evidence, not new speakers. Keep adjacent
        # unresolved words together even when the competing segment indices change.
        boundary = (choice,)
        if groups and groups[-1][3] == boundary:
            groups[-1][1].append(word); groups[-1][2].append(text)
        else:
            groups.append((choice, [word], [text], boundary))
    # Local uncertainty/overlap alone is not evidence of a speaker change. If the provider
    # explicitly grouped this utterance as one voice and local evidence contains
    # just one clear voice, keep the provider's original input.
    # Preserve ambiguity/overlap; never extend a partial named match to the whole.
    provider = row.track_id.strip()
    local_voices = {s.speaker_cluster_id or s.track_id for s in segments if not s.overlap}
    if (provider and provider not in {'unknown', '-1'}
            and not provider.startswith(('unidentified-', 'pending-', 'overlap-'))
            and not row.overlap and len(local_voices) == 1
            and len(groups) >= 2 and any(g[0] is None for g in groups)):
        note = {'reason': '局部字词本地归属不明，未见明确换人，沿用ASR分组保留原句；不据此确认人物',
                'person_scope': 'unaligned_cloud_span',
                'grouping_basis': ('provider_single_local_uncertain_prefix' if groups[0][0] is None
                                   else 'provider_single_local_uncertain_span'),
                'preserve_utterance': True,
                'local_uncertain_ranges': [[g[1][0].start_ms, g[1][-1].end_ms] for g in groups if g[0] is None],
                **({'local_uncertain_prefix_ms': [groups[0][1][0].start_ms, groups[0][1][-1].end_ms]}
                   if groups[0][0] is None else {}),
                'local_overlap_ranges': [[s.start_ms, s.end_ms] for s in segments if s.overlap]}
        return (replace(row, voiceprint_id='', confidence=None, speaker_cluster_id='',
                        identity_uncertain=True, identity_tentative=False,
                        overlap=any(s.overlap for s in segments),
                        identity_note=json.dumps(note, ensure_ascii=False)),)
    result = []
    for choice, words, texts, boundary in groups:
        text = ''.join(texts)
        base = replace(row, text=text, start_ms=words[0].start_ms, end_ms=words[-1].end_ms, words=tuple(words))
        result.append(unknown(base, '换人边界或重叠字词归属不明') if choice is None else _attributed(base, segments[choice]))
    if result:
        result[-1] = replace(result[-1], text=result[-1].text + row.text[cursor:])
    joined = []
    for part in result:
        previous = joined[-1] if joined else None
        key = part.speaker_cluster_id or part.track_id
        if (previous and key and key == (previous.speaker_cluster_id or previous.track_id)
                and not key.startswith(('unidentified-', 'overlap-'))
                and not part.overlap and not previous.overlap
                and 0 <= part.start_ms-previous.end_ms <= 1500):
            identity_same = previous.voiceprint_id == part.voiceprint_id
            joined[-1] = replace(previous, text=previous.text+part.text,
                end_ms=part.end_ms, words=previous.words+part.words,
                voiceprint_id=previous.voiceprint_id if identity_same else '',
                confidence=previous.confidence if identity_same else None,
                identity_uncertain=previous.identity_uncertain or part.identity_uncertain or not identity_same)
        else:
            joined.append(part)
    return tuple(joined)


class CloudSpeakerAlignment:
    def __init__(self, audio, analyzer, *, segments=None):
        self.audio, self.analyzer = audio, analyzer
        self.segment_source = segments
        self.children = {}

    def process(self, row):
        if not row.final:
            return (row,)
        if row.replaces:
            ranges = tuple(child for source in row.replaces for child in self.children.get(source, (source,)))
            return (unknown(replace(row, replaces=ranges), '云端整体修订，沿用原分段归属或保留跨人修订缺口'),)
        if self.segment_source is not None:
            segments = self.segment_source.for_range(row.start_ms, row.end_ms)
            segments = self.analyzer.identify_segments(segments)
            with phase('word_alignment_ms'):
                return align_words(row, segments)
        with phase('audio_read_ms'):
            pcm = self.audio.segment(row.start_ms, row.end_ms)
        if pcm is None:
            return (unknown(row, '原音频未完整保留，不能确认整段为同一人'),)
        import numpy as np
        with phase('pcm_convert_ms'):
            samples = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
        segments = self.analyzer.analyze(samples, row.start_ms)
        with phase('word_alignment_ms'):
            return align_words(row, segments)

    def remember(self, row, result):
        # Record only parts actually delivered.
        self.children[(row.start_ms, row.end_ms)] = tuple((r.start_ms, r.end_ms) for r in result)
        while len(self.children) > 128:
            self.children.pop(next(iter(self.children)))
