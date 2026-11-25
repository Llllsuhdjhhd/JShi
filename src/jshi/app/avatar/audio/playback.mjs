// One analysis-only branch of the actual speaker bus. HeadAudio has zero outputs.
const worklets = new WeakMap();

export function observePlayback(bus, onShape, onError) {
  const context = bus.context;
  let closed = false, node = null, last = -1;
  const publish = shape => {
    if (!closed && shape !== last) {
      last = shape;
      onShape(shape);
    }
  };
  async function connect() {
    const { HeadAudio } = await import('./headaudio.mjs');
    if (closed) return;
    if (!worklets.has(context)) {
      const registration = context.audioWorklet.addModule('/avatar/audio/headworklet.mjs');
      worklets.set(context, registration);
      registration.catch(() => worklets.delete(context));
    }
    await worklets.get(context);
    if (closed) return;
    const detector = new HeadAudio(context, {visemeMinMs: 65});
    node = detector;
    await detector.loadModel('/avatar/audio/model-en-mixed.bin');
    if (closed) return;
    // Let HeadAudio process its own message first, then forward only shape changes.
    detector.port.addEventListener('message', () => publish(detector.visemeActive));
    bus.connect(detector);
  }
  function release() {
    if (!node) return;
    try { bus.disconnect(node); } catch {}
    node.port.close();
    node.disconnect();
    node = null;
  }
  connect().catch(error => {
    release();
    if (!closed) onError(error);
  });
  return () => { closed = true; release(); };
}
