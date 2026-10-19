const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../src/jshi/app/voice_ui.html'),'utf8');
class Element {
  constructor(){this.children=[];this.style={};this.value='';this.textContent='';const classes=new Set();this.classList={add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x)};}
  appendChild(x){x.parent=this;this.children.push(x);}
  prepend(x){x.parent=this;this.children.unshift(x);}
  replaceChildren(...xs){this.children=[];xs.forEach(x=>this.appendChild(x));}
  add(x){this.appendChild(x);}
  remove(index){if(typeof index==='number'){this.children.splice(index,1);return;}if(this.parent)this.parent.children.splice(this.parent.children.indexOf(this),1);}
  get options(){return this.children;}
  get lastChild(){return this.children.at(-1);}
  focus(){}
  pause(){}
  showModal(){this.open=true;}
  close(){this.open=false;if(this.onclose)this.onclose();}
}
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
const sent=[];
const ctx=vm.createContext({document:{getElementById:node,createElement:()=>new Element()},navigator:{},setTimeout,clearTimeout,setInterval,clearInterval,console,fetch:async()=>({ok:true,json:async()=>({people:[]})}),
  Option:class extends Element{constructor(text,value){super();this.textContent=text;this.value=value;}},WebSocket:{OPEN:1},sent,
});
require('./helpers/browser_environment.cjs')(ctx);
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],ctx);
vm.runInContext(`ws={readyState:1,close(){},send:x=>sent.push(JSON.parse(x))};
message({type:'timeline',input_id:'one',start_ms:1000,end_ms:3000,text:'第一句',speaker:{object_id:'unknown-A',label:'未知',track_id:'7'}});
message({type:'timeline',input_id:'two',start_ms:3100,end_ms:5000,text:'第二句',speaker:{object_id:'unknown-B',label:'未知',track_id:'9'}});
inputRows.get('one').button.onclick();inputRows.get('two').button.onclick();`,ctx);
assert.equal(node('enroll-count').textContent,'已选 2 段声音');
vm.runInContext(`message({type:'transcript_revision',input_ids:['one'],text:'第一句的最终修订'});`,ctx);
assert.equal(vm.runInContext("inputRows.get('one').body.textContent",ctx),'第一句的最终修订');
assert.equal(vm.runInContext('inputRows.size',ctx),2);
node('enroll-name').value='lux';node('enroll').onclick();
assert.deepEqual(JSON.parse(JSON.stringify(sent.at(-1))),{type:'enroll_inputs',input_ids:['one','two'],name:'lux'});
vm.runInContext(`message({type:'enrollment',input_ids:['one','two'],object_id:'lux-id',label:'lux',clips:2,seconds:3.9});`,ctx);
assert.match(node('enroll-status').textContent,/累计已保存/);
assert.equal(node('enroll-count').textContent,'尚未选择声音');
vm.runInContext(`message({type:'enrollment',input_ids:[],object_id:'lux-id',label:'lux',clips:2,seconds:6,user_labeled:true,min_similarity:.428,warning:'已按标注保存'});`,ctx);
assert.match(node('enroll-status').textContent,/兼容录音保留 6 秒/);
assert.match(node('enroll-status').textContent,/最多10段/);
assert.match(node('enroll-status').textContent,/仅供参考/);
assert.doesNotMatch(node('enroll-status').textContent,/要求 0.8/);
vm.runInContext(`message({type:'debug',section:'prompt',activity_id:'activity',text:'真实提示词',sections:['当前时间','关于输入']});`,ctx);
assert.ok(node('debug-part').options.some(x=>x.value==='关于输入'));
node('debug-part').value='关于输入';node('debug-purpose').value='write_zone';node('debug-turn').value='activity';node('debug-prompt').onclick();
assert.equal(sent.at(-1).part,'关于输入');assert.equal(sent.at(-1).purpose,'write_zone');assert.equal(sent.at(-1).activity_id,'activity');
assert.match(html, /value="voice_jev">JEV 入口判断/);
node('debug-part').value='user';node('debug-purpose').value='voice_jev';node('debug-prompt').onclick();
assert.equal(sent.at(-1).purpose,'voice_jev');assert.equal(sent.at(-1).part,'user');
vm.runInContext(`message({type:'timeline',input_id:'mixed',start_ms:5000,end_ms:8000,text:'混合声音',overlap:true,speaker:{object_id:'U',label:'未知',track_id:'9'}});`,ctx);
assert.equal(vm.runInContext(`inputRows.get('mixed').button.disabled`,ctx),true);
vm.runInContext(`ready=true; inputRows.get('one').button.onclick(); $('enroll-name').value='lux'; $('compare-test').onclick();`,ctx);
assert.equal(sent.at(-1).type,'compare_add');assert.equal(sent.at(-1).role,'test');
vm.runInContext(`message({type:'comparison_samples',samples:[{label:'lux',role:'test',seconds:2}],path:'local'});`,ctx);
assert.match(node('compare-report').textContent,/测试 · lux/);
node('match-threshold').value='.55';node('match-threshold').oninput();
assert.equal(node('match-value').textContent,'0.55');node('match-threshold').onchange();
assert.equal(sent.at(-1).type,'voice_settings');assert.equal(sent.at(-1).match_threshold,.55);
vm.runInContext(`message({type:'voice_settings',match_threshold:.55,match_margin:.08});`,ctx);
assert.match(node('match-status').textContent,/已生效并保存/);
vm.runInContext(`message({type:'timeline',input_id:'anon',start_ms:1,end_ms:2,text:'你好',speaker:{object_id:'v',label:'未命名访客 3',track_id:'a'}});`,ctx);
assert.match(vm.runInContext(`inputRows.get('anon').meta.textContent`,ctx),/内部名/);
vm.runInContext(`message({type:'rename_prompt',object_id:'v',previous:'未命名访客 3',name:'小明',text:'要把「未命名访客 3」改成「小明」吗？'});`,ctx);
vm.runInContext(`$('rename-ask').children.find(x=>x.textContent==='确认改名').onclick()`,ctx);
assert.equal(sent.at(-1).accept,true);
vm.runInContext(`message({type:'timeline',input_id:'aside',start_ms:1,end_ms:2,text:'旁人交谈',speaker:{object_id:'x',label:'未知',track_id:'b'}});
message({type:'input_route',input_id:'aside',kept:false});
message({type:'speaker',label:'未知',method:'context_attribution'});`,ctx);
assert.match(vm.runInContext(`inputRows.get('aside').meta.textContent`,ctx),/未交主流程/);
assert.match(node('speaker').textContent,/上下文推测，声纹未确认/);
vm.runInContext(`message({type:'write_failed'});`,ctx);
assert.equal(node('retry-write').hidden,false);
node('retry-write').onclick();
assert.equal(sent.at(-1).type,'retry_write');
assert.equal(node('retry-write').hidden,true);
console.log('Voice UI: multi-select, enrollment confirmation, prompt sections and call selection passed');
vm.runInContext(`message({type:'timeline',input_id:'ranked',start_ms:0,end_ms:3000,text:'比较这句话',
  identity_note:JSON.stringify({candidates:[{object_id:'qf-id',label:'qf',score:.55},{object_id:'lux-id',label:'lux',score:.81},{object_id:'lg-id',label:'luguang',score:.7}]}),
  speaker:{object_id:'pending-id',label:'待定声音 1',track_id:'A'}});`,ctx);
