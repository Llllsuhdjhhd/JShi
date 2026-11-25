'use strict';
const ui=id=>document.getElementById(id);
const notice=text=>{ui('notice').textContent=text;};
let device=null, info=null, muted=false, reconnect, retries=0;
const preview=new DevicePreview(event=>send(device,event));
const camera=new PhoneCamera(ui,()=>device,notice,preview);

function send(s,event){
  if(s?.ws?.readyState===WebSocket.OPEN)s.ws.send(JSON.stringify({...event,stream:s.stream}));
}
async function clock(){
  const start=Date.now(), res=await fetch('/api/device',{cache:'no-store'});
  if(!res.ok)throw Error('无法读取主机状态');
  const data=await res.json(),end=Date.now();
  return {...data,offset:data.server_time-(start+end)/2000,uncertainty:(end-start)/2000,rtt:end-start};
}
function render(){
  const online=!!device?.online;
  ui('app').classList.toggle('online',online);ui('gate').hidden=online;
  ui('options').hidden=ui('toolbar').hidden=!online;
  ui('pair').hidden=!!info?.paired;ui('connect').hidden=!info?.paired;
  ui('connect').disabled=!!device;
  ui('detail').textContent=info?.paired?'启用后可在电脑的“来源”里选择这部 iPhone。':'输入主机显示的八位配对码。';
  ui('state').textContent=device?.stream?(muted?'麦克风静音':'采集中'):online?'已连接 · 待采集':device?'连接中':'未连接';
  ui('unlockAudio').hidden=!device?.stream || device.ctx.state==='running';
  ui('mute').textContent=muted?'麦克风已静音':device?.stream?'麦克风开':'麦克风待用';ui('mute').setAttribute('aria-pressed',String(!muted));
  camera.render();
}
function timing(s,data){
  Object.assign(s,{offset:data.offset,uncertainty:data.uncertainty,minInterval:data.min_interval});
  ui('network').textContent='主机往返 '+data.rtt+' ms'+
    (s.stream && s.referenceFrames ? ' · 已发送 '+(s.referenceFrames*.02).toFixed(1)+' 秒播报参考' : '');
  const select=ui('interval');
  if(s.minInterval!=null&&!Array.from(select.options).some(o=>+o.value>=s.minInterval))
    select.add(new Option(s.minInterval+' 秒',String(s.minInterval)));
  for(const o of select.options)o.disabled=+o.value<s.minInterval;
  if(+select.value<s.minInterval)select.value=Array.from(select.options).find(o=>!o.disabled).value;
}
function stopMic(s){
  s.releaseMouth?.();s.releaseMouth=null;
  s.player?.reset();s.player=null;
  s.speaker?.disconnect();s.speaker=null;
  s.worklet?.disconnect();s.input?.disconnect();s.silent?.disconnect();
  if(s.worklet)s.worklet.port.onmessage=null;
  s.mic?.getTracks().forEach(t=>t.stop());s.mic=s.worklet=s.input=s.silent=null;
}
function standby(s){
  s.stream='';s.camera='';camera.pause();stopMic(s);render();
}
function closeDevice(reason='',retry=false){
  clearTimeout(reconnect);
  const s=device;device=null;camera.close();ui('settings').hidden=true;ui('options').setAttribute('aria-expanded','false');
  if(s){clearTimeout(s.deadline);clearInterval(s.ping);stopMic(s);if(s.ws){s.ws.onclose=null;s.ws.close();}
    s.ctx?.close().catch(()=>{});}
  render();notice(reason);
  if(retry&&!document.hidden&&retries<4)reconnect=setTimeout(connect,500*2**retries++);
}
function pcm(s,token,frame){
  if(device!==s||s.stream!==token||s.ws.readyState!==WebSocket.OPEN)return;
  if(s.ws.bufferedAmount>16000){closeDevice('网络拥堵，正在重连',true);return;}
  // The host derives interruption activity from the cleaned microphone PCM.
  if(frame.reference){s.ws.send(frame.reference);s.referenceFrames++;}
  if(muted)new Uint8Array(frame.pcm).fill(0);
  s.ws.send(frame.pcm);
  if(++s.frames%250===0)send(s,{type:'audio_clock',page_lag_ms:Math.round(performance.now()-s.startAt-s.frames*20),
    context_lag_ms:Math.round(performance.now()-s.startAt-(s.ctx.currentTime-s.startAudio)*1000),buffered:s.ws.bufferedAmount});
}
async function startMic(s,token){
  stopMic(s);s.stream=token;render();
  send(s,{type:'device_status',text:'正在开启 iPhone 麦克风'});
  try{
    if(!s.ctx||s.ctx.state==='closed')s.ctx=new AudioContext({latencyHint:'interactive'});
    s.player=new PhonePlayback(s.ctx,event=>{
      if(device===s&&s.stream===token)send(s,{...event,browser_at_ms:Date.now()+(s.offset||0)*1000});
    });
    const module=s.module??=s.ctx.audioWorklet.addModule('/voice_capture.js?duplex=1');
    const mic=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:false,autoGainControl:true},video:false});
    if(s!==device||token!==s.stream){mic.getTracks().forEach(t=>t.stop());return;}
    s.mic=mic;await Promise.all([module,s.ctx.resume()]);
    if(s!==device||token!==s.stream)return;
    const track=mic.getAudioTracks()[0];track.enabled=!muted;track.onended=()=>{if(s.stream===token)closeDevice('麦克风已断开');};
    s.speaker=s.ctx.createGain();s.speaker.connect(s.ctx.destination);s.player.destination=s.speaker;
    const bus = s.speaker;
    import('/avatar/audio/playback.mjs').then(({observePlayback}) => {
      if (s.speaker !== bus || s.stream !== token) return;
      s.releaseMouth = observePlayback(bus, shape => {
        const player = s.player;
        if (s.stream === token && player?.reply && !player.paused && device === s) {
          send(s,{type:'viseme',reply_id:player.reply,shape});
        }
      }, () => send(s,{type:'device_status',text:'手机口型分析不可用'}));
    }).catch(() => send(s,{type:'device_status',text:'手机口型组件载入失败'}));
    s.input=s.ctx.createMediaStreamSource(mic);
    s.worklet=new AudioWorkletNode(s.ctx,'voice-capture',{numberOfInputs:2,
      processorOptions:{frameMs:20,playbackReference:true}});
    s.speaker.connect(s.worklet,0,1);
    s.silent=s.ctx.createGain();s.silent.gain.value=0;
    Object.assign(s,{frames:0,referenceFrames:0,startAt:performance.now(),startAudio:s.ctx.currentTime});
    send(s,{type:'started',echo_reference:true});s.player.enabled=true;s.player.pump();
    s.worklet.port.onmessage=({data})=>pcm(s,token,data);
    s.input.connect(s.worklet).connect(s.silent).connect(s.ctx.destination);
    send(s,{type:'device_status',text:muted?'iPhone 麦克风已静音 · 手机播报':'iPhone 收音 · 手机播报',muted});render();
  }catch(e){if(device===s&&s.stream===token){send(s,{type:'device_status',text:'手机麦克风未能开启：'+e.message});closeDevice('无法开启麦克风：'+e.message);}}
}
function connect(context){
  clearTimeout(reconnect);if(device||!info?.paired)return;
  if(!navigator.mediaDevices||!window.isSecureContext){notice('请使用受信任的 HTTPS 手机入口');return;}
  const s=device={stream:'',camera:'',congested:()=>!s.stream||s.ws.bufferedAmount>1280};
  try{
    // Unlock iOS audio in the initiating tap. The microphone is acquired on desktop demand.
    s.ctx=context||new AudioContext({latencyHint:'interactive'});s.ctx.resume().catch(()=>{});
    s.ctx.onstatechange=()=>{if(device===s){s.player?.pump();render();}};
    s.module=s.ctx.audioWorklet.addModule('/voice_capture.js?duplex=1');s.module.catch(()=>{});
    s.reportCamera=(text,sending)=>send(s,{type:'device_video',camera:s.camera,text,sending});
    s.ws=new WebSocket('wss://'+location.host+'/device-link?playback=1');
    s.deadline=setTimeout(()=>{if(device===s)closeDevice('连接超时，请重试',true);},8000);
    timing(s,info);notice('');render();
    s.ws.onmessage=({data})=>{
      if(device!==s)return;const m=JSON.parse(data);
      if(m.type==='available'){
        clearTimeout(s.deadline);s.online=true;retries=0;render();
        if(s.minInterval!=null)camera.open();
        s.ping=setInterval(async()=>{try{const t=await clock();if(device===s)timing(s,t);}catch{}},5000);
      }else if(m.type==='start'){startMic(s,m.stream);}
      else if(m.type==='playback'&&m.stream===s.stream){s.player?.handle(m.event);}
      else if(m.type==='standby'){standby(s);}
      else if(m.type==='camera'&&m.stream===s.stream){
        s.camera=m.camera;s.videoState=null;camera.request(!!m.camera);
      }else if(m.type==='preview_signal'&&m.camera===s.camera)preview.accept(m);
    };
    s.ws.onclose=()=>{if(device===s)closeDevice('设备连接已断开',s.online);};
  }catch(e){closeDevice('未能连接：'+e.message);}
}
ui('pair').onsubmit=async e=>{
  e.preventDefault();
  let context;
  try{
    context=new AudioContext({latencyHint:'interactive'});context.resume().catch(()=>{});
    const r=await fetch('/pair',{method:'POST',body:ui('code').value.trim()});
    if(!r.ok)throw Error(await r.text());
    info=await clock();ui('code').value='';render();
    if(!document.hidden)connect(context);
  }catch(err){notice(err.message);}
  finally{if(context&&device?.ctx!==context)context.close().catch(()=>{});}
};
ui('connect').onclick=()=>{retries=0;connect();};ui('disconnect').onclick=()=>closeDevice();
ui('unlockAudio').onclick=()=>device?.ctx.resume().catch(()=>notice('手机音频尚未启用，请再点一次'));
ui('mute').onclick=()=>{muted=!muted;device?.mic?.getAudioTracks().forEach(t=>{t.enabled=!muted;});
  send(device,{type:'device_status',text:muted?'iPhone 麦克风已静音':'iPhone 麦克风已打开',muted});render();};
ui('options').onclick=()=>{const p=ui('settings');p.hidden=!p.hidden;ui('options').setAttribute('aria-expanded',String(!p.hidden));};
window.addEventListener('pagehide',()=>closeDevice());
document.addEventListener('visibilitychange',()=>{if(document.hidden)closeDevice('返回前台后可重新启用设备');});
clock().then(v=>{info=v;render();}).catch(e=>notice(e.message));
