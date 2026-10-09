const assert=require('node:assert/strict');
const VisionChat=require('../src/jshi/app/vision_chat.js');
class Element {
  constructor(){this.children=[];this.dataset={};this.classList={add(){}};this.videoWidth=640;this.videoHeight=480;}
  appendChild(child){this.children.push(child);return child;}
  insertBefore(child,before){this.children.splice(this.children.indexOf(before),0,child);}
  async play(){}
}
const document={createElement:()=>new Element()};
const response=data=>({ok:true,json:async()=>data});
async function main(){
  let cards=[],calls=0,stops=0,release;
  const mediaDevices={getUserMedia:()=>{calls++;return new Promise(resolve=>release=resolve)}};
  let data={sample_seconds:2,server_time:Date.now()/1000,voice_active:true,
    environment:{image_available:true,frame:'first',description:'有人在门边',described_at:20},errors:[]};
  const chat=new VisionChat({document,mediaDevices,ensureVoice:async()=>true,
    insert:card=>cards.push(card),request:async()=>response(data),notice(){},state(){}});
  chat.tick=()=>{};
  const first=chat.start(),second=chat.start();
  assert.equal(first,second);await Promise.resolve();
  assert.equal(calls,1);
  release({getTracks:()=>[{stop:()=>stops++}]});assert.equal(await first,true);
  for(let n=0;n<25;n++){data.environment.frame=String(n);await chat.poll();}
  assert.equal(cards.length,1);assert.equal(cards[0].dataset.display,'compact');
  cards[0].children[0].children.find(x=>x.textContent==='隐藏').onclick();
  assert.equal(cards[0].dataset.display,'hidden');
  data.errors=[{at:1,message:'JSHI_VISION_API_KEY'}];await chat.poll();
  assert.equal(chat.card.description.textContent,'有人在门边');
  data.errors=[{at:30,message:'JSHI_VISION_API_KEY'}];await chat.poll();
  assert.match(chat.card.description.textContent,/尚未配置/);
  chat.stop();const size=chat.card.body.children.length;chat.stop();
  assert.equal(stops,1);assert.equal(chat.card.body.children.length,size);
  const canceled=chat.start();await Promise.resolve();chat.stop();
  release({getTracks:()=>[{stop:()=>stops++}]});assert.equal(await canceled,false);
  assert.equal(stops,2);assert.equal(cards.length,1);
  data.errors=[];
  const photo=new Blob(['image'],{type:'image/jpeg'});
  assert.equal(await chat.attachPhoto(photo),true);
  assert.equal(await chat.attachPhoto(photo),true);
  assert.equal(cards.length,2);assert.equal(chat.photoCard.photos.children.length,2);
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
