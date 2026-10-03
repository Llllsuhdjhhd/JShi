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
