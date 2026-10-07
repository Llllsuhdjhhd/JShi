const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element {
 constructor(){this.children=[];this.textContent='';this.value='';this.classList={add(){},toggle(){}};}
 appendChild(x){this.children.push(x);return x;}
 append(...xs){xs.forEach(x=>this.appendChild(x));}
 replaceChildren(...xs){this.children=xs;}
}
const elements={};
const context={document:{getElementById:id=>elements[id]||(elements[id]=new Element()),createElement:()=>new Element(),body:new Element()},
 URLSearchParams,location:{search:''},fetch:()=>new Promise(()=>{}),navigator:{},console};
vm.createContext(context);
const html=fs.readFileSync('src/jshi/app/data_ui.html','utf8');
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
function evaluate(code){return vm.runInContext(code,context);}
assert.match(evaluate("people={p1:'lux'}; recordTitle({object_id:'p1',content:'正文',brief:'精要'},'memory:person_experience_portraits')"),/lux.*经历肖像.*精要/);
assert.match(evaluate("recordTitle({total_ms:1500},'timing')"),/1.50 秒/);
assert.equal(evaluate("organizedView(document.createElement('div'),{query:'旧事',items:[{event_id:'e1',score:.8}],n_items:1},'memory:recall_traces')"),true);
assert.equal(evaluate("organizedView(document.createElement('div'),{content:{text:'原话'},event_type:'external_input',id:'h1'},'memory_placement')"),true);
assert.equal(evaluate("organizedView(document.createElement('div'),{id:'a1',status:'completed'},'subject:activities')"),true);
evaluate("$('sources').value='memory:person_experience_portraits';$('presentation').value='organized'; rows=[{object_id:'p1',brief:'精要',content:'完整正文',shape:{uncertainties:['待核']}}];render()");
function text(element){return String(element.textContent||'')+element.children.map(text).join(' ');}
const rendered=text(elements.rows);
assert.match(rendered,/完整正文/);assert.match(rendered,/精要/);assert.match(rendered,/待核/);assert.match(rendered,/原始 JSON/);
evaluate("$('presentation').value='original';render()");
assert.match(text(elements.rows),/完整正文/);
console.log('Data UI: readable titles, organized memory/recall/placement/activity, original fields passed');
