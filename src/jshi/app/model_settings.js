// A single global model; key and selection save together.
(()=>{
 const host=document.querySelector('#debug-purpose')?.closest('details')?.parentElement||document.querySelector('.panel')?.parentElement;
 if(!host)return;
 const panel=document.createElement('details');panel.className='panel';
 panel.innerHTML='<summary>全局模型设置</summary><p class="tip">所有用途共用一个模型。选好后保存即可。</p><div class="model-form"></div><p class="model-status" role="status"></p>';
 host.prepend(panel);const status=panel.querySelector('.model-status');let state;
 const el=(tag,text)=>{const n=document.createElement(tag);if(text)n.textContent=text;return n;};
 async function request(data){const r=await fetch('/model-settings',data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const value=await r.json();if(!r.ok)throw Error(value.error||'保存未成功');return value;}
 function render(){
  const form=panel.querySelector('.model-form');form.replaceChildren();
  const row=state.rows.cognition||Object.values(state.rows)[0];if(!row)return;
  const displayLabel=el('label'),display=el('input');display.type='checkbox';
  try{display.checked=localStorage.getItem('jshi.showModelInfo')==='true';}catch{}
  display.onchange=()=>window.dispatchEvent(new CustomEvent('jshi-model-info',{detail:display.checked}));
  displayLabel.append(display,document.createTextNode(' 默认展开对话中的模型调用信息'));form.append(displayLabel);
  form.append(el('p','当前模型：'+row.current));
  const jevMode=el('select');jevMode.append(new Option('两段串行：先判断入口，按需准备回应','split'),new Option('单次合并：一次完成入口与回应准备','combined'));
  jevMode.value=state.jev_mode==='combined'?'combined':'split';
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
  const actual=el('details');actual.append(el('summary','实际使用情况'));
  for(const [name,r] of Object.entries(state.rows)){const p=el('p',r.label+'：'+r.current);p.dataset.modelName=name;actual.append(p);}form.append(actual);
 }
 request().then(d=>{state=d;render();}).catch(e=>status.textContent=e.message);
 setInterval(async()=>{if(!panel.open)return;try{const d=await request();for(const [name,r] of Object.entries(d.rows)){const p=panel.querySelector('[data-model-name="'+name+'"]');if(p)p.textContent=r.label+'：'+r.current+(r.last.model?'；最近调用 '+r.last.model:'');}}catch{}},5000);
})();
