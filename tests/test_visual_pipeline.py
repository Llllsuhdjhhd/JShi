import json
from dataclasses import replace
from io import BytesIO
from time import time

import pytest
pytest.importorskip('PIL')
from PIL import Image

from jshi.vision import VisionConfig, VisionStore, VisionService
from jshi.vision.faces import LocalFaces, FaceReferences
from jshi.vision.main_media import MainImageGate
from jshi.core.prompt_profile import select_profile, normalize_hint
from jshi.memory.contracts import MemoryBatch, MemoryExperience, BackendIngestResult
from jshi.vision.memory import VisualMemory


def picture(color='red'):
    out = BytesIO()
    Image.new('RGB', (128,128), color).save(out, 'PNG')
    return out.getvalue()


@pytest.fixture
def store(tmp_path):
    return VisionStore(tmp_path/'vision', VisionConfig())


def test_gate_deduplicates_content_only_after_success_and_scopes_subject(store):
    a = store.add_frame('stone', picture(), time())
    b = store.add_frame('stone', picture(), time())
    gate = MainImageGate()
    args = dict(subject='stone', context='session', allowed=True, store=store)
    parts = [{'kind':'image', 'reference':a}, {'kind':'image', 'reference':b}]
    result, tokens, refs = gate.prepare(parts, **args)
    assert len(result) == 1 and refs == (a,)
    assert result[0]['reference'] == 'M1'
    assert len(gate.prepare(parts, **args)[0]) == 1  # failed request is retryable
    gate.commit('stone', 'session', tokens)
    assert not gate.prepare(parts, **args)[0]
    assert len(gate.prepare(parts, **{**args, 'context':'new'})[0]) == 1
    assert len(gate.prepare(parts, **args, force=True)[0]) == 1
    assert not gate.prepare(parts, **{**args, 'subject':'other'})[0]
    assert not gate.prepare(parts, **{**args, 'allowed':False})[0]


def test_image_choice_is_independent_of_level_and_legacy_defaults_omit():
    for level in (1,2,3):
        p = select_profile(('a',), ({'input_ids':['a'], 'main_prompt_hint':{
            'level':level, 'recall_memory':False, 'main_image':'include'}},))
        assert p.level == level and p.main_image == 'include'
    assert select_profile(('a',), ({'input_ids':['a'], 'main_prompt_hint':{'level':1}},)).main_image == 'omit'
    assert not normalize_hint({'level':2, 'main_image':'guess'})


def test_local_only_collects_independent_evidence_and_failure_does_not_hide_results(store):
    class Detector:
        def detect(self, data): return [{'class':'person','track':7,'box':[0,0,.5,1],'confidence':.9}], True
    class Color:
        def analyze(self, image, *, subject): return {'color':'red'}
    class Broken:
        def analyze(self, image, *, subject): raise RuntimeError('unavailable')
    class Cloud:
        def describe(self, *a, **k): pytest.fail('local-only must not call cloud')
    config = replace(store.config, cloud_enabled=False)
    service = VisionService(store, config, Cloud(), Detector(), {'color':Color(), 'broken':Broken()})
    try:
        value = service.process('stone', picture(), time())
        rows = {r['module']:r for r in value['analysis']}
        assert rows['color']['result']['color'] == 'red'
        assert rows['broken']['status'] == 'failed'
        assert rows['face']['status'] == 'not_run'
        assert value['described_at'] == 0
        assert all(r['frame'] == value['frame'] for r in rows.values())
        assert value['environment_experience']['status'] == 'not_implemented'
    finally: service.close()


def test_faces_enroll_without_names_then_main_links_multiple_references(tmp_path):
    config = VisionConfig(face_enabled=True, portrait_limit=2, face_match_threshold=.5)
    library = FaceReferences(tmp_path, config)
    first = library.match_or_enroll('stone', [1,0], picture())
    assert first['face'] == 'F1' and first['object_id'] == ''
    second = library.match_or_enroll('stone', [.8,.6], picture('blue'))
    assert second['face'] == 'F1'
    library.bind('stone', 'F1', 'lux', basis='main_visual_association')
    from jshi.recognition.photos import PersonPhotos
    assert len(PersonPhotos(tmp_path).list('stone','lux',confirmed_only=True)) == 2
    assert library.match_or_enroll('stone', [1,0], picture())['object_id'] == 'lux'
    assert library.match_or_enroll('other', [1,0], picture())['object_id'] == ''
    with pytest.raises(ValueError): library.bind('stone','F1','other',basis='main_visual_association')
    with pytest.raises(ValueError): library.bind('stone','F1','other',basis='voice_only')
    library.bind('stone','F1','other',basis='manual')
    assert not PersonPhotos(tmp_path).list('stone','lux',confirmed_only=True)
    assert PersonPhotos(tmp_path).get('stone','lux') is None
    assert len(PersonPhotos(tmp_path).list('stone','other',confirmed_only=True)) == 2


