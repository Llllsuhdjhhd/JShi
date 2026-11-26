const assert=require('node:assert/strict');
const VisionChat=require('../src/jshi/app/vision_chat.js');
class Element {
  constructor(){this.children=[];this.dataset={};this.classList={add(){}};this.videoWidth=640;this.videoHeight=480;}
  appendChild(child){this.children.push(child);return child;}
  setAttribute(name,value){this[name]=value;}
  insertBefore(child,before){this.children.splice(this.children.indexOf(before),0,child);}
  async play(){}
  showModal(){this.open=true;}
}
const nodes=new Map();const document={createElement:()=>new Element(),getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);}};
const response=data=>({ok:true,json:async()=>data});
async function main(){
  let cards=[],pinned=[],calls=0,stops=0,release;
  const mediaDevices={getUserMedia:()=>{calls++;return new Promise(resolve=>release=resolve)}};
  const now = Date.now()/1000;
  let data={sample_seconds:2,server_time:now,voice_active:true,
    environment:{image_available:true,frame:'first',description:'有人在门边',description_model:'observed-model',described_at:now+1},errors:[]};
  data.capture={id:'capture-1',kind:'desktop'};
  const chat=new VisionChat({document,mediaDevices,captureToken:()=>'desktop-1',captureId:()=>'capture-1',ensureVoice:async()=>true,
    pin:card=>pinned.push(card),insert:card=>cards.push(card),request:async()=>response(data),notice(){},state(){}});
  chat.tick=()=>{};
  const first=chat.start(),second=chat.start();
  assert.equal(first,second);await Promise.resolve();
  assert.equal(calls,1);
  release({getTracks:()=>[{stop:()=>stops++}]});assert.equal(await first,true);
  chat.viewCamera();assert.equal(chat.card.node.dataset.display,'normal');assert.equal(nodes.has('media-view'),false);assert.equal(calls,1);
  const expand=chat.card.node.children[0].children.find(x=>x.textContent==='收起');
  assert.equal(expand['aria-pressed'],'true');expand.onclick();assert.equal(chat.card.node.dataset.display,'compact');
  for(let n=0;n<25;n++){data.environment.frame=String(n);await chat.poll();}
  data.environment.description_frame='first';await chat.poll();
  assert.match(chat.card.copyLabel.textContent,/新画面待更新/);
  assert.match(chat.card.meta.textContent,/描述依据/);
  assert.match(chat.card.meta.textContent,/observed-model/);
  assert.match(chat.card.meta.textContent,/暂用上次描述/);
  data.environment.description_frame=data.environment.frame;await chat.poll();
  assert.equal(chat.card.copyLabel.textContent,'画面描述');
  assert.equal(cards.length,0);assert.equal(pinned.length,1);assert.equal(pinned[0].dataset.display,'compact');
  pinned[0].children[0].children.find(x=>x.textContent==='隐藏画面').onclick();
  assert.equal(pinned[0].dataset.display,'hidden');
  const environment=data.environment;
  data.environment=null;
  data.errors=[{at:now-3600,message:'视觉描述被输出长度限制截断'}];await chat.poll();
  assert.equal(chat.card.status.hidden,true,'a previous observation error must stay off the new card');
  data.environment=environment;
  data.errors=[{at:now-1,message:'JSHI_VISION_API_KEY'}];await chat.poll();
  assert.equal(chat.card.description.textContent,'有人在门边');
  data.errors=[{at:now+2,message:'JSHI_VISION_API_KEY'}];await chat.poll();
  assert.equal(chat.card.description.textContent,'有人在门边');assert.match(chat.card.status.textContent,/沿用已有描述/);
  data.errors=[];await chat.poll();assert.equal(chat.card.status.hidden,true);
  data.errors=[{at:now+2,message:'视觉模型未返回有效描述'}];await chat.poll();assert.equal(chat.card.description.textContent,'有人在门边');assert.match(chat.card.status.textContent,/模型未返回描述/);
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
    assert.equal(options.headers['X-Device-Stream'],'desktop-1');
    uploads.push(options.headers['X-Visual-Source']);
    if(uploads.length===1)await new Promise(resolve=>finish=resolve);
    return response(data);
  };
  const camera=chat.submit(photo,1,'browser',chat.card);
  const chosen=chat.submit(photo,2,'photo',chat.photoCard);
  await Promise.resolve();assert.deepEqual(uploads,['browser']);finish();
  await Promise.all([camera,chosen]);assert.deepEqual(uploads,['browser','photo']);

  try {
    const start=chat.start();await Promise.resolve();
    release({getTracks:()=>[{stop:()=>stops++}]});assert.equal(await start,true);
    let frameResponse={ok:false,status:409,text:async()=>'只有当前语音连接可以发送画面'};
    chat.options.request=async(url,options)=>options?.method?frameResponse:response(data);
    assert.equal(await chat.submit(photo,3,'browser',chat.card),false);
    assert.equal(chat.active,true,'a rejected frame must not stop the preview while the same capture is active');
    assert.match(chat.card.uploadStatus.textContent,/正在重试/);
    frameResponse=response({accepted:true});
    assert.equal(await chat.submit(photo,4,'browser',chat.card),true);
    assert.equal(chat.card.uploadStatus.hidden,true);

    data.capture={id:'capture-2',kind:'desktop'};
    await chat.poll();
    assert.equal(chat.active,false);
    assert.match(chat.card.description.textContent,/会话已切换/);

    data.capture={id:'capture-1',kind:'desktop'};
    const restart=chat.start();await Promise.resolve();
    release({getTracks:()=>[{stop:()=>stops++}]});assert.equal(await restart,true);
    data.voice_active=false;data.capture=null;
    await chat.poll();
    assert.equal(chat.active,false);
    assert.match(chat.card.description.textContent,/语音连接已结束/);
  } finally {
    chat.dispose();
  }
  console.log('Vision chat: one camera card, cancellation, compact display, photo grouping and upload ordering passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
