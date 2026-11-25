// Browser player state tests without a microphone, network, or DOM dependency.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname,'../src/jshi/app/voice_ui.html'),'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id,{textContent:'',children:[],value:0,focus(){},classList:{add(){},remove(){}},appendChild(p){this.children.push(p);},prepend(p){this.children.unshift(p);},replaceChildren(){},add(){}});
  return nodes.get(id);
}
const sent = [], sources = [];
const context = vm.createContext({
  document:{getElementById:node,createElement:()=>({prepend(){},scrollIntoView(){},
    querySelector(selector){return selector==='time'?(this.timeNode??={textContent:''}):null;}})},
  navigator:{},performance,setTimeout:(fn,...args)=>{const timer=setTimeout(fn,...args);timer.unref();return timer;},
  clearTimeout,console,fetch:async()=>({ok:true,json:async()=>({people:[]})}),
  WebSocket:{OPEN:1},Uint8Array,Float32Array,DataView,Set,
  atob:s=>Buffer.from(s,'base64').toString('binary'),
  audio:{currentTime:0,createBuffer:(n,length,rate)=>({copyToChannel(){}}),
    createBufferSource:()=>{const s={connect(){},disconnect(){},start(){},stop(){},onended:null};sources.push(s);return s;},destination:{}},
  pcm:Buffer.alloc(200).toString('base64'),
  sendRaw:s=>sent.push(JSON.parse(s)),assert,
});
require('./helpers/browser_environment.cjs')(context);
vm.runInContext(script,context);
vm.runInContext(`log('旧消息');log('新消息');`,context);
assert.equal(node('log').children[0].textContent,'旧消息');
assert.equal(node('log').children.at(-1).textContent,'新消息');
vm.runInContext(`
ws={readyState:1,send:sendRaw}; outputContext=audio;ready=true;
message({type:'reply',reply_id:'one',text:'第一句。',paused:false});
message({type:'audio',reply_id:'one',segment:0,pcm,rate:100});
message({type:'segment_end',reply_id:'one',segment:0});
audio.currentTime=.4; pause();
assert.equal(queue[0].samples.length,60);
assert.equal(paused,true);
message({type:'resume',reply_id:'one'});
assert.equal(playing.samples.length,60);
`,context);
assert.equal(sent.filter(x=>x.phase==='started').length,1);
sources.at(-1).onended();
assert.equal(sent.filter(x=>x.phase==='completed').length,1);
vm.runInContext(`
message({type:'stop',reply_id:'one'});
message({type:'audio',reply_id:'one',segment:0,pcm,rate:100});
assert.equal(queue.length,0); assert.equal(playing,null);
message({type:'reply',reply_id:'two',text:'第二句。',paused:false});
message({type:'audio',reply_id:'two',segment:0,pcm,rate:100});
message({type:'audio',reply_id:'two',segment:0,pcm,rate:100});
message({type:'segment_end',reply_id:'two',segment:0});
message({type:'audio_end',reply_id:'two'});
message({type:'turn_complete'});
assert.equal($('status').textContent,'匠石正在说话');
`,context);
sources.at(-1).onended();
assert.equal(sent.filter(x=>x.reply_id==='two' && x.phase==='completed').length,0);
sources.at(-1).onended();
assert.equal(sent.filter(x=>x.reply_id==='two' && x.phase==='completed').length,1);
assert.equal(node('status').textContent,'已连接，正在听');
console.log('Voice player: pause/resume, chunk completion, stale audio passed');

const mouthEvents = [];
context.window.jshiAvatar = {mouth: value => mouthEvents.push(value)};
vm.runInContext(`
  replyId = 'speaking'; paused = false; phoneSpeaking = false;
  const shape = (reply, value) => message({type:'phone_viseme', reply_id:reply, shape:value});
  phoneOutput = false; shape('speaking', 7);
  phoneOutput = true; shape('speaking', 8);
  message({type:'phone_played',reply_id:'speaking',segment:0,phase:'started'});
  shape('retired', 9); shape('speaking', 2);
  pause(); shape('speaking', 10);
  paused = false; shape('speaking', 4);
  message({type:'phone_played',reply_id:'speaking',segment:0,phase:'completed'});
  shape('speaking', 8);
  phoneOutput = false; phoneSpeaking = true; shape('speaking', 7);
`,context);
assert.deepEqual(mouthEvents,[2,-1,4,-1]);
console.log('Screen mouth follows only the playing phone reply and closes on pause and completion');