def test_face_disabled_missing_models_and_collection_off_are_distinct(tmp_path):
    module = LocalFaces(tmp_path, VisionConfig())
    assert module.analyze(picture(),subject='stone')['reason'] == 'disabled'
    module = LocalFaces(tmp_path, VisionConfig(face_enabled=True))
    assert module.analyze(picture(),subject='stone')['reason'] == 'missing_models'
    module = LocalFaces(tmp_path, VisionConfig(face_enabled=True,portrait_enabled=False),
                        extractor=lambda _: [{'box':[0,0,1,1], 'feature':[1,0], 'image':picture()}])
    assert module.analyze(picture(),subject='stone')['faces'][0]['reference_collection'] == 'disabled'
    assert not (tmp_path/'face_references.sqlite3').exists()


@pytest.mark.parametrize('retain', [True,False])
def test_main_images_reach_memory_and_backend_selects_retention(store, retain):
    frame = store.add_frame('stone', picture(), time())
    store.record_main_images('stone', ('input',), (frame,))
    class Backend:
        def ingest_batch(self,batch):
            self.batch = batch
            return BackendIngestResult('stone', stored_marks={'segment':['event']},
                                       media_marks={'segment':[frame]} if retain else {})
    backend = Backend()
    memory = VisualMemory(backend,store)
    memory.ingest_batch(MemoryBatch('batch','stone',(MemoryExperience('stone','你好',
                       source_ids=('input',),segment_id='segment'),),1,1))
    assert backend.batch.experiences[0].media[0]['reference'] == frame
    retained = store.associated('stone',event_id='event')
    assert retained
    assert bool(store.associated_many('stone',[((), 'event')],images_only=True)[0]) == retain


def test_render_maps_face_identity_without_leaking_internal_ids(store):
    frame = store.add_frame('stone',picture(),time())
    store.add_evidence('stone',frame,[{'module':'face','frame':frame,'observed_at':time(),
        'status':'completed','result':{'faces':[{'face':'F1','object_id':'internal-person-long-id','certainty':'tentative'}]}}])
    snapshot = store.publish('stone',frame,'当前图像')
    text = store.render(snapshot,(('P1','internal-person-long-id'),))
    assert 'internal-person-long-id' not in text and 'P1' in text
    assert frame not in text and snapshot['id'] not in text


def test_actual_cognition_request_uses_jev_gate_and_failure_does_not_commit(store):
    from threading import RLock
    from types import SimpleNamespace
    from jshi.subject.process import SubjectProcess, AssembledCurrentState
    from jshi.core import SubjectState
    from jshi.models import ModelResponse
    frame = store.add_frame('stone',picture(),time())
    observation = store.publish('stone',frame,'桌旁有人')
    class Cognition:
        def __init__(self): self.requests = []; self.fail = False
        def generate(self,request):
            self.requests.append(request)
            if self.fail: raise RuntimeError('failed')
            return ModelResponse(model='fake',text='你好')
    process = SubjectProcess.__new__(SubjectProcess)
    process.vision = SimpleNamespace(store=store,config=replace(store.config,cognition_images=True))
    process._main_images = MainImageGate()
    process._media_context = 'fallback'
    process._style_fields = lambda *a: ('',False)
    process._persona_fields = lambda *a: ('',None,None,0)
    process._write_lock = RLock()
    process._reconcile_pending_scene = lambda *a: None
    process.active_zone = SimpleNamespace(load=lambda *a: None)
    process.pending_scene = SimpleNamespace(render=lambda *a: '')
    process._governing_rules = lambda *a: ()
    process._model_context = lambda *a: ()
    process._record_step_input = lambda *a,**k: None
    process.style_packs = {}
    process.cognition = Cognition()
    current = AssembledCurrentState(input_text='看看',subject_state=SubjectState('stone','匠石','测试'),
                personal_items=(),context_view=None,active_segment_ids=(),recalled=(),
                media_context='session', media_source_ids=('fact',), visual_snapshot=observation,main_image='include')
    process.cognition.fail = True
    with pytest.raises(RuntimeError): process._cognize_once(current,(),activity_id='failed')
    process.cognition.fail = False
    process._cognize_once(current,(),activity_id='success')
    assert len(process.cognition.requests[-1].input_parts) == 1
    process._cognize_once(current,(),activity_id='repeat')
    assert not process.cognition.requests[-1].input_parts
    process._cognize_once(replace(current,media_context='new',main_image='omit'),(),activity_id='omit')
    assert not process.cognition.requests[-1].input_parts
    assert store.associated('stone',('fact',))


def test_two_image_comparison_is_explicit_and_keeps_order_and_single_call(store):
    from jshi.vision.model import VisionModel
    payloads=[]
    def transport(payload):
        payloads.append(payload)
        return {'choices':[{'message':{'content':json.dumps({'description':'当前有人','change_degree':'medium','change_reason':'旧图无人'})}}]}
    model=VisionModel(store.config,transport)
    old=store.add_frame('stone',picture(),time()-60)
    new=store.add_frame('stone',picture('blue'),time())
    service=VisionService(store,store.config,model)
    try:
        value=service.compare_images('stone',old,new)
        assert value['change_degree']=='medium' and len(payloads)==1
        content=payloads[0]['messages'][0]['content']
        assert len([p for p in content if p['type']=='image_url'])==2
        assert '图片1为当前图，图片2为历史比较图' in content[0]['text']
        assert '60秒' in content[0]['text']
        with pytest.raises(ValueError):service.compare_images('stone',new,old)
        assert len(payloads)==1
    finally:service.close()


