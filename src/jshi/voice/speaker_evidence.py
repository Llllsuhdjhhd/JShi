"""Preserve provider evidence; local disagreement is not automatic authority."""
from dataclasses import replace
import json


def compare_evidence(source, parts):
    provider = source.track_id if not source.track_id.startswith(('unidentified-', 'pending-')) else ''
    tracks = {p.speaker_cluster_id or p.track_id for p in parts if not p.overlap}
    split = bool(provider and len(tracks) > 1)
    result = []
    for part in parts:
        try:
            note = json.loads(part.identity_note or '{}')
        except (ValueError, TypeError):
            note = {}
        note['provider_evidence'] = {'track': provider, 'overlap': source.overlap}
        if split:
            note['speaker_conflict'] = 'provider_single_local_multiple'
        if source.overlap and not part.overlap:
            note['speaker_conflict'] = 'provider_overlap_local_single'
        # Keep ambiguous split boundaries, but do not claim a fresh named identity
        # solely because local clustering disagreed with provider grouping.
        result.append(replace(part, identity_note=json.dumps(note, ensure_ascii=False),
                              overlap=part.overlap or source.overlap,
                              identity_uncertain=part.identity_uncertain or split or source.overlap,
                              voiceprint_id='' if split or source.overlap else part.voiceprint_id))
    return tuple(result)


class TrackLinker:
    """Associate window-local clusters using common audio, never equal label numbers."""
    def __init__(self):
        self.previous = ()
        self.serial = 0

    def link(self, segments):
        groups = {}
        for segment in segments:
            if not segment.overlap:
                groups.setdefault(segment.track_id, []).append(segment)
        mapping, used = {}, set()
        candidates = []
        for label, spans in groups.items():
            scores = {}
            for span in spans:
                for old in self.previous:
                    if old.overlap:
                        continue
                    amount = max(0, min(span.end_ms, old.end_ms)-max(span.start_ms, old.start_ms))
                    if amount:
                        scores[old.track_id] = scores.get(old.track_id, 0) + amount
            ranked = sorted(scores.items(), key=lambda p: p[1], reverse=True)
            if ranked and ranked[0][1] >= 400 and (len(ranked) == 1 or ranked[0][1] > 2*ranked[1][1]):
                candidates.append((ranked[0][1], label, ranked[0][0]))
        for score, label, old in sorted(candidates, reverse=True):
            if old not in used:
                mapping[label] = old
                used.add(old)
        for label in groups:
            if label not in mapping:
                self.serial += 1
                mapping[label] = f'acoustic-{self.serial}'
        linked = tuple(replace(s, track_id=mapping[s.track_id], speaker_cluster_id=mapping[s.track_id],
                               identity_tentative=True) if not s.overlap else s for s in segments)
        self.previous = linked
        return linked
