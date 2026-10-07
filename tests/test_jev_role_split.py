import asyncio
import json
from threading import Event

from jshi.app.voice import VoiceConversation
from jshi.models import ModelResponse, ResponsePlan, build_user
from jshi.voice.jev import VoiceJEV
from jshi.voice.volc import Transcript
from tests.test_voice import process, FakeCloud, Model


class IdentityGate:
    def generate(self, request):
        lines = json.loads(request.input_text)['input']['lines']
        return ModelResponse(model='identity', text=json.dumps({
            'items': [{'i': r['i'], 'person': 'unknown', 'certainty': 'unknown',
                       'why': '有效候选不足，无法合理选人'} for r in lines],
            'level': 3, 'recall_memory': False}))


def test_identity_protocol_does_not_gate_background_or_playback():
    gate = VoiceJEV(IdentityGate())
    for state in ('completed', 'playing'):
        result = gate.decide_batch('stone', [{'n': 1, 'text': '这把木镐给你'}], [], {'state': state})
        assert result.judged and result.action == 'respond'
        assert result.items[0].identity_only and result.items[0].keep
        assert result.items[0].to_jiangshi == ''
        assert result.main_prompt_hint == {'level': 3, 'needs': [], 'recall_memory': False}
    assert gate.decide_batch('stone', [{'n': 1, 'text': '匠石，别说了'}], [], {}).action == 'stop'
    assert gate.decide_batch('stone', [{'n': 1, 'text': '停一下，我补充一句'}], [], {}).action == 'stop'


def test_first_judgment_precedes_scene_and_main_receives_context(process):
    entered, release = Event(), Event()
    class SlowGate(IdentityGate):
        def generate(self, request):
            entered.set()
            assert release.wait(3)
            return super().generate(request)
    class QuietMain(Model):
        def generate(self, request):
            self.requests.append(request)
            return ModelResponse(model='quiet', response_plan=ResponsePlan(mode='ignore'))
    process.cognition = QuietMain()
    process.style_packs.set('stone', 'smith')
    async def run():
        sent = []
        async def send(message): sent.append(message)
        c = VoiceConversation(process, 'stone', FakeCloud(), send, jev=VoiceJEV(SlowGate()), input_pause_s=.01)
        try:
            c.jev_scene_store.append('old', {'text': '此前两人在交流游戏', 'at': 1})
            assert c.jev_scene_store.commit(c.jev_scene_store.snapshot(), '两人在共同游戏，主要彼此问答，匠石尚未被纳入。')
            await c.accept(Transcript('这把木镐给你', 'A', 0, 1800, True))
            while not entered.is_set(): await asyncio.sleep(.01)
            assert not c.jev_scene_store.snapshot()['events']
            release.set()
            await asyncio.wait_for(c.queue.join(), 3)
            await asyncio.wait_for(c.turn_queue.join(), 3)
            assert len(process.cognition.requests) == 1
            request = process.cognition.requests[0]
            user = build_user(request)
            assert '【JEV情境参考】' in user and '两人在共同游戏' in user
            assert '这把木镐给你' in user
            assert '指向：可能对匠石说' not in user
            event = next(r['payload'] for r in c.jev_scene_store.snapshot()['events'] if r['payload'].get('text') == '这把木镐给你')
            assert event['entry_status'] == 'completed'
            assert event['judgment']['person'] == 'unknown' and 'why' in event['judgment']
            assert 'to' not in event['judgment']
            assert 'voice' in event and 'candidates' in event and event['input_id']
            assert not any(m['type'] == 'reply' for m in sent)
        finally:
            release.set()
            await c.close()
    asyncio.run(run())


def test_second_scene_full_request_is_preserved(process):
    from jshi.voice.jev_scene import JEVSceneStore, JEVSceneWriter
    class Writer:
        def generate(self, request):
            return ModelResponse(model='scene', text=json.dumps({'scene': '【当前情境】多人游戏与媒体并存。'}))
    store = JEVSceneStore(process.repository.path.parent / 'scene.json')
    store.append('a', {'text': '给你木镐', 'entry_status': 'completed',
                       'judgment': {'person': 'P2', 'certainty': '可能', 'why': '声纹领先'},
                       'voice': {'strength': 'weak'}, 'candidates': [{'p': 'P2', 'similarity': .2}]})
    writer = JEVSceneWriter(Writer())
    writer.run('stone', store.snapshot(), '', on_request=lambda req:
               process._record_step_input(req, writer.model, subject_id='stone', activity_id='scene-example'))
    rows = process.step_inputs._read()
    call = next(r for r in rows if r.get('purpose') == 'voice_jev_scene')
    user = json.loads(call['user_text'])
    assert user['input_output'][0]['judgment']['person'] == 'P2'
    assert user['input_output'][0]['voice']['strength'] == 'weak'
    assert any(r.get('hash') == call['system_hash'] for r in rows)
