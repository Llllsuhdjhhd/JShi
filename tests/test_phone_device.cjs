const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const src = name => fs.readFileSync(path.join(__dirname,'../src/jshi/app',name),'utf8');
const box = vm.createContext({atob, performance, setTimeout, clearTimeout, console});
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

  const state={online:true,camera:'capture-a'};
  let pendingPreview,stopped=0;
  Object.assign(camera,{session:()=>state, shape:'portrait', stream:{}, render(){},
    preview:{publish(canvas,token){pendingPreview=token;}, close(){pendingPreview=null;stopped++;}}});
  camera.request(true);assert.equal(pendingPreview,'capture-a');
  camera.request(false);assert.equal(pendingPreview,null);assert.equal(camera.sending,false);
  camera.needsConfirmation=true;camera.request(true);
  assert.equal(camera.sending,false,'a desktop request cannot resume an edited mask');
  camera.resume();assert.equal(camera.sending,true);
  camera.blocked=true;camera.request(true);assert.equal(camera.sending,false);
  assert(stopped>0);
  console.log('Phone: masked pixels, cancelled uploads, desktop camera control and local privacy override passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
