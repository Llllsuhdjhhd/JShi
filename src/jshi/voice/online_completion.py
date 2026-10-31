"""Resolve online boundary/acoustic conflicts with bounded local text inference."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from time import monotonic
import re

from .turns import TurnDecision, checked_decision


def open_tail(rows):
    """A conservative continuation hint, not a grammar/completion classifier.

    Ignore hesitation at the tail, but do not strip meaningful sentence words.
    Quoted/standalone particles and common self-contained answers are excluded.
    """
    text = ''.join(row.text.strip() for row in rows).rstrip('，,。.!！?？；;… ')
    text = re.sub(r'(?:[，,\s]*(?:呃|额|嗯))+$', '', text).rstrip('，, ')
    if len(text) < 4 or text.endswith(('是的', '好的', '对的', '我的', '你的', '他的', '她的')):
        return False
    return text.endswith(('的', '一定要', '需要', '必须', '按照', '比如', '因为', '所以'))


@dataclass(frozen=True)
class OnlineCompletionDecision(TurnDecision):
    correction: dict = field(default_factory=dict)


class OnlineCompletionPolicy:
    """A cloud final is a candidate fragment boundary, not semantic completion.

    Open textual tails request bounded continuation. Otherwise acoustic
    'incomplete' invokes text; uncertainty releases without asserting complete.
    The unvalidated Namo model cannot promote acoustic incomplete to complete.
    """
    def __init__(self, audio, semantic=None, *, timeout_s=.2):
        self.audio, self.semantic, self.timeout_s = audio, semantic, timeout_s
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='online-text-turn')
        self.future = None

    def analyze_audio(self, samples):
        return self.audio.analyze_audio(samples)

    def decide(self, rows, audio):
        audio = checked_decision(self.audio.decide(rows, audio))
        evidence = dict(cloud_boundary='candidate_final', acoustic=audio.state,
                        acoustic_probability=audio.probability, acoustic_reason=audio.reason,
                        semantic_available=self.semantic is not None,
                        semantic_called=False, semantic='not_needed')
        if open_tail(rows):
            evidence['continuation_hint'] = 'open_text_tail'
            return OnlineCompletionDecision('incomplete', None,
                'online_open_tail_wait_bounded', evidence)
        if audio.state != 'incomplete':
            return OnlineCompletionDecision(audio.state, audio.probability, audio.reason, evidence)
        semantic = TurnDecision('unknown', reason='semantic_disabled_or_unavailable')
        started = monotonic()
        if self.semantic is not None:
            if self.future is not None and not self.future.done():
                semantic = TurnDecision('unknown', reason='semantic_busy')
            else:
                snapshot = tuple(rows)
                self.future = self.executor.submit(self.semantic.analyze_text, snapshot)
                evidence['semantic_called'] = True
                try:
                    semantic = checked_decision(self.future.result(timeout=self.timeout_s))
                except TimeoutError:
                    semantic = TurnDecision('unknown', reason='semantic_timeout')
                except Exception as exc:
                    semantic = TurnDecision('unknown', reason='semantic_error:'+type(exc).__name__)
        evidence.update(semantic=semantic.state, semantic_reason=semantic.reason,
                        semantic_ms=round((monotonic()-started)*1000,1),
                        semantic_budget_ms=self.timeout_s*1000)
        # The current Namo revision failed the local Chinese controls. Preserve
        # its observation, but do not let it overrule an incomplete acoustic tail.
        if semantic.state == 'complete' and getattr(self.semantic, 'name', '') == 'namo':
            evidence['semantic_authoritative'] = False
            return OnlineCompletionDecision('incomplete', None,
                'online_unvalidated_semantic_preserve_continuation', evidence)
        if semantic.state in {'complete','incomplete'}:
            return OnlineCompletionDecision(semantic.state, None,
                'online_semantic_supports_boundary' if semantic.state=='complete' else 'online_semantic_supports_audio', evidence)
        return OnlineCompletionDecision('unknown', None, 'online_conflict_unresolved_release', evidence)

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