const ranking=vm.runInContext(`inputRows.get('ranked').ranking.textContent`,ctx);
assert.match(ranking,/第一名：lux 0\.810 · 第二名：luguang 0\.700/);
assert.doesNotMatch(ranking,/qf-id|lux-id|lg-id/);
assert.match(vm.runInContext(`inputRows.get('mixed').ranking.textContent`,ctx),/没有有效声纹排名/);
vm.runInContext(`message({type:'identity_final',input_id:'ranked',jev_status:'completed',certainty:'确定',reason:'声音辅助与相邻对话共同支持',
  speaker:{object_id:'lux-id',label:'lux',track_id:'A'},voice_initial:{label:'待定声音 1'}});`,ctx);
assert.match(vm.runInContext(`inputRows.get('ranked').meta.textContent`,ctx),/lux.*JEV 最终判断：确定/);
assert.match(node('speaker').textContent,/lux.*JEV 最终判断：确定/);
assert.match(vm.runInContext(`inputRows.get('ranked').meta.title`,ctx),/声纹初判：待定声音 1/);
vm.runInContext(`message({type:'identity_final',input_id:'one',jev_status:'failed',reason:'输出无效',speaker:{label:'未知',object_id:'old',track_id:'7'},voice_initial:{label:'未知'}});`,ctx);
assert.match(node('speaker').textContent,/lux/); // Older batches must not replace the latest speaker.
vm.runInContext(`message({type:'identity_final',input_id:'ranked',jev_status:'failed',reason:'输出无效',speaker:{label:'待定声音',object_id:'unknown',track_id:'A'},voice_initial:{label:'待定声音'}});`,ctx);
assert.match(node('speaker').textContent,/JEV 判断失败，保留声纹初判/);
// Text remains available in every input mode.
vm.runInContext(`startVoice=async()=>true`,ctx);
assert.equal(node('text-dock').hidden,true);
node('mode-text').onclick();
assert.equal(node('text-dock').hidden,false);
assert.equal(node('voice-dock').hidden,true);
const hiddenBefore=vm.runInContext(`inputRows.get('ranked').extra.hidden`,ctx);assert.equal(hiddenBefore,true);
vm.runInContext(`inputRows.get('ranked').toggle.onclick()`,ctx);assert.equal(vm.runInContext(`inputRows.get('ranked').extra.hidden`,ctx),false);
assert.equal(vm.runInContext(`inputRows.get('one').extra.hidden`,ctx),true);
vm.runInContext(`message({type:'input_route',input_id:'ranked',kept:false})`,ctx);assert.equal(vm.runInContext(`inputRows.get('ranked').extra.hidden`,ctx),false);
vm.runInContext(`inputRows.get('ranked').toggle.onclick()`,ctx);assert.equal(vm.runInContext(`inputRows.get('ranked').extra.hidden`,ctx),true);
node('mode-voice').onclick();
assert.equal(node('text-dock').hidden,false);
node('toggle-inspector').onclick();
assert.equal(node('debug-workspace').hidden,false);
assert.equal(node('conversation').hidden,false);
node('maximize-debug').onclick();
assert.equal(node('conversation').hidden,false);
assert.equal(node('maximize-debug').textContent,'还原');
node('open-work').onclick();
assert.equal(node('settings-pane').hidden,true);
assert.equal(node('data-pane').hidden,false);
assert.match(node('data-frame').src,/source=tool_work/);
node('debug-settings-tab').onclick();
assert.equal(node('settings-pane').hidden,false);
node('close-inspector').onclick();
assert.equal(node('debug-workspace').hidden,true);
assert.equal(node('conversation').hidden,false);
assert.equal(node('toggle-inspector').ariaExpanded,'false');
assert.match(html, /id="media-view"/);
assert.doesNotMatch(html, /id="vision-panel"/);
assert.equal((html.match(/id="composer"/g)||[]).length,1);
console.log('Conversation layout: inline debugging, maximize, restore and hide passed');

