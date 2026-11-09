"""Normalize transcript revisions and session speaker tracks, independent of transport."""
from dataclasses import replace
from .contracts import Transcript, TimedWord

class TranscriptAssembler:
    def __init__(self, *, identity_mode: str = "diarization") -> None:
        self.identity_mode = identity_mode
        self.seen: set[tuple] = set()
        self.ranges: list[tuple[int, int, str]] = []
        self.expired_before_ms = 0
        self.final_texts: dict[tuple[int, int], str] = {}

    @staticmethod
    def utterance_text(utterance):
        text = utterance.get('text')
        if isinstance(text, str) and text.strip():
            return text.strip()
        # Some two-pass finals omit sentence text while supplying final words.
        # Never promote a preview or reuse earlier provisional wording.
        words = utterance.get('words')
        if utterance.get('definite') is not True or not isinstance(words, list) or not words:
            return ''
        start, end = utterance.get('start_time', 0), utterance.get('end_time', 0)
        if not isinstance(start,(int,float)) or not isinstance(end,(int,float)) or end <= start:
            return ''
        previous = start
        pieces = []
        for word in words:
            if not isinstance(word, dict):return ''
            value = word.get('text')
            a,b = word.get('start_time'),word.get('end_time')
            if (not isinstance(value,str) or not value.strip()
                or not isinstance(a,(int,float)) or not isinstance(b,(int,float))
                or not start <= a <= b <= end or a < previous):
                return ''
            value=value.strip()
            if pieces and pieces[-1][-1].isascii() and pieces[-1][-1].isalnum() and value[0].isascii() and value[0].isalnum():
                pieces.append(' ')
            pieces.append(value);previous=b
        return ''.join(pieces)

    @staticmethod
    def covered(start: int, end: int, ranges) -> bool:
        if end <= start:
            return False
        # A newly arriving speaker can speak wholly inside another person's
        # interval. Only shared committed boundaries justify treating a range
        # as a revision/merge of previously delivered audio.
        if not any(a == start for a, _, _ in ranges) or not any(b == end for _, b, _ in ranges):
            return False
        cursor = start
        gaps = 0
        for a, b, _ in sorted(ranges):
            if b <= cursor:
                continue
            if a > cursor:
                gaps += a - cursor
                # Re-segmentation can absorb short pauses between old words.
                if a - cursor > 1200 or gaps > (end - start) * .2:
                    return False
            cursor = max(cursor, b)
            if cursor >= end:
                return True
        return False

    def accept(self, payload: dict) -> tuple[Transcript, ...]:
        result = payload.get("result", {})
        results = result if isinstance(result, list) else [result]
        outputs = []
        # Compare against earlier packets only: simultaneous new speakers in
        # this packet must both reach the scene, including overlapping speech.
        committed = tuple(self.ranges)
        for r in results:
            utterances = r.get("utterances", [])
            for u in utterances:
                text = self.utterance_text(u)
                if not text:
                    continue
                start, end = int(u.get("start_time", 0)), int(u.get("end_time", 0))
                additions = u.get("additions") or {}
                sid = str(additions.get("speaker_id", u.get("speaker_id", "")))
                # Unknown or absent speaker id must not bind successive people.
                track = sid if sid and sid not in {"-1", "unknown"} else f"unidentified-{start}"
                final = u.get("definite") is True
                if end > start and end <= self.expired_before_ms:
                    continue
                key = (start, end, text)
                if final and key in self.seen:
                    continue
                replaces = ()
                if end > start and self.covered(start, end, committed):
                    if not final:
                        continue
                    if (start, end) not in self.final_texts:
                        continue  # history regrouping can span different people; never reassign it
                    replaces = ((start, end),)
                    prior = ''.join(self.final_texts[k] for k in replaces)
                    if not replaces or ''.join(prior.split()) == ''.join(text.split()):
                        continue  # only speaker labels / history grouping changed
                overlap = bool(u.get("overlap", additions.get("overlap", False))) or any(
                    a < end and start < b and other != track for a, b, other in self.ranges)
                # Only registered ids supplied by voiceprint mode can be used as
                # permanent identity evidence. Diarization numbers are never ids.
                vpid = sid if self.identity_mode == "voiceprint" and sid and sid not in {"-1", "unknown"} else ""
                words = []
                previous = start
                for word in u.get('words') or ():
                    if not isinstance(word, dict):
                        words = []; break
                    a, b, token = word.get('start_time'), word.get('end_time'), word.get('text')
                    if (not isinstance(a, (int, float)) or not isinstance(b, (int, float))
                            or not isinstance(token, str) or not token.strip()
                            or not start <= a < b <= end or a < previous):
                        words = []; break
                    words.append(TimedWord(token, int(a), int(b)))
                    previous = b
                outputs.append(Transcript(text, track, start, end, final, overlap, vpid,
                                          replaces=replaces, words=tuple(words)))
                if final:
                    self.seen.add(key)
                    for old in replaces:
                        self.final_texts.pop(old, None)
                    self.final_texts[start, end] = text
                    self.ranges.append((start, end, track))
        # Session ASR frames repeat the history; bound bookkeeping by time.
        if self.seen:
            cutoff = max(self.expired_before_ms, max(k[1] for k in self.seen) - 60000)
            self.expired_before_ms = cutoff
            self.seen = {k for k in self.seen if k[1] >= cutoff}
            self.ranges = [r for r in self.ranges if r[1] >= cutoff]
            self.final_texts = {k: v for k, v in self.final_texts.items() if k[1] >= cutoff}
        return tuple(replace(t, overlap=True) if any(
            other.track_id != t.track_id and other.start_ms < t.end_ms and t.start_ms < other.end_ms
            for other in outputs) else t for t in outputs)




