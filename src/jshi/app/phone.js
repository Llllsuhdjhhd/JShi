'use strict';
const ui = id => document.getElementById(id);
const notice = message => {ui('notice').textContent = message;};
let session = null, info = null, wantMuted = false, retries = 0, retryTimer;
const camera = new PhoneCamera(ui, () => session, notice);

async function readInfo(signal) {
  const start = Date.now();
  const res = await fetch('/api/device',{cache:'no-store',signal});
  if (!res.ok) throw new Error('无法读取主机状态');
  const result = await res.json(), end = Date.now();
  return {...result, offset:result.server_time-(start+end)/2000, uncertainty:(end-start)/2000,
    rtt:Math.max(0,end-start)};
}
function showPairing() {
  ui('pair').hidden = Boolean(info?.paired);
  ui('connect').hidden = !info?.paired;
  ui('detail').textContent = info?.paired ? '主机已配对，连接后即可说话。' : '输入匠石主机启动时显示的 8 位配对码。';
}
async function setup() {
  try {
    info = await readInfo(); showPairing();
    ui('limits').textContent = info.min_interval == null ? '主机当前仅提供语音。' :
      '主机允许的最短采样间隔为 '+info.min_interval+' 秒。';
    for (const o of ui('interval').options) o.disabled = +o.value < info.min_interval;
    if (info.min_interval != null && ![...ui('interval').options].some(o=>+o.value >= info.min_interval))
      ui('interval').add(new Option(info.min_interval+' 秒',String(info.min_interval)));
    if (+ui('interval').value < info.min_interval)
      ui('interval').value = [...ui('interval').options].find(o=>!o.disabled).value;
  } catch(e) {
    notice(e.message); ui('detail').textContent = '检查与主机的网络连接后重新载入。';
  }
}
ui('pair').onsubmit = async e => {
  e.preventDefault();
  try {
    const response = await fetch('/pair',{method:'POST',body:ui('code').value.trim()});
    if (!response.ok) throw new Error(await response.text());
    ui('code').value = ''; notice(''); await setup();
  } catch(error) {notice(error.message);}
};
function send(s, payload) {
  if (s.ws?.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify(payload));
}
function shutdown(reason='', retry=false) {
  clearTimeout(retryTimer);
  const old = session;
  session = null;
  camera.close();
  if (old) {
    old.abort.abort(); clearTimeout(old.deadline); clearInterval(old.ping);
    if (old.ws) {
      old.ws.onclose = old.ws.onmessage = old.ws.onerror = null;
      send(old,{type:'end'}); old.ws.close();
    }
    old.playback?.clear();
    if (old.worklet) {old.worklet.port.onmessage = null; old.worklet.disconnect();}
    old.input?.disconnect(); old.silent?.disconnect();
    old.mic?.getTracks().forEach(t=>{t.onended = null; t.stop();});
    if (old.ctx) {
      old.ctx.onstatechange = null;
      if (old.ctx.state !== 'closed') old.ctx.close().catch(()=>{});
    }
  }
  ui('network').textContent = '未连接'; ui('state').textContent = '连接主机';
  ui('level').value = 0; ui('mute').disabled = ui('stopReply').disabled = true;
  ui('connect').textContent = '连接匠石'; ui('detail').textContent = '点“连接匠石”开启麦克风。';
  if (reason) notice(reason);
  if (retry && retries < 3 && !document.hidden) {
    const delay = 1000 * 2 ** retries++;
    ui('detail').textContent = '连接中断，正在准备重连…';
    ui('connect').textContent = '取消重连';
    retryTimer = setTimeout(()=>{retryTimer = null; connect(true);},delay);
  } else retryTimer = null;
}
function capture(s, frame) {
  if (!s.stream || s !== session || s.ws.readyState !== WebSocket.OPEN) return;
  if (s.ws.bufferedAmount > 16000) {
    shutdown('声音上传积压，正在重新建立实时连接。',true); return;
  }
  const level = wantMuted ? 0 : frame.level;
  if (++s.frames % 5 === 0) ui('level').value = level;
  if (level >= .02) {
    s.quiet = 0; s.loud++;
    if (!s.speech) {s.speech = 'short'; send(s,{type:'speech_start',level:s.speech});}
    else if (s.speech === 'short' && s.loud >= 20) {
      s.speech = 'sustained'; send(s,{type:'speech_start',level:s.speech});
    }
  } else if (s.speech && ++s.quiet >= 10) {
    send(s,{type:'speech_end',level:s.speech}); s.speech = ''; s.loud = 0;
  }
  if (wantMuted) new Uint8Array(frame.pcm).fill(0);
  s.ws.send(frame.pcm);
  if (s.frames === 1) {s.firstAt = performance.now(); s.firstAudioTime = s.ctx.currentTime;}
  if (s.frames % 250 === 0) {
    const elapsed = performance.now()-s.firstAt;
    send(s,{type:'audio_clock',page_lag_ms:Math.round(elapsed-(s.frames-1)*20),
      context_lag_ms:Math.round(elapsed-(s.ctx.currentTime-s.firstAudioTime)*1000),buffered:s.ws.bufferedAmount});
  }
}
async function connect(automatic=false) {
  if (session) return;
  if (!automatic) {retries = 0; notice('');}
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    notice('请在 Safari 中使用受信任的 HTTPS 手机入口。'); return;
  }
  const s = {abort:new AbortController(), frames:0, loud:0, quiet:0, speech:'',
    congested:()=>!s.stream || s.ws.bufferedAmount > 1280};
  session = s;
  ui('state').textContent = '正在连接'; ui('detail').textContent = '正在准备麦克风和声音播放…';
  ui('connect').textContent = '取消连接';
  s.deadline = setTimeout(()=>{if (session === s) shutdown('连接超时，请检查主机或麦克风权限。');},45000);
  try {
    s.ctx = new AudioContext({latencyHint:'interactive'});
    // Unlock output within the tap; a permission prompt can consume user activation on iOS.
    const unlocked = s.ctx.resume();
    const micReady = navigator.mediaDevices.getUserMedia({video:false,audio:{
      channelCount:1, echoCancellation:true, noiseSuppression:false, autoGainControl:true
    }}).then(mic=>{
      if (session !== s) {mic.getTracks().forEach(t=>t.stop()); return;}
      s.mic = mic;
      for (const track of mic.getAudioTracks()) {
        track.enabled = !wantMuted;
        track.onended = ()=>{if (session === s) shutdown('麦克风已中断，点连接可恢复。');};
      }
    });
    const [, , state] = await Promise.all([unlocked, micReady, readInfo(s.abort.signal),
      s.ctx.audioWorklet.addModule('/voice_capture.js')]);
    if (session !== s) return;
    info = state;
    while (info.voice_state === 'this_device' && session === s) {
      ui('detail').textContent = '主机正在收尾上一次连接…';
      await new Promise(resolve=>setTimeout(resolve,300));
      if (session !== s) return;
      info = await readInfo(s.abort.signal);
    }
    if (session !== s) return;
    if (info.voice_state === 'in_use') throw new Error('主机正在使用另一条语音连接，请先结束该连接。');
    if (!info.paired) {shutdown(); showPairing(); return;}
    s.offset = info.offset; s.uncertainty = info.uncertainty; s.minInterval = info.min_interval;
    s.input = s.ctx.createMediaStreamSource(s.mic);
    s.worklet = new AudioWorkletNode(s.ctx,'voice-capture',{processorOptions:{frameMs:20}});
    s.silent = s.ctx.createGain(); s.silent.gain.value = 0;
    s.input.connect(s.worklet).connect(s.silent).connect(s.ctx.destination);
    s.worklet.port.onmessage = ({data})=>capture(s,data);
    s.playback = new PhonePlayback(s.ctx,payload=>send(s,payload));
    s.ctx.onstatechange = ()=>{
      if (session !== s) return;
      if (s.ctx.state === 'running') s.playback.play();
      else if (s.stream && s.ctx.state !== 'closed') shutdown('系统中断了声音，点连接可恢复。');
    };
    s.ws = new WebSocket('wss://'+location.host+'/voice');
    s.ws.onmessage = e=>{
      if (session !== s) return;
      const m = JSON.parse(e.data);
      if (m.type === 'ready') {
        clearTimeout(s.deadline); retries = 0; notice('');
        s.stream = m.stream; camera.render();
        ui('connect').textContent = '断开连接'; ui('state').textContent = '已连接匠石';
        ui('detail').textContent = '可以直接说话，回复会从手机播放。';
        ui('mute').disabled = ui('stopReply').disabled = false;
        ui('network').textContent = '往返 '+info.rtt+' ms';
        s.ping = setInterval(async()=>{
          if (s.pinging) return;
          s.pinging = true;
          try {
            const current = await readInfo(s.abort.signal);
            if (session === s) {
              s.offset = current.offset; s.uncertainty = current.uncertainty;
              ui('network').textContent = '往返 '+current.rtt+' ms';
            }
          } catch {} finally {s.pinging = false;}
        },5000);
      } else if (m.type === 'notice') {s.lastNotice = m.text; notice(m.text);}
      else s.playback.receive(m);
    };
    s.ws.onclose = ()=>{if (session === s) shutdown(s.lastNotice || '连接已关闭，请检查网络及主机语音会话。', Boolean(s.stream) || automatic);};
  } catch(e) {
    if (session === s) shutdown('连接失败：'+e.message);
  }
}
ui('connect').onclick = ()=>{
  if (session || retryTimer) {shutdown(); return;}
  connect();
};
ui('mute').onclick = ()=>{
  wantMuted = !wantMuted;
  session?.mic?.getAudioTracks().forEach(t=>{t.enabled = !wantMuted;});
  ui('mute').setAttribute('aria-pressed',String(wantMuted));
  ui('mute').textContent = wantMuted ? '打开麦克风' : '麦克风静音';
};
ui('stopReply').onclick = ()=>{
  if (session?.stream) {session.playback.clear(); send(session,{type:'stop'});}
};
window.addEventListener('pagehide',()=>shutdown());
document.addEventListener('visibilitychange',()=>{
  if (document.hidden && (session || retryTimer)) shutdown('页面已转入后台，返回后点连接即可恢复。');
});
setup();
