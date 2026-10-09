import asyncio
import json
import sqlite3
from io import BytesIO
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jshi.app.voice import VoiceConversation, create_app
from jshi.app.web_data import WebData
from jshi.voice.config import VoiceConfig
from tests.test_voice import FakeCloud, process


def test_person_photo_binding_requires_existing_person_and_same_origin(process):
    from PIL import Image
    from jshi.recognition.photos import PersonPhotos
    out = BytesIO()
    Image.new('RGB', (48, 64), 'blue').save(out, 'PNG')
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(api_key='')))) as client:
            url = str(client.make_url('')).rstrip('/')
            assert (await client.get('/api/person-photo/lux-id')).status == 404
            assert (await client.post('/api/person-photo/lux-id', data=out.getvalue())).status == 403
            assert (await client.post('/api/person-photo/missing', headers={'Origin':url}, data=out.getvalue())).status == 404
            assert (await client.post('/api/person-photo/lux-id', headers={'Origin':url}, data=b'bad')).status == 400
            response = await client.post('/api/person-photo/lux-id', headers={'Origin':url}, data=out.getvalue())
            assert response.status == 200
            response = await client.get('/api/person-photo/lux-id')
            assert response.status == 200 and response.content_type == 'image/jpeg'
            with Image.open(BytesIO(await response.read())) as image:
                assert image.size == (48,64)
            assert (await client.get('/api/person-photo/lux-id',headers={'Sec-Fetch-Site':'cross-site'})).status == 403
    asyncio.run(run())
    store = PersonPhotos(process.repository.path.parent)
    assert store.get('other','lux-id') is None
    assert store.get('stone','lux-id') is not None


def test_data_search_is_scoped_paginated_and_read_only(tmp_path):
    path = tmp_path/'subject.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('create table histories(sequence integer primary key, subject_id text, content text)')
        c.executemany('insert into histories values(?,?,?)', [(i,'stone',json.dumps({'text':'needle '+str(i)})) for i in range(25)])
        c.execute('insert into histories values(99,?,?)',('other','needle private'))
    before = path.read_bytes()
    browser = WebData(tmp_path,'stone')
    result = browser.read('subject:histories',query='needle',page=1)
    assert result['total'] == 25 and len(result['rows']) == 5
    assert result['rows'][0]['content']['text'] == 'needle 4'
    assert path.read_bytes() == before
    assert browser.read('subject:histories',query='%')['total'] == 0
    with pytest.raises(ValueError): browser.read('../../voiceprints.json')


def test_placement_view_filters_events_without_changing_records(tmp_path):
    path=tmp_path/'subject.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('create table histories(sequence integer primary key,subject_id text,event_type text,content text)')
        c.executemany('insert into histories values(?,?,?,?)',[(1,'stone','external_input','原话'),
            (2,'stone','object_resolved','人物落位'),(3,'stone','other','不相关'),
            (4,'other','external_input','另一主体')])
    before=path.read_bytes()
    result=WebData(tmp_path,'stone').read('memory_placement')
    assert result['total']==2
    assert [r['event_type'] for r in result['rows']]==['object_resolved','external_input']
    assert path.read_bytes()==before


def test_logs_include_prompt_body_and_handle_partial_append(tmp_path):
    (tmp_path/'step_inputs.jsonl').write_text(json.dumps({'kind':'prompt','hash':'abc','text':'system rules'})+'\n'+
        json.dumps({'kind':'call','system_hash':'abc','user_text':'actual user'})+'\n{"partial":',encoding='utf-8')
    result = WebData(tmp_path,'stone').read('prompts',query='actual user')
    assert result['rows'][0]['system_text'] == 'system rules'
    assert WebData(tmp_path,'stone').read('tool_work')['rows'] == []


def test_saved_debug_buttons_work_without_voice_connection(process):
    root = process.repository.path.parent
    (root/'activity_timings.jsonl').write_text(json.dumps({'activity_id':'turn-1','subject_id':'stone',
        'started_at':'2026-10-07T04:00:00Z','total_ms':123,
        'voice':{'jev_calls':[{'prompt_activity_id':'jev-1'}]}})+'\n',encoding='utf-8')
    rows = [{'kind':'prompt','hash':'rules','text':'【人物判断方法】\n识别人。'},
        {'kind':'call','activity_id':'jev-1','subject_id':'stone','purpose':'voice_jev',
         'model':'fake','system_hash':'rules','user_text':'实际输入'},
        {'kind':'call','activity_id':'turn-1','subject_id':'stone','purpose':'subject_activity',
         'model':'fake','system_hash':'rules','user_text':'认知输入'}]
    (root/'step_inputs.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
    (root/'tool.jsonl').write_text(json.dumps({'activity_id':'turn-1','status':'completed'})+'\n',encoding='utf-8')
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(api_key='')))) as client:
            turns = await (await client.get('/api/debug?section=turns')).json()
            assert turns['turns'][0]['activity_id'] == 'turn-1'
            prompt = await (await client.get('/api/debug?section=prompt&purpose=voice_jev&part=user')).json()
            assert '实际输入' in prompt['text'] and '认知输入' not in prompt['text']
            part = await (await client.get('/api/debug?section=prompt&part=人物判断方法')).json()
            assert '识别人' in part['text']
            timing = await (await client.get('/api/debug?section=timing')).json()
            assert '123' in timing['text']
            tool = await (await client.get('/api/debug?section=tool')).json()
            assert 'completed' in tool['text']
            missing = await (await client.get('/api/debug?section=prompt&activity_id=missing')).json()
            assert '没有' in missing['text']
            assert (await client.get('/api/debug?section=bad')).status == 400
            assert (await client.get('/api/debug',headers={'Sec-Fetch-Site':'cross-site'})).status == 403
    asyncio.run(run())


