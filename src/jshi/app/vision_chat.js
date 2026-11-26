/* Media lives inside the conversation; camera samples never create a message each. */
class JShiVisionChat {
  constructor(options) {
    this.options=options;this.doc=options.document;this.stream=null;this.epoch=0;
    this.starting=null;this.sending=false;this.card=null;this.photoCard=null;
    this.timer=null;this.pollTimer=null;this.interval=2000;this.offset=0;this.uncertainty=0;
    this.latestURL='';this.urls=[];this.display='compact';
    try{this.display=localStorage.getItem('jshi.media.display')||'compact'}catch{}
  }
  element(tag,cls,text='') {const e=this.doc.createElement(tag);e.className=cls;e.textContent=text;return e;}
  makeCard(title,pinned=false) {
    const card=this.element('article','media-message');card.dataset.display=pinned?'compact':this.display;
    const header=this.element('div','media-header'),label=this.element('span','media-title',title);
    header.appendChild(label);
    const controls=pinned ? [['compact','显示画面'],['normal','放大'],['hidden','隐藏画面']] : [['compact','缩略'],['normal','展开'],['hidden','隐藏']];
    const buttons=new Map();
    const display=mode=>{
      card.dataset.display=mode;this.display=mode;
      for(const [value,button] of buttons)button.setAttribute('aria-pressed',String(value===mode));
      if(pinned)buttons.get('normal').textContent=mode==='normal'?'收起':'放大';
      try{localStorage.setItem('jshi.media.display',mode)}catch{}
    };
    card.setDisplay=display;
    for(const [mode,text] of controls) {
      const button=this.element('button','',text);button.type='button';
      button.setAttribute('aria-pressed',String(mode===card.dataset.display));buttons.set(mode,button);
      button.onclick=()=>display(pinned&&mode==='normal'&&card.dataset.display==='normal'?'compact':mode);
      header.appendChild(button);
    }
    const body=this.element('div','media-body'),video=this.element('video','media-preview');
    video.muted=true;video.playsInline=true;
    const copy=this.element('div','media-copy'),description=this.element('div','media-description','正在等待画面描述…');
    const copyLabel=pinned?this.element('div','media-copy-label','画面描述'):null;
    if(copyLabel)copy.appendChild(copyLabel);
    const meta=this.element('div','media-meta'),status=this.element('div','media-meta');status.hidden=true;copy.appendChild(description);copy.appendChild(meta);copy.appendChild(status);
    const uploadStatus=this.element('div','media-meta');uploadStatus.hidden=true;copy.appendChild(uploadStatus);
    body.appendChild(video);body.appendChild(copy);card.appendChild(header);card.appendChild(body);
    if(pinned&&this.options.pin)this.options.pin(card);else this.options.insert(card);
    return {node:card,label,body,video,description,meta,status,uploadStatus,pinned,copyLabel,openedAt:Date.now()/1000};
  }
  get active(){return !!(this.stream||this.remote);}
  deviceState(m){
    if(!this.remote||!this.card)return;
    this.card.label.textContent=m.text;
    if(!m.sending){this.preview?.close();this.card.video.srcObject=null;}
  }
  handlePreview(m){if(this.remote)this.preview?.answer(m,this.card.video);}
  start() {
    if(this.active)return Promise.resolve(true);
    if(this.starting)return this.starting;
    const token=++this.epoch;
    const run=this.openCamera(token);this.starting=run;
    run.finally(()=>{if(this.starting===run)this.starting=null});
    return run;
  }
  async openCamera(token) {
    try {
      if(!await this.options.ensureVoice()||token!==this.epoch)return false;
      this.remote=this.options.remoteSource?.()||'';
      if(!this.remote){
        const stream=await this.options.mediaDevices.getUserMedia({video:true,audio:false});
        if(token!==this.epoch){stream.getTracks().forEach(t=>t.stop());return false;}
        this.stream=stream;
      }
      this.latestURL='';this.card=this.makeCard('正在观察',true);
      const stop=this.element('button','','停止画面');stop.type='button';stop.onclick=()=>this.stop();
      this.card.node.children[0].appendChild(stop);
      this.card.video.onclick=()=>this.viewCamera();
      if(this.remote){
        this.preview=new DevicePreview(this.options.send);
        this.options.send({type:'device_camera',enabled:true});
      }else{
        this.card.video.srcObject=this.stream;
        await this.card.video.play();
      }
      if(token!==this.epoch)return false;
      await this.poll();
      if(token!==this.epoch)return false;
      if(!this.remote)this.timer=setInterval(()=>this.tick(),this.interval);
      clearInterval(this.pollTimer);
      this.pollTimer=setInterval(()=>this.poll(),3000);this.tick();this.options.state(true);
      return true;
    }catch(e){if(token===this.epoch){this.stop();this.options.notice('摄像头未能开启：'+e.message);}return false;}
  }
  stop(reason='') {
    ++this.epoch;this.starting=null;clearInterval(this.timer);clearInterval(this.pollTimer);this.timer=this.pollTimer=null;
    const wasActive=this.active;
    if(this.remote){this.options.send({type:'device_camera',enabled:false});this.preview?.close();this.remote='';}
    if(this.stream)this.stream.getTracks().forEach(t=>t.stop());this.stream=null;
    if(this.card&&wasActive){this.card.video.srcObject=null;this.card.video.hidden=true;this.card.label.textContent='画面已停止';
      this.card.uploadStatus.hidden=true;
      if(reason)this.card.description.textContent=reason;
      if(this.latestURL){const img=this.element('img','media-preview');img.src=this.latestURL;img.alt='最后一次采样画面';img.onclick=()=>this.viewCamera();this.card.body.insertBefore(img,this.card.video);}}
    const enlarged=this.doc.getElementById?.('media-video');if(enlarged){enlarged.srcObject=null;enlarged.hidden=true;}
    this.options.state(false);
  }
  tick() {
    if(!this.stream||this.sending||!this.card.video.videoWidth)return;
    const token=this.epoch,card=this.card,captured=Date.now()/1000,canvas=this.doc.createElement('canvas');
    canvas.width=Math.min(1280,this.card.video.videoWidth);canvas.height=canvas.width*this.card.video.videoHeight/this.card.video.videoWidth;
    canvas.getContext('2d').drawImage(this.card.video,0,0,canvas.width,canvas.height);
    const streamToken=this.options.captureToken();
    canvas.toBlob(blob=>{if(blob&&this.stream&&token===this.epoch)this.submit(blob,captured,'browser',card,streamToken)},'image/jpeg',.85);
  }
  async submit(blob,captured,source,card,streamToken=this.options.captureToken()) {
    const epoch=this.epoch;
    const current=()=>epoch===this.epoch&&!!streamToken&&streamToken===this.options.captureToken();
    while(this.sending){if(source==='browser')return false;await this.inFlight;}
    if(!current())return false;
    let finish;this.inFlight=new Promise(resolve=>finish=resolve);this.sending=true;
    try {
      const upload=()=>this.options.request('/api/vision/frame',{method:'POST',headers:{
        'Content-Type':blob.type,'X-Device-Stream':streamToken,'X-Captured-At':String(captured),
        'X-Clock-Offset':String(this.offset),'X-Clock-Uncertainty':String(this.uncertainty),
        'X-Visual-Source':source},body:blob});
      let r=await upload();
      if(r.status===429&&source==='photo'){
        await new Promise(resolve=>setTimeout(resolve,this.interval));
        if(!current())return false;
        r=await upload();
      }
      if(!current())return false;
      if(!r.ok){
        if(r.status===429&&source==='browser')return false;
        const detail=await r.text();
        // A rejected upload alone doesn't tell us whether the capture ended.
        if(r.status===409&&current())await this.poll(card);
        if(!current())return false;
        card.uploadStatus.hidden=false;
        card.uploadStatus.textContent=r.status===429?'图片发送过于频繁，请稍后重新选择':
          source==='photo'?'图片暂未送达，请重新选择':'画面暂未送达，正在重试';
        card.uploadStatus.title=detail;
        return false;
      }
      card.uploadStatus.hidden=true;
      card.lastCaptured=captured;card.meta.textContent=this.sampleLabel(captured);
      await this.poll(card);return true;
    }catch(e){
      if(current()){
        card.uploadStatus.hidden=false;
        card.uploadStatus.textContent=source==='photo'?'图片暂未送达，请重新选择':'画面暂未送达，正在重试';
        card.uploadStatus.title=e.message;
      }
      return false;
    }
    finally{this.sending=false;finish();}
  }
  async poll(card=this.card) {
    const token=this.options.captureToken(),epoch=this.epoch;
    const captureId=this.options.captureId?.();
    try {
      const start=Date.now()/1000,r=await this.options.request('/api/vision',{cache:'no-store'});
      if(!r.ok)return;const data=await r.json(),end=Date.now()/1000;
      if(token!==this.options.captureToken()||epoch!==this.epoch)return;
      this.interval=(data.sample_seconds||2)*1000;this.offset=data.server_time-(start+end)/2;this.uncertainty=(end-start)/2;
      if(this.active&&(!data.voice_active||(captureId&&data.capture?.id&&captureId!==data.capture.id))){
        this.stop(!data.voice_active?'语音连接已结束，画面已停止':'语音采集会话已切换，请重新开始视频');
        return;
      }
      if(!card)return;
      const s=data.environment;
      const source=s?.envelope?.source;
      if(source && !(card.pinned&&this.remote?source.startsWith('phone-'+this.remote+'-'):source===(card.pinned?'browser':'photo')))return;
      if(s){this.latestURL=s.image_available?'/api/vision/photo/'+s.frame:'';
        if(this.remote&&card.pinned&&this.latestURL)card.video.poster=this.latestURL;
        card.description.textContent=s.described_at?s.description:'正在采样，环境描述尚未完成';
        card.description.title=s.described_at?s.description:'';
        const stale=!!s.description_frame&&s.description_frame!==s.frame;
        if(card.copyLabel)card.copyLabel.textContent=stale?'画面描述 · 新画面待更新':'画面描述';
        const basis=s.described_at?'描述依据 '+new Date(s.described_at*1000).toLocaleTimeString('zh-CN',{hour12:false}):'尚无描述';
        const model=s.description_model?' · '+s.description_model:'';
        if(card.pinned)card.meta.textContent=this.sampleLabel(card.lastCaptured||s.captured)+' · '+basis+model+(stale?' · 暂用上次描述':'');else if(s.described_at)card.meta.textContent=basis+model;}
      const latestError=data.errors?.[0];
      const currentError = latestError && latestError.at >= card.openedAt + this.offset
        && (!s?.described_at || latestError.at > s.described_at);
      const error = currentError ? latestError.message : '';
      card.status.hidden=!error;card.status.textContent='';
      if(error){const reason=error.includes('API_KEY')?'视觉模型未配置':error.includes('未返回有效描述')?'模型未返回描述':'环境更新失败';
        card.status.textContent=s?.described_at?'最近更新失败，沿用已有描述（'+reason+'）':reason+'，尚无成功的环境描述';card.status.title=error;}
      if(card.pinned&&s?.described_at){card.description.title=s.description+'\n描述依据 '+new Date(s.described_at*1000).toLocaleTimeString('zh-CN',{hour12:false});}
    }catch{}
  }
  async attachPhoto(file) {
    this.stop();
    if(!await this.options.ensureVoice())return false;
    if(!file.type.startsWith('image/'))return false;
    const card=this.photoCard&&Date.now()-this.photoCard.at<30000?this.photoCard:this.makeCard('图片');
    if(!card.photos){card.video.hidden=true;card.photos=this.element('div','media-photos');card.body.appendChild(card.photos);card.body.classList.add('photo-body');this.photoCard=card;}
    card.at=Date.now();const url=URL.createObjectURL(file);this.urls.push(url);
    const img=this.element('img','');img.src=url;img.alt=file.name||'聊天图片';img.onclick=()=>this.view(url);card.photos.appendChild(img);
    card.label.textContent='图片 · '+card.photos.children.length+'张';card.description.textContent='已选图片，可以继续输入文字';
    const streamToken=this.options.captureToken(),captured=Date.now()/1000;
    await this.poll(card);const done=await this.submit(file,captured,'photo',card,streamToken);
    // A cloud result may arrive later; update this same card rather than adding messages.
    if(done)this.pollTimer=setInterval(()=>this.poll(card),3000);
    return done;
  }
  sampleLabel(at) {return '每 '+this.interval/1000+' 秒采样'+(at?' · 最近 '+new Date(at*1000).toLocaleTimeString('zh-CN',{hour12:false}):'');}
  viewCamera() {
    if(this.card)this.card.node.setDisplay(this.card.node.dataset.display==='normal'?'compact':'normal');
  }
  view(url) {if(!url)return;const video=this.doc.getElementById('media-video');video.hidden=true;video.srcObject=null;this.doc.getElementById('media-large').hidden=false;this.doc.getElementById('media-caption').textContent='';this.doc.getElementById('media-large').src=url;this.doc.getElementById('media-view').showModal();}
  dispose(){this.stop();this.urls.forEach(url=>URL.revokeObjectURL(url));this.urls=[];}
}
if(typeof module!=='undefined')module.exports=JShiVisionChat;
else {
  visualChat=new JShiVisionChat({document,mediaDevices:navigator.mediaDevices,request:(...a)=>fetch(...a),
    pin:card=>{$('vision-dock').replaceChildren(card);$('vision-dock').hidden=false},
    remoteSource:()=>selectedSource,send, captureToken:()=>captureToken,captureId:()=>captureId,ensureVoice:async()=>{inputMode('vision');return startVoice()},notice:log,
    insert:card=>{if($('empty'))$('empty').remove();$('log').appendChild(card);while($('log').children.length>100)$('log').firstChild.remove()},
    state:active=>{$('camera-toggle').textContent=active?'观察中':'开始视频';$('camera-toggle').ariaPressed=String(active)}});
  // The observation page operates the camera in the tab that owns the microphone.
  if(typeof BroadcastChannel==='function'){
    const controls=new BroadcastChannel('jshi-desktop-vision');
    controls.onmessage=async({data})=>{
      if(!ready||!captureId||data?.capture!==captureId)return;
      if(data.action==='start')await visualChat.start();
      else if(data.action==='stop')visualChat.stop();
      else if(data.action==='photo'&&data.file instanceof Blob)await visualChat.attachPhoto(data.file);
    };
    window.addEventListener('pagehide',()=>controls.close(),{once:true});
  }
  $('attach-photo').onclick=()=>$('photo-file').click();
  $('photo-file').onchange=()=>{const file=$('photo-file').files[0];if(file)visualChat.attachPhoto(file);$('photo-file').value='';};
  $('camera-toggle').onclick=()=>visualChat.active||visualChat.starting?visualChat.stop():visualChat.start();
  $('close-media').onclick=()=>$('media-view').close();
  $('media-view').onclose=()=>{$('media-video').srcObject=null;};
  $('typed').addEventListener('paste',e=>{const file=[...(e.clipboardData?.files||[])].find(f=>f.type.startsWith('image/'));if(file){e.preventDefault();visualChat.attachPhoto(file);}});
  window.refreshVisionAvailability=async()=>{
    try{const r=await fetch('/status',{cache:'no-store'});if(!r.ok)throw Error('服务暂不可用');const s=await r.json();
      $('attach-photo').hidden=$('camera-toggle').hidden=!s.vision_available;
    }catch(e){setTimeout(window.refreshVisionAvailability,3000);}
  };
  window.refreshVisionAvailability();
  window.addEventListener('pagehide',()=>visualChat.dispose());
}
