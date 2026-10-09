"""Sidecar media associations work with both in-process and REMS backends."""
from dataclasses import replace
from threading import Lock
from time import perf_counter
from datetime import datetime, timezone
import json
import logging


class VisualMemory:
    def __init__(self, backend, store):
        self.backend, self.store = backend, store
        self.lock = Lock()

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def ingest_batch(self, batch):
        timings = {'started_at': datetime.now(timezone.utc).isoformat(), 'operation': 'ingest', 'subject_id': batch.subject_id}
        started = perf_counter()
        try:
            with self.lock:
                timings['lock_wait_ms'] = round((perf_counter() - started) * 1000, 3)
                return self._ingest_batch(batch, timings)
        finally:
            self._record_timings(timings, started)

    def _ingest_batch(self, batch, timings):
        prepared = perf_counter()
        seen = set()
        associations = {}
        experiences = []
        for e in batch.experiences:
            ss = self.store.associated(batch.subject_id, e.source_ids)
            associations[e.segment_id] = ss
            additions = []
            for s in ss:
                # Same semantic description is included once across local updates.
                key = s['description']
                if key not in seen and not self.store.description_delivered(batch.subject_id, key, s['described_at']):
                    additions.append(self.store.render(s))
                    seen.add(key)
                elif s['reason'] == 'local_detection' and not self.store.delivered(batch.subject_id, s['id']):
                    change = '【本地画面位置观察，时间 ' + str(s['captured']) + '】' + json.dumps(s['detections'], ensure_ascii=False)
                    if change not in seen:
                        additions.append(change)
                        seen.add(change)
            experiences.append(replace(e, text=e.text + ('\n' + '\n'.join(additions) if additions else '')))
        timings['visual_prepare_ms'] = round((perf_counter() - prepared) * 1000, 3)
        backend_started = perf_counter()
        result = self.backend.ingest_batch(replace(batch, experiences=tuple(experiences)))
        timings['backend_ms'] = round((perf_counter() - backend_started) * 1000, 3)
        delivery_started = perf_counter()
        self.store.record_delivery(batch.subject_id, associations, result.stored_marks)
        timings['visual_delivery_ms'] = round((perf_counter() - delivery_started) * 1000, 3)
        timings['status'] = 'succeeded'
        return result

    def recall(self, subject_id, query, **kwargs):
        timings = {'started_at': datetime.now(timezone.utc).isoformat(), 'operation': 'recall', 'subject_id': subject_id,
                   'query_chars': len(query), 'limit': kwargs.get('limit')}
        started = perf_counter()
        try:
            return self._recall(subject_id, query, timings, **kwargs)
        finally:
            self._record_timings(timings, started)

    def _recall(self, subject_id, query, timings, **kwargs):
        backend_started = perf_counter()
        fragments = self.backend.recall(subject_id, query, **kwargs)
        timings['backend_ms'] = round((perf_counter() - backend_started) * 1000, 3)
        timings['returned_count'] = len(fragments)
        visual_started = perf_counter()
        result = []
        for f in fragments:
            ss = self.store.associated(subject_id, f.source_ids, f.event_id)
            photo_notes = '\n'.join(self.store.render(s) + '\n' + (
                f"关联照片：{s['frame']}；描述来源照片：{s['description_frame']}；可用动作‘回看照片 {s['frame']}：观察问题’按需解读。" if s['image_available'] else '关联照片已不可用。') for s in ss)
            result.append(replace(f, visual_observations=tuple(ss), content=(f.content or f.text) + ('\n' + photo_notes if photo_notes else '')))
        timings['visual_associations_ms'] = round((perf_counter() - visual_started) * 1000, 3)
        timings['status'] = 'succeeded'
        return tuple(result)

    def _record_timings(self, timings, started):
        timings.setdefault('status', 'failed')
        timings['total_ms'] = round((perf_counter() - started) * 1000, 3)
        timings['finished_at'] = datetime.now(timezone.utc).isoformat()
        try:
            path = self.store.root.parent / 'memory_phase_timings.jsonl'
            with path.open('a', encoding='utf-8') as out:
                out.write(json.dumps(timings, ensure_ascii=False) + '\n')
        except Exception:
            logging.getLogger(__name__).warning('Memory phase timing could not be saved', exc_info=True)
