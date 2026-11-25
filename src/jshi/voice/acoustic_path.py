"""Estimate early loudspeaker reflections from synchronized 16 kHz PCM."""
import numpy as np


class AcousticPath:
    def __init__(self, frame_size, delay_samples):
        self.size = frame_size
        self.mic = np.zeros(2 * frame_size, dtype=np.float32)
        self.history = np.zeros(delay_samples + len(self.mic), dtype=np.float32)
        self.taps = None
        self.until_fit = 0
        self.echo_evidence = 0

    def _fit(self):
        # Pre-emphasis reduces false delay matches between similar voiced syllables.
        if not np.any(self.mic):
            return
        h = self.history[1:] - .85 * self.history[:-1]
        voice = self.mic[1:] - .85 * self.mic[:-1]
        length = len(voice)
        energy = np.cumsum(np.r_[0., h.astype(float)**2])
        powers = np.maximum(energy[length:] - energy[:-length], 1)
        residual = voice
        offsets = []
        # Fit a small group of adjacent taps around each distinct reflection.
        for _ in range(3):
            correlation = np.correlate(h, residual, 'valid') / np.sqrt(powers)
            peak = int(np.argmax(np.abs(correlation)))
            power = float(residual @ residual)
            if power < 10000 or powers[peak] < 6400:
                break
            coherence = correlation[peak]**2 / power
            if not offsets and coherence > .12:
                self.echo_evidence = 10
            if coherence < .29:
                break
            group = range(max(0, peak-3), min(peak+5, len(powers)))
            offsets = sorted(set(offsets).union(group))
            design = np.stack([h[i:i+length] for i in offsets], axis=1)
            weights = np.linalg.lstsq(design, voice, rcond=.01)[0]
            residual = voice - design @ weights

        if offsets:
            # Keep the old path during double talk. Both halves of this observation
            # must agree with the proposed room response before replacing it.
            for error, observed in zip(np.array_split(residual, 2), np.array_split(voice, 2)):
                energy = observed @ observed
                if energy < 10000 or error @ error > .30 * energy:
                    return
            self.taps = offsets, weights

    def clean(self, mic, reference):
        self.history[:-self.size] = self.history[self.size:]
        self.history[-self.size:] = np.frombuffer(reference, '<i2')
        self.mic[:-self.size] = self.mic[self.size:]
        voice = self.mic[-self.size:]
        voice[:] = np.frombuffer(mic, '<i2')
        if self.echo_evidence:
            self.echo_evidence -= 1
        if self.until_fit:
            self.until_fit -= 1
        else:
            self._fit()
            self.until_fit = 3 if self.taps else 0
        if self.taps:
            offsets, weights = self.taps
            ref = np.stack([self.history[i+self.size:i+2*self.size] for i in offsets], axis=1)
            predicted = ref @ weights
            residual = voice - predicted
            # A changed audio route or native AEC can remove a previously known
            # echo. Do not inject a stale prediction back into the microphone.
            if (residual @ residual) < (voice @ voice):
                return residual.clip(-32768, 32767).astype('<i2').tobytes()
        # A weaker coherent reflection may still benefit from Speex's longer filter.
        return None if self.echo_evidence else mic
