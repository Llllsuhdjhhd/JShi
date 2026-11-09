import asyncio
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jshi.app.voice import VoiceConversation
from jshi.core.envelope import SceneUtterance, SpeakerEvidence
from jshi.models import ResponseItem
from jshi.voice.contracts import Transcript
from jshi.voice.entry_queue import take_group
from jshi.voice.transcripts import same_request_wording
from jshi.voice.tts_retry import sentence_audio
from tests.test_voice import process, FakeCloud


def speech(i, text, track='a', start=100):
    return SceneUtterance(input_id=i, text=text,
                          speaker=SpeakerEvidence(track, 'person-'+track, track, 'unknown', 'voice_continuity'),
                          start_ms=start, end_ms=start+1000)


def group(u, at=10):
    return {'current': [u], 'track_id': u.speaker.track_id, 'queued': 1,
            'queued_at': at, 'collection_trigger': 'pause', 'expression_complete': True}


def test_named_call_overtakes_other_voice_backlog_without_dropping_it():
    pending = deque([group(speech('a', '游戏合成', 'tv')), group(speech('b', '匠石，你在吗？', 'user'))])
    chosen = take_group(pending, now=11)
    assert [u.input_id for u in chosen['current']] == ['b']
    assert [u.input_id for g in pending for u in g['current']] == ['a']
    assert chosen['queued'] + sum(g['queued'] for g in pending) == 2


def test_aging_and_same_voice_predecessors_are_not_starved():
    pending = deque([group(speech('a', '前半句', 'a'), 0), group(speech('b', '匠石，你在吗？', 'b'), 9)])
    assert take_group(pending, now=10)['current'][0].input_id == 'a'
    pending = deque([group(speech('a', '这个问题', 'a')), group(speech('b', '匠石，你怎么看？', 'a'))])
    assert [u.input_id for u in take_group(pending, now=11)['current']] == ['a', 'b']


def test_batch_bound_and_revision_dedup_keep_queue_accounting():
    pending = deque(group(speech(str(i), '旁聊', start=i*1000)) for i in range(18))
    assert len(take_group(pending, now=11)['current']) == 6
    host = VoiceConversation.__new__(VoiceConversation)
    host.input_records = {'a': {'transcript': Transcript('新文字', 'a', 100, 1100, True)}}
    rows = host._latest_utterances([speech('a', '旧文字'), speech('a', '新文字')])
    assert len(rows) == 1 and rows[0].text == '新文字'


@pytest.mark.parametrize('old,new,same', [('石，你还在吗？', '匠石，你还在吗？', True),
    ('关灯。', '关灯', True), ('不要，停', '不要停', False), ('关灯', '开灯', False),
    ('石头拿来', '匠石头拿来', False), ('一百元', '一千元', False)])
def test_revision_equivalence_is_conservative(old, new, same):
    assert same_request_wording(old, new) is same


def test_revision_keeps_or_replaces_only_its_own_response(process):
    async def run():
        sent = []
        async def send(m): sent.append(m)
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        try:
            # Populate a running turn without starting recognition or a remote model.
            c.input_records['a'] = {'transcript': Transcript('石，你还在吗？', 'a', 100, 1100, True), 'processing_started': True}
            c.scene.append(speech('a', '石，你还在吗？'))
            c.reply_input_ids[(0, 1)] = ['a']
            c.reply_input_ids[(0, 2)] = ['unrelated']
            c.answer_status.update({(0, 1): 'ready', (0, 2): 'ready'})
            items = (ResponseItem('verbal', '我在呢。', ('person-a',)),)
            c.pending_plans.extend([(items, 'person-a', 0, (0, 1)), (items, 'other', 0, (0, 2))])
            await c._revise_transcript(Transcript('匠石，你还在吗？', 'a', 100, 1100, True, replaces=((100, 1100),)))
            assert not c.retired_turns and len(c.pending_plans) == 2 and not c.judge_pending
            assert '保留' in c._answer_handling_context()
            await c._revise_transcript(Transcript('匠石，帮我开灯。', 'a', 100, 1100, True, replaces=((100, 1100),)))
            assert c.retired_turns == {(0, 1): 'asr_revision'}
            assert [p[3] for p in c.pending_plans] == [(0, 2)]
            assert c.judge_pending and '替换' in c._answer_handling_context()
            assert c.epoch == 0
            await c._start_plan(items, 'person-a', 0, (0, 1))
            assert not any(m['type'] == 'reply' for m in sent)
        finally:
            await c.close()
    asyncio.run(run())


