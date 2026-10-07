import asyncio
import json
from dataclasses import replace
from threading import Event

import pytest

from jshi.app.voice import VoiceConversation
from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from jshi.models import ModelResponse
from jshi.voice.jev import VoiceJEV, BatchItem
from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter, recent_material, scene_refresh_reason
from jshi.voice.volc import Transcript
from tests.test_voice import process, FakeCloud


def test_commit_covers_only_snapshot_and_survives_restart(tmp_path):
    path = tmp_path / 'jev.json'
    store = JEVSceneStore(path)
    store.append('a', {'text': '先问去哪', 'at': 1}, codes={'P1': 'person-id'})
    snap = store.snapshot()
    store.append('b', {'text': '杭州', 'at': 2})
    assert store.commit(snap, 'P1在讨论去哪。')
    restored = JEVSceneStore(path).snapshot()
    assert restored['scene'] == 'P1在讨论去哪。'
    assert [r['id'] for r in restored['events']] == ['b']
    assert restored['codes'] == {'P1': 'person-id'}
    assert not store.commit(snap, '旧任务晚到')


def test_scene_idle_refresh_count_chars_age_and_recent_main_guard():
    snap = {'events': [{'id': str(n), 'payload': {'text': '一段输入'}} for n in range(6)]}
    assert scene_refresh_reason(snap, idle_seconds=14, pending_seconds=90) == ''
    assert scene_refresh_reason(snap, idle_seconds=16, pending_seconds=16) == 'idle_input_count'
    snap['events'] = [{'id': 'a', 'payload': {'text': '字' * 800}}]
    assert scene_refresh_reason(snap, idle_seconds=16, pending_seconds=16) == 'idle_input_chars'
    snap['events'][0]['payload']['text'] = '少量旁谈'
    assert scene_refresh_reason(snap, idle_seconds=16, pending_seconds=29) == ''
    assert scene_refresh_reason(snap, idle_seconds=30, pending_seconds=30) == 'idle_pending_age'
    assert scene_refresh_reason({'events': []}, idle_seconds=100, pending_seconds=100) == ''


def test_background_only_inputs_refresh_scene_without_starting_main(process):
    class Gate:
        def generate(self, req):
            rows = json.loads(req.input_text)['input']['lines']
            return ModelResponse(model='fake', text=json.dumps({
                'items':[{'n':r['i'],'to_jiangshi':'no','relevance':'unrelated'} for r in rows]}))
    class Writer:
        def generate(self, req):
            return ModelResponse(model='fake', text=json.dumps({'scene':'电视在播放英文叙事，两句是背景声音，尚无向匠石的交往。'}))
    process.person_review_model = Writer()
    async def run():
        async def send(m): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send, jev=VoiceJEV(Gate()), input_pause_s=.01,
                              scene_idle_s=0, scene_batch_inputs=2, scene_batch_chars=10000)
        try:
            await c.accept(Transcript('媒体台词一', 'A', 0, 1000, True))
            await c.accept(Transcript('媒体台词二', 'A', 1000, 2000, True))
            await asyncio.wait_for(c.queue.join(), 2)
            async def wait_scene():
                while not c.jev_scene_store.snapshot()['version']:
                    await asyncio.sleep(.02)
            await asyncio.wait_for(wait_scene(), 3)
            assert not process.cognition.requests
            rows = [r for r in process.repository.list_history('stone') if r.event_type == 'voice_jev_scene']
            assert rows[-1].content['trigger'] == 'idle_input_count'
            assert not c.jev_scene_store.snapshot()['events']
        finally:
            await c.close()
    asyncio.run(run())


