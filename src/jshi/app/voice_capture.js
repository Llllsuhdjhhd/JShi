class VoiceCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.phase = 0;
    this.total = 0;
    this.count = 0;
    this.frameSize = options?.processorOptions?.frameMs === 20 ? 320 : 1600;
    this.frame = new DataView(new ArrayBuffer(this.frameSize * 2));
    this.position = 0;
    this.energy = 0;
    this.channel = 'left';
    this.port.onmessage = ({data}) => {
      if (['left','right','mix'].includes(data.channel)) this.channel = data.channel;
    };
  }
  append(sample) {
    const value = Math.round(Math.max(-1, Math.min(1, sample)) * 32767);
    this.frame.setInt16(this.position * 2, value, true);
    this.position++;
    this.energy += sample * sample;
    if (this.position === this.frameSize) {
      const pcm = this.frame.buffer;
      this.port.postMessage({pcm, level: Math.sqrt(this.energy / this.frameSize)}, [pcm]);
      this.frame = new DataView(new ArrayBuffer(this.frameSize * 2));
      this.position = 0;
      this.energy = 0;
    }
  }
  process(inputs) {
    const input = inputs[0];
    if (!input?.[0]) return true;
    for (let i = 0; i < input[0].length; i++) {
      if (this.channel === 'mix') {
        for (const channel of input) this.total += channel[i] / input.length;
      } else {
        this.total += input[this.channel === 'right' && input[1] ? 1 : 0][i];
      }
      this.count++;
      // Integer phase keeps packet boundaries exact at hardware rates such as 44.1 kHz.
      this.phase += 16000;
      if (this.phase < sampleRate) continue;
      const sample = this.total / this.count;
      this.total = 0;
      this.count = 0;
      while (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        this.append(sample);
      }
    }
    return true;
  }
}
registerProcessor('voice-capture', VoiceCapture);
