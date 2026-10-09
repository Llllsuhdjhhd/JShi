import asyncio
import json
from dataclasses import replace
from io import BytesIO
from time import time

import pytest
pytest.importorskip('PIL')
from PIL import Image

from jshi.vision import VisionConfig, VisionStore, VisionService
from jshi.vision.model import VisionModel
from jshi.vision.memory import VisualMemory
from jshi.memory.contracts import MemoryBatch, MemoryExperience, BackendIngestResult
from jshi.memory.port import RecalledFragment
from jshi.voice.jev import VoiceJEV
from tests.test_voice import process


def image(color='red'):
    out = BytesIO()
    Image.new('RGB', (160, 120), color).save(out, 'PNG')
    return out.getvalue()


@pytest.fixture
def store(tmp_path):
    return VisionStore(tmp_path/'vision', VisionConfig())


def snapshot(store, color='red', at=None, description='客厅里有一个人。'):
    frame = store.add_frame('stone', image(color), at or time())
    return store.publish('stone', frame, description)


def test_media_roundtrip_and_subject_isolation(store):
    s = snapshot(store)
    assert store.image(s['frame'], 'stone').startswith(b'\xff\xd8')
    assert store.latest('other') is None
    with pytest.raises(KeyError):
        store.image(s['frame'], 'other')
    with pytest.raises(ValueError):
        store.add_frame('stone', b'bad', float('nan'))
    with pytest.raises(Exception):
        store.add_frame('stone', b'bad', time())


def test_late_result_cannot_replace_new_scene(store):
    older = store.add_frame('stone', image(), time()-10)
    latest = snapshot(store)
    assert store.publish('stone', older, '旧现场') is None
    assert store.latest('stone')['id'] == latest['id']


def test_local_update_keeps_description_observation_time(store):
    first = snapshot(store, at=time()-10)
    newer = store.add_frame('stone', image('blue'), time())
    s = store.publish('stone', newer, first['description'], [{'class':'person','box':[.1,.1,.5,.5]}],
                      'local_detection', 'local', described_at=first['described_at'])
    assert s['captured'] > s['described_at']
    assert '描述依据' in store.render(s)


def test_memory_photo_survives_rolling_cleanup(store):
    kept = snapshot(store, at=time()-20)
    assert store.bind('stone', 'input-1', kept['id'])
    discarded = store.add_frame('stone', image('green'), time()-30)
    latest = snapshot(store, 'blue')
    store.config = replace(store.config, retention_seconds=1, rolling_bytes=1)
    store.cleanup()
    assert store.image(kept['frame'], 'stone')
    assert store.image(latest['frame'], 'stone')
    with pytest.raises(KeyError):
        store.image(discarded, 'stone')


def test_identical_media_and_bindings_are_deduplicated(store):
    s = snapshot(store)
    another = store.add_frame('stone', image(), time())
    store.bind('stone', 'input-1', s['id'])
    store.bind('stone', 'input-1', s['id'])
    with store.db() as c:
        assert c.execute('select count(*) from assets').fetchone()[0] == 1
        assert c.execute('select count(*) from bindings').fetchone()[0] == 1
    assert store.frame(another, 'stone')['asset'] == store.frame(s['frame'], 'stone')['asset']


def test_memory_budget_rejects_new_photo_but_keeps_old(store):
    first = snapshot(store)
    store.bind('stone', 'first', first['id'])
    store.config = replace(store.config, memory_bytes=1)
    assert store.bind('stone', 'repeat', first['id'])
    second = snapshot(store, 'blue')
    assert not store.bind('stone', 'new', second['id'])
    assert store.associated('stone', ['first'])


class Model:
    def __init__(self): self.calls = 0
    def describe(self, data, **kwargs):
        self.calls += 1
        return '这是客厅，一个人在画面左侧。'


class Detector:
    def __init__(self): self.rows = []; self.changed = False
    def detect(self, data): return self.rows, self.changed


