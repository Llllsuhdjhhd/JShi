// Browser player state tests without a microphone, network, or DOM dependency.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname,'../src/jshi/app/voice_ui.html'),'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id,{textContent:'',children:[],value:0,classList:{add(){},remove(){}},appendChild(){},prepend(p){this.children.unshift(p);},replaceChildren(){},add(){}});
  return nodes.get(id);
}
const sent = [], sources = [];
const context = vm.createContext({
  document:{getElementById:node,createElement:()=>({prepend(){},scrollIntoView(){}})},
  navigator:{},setTimeout,clearTimeout,console,fetch:async()=>({ok:true,json:async()=>({people:[]})}),
  WebSocket:{OPEN:1},Uint8Array,Float32Array,DataView,Set,
  atob:s=>Buffer.from(s,'base64').toString('binary'),
  audio:{currentTime:0,createBuffer:(n,length,rate)=>({copyToChannel(){}}),
    createBufferSource:()=>{const s={connect(){},disconnect(){},start(){},stop(){},onended:null};sources.push(s);return s;},destination:{}},
  pcm:Buffer.alloc(200).toString('base64'),
  sendRaw:s=>sent.push(JSON.parse(s)),assert,
});
vm.runInContext(script,context);
vm.runInContext(`log('旧消息');log('新消息');`,context);
assert.equal(node('log').children[0].textContent,'新消息');
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
