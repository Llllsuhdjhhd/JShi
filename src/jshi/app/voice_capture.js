class VoiceCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.phase = 0;
    this.total = 0;
    this.count = 0;
    this.frameSize = options?.processorOptions?.frameMs === 20 ? 320 : 1600;
    this.capturePlayback = options?.processorOptions?.playbackReference === true;
    this.playbackTotal = 0;
    this.reference = null;
    this.frame = new DataView(new ArrayBuffer(this.frameSize * 2));
    this.position = 0;
    this.energy = 0;
    this.channel = 'left';
    this.port.onmessage = ({data}) => {
      if (['left','right','mix'].includes(data.channel)) this.channel = data.channel;
    };
  }
  append(sample, playbackSample) {
    const value = Math.round(Math.max(-1, Math.min(1, sample)) * 32767);
    this.frame.setInt16(this.position * 2, value, true);
    if (playbackSample) {
      // Allocate only for frames containing rendered audio. Other positions
      // stay zero, including silence before the first sample of a reply.
      if (!this.reference) {
        this.reference = new DataView(new ArrayBuffer(4 + this.frameSize * 2));
        this.reference.setUint32(0, 0x4a524546); // JREF, followed by little-endian PCM
      }
      this.reference.setInt16(4 + this.position * 2,
        Math.round(Math.max(-1, Math.min(1, playbackSample)) * 32767), true);
    }
    this.position++;
    this.energy += sample * sample;
    if (this.position === this.frameSize) {
      const pcm = this.frame.buffer;
      const frame = {pcm, level: Math.sqrt(this.energy / this.frameSize)};
      const transfers = [pcm];
      if (this.reference) {
        frame.reference = this.reference.buffer;
        transfers.push(frame.reference);
        this.reference = null;
      }
      this.port.postMessage(frame, transfers);
      this.frame = new DataView(new ArrayBuffer(this.frameSize * 2));
      this.position = 0;
      this.energy = 0;
    }
  }
  process(inputs) {
    const input = inputs[0];
    const playback = this.capturePlayback ? inputs[1]?.[0] : null;
    const length = input?.[0]?.length || playback?.length || 0;
    for (let i = 0; i < length; i++) {
      if (input?.[0] && this.channel === 'mix') {
        for (const channel of input) this.total += channel[i] / input.length;
      } else if (input?.[0]) {
        this.total += input[this.channel === 'right' && input[1] ? 1 : 0][i];
      }
      if (playback) this.playbackTotal += playback[i];
      this.count++;
      // Integer phase keeps packet boundaries exact at hardware rates such as 44.1 kHz.
      this.phase += 16000;
      if (this.phase < sampleRate) continue;
      const sample = this.total / this.count;
      const playbackSample = this.playbackTotal / this.count;
      this.playbackTotal = 0;
      this.total = 0;
      this.count = 0;
      while (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        this.append(sample, playbackSample);
      }
    }
    return true;
  }
}
registerProcessor('voice-capture', VoiceCapture);
