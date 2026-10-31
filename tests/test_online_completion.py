from threading import Event
from types import SimpleNamespace

import pytest

from jshi.voice.contracts import Transcript
from jshi.voice.online_completion import OnlineCompletionPolicy
from jshi.voice.turns import TurnDecision


@pytest.mark.parametrize('text', ['这个时尚的', '对这个物品摆放的话一定要，呃'])
def test_open_tail_waits_even_when_acoustics_claim_complete(text):
    policy = OnlineCompletionPolicy(SimpleNamespace(decide=lambda rows,d:d))
    try:
        result = policy.decide((Transcript(text,'0',0,1000,True),),TurnDecision('complete'))
        assert result.state == 'incomplete'
        assert result.correction['acoustic'] == 'complete'
        assert result.correction['continuation_hint'] == 'open_text_tail'
        assert not result.correction['semantic_called']
    finally: policy.close()


@pytest.mark.parametrize('text', ['是的', '这个是我的', '今天就到这里。', '服装'])
def test_self_contained_answers_do_not_trigger_open_tail(text):
    from jshi.voice.online_completion import open_tail
    assert not open_tail((Transcript(text,'0',0,1000,True),))


def test_unvalidated_namo_cannot_overrule_incomplete_acoustics():
    policy = OnlineCompletionPolicy(SimpleNamespace(decide=lambda rows,d:d),
        SimpleNamespace(name='namo', analyze_text=lambda rows:TurnDecision('complete')))
    try:
        result = policy.decide((),TurnDecision('incomplete'))
        assert result.state == 'incomplete'
        assert result.correction['semantic'] == 'complete'
        assert result.correction['semantic_authoritative'] is False
    finally: policy.close()


@pytest.mark.parametrize('audio_state,text_state,expected,called',[
    ('complete','incomplete','complete',False),
    ('unknown','complete','unknown',False),
    ('incomplete','complete','complete',True),
    ('incomplete','incomplete','incomplete',True),
    ('incomplete','unknown','unknown',True)])
def test_only_conflict_calls_text_and_unknown_has_an_explicit_release(audio_state,text_state,expected,called):
    seen=[]
    def analyze(rows):seen.append(rows);return TurnDecision(text_state)
    audio=SimpleNamespace(decide=lambda rows,result:result)
    policy=OnlineCompletionPolicy(audio,SimpleNamespace(analyze_text=analyze))
    rows=(Transcript('如果明天下雨','A',0,1000,True),)
    try:
        result=policy.decide(rows,TurnDecision(audio_state))
        assert result.state==expected
        assert result.correction['semantic_called']==called
        assert seen==([rows] if called else [])
        if audio_state=='incomplete' and text_state=='unknown':
            assert result.reason=='online_conflict_unresolved_release'
    finally:policy.close()


def test_timeout_does_not_queue_more_inference_or_apply_a_late_vote():
    entered=Event();release=Event()
    def analyze(rows):entered.set();release.wait(2);return TurnDecision('incomplete')
    policy=OnlineCompletionPolicy(SimpleNamespace(decide=lambda rows,d:d),
        SimpleNamespace(analyze_text=analyze),timeout_s=.01)
    try:
        first=policy.decide((),TurnDecision('incomplete'))
        assert entered.is_set() and first.state=='unknown'
        assert first.correction['semantic_reason']=='semantic_timeout'
        second=policy.decide((),TurnDecision('incomplete'))
        assert second.state=='unknown' and second.correction['semantic_reason']=='semantic_busy'
        release.set();policy.future.result(timeout=1)
        assert first.state=='unknown' and second.state=='unknown'
    finally:release.set();policy.close()


def test_disabled_and_failed_text_release_without_claiming_complete():
    def fail(rows):raise ValueError('bad output')
    for semantic in (None,SimpleNamespace(analyze_text=fail)):
        policy=OnlineCompletionPolicy(SimpleNamespace(decide=lambda rows,d:d),semantic)
        try:
            result=policy.decide((),TurnDecision('incomplete'))
            assert result.state=='unknown' and result.reason=='online_conflict_unresolved_release'
        finally:policy.close()


def test_online_gate_releases_unknown_conflict_and_preserves_separate_evidence():
    import asyncio
    from jshi.voice.online_turn import OnlineTurnGate
    from jshi.voice.audio_buffer import SessionAudio
    async def run():
        audio=SessionAudio();audio.append(b'\0\0'*16000)
        raw=SimpleNamespace(analyze_audio=lambda samples:TurnDecision('incomplete',.1,'audio'),
                            decide=lambda rows,d:d)
        policy=OnlineCompletionPolicy(raw,SimpleNamespace(analyze_text=lambda rows:TurnDecision('unknown')))
        delivered=[];reports=[]
        async def accept(row,**kwargs):delivered.append(row)
        gate=OnlineTurnGate(audio,policy,SimpleNamespace(speech_active=lambda:True),accept,
            report=lambda kind,data:reports.append((kind,data)))
        row=Transcript('无法判断的半句话','A',0,1000,True,voiceprint_id='test')
        try:
            await gate.submit(row)
            assert delivered==[row] and not gate.coordinator.pending
            info=next(data for kind,data in reports if kind=='online_acoustic_turn')
            assert info['turn_state']=='unknown' and info['turn_audio_state']=='incomplete'
            assert info['turn_semantic_state']=='unknown' and info['turn_semantic_enabled']
            assert info['online_completion']['semantic_called']
        finally:await gate.close()
    asyncio.run(run())
