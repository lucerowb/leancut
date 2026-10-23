// LeanCut preview host, loaded first in a motion-graphics page shown in the editor's sandboxed iframe
// (sandbox="allow-scripts", no same-origin). The editor drives the page by postMessage:
//   {leancut: "frame", t, id} -> awaits film.ready, then film.frame(t) -> {leancut: "drawn", id, ok, error?}
//   {leancut: "meta", id}     -> {leancut: "meta", id, ok, duration, width, height, error?}
// Uncaught errors go up as {leancut: "error", error}. A frame request overtaken by a newer one while the page is
// busy drawing is answered {ok: false, skipped: true}, so scrubbing never queues up stale frames.
(() => {
  window.filmHost = { version: '0.1', name: 'leancut' }; // a page under a host never plays itself
  const up = (msg) => parent.postMessage(msg, '*'); // the parent's origin is unknown to an opaque-origin frame
  const why = (e) => String((e && (e.stack || e.message)) || e).split('\n')[0].slice(0, 300);

  let prepared;
  const prepare = () => prepared ??= (async () => {
    if (document.readyState !== 'complete') await new Promise((r) => addEventListener('load', r, { once: true }));
    const end = Date.now() + 10000;
    while (!(window.film && typeof window.film.frame === 'function') && Date.now() < end) {
      await new Promise((r) => setTimeout(r, 25));
    }
    if (!window.film || typeof window.film.frame !== 'function') throw new Error('window.film is missing: this page does not follow the page contract');
    const r = window.film.ready;
    await (typeof r === 'function' ? r() : r);
    await document.fonts.ready;
  })();

  let busy = false;
  let next = null;
  const drawLoop = async () => {
    busy = true;
    while (next) {
      const m = next;
      next = null;
      try {
        await prepare();
        await window.film.frame(Math.max(0, Number(m.t) || 0));
        up({ leancut: 'drawn', id: m.id, ok: true });
      } catch (e) {
        up({ leancut: 'drawn', id: m.id, ok: false, error: why(e) });
      }
    }
    busy = false;
  };

  addEventListener('message', async (e) => {
    if (e.source !== parent) return;
    const m = e.data || {};
    if (m.leancut === 'frame') {
      if (next) up({ leancut: 'drawn', id: next.id, ok: false, skipped: true });
      next = m;
      if (!busy) drawLoop();
    } else if (m.leancut === 'meta') {
      try {
        await prepare();
        const f = window.film;
        up({ leancut: 'meta', id: m.id, ok: true, duration: f.duration ?? null, width: f.width ?? innerWidth, height: f.height ?? innerHeight });
      } catch (err) {
        up({ leancut: 'meta', id: m.id, ok: false, error: why(err) });
      }
    }
  });
  addEventListener('error', (e) => up({ leancut: 'error', error: why(e.error || e.message) }));
  addEventListener('unhandledrejection', (e) => up({ leancut: 'error', error: why(e.reason) }));
})();
