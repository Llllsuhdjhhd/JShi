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
    result = VoiceJEV(Model(), mode='legacy').decide_batch('stone', [{'n': 1, 'text': '这是什么情况？'}], [], {})
    assert not result.judged and result.action == 'ignore' and result.items[0].to_jiangshi == 'no'
    assert result.model_raw == 'invalid response' and result.model_error


def test_tts_segments_never_start_with_punctuation():
    from jshi.voice.delivery import sentences
    joke = ('当然可以。给你讲一个：有天小明问老师，“老师，为什么我的作业总是写不完？”'
            '老师说：“因为你太慢了。”小明说：“那我快一点是不是就写完了？”老师说：“不，是你太懒了。”')
    parts = sentences(joke)
    assert ''.join(parts) == joke
    assert parts[1].endswith('写不完？”') and parts[2].startswith('老师说')
    long = '一是' + '走稳一点，' * 40 + '别乱跑消耗体力。望天树的照片我还在找，找到再给你。'
    parts = sentences(long)
    assert ''.join(parts) == long
    assert all(len(p) <= 182 and p[0] not in '，。、”' for p in parts)
