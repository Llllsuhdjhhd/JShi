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