def equivalent_regrouping(transcript, records):
    """Recognize an exact lexical replay of contiguous audio from this session.

    This is not a general overlap/revision detector. Different wording or audio
    sources remain untouched, and no earlier speaker assignment is rewritten.
    """
    if not transcript.final or transcript.replaces or transcript.end_ms <= transcript.start_ms:
        return ()
    rows = sorted(((iid, row['transcript']) for iid, row in records.items()
                   if row.get('transcript') is not None and row.get('audio_kind') != 'separated'
                   and not row.get('superseded_by')
                   and row['transcript'].final
                   and transcript.start_ms <= row['transcript'].start_ms
                   and row['transcript'].end_ms <= transcript.end_ms),
                  key=lambda pair: (pair[1].start_ms, pair[1].end_ms))
    if len(rows) < 2:
        return ()
    cursor = transcript.start_ms
    for _, old in rows:
        if old.start_ms != cursor or old.end_ms <= cursor:
            return ()
        cursor = old.end_ms
    if cursor != transcript.end_ms:
        return ()
    def words(text):
        return ''.join(ch for ch in text if ch.isalnum())
    if words(''.join(old.text for _, old in rows)) != words(transcript.text):
        return ()
    return tuple(iid for iid, _ in rows)


def cosmetic_revision(previous, current):
    # Preserve question/exclamation marks, commas, digits and inner punctuation.
    # Only whitespace and a terminal full stop are irrelevant here.
    def normalize(text):
        return ''.join(text.split()).rstrip('。.')
    return normalize(previous) == normalize(current)


def same_request_wording(previous, current):
    """Only formatting or the known vocative 石→匠石 is safe without interpretation.

    Never use edit distance: a single negation, number or person can change a request.
    The short name is accepted only before a vocative comma, not inside a sentence.
    """
    import re
    def normalize(text):
        text = re.sub(r'^\s*(?:匠石|石)\s*[,，]', '匠石，', text)
        return ''.join(text.split()).rstrip('。.')
    return normalize(previous) == normalize(current)


def equivalent_input_revision(previous, current):
    """Only the same input: typography and a vocative comma don't need a reply.

    Do not strip all punctuation (1.5 / 15, 不，要 / 不要 can differ), or
    equate arbitrary one-character edits. Semantic edits use JEV's revision context.
    """
    import re
    import unicodedata
    def punctuation_equivalent(a, b):
        # ASR may punctuate an already lexical question only in its final packet.
        # Preserve changes at negation/number/quote boundaries (不，要 vs 不要).
        def key(text):
            text=unicodedata.normalize('NFKC',''.join(text.split()))
            if re.search(r'[“”"‘’\':;：；]',text):
                return None
            if re.search(r'(?:不|别|没|无|否|莫|勿|休|未|不要|不能|不用|不是|没有),|,(?:不|别|没|无|否|莫|勿|休|未)(?:[,。.!?]|$)|\d[,，]\d',text):
                return None
            # Merely adding ? to a plain statement is not necessarily cosmetic.
            if re.search(r'(?:吗|么|呢|谁|什么|啥|怎么样|多少|哪里|哪儿)[?。.]*$',text):
                text=text.rstrip('?。.')
            else:
                text=text.rstrip('。.')
            return text.replace(',','')
        ka,kb=key(a),key(b)
        return ka is not None and ka==kb
    if punctuation_equivalent(previous,current):
        return True
    def normalize(text):
        text=unicodedata.normalize('NFKC',text)
        text=re.sub(r'^\s*(?:匠石|石)\s*,', '匠石', text)
        # Commas in ordinary positive prose may change without a new intent.
        # Keep them around negation, quantities and quoted speech for review.
        if not re.search(r'[不别没无否莫勿休未\d一二三四五六七八九十百千万零两“”"‘’]',text):
            text=text.replace(',','').replace('，','')
        return ''.join(text.split()).rstrip('。.')
    return normalize(previous)==normalize(current)
