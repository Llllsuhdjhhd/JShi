"""事项、判断和播放许可。判断只提供关系；能否播放由这里决定。"""
from dataclasses import dataclass, field


def correction_grade(premise_changed, compatible, continuation):
    """Reuse an old analysis only when preparation still fits and the premise is unchanged."""
    if premise_changed or (compatible and continuation == 'restart'):
        return 'restart'
    if compatible and continuation == 'revise':
        return 'revise'
    if compatible and continuation == 'reuse':
        return 'reuse'
    return 'reconstruct'


def keeps_old_work(relation, old_work):
    """Only a confident continuation, correction or stop may disturb running work.

    New speech is judged as its own input afterwards, so an unclear relation keeps
    both the old work and the new speech instead of merging and re-judging them.
    """
    return relation in {'background', 'independent', 'uncertain'} and old_work != 'stop'


@dataclass(frozen=True)
class Effect:
    kind: str
    matter_id: str = ''
    attempt_id: str = ''
    answer_id: str = ''
    input_version: int = 0
    basis_version: int | None = None
    approval_version: int | None = None
    reason: str = ''
    grade: str = ''
    resume: bool = False
    immediate: bool = False
    seq: int = 0
    covered: tuple = ()
    persist: tuple = ()
    cleared: tuple = ()


@dataclass(frozen=True)
class Scope:
    input_version: int
    pending: tuple
    reason_ids: frozenset
    speech_seq: int


@dataclass
class Reason:
    reason_id: str
    kind: str
    seq: int


@dataclass
class Answer:
    answer_id: str
    basis_version: int
    eligible: bool = True
    played: bool = False


@dataclass
class Attempt:
    attempt_id: str
    covers: tuple
    execution: str = 'open'
    finished: bool = False
    orphan: bool = False


@dataclass
class Matter:
    matter_id: str
    input_version: int = 0
    pending: list = field(default_factory=list)
    inputs: dict = field(default_factory=dict)
    answers: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)
    played: list = field(default_factory=list)
    tasks: dict = field(default_factory=dict)
    closed: bool = False
    cancelled: bool = False
    seq: int = 0
    speech_seq: int = 0
    persist_queue: list = field(default_factory=list)
    persist_failed: bool = False
    unclear: bool = False
    asr_pending: bool = False
    transient_token: int = 0
    current_attempt: str = ''


