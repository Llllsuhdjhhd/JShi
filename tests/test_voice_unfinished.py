"""Former word-ending heuristics must not delay or annotate voice input."""
import asyncio

import pytest

from jshi.app.voice import VoiceConversation
from jshi.voice.jev import VoiceJEV
from jshi.voice.volc import Transcript
from tests.test_voice import process, FakeCloud
from tests.test_voice_window import Gate


@pytest.mark.parametrize('text', ['我想', '因为', '我觉得', '比如说', '变成了那种…', '那我回来'])
def test_former_word_endings_do_not_wait_for_collection_pause(process, text):
    async def run():
        async def send(message):
            pass
        gate = Gate()
        conversation = VoiceConversation(process, 'stone', FakeCloud(), send,
            jev=VoiceJEV(gate, mode='legacy'), input_pause_s=.03, input_batch_chars=0)
        try:
            await conversation.accept(Transcript(text, 'A', 0, 1000, True))
            await asyncio.wait_for(conversation.queue.join(), 1)
            assert gate.batches[0][0]['text'] == text
            assert not hasattr(VoiceConversation, 'unfinished')
            await asyncio.wait_for(conversation.turn_queue.join(), 2)
            assert process.cognition.requests
            assert '第 1 条话还没说完' not in process.cognition.requests[-1].input_text
        finally:
            await conversation.close()
    asyncio.run(run())