def test_preserved_sentence_revision_keeps_local_uncertainty_in_runtime(process):
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            c.input_records['a']={'transcript':Transcript('陆广今天作业多吗','a',457792,458651,True)}
            note='{"preserve_utterance":true,"person_scope":"unaligned_cloud_span"}'
            await c._revise_transcript(Transcript('陆广今天作业多吗','0',457351,459002,True,
                replaces_input_ids=('a',),identity_note=note,identity_uncertain=True))
            stored=c.input_records['a']['transcript']
            assert stored.identity_uncertain and stored.identity_note==note
            assert stored.track_id=='a'  # no invented name or new input
            assert len(c.input_records)==1
        finally:await c.close()
    asyncio.run(run())


def test_late_punctuation_revision_never_requeues_completed_answer(process):
    async def run():
        async def send(m):pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            c.input_records['a']={'transcript':Transcript('匠石，有人说你笨。','a',72031,74171,True), 'processing_started':True}
            c.scene.append(speech('a','匠石，有人说你笨。'))
            c.reply_input_ids[(0,1)]=['a'];c.answer_status[(0,1)]='completed'
            await c._revise_transcript(Transcript('匠石有人说你笨','a',72031,74171,True,replaces_input_ids=('a',)))
            assert not c.judge_pending and not c.retired_turns
            assert c.input_records['a']['transcript'].text=='匠石有人说你笨'
            assert '已播完' in c._answer_handling_context()
        finally:await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('original,revised,stale', [
    ('匠石，有人说你笨。', '匠石有人说你笨', False),
    ('匠石，有人说你笨。', '匠石有人说你聪明', True),
    ('匠石有人说我有点累如果不让你安慰只让你做一个动作你会做啥',
     '匠石，有人说我有点累，如果不让你安慰，只让你做一个动作，你会做啥？', False)])
def test_revision_during_entry_keeps_equivalent_result_only(process, original, revised, stale):
    from jshi.voice.jev import VoiceJEV
    from tests.test_jev_split_modes import Model, entry, preparation
    async def run():
        async def send(m): pass
        loop = asyncio.get_running_loop()
        def revise(request):
            asyncio.run_coroutine_threadsafe(c._revise_transcript(Transcript(
                revised, 'a', 72031, 74171, True, replaces_input_ids=('a',))), loop).result(3)
            return entry(control='keep', items=[{'i':1, 'person':'unknown',
                'certainty':'unknown', 'why':'声音待定'}])
        c = VoiceConversation(process, 'stone', FakeCloud(), send,
            jev=VoiceJEV(Model([revise, preparation()]), mode='split', fallback_models=[]))
        try:
            u = speech('a', original)
            c.input_records['a'] = {'transcript': Transcript(u.text, 'a', 72031, 74171, True)}
            _, record, _ = await c._judge_once((u,), u.speaker, False)
            assert ('source' in record.get('stale_causes', ())) is stale
            if not stale:
                assert record['route'] == 'main'
        finally:
            await c.close()
    asyncio.run(run())


