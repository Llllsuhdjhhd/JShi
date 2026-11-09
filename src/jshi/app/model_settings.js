// A single global model; key and selection save together.
(()=>{
 const host=document.querySelector('#debug-purpose')?.closest('details')?.parentElement||document.querySelector('.panel')?.parentElement;
 if(!host)return;
 const panel=document.createElement('details');panel.className='panel model-settings';panel.open=true;
 panel.innerHTML='<summary>模型与声音</summary><p class="tip">按用途选择。对话设置保存后生效；声音选择用于下一次语音会话。</p><div class="model-form"></div><p class="model-status" role="status"></p>';
 host.prepend(panel);const status=panel.querySelector('.model-status');let state,voiceState=null;
 const asr=document.getElementById('asr'),speaker=document.getElementById('speaker-model'),tts=document.getElementById('tts-backend');
 const asrLabel=asr?.closest('label'),speakerLabel=speaker?.closest('label'),ttsLabel=tts?.closest('label');
 for(const [input,key] of [[asr,'jshi.asrChoice'],[speaker,'jshi.speakerChoice'],[tts,'jshi.ttsChoice']])if(input){
  try{const value=localStorage.getItem(key);if(value!==null&&[...input.options].some(o=>o.value===value))input.value=value;}catch{}
  input.addEventListener('change',()=>{try{localStorage.setItem(key,input.value);}catch{}status.textContent='声音选择已保留，下次开始语音时使用；当前会话不切换。';});
 }
 const style=document.createElement('style');style.textContent='.model-settings .model-form{display:grid;gap:18px}.model-settings .model-card{border:1px solid #e1e6ef;border-radius:12px;padding:18px;background:#fff;min-width:0}.model-settings h3{margin:0 0 7px;font-size:16px}.model-settings .model-card label{display:block;margin:14px 0 6px}.model-settings .model-card select,.model-settings .model-card input:not([type=checkbox]){box-sizing:border-box;width:100%;min-height:40px;border:1px solid #d8dfeb;border-radius:8px;padding:9px 11px}.model-settings .model-card .tip{margin:5px 0 12px;line-height:1.6}.model-settings .model-status{position:sticky;bottom:0;background:#f0f5ff;border-radius:8px;padding:10px;overflow-wrap:anywhere}.model-settings .model-status:empty{display:none}.model-settings .model-current{font-size:13px;color:#53637b}.model-settings .model-advanced>summary{font-size:14px;color:#53637b}';style.textContent+=`.model-settings .model-form{gap:12px}.model-settings .model-card{padding:16px;border-radius:14px;background:#fff}.model-settings .model-card>p{font-size:13px;color:#738097;margin:6px 0 12px}.model-settings button{background:#edf3ff;color:#2763c8;border:1px solid #dbe6ff;border-radius:9px;padding:8px 15px;font-size:13px;font-weight:500;cursor:pointer}.model-settings button:hover{background:#dfeaff}.model-settings button:disabled{opacity:.5;cursor:wait}.model-settings .purpose-row{display:grid;grid-template-columns:minmax(120px,1fr) 2fr 1.2fr auto;gap:8px;align-items:center;padding:12px 0;border-bottom:1px solid #edf0f5}.model-settings .purpose-row small{grid-column:2/-1;color:#7b8798}.model-settings .purpose-row select{min-width:0}.model-settings .model-advanced{margin-top:14px}.model-settings summary{cursor:pointer}.model-settings .model-save-row{display:flex;justify-content:flex-end;margin-top:18px;padding-top:14px;border-top:1px solid #edf0f5}@media(max-width:650px){.model-settings .purpose-row{grid-template-columns:1fr auto}.model-settings .purpose-row select{grid-column:1/-1}.model-settings .purpose-row small{grid-column:1/-1}}`;document.head.append(style);
 const el=(tag,text)=>{const n=document.createElement(tag);if(text)n.textContent=text;return n;};
 async function request(data){const r=await fetch('/model-settings',data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const value=await r.json();if(!r.ok)throw Error(value.error||'保存未成功');return value;}
 const effortLabel=value=>({off:'直接回答（不思考）',minimal:'少量思考',low:'适度思考',medium:'中等思考',high:'高（high）',max:'最高（max）'}[value]||'思考程度未提供');
 const actualModelText=r=>{const c=r.profile?.choices?.[0],native=state.services[c?.service]?.efforts?.[c?.model]?.[r.effective_effort];return r.label+'：'+r.current+' · '+(native||effortLabel(r.effective_effort))+(r.last?.model?'；最近调用 '+r.last.model:'');};
 function fillEfforts(select,service,model,current,inherit=false){
  const options=state.services[service]?.efforts?.[model];
  select.replaceChildren();if(inherit)select.append(new Option('跟随默认思考程度',''));
  for(const [v,label] of Object.entries(options||{off:'服务默认（未配置思考参数）'}))select.append(new Option(label,v));
  if(current!==undefined&&![...select.options].some(o=>o.value===current)){
   const invalid=new Option('原档位不受支持，请重新选择：'+current,current);invalid.disabled=true;select.append(invalid);
  }
  select.value=current??select.options[0]?.value;
 }
 function showEntryRoute(decision){const r=decision?.entry_last,p=panel.querySelector('[data-entry-route]');if(p)p.textContent=r?.actual?'最近入口实际使用：'+(r.actual==='decision'?'决策模式':'拆分模式')+(r.reason?'；原因：'+({shadow:'仅观察',entry_disabled:'入口决策关闭',unsupported_scope:'本批需要完整判断',host_bridge_unavailable:'此入口尚未连接决策服务',uncertain_or_conflicting:'决策不确定或结果冲突',material_budget:'材料超出入口预算'}[r.reason]||'本批需要完整复核'):''):'最近入口尚无决策模式记录。';}
 function render(){
  const form=panel.querySelector('.model-form');form.replaceChildren();
  const conversation=el('section');conversation.className='model-card';conversation.append(el('h3','默认模型'),el('p','未单独设置的用途使用默认模型；展开下方可分别调整。'));form.append(conversation);
  const routing=el('details');routing.className='model-card model-advanced';routing.append(el('summary','高级：接话判断与快速决策'));
  const row=state.rows.cognition||Object.values(state.rows)[0];if(!row)return;
  const displayLabel=el('label'),display=el('input');display.type='checkbox';
  try{display.checked=localStorage.getItem('jshi.showModelInfo')==='true';}catch{}
  display.onchange=()=>window.dispatchEvent(new CustomEvent('jshi-model-info',{detail:display.checked}));
  displayLabel.append(display,document.createTextNode(' 默认展开对话中的模型调用信息'));routing.append(displayLabel);
  const current=el('p','默认使用：'+(state.global_profile?.choices?.[0]?.model||row.current));current.className='model-current';conversation.append(current);
  const jevMode=el('select');jevMode.append(new Option('两段串行：先判断入口，按需准备回应','split'),new Option('单次合并：一次完成入口与回应准备','combined'),new Option('决策模型：入口决策，按需准备回应','decision'));
  jevMode.value=['combined','decision'].includes(state.jev_mode)?state.jev_mode:'split';
  routing.append(el('p','决策模式需快速决策设置为参与调度；关闭或仅观察时实际执行拆分模式。复杂输入或判断失败可单批回退。页面文字只在决策模式下进入撞车判断。'));
  const jevLabel=el('label','接话处理方式');jevLabel.append(jevMode);routing.append(jevLabel);routing.append(el('p','随“保存默认设置”一起保存。通常保持默认即可。'));
  const select=el('select');select.append(new Option('使用服务默认模型',''));
  for(const [service,s] of Object.entries(state.services))for(const model of s.models)select.append(new Option(model+' · '+s.label,service+'|'+model));
  const profile=state.global_profile||row.profile,choice=profile.choices?.[0];
  if(choice){const v=choice.service+'|'+choice.model;if(![...select.options].some(o=>o.value===v))select.append(new Option(choice.model,v));select.value=v;}
  const modelLabel=el('label','默认模型');modelLabel.append(select);conversation.append(modelLabel);
  const key=el('input');key.type='password';key.autocomplete='new-password';key.style.width='100%';
  const keyLabel=el('label','API Key');keyLabel.append(key);conversation.append(keyLabel);
  const hint=el('p');hint.className='tip';conversation.append(hint);
  const effort=el('select');for(const [v,t] of [['off','直接回答'],['minimal','少量思考'],['low','适度思考'],['high','深入思考']])effort.append(new Option(t,v));effort.value=profile.effort||'off';
  const effortLabel=el('label','回答时的思考程度');effortLabel.append(effort);conversation.append(effortLabel);
  const advanced=el('details');advanced.append(el('summary','高级：API地址'));
  const endpoint=el('input');endpoint.style.width='100%';advanced.append(endpoint);conversation.append(advanced);
  const values=()=>{const i=select.value.indexOf('|');return i<0?null:{service:select.value.slice(0,i),model:select.value.slice(i+1)};};
  function update(changed=false){
   const value=values(),s=value?state.services[value.service]:null;
   key.value='';key.disabled=!s;key.placeholder=s?.has_key?'已配置，留空保留；更换时填写新Key':'粘贴所选服务的API Key';
   endpoint.value=s?.endpoint||'';endpoint.disabled=!s;
   fillEfforts(effort,value?.service,value?.model,effort.value);
   effort.disabled=!s;
   hint.textContent=value?.service==='qwen'?'填百炼北京地域的API Key。地址已预填；业务空间专用地址在高级设置修改。轻量思考预算512 token。':s?.has_key?'密钥已保存，不用重复填写。':s?'填写所选服务的密钥，地址已预填。':'保留启动配置，无需填写密钥。';
  }
  update();
  const credentials=el('details'),credentialSummary=el('summary');
  const connectionLabel=()=>{const value=values();return value&&state.services[value.service]?.has_key?'连接设置 · 已配置':'连接设置 · 配置密钥';};
  credentialSummary.textContent=connectionLabel();credentials.append(credentialSummary,keyLabel,hint,advanced);conversation.append(credentials);
  select.onchange=()=>{update(true);credentialSummary.textContent=connectionLabel();};
  const save=el('button','保存默认设置');save.onclick=async()=>{
   save.disabled=true;
   try{
    const value=values();
    if(value&&!state.services[value.service].has_key&&!key.value.trim())throw Error('请先在API Key栏粘贴密钥');
    const data={name:'all',jev_mode:jevMode.value,profile:{effort:effort.value,effort_explicit:true,choices:value?[value]:[]}};
    if(value)data.services={[value.service]:{endpoint:endpoint.value.trim(),api_key:key.value.trim()}};
    state=await request(data);render();status.textContent='默认模型已保存，后续理解与回复使用 '+(state.global_profile?.choices?.[0]?.model||row.current)+'；声音模型分别选择。';
   }catch(e){status.textContent=e.message;}finally{save.disabled=false;}
  };const saveRow=el('div');saveRow.className='model-save-row';saveRow.append(save);conversation.append(saveRow);
  const overrides=el('details');overrides.className='model-card model-advanced';
  overrides.append(el('summary','按用途单独设置'));
  for(const [name,row] of Object.entries(state.rows)){
   const section=el('div');section.className='purpose-row';
   const heading=el('div',row.label);heading.className='purpose-name';section.append(heading);
   const model=el('select');model.setAttribute('aria-label',row.label+'模型');model.append(new Option('跟随默认模型',''));
   for(const [service,s] of Object.entries(state.services))for(const m of s.models)model.append(new Option(m+' · '+s.label,service+'|'+m));
   const choice=row.profile?.choices?.[0];
   if(!row.inherited&&choice){const value=choice.service+'|'+choice.model;if(![...model.options].some(o=>o.value===value))model.append(new Option(choice.model,value));model.value=value;}
   const thought=el('select');thought.setAttribute('aria-label',row.label+'思考程度');
   for(const [v,t] of [['','跟随默认思考程度'],['off','直接回答'],['minimal','少量思考'],['low','适度思考'],['high','深入思考']])thought.append(new Option(t,v));
   const thoughtModel=choice||state.global_profile?.choices?.[0];
   fillEfforts(thought,thoughtModel?.service,thoughtModel?.model,row.inherited?'':row.profile.effort,true);
   model.onchange=()=>{const parts=model.value.split('|'),base=state.global_profile?.choices?.[0];fillEfforts(thought,parts[0]||base?.service,parts[1]||base?.model,thought.value,true);};
   const actual=el('small',actualModelText(row));
   const button=el('button','保存');button.className='save-purpose';
   button.onclick=async()=>{button.disabled=true;try{
    const base=state.global_profile?.choices?.[0];let selected=model.value?{service:model.value.split('|')[0],model:model.value.slice(model.value.indexOf('|')+1)}:base;
    if(thought.value&&!selected)throw Error('请先选择默认模型，再单独设置思考程度');
    const custom=Boolean(model.value||thought.value);
    state=await request({name,profile:custom?{choices:[selected],effort:thought.value||state.global_profile?.effort||'low',effort_explicit:true}:{choices:[]}});
    render();status.textContent=row.label+'已保存';
   }catch(e){status.textContent=e.message;}finally{button.disabled=false;}};
   section.append(model,thought,button,actual);overrides.append(section);
  }
  conversation.append(overrides);
  if(asrLabel||speakerLabel){
   const recognition=el('section');recognition.className='model-card';recognition.append(el('h3','语音转文字'),el('p','把听到的声音转成文字。模型不同，断句和分人能力也不同。'));
   if(asrLabel)recognition.append(asrLabel);
   const availability={online:'online_available',bailian:'bailian_available',deepgram:'deepgram_available',local:'local_available',qwen:'qwen_available'};
   for(const option of asr?.options||[]){option.disabled=Boolean(option.value&&voiceState&&voiceState[availability[option.value]]===false);}
   const rtip=el('p',voiceState?'服务默认：'+(voiceState.asr_backends?.[voiceState.asr]?.label||voiceState.asr):'模型选择用于下次语音会话。');rtip.className='tip';recognition.append(rtip);
   if(asr?.options[0]&&voiceState)asr.options[0].textContent='默认（'+(voiceState.asr_backends?.[voiceState.asr]?.label||voiceState.asr)+'）';
   if(speaker?.options[0]&&voiceState?.speaker_model_default)speaker.options[0].textContent='默认（'+voiceState.speaker_model_default+'）';
   if(tts?.options[0]&&voiceState?.tts)tts.options[0].textContent='默认（'+voiceState.tts+'）';
   const capabilities=el('p');capabilities.className='tip';recognition.append(capabilities);
   const describe=()=>{const value=asr?.value||voiceState?.asr;capabilities.textContent=value==='bailian'?'阿里只负责文字识别；换人边界由本地声音分段校验，分不清时标为不确定。':value==='online'?'火山字词时间与本地分段共同判断换人；声纹单独认人。':value==='deepgram'?'使用服务端人物时间线，并结合本地声纹判断人物。':'使用本地声音处理与转写；未加载的选项不可选择。';};if(asr)asr.onchange=describe;describe();form.append(recognition);
   const identity=el('section');identity.className='model-card';identity.append(el('h3','识别谁在说话'),el('p','本地声纹用于认人，与上面的转文字模型分开选择。'));
   if(speakerLabel)identity.append(speakerLabel);
   const ihint=el('p','CAM++ 与 ERes2NetV2 使用各自的登记样本；换模型后需重新开始语音。');ihint.className='tip';identity.append(ihint);form.append(identity);
   const speech=el('section');speech.className='model-card';speech.append(el('h3','语音播报'),el('p',voiceState?.tts||'沿用服务启动时的播报配置。'));if(ttsLabel)speech.append(ttsLabel);for(const option of tts?.options||[])option.disabled=Boolean(option.value&&voiceState?.tts_backends&&voiceState.tts_backends[option.value]===false);
   speech.append(el('p','播报与识别独立，不会随转文字模型一起切换。'));form.append(speech);
  }
  const decision=state.decision;
  if(decision){
   const box=el('details');box.append(el('summary','快速决策模型（独立选择）'));
   const provider=el('select');provider.append(new Option('阿里云百炼','bailian'),new Option('TypeSafe Jev','typesafe'));provider.value=decision.provider;
   const mode=el('select');mode.append(new Option('关闭','off'),new Option('仅观察，不影响对话','shadow'),new Option('参与调度','active'));mode.value=decision.mode;
   const dmodel=el('input'),url=el('input'),keyenv=el('input');
   const fields=[['供应商',provider],['运行方式',mode],['模型',dmodel],['完整System One地址',url],['密钥环境变量名',keyenv]];
   for(const [title,input] of fields){const label=el('label',title);input.style.width='100%';label.append(input);box.append(label);}
   const dhint=el('p');dhint.className='tip';box.append(dhint);
   function fillDecision(){const p=decision.providers[provider.value];dmodel.value=p.model;url.value=p.endpoint;keyenv.value=p.api_key_env;dhint.textContent=(p.credential_source==='existing_qwen_settings'?'使用已保存的百炼凭据。':p.has_key?'密钥环境变量已配置。':'未检测到密钥环境变量。')+' 决策接口不生成接话或答案；主认知模型单独选择。';}provider.onchange=fillDecision;fillDecision();
   const flags={};for(const [name,label] of [['entry','决策模式入口判断'],['collision','JEV1撞车判断'],['collision_apply','采用撞车结果（阈值校准前只记录，不让旧判断等待）'],['continuation','后续表达影响判断'],['preparation','提前准备与分析（实验，仅本地识别的未完长句）']]){const line=el('label',label),input=el('input');input.type='checkbox';input.checked=decision[name];flags[name]=input;line.prepend(input);box.append(line);}
   const dsave=el('button','保存决策设置'),test=el('button','测试已保存的决策连接');
   dsave.onclick=async()=>{dsave.disabled=true;try{const r=await fetch('/model-settings/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:provider.value,mode:mode.value,providers:{[provider.value]:{model:dmodel.value.trim(),endpoint:url.value.trim(),api_key_env:keyenv.value.trim()}},...Object.fromEntries(Object.entries(flags).map(([k,v])=>[k,v.checked]))})});const d=await r.json();if(!r.ok)throw Error(d.error);state.decision=d;render();status.textContent='决策设置已保存；下一次判断生效。';}catch(e){status.textContent=e.message;}finally{dsave.disabled=false;}};
   test.onclick=async()=>{test.disabled=true;try{const r=await fetch('/model-settings/decision/test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const d=await r.json();if(!r.ok)throw Error(d.error);status.textContent=d.status+' · '+d.model+' · '+d.elapsed_ms+'毫秒'+(d.reason?' · '+d.reason:'');}catch(e){status.textContent=e.message;}finally{test.disabled=false;}};
   box.append(dsave,test);routing.append(box);
  }
  const route=el('p');route.dataset.entryRoute='true';routing.append(route);form.append(routing);showEntryRoute(state.decision);
  const actual=el('details');actual.append(el('summary','实际使用情况'));
  for(const [name,r] of Object.entries(state.rows)){const p=el('p',actualModelText(r));p.dataset.modelName=name;actual.append(p);}routing.append(actual);
 }
 Promise.all([request(),asr?fetch('/status',{cache:'no-store'}).then(r=>r.ok?r.json():null).catch(()=>null):Promise.resolve(null)]).then(([d,v])=>{state=d;voiceState=v;render();}).catch(e=>status.textContent=e.message);
 setInterval(async()=>{if(!panel.open)return;try{const d=await request();showEntryRoute(d.decision);for(const [name,r] of Object.entries(d.rows)){const p=panel.querySelector('[data-model-name="'+name+'"]');if(p)p.textContent=actualModelText(r);}}catch{}},5000);
})();
