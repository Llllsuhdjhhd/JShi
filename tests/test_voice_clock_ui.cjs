const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../src/jshi/app/voice_ui.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script); // Compile the complete page, including all handlers.
const ctx=vm.createContext({});
const nodes=script.match(/const pipelineNodes=.*?;/)[0];
const clock=script.slice(script.indexOf('function wallClock('),script.indexOf('const replyRows='));
const detail=script.slice(script.indexOf('function pipelineDetailText('),script.indexOf('function applyPipeline('));
vm.runInContext(nodes+clock+detail,ctx);
// A cumulative capture pause must never be presented as this sentence's ASR delay.
const packetClockText=vm.runInContext(`pipelineDetailText({timing_version:2,
  speech_start_at_ms:100000,speech_end_at_ms:103600,
  audio_start_received_at_ms:149000,audio_end_received_at_ms:152600,
  asr_returned_at_ms:152800,speech_audio_duration_ms:3600,audio_clock_drift_ms:49000})`,ctx);
assert.match(packetClockText,/识别结果到达  \+0.2秒/);
assert.match(packetClockText,/音频片段长度：3.600秒/);
assert.match(packetClockText,/累计音频时钟偏差：49.0秒（独立诊断，非本句等待）/);
assert.doesNotMatch(packetClockText,/说话结束|断句完成|\+49/);
const legacyClockText=vm.runInContext(`pipelineDetailText({speech_end_at_ms:103600,asr_returned_at_ms:153000})`,ctx);
assert.match(legacyClockText,/无法还原本句识别延迟/);
assert.doesNotMatch(legacyClockText,/\+49/);
assert.doesNotMatch(vm.runInContext(`pipelineDetailText({text_sent_at_ms:200000})`,ctx),/音频/);
console.log('Voice clock UI: packet timing, drift and legacy unknown checks passed');

// Transition speech is a separate instant; it must not become a three-second reply span.
const stepFn=script.slice(script.indexOf('function modelStep('),script.indexOf('function pipelineDetailText('));
const applyFn=script.slice(script.indexOf('function applyPipeline('),script.indexOf('function bindPipelineRow('));
const updateFn=script.slice(script.indexOf('function updateModelPanel('),script.indexOf('function modelNotice('));
vm.runInContext(`const liveStepKeys=new Set(['jev','main','ack','reply','finished']);
  const latestModelInput=null;function positionModelPanel(){};
  const row={modelUi:{pipeline:{},steps:new Map(),started:1794143358500,ended:1794143366000,
    elapsed:{},panelTitle:{},panelBody:{}}};`+stepFn+applyFn+updateFn,ctx);
vm.runInContext(`applyPipeline(row,{jev_started_at_ms:1794143358847,jev_finished_at_ms:1794143361630,
  waiting_reply_sent_at_ms:1794143361970});`,ctx);
assert.equal(vm.runInContext(`row.modelUi.steps.has('reply')`,ctx),false);
vm.runInContext(`applyPipeline(row,{reply_sent_at_ms:1794143364720});`,ctx);
assert.equal(vm.runInContext(`row.modelUi.steps.get('ack').at`,ctx),1794143361970);
assert.equal(vm.runInContext(`row.modelUi.steps.get('reply').at`,ctx),1794143364720);
assert.equal(vm.runInContext(`row.modelUi.steps.get('reply').ended`,ctx),1794143364720);
const flowText=vm.runInContext(`row.modelUi.panelBody.textContent`,ctx);
assert.match(flowText,/21:09:18 → 21:09:21  接话判断/);
assert.match(flowText,/21:09:21  过渡语发出/);
assert.match(flowText,/21:09:24  回复发出/);
assert.doesNotMatch(flowText,/回复发出 ·/);
console.log('Voice clock UI: transition and answer instants remain separate; stage time ranges passed');
