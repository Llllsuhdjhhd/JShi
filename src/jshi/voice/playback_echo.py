"""Remove rendered reply audio from a paired phone microphone."""
from array import array
from collections import deque
from time import monotonic


class PlaybackEcho:
    # Both phone worklets produce mono, little-endian 16 kHz PCM in 20 ms frames.
    tag = b'JREF'
    samples = 320
    silence = bytes(samples * 2)

    def __init__(self):
        try:
            from pyaec import Aec
        except (ImportError, OSError, AttributeError) as exc:
            raise RuntimeError('手机播报回声处理未就绪，请在主机安装当前版本的 jshi[vision]') from exc
        self.filter = Aec(self.samples, 6400, 16000, True)
        self.frames = deque(maxlen=4)
        self.last_reference = None
        self.tail = 0
        self.speech = ''
        self.loud = self.quiet = 0

    def reference(self, pcm):
        if len(pcm) != len(self.silence):
            return
        now = monotonic()
        if self.last_reference is not None and now - self.last_reference > .12:
            self.frames.clear()
        self.frames.append(pcm)
        self.last_reference = now

    def clean(self, pcm):
        if len(pcm) != len(self.silence):
            return pcm
        if self.last_reference is not None and monotonic() - self.last_reference > .12:
            self.frames.clear()
        reference = self.frames.popleft() if self.frames else self.silence
        if pcm == self.silence:
            return pcm  # Muting the phone must produce silence even during playback.
        if reference != self.silence:
            self.tail = 20  # 400 ms for the acoustic path after playback stops
        elif self.tail:
            self.tail -= 1
        else:
            return pcm
        samples = self.filter.cancel_echo(array('h', pcm), array('h', reference))
        return array('h', samples).tobytes()

    def activity(self, pcm):
        """Use the cleaned phone signal for the same short/sustained speech controls."""
        samples = array('h', pcm)
        if not samples:
            return None
        if sum(v*v for v in samples) >= (.02 * 32768)**2 * len(samples):
            self.quiet = 0
            self.loud += len(samples)
            if not self.speech or (self.speech == 'short' and self.loud >= 6400):
                self.speech = 'sustained' if self.speech else 'short'
                return {'type': 'speech_start', 'level': self.speech}
        elif self.speech:
            self.quiet += len(samples)
            if self.quiet >= 3200:
                old = self.speech
                self.speech = ''
                self.quiet = self.loud = 0
                return {'type': 'speech_end', 'level': old}
        return None
