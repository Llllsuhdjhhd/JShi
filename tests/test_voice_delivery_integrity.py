from jshi.style.zone import ZoneBlock, ZoneStore
from jshi.voice.jev import VoiceJEV
from jshi.models import ModelResponse


def test_writer_cannot_promote_planned_speech_or_rewrite_player_facts(tmp_path):
    path = tmp_path / 'zone.json'
    store = ZoneStore(path)
    store.save('stone', [ZoneBlock('我准备说：“第二句尚未播放。”', origin='speech_plan'),
                         ZoneBlock('（播放器确认已播出）匠石：第一句。', origin='delivery_fact')])
    store = ZoneStore(path)
    store.apply_edit('stone', [{'op': 'mod', 'id': 'B1', 'text': '我说（已播出）：第二句尚未播放。'},
                               {'op': 'mod', 'id': 'B2', 'text': '所有句子已经播出。'}])
    assert store.get('stone') == ('我准备说：“第二句尚未播放。”', '（播放器确认已播出）匠石：第一句。')
    # Completed/obsolete records may still be removed to keep the scene bounded.
    store.apply_edit('stone', [{'op': 'del', 'id': 'B1'}])
    assert len(store.get('stone')) == 1


def test_legacy_planned_block_is_protected_on_reload(tmp_path):
    store = ZoneStore(tmp_path / 'zone.json')
    store.save('stone', ['我准备说：“还没播放。”'])
    store = ZoneStore(store.path)
    store.apply_edit('stone', [{'op': 'mod', 'id': 'B1', 'text': '我已经说完了。'}])
    assert store.get('stone') == ('我准备说：“还没播放。”',)


def test_jev_parse_fallback_preserves_raw_output_and_error():
    class Model:
        def generate(self, request):
            return ModelResponse(model='test', text='invalid response')
    result = VoiceJEV(Model()).decide_batch('stone', [{'n': 1, 'text': '这是什么情况？'}], [], {})
    assert not result.judged and result.items[0].to_jiangshi == 'maybe'
    assert result.model_raw == 'invalid response' and result.model_error
