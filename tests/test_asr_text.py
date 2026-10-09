from types import SimpleNamespace

import numpy as np

from jshi.voice.asr_text import clean_asr_text
from jshi.voice.local import LocalASR
from jshi.voice.diarization import LocalDiarizer


class Recognizer:
    text = '<unk> <unk>'

    def create_stream(self):
        return SimpleNamespace(accept_waveform=lambda *a: None,
                               input_finished=lambda: None,
                               result=SimpleNamespace(text=self.text))

    def is_ready(self, stream):
        return False

    def is_endpoint(self, stream):
        return False

    def get_result(self, stream):
        return self.text

    def decode_stream(self, stream):
        pass


def test_streaming_tokens_never_become_speech_and_clear_previous_preview():
    recognizer = Recognizer()
    asr = LocalASR(recognizer)
    assert asr.feed(b'\0\0') == ()
    recognizer.text = '你好<unk>'
    assert asr.feed(b'\0\0')[0].text == '你好'
    recognizer.text = '<unk>'
    assert asr.feed(b'\0\0')[0].text == ''
    assert asr.feed(b'', final=True) == ()


def test_cleaning_preserves_real_repetitions():
    assert clean_asr_text('<|zh|>你好<|NEUTRAL|>') == '你好'
    assert clean_asr_text('HELLO HELLO HELLO') == 'HELLO HELLO HELLO'


def test_diarization_does_not_deliver_tokens_to_identity_or_cognition():
    class Spans:
        def sort_by_start_time(self):
            return [SimpleNamespace(start=0, end=1, speaker=0)]

    engine = SimpleNamespace(process=lambda audio: Spans())
    diarizer = LocalDiarizer(engine, None, Recognizer())
    assert diarizer.transcribe(np.zeros(16000), 0) == ()
