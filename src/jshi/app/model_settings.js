// A single global model; key and selection save together.
(()=>{
 const host=document.querySelector('#debug-purpose')?.closest('details')?.parentElement||document.querySelector('.panel')?.parentElement;
 if(!host)return;
 const panel=document.createElement('details');panel.className='panel';
 panel.innerHTML='<summary>全局模型设置</summary><p class="tip">生成式用途共用一个模型；快速决策模型可独立选择。</p><div class="model-form"></div><p class="model-status" role="status"></p>';
 host.prepend(panel);const status=panel.querySelector('.model-status');let state;
 const el=(tag,text)=>{const n=document.createElement(tag);if(text)n.textContent=text;return n;};
 async function request(data){const r=await fetch('/model-settings',data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const value=await r.json();if(!r.ok)throw Error(value.error||'保存未成功');return value;}
 function showEntryRoute(decision){const r=decision?.entry_last,p=panel.querySelector('[data-entry-route]');if(p)p.textContent=r?.actual?'最近入口实际使用：'+(r.actual==='decision'?'决策模式':'拆分模式')+(r.reason?'；原因：'+({shadow:'仅观察',entry_disabled:'入口决策关闭',unsupported_scope:'本批需要完整判断',host_bridge_unavailable:'此入口尚未连接决策服务',uncertain_or_conflicting:'决策不确定或结果冲突',material_budget:'材料超出入口预算'}[r.reason]||'本批需要完整复核'):''):'最近入口尚无决策模式记录。';}
 function render(){
  const form=panel.querySelector('.model-form');form.replaceChildren();
  const row=state.rows.cognition||Object.values(state.rows)[0];if(!row)return;
  const displayLabel=el('label'),display=el('input');display.type='checkbox';
  try{display.checked=localStorage.getItem('jshi.showModelInfo')==='true';}catch{}
  display.onchange=()=>window.dispatchEvent(new CustomEvent('jshi-model-info',{detail:display.checked}));
  displayLabel.append(display,document.createTextNode(' 默认展开对话中的模型调用信息'));form.append(displayLabel);
  form.append(el('p','当前模型：'+row.current));
  const jevMode=el('select');jevMode.append(new Option('两段串行：先判断入口，按需准备回应','split'),new Option('单次合并：一次完成入口与回应准备','combined'),new Option('决策模型：入口决策，按需准备回应','decision'));
  jevMode.value=['combined','decision'].includes(state.jev_mode)?state.jev_mode:'split';
  form.append(el('p','决策模式需快速决策设置为参与调度；关闭或仅观察时实际执行拆分模式。复杂输入或判断失败可单批回退。页面文字只在决策模式下进入撞车判断。'));
  const jevLabel=el('label','JEV1处理方式');jevLabel.append(jevMode);form.append(jevLabel);
  const modeSave=el('button','保存JEV1处理方式');modeSave.onclick=async()=>{
   modeSave.disabled=true;try{state=await request({jev_mode:jevMode.value});render();status.textContent='已保存，后续输入使用所选处理方式。';}catch(e){status.textContent=e.message;}finally{modeSave.disabled=false;}
  };form.append(modeSave);
  const select=el('select');select.append(new Option('沿用启动配置',''));
  for(const [service,s] of Object.entries(state.services))for(const model of s.models)select.append(new Option(model+' · '+s.label,service+'|'+model));
  const profile=state.global_profile||row.profile,choice=profile.choices?.[0];
  if(choice){const v=choice.service+'|'+choice.model;if(![...select.options].some(o=>o.value===v))select.append(new Option(choice.model,v));select.value=v;}
  const modelLabel=el('label','模型');modelLabel.append(select);form.append(modelLabel);
  const key=el('input');key.type='password';key.autocomplete='new-password';key.style.width='100%';
  const keyLabel=el('label','API Key');keyLabel.append(key);form.append(keyLabel);
  const hint=el('p');hint.className='tip';form.append(hint);
  const effort=el('select');for(const [v,t] of [['off','关闭思考'],['minimal','最低思考'],['low','轻量思考'],['high','充分思考']])effort.append(new Option(t,v));effort.value=profile.effort||'off';
  const effortLabel=el('label','思考强度（入口JEV自动使用快速判断）');effortLabel.append(effort);form.append(effortLabel);
  const advanced=el('details');advanced.append(el('summary','高级：API地址'));
  const endpoint=el('input');endpoint.style.width='100%';advanced.append(endpoint);form.append(advanced);
  const values=()=>{const i=select.value.indexOf('|');return i<0?null:{service:select.value.slice(0,i),model:select.value.slice(i+1)};};
  function update(changed=false){
   const value=values(),s=value?state.services[value.service]:null;
   key.value='';key.disabled=!s;key.placeholder=s?.has_key?'已配置，留空保留；更换时填写新Key':'粘贴所选服务的API Key';
   endpoint.value=s?.endpoint||'';endpoint.disabled=!s;
   if(changed&&value?.service==='qwen')effort.value='low';
   effort.options[0].disabled=value?.service==='gemini';effort.options[1].disabled=value?.model==='gemini-3.8-flash';
   if(effort.selectedOptions[0]?.disabled)effort.value='low';effort.disabled=!s;
   hint.textContent=value?.service==='qwen'?'填百炼北京地域的API Key。地址已预填；业务空间专用地址在高级设置修改。轻量思考预算512 token。':s?.has_key?'密钥已保存，不用重复填写。':s?'填写所选服务的密钥，地址已预填。':'保留启动配置，无需填写密钥。';
  }
  select.onchange=()=>update(true);update();
  const save=el('button','保存并使用');save.onclick=async()=>{
   save.disabled=true;
   try{
    const value=values();
    if(value&&!state.services[value.service].has_key&&!key.value.trim())throw Error('请先在API Key栏粘贴密钥');
    const data={name:'all',jev_mode:jevMode.value,profile:{effort:effort.value,choices:value?[value]:[]}};
    if(value)data.services={[value.service]:{endpoint:endpoint.value.trim(),api_key:key.value.trim()}};
    state=await request(data);render();status.textContent='已保存，后续所有用途统一使用 '+(value?.model||'启动配置')+'。';
   }catch(e){status.textContent=e.message;}finally{save.disabled=false;}
  };form.append(save);
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
   const flags={};for(const [name,label] of [['entry','决策模式入口判断'],['collision','JEV1撞车判断'],['continuation','后续表达影响判断'],['preparation','提前准备与分析（实验，仅本地识别的未完长句）']]){const line=el('label',label),input=el('input');input.type='checkbox';input.checked=decision[name];flags[name]=input;line.prepend(input);box.append(line);}
   const dsave=el('button','保存决策设置'),test=el('button','测试已保存的决策连接');
   dsave.onclick=async()=>{dsave.disabled=true;try{const r=await fetch('/model-settings/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:provider.value,mode:mode.value,providers:{[provider.value]:{model:dmodel.value.trim(),endpoint:url.value.trim(),api_key_env:keyenv.value.trim()}},...Object.fromEntries(Object.entries(flags).map(([k,v])=>[k,v.checked]))})});const d=await r.json();if(!r.ok)throw Error(d.error);state.decision=d;render();status.textContent='决策设置已保存；下一次判断生效。';}catch(e){status.textContent=e.message;}finally{dsave.disabled=false;}};
   test.onclick=async()=>{test.disabled=true;try{const r=await fetch('/model-settings/decision/test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const d=await r.json();if(!r.ok)throw Error(d.error);status.textContent=d.status+' · '+d.model+' · '+d.elapsed_ms+'毫秒'+(d.reason?' · '+d.reason:'');}catch(e){status.textContent=e.message;}finally{test.disabled=false;}};
   box.append(dsave,test);form.append(box);
  }
  const route=el('p');route.dataset.entryRoute='true';form.append(route);showEntryRoute(state.decision);
  const actual=el('details');actual.append(el('summary','实际使用情况'));
  for(const [name,r] of Object.entries(state.rows)){const p=el('p',r.label+'：'+r.current);p.dataset.modelName=name;actual.append(p);}form.append(actual);
 }
 request().then(d=>{state=d;render();}).catch(e=>status.textContent=e.message);
 setInterval(async()=>{if(!panel.open)return;try{const d=await request();showEntryRoute(d.decision);for(const [name,r] of Object.entries(d.rows)){const p=panel.querySelector('[data-model-name="'+name+'"]');if(p)p.textContent=r.label+'：'+r.current+(r.last.model?'；最近调用 '+r.last.model:'');}}catch{}},5000);
})();