def test_text_without_microphone_preserves_selected_identity(process):
    async def run():
        app = create_app(process,'stone',VoiceConfig(api_key=''))
        async with TestClient(TestServer(app)) as client:
            people = await (await client.get('/text-people')).json()
            assert any(p['object_id']=='lux-id' for p in people['people'])
            url = str(client.make_url('')).rstrip('/')
            forbidden = await client.post('/text-input',json={'text':'hello','object_id':'lux-id'})
            assert forbidden.status == 403
            invalid = await client.post('/text-input',headers={'Origin':url},json={'text':'hello','object_id':'missing'})
            assert invalid.status == 400
            response = await client.post('/text-input',headers={'Origin':url},json={'text':'你好\n帮我看看','object_id':'lux-id'})
            assert response.status == 200, await response.text()
            assert (await response.json())['label']=='lux'
            assert process.last_model_response is not None
            browser = await client.get('/api/data?source=subject:histories&q=web_text')
            assert browser.status == 200 and (await browser.json())['total'] > 0
            assert (await client.get('/data')).status == 200
    asyncio.run(run())


def test_selected_text_joins_voice_queue_without_voice_identification(process):
    async def run():
        emitted=[]
        async def send(m): emitted.append(m)
        c = VoiceConversation(process,'stone',FakeCloud(),send)
        c.turn_worker.cancel()
        await asyncio.gather(c.turn_worker,return_exceptions=True)
        try:
            await c.typed('来自文字的发言','lux-id')
            envelope,_ = c.turn_queue.get_nowait()
            c.turn_queue.task_done()
            assert envelope.source=='web_text'
            assert envelope.speaker.object_id=='lux-id'
            assert envelope.speaker.method=='user_selected'
            assert envelope.current_utterances[0].identity_note.startswith('文字输入')
            assert all(p.kind!='audio' for p in envelope.parts)
            assert emitted[-1]['type']=='typed_input'
            snap=c.jev_scene_store.snapshot()
            assert '来自文字的发言' in json.dumps(snap,ensure_ascii=False)
            await c.typed('wrong','missing')
            assert emitted[-1]['type']=='typed_error' and c.turn_queue.empty()
        finally: await c.close()
    asyncio.run(run())


def test_selected_text_completes_in_connected_voice_session(process):
    async def run():
        emitted = []
        async def send(message): emitted.append(message)
        conversation = VoiceConversation(process, 'stone', FakeCloud(), send)
        try:
            for text in ('哈喽', '再聊一句'):
                await conversation.typed(text, 'lux-id')
                await asyncio.wait_for(conversation.turn_queue.join(), 3)
            assert len(process.cognition.requests) == 2
            assert len(conversation.debug_history) == 2
            for turn in conversation.debug_history:
                assert turn['input']['source'] == 'web_text'
                assert turn['timing']['voice']['asr_lag_ms'] is None
                assert turn['timing']['voice']['speech_end_at_ms'] is None
            assert not any(m['type'] == 'notice' and '主流程调用失败' in m.get('text', '') for m in emitted)
        finally:
            await conversation.close()
    asyncio.run(run())


def test_standalone_text_requests_are_serialized(process):
    from threading import Event
    entered,release=Event(),Event()
    original=process.experience
    def blocked(*args,**kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args,**kwargs)
    process.experience=blocked
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(api_key='')))) as client:
            origin=str(client.make_url('')).rstrip('/')
            task=asyncio.create_task(client.post('/text-input',headers={'Origin':origin},json={'text':'你好','object_id':'lux-id'}))
            try:
                while not entered.is_set(): await asyncio.sleep(.01)
                second=await client.post('/text-input',headers={'Origin':origin},json={'text':'再次','object_id':'lux-id'})
                assert second.status==409
            finally: release.set()
            assert (await task).status==200
    asyncio.run(run())


def test_text_reply_and_next_turn_do_not_wait_for_scene_write(process):
    from threading import Event
    entered, release = Event(), Event()
    original = process._flush_pending_scene
    def slow_write(subject_id):
        entered.set()
        assert release.wait(10)
        return original(subject_id)
    process._flush_pending_scene = slow_write
    async def run():
        async with TestClient(TestServer(create_app(process,'stone',VoiceConfig(api_key='')))) as client:
            origin = str(client.make_url('')).rstrip('/')
            try:
                first = await asyncio.wait_for(client.post('/text-input',headers={'Origin':origin,
                    'Accept':'application/x-ndjson'},json={'text':'你好','object_id':'lux-id'}),3)
                events = [json.loads(line) for line in (await asyncio.wait_for(first.text(),3)).splitlines()]
                assert [e['type'] for e in events] == ['accepted','reply','complete']
                assert events[-1]['scene_write'] == 'background'
                while not entered.is_set(): await asyncio.sleep(.01)
                assert (await (await client.get('/status')).json())['text_scene_writing']
                second = await asyncio.wait_for(client.post('/text-input',headers={'Origin':origin},
                    json={'text':'再聊一句','object_id':'lux-id'}),3)
                assert second.status == 200
                assert (await second.json())['scene_write'] == 'background'
                assert not release.is_set()
            finally:
                release.set()
    asyncio.run(run())