vm.runInContext(`message({type:'transcript',text:'<unk> <unk>',final:false})`,ctx);assert.equal(node('live').textContent,'');
vm.runInContext(`message({type:'transcript',text:'<|zh|>你好<unk>',final:false})`,ctx);assert.equal(node('live').textContent,'识别中：你好');
vm.runInContext(`message({type:'transcript',text:'你好',final:true})`,ctx);assert.equal(node('live').textContent,'');

// Details remain accessible with diagnostic content folded by default.
vm.runInContext(`var typedRow=log('lux（文字）：哈喽','input');var typedExtra=typedDetails(typedRow,'lux');`,ctx);
assert.equal(vm.runInContext('typedExtra.hidden',ctx),true);
assert.match(vm.runInContext('typedExtra.children[0].textContent',ctx),/页面选择：lux/);
vm.runInContext('typedRow.inputDetails.toggle.onclick()',ctx);
assert.equal(vm.runInContext('typedExtra.hidden',ctx),false);
assert.equal(vm.runInContext('typedRow.inputDetails.toggle.textContent',ctx),'隐藏');
vm.runInContext('typedRow.inputDetails.toggle.onclick()',ctx);
assert.equal(vm.runInContext('typedExtra.hidden',ctx),true);
vm.runInContext(`var modelRow=log('JEV入口判断：DeepSeek（deepseek-v4-flash）','model-info');`,ctx);
assert.notEqual(vm.runInContext('modelRow.hidden',ctx),true);
assert.equal(vm.runInContext('modelRow.children[1].open',ctx),false);
assert.match(vm.runInContext('modelRow.children[1].children[1].textContent',ctx),/deepseek-v4-flash/);
assert.equal(vm.runInContext(`timingSummary({end_to_end:{jev_ms:1250,reply_ready_ms:2500}})`,ctx),'JEV判断 1.25秒 · 收到输入至回复就绪 2.50秒');
console.log('Input and model details: collapsed by default, accessible on demand passed');

vm.runInContext(`var statusRow=log('当前输入','input');message({type:'model_status',call_id:'main',state:'calling',purpose:'主认知',model:'deepseek-v4-flash'});message({type:'model_status',call_id:'write',state:'calling',purpose:'后台写场',model:'qwen3.5-flash'});`,ctx);
assert.match(vm.runInContext('statusRow.modelUi.records.textContent',ctx),/主认知.*deepseek-v4-flash[\s\S]*后台写场.*qwen3.5-flash/);
assert.equal(vm.runInContext('statusRow.modelUi.button.hidden===true',ctx),false);
vm.runInContext('statusRow.modelUi.button.onclick()',ctx);
assert.equal(vm.runInContext('statusRow.modelUi.extra.hidden',ctx),false);
vm.runInContext(`var nextRow=log('下一条输入','input');message({type:'model_status',call_id:'main',state:'completed',purpose:'主认知',model:'deepseek-v4-flash',elapsed_ms:1230})`,ctx);
assert.equal(vm.runInContext('statusRow.modelUi.button.hidden',ctx),true);
assert.equal(vm.runInContext('statusRow.modelUi.extra.hidden',ctx),true);
assert.equal(vm.runInContext('statusRow.modelUi.button.textContent',ctx),'模型 · 调用中');
assert.match(vm.runInContext('nextRow.modelUi.records.textContent',ctx),/准备处理本条输入/);
vm.runInContext(`message({type:'model_status',call_id:'write',state:'failed',purpose:'后台写场',model:'qwen3.5-flash',elapsed_ms:2200})`,ctx);
assert.match(vm.runInContext('statusRow.modelUi.records.textContent',ctx),/失败.*2.20秒/);
assert.equal(vm.runInContext('statusRow.modelUi.button.hidden',ctx),true);
vm.runInContext('statusRow.inputDetails.toggle.onclick()',ctx);
assert.equal(vm.runInContext('statusRow.modelUi.extra.hidden',ctx),false);
console.log('Per-input model button: expansion, latest-only display and late events passed');

