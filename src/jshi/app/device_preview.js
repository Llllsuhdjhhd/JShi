// One LAN video track. Recognition samples keep their own cadence and upload path.
class DevicePreview {
  constructor(signal) {this.signal=signal;}
  close() {
    const pc=this.pc;this.pc=null;this.id=null;
    if(pc){pc.ontrack=null;pc.close();}
    if(this.track){this.track.stop();this.track=null;}
  }
  async describe(pc,description,camera) {
    await pc.setLocalDescription(description);
    if(pc.iceGatheringState!=='complete'){
      await new Promise(resolve=>{
        const finish=()=>{clearTimeout(timer);pc.removeEventListener('icegatheringstatechange',changed);resolve();};
        const changed=()=>{if(pc.iceGatheringState==='complete')finish();};
        const timer=setTimeout(finish,1200);pc.addEventListener('icegatheringstatechange',changed);changed();
      });
    }
    if(pc===this.pc)this.signal({type:'preview_signal',camera,id:this.id,sdp:pc.localDescription.toJSON()});
  }
  async publish(canvas,camera) {
    this.close();
    if(!window.RTCPeerConnection||!canvas.captureStream)return;
    const pc=this.pc=new RTCPeerConnection({iceServers:[]});this.id=crypto.randomUUID();
    try{
      const stream=canvas.captureStream(24);
      this.track=stream.getVideoTracks()[0];pc.addTrack(this.track,stream);
      await this.describe(pc,await pc.createOffer(),camera);
    }catch{if(pc===this.pc)this.close();}
  }
  async answer(m,video) {
    if(!window.RTCPeerConnection||m.sdp?.type!=='offer')return;
    this.close();const pc=this.pc=new RTCPeerConnection({iceServers:[]});this.id=m.id;
    try{
      pc.ontrack=e=>{if(pc===this.pc){video.srcObject=e.streams[0];video.play().catch(()=>{});}};
      await pc.setRemoteDescription(m.sdp);
      await this.describe(pc,await pc.createAnswer(),m.camera);
    }catch{if(pc===this.pc)this.close();}
  }
  accept(m) {
    if(this.pc&&this.id===m.id&&m.sdp?.type==='answer')this.pc.setRemoteDescription(m.sdp).catch(()=>{});
  }
}
