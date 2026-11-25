const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../src/jshi/app/voice_ui.html'),'utf8');
class Element {
  constructor(){this.children=[];this.style={};this.value='';this.classList={add(){},remove(){}};}
  appendChild(x){this.children.push(x)} prepend(x){this.children.unshift(x)}
  replaceChildren(...xs){this.children=xs} add(x){this.children.push(x)}
  focus(){} pause(){} remove(){} get options(){return this.children}
  get selectedOptions(){return this.children.filter(option=>option.value===this.value)}
}
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)};
const sockets=[];let acquisitions=0,stops=0,releaseMic;
class Socket {
  static OPEN=1;
  constructor(url){this.url=url;this.readyState=1;sockets.push(this)}send(){}close(){this.readyState=3}
}
const link={connect(){return this},disconnect(){}};
class Audio {
  constructor(){this.state='running';this.audioWorklet={addModule:async()=>{}};this.destination={};}
  async resume(){}async close(){this.state='closed'}
  createMediaStreamSource(){return link}createGain(){return {...link,gain:{}}}
}
const phone='abcdef1234567890',phones=[];
let currentUrl;
const ctx=vm.createContext({document:{getElementById:node,createElement:()=>new Element()},
  navigator:{mediaDevices:{getUserMedia:()=>{acquisitions++;return new Promise(resolve=>releaseMic=resolve)}}},
  fetch:async url=>({ok:true,json:async()=>url==='/status'?{busy:false,asr:'local',local_available:true,phones}:
    url==='/api/capture-sources'?{enabled:true,phones}:{people:[]}}),
  AudioContext:Audio,AudioWorkletNode:class{constructor(){this.port={postMessage(){}}}connect(){return link}disconnect(){}},
  WebSocket:Socket,location:{host:'localhost',pathname:'/',search:'?source='+phone,hash:''},
  history:{replaceState(_state,_title,url){currentUrl=url}},
  Option:class extends Element{constructor(label,value){super();this.textContent=label;this.value=value}},setTimeout,clearTimeout,console});
require('./helpers/browser_environment.cjs')(ctx);
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],ctx);
const call=s=>vm.runInContext(s,ctx),flush=async()=>{for(let n=0;n<12;n++)await Promise.resolve()};
async function main(){
  await flush();
  const select=node('capture-source');
  assert.equal(select.value,phone);
  assert.equal(select.selectedOptions[0].disabled,true);
  assert.equal(await call('startVoice()'),false);
  assert.match(node('status').textContent,/iPhone 未连接/);
  assert.equal(acquisitions,0);
  phones.push(phone);
  await call('refreshSources()');
  const remote=call('startVoice()');await flush();
  assert.equal(new URL(sockets[0].url).searchParams.get('source'),phone);
  sockets[0].onmessage({data:JSON.stringify({type:'ready'})});
  assert.equal(await remote,true);assert.equal(acquisitions,0);
  await call('end()');sockets.length=0;
  select.value='';await select.onchange();
  assert.equal(currentUrl,'/?source=');
  const first=call('startVoice()'),second=call('startVoice()');assert.equal(first,second);
  await flush();assert.equal(acquisitions,1);
  // Ending before the microphone prompt completes must discard its late result.
  await call('end()');releaseMic({getTracks:()=>[{stop:()=>stops++}]});
  assert.equal(await first,false);assert.equal(stops,1);assert.equal(sockets.length,0);
  const next=call('startVoice()');await flush();releaseMic({getTracks:()=>[{stop:()=>stops++}]});await flush();
  assert.equal(sockets.length,1);
  // A second start during the socket handshake reuses the pending connection.
  assert.equal(call('startVoice()'),next);
  sockets[0].onmessage({data:JSON.stringify({type:'ready'})});assert.equal(await next,true);
  assert.equal(await call('startVoice()'),true);assert.equal(acquisitions,2);
  await call('end()');assert.equal(stops,2);
  const pending=call('startVoice()');await flush();releaseMic({getTracks:()=>[{stop:()=>stops++}]});await flush();
  await call('end()');assert.equal(await pending,false);assert.equal(stops,3);
  assert.equal(node('text-dock').hidden,true);
  node('mode-text').onclick();assert.equal(node('text-dock').hidden,false);node('mode-text').onclick();assert.equal(node('text-dock').hidden,true);
  assert.equal(node('start').textContent,'开始对话');assert.equal(node('start').ariaPressed,'false');
  ctx.navigator.mediaDevices.getUserMedia=async()=>{throw Object.assign(new Error('Requested device not found'),{name:'NotFoundError'})};
  assert.equal(await call('startVoice()'),false);
  assert.match(node('status').textContent,/来源是本机.*已连接的 iPhone/);
  console.log('Voice lifecycle: deduplicated startup, late microphone cancellation and handshake cancellation passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
