// Schedule adjacent TTS chunks on the microphone's audio clock. Only this device
// can acknowledge which parts of a reply actually reached the playback clock.
class PhonePlayback {
  constructor(context, acknowledge) {
    this.context=context;
    this.destination=context.destination;
    this.acknowledge=acknowledge;
    this.holds=new Set();
    this.blocks=new Set();
    this.sequence=0;
    this.chunks=[];
    this.enabled=false;
    this.reset();
  }
  unschedule(item) {
    if(!item.source)return;
    item.source.onended=null;
    item.source.stop();
    item.source.disconnect();
    item.source=null;
  }
  reset() {
    for(const item of this.chunks)this.unschedule(item);
    this.chunks=[];this.reply='';this.paused=false;
    this.started=new Set();this.ended=new Set();this.completed=new Set();
  }
  receipt(segment,phase) {
    this.acknowledge({type:'played',reply_id:this.reply,segment,phase,
      audio_wait_ms:performance.now()-this.firstAudio});
  }
  complete(segment) {
    if(this.started.has(segment)&&this.ended.has(segment)&&!this.completed.has(segment)&&
       !this.chunks.some(item=>item.segment===segment)){
      this.completed.add(segment);this.receipt(segment,'completed');
    }
  }
  pump() {
    if(!this.enabled||!this.reply||this.paused||this.blocks.size||this.context.state!=='running')return;
    let at=this.context.currentTime;
    // Keep the next chunk scheduled even if the main thread's onended event is late.
    for(const item of this.chunks.slice(0,2)){
      if(!item.source){
        const node=this.context.createBufferSource();
        node.buffer=item.buffer;node.connect(this.destination);
        item.source=node;item.at=at;
        node.onended=()=>{
          if(item.source!==node)return;
          node.disconnect();item.source=null;
          this.chunks.splice(this.chunks.indexOf(item),1);
          this.markStarted(item);this.complete(item.segment);this.pump();
        };
        node.start(at,item.offset);
      }
      if(item===this.chunks[0]&&item.at<=this.context.currentTime)this.markStarted(item);
      at=Math.max(this.context.currentTime,item.at+item.buffer.duration-item.offset);
    }
  }
  markStarted(item) {
    if(!this.started.has(item.segment)){
      this.started.add(item.segment);this.receipt(item.segment,'started');
    }
  }
  suspend(futureOnly=false) {
    const now=this.context.currentTime;
    for(const item of [...this.chunks]){
      if(!item.source || (futureOnly && item.at<=now))continue;
      if(item.at<=now)this.markStarted(item);
      item.offset+=Math.max(0,now-item.at);
      this.unschedule(item);
      if(item.offset>=item.buffer.duration){
        this.chunks.splice(this.chunks.indexOf(item),1);this.complete(item.segment);
      }
    }
  }
  handle(m) {
    if(m.type==='reply'){
      this.reset();this.reply=m.reply_id;this.paused=!!m.paused;this.firstAudio=null;return;
    }
    if(m.type!=='block_next' && m.type!=='clear_block' && m.reply_id!==this.reply &&
       !(m.type==='stop'&&!m.reply_id))return;
    if(Number.isFinite(m.permit_seq)){
      if(m.permit_seq<this.sequence)return;
      this.sequence=m.permit_seq;
    }
    switch(m.type){
      case 'audio': {
        const raw=atob(m.pcm),count=Math.floor(raw.length/2);
        if(!count)return;
        const buffer=this.context.createBuffer(1,count,m.rate),samples=buffer.getChannelData(0);
        for(let i=0;i<count;i++){
          const value=raw.charCodeAt(2*i)|(raw.charCodeAt(2*i+1)<<8);
          samples[i]=(value>=32768?value-65536:value)/32768;
        }
        if(this.firstAudio===null)this.firstAudio=performance.now();
        this.chunks.push({buffer,segment:m.segment,offset:0,source:null});this.pump();break;
      }
      case 'segment_end': this.ended.add(m.segment);this.complete(m.segment);break;
      case 'pause':
        if(m.reason_id)this.holds.add(m.reason_id);
        this.paused=true;this.suspend();break;
      case 'resume':
        for(const id of m.cleared||[]){this.holds.delete(id);this.blocks.delete(id);}
        this.holds.delete(m.reason_id);
        if(!this.holds.size){this.paused=false;this.pump();}break;
      case 'block_next': this.blocks.add(m.reason_id);this.suspend(true);break;
      case 'clear_block':
        for(const id of m.cleared||[]){this.holds.delete(id);this.blocks.delete(id);}
        this.pump();break;
      case 'stop': this.reset();this.holds.clear();this.blocks.clear();break;
    }
  }
}
