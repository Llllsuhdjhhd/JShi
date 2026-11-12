from types import SimpleNamespace
from threading import Event, Thread, Lock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request
import time

from jshi.subject.cognition_runtime import serialized, runtime_for
from jshi.subject.cancellation import check, TurnCancelled
from jshi.subject.wakeups import WakeQueue, tool_mode


def test_same_repository_serializes_different_owners(tmp_path):
    a = SimpleNamespace(repository=SimpleNamespace(path=tmp_path/'subject.db'))
    b = SimpleNamespace(repository=SimpleNamespace(path=tmp_path/'subject.db'))
    entered, release = Event(), Event()
    counts = [0, 0]
    mutex = Lock()
    @serialized
    def work(process, subject, first=False):
        with mutex:
            counts[0] += 1
            counts[1] = max(counts)
        if first:
            entered.set(); release.wait(3)
        with mutex:
            counts[0] -= 1
    first = Thread(target=work,args=(a,'stone',True)); first.start()
    assert entered.wait(2)
    second = Thread(target=work,args=(b,'stone')); second.start()
    time.sleep(.05)
    assert counts == [1,1]
    release.set(); first.join(3); second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert counts[1] == 1


def test_cancel_stalled_http_then_switch(tmp_path):
    started, release, finished, next_done = Event(), Event(), Event(), Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            started.set()
            release.wait(10)  # Never completes before cancellation.
            try:
                self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
            except OSError:
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads = True
    Thread(target=server.serve_forever,daemon=True).start()
    owner = SimpleNamespace(repository=SimpleNamespace(path=tmp_path/'subject.db'))
    results = []
    @serialized
    def work(process, subject):
        from jshi.models.http_pool import urlopen
        try:
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}/'),timeout=20) as response:
                response.read()
            results.append('unexpected')
        except TurnCancelled:
            results.append('cancelled')
        finally:
            finished.set()
    @serialized
    def other(process, subject):
        assert finished.is_set()
        next_done.set()
    first = Thread(target=work,args=(owner,'stone')); first.start()
    try:
        assert started.wait(8)
        runtime_for(owner,'stone').interrupt()
        second = Thread(target=other,args=(owner,'stone')); second.start()
        assert next_done.wait(5)
        assert results == ['cancelled']
        first.join(2); second.join(2)
    finally:
        release.set(); server.shutdown(); server.server_close(); first.join(3)


def test_pending_sampling_does_not_reset_timer_or_lose_late_events():
    now = [0.]
    q = WakeQueue(clock=lambda:now[0], seconds=10)
    for i in range(20):
        now[0] += 1
        q.offer('perception',str(i),'new observation')
    batch = q.claim()
    assert {x.source for x in batch} == {'timer','perception'}
    q.offer('perception','late','late observation')
    q.finish(batch,success=True)
    now[0] += 10
    assert any(x.key == 'late' for x in q.claim())
    assert tool_mode(True) == 'conversation'
    assert tool_mode(False) == 'initiative'


def test_quiet_timer_backs_off_but_new_evidence_does_not_wait_for_backoff():
    now = [0.]
    q = WakeQueue(clock=lambda: now[0], seconds=180)
    for expected in (180, 360, 720, 1440, 1800):
        now[0] += q.interval
        batch = q.claim()
        assert [x.source for x in batch] == ['timer']
        q.finish(batch, success=True, quiet=True)
        assert q.interval == expected
    now[0] += 31
    q.offer('perception', 'new', 'new observation')
    batch = q.claim()
    assert [x.source for x in batch] == ['perception']
    assert q.interval == 180
    q.finish(batch, success=True, quiet=True)
    assert q.quiet_rounds == 0


def test_new_interaction_retires_interrupted_timer_but_keeps_new_observation():
    now = [180.]
    q = WakeQueue(clock=lambda: now[0], seconds=180)
    now[0] += 181
    batch = q.claim()
    q.note_activity()  # Human interrupted the review.
    q.offer('perception', 'late', 'observation during the interrupted review')
    q.finish(batch, success=False)
    now[0] += 30
    assert [x.key for x in q.claim()] == ['late']
    assert q.claim() == ()


def test_initiative_is_internal_and_keeps_no_fictional_speaker(tmp_path):
    from tests.test_idle_stimulus import _process
    from jshi.core.envelope import InputEnvelope, SpeakerEvidence
    from jshi.models.prompt import build_user
    p, repo = _process(tmp_path)
    envelope=InputEnvelope(session_id='test',source='initiative',parts=(),
        speaker=SpeakerEvidence('initiative',object_id='scene:stone',label='现场',method='voice_scene'),
        cognition_mode='initiative',wake_context='定时审视；经过180秒。')
    result=p.experience('stone','',envelope=envelope)
    assert result.current_state.cognition_mode == 'initiative'
    assert result.current_state.speaker is None
    assert not any(x.event_type=='external_input' for x in repo.list_history('stone'))
    assert '180秒' in build_user(p.cognition.requests[0])


from tests.test_voice import process


def test_cancelled_real_tool_review_closes_activity_and_retries_notice(process, monkeypatch):
    import pytest
    from jshi.subject.domain import ActivityStatus
    from tests.test_tool_notifications import failed_task
    task = failed_task(process)
    original = process.cognition.generate
    def interrupt(request):
        raise TurnCancelled('new input')
    monkeypatch.setattr(process.cognition, 'generate', interrupt)
    with pytest.raises(TurnCancelled):
        process.experience_tool_results('stone', 'lux-id', (task,))
    activities = process.repository.list_activities('stone')
    assert activities[-1].status == ActivityStatus.ABANDONED
    assert not process.repository.list_activities('stone', ActivityStatus.OPEN)
    assert any(h.event_type == 'activity_interrupted' for h in process.repository.list_history('stone'))
    assert process.tool_service.claim_notices('stone', 'lux-id') == (task,)
    process.tool_service.release_notices((task,))
    monkeypatch.setattr(process.cognition, 'generate', original)
    result = process.experience_tool_results('stone', 'lux-id', (task,))
    assert result.activity.status == ActivityStatus.COMPLETED
    assert process.tool_service.claim_notices('stone', 'lux-id') == ()

