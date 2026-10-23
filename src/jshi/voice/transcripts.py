"""Normalize transcript revisions and session speaker tracks, independent of transport."""
from dataclasses import replace
from .contracts import Transcript

class TranscriptAssembler:
    def __init__(self, *, identity_mode: str = "diarization") -> None:
        self.identity_mode = identity_mode
        self.seen: set[tuple] = set()
        self.ranges: list[tuple[int, int, str]] = []
        self.expired_before_ms = 0
        self.final_texts: dict[tuple[int, int], str] = {}

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
                text = str(u.get("text", "")).strip()
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
                outputs.append(Transcript(text, track, start, end, final, overlap, vpid, replaces=replaces))
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


