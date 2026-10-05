from concurrent.futures import ThreadPoolExecutor
from threading import Event

from jshi.models import ModelResponse
from jshi.models.prompt import build_user
from jshi.subject import SubjectProcess
from jshi.subject.pending_scene import PendingScene
from jshi.style import SMITH, ZoneStore
from jshi.core.unknown_inputs import UnknownInputs
from tests.test_write_zone_split import runtime, ReplyModel, WriteModel


class RecordingReply(ReplyModel):
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return super().generate(request)


def test_slow_write_does_not_block_cognition_and_next_batch_covers_all_new_rounds(tmp_path):
    entered, release = Event(), Event()

    class Writer(WriteModel):
        def __init__(self):
            super().__init__()
            self.requests = []

        def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                entered.set()
                assert release.wait(10)
            return ModelResponse(model=self.name, rewritten_context=build_user(request))

    reply, writer = RecordingReply(), Writer()
    process, _ = runtime(tmp_path, reply, writer)
    first = process.experience('stone', '第一轮输入', object_ref='user', defer_write=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(process.take_deferred_write())
        try:
            assert entered.wait(5)
            second = process.experience('stone', '第二轮输入', object_ref='user', defer_write=True)
            third = process.experience('stone', '第三轮输入', object_ref='user', defer_write=True)
            assert not writing.done()
            visible = build_user(reply.requests[-1])
            assert all(text in visible for text in ('第一轮输入', '第二轮输入', '你好，我接着说。'))
            assert '尚未整理的输入输出' in visible
        finally:
            release.set()
        assert writing.result(timeout=5)
    assert [r['id'] for r in process.pending_scene.snapshot('stone')] == [second.activity.id, third.activity.id]
    assert process.activity_ledger.current_context_view('stone').last_applied_sequence == 2
    assert process.take_deferred_write()()
    assert len(writer.requests) == 2
    batch = build_user(writer.requests[1]).rsplit('【本次合批整理的输入输出】', 1)[1]
    assert '第一轮输入' not in batch
    assert '第二轮输入' in batch and '第三轮输入' in batch
    assert batch.count('你好，我接着说。') == 2
    assert not process.pending_scene.snapshot('stone')


def test_failed_batch_is_rewritten_with_new_inputs_and_replies(tmp_path):
    class Writer(WriteModel):
        def generate(self, request):
            self.calls += 1
            if self.calls <= 2:
                raise RuntimeError('first batch failed')
            self.last_request = request
            return ModelResponse(model=self.name, rewritten_context=build_user(request))

    writer = Writer()
    process, _ = runtime(tmp_path, ReplyModel(), writer)
    process.experience('stone', '失败前输入', object_ref='user', defer_write=True)
    assert process.take_deferred_write()() is False
    assert PendingScene(tmp_path / 'pending_scene.json').snapshot('stone')
    process.experience('stone', '失败后输入', object_ref='user', defer_write=True)
    assert process.take_deferred_write()()
    text = build_user(writer.last_request)
    assert '失败前输入' in text and '失败后输入' in text
    assert not process.pending_scene.snapshot('stone')


def test_restart_after_scene_commit_before_journal_cleanup_does_not_duplicate_events(tmp_path, monkeypatch):
    process, repo = runtime(tmp_path, ReplyModel(), WriteModel())
    path = tmp_path / 'zone.json'
    process.zone_store = ZoneStore(path)
    process.style_packs.set('stone', SMITH)
    process.zone_store.boot('stone', '我是匠石', ('已有场景',))
    process.write_zone = ReplyModel()
    process.experience('stone', '已经提交过的输入', object_ref='user', defer_write=True)
    def failed_cleanup(ids):
        raise OSError('simulate exit after committed scene')
    monkeypatch.setattr(process.pending_scene, 'complete', failed_cleanup)
    assert process.take_deferred_write()() is False
    assert process.pending_scene.snapshot('stone')
    restored = SubjectProcess(repo, process.identities, ReplyModel(), zone_store=ZoneStore(path))
    scene = restored._zone_text_for_turn('stone', None, live=False)
    assert scene.count('已经提交过的输入') == 1
    assert not restored.pending_scene.snapshot('stone')


def test_unknown_archive_is_bounded_and_manual_binding_is_input_specific(tmp_path):
    store = UnknownInputs(tmp_path / 'unknown.sqlite3')
    assert store.append('one', 'unknown-A', '第一句', 'session', 0, 1)
    assert store.append('two', 'unknown-A', '第二句', 'session', 1, 2)
    store.bind(('one',), 'known-person')
    assert store.read('one')['manual_object_id'] == 'known-person'
    assert store.read('two')['manual_object_id'] is None
    store.max_bytes = 1
    assert not store.append('three', 'unknown-B', '不再扩容', 'session', 2, 3)
    assert store.read('one')['text'] == '第一句'
    assert store.read('three') is None


def test_journal_failed_save_does_not_leave_unpersisted_event_in_memory(tmp_path, monkeypatch):
    store = PendingScene(tmp_path / 'pending.json')
    def fail():
        raise OSError('disk unavailable')
    monkeypatch.setattr(store, '_save', fail)
    import pytest
    with pytest.raises(OSError):
        store.append('stone', 'one', 'input')
    assert not store.snapshot('stone')


def test_unknown_text_envelope_has_same_input_structure_without_an_identity_gate(tmp_path):
    from jshi.core.envelope import InputEnvelope, InputPart, SpeakerEvidence
    reply = RecordingReply()
    process, _ = runtime(tmp_path, reply, WriteModel())
    envelope = InputEnvelope('session', 'text', (InputPart('text', '我想问一个问题'),),
                             SpeakerEvidence('text', confidence=.1))
    result = process.experience('stone', envelope.text, envelope=envelope, defer_write=True)
    assert result.current_state.audio_delivery is False
    assert envelope.speaker.object_id.startswith('input:')
    assert process.profiles.get(envelope.speaker.object_id) is None
    item = reply.requests[0].input_items[0]
    assert item['input_id'] == envelope.input_id
    assert item['identity_level'] == '不确定' and item['source'] == 'text'
    assert '人物：不确定' in build_user(reply.requests[0])
    assert process.unknown_inputs.read(envelope.input_id)['actor_id'] == envelope.speaker.object_id


def test_input_preserves_sparse_batch_numbers_and_four_identity_levels():
    from jshi.core.main_input import IDENTITY_LEVELS, make_input, render_inputs
    assert IDENTITY_LEVELS == ('确定', '可能', '不太可能', '不确定')
    item = make_input('global-id', 'unknown', '待定声音', '你好', number='N2',
                      level='不太可能', evidence='入口存在冲突')
    text = render_inputs((item,))
    assert text.startswith('2. 待定声音：你好')
    assert '人物：不太可能' in text and '%' not in text


def test_tool_brief_deduplicates_without_losing_error_or_task_reference():
    from jshi.tool.service import format_tool_related
    text = format_tool_related(({
        'section': 'recent', 'id': 'original-task-id', 'need': '查天气',
        'tool_name': '天气', 'tool_description': '天气', 'status': 'FAILED',
        'new_info': '请求超时\n请求超时', 'final_result': '请求超时',
        'end_reason': '服务暂时不可用', 'work_open': True,
    },), task_codes={'original-task-id': 'T1'})
    assert '任务ID：T1' in text and 'original-task-id' not in text
    assert text.count('请求超时') == 1
    assert 'FAILED' in text and '服务暂时不可用' in text
    assert '尚未交付完毕' in text


def test_overlapping_modes_share_one_cognition_call(tmp_path):
    reply = RecordingReply()
    process, _ = runtime(tmp_path, reply, WriteModel())
    process.experience('stone', '观察并交流', object_ref='user', defer_write=True,
                       processing_modes=('interaction', 'environment', 'interaction'))
    assert len(reply.requests) == 1
    assert reply.requests[0].processing_modes == ('interaction', 'environment')


def test_scene_save_failure_rolls_back_checkpoint_before_retry(tmp_path, monkeypatch):
    process, _ = runtime(tmp_path, ReplyModel(), WriteModel())
    process.experience('stone', '写入失败仍须保留', object_ref='user', defer_write=True)
    previous = process.activity_ledger.current_context_view('stone')
    after_write = process.activity_ledger._after_write
    def fail(subject):
        raise OSError('storage failure')
    monkeypatch.setattr(process.activity_ledger, '_after_write', fail)
    job = process.take_deferred_write()
    assert job() is False
    assert process.activity_ledger.current_context_view('stone') == previous
    assert process.pending_scene.snapshot('stone')
    monkeypatch.setattr(process.activity_ledger, '_after_write', after_write)
    assert job() is True
    assert not process.pending_scene.snapshot('stone')


def test_only_manually_selected_unknown_history_is_loaded_for_named_person(tmp_path):
    reply = RecordingReply()
    process, _ = runtime(tmp_path, reply, WriteModel())
    store = process.unknown_inputs
    store.append('selected', 'unknown', '我以前提过蓝色雨伞', 'session', 0, 1)
    store.append('unselected', 'unknown', '别人的私人安排', 'session', 1, 2)
    process.experience('stone', '蓝色雨伞呢', object_ref='user', defer_write=True)
    assert '我以前提过蓝色雨伞' not in build_user(reply.requests[-1])
    store.bind(('selected',), 'OBJ-USER')
    process.experience('stone', '蓝色雨伞呢', object_ref='user', defer_write=True)
    text = build_user(reply.requests[-1])
    assert '人工已将此句关联为user；原话：我以前提过蓝色雨伞' in text
    assert '别人的私人安排' not in text
    assert store.read('unselected')['manual_object_id'] is None