def test_web_timer_uses_real_main_and_is_not_a_new_user_message(process, monkeypatch):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    now=[0.]
    original=WakeQueue.__init__
    def init(self, **kwargs):
        original(self,clock=lambda:now[0],seconds=10)
    monkeypatch.setattr(WakeQueue,'__init__',init)
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig('')))) as client:
            now[0]=11
            headers={'Origin':str(client.make_url('/')).rstrip('/')}
            response=await client.post('/initiative-results',json={'object_id':'lux-id'},headers=headers)
            assert response.status==200
            assert (await response.json())['reply']
            assert process.cognition.requests[-1].cognition_mode=='initiative'
            count=len(process.cognition.requests)
            await client.post('/initiative-results',json={'object_id':'lux-id'},headers=headers)
            assert len(process.cognition.requests)==count
    asyncio.run(run())


def test_web_tool_result_resets_overdue_timer(process, monkeypatch):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    from tests.test_tool_notifications import failed_task
    now = [0.]
    original = WakeQueue.__init__
    def init(self, **kwargs):
        original(self, clock=lambda: now[0], seconds=10)
    monkeypatch.setattr(WakeQueue, '__init__', init)
    failed_task(process)
    async def run():
        async with TestClient(TestServer(create_app(process, 'stone', VoiceConfig('')))) as client:
            now[0] = 100
            headers = {'Origin': str(client.make_url('/')).rstrip('/')}
            response = await client.post('/initiative-results', json={'object_id': 'lux-id'}, headers=headers)
            assert (await response.json())['reply']
            count = len(process.cognition.requests)
            response = await client.post('/initiative-results', json={'object_id': 'lux-id'}, headers=headers)
            assert (await response.json())['reply'] == ''
            assert len(process.cognition.requests) == count
            now[0] += 11
            response = await client.post('/initiative-results', json={'object_id': 'lux-id'}, headers=headers)
            assert (await response.json())['reply']
    asyncio.run(run())


def test_voice_timer_and_background_enter_same_turn_worker(process):
    import asyncio
    from jshi.app.voice import VoiceConversation
    from jshi.voice.jev import VoiceJEV
    from tests.test_voice_control import FakeCloud
    async def run():
        sent=[]
        async def send(item):sent.append(item)
        c=VoiceConversation(process,'stone',FakeCloud(),send,jev=VoiceJEV(None),scene_idle_s=60)
        c.person_reviewer=None
        try:
            c.wakeups.last_review-=200
            await asyncio.sleep(.65)
            await asyncio.wait_for(c.turn_queue.join(),4)
            assert any(r.cognition_mode=='initiative' for r in process.cognition.requests)
            assert not c.wake_batches
        finally:
            await c.close()
    asyncio.run(run())


def test_all_personas_select_standalone_initiative_task():
    from jshi.models import ModelRequest
    from jshi.core import SubjectState
    from jshi.skill.cognition import CognitionSkill
    from jshi.style.packs import reply_instruction_for, reply_schema_for
    skill=CognitionSkill(SimpleNamespace(name='fixture'))
    for persona in ('wood','smith','suxipo','aqiu'):
        for level in (1,2,3):
            request=ModelRequest(purpose='subject_activity',input_text='',subject_state=SubjectState('s','匠石',''),
                cognition_mode='initiative',persona_instruction=reply_instruction_for(persona,prompt_level=level),
                persona_schema=reply_schema_for(persona,prompt_level=level))
            system=skill.system_extra(request)
            assert '本轮任务：自主审视与发起' in system
            assert '不穷尽可能意向' in system
            assert '对象未成立的旁聊选ignore' not in system
            assert 'JSON Schema' in system


def test_tool_notification_adapter_chooses_conversation_or_initiative(process):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from jshi.app.voice import create_app
    from jshi.voice.config import VoiceConfig
    from jshi.voice.jev import VoiceJEV
    from jshi.models import ResponsePlan, ResponseItem
    modes=[]
    available=[True]
    def claim(subject, owner):
        if available[0]:
            available[0]=False
            return ('task',)
        return ()
    process.tool_service.claim_notices=claim
    process.tool_service.release_notices=lambda ids:None
    def entry(subject, owner, ids, *, envelope, **kwargs):
        modes.append(envelope.cognition_mode)
        return SimpleNamespace(activity=SimpleNamespace(id='a'),response_plan=ResponsePlan('respond',items=(ResponseItem('verbal','结果',()),)))
    process.experience_tool_results=entry
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(''),jev=VoiceJEV(None)))) as client:
            headers={'Origin':str(client.make_url('/')).rstrip('/')}
            assert (await client.post('/initiative-results',json={'object_id':'lux-id'},headers=headers)).status==200
            assert modes==['initiative']
        available[0]=True
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(''),jev=VoiceJEV(None)))) as client:
            headers={'Origin':str(client.make_url('/')).rstrip('/')}
            response=await client.post('/text-input',json={'object_id':'lux-id','text':'你好'},headers=headers)
            assert response.status==200
            assert (await client.post('/initiative-results',json={'object_id':'lux-id'},headers=headers)).status==200
            assert modes==['initiative','conversation']
    asyncio.run(run())
