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
  makeCard(title) {
    const card=this.element('article','media-message');card.dataset.display=this.display;
    const header=this.element('div','media-header'),label=this.element('span','media-title',title);
    header.appendChild(label);
    for(const [mode,text] of [['compact','缩略'],['normal','展开'],['hidden','隐藏']]) {
      const button=this.element('button','',text);button.type='button';
      button.onclick=()=>{card.dataset.display=mode;this.display=mode;try{localStorage.setItem('jshi.media.display',mode)}catch{}};
      header.appendChild(button);
    }
    const body=this.element('div','media-body'),video=this.element('video','media-preview');
    video.muted=true;video.playsInline=true;
    const copy=this.element('div','media-copy'),description=this.element('div','media-description','画面独立采样，不会逐帧刷屏。');
    const meta=this.element('div','media-meta');copy.appendChild(description);copy.appendChild(meta);
    body.appendChild(video);body.appendChild(copy);card.appendChild(header);card.appendChild(body);
    this.options.insert(card);
    return {node:card,label,body,video,description,meta};
  }
  start() {
    if(this.stream)return Promise.resolve(true);
    if(this.starting)return this.starting;
    const token=++this.epoch;
    const run=this.openCamera(token);this.starting=run;
    run.finally(()=>{if(this.starting===run)this.starting=null});
    return run;
  }
  async openCamera(token) {
    try {
      if(!await this.options.ensureVoice()||token!==this.epoch)return false;
      const stream=await this.options.mediaDevices.getUserMedia({video:true,audio:false});
      if(token!==this.epoch){stream.getTracks().forEach(t=>t.stop());return false;}
      this.stream=stream;this.card=this.makeCard('视频 · 正在观察');
      const stop=this.element('button','','停止画面');stop.type='button';stop.onclick=()=>this.stop();
      this.card.node.children[0].appendChild(stop);
      this.card.video.srcObject=stream;this.card.video.onclick=()=>this.view(this.latestURL);
      await this.card.video.play();
      if(token!==this.epoch)return false;
      await this.poll();
      if(token!==this.epoch)return false;
      this.timer=setInterval(()=>this.tick(),this.interval);
      this.pollTimer=setInterval(()=>this.poll(),3000);this.tick();this.options.state(true);
      return true;
    }catch(e){if(token===this.epoch){this.stop();this.options.notice('摄像头未能开启：'+e.message);}return false;}
  }
  stop() {
    ++this.epoch;clearInterval(this.timer);clearInterval(this.pollTimer);this.timer=this.pollTimer=null;
    const wasActive=!!this.stream;
    if(this.stream)this.stream.getTracks().forEach(t=>t.stop());this.stream=null;
    if(this.card&&wasActive){this.card.video.srcObject=null;this.card.video.hidden=true;this.card.label.textContent='视频 · 已停止';
      if(this.latestURL){const img=this.element('img','media-preview');img.src=this.latestURL;img.alt='最后一次采样画面';img.onclick=()=>this.view(img.src);this.card.body.insertBefore(img,this.card.video);}}
    this.options.state(false);
  }
  tick() {
    if(!this.stream||this.sending||!this.card.video.videoWidth)return;
    const token=this.epoch,card=this.card,captured=Date.now()/1000,canvas=this.doc.createElement('canvas');
    canvas.width=Math.min(1280,this.card.video.videoWidth);canvas.height=canvas.width*this.card.video.videoHeight/this.card.video.videoWidth;
    canvas.getContext('2d').drawImage(this.card.video,0,0,canvas.width,canvas.height);
    canvas.toBlob(blob=>{if(blob&&this.stream&&token===this.epoch)this.submit(blob,captured,'browser',card)},'image/jpeg',.85);
  }
  async submit(blob,captured,source,card) {
    while(this.sending){if(source==='browser')return false;await this.inFlight;}
    let finish;this.inFlight=new Promise(resolve=>finish=resolve);this.sending=true;
    try {
      let r=await this.options.request('/api/vision/frame',{method:'POST',headers:{'Content-Type':blob.type,
        'X-Captured-At':String(captured),'X-Clock-Offset':String(this.offset),
        'X-Clock-Uncertainty':String(this.uncertainty),'X-Visual-Source':source},body:blob});
      if(r.status===429&&source==='photo'){await new Promise(resolve=>setTimeout(resolve,this.interval));r=await this.options.request('/api/vision/frame',{method:'POST',headers:{'Content-Type':blob.type,'X-Captured-At':String(captured),'X-Clock-Offset':String(this.offset),'X-Clock-Uncertainty':String(this.uncertainty),'X-Visual-Source':source},body:blob});}
      if(!r.ok){if(r.status===429){if(source==='photo')card.description.textContent='图片发送过于频繁，请稍后重新选择';return false;}throw Error(await r.text());}
      card.meta.textContent='最近画面 '+new Date(captured*1000).toLocaleTimeString('zh-CN',{hour12:false});
      await this.poll(card);return true;
    }catch(e){card.description.textContent=e.message.includes('语音')?'语音连接已结束，画面已停止':'画面暂未送达，稍后再试';if(e.message.includes('语音'))this.stop();return false;}
    finally{this.sending=false;finish();}
  }
  async poll(card=this.card) {
    try {
      const start=Date.now()/1000,r=await this.options.request('/api/vision',{cache:'no-store'});
      if(!r.ok)return;const data=await r.json(),end=Date.now()/1000;
      this.interval=(data.sample_seconds||2)*1000;this.offset=data.server_time-(start+end)/2;this.uncertainty=(end-start)/2;
      if(!data.voice_active&&this.stream){this.stop();return;}
      if(!card)return;
      const s=data.environment;
      if(s){this.latestURL=s.image_available?'/api/vision/photo/'+s.frame:'';
        card.description.textContent=s.described_at?s.description:'正在采样，环境描述尚未完成';
        card.description.title=s.described_at?s.description:'';
        if(s.described_at)card.meta.textContent='描述依据 '+new Date(s.described_at*1000).toLocaleTimeString('zh-CN',{hour12:false});}
      const latestError=data.errors?.[0];
      const error=(!s?.described_at||Number(latestError?.at||0)>s.described_at)?latestError?.message||'':'';
      if(error.includes('API_KEY'))card.description.textContent='画面采样中，环境描述服务尚未配置';
      else if(error)card.description.textContent='画面采样中，环境描述暂时不可用';
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
    await this.poll(card);const done=await this.submit(file,Date.now()/1000,'photo',card);
    // A cloud result may arrive later; update this same card rather than adding messages.
    if(done)this.pollTimer=setInterval(()=>this.poll(card),3000);
    return done;
  }
  view(url) {if(!url)return;this.doc.getElementById('media-large').src=url;this.doc.getElementById('media-view').showModal();}
  dispose(){this.stop();this.urls.forEach(url=>URL.revokeObjectURL(url));this.urls=[];}
}
if(typeof module!=='undefined')module.exports=JShiVisionChat;
else {
  visualChat=new JShiVisionChat({document,mediaDevices:navigator.mediaDevices,request:(...a)=>fetch(...a),
    ensureVoice:async()=>{inputMode('vision');return startVoice()},notice:log,
    insert:card=>{if($('empty'))$('empty').remove();$('log').prepend(card);while($('log').children.length>100)$('log').lastChild.remove()},
    state:active=>{$('camera-toggle').textContent=active?'停止画面':'摄像头'}});
  $('attach-photo').onclick=()=>$('photo-file').click();
  $('photo-file').onchange=()=>{const file=$('photo-file').files[0];if(file)visualChat.attachPhoto(file);$('photo-file').value='';};
  $('camera-toggle').onclick=()=>visualChat.stream?visualChat.stop():visualChat.start();
  $('close-media').onclick=()=>$('media-view').close();
  $('typed').addEventListener('paste',e=>{const file=[...(e.clipboardData?.files||[])].find(f=>f.type.startsWith('image/'));if(file){e.preventDefault();visualChat.attachPhoto(file);}});
  fetch('/status').then(r=>r.json()).then(s=>{$('attach-photo').hidden=$('camera-toggle').hidden=!s.vision_available});
  window.addEventListener('pagehide',()=>visualChat.dispose());
}