def test_recent_window_caps_material_and_archives_backlog_without_replaying_it(tmp_path):
    store = JEVSceneStore(tmp_path / 'scene.json')
    store.append('correction:1', {'text': '人工确认P2是luguang，不是lux', 'at': 1})
    for n in range(40):
        store.append(str(n), {'text': f'第{n}句' + '材料' * 100, 'at': n + 2})
    snap = store.snapshot()
    rows, reference = recent_material(snap, '\n'.join(f'B{n}  完整块' + '参考' * 100 for n in range(8)))
    assert snap['material_window']['chars'] <= 3000
    assert rows[-1]['text'].startswith('第39句')
    assert rows[0]['text'].startswith('人工确认')
    assert all('完整块' in block for block in reference.splitlines())
    store.append('new-after-snapshot', {'text': '下一轮新话', 'at': 100})
    assert store.commit(snap, '正在进行最新对话。')
    assert [r['id'] for r in store.snapshot()['events']] == ['new-after-snapshot']
    archive = json.loads(store.path.with_suffix('.archive.jsonl').read_text(encoding='utf-8'))
    assert {r['id'] for r in archive['events']} == set(snap['material_window']['archived_input_ids'])
    assert all(len(r['payload']['text']) > 200 for r in archive['events'])


def test_scene_writer_marks_omissions_and_never_silently_truncates_large_event(tmp_path):
    class Model:
        def generate(self, request):
            self.data = json.loads(request.input_text)
            return ModelResponse(model='fake', text='{"scene":"本轮长材料未载入，不推断已解决。"}')
    store = JEVSceneStore(tmp_path / 'scene.json')
    store.append('large', {'text': '不是' + '字' * 5000, 'at': 1})
    store.append('recent', {'text': '等一下，先别继续', 'at': 2})
    snap = store.snapshot()
    model = Model()
    scene, _ = JEVSceneWriter(model).run('stone', snap, 'B1  旧参考' + '字' * 4000 + '\nB2  最新完整参考')
    assert model.data['window']['omitted_events'] == 1
    assert model.data['input_output'] == [{'text': '等一下，先别继续', 'at': 2}]
    assert model.data['main_scene'] == 'B2  最新完整参考'
    assert store.commit(snap, scene)
    archive = json.loads(store.path.with_suffix('.archive.jsonl').read_text(encoding='utf-8'))
    assert archive['events'][0]['payload']['text'] == '不是' + '字' * 5000


def test_failed_window_commit_keeps_unread_records_and_writes_no_archive(tmp_path):
    store = JEVSceneStore(tmp_path / 'scene.json')
    store.append('large', {'text': '字' * 5000})
    snap = store.snapshot()
    recent_material(snap, '')
    store.correct({'text': '人工纠正'})
    assert not store.commit(snap, '过时片场')
    assert not store.path.with_suffix('.archive.jsonl').exists()
    assert store.snapshot()['events'][0]['id'] == 'large'


def test_manual_correction_invalidates_inflight_scene(tmp_path):
    store = JEVSceneStore(tmp_path / 'jev.json')
    store.append('a', {'text': '姓名线索'})
    snap = store.snapshot()
    store.correct({'text': '人工确认是另一人'})
    assert not store.commit(snap, '错误姓名')
    assert len(store.snapshot()['events']) == 2


def test_oversized_scene_does_not_lose_pending_events(tmp_path):
    store = JEVSceneStore(tmp_path / 'jev.json')
    store.append('a', {'text': '未解决提问'})
    with pytest.raises(ValueError):
        store.commit(store.snapshot(), '字' * 1001)
    assert store.snapshot()['scene'] == ''
    assert store.snapshot()['events'][0]['id'] == 'a'


