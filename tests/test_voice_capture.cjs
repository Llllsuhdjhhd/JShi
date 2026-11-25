const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const code=fs.readFileSync(require('node:path').join(__dirname,'../src/jshi/app/voice_capture.js'),'utf8');
function capture(channel) {
  const messages=[];
  const context=vm.createContext({sampleRate:48000,AudioWorkletProcessor:class {
    constructor(){this.port={postMessage:m=>messages.push(m)};}
  }, registerProcessor:(name,klass)=>{context.Processor=klass;},ArrayBuffer,DataView,Math});
  vm.runInContext(code,context);
  const processor=new context.Processor();
  if(channel) processor.port.onmessage({data:{channel}});
  // Opposite stereo channels must not cancel unless mixing was selected.
  processor.process([[new Float32Array(4800).fill(.25),new Float32Array(4800).fill(-.25)]]);
  assert.equal(messages.length,1);
  assert.equal(messages[0].pcm.byteLength,3200);
  return new DataView(messages[0].pcm).getInt16(0,true);
}
assert.equal(capture(),8192);
assert.equal(capture('left'),8192);
assert.equal(capture('right'),-8192);
assert.equal(capture('mix'),0);
console.log('Voice capture: 16 kHz PCM and stereo channel selection passed');

// The mobile entry requests 20 ms blocks even on a 44.1 kHz hardware input.
{
  const blocks=[];
  const env=vm.createContext({sampleRate:44100, AudioWorkletProcessor:class {
    constructor(){this.port={postMessage:m=>blocks.push(m)};}
  }, registerProcessor:(_,cls)=>{env.Capture=cls;}, ArrayBuffer, DataView, Math});
  vm.runInContext(code,env);
  const processor=new env.Capture({processorOptions:{frameMs:20}});
  processor.process([[new Float32Array(44100).fill(.25)]]);
  assert.equal(blocks.length,50);
  assert.ok(blocks.every(b=>b.pcm.byteLength===640 && b.level===.25));
}

// Feed the microphone and speaker on the same render clock. A reference can
// start mid-packet and cross render quanta at either iPhone hardware rate.
for(const rate of [44100,48000]){
  const blocks=[];
  const env=vm.createContext({sampleRate:rate, AudioWorkletProcessor:class {
    constructor(){this.port={postMessage:m=>blocks.push(m)};}
  }, registerProcessor:(_,cls)=>{env.Capture=cls;}, ArrayBuffer, DataView, Math});
  vm.runInContext(code,env);
  const processor=new env.Capture({processorOptions:{frameMs:20,playbackReference:true}});
  const gate=i=>i>=rate*.025 && i<rate*.065;
  for(let i=0;i<rate/10;i+=128){
    const n=Math.min(128,rate/10-i);
    const far=Float32Array.from({length:n},(_,j)=>gate(i+j)?-.5:0);
    const mic=Float32Array.from(far,x=>-.5*x);
    processor.process([[mic],[far]]);
  }
  assert.equal(blocks.length,5);
  assert.deepEqual(blocks.map(b=>!!b.reference),[false,true,true,true,false]);
  for(const {pcm,reference} of blocks.filter(b=>b.reference)){
    const far=new DataView(reference),mic=new DataView(pcm);
    assert.equal(far.byteLength,644);
    assert.equal(far.getUint32(0),0x4a524546);
    for(let i=0;i<320;i++)
      assert.ok(Math.abs(far.getInt16(4+i*2,true)+2*mic.getInt16(i*2,true))<=1);
  }
}
