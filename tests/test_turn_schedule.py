from jshi.voice.turn_schedule import TurnSchedule, correction_grade


def test_backchannel_reapproves_old_answer_without_rewriting_its_basis():
    schedule = TurnSchedule()
    matter = schedule.open_matter()
    schedule.add_answer('reply', 1)
    schedule.begin_attempt(('a',))
    schedule.note_text('b', '嗯，你继续')
    scope = schedule.capture()
    effect = schedule.apply_collision('independent', 'continue', matter.pending, scope)
    assert effect.kind == 'reapprove'
    assert effect.basis_version == 1 and effect.approval_version == 2
    assert matter.answers['reply'].basis_version == 1 and matter.answers['reply'].eligible


def test_hold_then_takeover_revokes_execution_and_late_result_is_diagnostic():
    schedule = TurnSchedule()
    schedule.open_matter()
    attempt = schedule.begin_attempt(('a',))
    held = schedule.note_text('b', '后面还有')
    assert held.kind == 'hold_submission' and attempt.execution == 'held'
    scope = schedule.capture()
    effect = schedule.apply_collision('continue', 'hold', schedule.current().pending, scope)
    assert effect.kind == 'replace_entry' and attempt.execution == 'revoked'
    assert schedule.current().inputs['b'] == '后面还有'
    late = schedule.apply_collision('independent', 'continue', (), scope, attempt_id=attempt.attempt_id)
    assert late.kind == 'diagnostic'


def test_cap_keeps_collecting_and_stop_still_cancels():
    schedule = TurnSchedule(max_leftover=1)
    schedule.open_matter()
    first = schedule.begin_attempt(('a',))
    second = schedule.begin_attempt(('b',))
    first.execution = 'revoked'
    first.orphan = True
    schedule.note_text('c', '还有一句')
    again = schedule.apply_collision('continue', 'hold', schedule.current().pending, schedule.capture(),
                                     attempt_id=second.attempt_id)
    assert again.kind == 'capped' and second.execution == 'held'
    assert schedule.current().inputs['c'] == '还有一句'
    assert schedule.begin_attempt(('c',)) is None
    stopped = schedule.user_stop()
    assert stopped.kind == 'cancel' and stopped.immediate and schedule.current().played == []


def test_old_approval_cannot_clear_a_newer_pause():
    schedule = TurnSchedule(speech_hold=True)
    schedule.open_matter()
    schedule.begin_attempt(('a',))
    schedule.add_answer('reply', 1)
    schedule.note_text('b', '嗯')
    scope = schedule.capture()
    schedule.sustained_voice()
    effect = schedule.apply_collision('background', 'continue', ('b',), scope)
    assert effect.kind == 'reapprove' and effect.resume is False
    assert any(reason.kind == 'pause' for reason in schedule.current().reasons.values())
    assert schedule.blocks_start()


def test_short_noise_without_text_clears_only_its_own_block():
    schedule = TurnSchedule(speech_hold=True)
    schedule.open_matter()
    schedule.begin_attempt(('a',))
    schedule.add_answer('reply', 1)
    assert schedule.short_voice().kind == 'block_next'
    token = schedule.note_short_end()
    effect = schedule.confirm_transient(token)
    assert effect.kind == 'clear_block' and effect.resume
    assert not schedule.blocks_start()


def test_text_or_asr_failure_prevents_treating_speech_as_noise():
    schedule = TurnSchedule(speech_hold=True)
    schedule.open_matter()
    schedule.begin_attempt(('a',))
    schedule.short_voice()
    token = schedule.note_short_end()
    schedule.note_text('b', '不是电脑')
    assert schedule.confirm_transient(token).kind == 'none'
    failed = schedule.asr_failed()
    assert failed.kind == 'unclear' and failed.resume is False
    assert schedule.blocks_start()


def test_sustained_end_waits_for_recognition():
    schedule = TurnSchedule(speech_hold=True)
    schedule.open_matter()
    schedule.sustained_voice()
    effect = schedule.note_sustained_end()
    assert effect.kind == 'await_asr' and effect.resume is False
    assert schedule.confirm_transient(schedule.current().transient_token).kind == 'none'


def test_sustained_sound_without_text_releases_after_wait_limit():
    schedule = TurnSchedule(speech_hold=True)
    schedule.open_matter()
    schedule.add_answer('reply', 1)
    schedule.sustained_voice()
    schedule.note_sustained_end()
    token = schedule.current().transient_token
    assert schedule.blocks_start()
    effect = schedule.expire_asr_wait(token)
    assert effect.kind == 'clear_block' and effect.resume and effect.cleared
    assert not schedule.blocks_start()

    schedule.sustained_voice()
    schedule.note_sustained_end()
    token = schedule.current().transient_token
    schedule.note_text('b', '你好')
    assert schedule.expire_asr_wait(token).kind == 'none'
    assert schedule.blocks_start()


def test_shadow_observation_does_not_revoke_or_resume():
    schedule = TurnSchedule()
    schedule.open_matter()
    attempt = schedule.begin_attempt(('a',))
    schedule.note_text('b', active_mode=False)
    effect = schedule.apply_collision('stop', 'stop', schedule.current().pending, schedule.capture(), mode='shadow')
    assert effect.kind == 'observe' and attempt.execution == 'open'


def test_correction_grades_do_not_reuse_a_changed_premise():
    assert correction_grade(False, True, 'revise') == 'revise'
    assert correction_grade(False, True, 'reuse') == 'reuse'
    assert correction_grade(True, True, 'revise') == 'restart'
    assert correction_grade(True, False, None) == 'restart'
    assert correction_grade(False, False, 'revise') == 'reconstruct'
    schedule = TurnSchedule()
    schedule.open_matter()
    schedule.add_answer('reply', 1)
    effect = schedule.apply_impact(True, True, 'revise')
    assert effect.grade == 'restart' and not schedule.current().answers['reply'].eligible


def test_storage_failure_does_not_release_a_cancelled_playback():
    schedule = TurnSchedule()
    schedule.open_matter()
    answer = schedule.add_answer('reply', 1)
    effect = schedule.user_stop()
    assert effect.immediate and effect.persist and not answer.eligible
    failed = schedule.persist_failed()
    assert failed.resume is False and schedule.blocks_start() and schedule.current().played == []


def test_quick_reply_completion_does_not_close_running_cognition():
    schedule = TurnSchedule()
    matter = schedule.open_matter()
    schedule.set_task('cognition', 'running')
    schedule.add_answer('ack', 1)
    schedule.ack('ack', 0, final=True, quick=True)
    assert matter.tasks['quick'] == 'done' and matter.tasks['cognition'] == 'running'
    assert not matter.closed


def test_idle_text_does_not_request_collision_and_speech_hold_defaults_off():
    schedule = TurnSchedule()
    assert schedule.note_text('a', '你好').kind == 'normal_entry'
    assert schedule.short_voice().kind == 'ignored'
    assert schedule.sustained_voice().kind == 'ignored'