def test_first_stage_is_compact_and_only_selects_prompt():
    class Model:
        def generate(self, req):
            self.req = req
            payload = json.loads(req.input_text)
            assert set(payload) == {'input', 'scene', 'now'}
            assert payload['input']['unwritten'][0]['text'] == '要我帮你查天气吗？'
            assert 'long-internal-input-id' not in req.input_text
            assert payload['input']['lines'][0]['p'] == 'P1'
            assert payload['input']['lines'][0]['name'] == 'lux'
            schema = json.loads(req.system_extra.split('Schema：')[1])
            assert 'tool_needed' not in schema['properties']
            assert set(schema['properties']) == {'items', 'level', 'recall_memory'}
            assert set(schema['properties']['items']['items']['properties']) == {'i','person','certainty','why'}
            return ModelResponse(model='gate', text=json.dumps({'level':1, 'recall_memory':False,
                'items':[{'i':1,'person':'P1','certainty':'confirmed','why':'本句声纹明确，无具体冲突'}]}))
    model = Model()
    result = VoiceJEV(model).decide_batch('stone', [{'n':1,'input_id':'long-internal-input-id',
        'who':'P1（lux）','text':'要','voice_evidence':{'pick':'P1','strength':'clear'},
        'candidates':[{'who':'P1','score':.5,'source':'voiceprint'}]}],
        [{'p':'匠石','basis':'已播出','text':'要我帮你查天气吗？'}], {'jev_scene':'之前在闲聊','state':'completed'})
    assert result.main_prompt_hint['level'] == 1
    assert result.items[0].speaker_pick == 'P1'


def test_invalid_batch_output_falls_back_without_false_confirmation():
    class Model:
        def generate(self, req):
            return ModelResponse(model='gate', text='{"level":3,"items":[],"control":"pass"}')
    result = VoiceJEV(Model()).decide_batch('stone', [{'n':1,'text':'匠石你好'}], [], {})
    assert not result.judged and result.model_error
    assert result.items[0].to_jiangshi == 'maybe'
    assert not result.main_prompt_hint


def test_exact_supplied_display_labels_do_not_discard_whole_batch():
    batch = [dict(n=1, who='P1（lux）', text='你好', voice_evidence={'pick':'P1','strength':'clear'}),
             dict(n=2, who='待定声音', text='好不开心啊', voice_evidence={'pick':'','strength':'unavailable'})]
    result = VoiceJEV()._from_current([
        dict(i=1,person='P1（lux）',certainty='confirmed',why='本句声纹明确'),
        dict(i=2,person='待定声音',certainty='unknown',to='maybe',keep='uncertain',why='暂不能确定')], batch, '')
    assert [i.speaker_pick for i in result.items] == ['P1','unknown']
    with pytest.raises(ValueError):
        VoiceJEV()._from_current([dict(i=1,person='P1（另一人）',certainty='confirmed',to='yes',keep='related')],batch[:1],'')


def test_short_audio_context_confirmation_reaches_final_identity_and_ui(process):
    async def run():
        sent=[]
        async def send(m): sent.append(m)
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            code=c.assign_code('lux-id','lux')
            initial=SpeakerEvidence('1',label='声音归属待定',method='unassigned_audio')
            u=SceneUtterance('好不开心啊。',initial,0,1410,input_id='short')
            c.source_speakers['short']=initial
            c.candidate_cache['short']=[{'object_id':'lux-id','source':'repeated_short_audio'}]
            item=VoiceJEV()._from_current([dict(i=1,person=code,certainty='confirmed',to='yes',keep='related',
                why='辅助比较lux明显领先，紧接lux的假期感受，无换人线索')],
                [dict(n=1,who='待定声音',voice_evidence={'pick':'','strength':'unavailable'},candidates=[{'who':code}])],'').items[0]
            final=c._apply_attribution([u],[item])
            assert final[0].speaker.object_id=='lux-id'
            assert final[0].speaker.status=='recognized'
            await c._publish_attribution(final,[item],{'judged':True})
            assert sent[-1]['type']=='identity_final'
            assert sent[-1]['speaker']['label']=='lux'
            assert sent[-1]['voice_initial']['label']=='声音归属待定'
            assert sent[-1]['jev_status']=='completed'
            await c._publish_attribution([u],[],{'judged':False,'model_error':'invalid identity'})
            assert sent[-1]['jev_status']=='failed'
            assert sent[-1]['certainty']==''
        finally:
            await c.close()
    asyncio.run(run())


