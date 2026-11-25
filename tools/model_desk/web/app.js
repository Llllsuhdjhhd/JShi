// idb 8.0.3 stores each completed turn and its conversation summary atomically.
const chatDatabase = idb.openDB('model-desk-chats', 1, {
  upgrade(db) {
    db.createObjectStore('threads', {keyPath:'id'});
    db.createObjectStore('turns', {autoIncrement:true}).createIndex('thread', 'thread');
  }
});
const chatArchive = {
  async list() { return (await chatDatabase).getAll('threads'); },
  async turns(id) { return (await chatDatabase).getAllFromIndex('turns','thread',id); },
  async append(thread,user,assistant) {
    const tx=(await chatDatabase).transaction(['threads','turns'],'readwrite');
    await Promise.all([
      tx.objectStore('threads').put(thread),
      tx.objectStore('turns').add({thread:thread.id,user,assistant}),
      tx.done
    ]);
  }
};
const $ = s => document.querySelector(s);
const scenes = {
  image: ['图片生成','输入描述，生成图片','IMAGE','<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m4 17 6-6 4 4 3-3 4 4"/>'],
  chat: ['多模态聊天','发送文字或图片，进行多轮对话','LLM','<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8m-8 3h5"/>'],
  asr: ['语音转写','上传音频，提取文字内容','ASR','<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 11a7 7 0 0 0 14 0m-7 7v4m-3 0h6"/>'],
  tts: ['语音合成','把输入的文字转换为语音','TTS','<path d="M3 10v4m4-8v12m5-16v20m5-16v12m4-8v4"/>']
};
const icon = k => '<span class="icon '+k+'"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'+scenes[k][3]+'</svg></span>';
const escapeText = s => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let models = JSON.parse(localStorage.getItem('model-desk.models') || '[]');
let page = 'home', editing = null, activeId = '', renderVersion = 0, audioUrl;
const threads = new Map(), selectedThreads = new Map(), pendingThreads = new Set();
let archiveAvailable = true;
function currentThread(modelId){
  const selected=selectedThreads.get(modelId);
  if(selected)return selected;
  const recent=[...threads.values()].filter(t=>t.modelId===modelId).sort((a,b)=>b.updated-a.updated)[0];
  const id=recent?.id || crypto.randomUUID();
  selectedThreads.set(modelId,id);
  return id;
}
async function threadMessages(id){
  return (await chatArchive.turns(id)).flatMap(turn=>[turn.user,turn.assistant]);
}
function refreshThreadPicker(){
  const picker=$('#thread-picker');
  if(!picker)return;
  const selected=currentThread(activeId);
  const history=[...threads.values()].filter(t=>t.modelId===activeId).sort((a,b)=>b.updated-a.updated);
  picker.replaceChildren();
  if(!threads.has(selected))picker.add(new Option('新对话',selected));
  for(const t of history)picker.add(new Option(t.title+' · '+new Date(t.updated).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}),t.id));
  picker.value=selected;
}
async function openChatHistory(){
  const id=currentThread(activeId), version=renderVersion;
  refreshThreadPicker();
  $('#thread-picker').onchange=e=>{selectedThreads.set(activeId,e.target.value);render()};
  $('#new-thread').onclick=()=>{selectedThreads.set(activeId,crypto.randomUUID());render()};
  $('#task .primary').disabled=pendingThreads.has(id) || !archiveAvailable;
  if(!archiveAvailable){$('#status').textContent='当前浏览器无法打开本地聊天记录库。';return}
  try{
    const messages=await threadMessages(id);
    if(version===renderVersion)historyView(messages);
  }catch{if(version===renderVersion)$('#status').textContent='无法读取本地聊天记录。'}
}