def test_revision_arriving_before_first_judge_does_not_judge_same_version_twice(process):
    from jshi.voice.jev import VoiceJEV
    from tests.test_jev_split_modes import Model, entry
    async def run():
        sent=[]
        async def send(m):sent.append(m)
        loop=asyncio.get_running_loop()
        archive_started=asyncio.Event();release_archive=asyncio.Event()
        def first_result(request):
            loop.call_soon_threadsafe(release_archive.set)
            asyncio.run_coroutine_threadsafe(wait_revision(),loop).result(3)
            return entry(route='background',control='keep',interaction='none',
                items=[{'i':1,'person':'unknown','certainty':'unknown','why':'声音待定'}])
        model=Model([first_result])
        c=VoiceConversation(process,'stone',FakeCloud(),send,
            jev=VoiceJEV(model,mode='split',fallback_models=[]))
        async def archive(*args,**kwargs):
            if 'transcript' in kwargs:
                archive_started.set();await release_archive.wait()
        c._archive_update=archive
        async def wait_revision():await revision
        try:
            old='匠石有人说我有点累如果不让你安慰只让你做一个动作你会做啥'
            new='匠石，有人说我非常累，如果不让你安慰，只让你做一个动作，你会做啥？'
            u=speech('stable',old)
            c.input_records['stable']={'transcript':Transcript(old,'a',34552,39732,True),'processing_started':True}
            c.scene.append(u)
            revision=asyncio.create_task(c._revise_transcript(Transcript(new,'a',34552,39732,True,replaces_input_ids=('stable',))))
            await archive_started.wait()
            c.input_idle.clear()
            c.judge_pending.append({**group(u),'queued':0});c.judge_ready.set()
            await asyncio.wait_for(c.input_idle.wait(),5)
            await revision
            assert len(model.requests)==1
            assert c.input_records['stable']['last_judged_revision_text']==new
            assert c.input_records['stable']['last_judged_revision_version']==1
            assert not c.judge_pending
            assert not any(m.get('type')=='notice' for m in sent)
            # A real intermediate correction may have invalidated old work.
            # Returning to the old wording is a new version, not a stale duplicate.
            model.answers.append(entry(route='background',control='keep',interaction='none',
                items=[{'i':1,'person':'unknown','certainty':'unknown','why':'声音待定'}]))
            c.input_idle.clear()
            for text in ('匠石，帮我关灯。',new):
                await c._revise_transcript(Transcript(text,'a',34552,39732,True,replaces_input_ids=('stable',)))
            await asyncio.wait_for(c.input_idle.wait(),5)
            assert len(model.requests)==2
            assert c.input_records['stable']['last_judged_revision_version']==3
        finally:await c.close()
    asyncio.run(run())


def test_repeated_request_retains_only_pending_same_voice_answer(process):
    async def run():
        async def send(m): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send)
        try:
            c.input_records['a'] = {'transcript': Transcript('匠石，你还在吗？', 'a', 100, 1100, True)}
            c.input_records['b'] = {}
            c.reply_input_ids[(0, 1)] = ['a']
            c.answer_status[(0, 1)] = 'ready'
            repeated = speech('b', '匠石，你还在吗？', start=5000)
            assert c._retain_repeated_requests([replace(repeated, speaker=replace(repeated.speaker, track_id='other'))])
            assert c._retain_repeated_requests([repeated]) == []
            assert c.input_records['b']['answer_reused_from'] == 'a'
            assert '不另生成' in c._answer_handling_context()
            c.answer_status[(0, 1)] = 'completed'
            assert c._retain_repeated_requests([repeated]) == [repeated]
        finally:
            await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('status,expected,retire', [
    ('ready','替换未播部分',True), ('playing','替换未播部分',True),
    ('completed','已播完',False), ('silent','尚无待播回答',False)])
def test_stable_id_revision_reports_real_answer_state(process,status,expected,retire):
    async def run():
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            c.input_records['stable']={'transcript':Transcript('开灯','a',100,1100,True),
                                       'processing_started':True}
            c.scene.append(speech('stable','开灯'))
            c.reply_input_ids[(0,1)]=['stable']
            c.answer_status[(0,1)]=status
            await c.accept(Transcript('关灯','a',50,1200,True,
                                      replaces_input_ids=('stable',)))
            assert list(c.input_records)==['stable']
            assert c.input_records['stable']['transcript'].text=='关灯'
            assert c.scene[0].text=='关灯'
            assert c.judge_pending[0]['current'][0].input_id=='stable'
            assert expected in c._answer_handling_context()
            assert ((0,1) in c.retired_turns) is retire
            if status=='completed': assert '不自动重播' in c._answer_handling_context()
        finally: await c.close()
    asyncio.run(run())


def test_accept_preserves_early_input_id_for_late_revision(process):
    async def run():
        async def send(m): pass
        c=VoiceConversation(process,'stone',FakeCloud(),send)
        try:
            await c.accept(Transcript('你好','a',100,1100,True,input_id='stable'))
            assert 'stable' in c.input_records
            await c.accept(Transcript('您好','a',80,1200,True,input_id='stable',
                                      replaces_input_ids=('stable',)))
            assert list(c.input_records)==['stable']
            assert c.input_records['stable']['transcript'].text=='您好'
        finally: await c.close()
    asyncio.run(run())


