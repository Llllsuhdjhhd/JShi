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
}
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
const sent=[];
const ctx=vm.createContext({document:{getElementById:node,createElement:()=>new Element()},navigator:{},setTimeout,clearTimeout,console,
  Option:class extends Element{constructor(text,value){super();this.textContent=text;this.value=value;}},WebSocket:{OPEN:1},sent,
});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],ctx);
vm.runInContext(`ws={readyState:1,send:x=>sent.push(JSON.parse(x))};
message({type:'timeline',input_id:'one',start_ms:1000,end_ms:3000,text:'第一句',speaker:{object_id:'unknown-A',label:'未知',track_id:'7'}});
message({type:'timeline',input_id:'two',start_ms:3100,end_ms:5000,text:'第二句',speaker:{object_id:'unknown-B',label:'未知',track_id:'9'}});
inputRows.get('one').button.onclick();inputRows.get('two').button.onclick();`,ctx);
assert.equal(node('enroll-count').textContent,'已选 2 段声音');
node('enroll-name').value='lux';node('enroll').onclick();
assert.deepEqual(JSON.parse(JSON.stringify(sent.at(-1))),{type:'enroll_inputs',input_ids:['one','two'],name:'lux'});
vm.runInContext(`message({type:'enrollment',input_ids:['one','two'],object_id:'lux-id',label:'lux',clips:2,seconds:3.9});`,ctx);
assert.match(node('enroll-status').textContent,/声纹已保存/);
assert.equal(node('enroll-count').textContent,'尚未选择声音');
vm.runInContext(`message({type:'debug',section:'prompt',activity_id:'activity',text:'真实提示词',sections:['当前时间','关于输入']});`,ctx);
assert.ok(node('debug-part').options.some(x=>x.value==='关于输入'));
node('debug-part').value='关于输入';node('debug-purpose').value='write_zone';node('debug-turn').value='activity';node('debug-prompt').onclick();
assert.equal(sent.at(-1).part,'关于输入');assert.equal(sent.at(-1).purpose,'write_zone');assert.equal(sent.at(-1).activity_id,'activity');
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
console.log('Voice UI: multi-select, enrollment confirmation, prompt sections and call selection passed');
