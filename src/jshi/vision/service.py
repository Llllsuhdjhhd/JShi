from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import time

from .local import ChangeDetector, YoloDetector
from .model import VisionModel


class VisionService:
    """One independent worker, at most one pending frame; consumers never wait."""
    def __init__(self, store, config, model=None, detector=None):
        self.store, self.config = store, config
        self.model = model or VisionModel(config)
        self.detector = detector
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='vision')
        self.model_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='vision-model')
        self.lock = Lock()
        self.pending = None
        self.future = None
        self.closed = False
        self.last_online = {}
        self.last_archive = {}
        self.last_frame = {}
        self.previous = {}
        self.on_update = None
        self.scene_context = None
        self.action_future = None
        self.dirty = {}
        self.online_future = None
        self.online_pending = None
        self.completed_actions = set()

    def submit(self, subject, data, captured, source='visual', *, clock=None):
        if len(data) > self.config.max_image_bytes:
            raise ValueError('image exceeds upload limit')
        with self.lock:
            if self.closed:
                raise RuntimeError('vision closed')
            self.pending = (subject, data, captured, source, clock)
            if self.future is None or self.future.done():
                self.future = self.executor.submit(self._drain)

    def _drain(self):
        while True:
            with self.lock:
                item, self.pending = self.pending, None
                if item is None:
                    self.future = None
                    return
            try:
                result = self.process(*item, background=True)
                if result and self.on_update:
                    self.on_update(result)
            except Exception as exc:
                self.store.error(item[0], exc)
                self.store.cleanup()

    def process(self, subject, data, captured, source='visual', clock=None, *, background=False):
        frame = self.store.add_frame(subject, data, captured, source, **(clock or {}))
        old_frame = self.last_frame.get(subject)
        self.last_frame[subject] = frame
        if old_frame:
            self.store.discard_frame(subject, old_frame)
        normalized = self.store.image(frame, subject)
        # Detector state must not cross subjects or physical input sources.
        key = (subject, source)
        if self.detector is None or self.previous.get('_detector_key') != key:
            if self.detector is None or '_detector_key' in self.previous:
                self.detector = YoloDetector(self.config.weights) if self.config.detector == 'yolo' else ChangeDetector()
            self.previous['_detector_key'] = key
        detections, changed = self.detector.detect(normalized)
        current = self.store.latest(subject)
        source_changed = current and current.get('envelope', {}).get('source') != source
        identity = lambda rows: sorted((d.get('class', ''), d.get('track') or 0) for d in rows)
        if source_changed or (changed and (not detections or any(d.get('confidence', 0) < .5 for d in detections)
                        or not current or identity(detections) != identity(current['detections']))):
            with self.lock:
                self.dirty[key] = captured
        online = time() - self.last_online.get(key, 0) >= self.config.cooldown_seconds and (
            not current or key in self.dirty or time() - self.last_online.get(key, 0) >= self.config.refresh_seconds)
        archive = time() - self.last_archive.get(key, 0) >= self.config.archive_seconds
        if not online and not archive and not changed:
            self.store.discard_frame(subject, frame)
            return None
        if archive:
            self.last_archive[key] = time()
            self.store.archive(frame)
        self.previous[key] = detections
        if online:
            self.last_online[key] = time()  # Also limits repeated API failures.
            if background:
                self._schedule_online(subject, frame, detections, key)
                self.store.cleanup()
                return None
            if not self.store.claim_call(subject):
                self.store.cleanup()
                self.store.error(subject, '视觉模型达到每小时调用上限，保留已有环境信息')
                return None
            description = self.model.describe(self.store.image(frame, subject), previous=current['description'] if current else '',
                **({'scene':self.scene_context()} if self.scene_context else {}))
            result = self.store.publish(subject, frame, description, detections, 'initial' if not current else 'change_or_refresh', self.config.model)
            self._clear_dirty(key, captured)
        elif current and changed:
            result = self.store.publish(subject, frame, current['description'], detections, 'local_detection', 'local',
                                        described_at=current['described_at'])
        else:
            result = None
        if not archive and not result:
            self.store.discard_frame(subject, frame)
        self.store.cleanup()
        return result

    def _clear_dirty(self, key, captured):
        with self.lock:
            if self.dirty.get(key, 0) <= captured:
                self.dirty.pop(key, None)

    def _schedule_online(self, subject, frame, detections, key):
        with self.lock:
            if self.closed:
                return
            self.store.protect(frame)
            item = (subject, frame, detections, key)
            if self.online_future is not None and not self.online_future.done():
                if self.online_pending:
                    self.store.protect(self.online_pending[1], False)
                self.online_pending = item
            else:
                self.online_future = self.model_executor.submit(self._online_one, item)

    def _online_one(self, item):
        subject, frame, detections, key = item
        try:
            if not self.store.claim_call(subject):
                raise RuntimeError('视觉模型达到每小时调用上限')
            current = self.store.latest(subject)
            description = self.model.describe(self.store.image(frame, subject), previous=current['description'] if current else '',
                **({'scene':self.scene_context()} if self.scene_context else {}))
            result = self.store.publish(subject, frame, description, detections, 'initial' if not current else 'change_or_refresh', self.config.model)
            self._clear_dirty(key, self.store.frame(frame, subject)['captured'])
            if result and self.on_update:
                self.on_update(result)
        except Exception as exc:
            self.store.error(subject, exc)
        finally:
            self.store.protect(frame, False)
            self.store.cleanup()
            with self.lock:
                next_item, self.online_pending = self.online_pending, None
                if next_item and not self.closed:
                    self.online_future = self.model_executor.submit(self._online_one, next_item)
                else:
                    if next_item:
                        self.store.protect(next_item[1], False)
                    self.online_future = None

    def recall_image(self, subject, frame, question):
        image = self.store.image(frame, subject)
        if not self.store.claim_call(subject):
            raise RuntimeError('视觉模型达到每小时调用上限')
        return self.model.describe(image, question=question, **({'scene':self.scene_context()} if self.scene_context else {}))

    def request_action(self, subject, action):
        import re
        # Physical aiming is intentionally unsupported by this portable input adapter.
        if not re.search(r'观察|查看|仔细看|回看照片', action) or re.search(r'转头|转向|转动|看向|朝.{0,20}看', action):
            return False
        with self.lock:
            if self.closed or (self.action_future is not None and not self.action_future.done()):
                return False
            match = re.search(r'回看照片\s+([0-9a-f]{32})[：:]?\s*(.*)', action)
            current = self.store.latest_sample(subject)
            frame = match[1] if match else (current['frame'] if current else '')
            if not frame:
                return False
            action_key = (subject, frame, action)
            if action_key in self.completed_actions:
                return False
            try:
                self.store.frame(frame, subject)
            except KeyError:
                return False
            self.store.protect(frame)
            self.completed_actions.add(action_key)
            if len(self.completed_actions) > 256:
                self.completed_actions = {action_key}
            self.action_future = self.model_executor.submit(self._action, subject, frame, match[2] if match else action,
                                                      'recall_image' if match else 'observation_result')
        return True

    def _action(self, subject, frame, question, reason):
        try:
            description = self.recall_image(subject, frame, question)
            if reason == 'recall_image':
                description = '【历史照片重新解读，不是当前现场】\n' + description
            result = self.store.publish(subject, frame, description, reason=reason, model=self.config.model, make_current=False)
            if result and self.on_update:
                self.on_update(result)
            return result
        except Exception as exc:
            self.store.error(subject, exc)
            return None
        finally:
            self.store.protect(frame, False)

    def close(self):
        with self.lock:
            self.closed = True
            self.pending = None
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.model_executor.shutdown(wait=True, cancel_futures=False)