def test_initial_cloud_then_local_movement_and_special_change(store):
    model, detector = Model(), Detector()
    service = VisionService(store, store.config, model, detector)
    detector.rows = [{'class':'person','track':1,'confidence':.9,'box':[.1,.1,.4,.7]}]
    try:
        first = service.process('stone', image(), time())
        assert model.calls == 1
        detector.changed = True
        detector.rows = [{'class':'person','track':1,'confidence':.9,'box':[.3,.1,.6,.7]}]
        second = service.process('stone', image('blue'), time())
        assert model.calls == 1 and second['reason'] == 'local_detection'
        assert second['described_at'] == first['described_at']
        detector.rows.append({'class':'person','track':2,'confidence':.9,'box':[.7,.1,.9,.7]})
        service.last_online[('stone','visual')] = 0
        assert service.process('stone', image(), time())['reason'] == 'change_or_refresh'
        assert model.calls == 2
    finally: service.close()


def test_sampling_photos_can_be_saved_without_cloud(store):
    model = Model()
    service = VisionService(store, store.config, model, Detector())
    try:
        service.process('stone', image(), time())
        service.last_archive.clear()
        service.process('stone', image('blue'), time())
        assert model.calls == 1
        with store.db() as c:
            assert c.execute('select count(*) from frames').fetchone()[0] == 2
    finally: service.close()


def test_bad_model_result_does_not_replace_state(store):
    initial = snapshot(store)
    class Broken:
        def describe(self, *a, **kw): raise RuntimeError('offline')
    d = Detector(); d.changed = True
    service = VisionService(store, store.config, Broken(), d)
    try:
        with pytest.raises(RuntimeError): service.process('stone', image('blue'), time())
        assert store.latest('stone')['id'] == initial['id']
    finally: service.close()


def test_recall_action_uses_photo_without_replacing_current(store):
    old = snapshot(store, at=time()-10)
    store.bind('stone','input',old['id'])
    current = snapshot(store,'blue')
    service = VisionService(store, store.config, Model(), Detector())
    results = []; service.on_update = results.append
    try:
        assert not service.request_action('stone', '转头看向窗外')
        assert service.request_action('stone', f"回看照片 {old['frame']}：那个人在哪里？")
        result = service.action_future.result(timeout=3)
        assert '历史照片' in result['description']
        assert results and store.latest('stone')['id'] == current['id']
        assert not service.request_action('other', f"回看照片 {old['frame']}：查看")
    finally: service.close()


