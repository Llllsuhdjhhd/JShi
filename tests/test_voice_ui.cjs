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
const ctx=vm.createContext({document:{getElementById:node,createElement:()=>new Element()},navigator:{},setTimeout,clearTimeout,console,fetch:async()=>({ok:true,json:async()=>({people:[]})}),
  Option:class extends Element{constructor(text,value){super();this.textContent=text;this.value=value;}},WebSocket:{OPEN:1},sent,
});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],ctx);
vm.runInContext(`ws={readyState:1,close(){},send:x=>sent.push(JSON.parse(x))};
message({type:'timeline',input_id:'one',start_ms:1000,end_ms:3000,text:'第一句',speaker:{object_id:'unknown-A',label:'未知',track_id:'7'}});
message({type:'timeline',input_id:'two',start_ms:3100,end_ms:5000,text:'第二句',speaker:{object_id:'unknown-B',label:'未知',track_id:'9'}});
inputRows.get('one').button.onclick();inputRows.get('two').button.onclick();`,ctx);
assert.equal(node('enroll-count').textContent,'已选 2 段声音');
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
assert.equal(node('conversation').hidden,true);
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
