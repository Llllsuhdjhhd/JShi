// The canvas is the sole image upload source. Mask coordinates are fractions of the transmitted frame.
function maskRect(a, b) {
  return {x:Math.min(a.x,b.x), y:Math.min(a.y,b.y), w:Math.abs(a.x-b.x), h:Math.abs(a.y-b.y)};
}
function paintMask(ctx, r, w, h) {
  const left = Math.floor(r.x*w), top = Math.floor(r.y*h);
  ctx.fillRect(left, top, Math.ceil((r.x+r.w)*w) - left, Math.ceil((r.y+r.h)*h) - top);
}
class PhoneCamera {
  constructor(ui, session, notice) {
    this.ui = ui; this.session = session; this.notice = notice;
    this.lens = 'environment'; this.epoch = 0; this.revision = 0;
    this.canvas = ui('preview'); this.ctx = this.canvas.getContext('2d', {alpha:false});
    this.video = ui('sensor');
    this.video.muted = true; this.video.playsInline = true;
    this.masks = [];
    ui('camera').onclick = () => this.stream || this.opening ? this.close() : this.open();
    ui('switchLens').onclick = () => {
      this.close(); this.lens = this.lens === 'user' ? 'environment' : 'user'; this.open();
    };
    ui('mask').onclick = () => {
      this.pause(); this.drawing = !this.drawing; this.drag = null; this.render();
    };
    ui('undo').onclick = () => { this.pause(); this.masks.pop(); this.saveMasks(); this.render(); };
    ui('sendFrame').onclick = () => {
      if (this.sending) this.pause();
      else if (this.stream && this.shape) {
        this.drawing = false; this.drag = null; this.sending = true; this.nextAt = 0;
      }
      this.render();
    };
    this.canvas.onpointerdown = e => {
      if (!this.drawing || this.drag) return;
      this.canvas.setPointerCapture(e.pointerId);
      this.drag = {id:e.pointerId, start:this.point(e), end:this.point(e)};
    };
    this.canvas.onpointermove = e => { if (this.drag?.id === e.pointerId) this.drag.end = this.point(e); };
    this.canvas.onpointerup = e => {
      if (this.drag?.id !== e.pointerId) return;
      const r = maskRect(this.drag.start, this.point(e)); this.drag = null;
      if (r.w > .002 && r.h > .002) {this.masks.push(r); this.saveMasks();}
      this.render();
    };
    this.canvas.onpointercancel = () => {this.drag = null;};
    for (const id of ['resolution','interval']) {
      try {
        const saved = localStorage.getItem('phone.'+id);
        if ([...ui(id).options].some(o=>o.value === saved)) ui(id).value = saved;
      } catch {}
      ui(id).onchange = () => {
        try {localStorage.setItem('phone.'+id,ui(id).value);} catch {}
        if (id === 'resolution') this.pause();
        this.render();
      };
    }
  }
  point(e) {
    const box = this.canvas.getBoundingClientRect();
    const bounded = v => Math.max(0,Math.min(1,v));
    return {x:bounded((e.clientX-box.left)/box.width), y:bounded((e.clientY-box.top)/box.height)};
  }
  saveMasks() {try {localStorage.setItem(this.shape, JSON.stringify(this.masks));} catch {}}
  restoreMasks() {
    try {
      const rows = JSON.parse(localStorage.getItem(this.shape) || '[]');
      this.masks = rows.filter(r => [r.x,r.y,r.w,r.h].every(n => Number.isFinite(n) && n >= 0 && n <= 1));
    } catch {this.masks = [];}
  }
  async open() {
    if (!this.session()?.stream || this.opening || this.stream) return;
    const epoch = ++this.epoch;
    this.opening = true; this.render();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio:false, video:{
        facingMode:{ideal:this.lens}, width:{ideal:1920}, height:{ideal:1080}, frameRate:{ideal:12,max:20}}});
      if (epoch !== this.epoch) {stream.getTracks().forEach(t=>t.stop()); return;}
      this.stream = stream;
      const track = stream.getVideoTracks()[0];
      this.actualLens = track.getSettings().facingMode || this.lens;
      track.onended = () => {this.close(); this.notice('摄像头已中断，请重新打开。');};
      // Keep the inline video laid out under the opaque preview on WebKit.
      track.onmute = () => {this.pause(); this.render();};
      this.video.srcObject = stream;
      this.render();
      this.watchdog = setTimeout(()=>{
        if (epoch === this.epoch && !this.shape) {
          this.close(); this.notice('没有收到镜头画面，请检查摄像头权限后重试。');
        }
      },10000);
      await this.video.play();
      if (epoch !== this.epoch) return;
      this.opening = false; this.render();
      this.loop = setInterval(()=>this.draw(),100);
      this.draw();
    } catch(e) {
      if (epoch === this.epoch) {this.close(); this.notice('无法打开摄像头：'+e.message);}
    }
  }
  pause() {
    this.sending = false; this.revision++;
    this.upload?.abort(); this.upload = null;
  }
  close() {
    ++this.epoch; this.pause(); clearInterval(this.loop); clearTimeout(this.watchdog);
    if (this.stream) for (const track of this.stream.getTracks()) {track.onended = track.onmute = null; track.stop();}
    this.stream = null; this.opening = false; this.shape = null;
    this.video.pause(); this.video.srcObject = null; this.lastFrameTime = null; this.drawing = false; this.drag = null;
    this.ctx.clearRect(0,0,this.canvas.width,this.canvas.height);
    this.render();
  }
  render() {
    const ui = this.ui, s = this.session();
    ui('camera').disabled = !s?.stream || s.minInterval == null;
    ui('camera').textContent = this.stream || this.opening ? '关闭摄像头' : '打开摄像头';
    ui('switchLens').disabled = !this.stream || this.opening;
    ui('frameTools').hidden = !this.stream;
    ui('stage').hidden = !this.stream; ui('camEmpty').hidden = Boolean(this.stream);
    ui('camState').textContent = this.opening ? '正在打开' : !this.stream ? '已关闭' :
      (this.actualLens === 'user' ? '前置' : '后置') + (this.sending ? ' · 传送中' : ' · 仅预览');
    ui('mask').setAttribute('aria-pressed',String(Boolean(this.drawing)));
    ui('undo').disabled = !this.masks.length;
    ui('sendFrame').disabled = !this.shape || !!this.drag;
    ui('sendFrame').textContent = this.sending ? '暂停传画面' : '开始传画面';
    ui('camHint').textContent = this.drawing ? '在画面上拖动，框住需要遮蔽的区域。' :
      this.sending ? '正在发送预览中可见的画面。' : '确认镜头和遮蔽范围后，点“开始传画面”。';
  }
  draw() {
    const v = this.video, c = this.canvas, s = this.session();
    if (!s?.stream || v.readyState < 2 || !v.videoWidth || this.stream.getVideoTracks()[0].muted) return;
    const orientation = screen.orientation?.angle ?? window.orientation ?? 0;
    const settings = this.stream.getVideoTracks()[0].getSettings();
    const shape = ['phone.mask', settings.deviceId || this.actualLens, this.actualLens, orientation, v.videoWidth, v.videoHeight].join('.');
    if (this.shape !== shape) {
      this.pause(); this.drag = null; this.shape = shape; this.restoreMasks(); this.render();
    }
    const scale = Math.min(1, Number(this.ui('resolution').value)/Math.max(v.videoWidth,v.videoHeight));
    const w = Math.max(1, Math.round(v.videoWidth*scale)), h = Math.max(1, Math.round(v.videoHeight*scale));
    if (c.width !== w || c.height !== h) {c.width = w; c.height = h;}
    const fresh = this.lastFrameTime !== v.currentTime;
    this.lastFrameTime = v.currentTime;
    const captured = Date.now()/1000;
    this.ctx.drawImage(v,0,0,w,h);
    this.ctx.fillStyle = '#000';
    for (const r of this.masks) paintMask(this.ctx,r,w,h);
    if (this.drag) paintMask(this.ctx,maskRect(this.drag.start,this.drag.end),w,h);
    if (fresh && this.sending && !this.upload && !s.congested() && performance.now() >= this.nextAt)
      this.send(captured,s);
  }
  async send(captured,s) {
    const revision = this.revision, controller = new AbortController();
    this.upload = controller;
    const start = performance.now();
    const timeout = setTimeout(()=>controller.abort(),5000);
    const interval = Math.max(s.minInterval, Number(this.ui('interval').value))*1000;
    try {
      const blob = await new Promise(resolve => this.canvas.toBlob(resolve,'image/jpeg',.78));
      if (!blob || revision !== this.revision || s.congested()) return;
      const response = await fetch('/api/device/frame',{method:'POST',body:blob,signal:controller.signal,
        headers:{'Content-Type':'image/jpeg','X-Device-Stream':s.stream,
          'X-Device-Lens':this.actualLens === 'user' ? 'user' : 'environment',
          'X-Captured-At':String(captured),'X-Clock-Offset':String(s.offset),
          'X-Clock-Uncertainty':String(s.uncertainty)}});
      if (response.status === 429 || response.status === 408) return;
      if (!response.ok) throw new Error(await response.text() || String(response.status));
    } catch(e) {
      if (revision === this.revision && !controller.signal.aborted) {
        this.pause(); this.render(); this.notice('画面已暂停：'+e.message);
      }
    } finally {
      clearTimeout(timeout);
      if (this.upload === controller) {
        this.upload = null;
        const elapsed = performance.now() - start;
        this.nextAt = performance.now() + Math.max(interval, elapsed*2);
      }
    }
  }
}
