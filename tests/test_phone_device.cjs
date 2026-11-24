const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const src = name => fs.readFileSync(path.join(__dirname,'../src/jshi/app',name),'utf8');
const box = vm.createContext({atob, performance, setTimeout, clearTimeout, console});
vm.runInContext(src('phone_playback.js') + '\nglobalThis.Player = PhonePlayback;', box);
vm.runInContext(src('phone_camera.js') + '\nglobalThis.Camera = PhoneCamera;', box);

async function main() {
  let coords;
  box.paintMask({fillRect(...args){coords=args;}}, {x:.125,y:.22,w:.43,h:.42}, 101,101);
  assert.deepEqual(coords,[12,22,45,43], 'mask expands to cover partial edge pixels');
  let finish, requests = 0;
  box.AbortController = AbortController; box.fetch = async()=>{ requests++;return {ok:true}; };
  const camera = Object.create(box.Camera.prototype);
  Object.assign(camera,{revision:1,sending:true,ui:()=>({value:'2'}),canvas:{toBlob(cb){finish=cb;}}});
  const encoding = camera.send(0,{minInterval:1,congested:()=>false});
  camera.pause(); finish({}); await encoding;
  assert.equal(requests,0,'old unmasked JPEG cannot leave the device after a mask edit');

  const ctx = {state:'running', currentTime:1, destination:{},
    createBuffer:(_,length,rate)=>({duration:length/rate,getChannelData:()=>new Float32Array(length)}),
    createBufferSource:()=>({connect(){},disconnect(){},start(){},stop(){}})};
  const events = [], player = new box.Player(ctx, e=>events.push(e));
  const pcm = Buffer.alloc(3200).toString('base64');
  const event = (type,props={})=>player.receive({type,reply_id:'r',segment:0,...props});
  event('reply');
  event('audio',{pcm,rate:16000}); event('audio',{pcm,rate:16000}); event('segment_end');
  assert.equal(player.nodes.length,2); assert.equal(player.nodes[1].at,1.1);
  ctx.currentTime=1.05;
  event('pause',{reason_id:'a',permit_seq:2});
  assert.equal(player.nodes.length,0); assert.equal(player.queue.length,2);
  event('resume',{reason_id:'a',permit_seq:1}); assert.equal(player.nodes.length,0);
  event('resume',{reason_id:'a',permit_seq:3}); assert.equal(player.nodes.length,2);
  event('block_next',{reason_id:'b',permit_seq:4}); assert.equal(player.nodes.length,1);
  event('clear_block',{cleared:['b'],permit_seq:5}); assert.equal(player.nodes.length,2);
  while (player.nodes.length) {const n=player.nodes[0];ctx.currentTime=n.end;n.node.onended();}
  assert.deepEqual(events.map(e=>e.phase),['started','completed']);
  event('stop'); event('audio',{pcm,rate:16000}); assert.equal(player.nodes.length,0);
  console.log('Phone media: masking, cancelled upload, audio pause/resume, control ordering and delivery acknowledgements passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
