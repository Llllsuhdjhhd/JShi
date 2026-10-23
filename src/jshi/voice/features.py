"""Speaker feature model adapters. Embeddings never contain ASR text or identities."""
import math


class SherpaSpeakerEmbedder:
    def __init__(self, extractor):
        self.extractor = extractor

    def embed(self, samples):
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=samples)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            return None
        values = list(map(float, self.extractor.compute(stream)))
        return values if values and all(math.isfinite(x) for x in values) else None


def speaker_features(extractor, samples):
    if extractor is None or len(samples) < 16000 * 1.5:
        return None
    adapter = extractor if hasattr(extractor, 'embed') else SherpaSpeakerEmbedder(extractor)
    values = adapter.embed(samples)
    # Apply the same validation to alternate feature backends.
    return list(map(float, values)) if values is not None and len(values) and all(math.isfinite(x) for x in values) else None
