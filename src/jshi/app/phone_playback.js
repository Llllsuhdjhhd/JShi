// Each reply has one Web Audio timeline. Pause preserves the unplayed samples.
class PhonePlayback {
  constructor(context, emit) {
    this.context = context;
    this.emit = emit;
    this.nodes = [];
    this.queue = [];
    this.blocks = new Set();
    this.holds = new Set();
    this.started = new Set();
    this.completed = new Set();
    this.ended = new Set();
    this.sequence = 0;
  }
  clear() {
    for (const item of this.nodes) this.cancel(item);
    this.nodes = [];
    this.queue = [];
    this.reply = null;
    this.paused = false;
    this.holds.clear();
    this.started.clear();
    this.completed.clear();
    this.ended.clear();
    this.received = null;
  }
  cancel(item) {
    clearTimeout(item.timer);
    item.node.onended = null;
    item.node.stop();
    item.node.disconnect();
  }
  mark(segment, phase) {
    const seen = phase === 'started' ? this.started : this.completed;
    if (seen.has(segment)) return;
    seen.add(segment);
    this.emit({type:'played', reply_id:this.reply, segment, phase, browser_at_ms:Date.now(),
      audio_received_at_ms:this.received, audio_wait_ms:performance.now() - this.receivedMono});
  }
  finish(segment) {
    if (this.ended.has(segment) && this.started.has(segment) &&
        ![...this.nodes, ...this.queue].some(item => item.segment === segment)) this.mark(segment, 'completed');
  }
  play() {
    if (!this.reply || this.paused || this.blocks.size || this.context.state !== 'running') return;
    // Schedule adjacent buffers on the audio clock, so JS callback delays do not leave gaps.
    while (this.queue.length && this.nodes.length < 3) {
      const item = this.queue.shift();
      const node = this.context.createBufferSource();
      node.buffer = item.buffer;
      node.connect(this.context.destination);
      const at = Math.max(this.context.currentTime, this.nodes.at(-1)?.end ?? 0);
      const live = {...item, node, at, end:at + item.buffer.duration - item.offset};
      this.nodes.push(live);
      const markStart = () => {
        if (this.context.state === 'running' && this.context.currentTime >= at) this.mark(item.segment, 'started');
        else live.timer = setTimeout(markStart, 10);
      };
      node.onended = () => {
        clearTimeout(live.timer);
        this.mark(item.segment, 'started');
        node.disconnect();
        this.nodes.splice(this.nodes.indexOf(live), 1);
        this.finish(item.segment);
        this.play();
      };
      node.start(at, item.offset);
      live.timer = setTimeout(markStart, Math.max(0, (at - this.context.currentTime) * 1000));
    }
  }
  hold(keepCurrent) {
    const now = this.context.currentTime;
    const pending = [];
    const kept = [];
    const done = [];
    for (const item of this.nodes) {
      if (keepCurrent && item.at <= now && now < item.end) {
        kept.push(item);
        continue;
      }
      this.cancel(item);
      if (item.at < now) this.mark(item.segment, 'started');
      if (item.end > now) pending.push({buffer:item.buffer, segment:item.segment,
        offset:item.offset + Math.max(0, now - item.at)});
      else done.push(item.segment);
    }
    this.nodes = kept;
    this.queue.unshift(...pending);
    for (const segment of done) this.finish(segment);
  }
  receive(m) {
    if (Number.isFinite(m.permit_seq)) {
      if (m.permit_seq < this.sequence) return;
      this.sequence = m.permit_seq;
    }
    if (m.type === 'reply') {
      this.clear(); this.reply = m.reply_id; this.paused = Boolean(m.paused);
    } else if (m.type === 'block_next') {
      this.blocks.add(m.reason_id); this.hold(true);
    } else if (m.type === 'clear_block') {
      for (const id of m.cleared || []) { this.blocks.delete(id); this.holds.delete(id); }
      this.play();
    } else if (m.type === 'stop' && (!m.reply_id || m.reply_id === this.reply)) {
      this.blocks.clear(); this.clear();
    } else if (this.reply && m.reply_id === this.reply) {
      if (m.type === 'audio') {
        if (this.received == null) { this.received = Date.now(); this.receivedMono = performance.now(); }
        const bytes = Uint8Array.from(atob(m.pcm), c => c.charCodeAt(0));
        const pcm = new DataView(bytes.buffer);
        const buffer = this.context.createBuffer(1, bytes.length / 2, m.rate);
        const samples = buffer.getChannelData(0);
        for (let i = 0; i < samples.length; i++) samples[i] = pcm.getInt16(i * 2, true) / 32768;
        this.queue.push({segment:m.segment, buffer, offset:0}); this.play();
      } else if (m.type === 'segment_end') {
        this.ended.add(m.segment); this.finish(m.segment);
      } else if (m.type === 'pause') {
        this.paused = true; this.holds.add(m.reason_id); this.hold(false);
      } else if (m.type === 'resume') {
        for (const id of [...(m.cleared || []), m.reason_id]) {this.blocks.delete(id); this.holds.delete(id);}
        this.paused = this.holds.size > 0; this.play();
      }
    }
  }
}