def test_failed_observation_can_retry_same_photo_and_question(store):
    snapshot(store)
    class Recovering(Model):
        def describe(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('temporary network failure')
            return '恢复后的观察结果'
    service = VisionService(store, store.config, Recovering(), Detector())
    try:
        assert service.request_action('stone', '仔细看画面里的人')
        assert service.action_future.result(timeout=3) is None
        assert service.request_action('stone', '仔细看画面里的人')
        assert service.action_future.result(timeout=3)['description'] == '恢复后的观察结果'
        assert not service.request_action('stone', '仔细看画面里的人')
    finally:
        service.close()


@pytest.mark.parametrize('route', ['interpret', 'frame'])
def test_visual_operations_wait_without_blocking_http_loop(store, monkeypatch, route):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from threading import Event
    from types import SimpleNamespace
    from jshi.vision.web import install_vision
    entered, release = Event(), Event()
    service = VisionService(store, store.config, Model(), Detector())
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return True
    monkeypatch.setattr(service, 'request_action' if route == 'interpret' else 'submit', delayed)
    async def deliver(*args): pass
    async def ping(request): return web.Response(text='ready')
    async def run():
        app = web.Application()
        install_vision(app, SimpleNamespace(vision=service), 'stone', deliver)
        app.router.add_get('/ping', ping)
        async with TestClient(TestServer(app)) as client:
            headers = {'Origin':str(client.make_url('')).rstrip('/')}
            kwargs = {'json':{'frame':'test'}} if route == 'interpret' else {'data':image()}
            pending = asyncio.create_task(client.post('/api/vision/'+route, headers=headers, **kwargs))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                response = await asyncio.wait_for(client.get('/ping'), .5)
                assert await response.text() == 'ready'
            finally:
                release.set()
                assert (await pending).status == 202
    try:
        asyncio.run(run())
    finally:
        release.set()
        service.close()


def test_image_adapter_sends_real_image_parts():
    payloads = []
    def send(p):
        payloads.append(p)
        return {'choices':[{'message':{'content':'两个人在桌旁。'}}]}
    model = VisionModel(VisionConfig(), send)
    assert model.describe(b'jpeg') == '两个人在桌旁。'
    block = payloads[0]['messages'][0]['content'][1]
    assert block['type']=='image_url' and block['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert payloads[0]['thinking'] == {'type': 'disabled'}


def test_image_adapter_rejects_empty_or_truncated_descriptions():
    import pytest
    for content, reason in [('', 'length'), ('只描述了一半', 'length')]:
        model = VisionModel(VisionConfig(), lambda p: {
            'choices': [{'message': {'content': content}, 'finish_reason': reason}]})
        with pytest.raises(ValueError):
            model.describe(b'jpeg')


def test_memory_batch_associations_and_recall(store):
    s = snapshot(store)
    store.bind('stone','fact-1',s['id']); store.bind('stone','fact-2',s['id'])
    class Backend:
        def ingest_batch(self, b):
            self.batch = b
            return BackendIngestResult('stone', stored_marks={e.segment_id:['event-'+e.segment_id] for e in b.experiences})
        def recall(self, *a, **kw): return (RecalledFragment('event-s1','event','说了你好'),)
    b = Backend(); memory = VisualMemory(b, store)
    batch = MemoryBatch('batch','stone',tuple(MemoryExperience('stone','你好',source_ids=(f'fact-{i}',),segment_id=f's{i}') for i in [1,2]),1,2)
    memory.ingest_batch(batch)
    assert sum(e.text.count('客厅里') for e in b.batch.experiences)==1
    recalled = memory.recall('stone','客厅')[0]
    assert recalled.visual_observations[0]['frame']==s['frame']
    assert '回看照片' in recalled.content
    memory.ingest_batch(batch)
    assert all('客厅里' not in e.text for e in b.batch.experiences)
    rows = [json.loads(line) for line in (store.root.parent / 'memory_phase_timings.jsonl').read_text(encoding='utf-8').splitlines()]
    recall = next(row for row in rows if row['operation'] == 'recall')
    assert recall['returned_count'] == 1
    assert recall['backend_ms'] >= 0 and recall['visual_associations_ms'] >= 0
    assert recall['status'] == 'succeeded'
    assert '客厅' not in json.dumps(rows, ensure_ascii=False)
    ingest = next(row for row in rows if row['operation'] == 'ingest')
    assert all(ingest[name] >= 0 for name in ('lock_wait_ms', 'backend_ms', 'visual_prepare_ms', 'visual_delivery_ms'))


def test_visual_diagnostic_write_failure_does_not_break_recall(store, monkeypatch):
    from pathlib import Path
    original = Path.open
    def fail_diagnostics(path, *args, **kwargs):
        if path.name == 'memory_phase_timings.jsonl':
            raise OSError('diagnostic file unavailable')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail_diagnostics)
    class Backend:
        def recall(self, *args, **kwargs):
            return (RecalledFragment('event-test', 'event', '有效记忆'),)
    memory = VisualMemory(Backend(), store)
    assert memory.recall('stone', '测试')[0].text == '有效记忆'


def test_visual_packet_uses_existing_jev_schema():
    import json
    from jshi.models import ModelResponse
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
    class Judge:
        def generate(self, r):
            self.request=r
            return ModelResponse(text=json.dumps({'items':[{'i':1,'person':'unknown','certainty':'unknown','why':'observation'}],'level':2,'recall_memory':False}), model='fake')
    model=Judge()
    envelope=InputEnvelope('test','visual_environment',(InputPart('text','有人进入'),),SpeakerEvidence('visual',method='visual_scene'))
    result=VoiceJEV(model).decide_envelope('stone',envelope,{'state':'completed','jev_scene':'recent scene'})
    assert model.request.purpose=='voice_jev'
    data=json.loads(model.request.input_text)
    assert data['input']['envelope']['source']=='visual_environment'
    assert data['scene']=='recent scene'
    assert result.main_prompt_hint['level']==2
    assert not hasattr(VoiceJEV,'decide_environment')


def test_main_uses_environment_and_keeps_raw_speech_separate(store, tmp_path):
    from jshi.identity import IdentityRepository, IdentityProfile
    from jshi.subject.repository import SubjectRepository
    from jshi.subject.process import SubjectProcess
    from jshi.models import ModelResponse
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
    from jshi.models.prompt import build_user
    class Cognition:
        def generate(self,r): self.request=r; return ModelResponse(text='你好',model='fake')
    identities=IdentityRepository(tmp_path/'identity.json')
    identities.create(IdentityProfile('stone','匠石','测试','我是匠石'))
    model=Cognition(); vision=VisionService(store,store.config,Model(),Detector())
    p=SubjectProcess(SubjectRepository(tmp_path/'subject.sqlite3'),identities,model,vision=vision)
    s=snapshot(store)
    e=InputEnvelope('test','text',(InputPart('text','你好'),),SpeakerEvidence('text','p1','来客','unknown'))
    try:
        result=p.experience('stone','你好',envelope=e)
        assert s['description'] in build_user(model.request)
        assert result.current_state.visual_snapshot['id']==s['id']
        from jshi.subject.domain import HistoryKind
        facts=p.repository.list_history('stone',kind=HistoryKind.FACT)
        raw=next(r for r in facts if r.event_type=='external_input')
        assert raw.content['text']=='你好'
        assert store.associated('stone',[raw.id])[0]['id']==s['id']
    finally: vision.close()


def test_visual_web_input_security_and_asynchronous_delivery(store, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from aiohttp import web
    from types import SimpleNamespace
    from jshi.vision.web import install_vision
    vision=VisionService(store,store.config,Model(),Detector())
    class Repository:
        def add_history(self,r): pass
    p=SimpleNamespace(vision=vision,repository=Repository(),person_review_scene=lambda s:'现场')
    seen=[]
    async def deliver(s,d): seen.append(s)
    async def run():
        app=web.Application(client_max_size=10*1024*1024)
        install_vision(app,p,'stone',deliver)
        async with TestClient(TestServer(app)) as client:
            r=await client.post('/api/vision/frame',data=image())
            assert r.status==403
            origin=str(client.make_url('')).rstrip('/')
            r=await client.post('/api/vision/frame',data=image(),headers={'Origin':origin,'X-Captured-At':str(time())})
            assert r.status==202
            for _ in range(100):
                if seen: break
                await asyncio.sleep(.01)
            assert seen
            state=await (await client.get('/api/vision')).json()
            photo=await client.get('/api/vision/photo/'+state['environment']['frame'])
            assert photo.status==200 and await photo.read()
    asyncio.run(run())


def test_visual_database_wait_does_not_block_http_loop(store, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from threading import Event
    from types import SimpleNamespace
    from jshi.vision.web import install_vision
    entered, release = Event(), Event()
    def delayed(subject):
        entered.set()
        assert release.wait(3)
        return None
    monkeypatch.setattr(store, 'latest_sample', delayed)
    vision = VisionService(store, store.config, Model(), Detector())
    async def deliver(*args): pass
    async def ping(request): return web.Response(text='ready')
    async def run():
        app = web.Application()
        install_vision(app, SimpleNamespace(vision=vision), 'stone', deliver)
        app.router.add_get('/ping', ping)
        async with TestClient(TestServer(app)) as client:
            pending = asyncio.create_task(client.get('/api/vision'))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                response = await asyncio.wait_for(client.get('/ping'), .5)
                assert await response.text() == 'ready'
            finally:
                release.set()
                await pending
    try:
        asyncio.run(run())
    finally:
        release.set()
        vision.close()


def test_slow_cloud_does_not_block_local_sampling_or_photo_archive(store):
    from threading import Event
    entered, release = Event(), Event()
    class Slow:
        def describe(self, *a, **kw):
            entered.set()
            assert release.wait(3)
            return '初始现场'
    service = VisionService(store, store.config, Slow(), Detector())
    try:
        service.submit('stone', image(), time())
        assert entered.wait(2)
        service.last_archive.clear()
        service.submit('stone', image('blue'), time())
        for _ in range(100):
            import time as clock
            with store.db() as c:
                count=c.execute('select count(*) from frames').fetchone()[0]
            if count>=2: break
            clock.sleep(.01)
        assert count>=2 and store.latest('stone') is None
        release.set()
    finally:
        release.set(); service.close()


@pytest.mark.parametrize('arrival_seconds', [.005, .01, .02])
def test_background_speech_and_slow_vision_do_not_starve_direct_question(store, process, arrival_seconds):
    from threading import Event
    from tests.test_voice import FakeCloud, Model as Cognition
    from jshi.app.voice import VoiceConversation
    from jshi.voice.volc import Transcript
    entered, release, main_entered = Event(), Event(), Event()
    class SlowVision:
        def describe(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return '测试现场'
    class Main(Cognition):
        def generate(self, request):
            if '匠石，你听得到吗' in request.input_text:
                main_entered.set()
            return super().generate(request)
    class Judge:
        name = 'test-entry'
        def generate(self, request):
            from jshi.models import ModelResponse
            lines = json.loads(request.input_text)['input']['lines']
            return ModelResponse(text=json.dumps({'items':[
                {'i':row['i'],'person':'unknown','certainty':'unknown','why':'测试声音，无身份依据'}
                for row in lines], 'level':3, 'recall_memory':False}),model=self.name)
    service = VisionService(store, store.config, SlowVision(), Detector())
    process.vision, process.cognition = service, Main()
    noise = ['我已经', 'J', 'T THE WEST', '作业做好了吗', '嗯']
    async def run():
        messages = []
        async def send(message): messages.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send,
            jev=VoiceJEV(Judge()), input_pause_s=.02, input_max_batch_s=.05)
        stop = asyncio.Event()
        async def produce():
            i = 0
            while not stop.is_set():
                text = '匠石，你听得到吗' if i == 6 else noise[i % len(noise)]
                await c.accept(Transcript(text, 'A', i*1000, i*1000+900, True))
                service.submit('stone', image('blue'), time())
                i += 1
                await asyncio.sleep(arrival_seconds)
        task = asyncio.create_task(produce())
        try:
            assert await asyncio.to_thread(main_entered.wait, 3), {
                'notices':[m for m in messages if m.get('type') == 'notice'],
                'requests':[r.input_text for r in process.cognition.requests]}
            assert not release.is_set() and not task.done()
            assert not any('photo unavailable' in str(m) for m in messages)
        finally:
            stop.set()
            await task
            release.set()
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            await c.close()
    try:
        service.submit('stone', image(), time())
        assert entered.wait(2)
        asyncio.run(run())
    finally:
        release.set()
        service.close()


def test_cloud_description_merges_with_newer_local_positions(store):
    first=snapshot(store,at=time()-20)
    cloud_frame=store.add_frame('stone',image('green'),time()-10)
    local_frame=store.add_frame('stone',image('blue'),time())
    local=store.publish('stone',local_frame,first['description'],[{'class':'person','box':[.4,.1,.7,.8]}],
                        'local_detection','local',described_at=first['described_at'])
    cloud=store.publish('stone',cloud_frame,'有人走到桌旁。',model='cloud')
    assert cloud['frame']==local_frame and cloud['description_frame']==cloud_frame
    assert cloud['detections']==local['detections']
    assert cloud['described_at']<cloud['captured']


def test_full_photo_budget_preserves_text_association(store):
    first=snapshot(store)
    store.bind('stone','first',first['id'])
    store.config=replace(store.config,memory_bytes=1,rolling_bytes=1,retention_seconds=1)
    second=snapshot(store,'blue')
    assert not store.bind('stone','second',second['id'])
    snapshot(store,'green')
    store.cleanup()
    s=store.associated('stone',['second'])[0]
    assert s['description']==second['description'] and not s['image_available']


def test_shared_clock_keeps_source_time_and_offset(store):
    from jshi.core.media import map_source_time
    source=time()-120
    captured=map_source_time(source,120)
    frame=store.add_frame('stone',image(),captured,source_time=source,clock_offset=120,clock_uncertainty=.01)
    s=store.publish('stone',frame,'现场')
    assert s['envelope']['source_time']==source
    assert s['envelope']['captured_at']==captured
    assert s['envelope']['clock_uncertainty']==.01


def test_environment_enters_existing_voice_turn_worker(store, tmp_path):
    from tests.test_voice import FakeCloud, Model
    from jshi.app.voice import VoiceConversation
    from jshi.subject.process import SubjectProcess
    from jshi.subject.repository import SubjectRepository
    from jshi.identity import IdentityRepository, IdentityProfile
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
    identities=IdentityRepository(tmp_path/'identity.json')
    identities.create(IdentityProfile('stone','匠石','测试','我是匠石'))
    vision=VisionService(store,store.config,Model(),Detector())
    p=SubjectProcess(SubjectRepository(tmp_path/'subject.db'),identities,Model(),vision=vision)
    s=snapshot(store)
    async def run():
        async def send(m): pass
        c=VoiceConversation(p,'stone',FakeCloud(),send)
        e=InputEnvelope('environment','visual_environment',(InputPart('text','环境中有人进入。'),),
            SpeakerEvidence('environment',label='环境观察',method='visual_scene'),visual_snapshot_id=s['id'])
        try:
            c.turn_queue.put_nowait((e,c.epoch))
            await asyncio.wait_for(c.turn_queue.join(),3)
            assert len(p.cognition.requests)==1
            assert p.cognition.requests[0].input_text=='环境中有人进入。'
            assert c.debug_history[0]['input']['source']=='visual_environment'
        finally: await c.close()
    try: asyncio.run(run())
    finally: vision.close()


def test_visual_call_budget_is_shared_and_persistent(store):
    store.config=replace(store.config,max_calls_per_hour=1)
    assert store.claim_call('stone')
    reopened=VisionStore(store.root,store.config)
    assert not reopened.claim_call('stone')
    assert reopened.claim_call('other')


def test_older_cloud_completion_keeps_newer_change_pending(store):
    from threading import Event
    entered, release = Event(), Event()
    class Slow:
        def describe(self, *a, **kw):
            entered.set()
            assert release.wait(3)
            return '较早的环境描述'
    service = VisionService(store, store.config, Slow(), Detector())
    try:
        captured = time()
        service.submit('stone', image(), captured)
        assert entered.wait(2)
        with service.lock:
            service.dirty[('stone', 'visual')] = captured + .01
        release.set()
        service.online_future.result(timeout=2)
        assert ('stone', 'visual') in service.dirty
    finally:
        release.set()
        service.close()


def test_switching_visual_source_refreshes_scene(store):
    service = VisionService(store, store.config, Model(), Detector())
    try:
        first = service.process('stone', image(), time(), 'first-camera')
        second = service.process('stone', image(), time(), 'second-camera')
        assert first and second and second['model'] == store.config.model
        assert second['envelope']['source'] == 'second-camera'
    finally:
        service.close()


def test_blank_visual_key_falls_back_to_provider_key(monkeypatch):
    monkeypatch.setenv('JSHI_VISION_API_KEY', '')
    monkeypatch.setenv('JSHI_API_DEEPSEEK_API_KEY', 'test-only-key')
    assert VisionConfig.from_env().api_key == 'test-only-key'


def test_pack_freezes_latest_photo_before_cloud_understanding(store):
    from jshi.core.inputpack import pack_input, packet
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
    first=snapshot(store,at=time()-20)
    new=store.add_frame('stone',image('blue'),time())
    service=VisionService(store,store.config,Model(),Detector())
    try:
        for source in ['web_text','voice','video','photo']:
            raw=InputEnvelope('test',source,(InputPart('text','当前文字'),),SpeakerEvidence('person'))
            packed=pack_input(raw,service,'stone')
            data=packet(packed,service,'stone')
            assert packed.text==raw.text
            assert data['environment']['frame']==new
            assert data['environment']['description_frame']==first['frame']
            assert data['environment']['described_at']<data['environment']['captured']
            assert any(p.reference==new for p in packed.parts)
            store.add_frame('stone',image('green'),time())
            assert pack_input(packed,service,'stone')==packed
            # Reset the newest sample for the next source without pretending a cloud update occurred.
            new=store.add_frame('stone',image('blue'),time())
    finally: service.close()


def test_optional_image_reaches_shared_jev_as_image_block(store):
    import json
    from jshi.core.inputpack import pack_input
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
    from jshi.models import ModelResponse
    from jshi.models.base import _chat_payload
    store.config=replace(store.config,jev_images=True)
    s=snapshot(store)
    service=VisionService(store,store.config,Model(),Detector())
    class Judge:
        def generate(self,r):
            self.request=r
            return ModelResponse(text=json.dumps({'items':[{'i':1,'person':'unknown','certainty':'unknown','why':'图像材料'}],'level':2,'recall_memory':False}),model='test')
    model=Judge()
    try:
        e=pack_input(InputEnvelope('test','photo',(InputPart('text','观察'),),SpeakerEvidence('photo',method='visual_scene')),service,'stone')
        VoiceJEV(model).decide_envelope('stone',e,{'state':'completed'},vision=service)
        assert 'base64' not in model.request.input_text
        body=json.loads(_chat_payload('test',model.request))
        content=body['messages'][1]['content']
        assert content[0]['type']=='text'
        assert content[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
        assert model.request.purpose=='voice_jev'
    finally: service.close()


def test_sampling_without_description_and_late_frame(store):
    latest=store.add_frame('stone',image(),time())
    store.add_frame('stone',image('blue'),time()-10)
    sample=store.latest_sample('stone')
    assert sample['frame']==latest and sample['described_at']==0
    store.config=replace(store.config,rolling_bytes=1,retention_seconds=.001)
    store.cleanup()
    assert store.image(latest,'stone')


def test_visual_capture_requires_active_voice(store):
    from aiohttp.test_utils import TestClient, TestServer
    from aiohttp import web
    from types import SimpleNamespace
    from jshi.vision.web import install_vision
    service=VisionService(store,store.config,Model(),Detector())
    async def deliver(*a): pass
    async def run():
        app=web.Application()
        install_vision(app,SimpleNamespace(vision=service),'stone',deliver,voice_active=lambda:False)
        async with TestClient(TestServer(app)) as c:
            origin=str(c.make_url('')).rstrip('/')
            r=await c.post('/api/vision/frame',data=image(),headers={'Origin':origin})
            assert r.status==409
    asyncio.run(run())


def test_cloud_request_includes_recent_scene_without_using_it_as_image_evidence(store):
    seen=[]
    def transport(payload):
        seen.append(payload)
        return {'choices':[{'message':{'content':'观察结果'}}]}
    model=VisionModel(store.config,transport)
    model.describe(image(),scene='有人刚提到门口的桌子',previous='先前环境')
    text=seen[0]['messages'][0]['content'][0]['text']
    assert '有人刚提到门口的桌子' in text
    assert '不证明画面中的人物身份' in text


def test_stable_sampling_keeps_latest_and_archives_without_filling_every_frame(store):
    class Stable:
        def detect(self,data): return [], False
    service=VisionService(store,store.config,Model(),Stable())
    try:
        initial=service.process('stone',image(),time())
        service.process('stone',image('blue'),time())
        service.process('stone',image('green'),time())
        last=store.latest_sample('stone')
        assert last['frame'] != initial['frame']
        with store.db() as c:
            assert c.execute('select count(*) from frames').fetchone()[0] == 2
    finally: service.close()


def test_mixed_inputs_preserve_text_and_media(process):
    from tests.test_voice import FakeCloud
    from jshi.app.voice import VoiceConversation
    from jshi.core.envelope import InputEnvelope, InputPart, SceneUtterance, SpeakerEvidence
    async def run():
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            voice=SpeakerEvidence('person','lux-id','lux','recognized','voiceprint_match')
            visual=SpeakerEvidence('visual',method='visual_scene')
            u=SceneUtterance('语音内容',voice,1,2,input_id='speech',received_at_ms=10)
            v=SceneUtterance('视觉观察',visual,0,0,input_id='visual',received_at_ms=20)
            a=InputEnvelope('test','microphone',(InputPart('text','语音内容'),InputPart('audio',reference='audio')),voice,current_utterances=(u,))
            b=InputEnvelope('test','visual_environment',(InputPart('text','视觉观察'),InputPart('image',reference='photo')),visual,current_utterances=(v,),deferred_interrupt=True)
            merged=c.merge_envelopes([a,b])
            assert '语音内容' in merged.text and '视觉观察' in merged.text
            assert {p.kind for p in merged.parts}=={'text','audio','image'}
            assert merged.source=='mixed'
            assert c._jev_batch((v,))[0]['who']=='unknown'
            assert c._jev_batch((v,))[0]['voice_evidence']['strength']=='unavailable'
        finally: await c.close()
    asyncio.run(run())
