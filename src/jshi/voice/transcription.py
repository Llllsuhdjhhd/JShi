"""Audio-to-text adapters; no speaker analysis, capture or identity assignment."""
from .asr_text import clean_asr_text


class OfflineTranscriber:
    """Wrap sherpa's offline recognizer behind the common text-only interface."""
    def __init__(self, recognizer):
        self.recognizer = recognizer

    def transcribe_many(self, audios):
        output = []
        for audio in audios:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(16000, audio)
            self.recognizer.decode_stream(stream)
            output.append(clean_asr_text(stream.result.text))
        return output


def as_transcriber(engine):
    return engine if hasattr(engine, 'transcribe_many') else OfflineTranscriber(engine)

