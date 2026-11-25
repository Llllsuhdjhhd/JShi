import { TalkingHead } from './vendor/talkinghead.mjs';

const shapes = ['aa','E','I','O','U','PP','SS','TH','DD','FF','kk','nn','RR','CH'];
const expressions = {
  '屏幕微笑': { mouthSmileLeft: .36, mouthSmileRight: .36, cheekSquintLeft: .14, cheekSquintRight: .14 },
  '屏幕关切': { browInnerUp: .33, browDownLeft: .12, browDownRight: .12 },
  '屏幕疑惑': { browOuterUpLeft: .45, eyeSquintRight: .23 },
};
const host = document.getElementById('avatar-view');
const status = document.getElementById('avatar-status');
let renderAudio, head, ready = false, currentShape = -1, expressionKeys = [], expressionTimer;
let pendingListen = false, displayReported = false;

function mouth(shape) {
  if (!ready || shape === currentShape) return;
  if (currentShape >= 0) head.setFixedValue('viseme_' + shapes[currentShape], 0);
  currentShape = Number.isInteger(shape) && shapes[shape] ? shape : -1;
  if (currentShape >= 0) head.setFixedValue('viseme_' + shapes[currentShape], .72);
}
function neutral() {
  clearTimeout(expressionTimer);
  for (const key of expressionKeys) head.setFixedValue(key, null);
  expressionKeys = [];
}
function action(actions) {
  if (!visible() || !Array.isArray(actions)) return;
  for (const command of actions) {
    if (command === '屏幕注视') head.lookAtCamera(500);
    else if (command === '屏幕点头') head.playGesture('yes', .85);
    else if (command === '屏幕摇头') head.playGesture('no', .85);
    else if (command === '屏幕平静') neutral();
    else if (Object.hasOwn(expressions, command)) {
      neutral();
      const values = expressions[command];
      expressionKeys = Object.keys(values);
      for (const [key, amount] of Object.entries(values)) head.setFixedValue(key, amount);
      expressionTimer = setTimeout(neutral, 2700);
    }
  }
}
function listen(active) {
  pendingListen = active;
  if (!ready) return;
  if (active) head.lookAtCamera(650);
  for (const key of ['eyeWideLeft', 'eyeWideRight']) head.setFixedValue(key, active ? .10 : null);
}
function reset() {
  listen(false);
  if (ready) { mouth(-1); neutral(); }
}
function visible() { return ready && !document.hidden && host.getClientRects().length > 0; }
window.jshiAvatar = { mouth, listen, action, reset, visible };

async function open() {
  status.textContent = '正在载入形象…';
  try {
    renderAudio = new AudioContext({sinkId: {type: 'none'}});
    head = new TalkingHead(host, {
      audioCtx: renderAudio, lightAmbientIntensity: .8, lightDirectIntensity: 2.2, lightDirectColor: 0xffefe4,
      cameraView: 'head', cameraDistance: .65, cameraY: .32, cameraRotateEnable: false, lipsyncModules: [],
      modelFPS: 26, modelPixelRatio: Math.min(1, 1.5 / devicePixelRatio),
      avatarIdleEyeContact: .72, avatarIdleHeadMove: .12, modelMovementFactor: .26,
    });
    await head.showAvatar({url: '/avatar/mpfb.glb', body: 'F', avatarMood: 'neutral'});
    if (!head) return;
    ready = true;
    status.hidden = true;
    if (pendingListen) listen(true);
    visibility();
  } catch (error) {
    close();
    status.textContent = '形象未能载入，仍可继续对话';
    console.warn('匠石屏幕形象', error);
  }
}
function close() {
  reset();
  ready = false;
  try { head?.dispose(); } finally {
    if (renderAudio && renderAudio.state !== 'closed') renderAudio.close().catch(() => {});
    head = null;
    renderAudio = null;
  }
}
function visibility() {
  const shown = visible();
  if (shown !== displayReported) {
    displayReported = shown;
    window.dispatchEvent(new Event('avatar-display'));
  }
  if (!ready) return;
  if (!shown) head.stop();
  else if (!head.isRunning) head.start();
}
document.addEventListener('visibilitychange', visibility);
new ResizeObserver(visibility).observe(host);
window.addEventListener('pagehide', event => { if (!event.persisted) close(); });
open();
