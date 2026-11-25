const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const src = name => fs.readFileSync(path.join(__dirname,'../src/jshi/app',name),'utf8');
const box = vm.createContext({atob, performance, setTimeout, clearTimeout, console});
vm.runInContext(src('phone_camera.js') + '\nglobalThis.Camera = PhoneCamera;', box);
vm.runInContext(src('phone_playback.js') + '\nglobalThis.Player = PhonePlayback;', box);

async function main() {
  const receipts=[],sources=[];
  const audio={currentTime:0,state:'running',destination:{},createBuffer(_channels,n,rate){
    return {duration:n/rate,getChannelData:()=>new Float32Array(n)};
  },createBufferSource(){
    const node={connect(){},disconnect(){},start(at,offset){Object.assign(this,{at,offset});},stop(){this.stopped=true;}};
    sources.push(node);return node;
  }};
  const player=new box.Player(audio,e=>receipts.push([e.phase,e.segment]));
  const event=(type,fields={})=>player.handle({type,reply_id:'reply',...fields});
  event('reply');const chunk={rate:16000,pcm:Buffer.alloc(3200).toString('base64')};
  event('audio',{...chunk,segment:0});assert.equal(sources.length,0);
  player.enabled=true;player.pump();event('audio',{...chunk,segment:1});
  assert.equal(sources[1].at,.1);assert.deepEqual(receipts,[['started',0]]);
  event('segment_end',{segment:0});event('segment_end',{segment:1});
  audio.currentTime=.03;event('pause',{permit_seq:2,reason_id:'speech'});
  assert.ok(sources.every(n=>n.stopped));event('resume',{permit_seq:1});assert.equal(sources.length,2);
  audio.currentTime=.10;event('resume',{permit_seq:3,cleared:['speech']});
  assert.equal(sources[2].offset,.03);audio.currentTime=.18;sources[2].onended();
  audio.currentTime=.30;sources[3].onended();
  assert.deepEqual(receipts,[['started',0],['completed',0],['started',1],['completed',1]]);
  event('stop');event('audio',{...chunk,segment:2});assert.equal(sources.length,4);
  let coords;
  box.paintMask({fillRect(...args){coords=args;}}, {x:.125,y:.22,w:.43,h:.42}, 101,101);
  assert.deepEqual(coords,[12,22,45,43], 'mask expands to cover partial edge pixels');
  let finish, requests = 0;
  box.AbortController = AbortController; box.fetch = async()=>{ requests++;return {ok:true}; };
  const camera = Object.create(box.Camera.prototype);
  Object.assign(camera,{revision:1,sending:true,ui:()=>({value:'2'}),canvas:{toBlob(cb){finish=cb;}}});
  const encoding = camera.send(0,{minInterval:1,congested:()=>false});
  camera.pause(); finish({}); await encoding;
  assert.equal(requests,0,'old unmasked JPEG cannot leave the device after a mask edit');

  const state={online:true,camera:'capture-a'};
  let pendingPreview,stopped=0;
  Object.assign(camera,{session:()=>state, shape:'portrait', stream:{}, render(){},
    preview:{publish(canvas,token){pendingPreview=token;}, close(){pendingPreview=null;stopped++;}}});
  camera.request(true);assert.equal(pendingPreview,'capture-a');
  camera.request(false);assert.equal(pendingPreview,null);assert.equal(camera.sending,false);
  camera.needsConfirmation=true;camera.request(true);
  assert.equal(camera.sending,false,'a desktop request cannot resume an edited mask');
  camera.resume();assert.equal(camera.sending,true);
  camera.blocked=true;camera.request(true);assert.equal(camera.sending,false);
  assert(stopped>0);
  console.log('Phone: masked pixels, cancelled uploads, desktop camera control and local privacy override passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});

// A native AEC flag is permission to use the OS filter, not proof the room is echo-free.
async function checkPhoneDuplex(){
  const sent=[],links=[];
  const track={getSettings:()=>({echoCancellation:true}),stop(){}};
  class AudioNode {
    constructor(){this.gain={};}
    connect(target,output=0,input=0){links.push([this,target,input]);return target;}
    disconnect(){}
  }
  const micSource=new AudioNode(),destination=new AudioNode();
  const ctx={state:'running',currentTime:0,destination,resume:async()=>{},createGain:()=>new AudioNode(),
    createMediaStreamSource:()=>micSource,audioWorklet:{addModule:async()=>{}}};
  const element={classList:{toggle(){}},setAttribute(){}};
  const scope=vm.createContext({performance,atob, WebSocket:{OPEN:1},document:{getElementById:()=>element,addEventListener(){}},
    window:{addEventListener(){}},fetch:async()=>({ok:true,json:async()=>({server_time:0})}),
    DevicePreview:class{},PhoneCamera:class{render(){}},navigator:{mediaDevices:{getUserMedia:async()=>({
      getTracks:()=>[track],getAudioTracks:()=>[track]})}},AudioWorkletNode:class extends AudioNode{
      constructor(ctx,name,opts){super();this.options=opts;this.port={};}
    },ctx,socket:{readyState:1,bufferedAmount:0,send:v=>sent.push(v)}});
  vm.runInContext(src('phone_playback.js')+'\n'+src('phone.js'),scope);
  await vm.runInContext("device={ctx,ws:socket};startMic(device,'test')",scope);
  const s=vm.runInContext('device',scope);
  assert.ok(sent.filter(x=>typeof x==='string').map(JSON.parse).find(e=>e.type==='started').echo_reference);
  assert.equal(s.worklet.options.numberOfInputs,2);
  assert.ok(links.some(([from,to,i])=>from===s.speaker && to===s.worklet && i===1));
  assert.ok(links.some(([from,to,i])=>from===micSource && to===s.worklet && i===0));
  assert.equal(s.player.destination,s.speaker);
  const pcm=new ArrayBuffer(640),reference=new ArrayBuffer(644);
  s.worklet.port.onmessage({data:{pcm,reference,level:.5}});
  assert.deepEqual(sent.slice(-2),[reference,pcm]);
}
checkPhoneDuplex().catch(e=>{console.error(e);process.exitCode=1;});
