"""Versioned utterances; publishing is separate from expression completion."""
from dataclasses import dataclass, field


@dataclass
class UtteranceRecord:
    utterance_id: str
    speaker: tuple | None
    parts: dict = field(default_factory=dict)
    version: int = 0
    state: str = 'unknown'
    interrupted_by: list = field(default_factory=list)

    @property
    def rows(self):
        return sorted(self.parts.values(), key=lambda r: (r.start_ms, r.end_ms))

    def view(self):
        return {'utterance_id': self.utterance_id, 'version': self.version,
            'speaker': self.speaker, 'state': self.state,
            'interrupted_by': list(self.interrupted_by),
            'parts': [{'text': r.text, 'start_ms': r.start_ms, 'end_ms': r.end_ms,
                       'final': r.final, 'overlap': r.overlap} for r in self.rows]}


class UtteranceOrganizer:
    def __init__(self, *, max_records=128, continuation_ms=4000):
        self.records = {}
        self.sources = {}
        self.active = {}
        self.serial = 0
        self.max_records, self.continuation_ms = max_records, continuation_ms

    @staticmethod
    def speaker(row):
        if row.identity_uncertain or row.overlap:
            return None
        if row.voiceprint_id:
            return ('voice', row.voiceprint_id)
        if row.speaker_cluster_id:
            return ('cluster', row.speaker_cluster_id)
        return None  # provider/window ids alone do not prove persistent identity

    def observe(self, rows):
        changed = {}
        for row in sorted(rows, key=lambda r: (r.start_ms, r.end_ms)):
            source = (row.start_ms, row.end_ms)
            target = self.sources.get(source)
            if row.replaces:
                targets = {self.sources.get(tuple(span)) for span in row.replaces}
                targets.discard(None)
                # Cross-person revisions require an explicit unresolved record.
                target = next(iter(targets)) if len(targets) == 1 else None
            key = self.speaker(row)
            record = self.records.get(target)
            if record is None and key is not None and not row.replaces:
                record = self.records.get(self.active.get(key))
                if record and (record.state == 'complete' or not
                    0 <= row.start_ms-record.rows[-1].end_ms <= self.continuation_ms):
                    record = None
            if record is None:
                self.serial += 1
                record = UtteranceRecord(f'U{self.serial}', key)
                self.records[record.utterance_id] = record
            elif source in record.parts and record.parts[source] == row:
                continue
            for old in row.replaces:
                record.parts.pop(tuple(old), None)
            record.parts[source] = row
            record.version += 1
            if key is not None and record.speaker == key:
                self.active[key] = record.utterance_id
            record.state = 'unknown'  # ASR final is never expression completion
            for other in self.records.values():
                if (other is record or other.speaker is None or key is None
                        or other.speaker == key or not other.parts):
                    continue
                if (other.state != 'complete' and
                    0 <= row.start_ms-other.rows[-1].end_ms <= self.continuation_ms and
                    record.utterance_id not in other.interrupted_by):
                    other.interrupted_by.append(record.utterance_id)
                    other.state = 'interrupted'
                    other.version += 1
                    changed[other.utterance_id] = other
            self.sources[source] = record.utterance_id
            changed[record.utterance_id] = record
        while len(self.records) > self.max_records:
            old = next(iter(self.records))
            self.records.pop(old)
            self.sources = {s: u for s,u in self.sources.items() if u != old}
            self.active = {k: u for k,u in self.active.items() if u != old}
        return [r.view() for r in changed.values()]

    def material(self, limit=12):
        people = {}
        result = []
        budget = 4000
        selected = []
        for record in reversed(list(self.records.values())[-limit:]):
            text = ' / '.join(r.text for r in record.rows)[-800:]
            if len(text) > budget:
                break
            budget -= len(text)
            selected.append((record,text))
        for record,text in reversed(selected):
            person = people.setdefault(record.speaker, f'声音{len(people)+1}') if record.speaker else '待定声音'
            result.append({'utterance':record.utterance_id, 'person':person,
                'state':record.state, 'interrupted_by':list(record.interrupted_by),
                'text':text})
        return result


def coalesce_rows(rows):
    """Collapse adjacent same-person fragments, never across another voice or revisions."""
    from dataclasses import replace
    result = []
    for row in sorted(rows, key=lambda r:(r.start_ms,r.end_ms)):
        key = UtteranceOrganizer.speaker(row)
        if (result and key is not None and key == UtteranceOrganizer.speaker(result[-1])
                and not row.replaces and not result[-1].replaces
                and 0 <= row.start_ms-result[-1].end_ms <= 1200):
            previous = result.pop()
            result.append(replace(previous, text=previous.text+row.text, end_ms=row.end_ms,
                                  words=previous.words+row.words))
        else:
            result.append(row)
    return tuple(result)
