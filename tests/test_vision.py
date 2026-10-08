import asyncio
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


def test_image_adapter_sends_real_image_parts():
    payloads = []
    def send(p):
        payloads.append(p)
        return {'choices':[{'message':{'content':'两个人在桌旁。'}}]}
    model = VisionModel(VisionConfig(), send)
    assert model.describe(b'jpeg') == '两个人在桌旁。'
    block = payloads[0]['messages'][0]['content'][1]
    assert block['type']=='image_url' and block['image_url']['url'].startswith('data:image/jpeg;base64,')


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


def test_jev_judges_environment_and_invalid_output_is_not_auto_wakeup():
    from jshi.models import ModelResponse
    class Judge:
        def generate(self, r): self.request=r; return ModelResponse(text='{"enter_main":true,"reason":"有人示意"}', model='fake')
    model=Judge(); jev=VoiceJEV(model)
    assert jev.decide_environment('stone',{'description':'有人'})['enter_main']
    assert model.request.purpose=='environment_jev'
    assert not VoiceJEV().decide_environment('stone',{})['enter_main']


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
    class Judge:
        model=None
        def decide_environment(self,*args): return {'enter_main':True,'reason':'变化'}
    async def run():
        app=web.Application(client_max_size=10*1024*1024)
        install_vision(app,p,'stone',Judge(),deliver)
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