def test_tts_retry_before_audio_but_never_replays_partial_sentence():
    async def run(partial):
        calls, notes = [], []
        async def synthesize(text):
            calls.append(text)
            if len(calls) == 1:
                if partial:
                    yield b'first'
                raise RuntimeError('TTS connection closed before session finished')
            yield b'complete'
        stream = sentence_audio(SimpleNamespace(synthesize=synthesize), '一句话',
                                is_current=lambda: True, on_retry=notes.append)
        heard = []
        if partial:
            with pytest.raises(RuntimeError):
                async for pcm in stream: heard.append(pcm)
            assert heard == [b'first'] and len(calls) == 1 and not notes
        else:
            async for pcm in stream: heard.append(pcm)
            assert heard == [b'complete'] and len(calls) == 2 and len(notes) == 1
    asyncio.run(run(False))
    asyncio.run(run(True))


def test_combined_format_conflicts_are_normalized_before_validation():
    from jshi.voice.jev_serial import entry_route_conflicts
    data = {'interaction': 'none', 'route': 'wait', 'process_refs': ['I1'], 'decision_refs_note': ''}
    fixed, notes = entry_route_conflicts(data, {'properties': {}})
    assert fixed == {'interaction': 'none', 'route': 'background', 'process_refs': []}
    assert len(notes) == 3
    data['decision_refs_note'] = '有意义的额外内容'
    assert entry_route_conflicts(data, {'properties': {}})[0]['decision_refs_note'] == '有意义的额外内容'


def test_answer_handling_reaches_both_entry_and_preparation_material():
    from jshi.voice.jev_material import build_snapshot, entry_text, preparation_text
    note = '【回应处理】答源1原话“匠石，你还在吗？”：保留。同一声音重问，不另生成一份。'
    payload = {'input': {'lines': [{'i': 1, 'text': '匠石，你还在吗？', 'evidence_ref': 'I1'}]}}
    snapshot = build_snapshot(payload, [{'n': 1}], {'answer_handling': note})
    accepted = {'items': [], 'process_refs': ['I1'], 'pending_refs': [], 'interaction': 'direct'}
    assert '不另生成一份' in entry_text(snapshot)
    assert '不另生成一份' in preparation_text(snapshot, accepted)
    assert 'answer_handling' not in build_snapshot(payload, [{'n': 1}], {})['state']


def test_watchdog_records_blocking_frame_without_local_values():
    import threading
    import time
    from jshi.voice.diagnostics import LoopWatchdog
    rows = []
    watchdog = LoopWatchdog(lambda kind, payload: rows.append((kind, payload)), threading.get_ident(), threshold_s=.03)
    try:
        time.sleep(.35)
    finally:
        watchdog.close()
    assert rows and rows[0][0] == 'event_loop_blocked_stack'
    assert any(r['function'] == 'test_watchdog_records_blocking_frame_without_local_values'
               for r in rows[0][1]['stack'])
    assert all(set(r) == {'file', 'line', 'function'} for r in rows[0][1]['stack'])


@pytest.mark.parametrize('action,reason', [
    ('取消','旧回应未播部分已作废'),
    ('已播完','旧回答已发生，不撤回、不自动重播；按修订内容判断是否需要补充纠正'),
    ('替换未播部分','按修订文字重新判断；已播部分保留为已发生事实，不能撤回')])
def test_handling_note_is_in_actual_main_request(process,action,reason):
    from jshi.models import ModelResponse, ResponsePlan
    from jshi.models.prompt import build_user
    from jshi.voice.jev import VoiceJEV
    from tests.test_jev_split_modes import Model, entry, preparation
    from tests.test_voice import Model as MainModel
    class Quiet(MainModel):
        def generate(self, request):
            self.requests.append(request)
            return ModelResponse(model='quiet', response_plan=ResponsePlan(mode='ignore'))
    process.cognition = Quiet()
    process.style_packs.set('stone', 'smith')
    first = entry(control='keep', items=[{'i': 1, 'person': 'unknown', 'certainty': 'unknown', 'why': '声音待定'}])
    async def run():
        async def send(m): pass
        c = VoiceConversation(process, 'stone', FakeCloud(), send,
                              jev=VoiceJEV(Model([first, preparation()]), mode='split', fallback_models=[]))
        try:
            c._note_answer_handling(action, (), '上一条请求', reason)
            await c.accept(Transcript('匠石，解释一下', 'A', 0, 1000, True))
            await asyncio.wait_for(c.queue.join(), 4)
            await asyncio.wait_for(c.turn_queue.join(), 4)
            request = process.cognition.requests[-1]
            user = build_user(request)
            assert '【回应处理】' in user and reason in user and action in user
            assert '答源仅关联声音，不确认身份' in user
        finally:
            await c.close()
    asyncio.run(run())
