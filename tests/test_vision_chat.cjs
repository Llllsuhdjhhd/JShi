const assert=require('node:assert/strict');
const VisionChat=require('../src/jshi/app/vision_chat.js');
class Element {
  constructor(){this.children=[];this.dataset={};this.classList={add(){}};this.videoWidth=640;this.videoHeight=480;}
  appendChild(child){this.children.push(child);return child;}
  insertBefore(child,before){this.children.splice(this.children.indexOf(before),0,child);}
  async play(){}
  showModal(){this.open=true;}
}
const nodes=new Map();const document={createElement:()=>new Element(),getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);}};
const response=data=>({ok:true,json:async()=>data});
async function main(){
  let cards=[],pinned=[],calls=0,stops=0,release;
  const mediaDevices={getUserMedia:()=>{calls++;return new Promise(resolve=>release=resolve)}};
  let data={sample_seconds:2,server_time:Date.now()/1000,voice_active:true,
    environment:{image_available:true,frame:'first',description:'有人在门边',described_at:20},errors:[]};
  const chat=new VisionChat({document,mediaDevices,ensureVoice:async()=>true,
    pin:card=>pinned.push(card),insert:card=>cards.push(card),request:async()=>response(data),notice(){},state(){}});
  chat.tick=()=>{};
  const first=chat.start(),second=chat.start();
  assert.equal(first,second);await Promise.resolve();
  assert.equal(calls,1);
  release({getTracks:()=>[{stop:()=>stops++}]});assert.equal(await first,true);
  chat.viewCamera();assert.equal(nodes.get('media-video').srcObject,chat.stream);assert.equal(nodes.get('media-view').open,true);assert.equal(calls,1);
  for(let n=0;n<25;n++){data.environment.frame=String(n);await chat.poll();}
  assert.equal(cards.length,0);assert.equal(pinned.length,1);assert.equal(pinned[0].dataset.display,'compact');
  pinned[0].children[0].children.find(x=>x.textContent==='隐藏').onclick();
  assert.equal(pinned[0].dataset.display,'hidden');
  data.errors=[{at:1,message:'JSHI_VISION_API_KEY'}];await chat.poll();
  assert.equal(chat.card.description.textContent,'有人在门边');
  data.errors=[{at:30,message:'JSHI_VISION_API_KEY'}];await chat.poll();
  assert.equal(chat.card.description.textContent,'有人在门边');assert.match(chat.card.status.textContent,/沿用已有描述/);
  data.errors=[];await chat.poll();assert.equal(chat.card.status.hidden,true);
  data.errors=[{at:30,message:'视觉模型未返回有效描述'}];await chat.poll();assert.equal(chat.card.description.textContent,'有人在门边');assert.match(chat.card.status.textContent,/模型未返回描述/);
  chat.stop();const size=chat.card.body.children.length;chat.stop();
  assert.equal(stops,1);assert.equal(chat.card.body.children.length,size);
  const canceled=chat.start();await Promise.resolve();chat.stop();
  release({getTracks:()=>[{stop:()=>stops++}]});assert.equal(await canceled,false);
  assert.equal(stops,2);assert.equal(cards.length,0);
  data.errors=[];
  const photo=new Blob(['image'],{type:'image/jpeg'});
  assert.equal(await chat.attachPhoto(photo),true);
  assert.equal(await chat.attachPhoto(photo),true);
  assert.equal(cards.length,1);assert.equal(chat.photoCard.photos.children.length,2);
  // An explicitly chosen photo waits for an in-flight camera upload.
  let finish,uploads=[];
  chat.options.request=async(url,options)=>{
    if(!options?.method)return response(data);
    uploads.push(options.headers['X-Visual-Source']);
    if(uploads.length===1)await new Promise(resolve=>finish=resolve);
    return response(data);
  };
  const camera=chat.submit(photo,1,'browser',chat.card);
  const chosen=chat.submit(photo,2,'photo',chat.photoCard);
  await Promise.resolve();assert.deepEqual(uploads,['browser']);finish();
  await Promise.all([camera,chosen]);assert.deepEqual(uploads,['browser','photo']);
  chat.dispose();
  console.log('Vision chat: one camera card, cancellation, compact display, photo grouping and upload ordering passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