vm.runInContext(`var legacyRow=log('兼容旧服务','input');modelProgress(legacyRow,'JEV 判断');`,ctx);
assert.equal(vm.runInContext('legacyRow.modelUi.button.textContent',ctx),'模型 · JEV 判断');
vm.runInContext(`modelProgress(legacyRow,'处理失败',true);`,ctx);
assert.equal(vm.runInContext('legacyRow.modelUi.button.textContent',ctx),'模型 · 处理失败');
console.log('Model progress: phase fallback and terminal failure passed');

vm.runInContext(`var visibleStatus=log('本条','input');modelProgress(visibleStatus,'主认知 · 模型思考');message({type:'model_status',call_id:'side-test',state:'calling',purpose:'主认知',model:'deepseek-v4-flash'});`,ctx);
assert.notEqual(vm.runInContext('visibleStatus.modelUi.panel.hidden',ctx),true);
assert.match(vm.runInContext('visibleStatus.modelUi.panelBody.textContent',ctx),/主认知 · 进行中/);
assert.doesNotMatch(vm.runInContext('visibleStatus.modelUi.panelBody.textContent',ctx),/deepseek-v4-flash|模型思考/);
assert.match(vm.runInContext('visibleStatus.modelUi.records.textContent',ctx),/deepseek-v4-flash/);
vm.runInContext(`var newerStatus=log('下一条','input');message({type:'model_status',call_id:'side-test',state:'completed',purpose:'主认知',model:'deepseek-v4-flash',elapsed_ms:1200})`,ctx);
assert.equal(vm.runInContext('visibleStatus.modelUi.panel.hidden',ctx),true);
assert.notEqual(vm.runInContext('newerStatus.modelUi.panel.hidden',ctx),true);
console.log('Visible latest-input status panel: model/stage and previous panel hiding passed');
// Full timeline is preserved; bottom-positioned input moves the panel upward.
vm.runInContext(`var longStatus=log('过程测试','input');modelProgress(longStatus,'JEV 判断');modelProgress(longStatus,'主认知 · 模型思考');modelProgress(longStatus,'已完成',true);`,ctx);
assert.match(vm.runInContext('longStatus.modelUi.panelBody.textContent',ctx),/收到输入[\s\S]*接话判断[\s\S]*主认知[\s\S]*本轮完成/);
vm.runInContext(`window.innerHeight=800;$('log').getBoundingClientRect=()=>({left:520});messageScroll.getBoundingClientRect=()=>({left:520});longStatus.getBoundingClientRect=()=>({left:600,top:730});longStatus.modelUi.panel.scrollHeight=500;positionModelPanel();`,ctx);
assert.equal(vm.runInContext('longStatus.modelUi.panel.style.top',ctx),'284px');
assert.equal(vm.runInContext('longStatus.modelUi.panel.style.width',ctx),'440px');
assert.ok(24+parseFloat(vm.runInContext('longStatus.modelUi.panel.style.width',ctx))<520);
assert.match(vm.runInContext('longStatus.modelUi.elapsed.textContent',ctx),/页面处理用时：.*秒/);
vm.runInContext('longStatus.modelUi.panel.scrollHeight=2000;positionModelPanel()',ctx);
assert.equal(vm.runInContext('longStatus.modelUi.panel.style.top',ctx),'16px');
assert.equal(vm.runInContext('longStatus.modelUi.panel.style.maxHeight',ctx),'768px');
vm.runInContext(`$('log').getBoundingClientRect=()=>({left:30});messageScroll.getBoundingClientRect=()=>({left:30});longStatus.getBoundingClientRect=()=>({left:30,top:730});positionModelPanel();`,ctx);
assert.equal(vm.runInContext('longStatus.modelUi.panel.style.position',ctx),'static');
console.log('Model timeline: complete stage history, bottom clamp and narrow layout passed');

