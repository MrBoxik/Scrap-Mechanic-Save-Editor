(() => {
  "use strict";

  const canvas = document.querySelector("#capture");
  let running = false;
  let lastProgress = 0;
  let pendingProgress = null;
  let activeJobId = "";

  async function api(method, payload = {}) {
    const result = await window.pywebview.api.request(method, payload);
    if (!result?.ok) throw new Error(result?.error || `Map renderer request failed: ${method}`);
    return result;
  }

  function report(message, progress) {
    pendingProgress = { message: String(message || "Rendering the real 3D map…"), progress: Number(progress) || 0 };
    const now = performance.now();
    if (now - lastProgress < 180) return;
    lastProgress = now;
    const payload = pendingProgress;
    pendingProgress = null;
    api("companion-progress", {
      ...payload,
      id: activeJobId,
      busy: true,
      mapReady: false,
    }).catch(() => {});
  }

  async function renderJob(job) {
    const claimed = await api("capture-claim", { id: job.id });
    if (!claimed.claimed) return;
    activeJobId = job.id;
    let renderer = null;
    let stored = false;
    try {
      if (job.finalizeOnly) {
        stored = true;
        await api("capture-complete", { id: job.id });
        return;
      }
      renderer = new window.ScrapMap3D(canvas, api, report);
      renderer.textureBudget = 1024 * 1024 * 1024;
      await renderer.captureWorldTopDownChunked(job.width, job.height, {
        chunkCells: 32,
        detailScale: Number(job.detailScale) || 2,
        onOverviewTile: tile => api("store-3d-map-tile", {
          ...tile,
          maxSide: Number(job.maxSide) || 8192,
          variant: String(job.variant || "companion-hq-v2"),
        }),
        onDetailTile: tile => api("store-3d-map-tile", {
          ...tile,
          maxSide: Number(job.maxSide) || 8192,
          variant: String(job.variant || "companion-hq-v2"),
        }),
      });
      report("Assembling the real 3D-to-2D PNG outside the browser…", .998);
      await api("finalize-3d-map-tiles", {
        // The producer and completion validator must address the exact same
        // generated-map cache entry.  Hard-coding the retired 4096px cache
        // here let rendering reach 100%, then made capture-complete look for
        // a different 8192px file and remove both map surfaces as failed.
        maxSide: Number(job.maxSide) || 8192,
        variant: String(job.variant || "companion-hq-v2"),
      });
      stored = true;
      await api("capture-complete", { id: job.id });
    } catch (error) {
      await api("capture-failed", {
        id: job.id,
        error: error?.message || String(error),
        stored,
      }).catch(() => {});
    } finally {
      renderer?.dispose();
      if (activeJobId === job.id) activeJobId = "";
    }
  }

  async function poll() {
    if (running) return;
    running = true;
    try {
      const result = await api("capture-job");
      if (result.available && result.job) await renderJob(result.job);
    } catch (_) {
      // The overlay may still be finding the active save. The next bounded
      // poll retries without exposing an incomplete image.
    } finally {
      running = false;
    }
  }

  function boot() {
    poll();
    setInterval(poll, 1000);
  }

  if (window.pywebview?.api) boot();
  else window.addEventListener("pywebviewready", boot, { once: true });
})();