def test_final_jev_can_rebut_voice_but_preserves_manual_annotation(process):
    async def run():
        async def send(m): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        try:
            p1 = c.assign_code('lux-id', 'lux')
            from jshi.recognition import ObjectProfile
            process.profiles.create(ObjectProfile('other-id', '另一人'))
            p2 = c.assign_code('other-id', '另一人')
            initial = SpeakerEvidence('A','lux-id','lux','recognized','voiceprint_match',.5)
            u = SceneUtterance('是我，另一人',initial,0,2000,input_id='a')
            c.source_speakers['a'] = initial
            c.candidate_cache['a'] = [{'object_id':'lux-id'}, {'object_id':'other-id'}]
            verdict = BatchItem(1,speaker_pick=p2,speaker_level='确定',speaker_reason='有区别于lux的明确语义依据')
            final = c._apply_attribution([u], [verdict])[0]
            assert final.speaker.object_id == 'other-id'
            assert final.speaker.status == 'recognized'
            assert c.source_speakers['a'] == initial
            manual = replace(u, speaker=replace(initial,method='manual_annotation'))
            assert c._apply_attribution([manual],[verdict])[0].speaker.object_id == 'lux-id'
        finally:
            await c.close()
    asyncio.run(run())


def test_unwritten_output_is_visible_until_scene_covers_it(process):
    async def run():
        async def send(m): pass
        c = VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            c.jev_scene_store.append('played',{'p':'匠石','text':'要查天气吗？','basis':'已播出','at':1})
            u=SceneUtterance('要',SpeakerEvidence('A'),0,1000,input_id='new',received_at_ms=2)
            context,_=c._jev_context([u])
            assert context[0]['text']=='要查天气吗？'
            snap=c.jev_scene_store.snapshot()
            assert c.jev_scene_store.commit(snap,'匠石已经问要不要查天气，正在等回答。')
            from time import time
            assert c._jev_context([replace(u,received_at_ms=time()*1000+1)])[0]==[]
        finally:
            await c.close()
    asyncio.run(run())


def test_same_session_restore_keeps_pending_and_short_code_mapping(process):
    async def run():
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send,session_id='resume123')
        code=c.assign_code('lux-id','lux')
        c.jev_scene_store.append('old',{'text':'还没有整理'},codes=c.code_owner,labels=c.code_labels)
        await c.close()
        restored=VoiceConversation(process,'stone',FakeCloud(),send,session_id='resume123')
        try:
            assert restored.assign_code('lux-id','lux')==code
            assert restored.jev_scene_store.snapshot()['events'][0]['id']=='old'
        finally:
            await restored.close()
    asyncio.run(run())


def test_scene_completed_after_input_cannot_add_future_context(tmp_path, monkeypatch):
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('early', {'text':'此前实际说过', 'at':10})
    snap = store.snapshot()
    monkeypatch.setattr('jshi.voice.jev_scene.time',lambda: .030)
    assert store.commit(snap,'后来才完成的场面')
    store.append('late',{'text':'之后才播出', 'at':40})
    scene, context = store.view(20)
    assert scene == ''
    assert [r['text'] for r in context] == ['此前实际说过']
    assert store.view(35)[0] == '后来才完成的场面'


def test_background_notification_does_not_shift_filtered_batch_judgments(process):
    async def run():
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            who=SpeakerEvidence('A')
            first=SceneUtterance('无关旁话',who,0,1000,input_id='first')
            second=SceneUtterance('向匠石说话',who,1000,2000,input_id='second')
            items=(BatchItem(1,'no',relevance='unrelated'),BatchItem(2,'yes',relevance='related'))
            c._capture_jev_inputs((first,second),items)
            c.schedule_person_review((second,),items)
            rows={r['id']:r['payload'] for r in c.jev_scene_store.snapshot()['events']}
            assert rows['second']['judgment']['to']=='yes'
            assert rows['first']['judgment']['to']=='no'
        finally:
            await c.close()
    asyncio.run(run())