// One-click labels use the original voiceprint ranking and wait for persistence.
vm.runInContext(`trainingPeople=[{object_id:'lux-id',label:'lux'},{object_id:'lg-id',label:'luguang'}];
refreshTrainingLabel(inputRows.get('ranked'));`,ctx);
assert.equal(vm.runInContext(`inputRows.get('ranked').training.pick.value`,ctx),'lux-id');
vm.runInContext(`inputRows.get('ranked').training.button.onclick()`,ctx);
assert.deepEqual(JSON.parse(JSON.stringify(sent.at(-1))),{type:'personal_label',input_ids:['ranked'],object_id:'lux-id'});
assert.equal(vm.runInContext(`inputRows.get('ranked').training.button.textContent`,ctx),'保存中…');
vm.runInContext(`message({type:'personal_labelled',input_ids:['ranked'],object_id:'lux-id',label:'lux',samples:1})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('ranked').training.button.textContent`,ctx),'✓ 已加入');
vm.runInContext(`message({type:'identity_final',input_id:'ranked',jev_status:'completed',speaker:{object_id:'lg-id',label:'luguang'},voice_initial:{label:'未知'}})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('ranked').who.textContent`,ctx),'lux[人工确定]：');
vm.runInContext(`inputRows.get('ranked').training.pick.value='lg-id';inputRows.get('ranked').training.pick.onchange();inputRows.get('ranked').training.button.onclick();`,ctx);
assert.equal(sent.at(-1).object_id,'lg-id');
vm.runInContext(`message({type:'personal_label_error',input_ids:['ranked'],object_id:'lg-id',text:'采集队列已满'})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('ranked').training.button.textContent`,ctx),'保存更正');
assert.match(vm.runInContext(`inputRows.get('ranked').training.status.textContent`,ctx),/队列已满/);
vm.runInContext(`inputRows.get('ranked').training.button.onclick();message({type:'personal_labelled',input_ids:['ranked'],object_id:'lg-id',label:'luguang',samples:1})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('ranked').training.savedId`,ctx),'lg-id');
assert.equal(vm.runInContext(`inputRows.get('mixed').training.button.disabled`,ctx),true);
assert.equal(vm.runInContext(`inputRows.get('anon').training.pick.value`,ctx),'');
node('toggle-inspector').onclick();
vm.runInContext(`positionModelPanel()`,ctx);
assert.equal(node('conversation').hidden,false);
assert.equal(vm.runInContext(`latestModelInput.modelUi.panel.hidden`,ctx),true);
node('close-inspector').onclick();
vm.runInContext(`positionModelPanel()`,ctx);
assert.equal(vm.runInContext(`latestModelInput.modelUi.panel.hidden`,ctx),false);
console.log('Inline training: candidate default, saved check, relabel, failure and preserved conversation passed');

// Weak voiceprint ranking only preselects the dataset label. JEV uncertainty stays visible.
vm.runInContext(`message({type:'timeline',input_id:'weak',start_ms:20000,end_ms:24000,text:'弱声纹测试',
  identity_note:'本句原声纹仍待确认',voiceprint_candidates:[{object_id:'lux-id',label:'lux',score:.127,source:'voiceprint'},{object_id:'lg-id',label:'luguang',score:.086,source:'voiceprint'}],
  speaker:{object_id:'pending',label:'声音归属待定',status:'unknown',method:'voice_continuity',track_id:'weak-track'}})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('weak').training.pick.value`,ctx),'lux-id');
assert.equal(vm.runInContext(`inputRows.get('weak').who.textContent`,ctx),'归属待定：');
assert.match(vm.runInContext(`inputRows.get('weak').ranking.textContent`,ctx),/第一名：lux 0.127.*第二名：luguang 0.086.*差距：0.041/);
vm.runInContext(`message({type:'identity_final',input_id:'weak',jev_status:'completed',certainty:'可能',reason:'声纹较弱，上下文仅支持可能',
  speaker:{object_id:'lux-id',label:'lux',status:'provisional',method:'jev_attribution'},voice_initial:{label:'声音归属待定'}})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('weak').who.textContent`,ctx),'lux[JEV可能]：');
assert.match(vm.runInContext(`inputRows.get('weak').jevNote.textContent`,ctx),/JEV未提供数值置信度/);
assert.equal(vm.runInContext(`inputRows.get('weak').training.savedId`,ctx),'');
vm.runInContext(`inputRows.get('weak').training.button.onclick();message({type:'personal_labelled',input_ids:['weak'],object_id:'lux-id',label:'lux',samples:1});inputRows.get('weak').training.undo.onclick()`,ctx);
assert.deepEqual(JSON.parse(JSON.stringify(sent.at(-1))),{type:'personal_unlabel',input_ids:['weak']});
assert.equal(vm.runInContext(`inputRows.get('weak').training.undo.disabled`,ctx),true);
vm.runInContext(`message({type:'personal_label_error',input_ids:['weak'],action:'undo',text:'暂不能撤销'})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('weak').training.savedId`,ctx),'lux-id');
vm.runInContext(`inputRows.get('weak').training.undo.onclick();message({type:'personal_unlabelled',input_ids:['weak'],restored:[{input_id:'weak',speaker:{object_id:'lux-id',label:'lux',status:'provisional',method:'jev_attribution'}}]})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('weak').training.savedId`,ctx),'');
assert.equal(vm.runInContext(`inputRows.get('weak').training.undo.hidden`,ctx),true);
assert.match(vm.runInContext(`inputRows.get('weak').who.textContent`,ctx),/lux\[JEV可能\]/);
assert.equal(vm.runInContext(`inputRows.get('weak').training.pick.value`,ctx),'lux-id');
console.log('Weak evidence: default training candidate, explicit uncertainty, score gap and undo passed');

