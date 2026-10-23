import json

import pytest

from jshi.models import ModelResponse
from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter


class Writer:
    def generate(self, request):
        self.request = request
        self.data = json.loads(request.input_text)
        return ModelResponse(model='fake', text='{"scene":"【当前情境】lux发来问候，未明确接续旧话题。"}')


def test_scene_retirement_keeps_only_bounded_summaries(tmp_path):
    store = JEVSceneStore(tmp_path/'scene.json')
    for n in range(12):
        store.append(f'original-{n}', {'text': f'第{n}条完整原话', 'at': n+1},
                     codes={'P1': 'lux'}, labels={'lux': 'lux'})
        assert store.commit(store.snapshot(), f'场景{n}')
    reloaded = JEVSceneStore(store.path)
    assert len(reloaded.snapshot()['revisions']) == 8
    assert reloaded.reference(1) is None
    latest = reloaded.reference(12)
    assert latest['scene'] == '场景11'
    assert 'events' not in latest
    assert latest['codes'] == {'P1': 'lux'}
    assert len(reloaded._history_rows()) <= 6
    assert reloaded.snapshot()['scene'] == '场景11'
    assert reloaded.snapshot()['events'] == []


def test_historical_lookup_respects_time_and_manual_corrections(tmp_path):
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('a', {'text': '旧归属'}, codes={'P1': 'lux'})
    assert store.commit(store.snapshot(), '原场景')
    first = store.reference(1)
    assert store.reference(1, cutoff_ms=first['at']-1) is None
    store.correct({'text': '人工纠正归属'})
    assert store.reference(1) is None
    rows = [json.loads(line) for line in store.path.with_suffix('.history.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(r['scene'] == '原场景' for r in rows)
    with pytest.raises(ValueError, match='人工纠正'):
        JEVSceneWriter(Writer()).run('stone', store.snapshot(), '', history_reference=first)


def test_history_not_replayed_unless_explicitly_requested(tmp_path, monkeypatch):
    monkeypatch.setattr('jshi.voice.jev_scene.time', lambda: 100)
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('long-old-id', {'text': '旧机器人问题', 'at': 90000, 'track': 'long-track'},
                 codes={'P1': 'lux'}, labels={'lux': 'lux'})
    assert store.commit(store.snapshot(), '旧活动')
    old = store.reference(1)
    monkeypatch.setattr('jshi.voice.jev_scene.time', lambda: 110)
    store.append('new-id', {'text': 'HELLO', 'at': 109000, 'track': 'new-track'})
    model = Writer()
    writer = JEVSceneWriter(model)
    writer.run('stone', store.snapshot(), '')
    assert 'history_reference' not in model.data
    assert model.data['scene_at'] == -10000
    assert model.data['input_output'][0]['at'] == -1000
    assert 'long-old-id' not in model.request.input_text
    assert model.request.input_text == model.request.persona_user_text
    assert store.snapshot()['events'][0]['payload']['at'] == 109000
    writer.run('stone', store.snapshot(), '', history_reference=old)
    history = model.data['history_reference']
    assert history['scope'] == 'historical_only'
    assert history['at'] == -10000
    assert 'events' not in history
    assert history['scene'] == '旧活动'
    assert model.data['input_output'][0]['text'] == 'HELLO'
    assert 'long-track' not in model.request.input_text
    assert store.snapshot()['scene'] == '旧活动'
    assert len(store.snapshot()['events']) == 1


def test_failed_or_stale_commit_does_not_archive_generated_scene(tmp_path):
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('a', {'text': '原话'})
    old = store.snapshot()
    store.correct({'text': '人工纠正'})
    before = store.path.with_suffix('.history.jsonl').read_text(encoding='utf-8')
    assert not store.commit(old, '不应该进入历史的迟到结果')
    assert store.path.with_suffix('.history.jsonl').read_text(encoding='utf-8') == before


def test_legacy_revisions_are_archived_before_retiring(tmp_path, monkeypatch):
    monkeypatch.setattr('jshi.voice.jev_scene.time', lambda: 100)
    store = JEVSceneStore(tmp_path/'scene.json')
    store.state['revisions'] = [{'scene': '升级前片场', 'at': 99000,
                                'events': [{'id': 'old', 'seq': 1, 'payload': {'text': '升级前原话'}}]}]
    store.append('new', {'text': '新原话'})
    assert store.commit(store.snapshot(), '新片场')
    rows = [json.loads(line) for line in store.path.with_suffix('.history.jsonl').read_text(encoding='utf-8').splitlines()]
    assert rows[0]['reason'] == 'legacy_revision'
    assert rows[0]['scene'] == '升级前片场'
    assert 'events' not in rows[0]


def test_history_age_and_character_limits_apply_to_stored_file(tmp_path, monkeypatch):
    monkeypatch.setattr('jshi.voice.jev_scene.time', lambda: 100)
    monkeypatch.setenv('JSHI_JEV_HISTORY_MAX_AGE_SECONDS', '10')
    monkeypatch.setenv('JSHI_JEV_HISTORY_MAX_CHARS', '8')
    store = JEVSceneStore(tmp_path/'scene.json')
    for n in range(10):
        store.append(str(n), {'text': '字'*50, 'at': 100000})
        assert store.commit(store.snapshot(), f'活动{n}')
    path = store.path.with_suffix('.history.jsonl')
    assert sum(len(row['scene']) for row in store._history_rows()) <= 8
    assert '字' not in path.read_text(encoding='utf-8')
    assert store.reference(1) is None
    assert store.reference(10)['scene'] == '活动9'
    monkeypatch.setattr('jshi.voice.jev_scene.time', lambda: 111)
    assert store.snapshot()['history_candidates'] == []
    assert path.read_text(encoding='utf-8') == ''


def test_explicit_model_deletion_only_applies_after_successful_commit(tmp_path, monkeypatch):
    monkeypatch.setattr('jshi.voice.jev_scene.time', lambda: 100)
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('first', {'text': '旧问题'})
    assert store.commit(store.snapshot(), '旧问题待核')
    store.append('second', {'text': '当前问题'})
    assert store.commit(store.snapshot(), '当前交往')
    store.append('third', {'text': 'HELLO'})
    snap = store.snapshot()
    assert snap['history_candidates'][0]['scene'] == '旧问题待核'
    class DeleteModel(Writer):
        def generate(self, request):
            super().generate(request)
            return ModelResponse(model='fake', text=json.dumps({'scene': '当前问候', 'discard_history': ['H1']}))
    model = DeleteModel()
    scene, _ = JEVSceneWriter(model).run('stone', snap, '')
    assert store.reference(1) is not None
    assert model.data['history_candidates'][0]['h'] == 'H1'
    schema = json.loads(model.request.system_extra.split('JSON Schema：', 1)[1])
    assert schema['properties']['discard_history']['items']['enum'] == ['H1']
    assert 'history_lookup' not in model.data
    assert store.commit(snap, scene)
    reloaded = JEVSceneStore(store.path)
    assert reloaded.reference(1) is None
    assert all(row['scene'] != '旧问题待核' for row in reloaded.snapshot()['revisions'])
    assert reloaded.reference(2) is not None
    assert reloaded.snapshot()['scene'] == '当前问候'


def test_invalid_deletion_cannot_delete_an_unprovided_reference(tmp_path):
    class BadModel:
        def generate(self, request):
            return ModelResponse(model='fake', text='{"scene":"当前状态","discard_history":["H999"]}')
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('a', {'text': '原话'})
    with pytest.raises(ValueError, match='引用非法'):
        JEVSceneWriter(BadModel()).run('stone', store.snapshot(), '')
    assert len(store.snapshot()['events']) == 1


def test_stale_model_deletion_is_not_applied_after_manual_correction(tmp_path):
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('a', {'text': '旧问题'})
    assert store.commit(store.snapshot(), '旧片场')
    store.append('b', {'text': '当前问题'})
    assert store.commit(store.snapshot(), '当前片场')
    snap = store.snapshot()
    snap['discard_history'] = ['H1']
    store.correct({'text': '人工修正'})
    path = store.path.with_suffix('.history.jsonl')
    before = path.read_text(encoding='utf-8')
    assert not store.commit(snap, '迟到模型结果')
    assert path.read_text(encoding='utf-8') == before


def test_old_person_codes_cannot_be_reused_as_current_identity(tmp_path):
    store = JEVSceneStore(tmp_path/'scene.json')
    store.append('a', {'text': '旧人物'}, codes={'P1': 'lux'})
    assert store.commit(store.snapshot(), '旧人物活动')
    store.append('b', {'text': '新人物'}, codes={'P1': 'other'})
    assert store.commit(store.snapshot(), '新人物活动')
    assert store.reference(1) is None
    assert store.snapshot()['history_candidates'] == []
