"""Replaceable turn policies and bounded continuation, independent of ASR/JEV.

Audio policies run alongside the single ASR batch; text policies run after ASR.
Original segments remain separate, with their speaker and audio positions intact.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol
import math

from .latency import elapsed_ms


@dataclass(frozen=True)
class TurnDecision:
    state: str  # complete, incomplete, unknown; never delete on invalid predictions
    probability: float | None = None
    reason: str = ''


class TurnPolicy(Protocol):
    def analyze_audio(self, samples) -> TurnDecision: ...
    def decide(self, transcripts, audio: TurnDecision) -> TurnDecision: ...


class EagerTurnPolicy:
    """No extra classifier: submit ready material to JEV without asserting completion."""
    name = 'eager'
    def analyze_audio(self, samples):
        return TurnDecision('unknown', reason='ready_for_jev')
    def decide(self, transcripts, audio):
        return audio


class SmartTurnPolicy(EagerTurnPolicy):
    name = 'smart_turn'
    def __init__(self, path, *, threshold=.5, threads=2):
        if not path.is_file():
            raise ValueError('Smart Turn模型未准备，请安装.[voice-turn]并运行 jshi voice-setup --turn；或选JSHI_VOICE_TURN_STRATEGY=eager')
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(str(path), sess_options=options,
                                            providers=['CPUExecutionProvider'])
        self.extractor = WhisperFeatureExtractor(chunk_length=8)
        self.threshold = threshold

    def analyze_audio(self, samples):
        import numpy as np
        audio = np.asarray(samples, dtype='float32')[-8*16000:]
        audio = np.pad(audio, (max(0, 8*16000-len(audio)), 0))
        features = self.extractor(audio, sampling_rate=16000, return_tensors='np',
            padding='max_length', max_length=8*16000, truncation=True,
            do_normalize=True).input_features.astype('float32')
        probability = float(np.asarray(self.session.run(None, {'input_features':features})[0]).reshape(-1)[0])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('Smart Turn返回无效概率')
        return TurnDecision('complete' if probability >= self.threshold else 'incomplete',
                            probability, 'smart_turn')


TURN_POLICIES = {'eager': lambda config, root: EagerTurnPolicy(),
    'smart_turn': lambda config, root: SmartTurnPolicy(
        root/'smart-turn-v3.2-cpu.onnx', threshold=config.turn_threshold)}


def build_turn_policy(config, root):
    """Registry is the extension point for audio, text or combined strategies."""
    return TURN_POLICIES[config.turn_strategy](config, root)


@dataclass
class TurnFragment:
    row: object
    audio: object
    timing: dict


@dataclass
class PendingTurn:
    fragments: list = field(default_factory=list)
    touched: float = 0

    @property
    def rows(self): return [f.row for f in self.fragments]
    @property
    def audios(self): return [f.audio for f in self.fragments]
    @property
    def timings(self): return [f.timing for f in self.fragments]


@dataclass(frozen=True)
class TurnTicket:
    grouped: dict
    future: object
    deadline: float
    generation: int


def checked_decision(decision):
    if not isinstance(decision, TurnDecision) or decision.state not in {'complete','incomplete','unknown'}:
        return TurnDecision('unknown', reason='invalid_decision_preserved')
    if decision.probability is not None and (not math.isfinite(decision.probability)
                                             or not 0 <= decision.probability <= 1):
        return TurnDecision('unknown', reason='invalid_probability_preserved')
    return decision


def fuse_decisions(audio, semantic=None):
    """Agreement strengthens evidence; never average uncalibrated probabilities."""
    audio = checked_decision(audio)
    if semantic is None:
        return audio
    semantic = checked_decision(semantic)
    valid = {'complete','incomplete'}
    if audio.state in valid and semantic.state in valid:
        if audio.state == semantic.state:
            return TurnDecision(audio.state, reason='agree_'+audio.state)
        return TurnDecision('unknown', reason='conflict_to_jev')
    if audio.state in valid:
        return TurnDecision(audio.state, audio.probability, 'audio_only')
    if semantic.state in valid:
        return TurnDecision(semantic.state, semantic.probability, 'semantic_only')
    return TurnDecision('unknown', reason='no_turn_evidence')


class TurnCoordinator:
    def __init__(self, policy, *, semantic=None, timeout_s=.6, semantic_timeout_s=.2,
                 max_hold_s=2, max_audio_s=12):
        self.policy, self.semantic = policy, semantic
        self.timeout_s, self.semantic_timeout_s = timeout_s, semantic_timeout_s
        self.max_hold_s, self.max_audio_s = max_hold_s, max_audio_s
        self.pending = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-turn')
        self.semantic_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jshi-semantic') if semantic else None
        self.future = self.semantic_future = None
        self.generation = 0
        self.released = {}  # bounded source-span dedupe, never text-based dedupe
        self.last_events = []

    @staticmethod
    def key(segment):
        if segment.overlap or getattr(segment, 'identity_uncertain', False):
            return None
        if segment.voiceprint_id:
            return ('voice', segment.voiceprint_id)
        if segment.speaker_cluster_id:
            return ('cluster', segment.speaker_cluster_id)
        return None

    @staticmethod
    def span(row):
        return (row.start_ms,row.end_ms,row.track_id,getattr(row,'audio_kind','original'))

    @staticmethod
    def discontinuous(previous, current):
        return current.start_ms - previous.end_ms > 4000 or current.start_ms < previous.end_ms

    def begin(self, segments):
        import numpy as np
        self.last_events = []
        grouped = {}
        for segment in segments:
            key = self.key(segment)
            if key is not None:
                grouped.setdefault(key, []).append(segment)
        contexts = {}
        for key, items in grouped.items():
            previous = self.pending.get(key)
            parts = [(f.row,f.audio) for f in previous.fragments] if previous else []
            for item in sorted(items,key=lambda r:(r.start_ms,r.end_ms)):
                same = next((i for i,(r,a) in enumerate(parts) if self.span(r)==self.span(item)),None)
                if same is not None:
                    parts[same] = (item,item.samples)
                else:
                    if parts and self.discontinuous(parts[-1][0],item):
                        parts = []
                    parts.append((item,item.samples))
            if parts:
                contexts[key] = np.concatenate([a for r,a in parts])[-8*16000:]
        if self.future is not None and not self.future.done():
            return TurnTicket(grouped,None,monotonic(),self.generation)
        def analyze():
            started = monotonic()
            decisions = {}
            for key, audio in contexts.items():
                try:
                    decisions[key] = checked_decision(self.policy.analyze_audio(audio))
                except Exception as exc:
                    decisions[key] = TurnDecision('unknown',reason='audio_error:'+type(exc).__name__)
            return decisions, elapsed_ms(monotonic(),started)
        deadline = monotonic()+self.timeout_s
        self.future = self.executor.submit(analyze)
        return TurnTicket(grouped,self.future,deadline,self.generation)

    def _semantic_start(self, keys):
        deadline = monotonic()+self.semantic_timeout_s
        if self.semantic is None or (self.semantic_future is not None and not self.semantic_future.done()):
            return None,deadline
        # Immutable row snapshots; a late result cannot read or mutate the next turn.
        snapshots = {key:tuple(self.pending[key].rows) for key in keys if key in self.pending}
        def analyze():
            started = monotonic()
            decisions = {}
            for key, rows in snapshots.items():
                try:
                    decisions[key] = checked_decision(self.semantic.analyze_text(rows))
                except Exception as exc:
                    decisions[key] = TurnDecision('unknown',reason='semantic_error:'+type(exc).__name__)
            return decisions,elapsed_ms(monotonic(),started)
        self.semantic_future = self.semantic_executor.submit(analyze)
        return self.semantic_future,deadline

    @staticmethod
    def _result(future, deadline):
        if future is not None:
            try:
                return future.result(timeout=max(0,deadline-monotonic()))
            except TimeoutError:
                pass
        return {},None

    def finish(self, ticket, segments, rows, timings, *, final=False):
        if ticket.generation != self.generation:
            return (),[]  # Reset/old session: never publish a late result.
        waiting = monotonic()
        samples = {(s.start_ms,s.end_ms,s.track_id):s.samples for s in segments}
        ready,ready_timings = [],[]
        for row,timing in sorted(zip(rows,timings),key=lambda item:(item[0].start_ms,item[0].end_ms)):
            if getattr(row,'replaces',()):
                ready.append(row);ready_timings.append({**timing,'turn_state':'unknown','turn_reason':'asr_revision'})
                continue
            span = self.span(row)
            if span in self.released:
                continue
            key = self.key(row)
            # Same physical span with revised identity is one fragment, not a
            # second person's new utterance. Move only that span; never infer
            # continuity for the rest of the old speaker's pending turn.
            for other_key, other in list(self.pending.items()):
                if other_key != key:
                    other.fragments[:] = [f for f in other.fragments if self.span(f.row) != span]
                    if not other.fragments:
                        self.pending.pop(other_key)
            if key is None:
                ready.append(row);ready_timings.append({**timing,'turn_state':'unknown','turn_reason':'unresolved_or_overlap'})
                self._remember_released(row)
                continue
            pending = self.pending.get(key)
            same = next((i for i,f in enumerate(pending.fragments) if self.span(f.row)==span),None) if pending else None
            if pending and same is None and self.discontinuous(pending.rows[-1],row):
                self._release(key,ready,ready_timings,'continuation_gap_or_overlap')
                pending = None
            pending = self.pending.setdefault(key,PendingTurn())
            fragment = TurnFragment(row,samples[(row.start_ms,row.end_ms,row.track_id)],dict(timing))
            if same is not None:
                pending.fragments[same] = fragment
            else:
                pending.fragments.append(fragment)
            pending.touched = monotonic()
        # ASR is ready. Start text inference BEFORE waiting for the audio branch.
        semantic_future,semantic_deadline = self._semantic_start(ticket.grouped)
        decisions,compute_ms = self._result(ticket.future,ticket.deadline)
        semantic_wait = monotonic()
        semantic_decisions,semantic_ms = self._result(semantic_future,semantic_deadline)
        semantic_join_ms = elapsed_ms(monotonic(),semantic_wait)
        extra_ms = elapsed_ms(monotonic(),waiting)
        for key in ticket.grouped:
            pending = self.pending.get(key)
            if not pending:
                continue
            audio = decisions.get(key,TurnDecision('unknown',reason='audio_timeout_or_unavailable'))
            try:
                audio = checked_decision(self.policy.decide(tuple(pending.rows),audio))
            except Exception as exc:
                audio = TurnDecision('unknown',reason='audio_policy_error:'+type(exc).__name__)
            semantic = semantic_decisions.get(key,TurnDecision('unknown',reason='semantic_timeout_or_unavailable')) if self.semantic else None
            decision = fuse_decisions(audio,semantic)
            metadata = {'turn_state':decision.state,'turn_probability':decision.probability,
                'turn_reason':decision.reason,'turn_audio_ms':compute_ms,'turn_join_wait_ms':extra_ms,
                'turn_audio_state':audio.state,'turn_audio_probability':audio.probability,'turn_audio_reason':audio.reason,
                'turn_semantic_enabled':self.semantic is not None,
                'turn_semantic_state':semantic.state if semantic else 'disabled',
                'turn_semantic_probability':semantic.probability if semantic else None,
                'turn_semantic_reason':semantic.reason if semantic else 'disabled',
                'turn_semantic_ms':semantic_ms,'turn_semantic_join_wait_ms':semantic_join_ms}
            self.last_events.append({'state':decision.state,'probability':decision.probability,
                'reason':decision.reason,'segments':len(pending.rows),'audio_ms':compute_ms,
                'join_wait_ms':extra_ms,**metadata})
            for t in pending.timings:
                t.update(metadata)
            duration = sum(len(a) for a in pending.audios)/16000
            if decision.state != 'incomplete':
                self._release(key,ready,ready_timings,decision.reason)
            elif duration >= self.max_audio_s or len(pending.rows)>=32:
                self._release(key,ready,ready_timings,'pending_limit')
        self.poll(ready,ready_timings,final=final)
        ordered = sorted(zip(ready,ready_timings),key=lambda pair:(pair[0].start_ms,pair[0].end_ms))
        return tuple(r for r,t in ordered),[t for r,t in ordered]

    def _remember_released(self, row):
        self.released[self.span(row)] = True
        while len(self.released)>256:
            self.released.pop(next(iter(self.released)))

    def _release(self, key, rows, timings, reason):
        pending = self.pending.pop(key)
        rows.extend(pending.rows)
        now = monotonic()
        timings.extend({**t,'turn_release_reason':reason,
            'turn_hold_ms':elapsed_ms(now,t.get('_asr_return_mono'))} for t in pending.timings)
        for row in pending.rows:
            self._remember_released(row)

    def poll(self, rows, timings, *, final=False, allow_expiry=True):
        for key,pending in list(self.pending.items()):
            if final or (allow_expiry and monotonic()-pending.touched>=self.max_hold_s):
                self._release(key,rows,timings,'capture_end' if final else 'hold_limit')

    def reset(self):
        self.generation += 1
        self.pending.clear()
        self.released.clear()
        self.last_events = []
        for future in (self.future,self.semantic_future):
            if future:
                future.cancel()

    def close(self):
        self.executor.shutdown(wait=True,cancel_futures=True)
        if self.semantic_executor:
            self.semantic_executor.shutdown(wait=True,cancel_futures=True)