vm.runInContext(`message({type:'timeline',input_id:'auto-row',start_ms:25000,end_ms:28000,text:'自动收录',voiceprint_candidates:[
{object_id:'lux-id',label:'lux',score:.555},{object_id:'lg-id',label:'luguang',score:.20}],speaker:{label:'声音归属待定',track_id:'A'}});
message({type:'personal_auto_labelled',input_ids:['auto-row'],object_id:'lux-id',label:'lux',score:.555});`,ctx);
assert.equal(vm.runInContext(`inputRows.get('auto-row').training.button.textContent`,ctx),'✓ 已自动加入');
assert.equal(vm.runInContext(`inputRows.get('auto-row').training.undo.hidden`,ctx),false);
assert.equal(vm.runInContext(`inputRows.get('auto-row').who.textContent`,ctx),'归属待定：');
vm.runInContext(`message({type:'identity_final',input_id:'auto-row',jev_status:'completed',certainty:'可能',speaker:{object_id:'lux-id',label:'lux',status:'provisional',method:'jev_attribution'},voice_initial:{label:'声音归属待定'}})`,ctx);
assert.match(vm.runInContext(`inputRows.get('auto-row').who.textContent`,ctx),/lux\[JEV可能\]/);
vm.runInContext(`inputRows.get('auto-row').training.undo.onclick();message({type:'personal_unlabelled',input_ids:['auto-row'],restored:[{input_id:'auto-row',speaker:{},identity_unchanged:true}]})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('auto-row').training.button.textContent`,ctx),'加入训练');
assert.match(vm.runInContext(`inputRows.get('auto-row').who.textContent`,ctx),/lux\[JEV可能\]/);
console.log('Automatic training data: persisted check, independent attribution and reversible collection passed');

vm.runInContext(`message({type:'timeline',input_id:'mlp-output',start_ms:29000,end_ms:29500,text:'个人模型输出',
speaker:{object_id:'pending',label:'声音归属待定',track_id:'A'},voiceprint_candidates:[{object_id:'lux-id',label:'lux',score:.15}],
personal_prediction:{status:'uncertain',matched:'',ranking:[{object_id:'lux-id',label:'lux',score:.72,threshold:.8},{object_id:'lg-id',label:'luguang',score:.12,threshold:.9}]}})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('mlp-output').personalPrediction.element.textContent`,ctx),'MLP：lux 0.720 · luguang 0.120');
assert.match(vm.runInContext(`inputRows.get('mlp-output').personalPrediction.element.title`,ctx),/lux模型门槛 0.800.*luguang模型门槛 0.900/);
vm.runInContext(`inputRows.get('mlp-output').training.pick.value='lg-id';inputRows.get('mlp-output').training.pick.onchange()`,ctx);
assert.equal(vm.runInContext(`inputRows.get('mlp-output').personalPrediction.element.textContent`,ctx),'MLP：lux 0.720 · luguang 0.120');
assert.equal(vm.runInContext(`personalPredictionText({status:'skipped_identified',ranking:[]})`,ctx),'MLP：本句已有归属，未调用');
assert.equal(vm.runInContext(`personalPredictionText({status:'no_ready_models',ranking:[]})`,ctx),'MLP：暂无适用模型');
assert.equal(vm.runInContext(`personalPredictionText({status:'timeout',ranking:[]})`,ctx),'MLP：本次计算超时');
console.log('Personal MLP display: named outputs below threshold, stable snapshots and explicit missing-result states passed');
for(const source of ['人工','自述','声纹','MLP','JEV','综合','未知'])for(const state of ['确定','可能','待定']){
  const caption=vm.runInContext(`identityCaption({label:'这是一个非常长的测试人物名字😀abcdefghijk',source:${JSON.stringify(source)},state:${JSON.stringify(state)},score:.936})+'：'`,ctx);
  assert.ok(Array.from(caption).length<=20,caption);
}
assert.equal(vm.runInContext(`identityCaption({label:'luguang',source:'MLP',state:'可能',score:.936})`,ctx),'luguang[MLP可能0.94]');
assert.equal(vm.runInContext(`identityCaption({label:'qf',source:'综合',state:'确定',score:1})`,ctx),'qf[综合确定]');
vm.runInContext(`trainingPeople=[{object_id:'lux-id',label:'lux'},{object_id:'lg-id',label:'luguang'},{object_id:'qf-id',label:'qf'}];
message({type:'timeline',input_id:'preferred-mlp',start_ms:31000,end_ms:32000,text:'这个意思吧',speaker:{object_id:'unknown',label:'声音归属待定',status:'unknown',method:'unassigned_audio',track_id:'v'},voiceprint_candidates:[{object_id:'lg-id',label:'luguang',score:.265},{object_id:'qf-id',label:'qf',score:.226}],personal_prediction:{ranking:[{object_id:'qf-id',label:'qf',score:1,threshold:.8,verified:false},{object_id:'lg-id',label:'luguang',score:0,threshold:.8,verified:false}]},identity_comparison:{state:'conflict',mlp_priority:true,voice:{object_id:'lg-id',label:'luguang',score:.265},mlp:{object_id:'qf-id',label:'qf',score:1}},identity_display:{label:'qf',source:'MLP',state:'可能',score:1,reason:'优先候选，尚未确认'}});
message({type:'identity_final',input_id:'preferred-mlp',jev_status:'completed',certainty:'不确定',speaker:{object_id:'unknown',label:'声音归属待定',status:'unknown',method:'unassigned_audio'},voice_initial:{label:'声音归属待定'},identity_display:{label:'qf',source:'MLP',state:'可能',score:1,reason:'优先候选，尚未确认'}});`,ctx);
assert.equal(vm.runInContext(`inputRows.get('preferred-mlp').who.textContent`,ctx),'qf[MLP可能1.00]：');
assert.equal(vm.runInContext(`inputRows.get('preferred-mlp').training.pick.value`,ctx),'lg-id');
assert.match(vm.runInContext(`inputRows.get('preferred-mlp').who.title`,ctx),/尚未确认/);
assert.equal(vm.runInContext(`inputRows.get('preferred-mlp').identityDetails.hidden`,ctx),true);
vm.runInContext(`inputRows.get('preferred-mlp').who.onclick()`,ctx);
assert.equal(vm.runInContext(`inputRows.get('preferred-mlp').identityDetails.hidden`,ctx),false);
assert.match(vm.runInContext(`inputRows.get('preferred-mlp').identityDetails.textContent`,ctx),/第一名：luguang 0.265.*第二名：qf 0.226.*差距：0.039/);
assert.match(vm.runInContext(`inputRows.get('preferred-mlp').identityDetails.textContent`,ctx),/MLP首名领先次名：1.000000/);
assert.match(vm.runInContext(`inputRows.get('preferred-mlp').identityDetails.textContent`,ctx),/JEV：不确定/);
vm.runInContext(`inputRows.get('preferred-mlp').who.onkeydown({key:'Enter',preventDefault(){}})`,ctx);
assert.equal(vm.runInContext(`inputRows.get('preferred-mlp').identityDetails.hidden`,ctx),true);
assert.equal(vm.runInContext(`inputRows.get('preferred-mlp').who.ariaExpanded`,ctx),'false');