const save = () => localStorage.setItem('model-desk.models',JSON.stringify(models));
// Older configurations used only the local service and had no project assignment.
models = models.map(model => ({project: '', deployment: 'local', ...model}));
let scopeFilter = '*', locationFilter = '*';
const projects = () => [...new Set(['匠石项目', 'web open', ...models.map(m => m.project).filter(Boolean)])];
const locationName = m => m.deployment === 'remote' ? '远程服务器' : '本机';
const scopeName = m => m.project || '各项目可用';
const modelKey = m => m.model.trim();
const baseSettings = new Map(JSON.parse(localStorage.getItem('model-desk.bases') || '[]'));
const paramNames=['max_tokens','historyTurns','inputLimit','thinking','temperature','top_p','systemPrompt'];
function resolvedParams(model) {
  const params={...baseSettings.get(modelKey(model)), ...model.generation};
  delete params.inputLimit;
  delete params.thinking;
  return params;
}
function formModel(){
  const f=$('#config');
  return {...models.find(m=>m.id===editing),model:f.model.value};
}
function loadParamFields(){
  const f=$('#config'), m=formModel(), base=baseSettings.get(modelKey(m)) || {};
  const common=f.paramLayer.value==='base';
  const values=common?base:(m.generation || {});
  for(const name of paramNames){
    const field=f.elements[name];
    field.value=values[name] ?? '';
    const hint=common?'服务默认':('沿用基础设置'+(base[name]!==undefined?' · '+base[name]:''));
    if(name==='thinking')field.options[0].textContent=hint;
    else field.placeholder=hint;
  }
  $('#layer-help').textContent=common?'同一模型的所有接入配置共用这份参数。':'只填写这项接入需要不同的参数，其余随基础设置更新。';
}
function chatInput(model) {
  return model?.chatInput || 'unknown';
}
function capabilityLabel(model) {
  if (model.kind !== 'chat') return scenes[model.kind][2];
  return {vision:'多模态聊天（文字和图片）',text:'文字聊天',unknown:'聊天（输入能力待确认）'}[chatInput(model)];
}
function available() {
  return models.filter(m => (scopeFilter === '*' || !m.project || m.project === scopeFilter)
    && (locationFilter === '*' || m.deployment === locationFilter));
}
function filters() {
  const chips = [['*','全部'],['','各项目可用'], ...projects().map(p => [p,p])];
  return '<div class="filters"><div class="chips" aria-label="适用范围">' + chips.map(([value,label]) =>
    '<button data-scope="' + escapeText(value) + '" aria-pressed="' + (value === scopeFilter) + '">' + escapeText(label) + '</button>').join('') +
    '</div><select aria-label="筛选部署位置" id="location-filter">' + [['*','全部部署位置'],['local','仅本机'],['remote','仅远程服务器']].map(([v,l]) =>
      '<option value="'+v+'" '+(v===locationFilter?'selected':'')+'>'+l+'</option>').join('')+'</select></div>';
}
function list() {
  const visible = available().sort((a,b) => Number(Boolean(b.project)) - Number(Boolean(a.project)));
  if (!visible.length) return '<div class="panel empty"><span class="icon">▤</span><div><b>'+(models.length?'当前筛选下暂无模型':'接入你的第一个模型')+'</b><p class="muted">接入时可指定适用范围和部署位置。</p></div><button data-add>接入模型</button></div>';
  return '<div class="service-grid">'+visible.map(m=>{
    const source=Object.keys(m.generation || {}).length?'含特殊参数':'沿用基础参数';
    return '<div class="service-card"><div class="line">'+icon(m.kind)+'<span class="badge">'+escapeText(scopeName(m))+'</span></div>'+
      '<h3>'+escapeText(m.name||m.model)+'</h3><p class="muted">'+capabilityLabel(m)+' · '+locationName(m)+'</p>'+
      '<code>'+escapeText(m.endpoint)+'</code>'+
      (m.kind==='chat'?'<small>'+source+'</small>':'')+
      '<footer>'+control(m)+'<button data-edit="'+m.id+'">编辑</button><button data-run="'+m.id+'">试用</button></footer></div>';
  }).join('')+'</div>';
}
function control(m){
  return '<button data-check="'+m.id+'">检查连接</button>';
}
function pageHeading(title,description='',waveform=false){
  const subtitle=description?'<p>'+escapeText(description)+'</p>':'';
  return '<div class="page-heading hero"><h1>'+escapeText(title)+'</h1>'+subtitle+
    (waveform?'<canvas aria-hidden="true"></canvas>':'')+'</div>';
}
let launchConfig={};
function appSettings(){
  const subjects=Object.entries(launchConfig.subjects || {}).map(([id,name])=>'<option value="'+escapeText(id)+'">'+escapeText(name)+' · '+escapeText(id)+'</option>').join('');
  const option=(value,label)=>'<option value="'+escapeText(value)+'">'+escapeText(label)+'</option>';
  const chatModels=Object.entries(launchConfig.models || {}).flatMap(([service,models])=>models.map(model=>option(service+'|'+model,model))).join('');
  const visionModels=Object.entries(launchConfig.visionModels || {}).flatMap(([service,models])=>
    models.map(model=>option(service+'|'+model,model))).join('');
  const currentVision=launchConfig.visionCurrent;
  const voiceLabels={online:'火山在线',local:'本机离线',qwen:'本机 Qwen ASR',bailian:'阿里云百炼',deepgram:'Deepgram'};
  const voiceOptions=(names,defaultValue)=>option('project','沿用项目设置 · '+(voiceLabels[defaultValue] || defaultValue || '默认'))+names.map(n=>option(n,voiceLabels[n])).join('');
  if(launchConfig.error)return pageHeading('应用设置')+'<p role="alert">'+escapeText(launchConfig.error)+'</p>';
  return pageHeading('应用设置')+`<div class="application-grid">
    <form class="panel launcher" data-app="jshi"><h2>匠石项目</h2><p>启动匠石进程，在独立网页打开当前主体。</p>
      <details open><summary>启动参数</summary><div class="launch-fields">
      <label>对话模型<select name="chat">${option('','沿用匠石 · '+(launchConfig.current || '项目默认'))}${chatModels}</select></label>
      <label>图像识别模型<select name="vision" aria-label="图像识别模型">${option('','沿用匠石 · '+(currentVision || '项目默认'))}${visionModels}</select></label>
      <label class="launch-key" hidden>OpenAI API Key<input name="apiKey" type="password" autocomplete="off" placeholder="已配置可留空"></label>
      <label>主体<select name="subject">${subjects}</select></label>
      <label>语音识别<select name="asr">${voiceOptions(['online','local','qwen','bailian','deepgram'],launchConfig.asr)}</select></label>
      <label>语音播报<select name="tts">${voiceOptions(['online','local'],launchConfig.tts)}</select></label>
      <label>本地声纹库<select name="speaker">${Object.entries(launchConfig.banks || {}).map(([name,count])=>option(name,name.toUpperCase()+' · '+count+' 条记录')).join('')}</select></label>
      <label>初始采集来源<select name="capture"><option value="desktop">电脑</option><option value="phone">手机 · 麦克风和摄像头</option></select></label>
      <label class="phone-network" hidden>Windows 局域网地址<select name="phoneIp">${Object.entries(launchConfig.phoneAddresses || {}).map(([ip,label])=>option(ip,label+' · '+ip)).join('') || option('','未检测到网络地址')}</select></label>
      <label>网页入口<select name="mode"><option value="vision">语音＋图像</option><option value="voice">语音</option></select></label>
      <label>记忆模块<select name="memory">${option('','项目默认 · '+(launchConfig.defaultMemory || '默认'))}${(launchConfig.memory || []).map(m=>option(m,m)).join('')}</select></label></div>
      <small class="phone-help" hidden>手机和电脑 连接同一局域网。用配对码连接后，在交谈页开始采集。</small>
      <small>使用已有存档 · ${escapeText(launchConfig.directory || '未找到目录')}</small>
      <small>火山识别可配合本地声纹认人。主体、入口和记忆模块在新进程启动时生效。</small></details>
      <button class="primary">启动并打开匠石</button><p role="status" class="launch-status"></p></form>
    <form class="panel launcher" data-app="webui"><h2>Open WebUI</h2><p>启动 WebUI 并打开聊天页，自动复用已运行的服务。</p>

      <button class="primary">启动并打开 Open WebUI</button><p role="status" class="launch-status"></p></form></div>`;
}
function bindLaunchers(){
  for(const form of document.querySelectorAll('.launcher')){
    const key='model-desk.launch.'+form.dataset.app;
    const saved=JSON.parse(localStorage.getItem(key) || '{}');
    if(saved.voice){saved.asr ??= saved.voice;saved.tts ??= saved.voice;}
    for(const select of form.querySelectorAll('select')){
      if([...select.options].some(o=>o.value===saved[select.name]))select.value=saved[select.name];
    }
    const sync=()=>{
      const field=form.querySelector('.launch-key');
      if(form.elements.capture){
        const phone=form.elements.capture.value==='phone';
        form.querySelector('.phone-network').hidden=!phone;
        form.querySelector('.phone-help').hidden=!phone;
      }
      if(field){
        field.hidden=![form.elements.chat,form.elements.vision].some(select=>select.value.startsWith('openai|'));
        field.querySelector('input').disabled=field.hidden;
      }
    };
    sync();
    form.onchange=()=>{sync();const data=new FormData(form);data.delete('apiKey');localStorage.setItem(key,JSON.stringify(Object.fromEntries(data)))};
    form.onsubmit=async event=>{
      event.preventDefault();
      const button=form.querySelector('.primary'), status=form.querySelector('.launch-status');
      const tab=window.open('about:blank','_blank');
      if(tab){
        tab.opener=null;tab.document.title='正在打开应用';
        tab.document.body.textContent='正在检查服务，应用就绪后会自动打开…';
        tab.document.body.style.cssText='font:16px system-ui;color:#166854;padding:32px;background:#fafcfb';
      }
      button.disabled=true;status.textContent='正在检查并启动服务…';
      try{
        const response=await fetch('/launch/'+form.dataset.app,{method:'POST',headers:{'Content-Type':'application/json','X-Model-Desk':'1'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});
        const result=await response.json();if(!response.ok)throw Error(result.error || '启动失败');
        if(form.elements.vision?.value){
          launchConfig.visionCurrent=form.elements.vision.value.split('|')[1];
          form.elements.vision.options[0].textContent='沿用匠石 · '+launchConfig.visionCurrent;
        }
        const destination=new URL(result.url,location.href);
        if(form.dataset.app==='jshi')destination.searchParams.set('source',result.phone?'phone':'');
        const link=document.createElement('a');link.href=destination.href;link.target='_blank';link.rel='noopener';link.textContent='打开应用网页';
        if(form.elements.apiKey)form.elements.apiKey.value='';
        status.replaceChildren(result.reused?'已复用运行中的匠石。':'服务已就绪。',link);
        if(result.phone){
          const title=document.createElement('strong'), address=document.createElement('p'), cert=document.createElement('a');
          title.textContent='配对码　'+result.phone.code;
          address.textContent='用 iPhone 的 Safari 打开 '+result.phone.url;
          cert.href='/iphone-ca.cer';cert.download='iphone-ca.cer';cert.textContent='下载 iPhone 公开证书';
          status.replaceChildren(title,address,cert,'。首次连接时，把公开证书传到 iPhone，在 Safari 或「文件」中打开并安装，再到「设置 → 通用 → 关于本机 → 证书信任设置」启用完全信任。',document.createElement('br'),link);
        }
        if(tab && !tab.closed)tab.location.replace(destination.href);
      }catch(err){
        status.textContent=err.message;
        if(tab && !tab.closed)tab.document.body.textContent='启动失败：'+err.message;
      }finally{button.disabled=false}
    };
  }
}
function wave() {
  const canvas = $('canvas');
  const ctx = canvas.getContext('2d');
  canvas.width=650; canvas.height=310;
  for(let x=0;x<650;x+=7) for(let j=0;j<42;j++) {
    const y=85+j*3.8+Math.sin(x/74+j/21)*36+Math.sin(x/139-j/18)*33;
    ctx.fillStyle='rgba(39,143,133,'+(.25+.52*(1+Math.cos(j/9+x/118))/2)+')';
    ctx.fillRect(x,y,2.5,2.5);
  }
}
function chatPanel(options, model){
  const choices=options.map(m=>'<option value="'+m.id+'" '+(m.id===model.id?'selected':'')+'>'+escapeText(m.name||m.model)+' · '+escapeText(scopeName(m))+'</option>').join('');
  const attachment=chatInput(model)==='text'?'':`<input hidden id="chat-picture" name="picture" type="file" accept="image/png,image/jpeg,image/webp">
    <button type="button" id="attach" aria-label="附加图片" title="附加图片"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 13 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9"/></svg></button>`;
  return `<form id="task" class="chat-layout">
    <div class="chat-scroll">
      <details class="chat-options"><summary>模型与对话设置</summary>${filters()}
        <label>当前模型<select id="selection">${choices}</select></label>
        <div class="thread-tools"><label>历史对话<select id="thread-picker"></select></label><button type="button" id="new-thread">＋ 新对话</button></div>
        <button type="button" data-edit="${model.id}">聊天参数</button> ${model.service?control(model):''}
      </details>
      <div class="result" id="history"></div><div id="output"></div>
      <details id="detail" hidden><summary>调用详情</summary><pre></pre></details>
    </div>
    <div class="composer"><p id="status" role="status"></p><span id="attached-name"></span>
      <div class="composer-line">${attachment}<textarea name="input" rows="1" aria-label="消息" placeholder="输入消息…"></textarea>
        <button class="primary" aria-label="发送消息" title="发送消息"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button>
      </div>
    </div>
  </form>`;
}
function render(next=page) {
  page=next; renderVersion++;
  document.body.classList.remove("chat-open");
  if(scopeFilter && scopeFilter !== '*' && !projects().includes(scopeFilter))scopeFilter='*';
  if(audioUrl){URL.revokeObjectURL(audioUrl);audioUrl=null}
  const titles={home:'模型设置',apps:'应用设置',try:'简单调用'};
  const title=titles[page] || scenes[page][0];
  $('#crumb').textContent=title;
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===(titles[page]?page:'try')));
  if(page==='home'){
    $('#content').innerHTML=pageHeading('模型设置','同一个模型可供多个项目使用，按需启动各自的服务。',true)+filters()+'<section><div class="line"><h2>模型服务</h2><small>'+available().length+' 个配置</small></div>'+list()+'<p class="muted footnote">语音识别与合成共用一个服务，首次调用时加载对应模型。</p></section>';
    wave();return;
  }
  if(page==='apps'){$('#content').innerHTML=appSettings();bindLaunchers();return}
  if(page==='try'){
    $('#content').innerHTML=pageHeading('简单调用')+'<div class="cards">'+Object.entries(scenes).map(([k,s])=>'<button class="card" data-page="'+k+'">'+icon(k)+'<h3>'+s[0]+'</h3><p>'+s[1]+'</p></button>').join('')+'</div>';return;
  }
  const options=available().filter(m=>m.kind===page);
  if(!options.some(m=>m.id===activeId))activeId=options[0]?.id || '';
  const selectedModel=options.find(m=>m.id===activeId);
  if(page==='chat' && selectedModel){
    document.body.classList.add('chat-open');
    $('#content').innerHTML='<div class="chat-heading"><h1>多模态聊天</h1><small>'+escapeText(selectedModel.name||selectedModel.model)+'</small></div>'+chatPanel(options,selectedModel);
    const form=$('#task');
    if(form.picture){
      $('#attach').onclick=()=>form.picture.click();
      form.picture.onchange=()=>{$('#attached-name').textContent=form.picture.files[0]?.name || ''};
    }
  }else $('#content').innerHTML=pageHeading(title,scenes[page][1])+filters()+(options.length?'<form id="task" class="panel"><label>当前模型<select id="selection">'+options.map(m=>'<option value="'+m.id+'" '+(activeId===m.id?'selected':'')+'>'+escapeText(m.name||m.model)+' · '+escapeText(scopeName(m))+' · '+locationName(m)+'</option>').join('')+'</select></label>'+(page==='chat'?'<div class="thread-tools"><label>历史对话<select id="thread-picker"></select></label><button type="button" id="new-thread">＋ 新对话</button></div><button type="button" data-edit="'+activeId+'">聊天参数</button> ':'')+ (models.find(m=>m.id===activeId)?.service ? control(models.find(m=>m.id===activeId)) : '')+'<div class="result" id="history"></div>'+(page==='asr'?'<label>音频文件<input required type="file" accept="audio/*,.m4a,.mp4,.webm" name="file"></label>':'<label>'+ (page==='chat'?'消息':page==='image'?'画面描述':'合成文本')+'<textarea name="input" placeholder="在此输入文字…"></textarea></label>')+(page==='chat' && chatInput(selectedModel)!=='text'?'<label>附加图片（可选）<input name="picture" type="file" accept="image/png,image/jpeg,image/webp"></label>':'')+(page==='tts'?'<label>音色<input name="voice" required value="Vivian"></label>':'')+'<button class="primary">'+({chat:'发送消息',asr:'开始转写',tts:'生成语音',image:'生成图片'}[page])+'</button><p role="status" id="status"></p><div class="result" id="output"></div><details id="detail" hidden><summary>调用详情</summary><pre></pre></details></form>':'<div class="panel empty"><div><b>还没有'+title+'模型</b><p class="muted">可调整筛选条件，或接入支持此能力的模型。</p></div><button data-add>接入模型</button></div>');
  if(options.length){
    $('#selection').onchange=e=>{activeId=e.target.value;render()};
    $('#task').onsubmit=run;
    if(page==='chat')openChatHistory();
  }
}
function revealResult(element){
  if(!element)return;
  requestAnimationFrame(()=>{
    if(!element.isConnected || document.querySelector('dialog[open]'))return;
    const rect=element.getBoundingClientRect();
    if(rect.top<12 || rect.bottom>window.innerHeight-12){
      element.scrollIntoView({block:'start',behavior:'instant'});
    }
  });
}
function historyView(messages){
  const container=$('#history');container.replaceChildren();
  for(const msg of messages){
    const bubble=document.createElement('div');bubble.className='bubble '+msg.role;
    const who=document.createElement('b');who.textContent=msg.role==='user'?'你':'模型';bubble.append(who);
    const parts=Array.isArray(msg.content)?msg.content:[{type:'text',text:msg.content}];
    for(const part of parts){
      if(part.type==='text'){const p=document.createElement('p');p.textContent=part.text;bubble.append(p)}
      else if(part.image_url){const img=new Image();img.src=part.image_url.url;img.alt='附加图片';bubble.append(img)}
    }
    container.append(bubble);
  }
  const last=container.lastElementChild, scroll=container.closest('.chat-scroll');
  if(last && scroll)requestAnimationFrame(()=>{
    if(last.isConnected)scroll.scrollTop+=last.getBoundingClientRect().top-scroll.getBoundingClientRect().top-12;
  });
}
function configure(m){
  editing=m?.id || null;
  const f=$('#config'); f.reset();
  const project = m ? m.project : (scopeFilter === '*' ? '' : scopeFilter);
  f.scope.value = project ? 'special' : 'common';
  f.project.value = project;
  f.deployment.value = m?.deployment || (locationFilter === 'remote' ? 'remote' : 'local');
  $('#projects').replaceChildren(...projects().map(p => new Option(p,p)));
  updateScope();
  f.endpoint.value=m?.endpoint || (f.deployment.value==='local'?'http://127.0.0.1:11434/v1':'');
  f.model.value=m?.model || ''; f.kind.value=m?.kind || (scenes[page]?page:'chat');
  f.chatInput.value=chatInput(m);
  f.chatInput.disabled=Boolean(m?.service);
  f.paramLayer.value=Object.keys(m?.generation || {}).length?'override':'base';
  f.paramLayer.onchange=loadParamFields;
  f.model.onchange=loadParamFields;
  loadParamFields();
  $('#think-level').hidden=true;
  $('#input-limit').hidden=true;
  f.kind.onchange=()=>{
    const chat=f.kind.value==='chat';
    $('#input-capability').hidden=!chat;
    $('#generation').hidden=!chat;
    $('#generation').disabled=!chat;
  };
  f.kind.onchange();
  f.alias.value=m?.name || '';
  for(const input of [f.endpoint,f.model,f.deployment,f.kind])input.disabled=Boolean(m?.service);
  $('#discover').hidden=Boolean(m?.service);
  $('#formtitle').textContent=m?'编辑模型':'接入模型';
  $('#formmsg').textContent='';$('#discovered').replaceChildren();
  $('#remove')?.remove();
  if(m && !m.service){
    const b=document.createElement('button');b.id='remove';b.type='button';b.textContent='移除配置';f.querySelector(".settings-actions").prepend(b);
    b.onclick=()=>{if(confirm('移除此模型配置？')){models=models.filter(x=>x.id!==editing);save();$('#dialog').close();render()}};
  }
  $('#dialog').showModal();
}
async function api(endpoint,route,body){
  const headers={'X-Model-Endpoint':endpoint};
  if(body && !(body instanceof FormData)){headers['Content-Type']='application/json';body=JSON.stringify(body)}
  const r=await fetch('/api/'+route,{method:'POST',headers,body});
  window.lastCallStatus=r.status;
  if(!r.ok){let msg='服务返回 '+r.status;try{const data=await r.json();msg=data.error?.message || data.detail || msg}catch{}throw Error(msg)}
  return r;
}
$('#discover').onclick=async e=>{
  const b=e.currentTarget, f=$('#config');b.disabled=true;$('#formmsg').textContent='正在查询…';
  try{const r=await api(endpointFor(f),'models');const {data}=await r.json();
    $('#discovered').replaceChildren(...data.map(m=>new Option(m.id,m.id)));
    if(data.length && !f.model.value)f.model.value=data[0].id;
    $('#formmsg').textContent='发现 '+data.length+' 个模型，可在模型 ID 中选择。';
  }catch(err){$('#formmsg').textContent=err.message}finally{b.disabled=false}
};
function updateScope(){
  const f=$('#config'), special=f.scope.value==='special';
  $('#project-field').hidden=!special;
  f.project.required=special;
}
$('#config').scope.onchange=updateScope;
document.addEventListener('change',e=>{
  if(e.target.id==='location-filter'){locationFilter=e.target.value;render()}
});
function endpointFor(f) {
  const u=new URL(f.endpoint.value.trim());
  if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)
    throw Error('请输入 HTTP 或 HTTPS 服务地址。');
  if(f.deployment.value==='local' && !['localhost','127.0.0.1','[::1]'].includes(u.hostname))
    throw Error('此地址位于其他主机，请选择远程服务器。');
  return u.href.replace(/\/+$/,'');
}
$('#config').onsubmit=e=>{
  e.preventDefault();const f=e.target;
  let endpoint;try{endpoint=endpointFor(f)}catch(err){$('#formmsg').textContent=err.message;return}
  const project=f.scope.value==='special'?f.project.value.trim():'';
  if(f.scope.value==='special' && (!project || project==='*')){$('#formmsg').textContent='请输入有效的项目或场景名称。';return}
  const item={...models.find(m=>m.id===editing),name:f.alias.value.trim(),id:editing||crypto.randomUUID(),endpoint,model:f.model.value.trim(),kind:f.kind.value,chatInput:f.kind.value==='chat'?f.chatInput.value:undefined,project,deployment:f.deployment.value};
  let pendingBase;
  if(item.kind==='chat'){
    const params={};
    for(const name of ['historyTurns','max_tokens','temperature','top_p','inputLimit']){
      const value=f.elements[name].value;
      if(value!=='')params[name]=Number(value);
    }
    if(f.thinking.value)params.thinking=f.thinking.value;
    if(f.systemPrompt.value.trim())params.systemPrompt=f.systemPrompt.value.trim();
    const common=f.paramLayer.value==='base';
    const shared=common?params:baseSettings.get(modelKey(item));
    const affected=common?[item,...models.filter(m=>m.id!==item.id && modelKey(m)===modelKey(item))]:[item];
    if(affected.some(m=>{
      const effective={...shared,...(common?m.generation:params)};
      return effective.max_tokens && Number(effective.thinking)>=effective.max_tokens;
    })){
      $('#formmsg').textContent='最大输出需大于思考预算，以便留出回答空间。';return;
    }
    if(common)pendingBase=params;
    else item.generation=params;
  }
  if(!item.model)return;
  if(models.some(m=>m.id!==editing&&m.endpoint===endpoint&&m.model===item.model&&m.kind===item.kind&&m.project===project&&m.deployment===item.deployment)){$('#formmsg').textContent='该模型已接入此场景。';return}
  if(pendingBase){
    baseSettings.set(modelKey(item),pendingBase);
    localStorage.setItem('model-desk.bases',JSON.stringify([...baseSettings]));
  }
  models=editing?models.map(m=>m.id===editing?item:m):[...models,item];save();$('#dialog').close();render();
};
async function chatRequest(model, history){
  const params=resolvedParams(model);
  const messages=params.historyTurns===undefined ? [...history] : history.slice(-(params.historyTurns*2+1));
  if(params.systemPrompt)messages.unshift({role:'system',content:params.systemPrompt});
  const body={model:model.model,messages,stream:false};
  for(const name of ['max_tokens','temperature','top_p'])
    if(params[name]!==undefined)body[name]=params[name];
  if(params.thinking){
    body.enable_thinking=params.thinking!=='off';
    if(body.enable_thinking)body.thinking_budget=Number(params.thinking);
  }
  if(params.inputLimit){
    const input=messages.filter(m=>m.role!=='system').map(m=>({
      role:m.role,content:Array.isArray(m.content)?m.content.map(part=>part.type==='text'
        ? {type:'input_text',text:part.text} : {type:'input_image',image_url:part.image_url.url}):m.content
    }));
    const count=await api(model.endpoint,'responses/input_tokens',{
      model:model.model,input,instructions:params.systemPrompt,enable_thinking:body.enable_thinking
    });
    const {input_tokens:tokens}=await count.json();
    if(!Number.isFinite(tokens))throw Error('服务未返回输入长度');
    if(tokens>params.inputLimit)throw Error('本次输入共 '+tokens+' token，超过设定的 '+params.inputLimit+'。请减少历史轮数、缩短消息或提高上限。');
  }
  return body;
}
async function run(e){
  e.preventDefault();const f=e.target, b=f.querySelector('.primary'), id=activeId, version=renderVersion;
  const m=models.find(m=>m.id===id), thread=m.kind==='chat'?currentThread(id):null;
  if(thread && pendingThreads.has(thread))return;
  if(thread)pendingThreads.add(thread);
  const submittedText=f.input?.value, submittedPicture=f.picture?.files[0];
  let messages=[];
  b.disabled=true;$('#status').textContent='正在处理…';$('#output').replaceChildren();
  const began=performance.now(); let route, metadata={}; window.lastCallStatus=null;
  try{
    let body;
    if(m.kind!=='asr' && !f.input.value.trim() && !(m.kind==='chat' && f.picture?.files.length))throw Error('请输入内容或添加图片');
    if(m.kind==='chat'){let content=f.input.value;
      messages=await threadMessages(thread);
      const picture=submittedPicture;
      if(picture){
        if(picture.size>8*1024*1024)throw Error('请选择小于 8 MB 的图片');
        const url=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('读取图片失败'));reader.readAsDataURL(picture)});
        content=[{type:'text',text:content||'请描述这张图片。'},{type:'image_url',image_url:{url}}];
      }
      messages.push({role:'user',content});route='chat/completions';body=await chatRequest(m,messages)}
    if(m.kind==='asr'){body=new FormData();body.set('file',f.file.files[0]);body.set('model',m.model);route='audio/transcriptions'}
    if(m.kind==='tts'){body={model:m.model,input:f.input.value,voice:f.voice.value,response_format:'mp3'};route='audio/speech'}
    if(m.kind==='image'){route='images/generations';body={model:m.model,prompt:f.input.value,n:1}}
    const r=await api(m.endpoint,route,body);
    const data=m.kind==='tts'?await r.blob():await r.json();
    if(thread){
      const reply=data.choices?.[0]?.message?.content;
      if(typeof reply!=='string')throw Error('模型返回了无法读取的回复。');
      const user=messages[messages.length-1], assistant={role:'assistant',content:reply};
      const firstText=typeof user.content==='string'?user.content:user.content.find(p=>p.type==='text')?.text;
      const record={id:thread,modelId:id,title:threads.get(thread)?.title || (firstText || '图片对话').slice(0,36),updated:Date.now()};
      await chatArchive.append(record,user,assistant);
      threads.set(thread,record);
      if(page==='chat' && activeId===id && currentThread(id)===thread){
        const restored=await threadMessages(thread);
        if(page==='chat' && activeId===id && currentThread(id)===thread){historyView(restored);refreshThreadPicker()}
      }
    }
    if(renderVersion!==version)return;
    metadata=m.kind==='tts'?{字节数:data.size,格式:data.type}:{返回模型:data.model,用量:data.usage,结束原因:data.choices?.[0]?.finish_reason};
    if(m.kind==='chat')metadata.聊天参数=resolvedParams(m);
    if(m.kind==='image'){
      const item=data.data?.[0];if(!item)throw Error('接口没有返回图片');
      const url=item.b64_json?'data:image/png;base64,'+item.b64_json:new URL(item.url,m.endpoint).href;
      if(!item.b64_json && !/^https?:/.test(url))throw Error('无效的图片地址');
      const img=new Image();img.src=url;img.alt='生成的图片';$('#output').append(img);
    }
    if(thread){
      if(f.input.value===submittedText)f.input.value='';
      if(f.picture && f.picture.files[0]===submittedPicture){f.picture.value='';$('#attached-name').textContent=''}
    }
    if(m.kind==='asr')$('#output').textContent=data.text;
    if(m.kind==='tts'){if(audioUrl)URL.revokeObjectURL(audioUrl);audioUrl=URL.createObjectURL(data);const a=new Audio(audioUrl);a.controls=true;$('#output').append(a)}
    $('#status').textContent='已完成';
    if(!thread)revealResult($('#output'));
  }catch(err){if(version===renderVersion)$('#status').textContent=err.message}finally{
    if(thread){
      pendingThreads.delete(thread);
      if(page==='chat' && activeId===id && currentThread(id)===thread)$('#task .primary').disabled=false;
    }
    b.disabled=false;if(version===renderVersion){
    $('#detail').hidden=false;$('#detail pre').textContent=JSON.stringify({模型:m.model,接口:m.endpoint,路径:route,HTTP:window.lastCallStatus,耗时秒:Number(((performance.now()-began)/1000).toFixed(2)),...metadata},null,2)
  }}
}
document.onclick=e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.hasAttribute('data-scope')){scopeFilter=b.dataset.scope;render()}
  if(b.dataset.page)render(b.dataset.page);
  if(b.id==='add'||b.hasAttribute('data-add'))configure();
  if(b.dataset.edit){
    configure(models.find(m=>m.id===b.dataset.edit));
    if(page==='chat')$('#generation').scrollIntoView({block:'center'});
  }
  if(b.dataset.check){const m=models.find(x=>x.id===b.dataset.check);b.disabled=true;b.textContent='检查中';api(m.endpoint,'models').then(()=>b.textContent='连接正常').catch(err=>{b.textContent='重试连接';b.title=err.message;alert(err.message)}).finally(()=>b.disabled=false)}
  if(b.dataset.run){activeId=b.dataset.run;render(models.find(m=>m.id===activeId).kind)}
  if(b.id==='close')$('#dialog').close();
};
window.addEventListener('storage',e=>{
  if(e.key==='model-desk.bases'){
    baseSettings.clear();
    for(const [key,value] of JSON.parse(e.newValue || '[]'))baseSettings.set(key,value);
  }
  if(e.key==='model-desk.models') models=JSON.parse(e.newValue || '[]');
});
async function boot(){
  try{for(const t of await chatArchive.list())threads.set(t.id,t)}
  catch{archiveAvailable=false}
  try{
    const launches=await fetch('/launch-options');
    launchConfig=await launches.json();
  }finally{
    const linkedModel=models.find(m=>location.hash==='#try/'+encodeURIComponent(m.id));
    if(linkedModel)activeId=linkedModel.id;
    render(linkedModel?.kind || (location.hash==='#apps'?'apps':'home'));
  }
}
boot();