def test_jev_exposes_image_choice_and_rejects_direct_reply_with_image():
    from jshi.voice.jev import VoiceJEV
    from jshi.models import ModelResponse
    class Gate:
        def generate(self,request):
            self.request=request
            return ModelResponse(model='fake',text=json.dumps({'items':[{'i':1,'person':'P1','certainty':'confirmed','why':'用户绑定'}],
                'level':3,'recall_memory':False,'main_image':'include','route':'self_reply','reply':'你好'}))
    model=Gate()
    result=VoiceJEV(model).decide_batch('stone',[{'n':1,'text':'看看新来的客人','who':'P1','source':'web_text'}],[],
                                     {'short_chat':{'allowed':True,'person_code':'P1'}})
    assert result.main_prompt_hint['main_image']=='include'
    assert not result.direct_reply
    assert '"main_image":{"enum":["omit","include"]}' in model.request.system_extra


def test_visual_route_state_and_manual_correction_are_same_origin(store):
    import asyncio
    from types import SimpleNamespace
    from aiohttp import web
    from jshi.vision.web import install_vision
    config=replace(store.config, face_enabled=True, cloud_enabled=False)
    service=VisionService(store,config)
    service.faces.extractor=lambda _: [{'box':[0,0,1,1],'feature':[1,0],'image':picture()}]
    frame=store.add_frame('stone',picture(),time())
    evidence=service.modules.analyze(picture(),frame,time(),subject='stone')
    store.add_evidence('stone',frame,evidence)
    store.publish('stone',frame,'本地现场')
    profile=SimpleNamespace(object_id='lux',label='lux')
    process=SimpleNamespace(vision=service,profiles=SimpleNamespace(list=lambda:[profile],get=lambda oid:profile if oid=='lux' else None))
    async def run():
        app=web.Application()
        async def deliver(*args):pass
        install_vision(app,process,'stone',deliver)
        routes={r.resource.canonical:r.handler for r in app.router.routes() if r.method!='HEAD'}
        def request(payload=None,origin='http://localhost'):
            async def read():return payload
            return SimpleNamespace(host='localhost',headers={'Origin':origin},json=read)
        state=json.loads((await routes['/api/vision'](request())).text)
        assert state['people']==[{'object_id':'lux','label':'lux'}]
        payload={'frame':frame,'face':'F1','object_id':'lux'}
        with pytest.raises(web.HTTPForbidden):await routes['/api/vision/associate-face'](request(payload,'http://other'))
        assert (await routes['/api/vision/associate-face'](request(payload))).status==200
        with pytest.raises(web.HTTPBadRequest):await routes['/api/vision/associate-face'](request({**payload,'face':'F9'}))
        with pytest.raises(web.HTTPBadRequest):await routes['/api/vision/compare'](request({'previous_frame':frame,'current_frame':frame}))
    try:asyncio.run(run())
    finally:service.close()


def test_identical_frames_during_initial_observation_do_not_churn_storage(store):
    from threading import Event
    entered,release=Event(),Event()
    class Detector:
        def detect(self,data):return [],False
    class Slow:
        def describe(self,*args,**kwargs):
            entered.set()
            assert release.wait(3)
            return '当前现场'
    service=VisionService(store,store.config,Slow(),Detector())
    try:
        service.process('stone',picture(),time(),background=True)
        assert entered.wait(1)
        original=store.latest_sample('stone')['frame']
        for _ in range(3):service.process('stone',picture(),time(),background=True)
        assert store.latest_sample('stone')['frame']==original
        with store.db() as c:assert c.execute('select count(*) from frames').fetchone()[0]==1
    finally:
        release.set()
        service.close()


def test_cognition_face_association_is_internal_and_does_not_become_robot_speech():
    from types import SimpleNamespace
    from jshi.subject.process import SubjectProcess
    from jshi.models import ModelResponse,ResponsePlan,ResponseItem
    calls=[]
    process=SubjectProcess.__new__(SubjectProcess)
    process.vision=SimpleNamespace(associate_face=lambda *a:calls.append(a),store=SimpleNamespace(error=lambda *a:None))
    process.profiles=SimpleNamespace(get=lambda oid:object() if oid=='lux' else None)
    process.repository=SimpleNamespace(add_history=lambda record:None)
    current=SimpleNamespace(visual_snapshot={'frame':'photo'},object_codes=(('P1','lux'),))
    response=ModelResponse(model='fake',response_plan=ResponsePlan('respond',items=(
        ResponseItem('embodied','关联面孔 F1 P1：对方明确指认'),ResponseItem('verbal','你好'))))
    result=process._apply_visual_associations('stone',current,response)
    assert calls==[('stone','photo','F1','lux')]
    assert [item.text for item in result.response_plan.items]==['你好']
    calls.clear()
    process._apply_visual_associations('stone',current,response,allowed=False)
    assert not calls