vm.runInContext(`message({type:'timeline',input_id:'joint-conflict',start_ms:29000,end_ms:30000,text:'分歧测试',speaker:{object_id:'unknown',label:'声音归属待定',track_id:'x'},identity_comparison:{state:'conflict',voice:{label:'lux',score:.223},mlp:{label:'陆广',score:.999}}});`,ctx);
assert.equal(vm.runInContext(`inputRows.get('joint-conflict').identityConflict.className`,ctx),'identity-conflict');
assert.match(vm.runInContext(`inputRows.get('joint-conflict').identityConflict.textContent`,ctx),/声纹与MLP排名不一致：lux 0.223 \/ 陆广 0.999/);
vm.runInContext(`message({type:'timeline',input_id:'tv-other',start_ms:30000,end_ms:31000,text:'电视声音',speaker:{object_id:'unknown-tv',label:'声音归属待定',track_id:'tv'}});
  message({type:'personal_labelled',input_ids:['tv-other'],object_id:'__negative_other__',label:'其他（仅反例）',negative_only:true,samples:1});`,ctx);
assert.equal(vm.runInContext(`inputRows.get('tv-other').training.pick.value`,ctx),'__negative_other__');
assert.equal(vm.runInContext(`inputRows.get('tv-other').training.train.disabled`,ctx),true);
assert.equal(vm.runInContext(`inputRows.get('tv-other').training.undo.hidden`,ctx),false);
assert.equal(vm.runInContext(`inputRows.get('tv-other').who.textContent`,ctx),'归属待定：');
assert.equal(vm.runInContext(`personalPredictionText({ranking:[{label:'lux',score:.9,verified:false}]})`,ctx),'MLP：lux 0.900（试用）');

