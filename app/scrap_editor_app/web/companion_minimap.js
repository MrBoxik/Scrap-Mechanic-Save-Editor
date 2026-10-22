(() => {
  "use strict";

  const canvas = document.querySelector("#minimap");
  const playerCanvas = document.querySelector("#playerLayer");
  const main = document.querySelector("main");
  const status = document.querySelector("#mapStatus");
  const buildStatus = document.querySelector("#buildStatus");
  const progressBar = document.querySelector("#progressBar");
  const compass = document.querySelector("#compass");
  const mapKey = document.querySelector("#mapKey");
  const context = canvas.getContext("2d", { alpha: true, willReadFrequently: true });
  const playerContext = playerCanvas.getContext("2d", { alpha: true, willReadFrequently: true });
  const fogCanvas = document.createElement("canvas");
  const fogContext = fogCanvas.getContext("2d", { alpha: true });
  const worldFogCanvas = document.createElement("canvas");
  const worldFogContext = worldFogCanvas.getContext("2d", { alpha: true });
  const nativeCanvas = document.createElement("canvas");
  const nativeContext = nativeCanvas.getContext("2d", {
    alpha: true,
    willReadFrequently: true,
  });
  const mapImage = new Image();
  mapImage.crossOrigin = "anonymous";
  let logicalSize = 226;
  let imageUri = "";
  let imageReady = false;
  let imageLoading = false;
  let imageRetryTimer = 0;
  let imageAttempts = 0;
  let newestContextRevision = 0;
  let fullStateRequest = null;
  let booted = false;
  let bootProbeInFlight = false;
  let nextBootProbe = 0;
  let baseState = { config: {}, overlay: {}, settings: {} };
  let currentLive = null;
  let targetLive = null;
  let currentHeading = null;
  let targetHeading = null;
  let targetReceivedAt = performance.now();
  let targetSampleUpdated = 0;
  let targetPositionChangedAt = 0;
  let targetHeadingChangedAt = 0;
  let targetPositionSampleUpdated = 0;
  let targetHeadingSampleUpdated = 0;
  let targetVelocity = { x: 0, y: 0, z: 0 };
  let targetAngularVelocity = 0;
  let worldFogSignature = "";
  let worldFogPointCount = -1;
  let worldFogCellCount = -1;
  let worldFogSampleKeys = new Set();
  let worldFogPendingSamples = [];
  let worldFogNeedsFullRebuild = true;
  let explorationIndexSignature = "";
  let explorationCellKeys = new Set();
  let explorationSampleKeys = new Set();
  let explorationSamplePositionKeys = new Set();
  let explorationDiscoveredKeys = new Set();
  let waypointDiscoveryCache = new Map();
  let staticWaypoints = [];
  let nativeFrameInFlight = false;
  let nativeSharedBuffer = null;
  let nativeSharedBytes = null;
  let nativeSharedWords = null;
  let nativeSharedWidth = 0;
  let nativeSharedHeight = 0;
  let nativeSignalInFlight = false;
  let pushedRenderPending = false;
  let swapRedBlue = null;
  let swapRedBlueBytes = null;
  let nextNativeFrameAt = 0;
  let lastFrame = performance.now();
  let nextLivePoll = Number.POSITIVE_INFINITY;
  let liveInFlight = false;
  let mapLayerDirty = true;
  let mapLayerTransform = { x: 0, y: 0, rotation: 0 };
  let activeWorldIdentity = "";
  let lastMapFrame = { x: NaN, y: NaN, z: NaN, heading: NaN, at: 0 };
  let newestLiveUpdated = 0;
  let newestLiveRank = 0;
  let exploration = { enabled: true, cells: [], points: [], samples: [], discovered: [], radius: 192, cellSize: 64 };
  const waypointImages = new Map();
  const waypointSymbols = window.CompanionWaypointSymbols;

  WebAssembly.instantiate(new Uint8Array([
    0,97,115,109,1,0,0,0,1,5,1,96,1,127,0,3,2,1,0,5,3,1,0,16,7,20,2,
    6,109,101,109,111,114,121,2,0,7,115,119,97,112,95,114,98,0,0,10,74,1,
    72,1,2,127,2,64,3,64,32,1,32,0,79,13,1,32,1,65,4,108,40,2,0,33,2,32,
    1,65,4,108,32,2,65,128,254,131,120,113,32,2,65,255,1,113,65,16,116,114,
    32,2,65,16,118,65,255,1,113,114,54,2,0,32,1,65,1,106,33,1,12,0,11,11,11,
  ])).then(({ instance }) => {
    swapRedBlue = instance.exports.swap_rb;
    swapRedBlueBytes = new Uint8Array(instance.exports.memory.buffer);
  }).catch(() => {});

  function finite(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  async function api(method, payload = {}) {
    const result = await window.pywebview.api.request(method, payload);
    if (!result?.ok) throw new Error(result?.error || "Minimap request failed");
    return result;
  }

  function apiWithin(method, payload = {}, timeoutMs = 750) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`${method} bridge timeout`)),
        timeoutMs,
      );
      api(method, payload).then(
        value => { clearTimeout(timer); resolve(value); },
        error => { clearTimeout(timer); reject(error); },
      );
    });
  }

  function applySettings(settings = {}) {
    baseState.settings = settings;
    logicalSize = Math.max(160, Math.min(window.innerWidth, window.innerHeight));
    const ratio = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
    canvas.width = Math.round(logicalSize * ratio);
    canvas.height = Math.round(logicalSize * ratio);
    playerCanvas.width = Math.round(logicalSize * ratio);
    playerCanvas.height = Math.round(logicalSize * ratio);
    fogCanvas.width = logicalSize;
    fogCanvas.height = logicalSize;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    playerContext.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";
    playerContext.imageSmoothingEnabled = true;
    playerContext.imageSmoothingQuality = "high";
    mapLayerDirty = true;
    main.className = `${settings.shape === "circle" ? "circle" : "square"} border-${settings.border || "mechanic"}`;
    compass.hidden = settings.showCompass === false || settings.orientation === "player-up";
    mapKey.hidden = settings.showKeyHint === false;
  }

  function loadMap(uri) {
    if (!uri) return;
    if (uri === imageUri && mapImage.complete && mapImage.naturalWidth > 0) {
      imageLoading = false;
      if (!imageReady) {
        imageReady = true;
        mapLayerDirty = true;
      }
      return;
    }
    // Decoding the 4096px cached map can legitimately take longer while the 3D
    // capture WebView is finishing. Restarting the same decoder every 1.5s
    // prevented it from ever completing and also starved live position calls.
    // Retry only after a real image error, never while this URI is in flight.
    if (uri === imageUri && imageLoading) return;
    if(uri!==imageUri){clearTimeout(imageRetryTimer);imageAttempts=0}
    imageUri = uri;
    imageReady = false;
    imageLoading = true;
    mapImage.onload = () => {
      clearTimeout(imageRetryTimer);imageAttempts=0;
      imageLoading = false;
      imageReady = mapImage.naturalWidth > 0;
      mapLayerDirty = true;
    };
    mapImage.onerror = () => {
      imageLoading = false;
      imageReady = false;
      mapLayerDirty = true;
      clearTimeout(imageRetryTimer);
      imageAttempts++;
      imageRetryTimer=setTimeout(()=>loadMap(imageUri),Math.min(15000,500*2**Math.min(5,imageAttempts-1)));
    };
    // Clearing src forces Chromium to leave a stalled decoder before retrying.
    if (mapImage.src) mapImage.removeAttribute("src");
    mapImage.src = imageAttempts ? `${uri}${uri.includes("?")?"&":"?"}retry=${imageAttempts}` : uri;
  }

  function explorationSignature(value = exploration) {
    const cells = Array.isArray(value.cells) ? value.cells : [];
    const points = Array.isArray(value.points) ? value.points : [];
    const samples = Array.isArray(value.samples) ? value.samples : [];
    const discovered = Array.isArray(value.discovered) ? value.discovered : [];
    const last = samples[samples.length - 1] || [];
    return [
      Number(value.revision) || 0,
      cells.length,
      points.length,
      samples.length,
      discovered.length,
      Number(last[0]) || 0,
      Number(last[1]) || 0,
      Number(last[2]) || 0,
    ].join(":");
  }

  function indexExploration(value, force = false) {
    const next = value || exploration;
    const signature = explorationSignature(next);
    if (!force && signature === explorationIndexSignature) return false;
    exploration = next;
    explorationIndexSignature = signature;
    explorationCellKeys = new Set((exploration.cells || []).map(item => `${Number(item?.[0])},${Number(item?.[1])}`));
    explorationSampleKeys = new Set((exploration.samples || []).map(item => `${Number(item?.[0])},${Number(item?.[1])},${Number(item?.[2])}`));
    explorationSamplePositionKeys = new Set((exploration.samples || []).map(item => `${Number(item?.[0])},${Number(item?.[1])}`));
    explorationDiscoveredKeys = new Set((exploration.discovered || []).map(String));
    waypointDiscoveryCache = new Map();
    worldFogPendingSamples = [];
    worldFogNeedsFullRebuild = true;
    mapLayerDirty = true;
    return true;
  }

  function applyFullState(state = {}) {
    if(Number(state.contextRevision||0)<newestContextRevision)return;
    newestContextRevision=Math.max(newestContextRevision,Number(state.contextRevision)||0);
    const previousConfig = baseState.config || {};
    const previousOverlay = baseState.overlay || {};
    const nextSettings = state.settings || baseState.settings || {};
    const settingsChanged = JSON.stringify(nextSettings) !== JSON.stringify(baseState.settings || {});
    baseState = { ...baseState, ...state };
    const nextConfig = baseState.config || {};
    const identity = `${nextConfig.sourcePath || nextConfig.sourceSave || ""}|${nextConfig.sourceSeed ?? ""}`;
    if(activeWorldIdentity && identity !== activeWorldIdentity){
      currentLive=null;targetLive=null;currentHeading=null;targetHeading=null;
      newestLiveUpdated=0;newestLiveRank=0;targetVelocity={x:0,y:0,z:0};targetAngularVelocity=0;
      staticWaypoints=[];lastMapFrame={x:NaN,y:NaN,z:NaN,heading:NaN,at:0};
      mapLayerTransform={x:0,y:0,rotation:0};mapLayerDirty=true;
      explorationIndexSignature="";worldFogSignature="";
    }
    activeWorldIdentity=identity;
    const nextOverlay = baseState.overlay || {};
    if (
      previousConfig.ready !== nextConfig.ready ||
      previousConfig.sourceSave !== nextConfig.sourceSave ||
      previousOverlay.mapReady !== nextOverlay.mapReady ||
      previousOverlay.message !== nextOverlay.message ||
      previousOverlay.progress !== nextOverlay.progress
    ) mapLayerDirty = true;
    if (settingsChanged) applySettings(nextSettings);
    if (state.exploration) indexExploration(state.exploration);
    const waypoints = state.live?.state?.waypoints;
    if (Array.isArray(waypoints)) {
      staticWaypoints = waypoints;
      mapLayerDirty = true;
    }
    loadMap(state.minimapImage || state.image || "");
    setTarget(state.live);
  }

  function drawDiamond(x, y) {
    context.save(); context.translate(x, y); context.rotate(Math.PI / 4);
    context.fillStyle = "#ffc42c"; context.strokeStyle = "#06171c"; context.lineWidth = 2;
    context.fillRect(-4, -4, 8, 8); context.strokeRect(-4, -4, 8, 8); context.restore();
  }

  function revealFactor(relativeAngle) {
    const forward = Math.max(0, Math.min(1, (1 + Math.cos(relativeAngle)) * .5));
    return .14 + .86 * Math.pow(forward, .65);
  }

  const FOG_SEAM_WORLD = 4;

  function directionalRevealContains(originX, originY, targetX, targetY, heading, radius) {
    const dx = targetX - originX, dy = targetY - originY;
    const distance = Math.hypot(dx, dy);
    if (distance <= radius * .14 + FOG_SEAM_WORLD) return true;
    if (!distance) return true;
    const forwardX = Math.sin(heading), forwardY = Math.cos(heading);
    const cosine = Math.max(-1, Math.min(1, (dx * forwardX + dy * forwardY) / distance));
    return distance <= radius * revealFactor(Math.acos(cosine)) + FOG_SEAM_WORLD;
  }

  function revealContains(originX, originY, targetX, targetY, heading, radius) {
    if (baseState.settings?.revealShape === "circle") {
      return Math.hypot(targetX - originX, targetY - originY) <= radius + FOG_SEAM_WORLD;
    }
    return directionalRevealContains(originX, originY, targetX, targetY, heading, radius);
  }

  function traceDirectionalReveal(target, x, y, radius, heading, seam = 0) {
    target.save(); target.translate(x, y); target.rotate(heading);
    for (let step = 0; step <= 48; step += 1) {
      const angle = -Math.PI + step / 48 * Math.PI * 2;
      const distance = radius * revealFactor(angle) + seam;
      const px = Math.sin(angle) * distance, py = -Math.cos(angle) * distance;
      if (!step) target.moveTo(px, py); else target.lineTo(px, py);
    }
    target.closePath(); target.restore();
  }

  function traceReveal(target, x, y, radius, heading, seam = 0) {
    if (baseState.settings?.revealShape !== "circle") {
      traceDirectionalReveal(target, x, y, radius, heading, seam);
      return;
    }
    target.moveTo(x + radius + seam, y);
    target.arc(x, y, radius + seam, 0, Math.PI * 2);
  }

  function drawPlayer(heading) {
    playerContext.save(); playerContext.translate(logicalSize / 2, logicalSize / 2); playerContext.rotate(heading);
    const cone = playerContext.createLinearGradient(0, -7, 0, -logicalSize * .38);
    cone.addColorStop(0, "rgba(255,196,44,.28)"); cone.addColorStop(1, "rgba(255,196,44,0)");
    playerContext.beginPath(); playerContext.moveTo(0, -4); playerContext.lineTo(-logicalSize * .17, -logicalSize * .38); playerContext.lineTo(logicalSize * .17, -logicalSize * .38); playerContext.closePath(); playerContext.fillStyle = cone; playerContext.fill();
    waypointSymbols?.drawCanvas?.(playerContext, "player", 0, 0, 15);
    playerContext.restore();
  }

  function cloneLive(live) {
    const state = live?.state || {}, p = state.position, d = state.direction;
    return p ? { ...live, state: { ...state, position: { x: finite(p.x), y: finite(p.y), z: finite(p.z) }, direction: { x: finite(d?.x), y: finite(d?.y, 1), z: finite(d?.z) } } } : live;
  }

  function headingOf(live) {
    const direction = live?.state?.direction;
    return Math.atan2(finite(direction?.x), finite(direction?.y, 1));
  }

  function shortestAngle(from, to) {
    return Math.atan2(Math.sin(to - from), Math.cos(to - from));
  }

  function setTarget(live) {
    const candidate = cloneLive(live);
    if (candidate?.state) {
      if (!Array.isArray(candidate.state.waypoints) && Array.isArray(targetLive?.state?.waypoints)) {
        candidate.state.waypoints = targetLive.state.waypoints;
      }
      if (!Array.isArray(candidate.state.discovered) && Array.isArray(targetLive?.state?.discovered)) {
        candidate.state.discovered = targetLive.state.discovered;
      }
    }
    const source = String(candidate?.state?.source || "");
    const rank = source === "game-memory" ? 3 : source === "game-telemetry" ? 2 : source === "save-snapshot" ? 1 : 0;
    const updated = Number(candidate?.updated) || 0;
    // Live memory samples and slower fallback sources share this function.
    // A delayed response must never overwrite newer memory with an autosave
    // snapshot and visually pull the player backwards.
    if (rank < newestLiveRank) return;
    if (rank === newestLiveRank && updated && updated + .001 < newestLiveUpdated) return;
    const now = performance.now();
    const previousPosition = targetLive?.state?.position;
    const nextPosition = candidate?.state?.position;
    const previousHeading = targetHeading;
    const nextHeading = headingOf(candidate);
    const sourceChanged = Boolean(targetLive?.state?.source && source !== targetLive.state.source);
    if (nextPosition && previousPosition && !sourceChanged) {
      const dx = nextPosition.x - previousPosition.x;
      const dy = nextPosition.y - previousPosition.y;
      const dz = nextPosition.z - previousPosition.z;
      const moved = Math.hypot(dx, dy, dz);
      const headingDelta = previousHeading === null ? 0 : shortestAngle(previousHeading, nextHeading);
      // The memory sampler runs faster than some of the game's transform
      // producers. Several consecutive bridge responses can therefore carry
      // the same transform. Treating each duplicate as a zero-velocity sample
      // repeatedly stopped the marker and arrow between genuine game frames.
      // Measure velocity only between samples that actually changed; the
      // render loop below decays prediction naturally once movement stops.
      if (moved > 1e-4) {
        const positionSampleMs = targetPositionSampleUpdated && updated > targetPositionSampleUpdated
          ? (updated - targetPositionSampleUpdated) * 1000
          : targetPositionChangedAt ? now - targetPositionChangedAt : 0;
        if (positionSampleMs >= 4 && positionSampleMs <= 250 && moved <= 96) {
          const blend = .72;
          targetVelocity.x += (dx / positionSampleMs - targetVelocity.x) * blend;
          targetVelocity.y += (dy / positionSampleMs - targetVelocity.y) * blend;
          targetVelocity.z += (dz / positionSampleMs - targetVelocity.z) * blend;
        } else {
          targetVelocity = { x: 0, y: 0, z: 0 };
        }
        targetPositionChangedAt = now;
        targetPositionSampleUpdated = updated;
      }
      if (Math.abs(headingDelta) > 1e-5) {
        const headingSampleMs = targetHeadingSampleUpdated && updated > targetHeadingSampleUpdated
          ? (updated - targetHeadingSampleUpdated) * 1000
          : targetHeadingChangedAt ? now - targetHeadingChangedAt : 0;
        if (headingSampleMs >= 4 && headingSampleMs <= 250) {
          const angular = headingDelta / headingSampleMs;
          targetAngularVelocity += (angular - targetAngularVelocity) * .72;
        } else {
          targetAngularVelocity = 0;
        }
        targetHeadingChangedAt = now;
        targetHeadingSampleUpdated = updated;
      }
    } else if (sourceChanged) {
      targetVelocity = { x: 0, y: 0, z: 0 };
      targetAngularVelocity = 0;
      targetPositionChangedAt = now;
      targetHeadingChangedAt = now;
      targetPositionSampleUpdated = updated;
      targetHeadingSampleUpdated = updated;
    }
    if (updated) newestLiveUpdated = Math.max(newestLiveUpdated, updated);
    newestLiveRank = Math.max(newestLiveRank, rank);
    targetLive = candidate;
    targetHeading = nextHeading;
    if (updated > targetSampleUpdated || !targetSampleUpdated) {
      targetSampleUpdated = updated;
      targetReceivedAt = now;
    }
    const cell = candidate?.state?.explorationCell;
    if (Array.isArray(cell) && cell.length >= 2) {
      const key = `${Number(cell[0])},${Number(cell[1])}`;
      if (!explorationCellKeys.has(key)) {
        explorationCellKeys.add(key);
        if (!Array.isArray(exploration.cells)) exploration.cells = [];
        exploration.cells.push([Number(cell[0]), Number(cell[1])]);
        mapLayerDirty = true;
      }
    }
    const sample = candidate?.state?.explorationSample;
    if (Array.isArray(sample) && sample.length >= 3) {
      const normalized = [Number(sample[0]), Number(sample[1]), Number(sample[2])];
      const key = normalized.join(",");
      if (!explorationSampleKeys.has(key)) {
        explorationSampleKeys.add(key);
        explorationSamplePositionKeys.add(`${normalized[0]},${normalized[1]}`);
        // Exploration histories can contain tens of thousands of samples.
        // Spreading the whole array for every four-metre/six-degree update was
        // O(history) work on the render thread and produced the intermittent
        // walking/turning freezes.  This state is private to this page, so a
        // constant-time append is both safe and substantially cheaper.
        if (!Array.isArray(exploration.samples)) exploration.samples = [];
        exploration.samples.push(normalized);
        worldFogPendingSamples.push(normalized);
        mapLayerDirty = true;
      }
    }
    if (Array.isArray(candidate?.state?.discovered)) {
      let changed = false;
      for (const key of candidate.state.discovered) {
        const normalized = String(key);
        if (!explorationDiscoveredKeys.has(normalized)) {
          explorationDiscoveredKeys.add(normalized);
          changed = true;
        }
      }
      if (changed) {
        exploration.discovered = [...explorationDiscoveredKeys];
        waypointDiscoveryCache.clear();
        mapLayerDirty = true;
      }
    }
    if (!currentLive?.state?.position && targetLive?.state?.position) {
      currentLive = cloneLive(targetLive);
      currentHeading = targetHeading;
    }
  }

  function smoothLive(dt, now) {
    if (!targetLive?.state?.position) return targetLive || currentLive || {};
    if (!currentLive?.state?.position) currentLive = cloneLive(targetLive);
    const positionAge = Math.max(0, Math.min(140, now - (targetPositionChangedAt || targetReceivedAt)));
    const headingAge = Math.max(0, Math.min(140, now - (targetHeadingChangedAt || targetReceivedAt)));
    const positionDecay = positionAge <= 55 ? 1 : Math.exp(-(positionAge - 55) / 24);
    const headingDecay = headingAge <= 55 ? 1 : Math.exp(-(headingAge - 55) / 24);
    const amount = 1 - Math.exp(-Math.max(1, dt) / 15);
    const targetPosition = targetLive.state.position;
    const distance = Math.hypot(
      targetPosition.x - currentLive.state.position.x,
      targetPosition.y - currentLive.state.position.y,
      targetPosition.z - currentLive.state.position.z,
    );
    for (const key of ["x", "y", "z"]) {
      const predicted = targetPosition[key] + targetVelocity[key] * Math.min(positionAge, 55) * positionDecay;
      if (distance > 96) currentLive.state.position[key] = predicted;
      else currentLive.state.position[key] += (predicted - currentLive.state.position[key]) * amount;
    }
    if (currentHeading === null) currentHeading = headingOf(currentLive);
    if (targetHeading === null) targetHeading = headingOf(targetLive);
    const predictedHeading = targetHeading + targetAngularVelocity * Math.min(headingAge, 55) * headingDecay;
    currentHeading += shortestAngle(currentHeading, predictedHeading) * amount;
    currentLive.state.direction = {
      x: Math.sin(currentHeading),
      y: Math.cos(currentHeading),
      z: finite(targetLive.state.direction?.z),
    };
    currentLive.available = targetLive.available; currentLive.age = targetLive.age; currentLive.updated = targetLive.updated;
    currentLive.state.source = targetLive.state.source;
    if (Array.isArray(targetLive.state.waypoints)) currentLive.state.waypoints = targetLive.state.waypoints;
    if (Array.isArray(targetLive.state.discovered)) currentLive.state.discovered = targetLive.state.discovered;
    return currentLive;
  }

  function waypointEnabled(category) {
    return baseState.settings?.waypointFilters?.[category]?.minimap !== false;
  }

  function waypointDiscovered(waypoint) {
    if (waypoint.category === "beacons" || exploration.enabled === false) return true;
    const cacheKey = String(waypoint.key);
    if (explorationDiscoveredKeys.has(cacheKey)) return true;
    if (waypointDiscoveryCache.has(cacheKey)) return waypointDiscoveryCache.get(cacheKey);
    const radius = Number(exploration.radius || baseState.settings?.fogRadius) || 192;
    const worldX = Number(waypoint.worldX ?? Number(waypoint.x) * 64);
    const worldY = Number(waypoint.worldY ?? Number(waypoint.y) * 64);
    const samples = Array.isArray(exploration.samples) ? exploration.samples : [];
    for (const sample of samples) {
      if (!Array.isArray(sample) || sample.length < 3) continue;
      if (revealContains(Number(sample[0]), Number(sample[1]), worldX, worldY, Number(sample[2]) * Math.PI / 180, radius)) {
        waypointDiscoveryCache.set(cacheKey, true);
        return true;
      }
    }
    for (const point of exploration.points || []) {
      if (explorationSamplePositionKeys.has(`${Number(point?.[0])},${Number(point?.[1])}`)) continue;
      if (Array.isArray(point) && Math.hypot(worldX - Number(point[0]), worldY - Number(point[1])) <= radius) {
        waypointDiscoveryCache.set(cacheKey, true);
        return true;
      }
    }
    if (samples.length || (exploration.points || []).length) {
      waypointDiscoveryCache.set(cacheKey, false);
      return false;
    }
    const cellSize = Number(exploration.cellSize) || 64;
    for (const cell of exploration.cells || []) {
      if (!Array.isArray(cell)) continue;
      if (Math.hypot(worldX - (Number(cell[0]) + .5) * cellSize, worldY - (Number(cell[1]) + .5) * cellSize) <= radius) {
        waypointDiscoveryCache.set(cacheKey, true);
        return true;
      }
    }
    waypointDiscoveryCache.set(cacheKey, false);
    return false;
  }

  function cachedImage(uri) {
    if (!uri) return null;
    if (!waypointImages.has(uri)) {
      const next = new Image(); next.decoding = "async"; next.src = uri; waypointImages.set(uri, next);
    }
    return waypointImages.get(uri);
  }

  function drawWaypointIcon(waypoint, x, y, size) {
    const category = waypoint.category || "landmarks";
    const beaconColors = waypoint.beaconAtlas?.colors || [];
    const markerColor = category === "beacons"
      ? (beaconColors[Math.max(0, Number(waypoint.colorIndex || 1) - 1)] || "#ffc42c")
      : (waypointSymbols?.entry(category)?.color || "#70df70");
    context.save();
    context.translate(x, y);
    context.beginPath(); context.arc(0, 0, size, 0, Math.PI * 2);
    context.fillStyle = "#102329"; context.fill();
    if (category === "beacons" && waypoint.beaconAtlas?.atlas) {
      // Draw canonical vector artwork first. The game atlas replaces it once
      // decoded, but a slow or missing local sprite can never leave a black
      // dot behind.
      waypointSymbols?.drawCanvas?.(context, "beacons", 0, 0, size * .62);
      context.save();
      context.beginPath(); context.arc(0, 0, Math.max(1, size - 1), 0, Math.PI * 2); context.clip();
      const atlas = cachedImage(waypoint.beaconAtlas.atlas);
      if (atlas?.complete && atlas.naturalWidth) {
        const frame = Number(waypoint.beaconAtlas.frame) || 132;
        const stride = frame + (Number(waypoint.beaconAtlas.gap) || 12);
        const inset = size * .82;
        context.drawImage(atlas, Number(waypoint.iconIndex || 0) * stride, 0, frame, frame, -inset, -inset, inset * 2, inset * 2);
      }
      context.restore();
    } else if (waypointSymbols?.drawCanvas) {
      waypointSymbols.drawCanvas(context, category, 0, 0, size * .72);
    } else {
      // Never let an optional icon asset disable the animation loop.
      context.fillStyle = markerColor;
      context.beginPath(); context.moveTo(0, -size * .65); context.lineTo(size * .58, size * .45);
      context.lineTo(-size * .58, size * .45); context.closePath(); context.fill();
    }
    context.beginPath(); context.arc(0, 0, Math.max(1, size - 1), 0, Math.PI * 2);
    context.strokeStyle = markerColor;
    context.lineWidth = Math.max(1.5, size * .2); context.stroke();
    context.restore();
  }

  function rebuildWorldFog(config) {
    const minX = Number(config.worldMinX), maxX = Number(config.worldMaxX);
    const minY = Number(config.worldMinY), maxY = Number(config.worldMaxY);
    const worldWidth = Math.max(1, maxX - minX), worldHeight = Math.max(1, maxY - minY);
    const samples = Array.isArray(exploration.samples) ? exploration.samples : [];
    const points = Array.isArray(exploration.points) ? exploration.points : [];
    const cells = Array.isArray(exploration.cells) ? exploration.cells : [];
    const legacyCellCount = !samples.length && !points.length ? cells.length : 0;
    const signature = [
      config.sourceSave || "", Number(config.sourceSeed || 0),
      Number(exploration.radius || baseState.settings?.fogRadius || 192),
      baseState.settings?.revealShape || "directional",
      minX, maxX, minY, maxY,
    ].join(":");
    const width = 2048;
    const height = Math.max(1, Math.round(width * worldHeight / worldWidth));
    const fullRebuild = worldFogNeedsFullRebuild || (
      signature !== worldFogSignature
      || worldFogCanvas.width !== width
      || worldFogCanvas.height !== height
      || samples.length < worldFogSampleKeys.size
      || points.length !== worldFogPointCount
      || legacyCellCount !== worldFogCellCount
    );
    if (worldFogCanvas.width !== width || worldFogCanvas.height !== height) {
      worldFogCanvas.width = width;
      worldFogCanvas.height = height;
    }
    const scale = width / worldWidth;
    const radius = (Number(exploration.radius || baseState.settings?.fogRadius) || 192) * scale;
    const seam = Math.max(1, FOG_SEAM_WORLD * scale);
    const mapPoint = (x, y) => ({
      x: (Number(x) - minX) / worldWidth * width,
      y: (maxY - Number(y)) / worldHeight * height,
    });
    if (fullRebuild) {
      worldFogNeedsFullRebuild = false;
      worldFogSignature = signature;
      worldFogPointCount = points.length;
      worldFogCellCount = legacyCellCount;
      worldFogSampleKeys = new Set();
      worldFogContext.setTransform(1, 0, 0, 1, 0, 0);
      worldFogContext.globalCompositeOperation = "source-over";
      worldFogContext.clearRect(0, 0, width, height);
      worldFogContext.fillStyle = "#000";
      worldFogContext.fillRect(0, 0, width, height);
    }
    const pendingSamples = fullRebuild ? samples : worldFogPendingSamples.splice(0);
    for (const sample of pendingSamples) {
      if (!Array.isArray(sample) || sample.length < 3) continue;
      worldFogSampleKeys.add(`${Number(sample[0])},${Number(sample[1])},${Number(sample[2])}`);
    }
    if (!fullRebuild && !pendingSamples.length) return;
    worldFogContext.globalCompositeOperation = "destination-out";
    worldFogContext.beginPath();
    for (const sample of pendingSamples) {
      const point = mapPoint(sample[0], sample[1]);
      traceReveal(worldFogContext, point.x, point.y, radius, Number(sample[2]) * Math.PI / 180, seam);
    }
    if (fullRebuild) {
      const sampledPositions = new Set(samples.map(sample => `${Number(sample?.[0])},${Number(sample?.[1])}`));
      for (const point of points) {
        if (!Array.isArray(point) || point.length < 2) continue;
        if (sampledPositions.has(`${Number(point[0])},${Number(point[1])}`)) continue;
        const mapped = mapPoint(point[0], point[1]);
        worldFogContext.moveTo(mapped.x + radius + seam, mapped.y);
        worldFogContext.arc(mapped.x, mapped.y, radius + seam, 0, Math.PI * 2);
      }
      if (!samples.length && !points.length) {
        const cellSize = Number(exploration.cellSize) || 64;
        for (const cell of cells) {
          if (!Array.isArray(cell) || cell.length < 2) continue;
          const mapped = mapPoint((Number(cell[0]) + .5) * cellSize, (Number(cell[1]) + .5) * cellSize);
          worldFogContext.moveTo(mapped.x + radius + seam, mapped.y);
          worldFogContext.arc(mapped.x, mapped.y, radius + seam, 0, Math.PI * 2);
        }
      }
    }
    worldFogContext.fillStyle = "#000";
    worldFogContext.fill();
    worldFogContext.globalCompositeOperation = "source-over";
  }

  function drawFog(position, pixelsPerWorld, heading, playerUp) {
    if (baseState.settings?.fogOfWar === false || exploration.enabled === false) return;
    const config = baseState.config || {};
    rebuildWorldFog(config);
    const minX = Number(config.worldMinX), maxX = Number(config.worldMaxX);
    const minY = Number(config.worldMinY), maxY = Number(config.worldMaxY);
    const worldWidth = Math.max(1, maxX - minX), worldHeight = Math.max(1, maxY - minY);
    const renderedWidth = worldWidth * pixelsPerWorld, renderedHeight = worldHeight * pixelsPerWorld;
    const playerX = (Number(position.x) - minX) / worldWidth * renderedWidth;
    const playerY = (maxY - Number(position.y)) / worldHeight * renderedHeight;
    fogContext.setTransform(1,0,0,1,0,0);
    fogContext.clearRect(0,0,logicalSize,logicalSize);
    fogContext.save();
    fogContext.translate(logicalSize / 2, logicalSize / 2);
    if (playerUp) fogContext.rotate(-heading);
    fogContext.translate(-logicalSize / 2, -logicalSize / 2);
    fogContext.drawImage(worldFogCanvas, logicalSize / 2 - playerX, logicalSize / 2 - playerY, renderedWidth, renderedHeight);
    fogContext.restore();
    fogContext.globalCompositeOperation = "destination-out";
    fogContext.beginPath();
    const revealPixels = (Number(exploration.radius || baseState.settings?.fogRadius) || 192) * pixelsPerWorld;
    // The live reveal starts at the exact arrow centre and is forward-biased
    // in the same direction as the visible cone.
    traceReveal(fogContext, logicalSize / 2, logicalSize / 2, revealPixels, playerUp ? 0 : heading, Math.max(1, FOG_SEAM_WORLD * pixelsPerWorld));
    fogContext.fillStyle = "#000";
    fogContext.fill();
    fogContext.globalCompositeOperation = "source-over";
    context.drawImage(fogCanvas,0,0,logicalSize,logicalSize);
  }

  function drawWaypoints(player, pixelsPerWorld, heading, playerUp) {
    const edge = logicalSize / 2 - 18;
    const visibleRadiusWorld = edge / pixelsPerWorld;
    const points=[];
    for (const waypoint of staticWaypoints) {
      if (!waypointEnabled(waypoint.category)) continue;
      if (!waypointDiscovered(waypoint)) continue;
      const worldX = Number(waypoint.worldX ?? Number(waypoint.x) * 64);
      const worldY = Number(waypoint.worldY ?? Number(waypoint.y) * 64);
      let dx = (worldX - Number(player.position.x)) * pixelsPerWorld;
      let dy = -(worldY - Number(player.position.y)) * pixelsPerWorld;
      if (playerUp) {
        const cos = Math.cos(-heading), sin = Math.sin(-heading);
        [dx, dy] = [cos * dx - sin * dy, sin * dx + cos * dy];
      }
      const screenDistance = Math.hypot(dx, dy);
      const worldDistance = Math.hypot(worldX - Number(player.position.x), worldY - Number(player.position.y));
      const clipped=waypointSymbols.edgePoint(dx,dy,edge,baseState.settings?.shape);
      let x = logicalSize / 2 + clipped.x, y = logicalSize / 2 + clipped.y, size = 10.5;
      if (clipped.offscreen) {
        const closeness = Math.max(0, Math.min(1, visibleRadiusWorld / Math.max(visibleRadiusWorld, worldDistance)));
        size = 7 + 3 * closeness;
      }
      if(Math.hypot(x-logicalSize/2,y-logicalSize/2)<20)continue;
      points.push({x,y,waypoint,size});
    }
    for(const group of waypointSymbols.layout(points,18)){
      const item=group.points[0];drawWaypointIcon(item.waypoint,group.x,group.y,item.size);
      if(group.points.length>1){context.fillStyle="#f4f6f7";context.strokeStyle="#102329";context.lineWidth=3;context.font="700 9px Arial";context.textAlign="center";context.textBaseline="middle";const x=group.x+item.size*.7,y=group.y-item.size*.7;context.strokeText(String(group.points.length),x,y);context.fillText(String(group.points.length),x,y)}
    }
  }

  function draw(live) {
    // The compositor may translate/rotate the completed map between bounded
    // repaints.  A fresh render already contains the current transform.
    canvas.style.transform = "translate3d(0,0,0)";
    mapLayerTransform = { x: 0, y: 0, rotation: 0 };
    const config = baseState.config || {}, player = live?.state || {}, overlay = baseState.overlay || {}, settings = baseState.settings || {};
    context.clearRect(0, 0, logicalSize, logicalSize); context.fillStyle = "#0a2228"; context.fillRect(0, 0, logicalSize, logicalSize);
    const position = player.position, mapReady = Boolean(overlay.mapReady && config.ready && imageReady);
    if (mapReady) {
      const minX=Number(config.worldMinX),maxX=Number(config.worldMaxX),minY=Number(config.worldMinY),maxY=Number(config.worldMaxY),worldWidth=Math.max(1,maxX-minX),worldHeight=Math.max(1,maxY-minY);
      if (position) {
        const visibleWorldSpan=Math.max(320,Number(settings.worldSpan)||720),pixelsPerWorld=logicalSize/visibleWorldSpan,renderedWidth=worldWidth*pixelsPerWorld,renderedHeight=worldHeight*pixelsPerWorld;
        const playerX=(Number(position.x)-minX)/worldWidth*renderedWidth,playerY=(maxY-Number(position.y))/worldHeight*renderedHeight;
        const heading=Math.atan2(finite(player.direction?.x),finite(player.direction?.y,1)),playerUp=settings.orientation==="player-up";
        context.save(); context.translate(logicalSize/2,logicalSize/2); if(playerUp)context.rotate(-heading); context.translate(-logicalSize/2,-logicalSize/2);
        context.drawImage(mapImage,logicalSize/2-playerX,logicalSize/2-playerY,renderedWidth,renderedHeight);
        context.restore();
        drawFog(position,pixelsPerWorld,heading,playerUp);
        drawWaypoints(player,pixelsPerWorld,heading,playerUp);
      } else {
        const scale=Math.min(logicalSize/mapImage.width,logicalSize/mapImage.height),width=mapImage.width*scale,height=mapImage.height*scale;
        context.drawImage(mapImage,(logicalSize-width)/2,(logicalSize-height)/2,width,height);
      }
      // The border already contains the M key badge. Do not obscure the
      // ready minimap with a second instruction or live-position warning.
      status.textContent="";progressBar.style.width="100%";buildStatus.classList.add("quiet");
    } else {
      context.strokeStyle="rgba(255,196,44,.18)";context.lineWidth=1;for(let offset=-logicalSize;offset<logicalSize*2;offset+=18){context.beginPath();context.moveTo(offset,0);context.lineTo(offset+logicalSize,logicalSize);context.stroke()}
      const amount=Math.max(0,Math.min(1,Number(overlay.progress)||0));status.textContent=overlay.mapReady&&!imageReady?(imageAttempts?"Map image unavailable · Retrying…":"Loading map image…"):overlay.message||(position?"Building this world's map…":"Finding the active world…");progressBar.style.width=`${amount*100}%`;buildStatus.classList.remove("quiet");
    }
  }

  function presentMapBetweenDraws(position, heading, playerUp) {
    mapLayerTransform = { x: 0, y: 0, rotation: 0 };
    if (!position || !Number.isFinite(lastMapFrame.x) || !Number.isFinite(lastMapFrame.heading)) {
      canvas.style.transform = "translate3d(0,0,0)";
      return;
    }
    const worldSpan = Math.max(320, Number(baseState.settings?.worldSpan) || 720);
    const pixelsPerWorld = logicalSize / worldSpan;
    const worldDx = (finite(position.x) - lastMapFrame.x) * pixelsPerWorld;
    const worldDy = -(finite(position.y) - lastMapFrame.y) * pixelsPerWorld;
    const headingDelta = playerUp ? shortestAngle(lastMapFrame.heading, heading) : 0;
    if (Math.hypot(worldDx, worldDy) > logicalSize * .22 || Math.abs(headingDelta) > .5) {
      mapLayerDirty = true;
      canvas.style.transform = "translate3d(0,0,0)";
      return;
    }

    // The canvas was rendered around the previous player transform.  Move it
    // as one GPU-composited layer until the next 60 Hz repaint, keeping map,
    // fog, and waypoints perfectly registered without repeatedly resampling
    // the large lossless source image on the WebView UI thread.
    const shiftX = -worldDx;
    const shiftY = -worldDy;
    const viewHeading = playerUp ? heading : 0;
    const cosine = Math.cos(-viewHeading), sine = Math.sin(-viewHeading);
    const translateX = cosine * shiftX - sine * shiftY;
    const translateY = sine * shiftX + cosine * shiftY;
    const rotation = playerUp ? -headingDelta : 0;
    mapLayerTransform = { x: translateX, y: translateY, rotation };
    canvas.style.transform = `translate3d(${translateX}px,${translateY}px,0) rotate(${rotation}rad)`;
  }

  function traceRoundedRect(ctx, x, y, width, height, radius) {
    const r = Math.max(0, Math.min(radius, width / 2, height / 2));
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + width - r, y);
    ctx.quadraticCurveTo(x + width, y, x + width, y + r);
    ctx.lineTo(x + width, y + height - r);
    ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
    ctx.lineTo(x + r, y + height);
    ctx.quadraticCurveTo(x, y + height, x, y + height - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function drawNativeBuildStatus(ctx, settings) {
    const overlay = baseState.overlay || {};
    if (overlay.mapReady && baseState.config?.ready && imageReady) return;
    const circle = settings.shape === "circle";
    // Keep the whole status panel inside the circular window region. Drawing
    // close to the square canvas edge makes its corners look clipped even
    // though the native host itself is correctly elliptical.
    const x = circle ? Math.max(34, Math.round(logicalSize * .15)) : 12;
    const width = logicalSize - x * 2;
    const message = String(overlay.mapReady&&!imageReady?(imageAttempts?"Map image unavailable · Retrying…":"Loading map image…"):overlay.message || "Building this world's map...");
    const fontSize = circle ? 8 : 9;
    ctx.font = `800 ${fontSize}px Arial`;
    const maxTextWidth = width - 14;
    const lines = [];
    // Capture progress deliberately separates independent facts with a middle
    // dot. Keep those facts on their own rows, then word-wrap any long stage
    // name so section, scene and placement counts all remain visible.
    for (const part of message.split(/\s*[·|]\s*/).map(value => value.trim()).filter(Boolean)) {
      let line = "";
      for (const word of part.split(/\s+/)) {
        const candidate = line ? `${line} ${word}` : word;
        if (line && ctx.measureText(candidate).width > maxTextWidth) {
          lines.push(line);
          line = word;
        } else {
          line = candidate;
        }
      }
      if (line) lines.push(line);
    }
    if (!lines.length) lines.push("Building world map");
    const lineHeight = fontSize + 3;
    const height = Math.max(42, lines.length * lineHeight + 18);
    const y = circle
      ? Math.round((logicalSize - height) / 2 + 10)
      : logicalSize - height - 12;
    traceRoundedRect(ctx, x, y, width, height, 7);
    ctx.fillStyle = "rgba(5,14,17,.93)";
    ctx.fill();
    ctx.strokeStyle = "rgba(255,196,44,.42)";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#ffd053";
    for (let index = 0; index < lines.length; index += 1) {
      ctx.fillText(lines[index], logicalSize / 2, y + 8 + lineHeight * (index + .5));
    }
    const progress = Math.max(0, Math.min(1, Number(overlay.progress) || 0));
    const progressY = y + height - 8;
    ctx.fillStyle = "rgba(255,255,255,.16)";
    ctx.fillRect(x + 7, progressY, width - 14, 3);
    ctx.fillStyle = "#ffc42c";
    ctx.fillRect(x + 7, progressY, (width - 14) * progress, 3);
  }

  function publishNativeFrame(now) {
    if (baseState.overlay?.directRenderer || baseState.overlay?.nativeCapture || !baseState.overlay?.inGameWorld || nativeFrameInFlight) return;
    // A ready cached map must decode before it can advertise a drawable HUD,
    // otherwise its first frame is a misleading blank. A genuinely new world
    // has no ready config, however, and its build-progress frame must be
    // published immediately so generation never looks like a dead companion.
    const configuredHz = Math.max(10, Math.min(60, Number(baseState.settings?.updateHz) || 60));
    const nativeInterval = 1000 / configuredHz;
    if (nextNativeFrameAt > 0 && now + 2 < nextNativeFrameAt) return;
    const sharedTransportReady = Boolean(
      nativeSharedBytes && swapRedBlue && swapRedBlueBytes &&
      nativeSharedWidth === Math.max(1, Math.round(logicalSize)) &&
      nativeSharedHeight === Math.max(1, Math.round(logicalSize))
    );
    // A set flag means the WinForms host has not consumed the previous shared
    // frame yet. Drop this render without consuming its deadline; the next
    // 60 Hz live packet will retry. Falling through to JPEG/RPC here made every
    // small backpressure event block the renderer and cut the HUD to ~33 Hz.
    if (sharedTransportReady && nativeSharedWords[0] !== 0) return;
    if (!Number.isFinite(nextNativeFrameAt) || nextNativeFrameAt <= 0 || now - nextNativeFrameAt > nativeInterval * 4) {
      nextNativeFrameAt = now + nativeInterval;
    } else {
      do { nextNativeFrameAt += nativeInterval; } while (nextNativeFrameAt <= now);
    }
    const settings = baseState.settings || {};
    // The visible WinForms surface is sized in physical pixels. Encoding a
    // device-scale PNG only for PictureBox to shrink it again multiplied the
    // work and starved the live-state bridge.
    const ratio = 1;
    const pixels = Math.max(1, Math.round(logicalSize));
    if (nativeCanvas.width !== pixels || nativeCanvas.height !== pixels) {
      nativeCanvas.width = pixels;
      nativeCanvas.height = pixels;
    }
    nativeContext.setTransform(ratio, 0, 0, ratio, 0, 0);
    nativeContext.clearRect(0, 0, logicalSize, logicalSize);
    nativeContext.save();
    if (settings.shape === "circle") {
      nativeContext.beginPath();
      nativeContext.arc(logicalSize / 2, logicalSize / 2, logicalSize / 2, 0, Math.PI * 2);
      nativeContext.clip();
    } else {
      traceRoundedRect(nativeContext, 0, 0, logicalSize, logicalSize, settings.border === "none" ? 1 : 18);
      nativeContext.clip();
    }
    nativeContext.fillStyle = "#071b20";
    nativeContext.fillRect(0, 0, logicalSize, logicalSize);
    // drawImage does not include CSS transforms. Apply the same interpolated
    // motion to the native output so it cannot repeat every second map frame.
    nativeContext.save();
    nativeContext.translate(logicalSize/2+mapLayerTransform.x,logicalSize/2+mapLayerTransform.y);
    nativeContext.rotate(mapLayerTransform.rotation);
    nativeContext.drawImage(canvas, -logicalSize/2, -logicalSize/2, logicalSize, logicalSize);
    nativeContext.restore();
    nativeContext.drawImage(playerCanvas, 0, 0, logicalSize, logicalSize);
    nativeContext.restore();

    if (settings.border !== "none") {
      nativeContext.strokeStyle = settings.border === "compact" ? "#ffd45c" : "#ffc42c";
      nativeContext.lineWidth = settings.border === "compact" ? 2 : 3;
      if (settings.shape === "circle") {
        nativeContext.beginPath();
        nativeContext.arc(logicalSize / 2, logicalSize / 2, logicalSize / 2 - nativeContext.lineWidth / 2, 0, Math.PI * 2);
      } else {
        const inset = nativeContext.lineWidth / 2;
        traceRoundedRect(nativeContext, inset, inset, logicalSize - nativeContext.lineWidth, logicalSize - nativeContext.lineWidth, 18);
      }
      nativeContext.stroke();
    }
    if (settings.showCompass !== false && settings.orientation !== "player-up") {
      nativeContext.fillStyle = "#fff4c0";
      nativeContext.font = "900 9px Arial";
      nativeContext.textAlign = "center";
      nativeContext.textBaseline = "middle";
      nativeContext.fillText("N", logicalSize / 2, 10);
      nativeContext.fillText("S", logicalSize / 2, logicalSize - 10);
      nativeContext.fillText("W", 10, logicalSize / 2);
      nativeContext.fillText("E", logicalSize - 10, logicalSize / 2);
    }
    drawNativeBuildStatus(nativeContext, settings);
    const nativeMapReady = Boolean(baseState.overlay?.mapReady && baseState.config?.ready && imageReady);
    if (settings.showKeyHint !== false && nativeMapReady) {
      const x = logicalSize - (settings.shape === "circle" ? 65 : 37);
      const y = logicalSize - (settings.shape === "circle" ? 55 : 29);
      traceRoundedRect(nativeContext, x, y, 29, 21, 5);
      nativeContext.fillStyle = "rgba(5,14,17,.92)";
      nativeContext.fill();
      nativeContext.fillStyle = "#ffc42c";
      nativeContext.font = "900 11px Arial";
      nativeContext.textAlign = "center";
      nativeContext.textBaseline = "middle";
      nativeContext.fillText("M", x + 14.5, y + 10.5);
    }

    if (sharedTransportReady) {
      const rgba = nativeContext.getImageData(0, 0, pixels, pixels).data;
      swapRedBlueBytes.set(rgba, 0);
      swapRedBlue(pixels * pixels);
      // A shared WebView2 ArrayBuffer has high per-index write overhead. One
      // native typed-array copy follows the WebAssembly RGBA-to-BGRA pass.
      nativeSharedBytes.set(swapRedBlueBytes.subarray(0, rgba.length), 4);
      nativeSharedWords[0] = 1;
      // The WinForms timer remains a crash-safe fallback, but its effective
      // cadence aliases with a 55-60 Hz source. Nudge the native presenter as
      // soon as a ready-world frame lands. Coalescing prevents bridge queues.
      if (baseState.overlay?.mapReady && !nativeSignalInFlight) {
        nativeSignalInFlight = true;
        api("minimap-frame-signal")
          .catch(() => {})
          .finally(() => { nativeSignalInFlight = false; });
      }
      return;
    }

    nativeFrameInFlight = true;
    // JPEG is appropriate for this opaque, natively clipped HUD surface and
    // is substantially cheaper to encode/decode than a new lossless PNG for
    // every displayed frame. The editor's cached HD map remains lossless.
    nativeCanvas.toBlob(blob => {
      if (!blob) {
        nativeFrameInFlight = false;
        return;
      }
      const reader = new FileReader();
      reader.onerror = () => { nativeFrameInFlight = false; };
      reader.onload = () => {
        api("minimap-bitmap", { data: String(reader.result || "") })
          .catch(() => {})
          .finally(() => { nativeFrameInFlight = false; });
      };
      reader.readAsDataURL(blob);
    }, "image/jpeg", .92);
  }

  function refreshFull() {
    // apiWithin only times out its JavaScript wrapper; pywebview cannot cancel
    // the underlying request. During map generation that created an unbounded
    // queue of full fog/config snapshots. Keep one raw bridge request until it
    // actually settles so live transform packets always have a clear path.
    if (fullStateRequest) return fullStateRequest;
    const request = api("minimap-state")
      .then(state => {
        applyFullState(state);
        return state;
      })
      .catch(error => {
        status.textContent = "Map companion reconnecting…";
        progressBar.style.width = "0%";
        buildStatus.classList.remove("quiet");
        return null;
      })
      .finally(() => {
        if (fullStateRequest === request) fullStateRequest = null;
      });
    fullStateRequest = request;
    return request;
  }

  async function refreshLive() {
    if (liveInFlight) return;
    liveInFlight = true;
    try {
      const state=await api("minimap-live");
      newestContextRevision=Math.max(newestContextRevision,Number(state.contextRevision)||0);
      const priorOverlay=baseState.overlay||{};
      baseState.overlay=state.overlay||baseState.overlay;
      if(
        priorOverlay.mapReady!==baseState.overlay?.mapReady||
        priorOverlay.message!==baseState.overlay?.message||
        priorOverlay.progress!==baseState.overlay?.progress
      )mapLayerDirty=true;
      if(JSON.stringify(state.settings)!==JSON.stringify(baseState.settings))applySettings(state.settings||{});
      setTarget(state.live);
      // The WebView can boot before the active save snapshot has finished
      // loading. Static fog history and waypoint rows arrive through the
      // bounded full endpoint; refresh it exactly once when Python publishes
      // a new world context instead of retaining an empty startup snapshot.
      if(Number(state.contextRevision)!==Number(baseState.contextRevision))refreshFull().catch(()=>{});
      // WebView2 may throttle the minimap's background interval while its
      // native host is being attached.  A ready transition must therefore
      // also be detected by the active live loop, otherwise the progress
      // frame can remain visible until the user toggles the full map.
      if(state.overlay?.mapReady&&!baseState.config?.ready)refreshFull().catch(()=>{});
    }
    catch(_) {}
    // This remains a resilient fallback. The Python bridge normally pushes
    // memory samples directly, avoiding RPC contention with 3D map capture.
    // Pull the already-cached 40-byte transform at the requested HUD rate.
    // This runs on WebView2's own UI thread; Python no longer calls
    // evaluate_js from a worker, which could kill the entire map companion.
    finally {
      liveInFlight = false;
    }
  }

  function frame(now) {
    // Queue the successor first.  A malformed optional waypoint or a single
    // oversized fog rebuild must never permanently kill the HUD animation
    // loop; the next frame can recover after the bad input is skipped.
    requestAnimationFrame(frame);
    renderFrame(now);
  }

  function renderFrame(now) {
    try {
    // WebView2 can throttle or discard recursive timers on a focus-disabled
    // transparent window.  Poll from the visible animation clock instead so
    // every displayed frame has a continuously advancing live-state source.
    if(now>=nextLivePoll&&!liveInFlight&&!baseState.overlay?.livePush){
      // Schedule from request *start*, not completion. Adding the bridge's
      // 2–10 ms response time to every 33 ms interval lowered the effective
      // cadence to ~23 Hz and made steady turning less fluid than it used to
      // be. A fast response now sustains the configured input stream while the
      // independent player canvas still renders at the display refresh rate.
      const configuredHz=Math.max(10,Math.min(60,Number(baseState.settings?.updateHz)||60));
      // The live payload is deliberately tiny (only the cached player
      // transform), so honour the configured 60 Hz rate.  The old 30 Hz cap
      // produced a 38-40 ms bridge cadence after alignment to display frames;
      // that was visible as stepped turning even though interpolation itself
      // was running at the monitor refresh rate.
      const pullHz=configuredHz;
      const liveInterval=Math.max(4,1000/pullHz);
      // Preserve the sampler's phase instead of scheduling from this frame.
      // A 60 Hz deadline is ~16.67 ms while WebView2's animation frames on
      // this display arrive ~15 ms apart.  ``now + interval`` therefore
      // skipped every other frame and silently reduced the supposedly 60 Hz
      // HUD to roughly 33 Hz.  Advancing the existing deadline lets the
      // cadence alternate naturally across frames and average the requested
      // rate.  Only reset after a real suspension so we do not issue a burst
      // of catch-up bridge calls when Windows resumes the overlay.
      if(!Number.isFinite(nextLivePoll)||nextLivePoll<=0||now-nextLivePoll>liveInterval*4){
        nextLivePoll=now+liveInterval;
      }else{
        nextLivePoll+=liveInterval;
      }
      refreshLive();
    }
    const dt = now - lastFrame;
    lastFrame = now;
    const live = smoothLive(dt, now);
    const player = live?.state || {};
    const position = player.position;
    const heading = Math.atan2(finite(player.direction?.x), finite(player.direction?.y, 1));
    if (position) {
      const moved = Math.hypot(
        finite(position.x)-lastMapFrame.x,
        finite(position.y)-lastMapFrame.y,
        finite(position.z)-lastMapFrame.z,
      ) > .002;
      const turned = Math.abs(shortestAngle(lastMapFrame.heading, heading)) > .001;
      const playerUp = baseState.settings?.orientation === "player-up";
      const mapInterval = 1000 / 60;
      const repaintDue = now - lastMapFrame.at >= mapInterval;
      // A map-up turn changes only the independent player canvas.  During
      // movement, let the compositor advance the already-rendered map on every
      // display frame and bound the expensive map/fog/waypoint repaint to
      // 60 Hz.  This retains high-refresh smoothness without doing 180–200
      // large-canvas redraws per second on this machine.
      if (mapLayerDirty || repaintDue && (moved || playerUp && turned)) {
        draw(live);
        lastMapFrame = { x:finite(position.x), y:finite(position.y), z:finite(position.z), heading, at:now };
        mapLayerDirty = false;
      } else if (moved || playerUp && turned) {
        presentMapBetweenDraws(position, heading, playerUp);
      }
    } else if (mapLayerDirty) {
      draw(live);
      mapLayerDirty = false;
    }
    playerContext.clearRect(0, 0, logicalSize, logicalSize);
    if (player.position) {
      drawPlayer(baseState.settings?.orientation === "player-up" ? 0 : heading);
    }
    publishNativeFrame(now);
    } catch (error) {
      window.__minimapLastFrameError = {
        message: String(error?.message || error),
        stack: String(error?.stack || ""),
        at: performance.now(),
      };
    }
  }
  function boot(){if(booted)return;booted=true;indexExploration(exploration,true);applySettings({});refreshFull().catch(()=>{}).finally(()=>{nextLivePoll=0;});requestAnimationFrame(frame)}
  async function bootWhenBridgeReady(now=performance.now()){
    if(booted)return;
    if(!window.pywebview?.api||bootProbeInFlight||now<nextBootProbe){
      requestAnimationFrame(bootWhenBridgeReady);
      return;
    }
    // Merely seeing ``window.pywebview.api`` is not sufficient: WebView2 can
    // expose the proxy one frame before its message channel accepts calls.
    // Probe a compact endpoint first so the initial full-state request cannot
    // become the one never-settling promise that freezes the HUD forever.
    bootProbeInFlight=true;
    try{
      await apiWithin("minimap-live",{},500);
      boot();
      return;
    }catch(_){
      nextBootProbe=performance.now()+250;
    }finally{
      bootProbeInFlight=false;
    }
    requestAnimationFrame(bootWhenBridgeReady);
  }
  window.companionMinimapRefresh=refreshFull;
  window.companionMinimapState=applyFullState;
  window.companionMinimapLive=setTarget;
  window.chrome?.webview?.addEventListener("message", event => {
    if (event.data?.type === "minimap-bootstrap") {
      bootWhenBridgeReady(performance.now());
      return;
    }
    if (event.data?.type === "minimap-live" && event.data.live) {
      newestContextRevision=Math.max(newestContextRevision,Number(event.data.contextRevision)||0);
      // Native telemetry remains active for covered, non-activating WebViews.
      // Use every packet as a boot retry so startup can never wait for a click.
      if (!booted) bootWhenBridgeReady(performance.now());
      const priorOverlay = baseState.overlay || {};
      baseState.overlay = event.data.overlay || baseState.overlay;
      if (JSON.stringify(event.data.settings) !== JSON.stringify(baseState.settings)) {
        applySettings(event.data.settings || {});
      }
      setTarget(event.data.live);
      if (Number(event.data.contextRevision) !== Number(baseState.contextRevision)) {
        refreshFull().catch(() => {});
      }
      if (baseState.overlay?.mapReady && !baseState.config?.ready) {
        refreshFull().catch(() => {});
      }
      if (
        priorOverlay.mapReady !== baseState.overlay?.mapReady ||
        priorOverlay.message !== baseState.overlay?.message ||
        priorOverlay.progress !== baseState.overlay?.progress
      ) mapLayerDirty = true;
      // A covered, non-activating WebView can receive native messages at full
      // speed while Chromium clamps requestAnimationFrame to roughly 15 Hz.
      // Render on the fresh telemetry event as well; publishNativeFrame keeps
      // the shared output bounded by the user's configured refresh rate.
      if (!pushedRenderPending) {
        pushedRenderPending = true;
        setTimeout(() => {
          pushedRenderPending = false;
          renderFrame(performance.now());
        }, 0);
      }
    }
  });
  window.chrome?.webview?.addEventListener("sharedbufferreceived", event => {
    let metadata = event.additionalData || {};
    if (typeof metadata === "string") {
      try { metadata = JSON.parse(metadata); }
      catch (_) { return; }
    }
    if (metadata.type !== "minimap-frame-buffer") return;
    if (nativeSharedBuffer) window.chrome.webview.releaseBuffer(nativeSharedBuffer);
    nativeSharedBuffer = event.getBuffer();
    nativeSharedBytes = new Uint8Array(nativeSharedBuffer);
    nativeSharedWords = new Uint32Array(nativeSharedBuffer);
    nativeSharedWidth = Number(metadata.width) || 0;
    nativeSharedHeight = Number(metadata.height) || 0;
    nativeSharedWords[0] = 0;
  });
  setInterval(() => {
    // Native live pushes already carry build progress. Only request the heavy
    // static snapshot after Python says the generated map is ready; image
    // decode has its own onload/onerror lifecycle and must not trigger RPCs.
    if(baseState.overlay?.mapReady&&!baseState.config?.ready)refreshFull().catch(()=>{});
  },750);
  // Once ready, live-state packets carry every changing HUD field. Re-sending
  // the entire map config and thousands of fog-history samples every five
  // seconds caused the repeatable long freezes reported while walking. The
  // readiness poll above still performs a full refresh for a new/rebuilt map.
  // pywebview can dispatch ``pywebviewready`` before a deferred script at the
  // bottom of the document has attached its listener.  That race left the HUD
  // showing its first placeholder forever and made pressing M appear to wake
  // it.  Keep the event for the normal path and use the visible frame clock as
  // a race-proof fallback until the bridge object is actually installed.
  window.addEventListener("pywebviewready",boot,{once:true});
  bootWhenBridgeReady();
})();
