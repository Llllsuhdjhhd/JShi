"""Sidecar media associations work with both in-process and REMS backends."""
from dataclasses import replace
from threading import Lock


class VisualMemory:
    def __init__(self, backend, store):
        self.backend, self.store = backend, store
        self.lock = Lock()

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def ingest_batch(self, batch):
        with self.lock:
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
                        import json
                        change = '【本地画面位置观察，时间 ' + str(s['captured']) + '】' + json.dumps(s['detections'], ensure_ascii=False)
                        if change not in seen:
                            additions.append(change)
                            seen.add(change)
                experiences.append(replace(e, text=e.text + ('\n' + '\n'.join(additions) if additions else '')))
            result = self.backend.ingest_batch(replace(batch, experiences=tuple(experiences)))
            self.store.record_delivery(batch.subject_id, associations, result.stored_marks)
            return result

    def recall(self, subject_id, query, **kwargs):
        fragments = self.backend.recall(subject_id, query, **kwargs)
        result = []
        for f in fragments:
            ss = self.store.associated(subject_id, f.source_ids, f.event_id)
            photo_notes = '\n'.join(self.store.render(s) + '\n' + (
                f"关联照片：{s['frame']}；描述来源照片：{s['description_frame']}；可用动作‘回看照片 {s['frame']}：观察问题’按需解读。" if s['image_available'] else '关联照片已不可用。') for s in ss)
            result.append(replace(f, visual_observations=tuple(ss), content=(f.content or f.text) + ('\n' + photo_notes if photo_notes else '')))
        return tuple(result)