// Exercise the actual text-submit handler with an older server's NDJSON events.
(async()=>{
  await Promise.resolve();await Promise.resolve();
  const contextRequests=[];
  ctx.fetch=async(url,options)=>{contextRequests.push({url,body:options?.body});return {ok:true};};
  vm.runInContext(`$('include-unplayed').checked=true`,ctx);
  await vm.runInContext(`$('include-unplayed').onchange()`,ctx);
  assert.equal(contextRequests[0].url,'/api/voice-context');
  assert.deepEqual(JSON.parse(contextRequests[0].body),{include_unplayed:true});
  assert.match(node('voice-context-status').textContent,/已保存/);
  vm.runInContext(`trainingPeople=[{object_id:'lux-id',label:'lux'},{object_id:'lg-id',label:'luguang'}];refreshTrainingLabel(inputRows.get('auto-row'))`,ctx);
  const trainingRequests=[];
  ctx.fetch=async(url,options)=>{trainingRequests.push({url,body:options?.body});return {ok:true,json:async()=>options?.method==='POST'?{queued:true}:{targets:[{object_id:'lux-id',status:'collecting',detail:'校准样本不足'}]}};};
  await vm.runInContext(`inputRows.get('auto-row').training.train.onclick()`,ctx);
  assert.deepEqual(JSON.parse(trainingRequests[0].body),{action:'train',object_id:'lux-id',select_target:true});
  assert.equal(vm.runInContext(`trainingRuns.get('lux-id').running`,ctx),false);
  assert.equal(vm.runInContext(`trainingRuns.get('lux-id').detail`,ctx),'尚未开始训练，缺少正例或反例');
  vm.runInContext(`trainingPeople=[{object_id:'lux-id',label:'lux'},{object_id:'lg-id',label:'luguang'}];refreshTrainingLabel(inputRows.get('auto-row'))`,ctx);
  ctx.fetch=async(url,options)=>({ok:true,json:async()=>options?.method==='POST'?{queued:true}:{targets:[{
    object_id:'lux-id',status:'collecting',progress:{positive:19,positive_sessions:1,negative_manual:0,negative_public:720,
      counts:{train:{positive:0,negative:504},calibration:{positive:0,negative:180},validation:{positive:18,negative:36}},
      minimum:{train:{positive:12,negative:24},calibration:{positive:4,negative:12},validation:{positive:4,negative:12}}}
  }]}});
  await vm.runInContext(`inputRows.get('auto-row').training.train.onclick()`,ctx);
  assert.equal(vm.runInContext(`trainingRuns.get('lux-id').detail`,ctx),'尚未开始训练：本人 19 条，反例 720 条（至少各一条有效录音）');
  assert.match(vm.runInContext(`inputRows.get('auto-row').training.trainStatus.title`,ctx),/本人 1 次会话.*人工标定其他人 0 条.*公开背景 720 条/);
  console.log('Train once: immediate request for selected person and meaningful final status passed');
  ctx.TextDecoder=TextDecoder;
  const events=[{type:'accepted'},{type:'flow_phase',stage:'jev'},{type:'model_notice',text:'JEV：正在调用 DeepSeek'},{type:'error',message:'The read operation timed out'}];
  const states=[];let index=0;
  ctx.fetch=async()=>({ok:true,headers:{get:()=> 'application/x-ndjson'},body:{getReader:()=>({read:async()=>{
    states.push(vm.runInContext('latestModelInput.modelUi.button.textContent',ctx));
    return index<events.length?{done:false,value:Buffer.from(JSON.stringify(events[index++])+'\n')}:{done:true};
  }})}});
  vm.runInContext(`ready=false;textSending=false;$('text-person').replaceChildren(new Option('lux','lux-id'));$('text-person').value='lux-id';$('typed').value='明天天气怎样';`,ctx);
  await vm.runInContext(`$('composer').onsubmit({preventDefault(){}})`,ctx);
  assert.ok(states.includes('模型 · JEV 判断'));
  assert.ok(states.includes('模型 · 调用中'));
  assert.equal(vm.runInContext('latestModelInput.modelUi.button.textContent',ctx),'模型 · 处理失败');
  assert.match(vm.runInContext('latestModelInput.modelUi.extra.children.at(-1).textContent',ctx),/read operation timed out/);
  console.log('Text stream: live stage, calling notice and timeout failure passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

assert.match(vm.runInContext(`personalPredictionText({overlap:true,ranking:[{label:'lux',score:.97}]})`,ctx),/MLP（重叠参考）：lux 0.970/);

vm.runInContext(`message({type:'personal_prediction',input_id:'preferred-mlp',identity_display:{label:'qf',source:'MLP',state:'可能',score:.987,reason:'后台已补回'},personal_prediction:{status:'uncertain',ranking:[{label:'qf',object_id:'qf-id',score:.987,verified:false}]},identity_comparison:{state:'mlp_only',mlp_high:true}})`,ctx);
assert.match(vm.runInContext(`inputRows.get('preferred-mlp').personalPrediction.element.textContent`,ctx),/qf 0.987/);
assert.match(vm.runInContext(`inputRows.get('preferred-mlp').identityDetails.textContent`,ctx),/0.987000/);
assert.equal(vm.runInContext(`inputRows.get('preferred-mlp').identityConflict.hidden`,ctx),true);

assert.equal(vm.runInContext(`inputRows.get('preferred-mlp').who.textContent`,ctx),'qf[MLP可能0.99]：');
