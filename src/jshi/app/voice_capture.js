class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.phase = 0;
    this.total = 0;
    this.count = 0;
    this.frame = [];
    this.channel = 'left';
    this.port.onmessage=({data})=>{if(['left','right','mix'].includes(data.channel)) this.channel=data.channel;};
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    for (let i = 0; i < input[0].length; i++) {
      let value = 0;
      if(this.channel==='mix') {for (const channel of input) value += channel[i] / input.length;}
      else value=input[this.channel==='right' && input[1] ? 1 : 0][i];
      this.total += value;
      this.count++;
      this.phase++;
      if (this.phase >= this.ratio) {
        this.phase -= this.ratio;
        this.frame.push(this.total / this.count);
        this.total = 0;
        this.count = 0;
      }
      if (this.frame.length === 1600) {
        const pcm = new ArrayBuffer(3200);
        const view = new DataView(pcm);
        let energy = 0;
        this.frame.forEach((x, index) => {
          energy += x * x;
          view.setInt16(index * 2, Math.round(Math.max(-1, Math.min(1, x)) * 32767), true);
        });
        this.port.postMessage({pcm, level: Math.sqrt(energy / 1600)}, [pcm]);
        this.frame = [];
      }
    }
    return true;
  }
}
registerProcessor('voice-capture', VoiceCapture);