class TurnSchedule:
    def __init__(self, *, speech_hold=False, max_leftover=1, transient_confirm_s=0.8, asr_wait_s=3.0):
        self.speech_hold = speech_hold
        self.max_leftover = max_leftover
        self.transient_confirm_s = transient_confirm_s
        self.asr_wait_s = asr_wait_s
        self.matters = {}
        self.current_id = None
        self._n = 0

    def _new(self, prefix):
        self._n += 1
        return f'{prefix}{self._n}'

    def current(self):
        return self.matters.get(self.current_id) if self.current_id else None

    def open_matter(self, matter_id=None):
        matter = Matter(matter_id or self._new('m'))
        self.matters[matter.matter_id] = matter
        self.current_id = matter.matter_id
        return matter

    def _matter(self, matter_id=None):
        if matter_id:
            return self.matters.get(matter_id)
        return self.current()

    def busy(self, matter=None):
        matter = matter or self.current()
        if matter is None or matter.closed:
            return False
        if matter.cancelled or matter.unclear or matter.asr_pending:
            return True
        if any(item.execution in {'open', 'held'} and not item.finished for item in matter.attempts.values()):
            return True
        if any(item.eligible and not item.played for item in matter.answers.values()):
            return True
        return matter.tasks.get('cognition') == 'running' or matter.tasks.get('preparation') == 'running'

    def orphan_count(self):
        return sum(item.orphan and not item.finished for matter in self.matters.values() for item in matter.attempts.values())

    def at_cap(self):
        return self.orphan_count() >= self.max_leftover

    def _live_count(self):
        return sum(item.execution in {'open', 'held'} and not item.finished and not item.orphan
                   for matter in self.matters.values() for item in matter.attempts.values())

    def begin_attempt(self, covers, *, matter_id=None):
        # One current entry may run beside the leftover call. A further entry waits.
        if self.orphan_count() > self.max_leftover or (self.at_cap() and self._live_count()):
            return None
        matter = self._matter(matter_id)
        if matter is None or matter.closed:
            matter = self.open_matter(matter_id)
        attempt = Attempt(self._new('a'), tuple(covers))
        matter.attempts[attempt.attempt_id] = attempt
        matter.current_attempt = attempt.attempt_id
        matter.tasks['entry'] = 'running'
        if covers and matter.input_version == 0:
            matter.input_version = 1
        return attempt

    def attempt_finished(self, attempt_id):
        for matter in self.matters.values():
            attempt = matter.attempts.get(attempt_id)
            if attempt:
                attempt.finished = True
                attempt.orphan = False
                return attempt
        return None

    def add_answer(self, answer_id, basis_version, *, matter_id=None):
        matter = self._matter(matter_id) or self.open_matter(matter_id)
        matter.answers[answer_id] = Answer(answer_id, basis_version)
        return matter.answers[answer_id]

    def set_task(self, name, state, *, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None or (matter.closed and state == 'running'):
            matter = self.open_matter()
        matter.tasks[name] = state
        return matter

    def note_text(self, input_id, text='', *, matter_id=None, active_mode=True):
        matter = self._matter(matter_id)
        if not self.busy(matter):
            return Effect('normal_entry', matter_id=matter.matter_id if matter else '')
        if matter.inputs.get(input_id) == text:
            return Effect('none', matter_id=matter.matter_id)
        matter.input_version += 1
        matter.inputs[input_id] = text
        if input_id not in matter.pending:
            matter.pending.append(input_id)
        matter.transient_token += 1
        attempt = matter.attempts.get(matter.current_attempt)
        if active_mode and attempt and attempt.execution == 'open':
            attempt.execution = 'held'
            return Effect('hold_submission', matter_id=matter.matter_id, attempt_id=attempt.attempt_id,
                          input_version=matter.input_version)
        return Effect('text', matter_id=matter.matter_id, input_version=matter.input_version,
                      attempt_id=attempt.attempt_id if attempt else '')

    def capture(self, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None:
            return Scope(0, (), frozenset(), 0)
        return Scope(matter.input_version, tuple(matter.pending), frozenset(matter.reasons), matter.speech_seq)

    def short_voice(self, matter_id=None):
        if not self.speech_hold:
            return Effect('ignored')
        matter = self._matter(matter_id) or self.open_matter(matter_id)
        return self._reason(matter, 'block_next', 'block_next')

    def sustained_voice(self, matter_id=None):
        if not self.speech_hold:
            return Effect('ignored')
        matter = self._matter(matter_id) or self.open_matter(matter_id)
        return self._reason(matter, 'pause', 'pause')

    def _reason(self, matter, kind, effect_kind):
        matter.transient_token += 1
        matter.speech_seq += 1
        reason_id = self._new('r')
        matter.reasons[reason_id] = Reason(reason_id, kind, matter.speech_seq)
        matter.seq += 1
        return Effect(effect_kind, matter_id=matter.matter_id, reason=reason_id, seq=matter.seq,
                      input_version=matter.input_version)

    def note_short_end(self, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None:
            return 0
        matter.transient_token += 1
        return matter.transient_token

    def confirm_transient(self, token, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None or matter.transient_token != token or matter.asr_pending or matter.unclear:
            return Effect('none', matter_id=matter.matter_id if matter else '')
        removed = [reason_id for reason_id, reason in matter.reasons.items() if reason.kind == 'block_next']
        for reason_id in removed:
            del matter.reasons[reason_id]
        matter.seq += 1
        return Effect('clear_block', matter_id=matter.matter_id, reason=removed[-1] if removed else '',
                      seq=matter.seq, resume=self._can_resume(matter), input_version=matter.input_version, cleared=tuple(removed))

    def note_sustained_end(self, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None:
            return Effect('none')
        matter.asr_pending = True
        matter.transient_token += 1
        return Effect('await_asr', matter_id=matter.matter_id, input_version=matter.input_version, resume=False)

    def expire_asr_wait(self, token, matter_id=None):
        """Sustained sound that produced no text within the wait limit releases its hold."""
        matter = self._matter(matter_id)
        if (matter is None or matter.transient_token != token or not matter.asr_pending
                or matter.pending or matter.unclear):
            return Effect('none', matter_id=matter.matter_id if matter else '')
        matter.asr_pending = False
        removed = [reason_id for reason_id, reason in matter.reasons.items() if reason.kind in {'pause', 'block_next'}]
        for reason_id in removed:
            del matter.reasons[reason_id]
        matter.seq += 1
        return Effect('clear_block', matter_id=matter.matter_id, reason=removed[-1] if removed else '',
                      seq=matter.seq, resume=self._can_resume(matter), input_version=matter.input_version, cleared=tuple(removed))

    def asr_failed(self, matter_id=None):
        matter = self._matter(matter_id) or self.open_matter(matter_id)
        matter.unclear = True
        matter.asr_pending = False
        matter.transient_token += 1
        return Effect('unclear', matter_id=matter.matter_id, resume=False, input_version=matter.input_version)

    def apply_collision(self, relation, old_work, covered, scope, *, mode='active', attempt_id=None, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None:
            return Effect('normal_entry')
        attempt = matter.attempts.get(attempt_id or matter.current_attempt)
        if attempt is None:
            return Effect('normal_entry', matter_id=matter.matter_id)
        if attempt.execution == 'revoked':
            return Effect('diagnostic', matter_id=matter.matter_id, attempt_id=attempt.attempt_id,
                          input_version=matter.input_version)
        if mode != 'active':
            return Effect('observe', matter_id=matter.matter_id, attempt_id=attempt.attempt_id,
                          input_version=matter.input_version)
        covered = tuple(covered)
        if scope.input_version != matter.input_version or set(matter.pending) - set(covered):
            return Effect('stale', matter_id=matter.matter_id, attempt_id=attempt.attempt_id,
                          input_version=matter.input_version, covered=covered)
        if relation == 'stop' and old_work == 'stop':
            return self._cancel(matter, attempt, 'collision_stop')
        if keeps_old_work(relation, old_work):
            return self._reapprove(matter, attempt, scope)
        premise_changed = relation == 'replace'
        if self.at_cap():
            attempt.execution = 'held'
            return Effect('capped', matter_id=matter.matter_id, attempt_id=attempt.attempt_id,
                          input_version=matter.input_version, grade='restart' if premise_changed else 'reconstruct')
        self._revoke(matter, attempt)
        grade = correction_grade(premise_changed, False, None)
        if premise_changed:
            self._drop_unplayed(matter)
        return Effect('replace_entry', matter_id=matter.matter_id, attempt_id=attempt.attempt_id,
                      input_version=matter.input_version, grade=grade, covered=tuple(matter.inputs),
                      basis_version=self._basis(matter))

    def apply_impact(self, premise_changed, compatible, continuation, *, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None:
            return Effect('reconstruct')
        grade = correction_grade(premise_changed, compatible, continuation)
        if grade in {'restart', 'revise', 'reconstruct'}:
            self._drop_unplayed(matter)
        return Effect(grade, matter_id=matter.matter_id, grade=grade, input_version=matter.input_version,
                      basis_version=self._basis(matter))

    def apply_work_collision(self, relation, old_work, scope, *, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None or scope.input_version != matter.input_version:
            return Effect('stale')
        if relation == 'stop' and old_work == 'stop':
            return self._cancel(matter, None, 'work_collision_stop')
        if keeps_old_work(relation, old_work):
            attempt = matter.attempts.get(matter.current_attempt)
            effect = self._reapprove(matter, attempt, scope)
            if attempt is None or attempt.finished:
                matter.tasks['entry'] = 'done'
            return effect
        self._drop_unplayed(matter)
        return Effect('invalidate_work', matter_id=matter.matter_id, input_version=matter.input_version,
                      grade='restart' if relation == 'replace' else 'reconstruct')

    def keep_after_unclear(self, *, attempt_id=None, matter_id=None):
        """No usable relation (failure or input kept changing): the old work stays valid."""
        matter = self._matter(matter_id)
        if matter is None:
            return Effect('normal_entry')
        attempt = matter.attempts.get(attempt_id or matter.current_attempt)
        if attempt is not None and attempt.execution == 'revoked':
            return Effect('diagnostic', matter_id=matter.matter_id, attempt_id=attempt.attempt_id,
                          input_version=matter.input_version)
        effect = self._reapprove(matter, attempt, self.capture(matter.matter_id))
        if attempt is None or attempt.finished:
            matter.tasks['entry'] = 'done'
        return effect

    def ack(self, reply_id, segment, *, final=False, quick=False, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None:
            return Effect('ack')
        matter.played.append((reply_id, segment, final))
        answer = matter.answers.get(reply_id)
        if answer and final:
            answer.played = True
        if quick and final and matter.tasks.get('cognition') == 'running':
            matter.tasks['quick'] = 'done'
            return Effect('ack', matter_id=matter.matter_id, answer_id=reply_id)
        return Effect('ack', matter_id=matter.matter_id, answer_id=reply_id)

    def user_stop(self, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None:
            return Effect('cancel', immediate=True, reason='user_stop')
        attempt = matter.attempts.get(matter.current_attempt)
        return self._cancel(matter, attempt, 'user_stop')

    def persist_failed(self, matter_id=None):
        matter = self._matter(matter_id)
        if matter is None:
            return Effect('persist_failed', resume=False, immediate=True)
        matter.persist_failed = True
        return Effect('persist_failed', matter_id=matter.matter_id, resume=False, immediate=True,
                      seq=matter.seq, input_version=matter.input_version)

    def blocks_start(self):
        matter = self.current()
        if matter is None:
            return False
        if matter.cancelled or matter.unclear:
            return True
        return bool(matter.pending or matter.asr_pending or matter.reasons)

    def _reapprove(self, matter, attempt, scope):
        if attempt is not None:
            attempt.execution = 'open'
            matter.tasks['entry'] = 'running'
        cleared = []
        if matter.speech_seq == scope.speech_seq:
            for reason_id in list(matter.reasons):
                if reason_id in scope.reason_ids:
                    cleared.append(reason_id)
                    del matter.reasons[reason_id]
        matter.pending.clear()
        matter.asr_pending = False
        matter.seq += 1
        answer = self._answer(matter)
        return Effect('reapprove', matter_id=matter.matter_id, attempt_id=attempt.attempt_id if attempt else '',
                      answer_id=answer.answer_id if answer else '', input_version=matter.input_version,
                      basis_version=answer.basis_version if answer else None, approval_version=matter.input_version,
                      resume=self._can_resume(matter), seq=matter.seq, cleared=tuple(cleared))

    def _revoke(self, matter, attempt):
        attempt.execution = 'revoked'
        attempt.orphan = True
        matter.tasks['entry'] = 'replaced'

    def _drop_unplayed(self, matter):
        for answer in matter.answers.values():
            if not answer.played:
                answer.eligible = False

    def _cancel(self, matter, attempt, reason):
        matter.cancelled = True
        matter.closed = True
        matter.seq += 1
        self._drop_unplayed(matter)
        if attempt and attempt.execution != 'revoked':
            attempt.execution = 'revoked'
        event = {'matter_id': matter.matter_id, 'closed': True, 'reason': reason,
                 'input_version': matter.input_version, 'played': list(matter.played)}
        matter.persist_queue.append(event)
        matter.tasks['entry'] = 'cancelled'
        return Effect('cancel', matter_id=matter.matter_id, attempt_id=attempt.attempt_id if attempt else '',
                      reason=reason, immediate=True, resume=False, seq=matter.seq,
                      input_version=matter.input_version, persist=(event,))

    def _answer(self, matter):
        return next(iter(matter.answers.values()), None)

    def _basis(self, matter):
        answer = self._answer(matter)
        return answer.basis_version if answer else None

    def _can_resume(self, matter):
        return not matter.cancelled and not matter.unclear and not matter.asr_pending and not matter.pending and not matter.reasons
