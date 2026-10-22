(() => {
  "use strict";
  if (!new URLSearchParams(location.search).has("preview")) return;
  let state = null;
  const waiters = [];
  const ready = () => state ? Promise.resolve() : new Promise(resolve => waiters.push(resolve));
  window.pywebview = { api: { request: async (method, payload = {}) => {
    await ready();
    if (method === "minimap-state") return { ok: true, ...state };
    if (method === "minimap-live") return { ok: true, live: state.live, overlay: state.overlay, settings: state.settings, contextRevision: 1 };
    if (method === "activate-current-save") return { ok: true, pending: false, current: { fileName: "Map preview", game: { seed: 1 } }, save: { path: "preview" } };
    if (method === "map-features") return { ok: true, features: state.live.state.waypoints };
    if (method === "live-state") return { ok: true, ...state.live };
    if (method === "exploration-state") return { ok: true, exploration: state.exploration };
    if (method === "save-companion-settings") { state.settings = { ...state.settings, ...payload }; return { ok: true, settings: state.settings }; }
    if (method === "hide-overlay") parent.postMessage({ type: "companion-preview-close" }, "*");
    return { ok: true, settings: state.settings };
  } } };
  window.addEventListener("message", event => {
    if (event.source !== parent || event.data?.type !== "companion-preview-state") return;
    state = event.data.state;
    for (const resolve of waiters.splice(0)) resolve();
    window.companionMinimapState?.(state);
    window.companionSettingsUpdated?.();
  });
  window.addEventListener("keydown", event => {
    if (event.key.toLowerCase() === "m") {
      event.preventDefault();
      parent.postMessage({ type: document.querySelector("#minimap") ? "companion-preview-open" : "companion-preview-close" }, "*");
    }
  });
  parent.postMessage({ type: "companion-preview-ready" }, "*");
})();
