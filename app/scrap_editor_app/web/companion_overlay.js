(() => {
  "use strict";

  const viewport = document.querySelector("#mapViewport");
  const stage = document.querySelector("#mapStage");
  const image = document.querySelector("#mapImage");
  const detailTiles = document.querySelector("#detailTiles");
  const markers = document.querySelector("#markers");
  const fogOverlay = document.querySelector("#fogOverlay");
  const fogContext = fogOverlay.getContext("2d", { alpha: true });
  const legend = document.querySelector("#legend");
  const legendRows = document.querySelector("#legendRows");
  const status = document.querySelector("#status");
  const statusText = document.querySelector("#statusText");
  const progress = document.querySelector("#progress");
  const worldName = document.querySelector("#worldName");
  const zoomValue = document.querySelector("#zoomValue");
  const errorBox = document.querySelector("#error");
  const mapSidebar = document.querySelector("#mapSidebar");
  const waypointTooltip = document.querySelector("#waypointTooltip");
  const waypointSymbols = window.CompanionWaypointSymbols || {
    info: { landmarks: { label: "Landmarks", color: "#70df70" } },
    svg: () => '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2 22 12 12 22 2 12z"/></svg>',
  };
  const categoryInfo = waypointSymbols.info;
  let meta = null;
  let features = [];
  let featureNodes = new Map();
  let playerNode = null;
  let liveState = {};
  let displayedPlayerPosition = null;
  let displayedPlayerHeading = null;
  let targetPlayerHeading = null;
  let playerFrameTime = performance.now();
  let exploration = { enabled: true, cells: [], points: [], samples: [], discovered: [], revision: 0, radius: 192 };
  let settings = {};
  let building = false;
  let booted = false;
  let mapIdentity = "";
  let worldContextIdentity = "";
  let displayReady = false;
  let renderedExplorationRevision = -1;
  let fogIdentity = "";
  let fogSampleKeys = new Set();
  let fogPointCount = -1;
  let fogCellCount = -1;
  let view = { scale: 1, x: 0, y: 0, fit: 1 };
  let drag = null;
  let detailTileNodes = new Map();
  let detailTileTimer = 0;
  let detailInteractionTimer = 0;
  let interacting = false;
  let viewFrame = 0;
  let selectedFeature = null;
  let quickSaveTimer = 0;
  let liveRequest = false;
  let refreshRequested = false;
  let searchQuery = "";
  let pushedLive = null;
  let pushedLiveAt = 0;
  let lastLiveUpdated = 0;
  let pendingPlayerCenter = false;

  const FOG_SEAM_WORLD = 4;

  async function api(method, payload = {}) {
    const result = await window.pywebview.api.request(method, payload);
    if (!result?.ok) throw new Error(result?.error || "Map companion request failed");
    return result;
  }

  function setStatus(message, amount = 0, options = {}) {
    const mapReady = Boolean(options.mapReady);
    const shownAmount = mapReady ? 1 : Math.min(.97, Math.max(0, Number(amount) || 0));
    statusText.textContent = !mapReady && String(message).startsWith("Complete 3D world")
      ? "3D geometry assembled · preparing the final lossless map…" : message;
    progress.style.width = `${shownAmount * 100}%`;
    status.classList.toggle("ready", mapReady);
  }

  function applyView() {
    if (viewFrame) return;
    viewFrame = requestAnimationFrame(() => {
      viewFrame = 0;
      stage.style.transform = `translate3d(${view.x}px,${view.y}px,0) scale(${view.scale})`;
      updateMarkerScreenPositions();
      zoomValue.textContent = view.fit && Math.abs(view.scale - view.fit) < .001
        ? "Fit" : `${Math.round(view.scale / Math.max(.0001, view.fit) * 100)}%`;
      if (!interacting) scheduleDetailTiles();
    });
  }

  function setInteracting(active, settleDelay = 0) {
    clearTimeout(detailInteractionTimer);
    if (!active && settleDelay > 0) {
      detailInteractionTimer = setTimeout(() => setInteracting(false), settleDelay);
      return;
    }
    interacting = active;
    // Keep already decoded detail tiles resident during pan/zoom. Hiding the
    // whole layer forces WebView2 to recompose a huge overview and then flash
    // every tile back in, which is both slower and visually unstable.
    if (!active) scheduleDetailTiles(true);
  }

  function scheduleDetailTiles(immediate = false) {
    clearTimeout(detailTileTimer);
    if (immediate) updateDetailTiles();
    else detailTileTimer = setTimeout(updateDetailTiles, 170);
  }

  function clearDetailTiles() {
    for (const node of detailTileNodes.values()) node.remove();
    detailTileNodes = new Map();
  }

  function updateDetailTiles() {
    const pyramid = meta?.detailTiles;
    if (!pyramid?.ready || !Array.isArray(pyramid.tiles) || view.scale <= 0) {
      clearDetailTiles();
      return;
    }
    // The overview remains cheapest while fitted. Detail tiles become useful
    // once one overview source pixel is close to one screen pixel.
    const sourceDemand = view.scale * Number(pyramid.scale || 2);
    if (sourceDemand < 1) {
      clearDetailTiles();
      return;
    }
    const margin = 64 / view.scale;
    const left = -view.x / view.scale - margin;
    const top = -view.y / view.scale - margin;
    const right = (viewport.clientWidth - view.x) / view.scale + margin;
    const bottom = (viewport.clientHeight - view.y) / view.scale + margin;
    const wanted = new Set();
    for (const tile of pyramid.tiles) {
      const x = Number(tile.x) || 0, y = Number(tile.y) || 0;
      const width = Number(tile.displayWidth) || 0, height = Number(tile.displayHeight) || 0;
      if (x + width < left || x > right || y + height < top || y > bottom) continue;
      const key = `${tile.column}:${tile.row}`;
      wanted.add(key);
      let node = detailTileNodes.get(key);
      if (!node) {
        node = document.createElement("img");
        node.className = "detail-tile";
        node.alt = "";
        node.decoding = "async";
        node.draggable = false;
        node.addEventListener("load", () => node.classList.add("ready"), { once: true });
        node.style.left = `${x}px`;
        node.style.top = `${y}px`;
        node.style.width = `${width}px`;
        node.style.height = `${height}px`;
        node.src = String(tile.image || "");
        detailTiles.append(node);
        detailTileNodes.set(key, node);
      }
      node.hidden = false;
      node.dataset.lastUsed = String(performance.now());
    }
    const unused = [];
    for (const [key, node] of detailTileNodes) {
      if (wanted.has(key)) continue;
      node.hidden = true;
      unused.push([key, node]);
    }
    // Keep a small decoded LRU.  Panning back no longer destroys and
    // re-decodes every tile, while the cap prevents an 8K map from growing
    // into an unbounded GPU texture cache.
    unused.sort((a, b) => Number(a[1].dataset.lastUsed || 0) - Number(b[1].dataset.lastUsed || 0));
    const keep = Math.max(24, wanted.size + 12);
    while (detailTileNodes.size > keep && unused.length) {
      const [key, node] = unused.shift();
      node.removeAttribute("src");
      node.remove();
      detailTileNodes.delete(key);
    }
  }

  function fitMap() {
    if (!meta) return;
    const leftRoom = legend.hidden ? 24 : legend.getBoundingClientRect().right + 24;
    const rightRoom = mapSidebar.hidden ? 24 : viewport.clientWidth - mapSidebar.getBoundingClientRect().left + 24;
    const available = Math.max(200, viewport.clientWidth - leftRoom - rightRoom);
    view.fit = Math.min(available / meta.width, (viewport.clientHeight - 136) / meta.height);
    view.scale = view.fit;
    view.x = leftRoom + (available - meta.width * view.scale) / 2;
    view.y = 88 + (viewport.clientHeight - 112 - meta.height * view.scale) / 2;
    applyView();
  }

  function zoomAt(factor, clientX = viewport.clientWidth / 2, clientY = viewport.clientHeight / 2) {
    if (!meta) return;
    const old = view.scale;
    const next = Math.max(view.fit * .75, Math.min(view.fit * 32, old * factor));
    const localX = (clientX - view.x) / old;
    const localY = (clientY - view.y) / old;
    view.scale = next;
    view.x = clientX - localX * next;
    view.y = clientY - localY * next;
    applyView();
  }

  function mapPointWorld(x, y) {
    const width = Math.max(1, meta.worldMaxX - meta.worldMinX);
    const height = Math.max(1, meta.worldMaxY - meta.worldMinY);
    return {
      x: (Number(x) - meta.worldMinX) / width * meta.width,
      y: (meta.worldMaxY - Number(y)) / height * meta.height,
    };
  }

  function revealFactor(relativeAngle) {
    const forward = Math.max(0, Math.min(1, (1 + Math.cos(relativeAngle)) * .5));
    return .14 + .86 * Math.pow(forward, .65);
  }

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
    if (settings.revealShape === "circle") {
      return Math.hypot(targetX - originX, targetY - originY) <= radius + FOG_SEAM_WORLD;
    }
    return directionalRevealContains(originX, originY, targetX, targetY, heading, radius);
  }

  function traceDirectionalReveal(target, x, y, radius, heading, seam = 0) {
    target.save();
    target.translate(x, y);
    target.rotate(heading);
    for (let step = 0; step <= 64; step += 1) {
      const angle = -Math.PI + step / 64 * Math.PI * 2;
      const distance = radius * revealFactor(angle) + seam;
      const px = Math.sin(angle) * distance;
      const py = -Math.cos(angle) * distance;
      if (!step) target.moveTo(px, py); else target.lineTo(px, py);
    }
    target.closePath();
    target.restore();
  }

  function traceReveal(target, x, y, radius, heading, seam = 0) {
    if (settings.revealShape !== "circle") {
      traceDirectionalReveal(target, x, y, radius, heading, seam);
      return;
    }
    target.moveTo(x + radius + seam, y);
    target.arc(x, y, radius + seam, 0, Math.PI * 2);
  }

  function filterFor(category) {
    return settings.waypointFilters?.[category] || { fullMap: true, minimap: true };
  }

  function featureVisible(feature) {
    if (!filterFor(feature.category).fullMap) return false;
    if (!exploration.enabled || feature.category === "beacons") return true;
    if (new Set(exploration.discovered || []).has(String(feature.key))) return true;
    const radius = Number(exploration.radius) || 192;
    const worldX = Number(feature.worldX ?? Number(feature.x) * 64);
    const worldY = Number(feature.worldY ?? Number(feature.y) * 64);
    const samples = Array.isArray(exploration.samples) ? exploration.samples : [];
    for (const sample of samples) {
      if (!Array.isArray(sample) || sample.length < 3) continue;
      if (revealContains(Number(sample[0]), Number(sample[1]), worldX, worldY, Number(sample[2]) * Math.PI / 180, radius)) return true;
    }
    const sampledPositions = new Set(samples.map(sample => `${Number(sample?.[0])},${Number(sample?.[1])}`));
    for (const point of exploration.points || []) {
      if (sampledPositions.has(`${Number(point?.[0])},${Number(point?.[1])}`)) continue;
      if (Array.isArray(point) && Math.hypot(worldX - Number(point[0]), worldY - Number(point[1])) <= radius) return true;
    }
    if (samples.length || (exploration.points || []).length) return false;
    const cellSize = Number(exploration.cellSize) || 64;
    for (const cell of exploration.cells || []) {
      if (!Array.isArray(cell)) continue;
      if (Math.hypot(worldX - (Number(cell[0]) + .5) * cellSize, worldY - (Number(cell[1]) + .5) * cellSize) <= radius) return true;
    }
    return false;
  }

  function beaconColor(feature) {
    const colors = feature.beaconAtlas?.colors || [];
    return colors[Math.max(0, Number(feature.colorIndex || 1) - 1)] || "#ffc42c";
  }

  function makeMarker(feature) {
    const point = mapPointWorld(feature.worldX, feature.worldY);
    const node = document.createElement("button");
    node.type = "button";
    node.className = `marker ${feature.category || "landmarks"}`;
    node.dataset.mapX = String(point.x);
    node.dataset.mapY = String(point.y);
    node.style.borderColor = feature.category === "beacons"
      ? beaconColor(feature)
      : (categoryInfo[feature.category]?.color || "#fff");
    node.style.setProperty("--marker-color", feature.category === "beacons"
      ? beaconColor(feature) : (categoryInfo[feature.category]?.color || "#fff"));
    node.title = feature.label || "Map location";
    node.setAttribute("aria-label", feature.label || "Map location");
    node.setAttribute("role", "button");
    node.tabIndex = 0;
    node.append(waypointSymbols.icon(feature, 19));
    node.addEventListener("pointerdown", event => event.stopPropagation());
    node.addEventListener("pointerenter", event => showWaypointTooltip(event, feature));
    node.addEventListener("pointermove", positionWaypointTooltip);
    node.addEventListener("pointerleave", hideWaypointTooltip);
    node.addEventListener("click", event => { event.stopPropagation(); if(node.classList.contains("cluster")){zoomAt(2,Number.parseFloat(node.style.left),Number.parseFloat(node.style.top));return}selectWaypoint(feature);updateMarkerScreenPositions(); });
    node.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectWaypoint(feature); }
    });
    markers.append(node);
    positionScreenMarker(node);
    return node;
  }

  function waypointType(feature) {
    return categoryInfo[feature.category]?.label || feature.kind || "Map location";
  }

  function positionWaypointTooltip(event) {
    if (!waypointTooltip || waypointTooltip.hidden) return;
    const pad = 14, width = waypointTooltip.offsetWidth || 210, height = waypointTooltip.offsetHeight || 58;
    const x = Math.max(pad, Math.min(window.innerWidth - width - pad, event.clientX + 18));
    const y = Math.max(82, Math.min(window.innerHeight - height - pad, event.clientY + 16));
    waypointTooltip.style.left = `${x}px`;
    waypointTooltip.style.top = `${y}px`;
  }

  function showWaypointTooltip(event, feature) {
    waypointTooltip.querySelector("strong").textContent = feature.label || "Map location";
    waypointTooltip.querySelector("span").textContent = waypointType(feature);
    waypointTooltip.hidden = false;
    positionWaypointTooltip(event);
  }

  function hideWaypointTooltip() { waypointTooltip.hidden = true; }

  function selectWaypoint(feature) {
    selectedFeature = feature;
    const category = feature.category || "landmarks", info = categoryInfo[category] || categoryInfo.landmarks;
    const detail = document.querySelector("#waypointDetails");
    detail.classList.remove("empty");
    document.querySelector("#waypointDetailsIcon").innerHTML = waypointSymbols.svg(category);
    document.querySelector("#waypointDetailsIcon").style.setProperty("--waypoint-color", category === "beacons" ? beaconColor(feature) : info.color);
    document.querySelector("#waypointDetailsName").textContent = feature.label || "Map location";
    document.querySelector("#waypointDetailsType").textContent = waypointType(feature);
    document.querySelector("#waypointDetailsCell").textContent = `${Number(feature.x) || 0}, ${Number(feature.y) || 0}`;
    document.querySelector("#waypointDetailsWorld").textContent = `${Math.round(Number(feature.worldX) || 0)}, ${Math.round(Number(feature.worldY) || 0)}`;
    const position = liveState.position;
    document.querySelector("#waypointDetailsDistance").textContent = position ? `${Math.round(Math.hypot(feature.worldX-position.x,feature.worldY-position.y))} m` : "—";
    const preview = document.querySelector("#waypointDetailsPreview");
    preview.hidden = !feature.preview;
    preview.removeAttribute("src");
    if (feature.preview) {
      preview.alt = `${feature.label || "Map location"} tile preview`;
      preview.onerror = () => { preview.hidden = true; };
      preview.src = feature.preview;
    }
    for (const [key, node] of featureNodes) node.classList.toggle("selected", String(key) === String(feature.key));
  }

  function positionScreenMarker(node, heading = null) {
    if (!node) return;
    const mapX = Number(node.dataset.mapX);
    const mapY = Number(node.dataset.mapY);
    if (!Number.isFinite(mapX) || !Number.isFinite(mapY)) return;
    node.style.left = `${view.x + mapX * view.scale}px`;
    node.style.top = `${view.y + mapY * view.scale}px`;
    node.style.transform = heading === null
      ? "translate(-50%,-50%)"
      : `translate(-50%,-50%) rotate(${heading}deg)`;
  }

  function updateMarkerScreenPositions() {
    const points = [];
    for (const [key,node] of featureNodes) {
      positionScreenMarker(node);
      node.hidden = false;
      node.classList.remove("cluster");
      node.querySelector(".cluster-count")?.remove();
      const x=Number.parseFloat(node.style.left),y=Number.parseFloat(node.style.top);
      if(x < -30 || y < -30 || x > viewport.clientWidth+30 || y > viewport.clientHeight+30){node.hidden=true;continue}
      points.push({x,y,key,node,pinned:key===selectedFeature?.key});
    }
    for(const group of waypointSymbols.layout(points,30)){
      if(group.points.length<2)continue;
      const [first,...others]=group.points;
      first.node.classList.add("cluster");
      const count=document.createElement("span");count.className="cluster-count";count.textContent=String(group.points.length);first.node.append(count);
      first.node.style.left=`${group.x}px`;first.node.style.top=`${group.y}px`;
      first.node.dataset.clusterSize=String(group.points.length);
      for(const point of others)point.node.hidden=true;
    }
    if (playerNode) {
      const heading = (displayedPlayerHeading ?? 0) * 180 / Math.PI;
      positionScreenMarker(playerNode, heading);
    }
  }

  function renderFeatureMarkers() {
    if (!meta) return;
    for (const node of featureNodes.values()) node.remove();
    featureNodes = new Map();
    for (const feature of features) {
      if (feature.kind === "Player" || !featureVisible(feature)) continue;
      if(searchQuery&&!`${feature.label} ${waypointType(feature)}`.toLocaleLowerCase().includes(searchQuery))continue;
      const node = makeMarker(feature);
      if (selectedFeature && String(selectedFeature.key) === String(feature.key)) node.classList.add("selected");
      featureNodes.set(feature.key, node);
    }
    updateMarkerScreenPositions();
    document.querySelector("#locationCount").textContent=`${featureNodes.size} discovered location${featureNodes.size===1?"":"s"}`;
    updatePlayerMarker();
  }

  function updatePlayerMarker() {
    if (!meta) return;
    const position = displayedPlayerPosition || liveState.position;
    if (!position) return;
    if (!playerNode) {
      playerNode = document.createElement("span");
      playerNode.className = "marker player";
      playerNode.title = "You";
      playerNode.innerHTML = waypointSymbols.svg("player");
      markers.append(playerNode);
    }
    const point = mapPointWorld(position.x, position.y);
    const heading = (displayedPlayerHeading ?? 0) * 180 / Math.PI;
    playerNode.dataset.mapX = String(point.x);
    playerNode.dataset.mapY = String(point.y);
    positionScreenMarker(playerNode, heading);
  }

  function setLiveState(next) {
    liveState = next || {};
    const position = liveState.position;
    const direction = liveState.direction || { x: 0, y: 1 };
    const rawX = Number(direction.x), rawY = Number(direction.y);
    targetPlayerHeading = Math.atan2(Number.isFinite(rawX) ? rawX : 0, Number.isFinite(rawY) ? rawY : 1);
    if (position && !displayedPlayerPosition) {
      displayedPlayerPosition = {
        x: Number(position.x) || 0,
        y: Number(position.y) || 0,
        z: Number(position.z) || 0,
      };
      displayedPlayerHeading = targetPlayerHeading;
    }
  }

  function animatePlayer(now) {
    const dt = Math.min(100, Math.max(1, now - playerFrameTime));
    playerFrameTime = now;
    const target = liveState.position;
    if (target) {
      if (!displayedPlayerPosition) setLiveState(liveState);
      const amount = 1 - Math.exp(-dt / 54);
      for (const key of ["x", "y", "z"]) {
        const value = Number(target[key]) || 0;
        displayedPlayerPosition[key] += (value - displayedPlayerPosition[key]) * amount;
      }
      if (displayedPlayerHeading === null) displayedPlayerHeading = targetPlayerHeading ?? 0;
      const desired = targetPlayerHeading ?? displayedPlayerHeading;
      const delta = Math.atan2(Math.sin(desired - displayedPlayerHeading), Math.cos(desired - displayedPlayerHeading));
      displayedPlayerHeading += delta * amount;
      updatePlayerMarker();
    }
    requestAnimationFrame(animatePlayer);
  }

  function clearEnclosedFogIslands(bounds = null, maxArea = 512) {
    const canvasWidth = fogOverlay.width;
    const canvasHeight = fogOverlay.height;
    if (!canvasWidth || !canvasHeight) return 0;
    const margin = 3;
    const left = bounds ? Math.max(0, Math.floor(bounds.left) - margin) : 0;
    const top = bounds ? Math.max(0, Math.floor(bounds.top) - margin) : 0;
    const right = bounds ? Math.min(canvasWidth, Math.ceil(bounds.right) + margin) : canvasWidth;
    const bottom = bounds ? Math.min(canvasHeight, Math.ceil(bounds.bottom) + margin) : canvasHeight;
    const width = right - left;
    const height = bottom - top;
    if (width <= 2 || height <= 2) return 0;

    const pixels = fogContext.getImageData(left, top, width, height);
    const data = pixels.data;
    // Canvas antialiasing can leave a one-pixel fog filament joining a small
    // interior smear to the real unexplored mass. A plain component scan then
    // considers both to be one valid region. Prune only genuinely thin fog
    // pixels (three or fewer neighbours) before the scan. Ordinary reveal
    // boundaries retain five or more neighbours and remain untouched.
    const binary = new Uint8Array(width * height);
    let normalized = false;
    for (let index = 0; index < binary.length; index += 1) {
      const alpha = data[index * 4 + 3];
      if (alpha <= 24) {
        if (alpha) normalized = true;
        data[index * 4 + 3] = 0;
      }
      else {
        binary[index] = 1;
        if (alpha >= 232) {
          if (alpha !== 255) normalized = true;
          data[index * 4 + 3] = 255;
        }
      }
    }
    const prune = [];
    for (let y = 1; y < height - 1; y += 1) {
      for (let x = 1; x < width - 1; x += 1) {
        const index = y * width + x;
        if (!binary[index]) continue;
        let neighbours = 0;
        for (let oy = -1; oy <= 1; oy += 1) {
          for (let ox = -1; ox <= 1; ox += 1) {
            if ((!ox && !oy) || !binary[(y + oy) * width + x + ox]) continue;
            neighbours += 1;
          }
        }
        if (neighbours <= 3) prune.push(index);
      }
    }
    for (const index of prune) {
      binary[index] = 0;
      data[index * 4 + 3] = 0;
    }
    if (prune.length) normalized = true;
    const visited = new Uint8Array(width * height);
    const queue = new Int32Array(width * height);
    let cleared = 0;
    const fogPixel = index => data[index * 4 + 3] > 24;

    for (let seed = 0; seed < visited.length; seed += 1) {
      if (visited[seed] || !fogPixel(seed)) continue;
      let head = 0;
      let tail = 0;
      let touchesBoundary = false;
      visited[seed] = 1;
      queue[tail++] = seed;
      while (head < tail) {
        const index = queue[head++];
        const x = index % width;
        const y = Math.floor(index / width);
        if (x === 0 || y === 0 || x === width - 1 || y === height - 1) touchesBoundary = true;
        for (let oy = -1; oy <= 1; oy += 1) {
          const ny = y + oy;
          if (ny < 0 || ny >= height) continue;
          for (let ox = -1; ox <= 1; ox += 1) {
            if (!ox && !oy) continue;
            const nx = x + ox;
            if (nx < 0 || nx >= width) continue;
            const next = ny * width + nx;
            if (visited[next] || !fogPixel(next)) continue;
            visited[next] = 1;
            queue[tail++] = next;
          }
        }
      }
      if (touchesBoundary || tail > maxArea) continue;
      for (let offset = 0; offset < tail; offset += 1) {
        const pixel = queue[offset] * 4;
        data[pixel] = 0;
        data[pixel + 1] = 0;
        data[pixel + 2] = 0;
        data[pixel + 3] = 0;
      }
      cleared += tail;
    }
    if (cleared || normalized) fogContext.putImageData(pixels, left, top);
    return cleared;
  }

  function renderFog(force = false) {
    if (!meta) return;
    const revision = Number(exploration.revision) || 0;
    if (!force && revision === renderedExplorationRevision) return;
    renderedExplorationRevision = revision;
    const maskWidth = Math.min(1024, meta.width);
    const maskHeight = Math.max(1, Math.round(maskWidth * meta.height / meta.width));
    const samples = Array.isArray(exploration.samples) ? exploration.samples : [];
    const points = Array.isArray(exploration.points) ? exploration.points : [];
    const cells = Array.isArray(exploration.cells) ? exploration.cells : [];
    const legacyCellCount = !samples.length && !points.length ? cells.length : 0;
    const nextFogIdentity = [
      mapIdentity, Number(exploration.radius || 192), settings.revealShape || "directional", maskWidth, maskHeight,
      Number(meta.worldMinX), Number(meta.worldMaxX), Number(meta.worldMinY), Number(meta.worldMaxY),
    ].join(":");
    const fullRebuild = (
      force
      || nextFogIdentity !== fogIdentity
      || fogOverlay.width !== maskWidth
      || fogOverlay.height !== maskHeight
      || samples.length < fogSampleKeys.size
      || points.length !== fogPointCount
      || legacyCellCount !== fogCellCount
    );
    if (fogOverlay.width !== maskWidth || fogOverlay.height !== maskHeight) {
      fogOverlay.width = maskWidth;
      fogOverlay.height = maskHeight;
    }
    fogOverlay.hidden = !exploration.enabled || settings.fogOfWar === false;
    if (!exploration.enabled || settings.fogOfWar === false) return;
    const radius = Number(exploration.radius) || 192;
    const radiusPixels = radius / Math.max(1, meta.worldMaxX - meta.worldMinX) * maskWidth;
    // A four-metre Minkowski expansion matches the exploration sample grid.
    // Adjacent directional footprints overlap, so antialiased one-pixel
    // islands cannot remain as grey/black smears inside a walked route.
    const seamPixels = Math.max(1, FOG_SEAM_WORLD / Math.max(1, meta.worldMaxX - meta.worldMinX) * maskWidth);
    if (fullRebuild) {
      fogIdentity = nextFogIdentity;
      fogPointCount = points.length;
      fogCellCount = legacyCellCount;
      fogSampleKeys = new Set();
      fogContext.globalCompositeOperation = "source-over";
      fogContext.clearRect(0, 0, maskWidth, maskHeight);
      // Covered terrain must be genuinely undiscovered, not a dark colour
      // filter through which roads and biomes are still visible.
      fogContext.fillStyle = "#000";
      fogContext.fillRect(0, 0, maskWidth, maskHeight);
    }
    const pendingSamples = [];
    for (const sample of samples) {
      if (!Array.isArray(sample) || sample.length < 3) continue;
      const key = `${Number(sample[0])},${Number(sample[1])},${Number(sample[2])}`;
      if (fogSampleKeys.has(key)) continue;
      fogSampleKeys.add(key);
      pendingSamples.push(sample);
    }
    fogContext.globalCompositeOperation = "destination-out";
    fogContext.beginPath();
    const dirtyBounds = { left: maskWidth, top: maskHeight, right: 0, bottom: 0 };
    const includeDirty = (x, y, amount) => {
      dirtyBounds.left = Math.min(dirtyBounds.left, x - amount);
      dirtyBounds.top = Math.min(dirtyBounds.top, y - amount);
      dirtyBounds.right = Math.max(dirtyBounds.right, x + amount);
      dirtyBounds.bottom = Math.max(dirtyBounds.bottom, y + amount);
    };
    for (const sample of pendingSamples) {
      const mapped = mapPointWorld(Number(sample[0]), Number(sample[1]));
      const sampleX = mapped.x / meta.width * maskWidth;
      const sampleY = mapped.y / meta.height * maskHeight;
      traceReveal(
        fogContext,
        sampleX,
        sampleY,
        radiusPixels,
        Number(sample[2]) * Math.PI / 180,
        seamPixels,
      );
      includeDirty(sampleX, sampleY, radiusPixels + seamPixels + 4);
    }
    if (fullRebuild) {
      const sampledPositions = new Set(samples.map(sample => `${Number(sample?.[0])},${Number(sample?.[1])}`));
      for (const point of points) {
        if (!Array.isArray(point) || point.length < 2) continue;
        if (sampledPositions.has(`${Number(point[0])},${Number(point[1])}`)) continue;
        const mapped = mapPointWorld(Number(point[0]), Number(point[1]));
        const x = mapped.x / meta.width * maskWidth;
        const y = mapped.y / meta.height * maskHeight;
        fogContext.moveTo(x + radiusPixels + seamPixels, y);
        fogContext.arc(x, y, radiusPixels + seamPixels, 0, Math.PI * 2);
      }
      // Old cell-only exploration histories remain readable, but modern
      // saves never use a 64 m tile centre as the reveal origin.
      if (!samples.length && !points.length) {
        const cellSize = Number(exploration.cellSize) || 64;
        for (const cell of cells) {
          if (!Array.isArray(cell) || cell.length < 2) continue;
          const point = mapPointWorld((Number(cell[0]) + .5) * cellSize, (Number(cell[1]) + .5) * cellSize);
          const x = point.x / meta.width * maskWidth;
          const y = point.y / meta.height * maskHeight;
          fogContext.moveTo(x + radiusPixels + seamPixels, y);
          fogContext.arc(x, y, radiusPixels + seamPixels, 0, Math.PI * 2);
        }
      }
    }
    // Follow the exact live arrow centre and facing between persistent 4 m
    // samples. Exploration therefore has the same reach at every tile edge.
    if (liveState.position) {
      const mapped = mapPointWorld(liveState.position.x, liveState.position.y);
      const x = mapped.x / meta.width * maskWidth;
      const y = mapped.y / meta.height * maskHeight;
      const direction = liveState.direction || { x: 0, y: 1 };
      const rawX = Number(direction.x), rawY = Number(direction.y);
      traceReveal(fogContext, x, y, radiusPixels, Math.atan2(Number.isFinite(rawX) ? rawX : 0, Number.isFinite(rawY) ? rawY : 1), seamPixels);
      includeDirty(x, y, radiusPixels + seamPixels + 4);
    }
    fogContext.fillStyle = "#000";
    fogContext.fill();
    fogContext.globalCompositeOperation = "source-over";
    // Radius expansion cannot reliably remove antialiased remnants where
    // several directional footprints nearly meet. Inspect the actual alpha
    // topology instead: an enclosed, small fog component is a rendering
    // remnant; the true unexplored world remains connected to the boundary.
    // Full rebuilds are scanned once. Live updates inspect only their small
    // dirty neighbourhood, so pan/zoom and the player marker remain fluid.
    if (fullRebuild) clearEnclosedFogIslands(null, 1536);
    else if (dirtyBounds.right > dirtyBounds.left && dirtyBounds.bottom > dirtyBounds.top) {
      clearEnclosedFogIslands(dirtyBounds, 512);
    }
  }

  function renderLegend() {
    legendRows.replaceChildren();
    for (const [category, info] of Object.entries(categoryInfo)) {
      const row = document.createElement("label");
      row.className = "legend-row";
      row.innerHTML = `<span class="legend-name"><i class="legend-swatch" style="--swatch:${info.color}"></i>${info.label}</span>`;
      for (const [target, label] of [["fullMap", "FULL"], ["minimap", "MINI"]]) {
        const wrap = document.createElement("span");
        wrap.className = "legend-check";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = filterFor(category)[target] !== false;
        input.title = `${label === "FULL" ? "Full map" : "Minimap"}: ${info.label}`;
        input.addEventListener("change", async () => {
          const waypointFilters = structuredClone(settings.waypointFilters || {});
          waypointFilters[category] = { ...filterFor(category), [target]: input.checked };
          const result = await api("save-companion-settings", { waypointFilters });
          settings = result.settings || settings;
          renderFeatureMarkers();
        });
        wrap.append(input, label);
        row.append(wrap);
      }
      legendRows.append(row);
    }
  }

  function syncQuickSettings() {
    document.querySelector("#quickWorldSpan").value = settings.worldSpan ?? 650;
    document.querySelector("#quickWorldSpanValue").textContent = `${settings.worldSpan ?? 650} m`;
    document.querySelector("#quickFogRadius").value = settings.fogRadius ?? 192;
    document.querySelector("#quickFogRadiusValue").textContent = `${settings.fogRadius ?? 192} m`;
    document.querySelector("#quickOrientation").value = settings.orientation || "map-up";
    document.querySelector("#quickShape").value = settings.shape || "square";
    document.querySelector("#quickBorder").value = settings.border || "mechanic";
    document.querySelector("#quickRevealShape").value = settings.revealShape || "directional";
    document.querySelector("#quickFog").checked = settings.fogOfWar !== false;
    document.querySelector("#quickCompass").checked = settings.showCompass !== false;
    document.querySelector("#quickKeyHint").checked = settings.showKeyHint !== false;
  }

  function quickSettingsPayload() {
    return {
      worldSpan: Number(document.querySelector("#quickWorldSpan").value),
      fogRadius: Number(document.querySelector("#quickFogRadius").value),
      orientation: document.querySelector("#quickOrientation").value,
      shape: document.querySelector("#quickShape").value,
      border: document.querySelector("#quickBorder").value,
      revealShape: document.querySelector("#quickRevealShape").value,
      fogOfWar: document.querySelector("#quickFog").checked,
      showCompass: document.querySelector("#quickCompass").checked,
      showKeyHint: document.querySelector("#quickKeyHint").checked,
    };
  }

  function updateQuickSettings() {
    const previousReveal = settings.revealShape;
    settings = { ...settings, ...quickSettingsPayload() };
    document.querySelector("#quickWorldSpanValue").textContent = `${settings.worldSpan} m`;
    document.querySelector("#quickFogRadiusValue").textContent = `${settings.fogRadius} m`;
    renderFog(true);
    renderFeatureMarkers();
    if (previousReveal !== settings.revealShape) renderedExplorationRevision = -1;
    const statusNode = document.querySelector("#quickSettingsStatus");
    statusNode.textContent = "Applying…";
    clearTimeout(quickSaveTimer);
    quickSaveTimer = setTimeout(async () => {
      try {
        const result = await api("save-companion-settings", quickSettingsPayload());
        settings = result.settings || settings;
        syncQuickSettings();
        statusNode.textContent = "Saved · minimap updated live.";
      } catch (error) {
        statusNode.textContent = error?.message || "Could not save settings.";
      }
    }, 180);
  }

  async function loadImage(uri) {
    if (image.src === uri && image.complete && image.naturalWidth) return;
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("The generated lossless map image could not be opened"));
      image.src = uri;
    });
  }

  async function refreshActiveSave(force = false) {
    if (building) { refreshRequested = refreshRequested || force; return; }
    building = true;
    try {
      const active = await api("activate-current-save");
      if (active.pending) {
        worldName.textContent = active.save?.name || "Waiting for Survival world…";
        setStatus(active.message || "Waiting for the world…", .02, { busy: true });
        // M can be pressed during the short save-snapshot bind. A single
        // startup request used to leave the full map empty until the 15-second
        // maintenance poll; retry promptly while activation is pending.
        setTimeout(() => refreshActiveSave(false), 250);
        return;
      }
      const current = active.current || {};
      const seed = Number(current.game?.seed || 0);
      worldName.textContent = `${current.fileName || active.save?.name || "Survival world"} · seed ${seed}`;
      const state = await api("minimap-state");
      const overlay = state.overlay || {};
      const config = state.config || {};
      // A 4K overview is much cheaper to transform at 60/120 Hz.  The
      // original lossless render remains available through idle detail tiles,
      // so zoom quality is preserved without dragging an 8K texture around.
      const nextImage = String(state.minimapImage || state.image || config.externalMap || "");
      const identity = `${active.save?.path || current.fileName}|${seed}|${nextImage}|${state.image || ""}`;
      const nextWorld = `${config.sourcePath || active.save?.path || current.fileName}|${seed}`;
      if (nextWorld !== worldContextIdentity) {
        worldContextIdentity = nextWorld;
        selectedFeature = null;
        hideWaypointTooltip();
        document.querySelector("#waypointDetails").classList.add("empty");
        document.querySelector("#waypointDetailsName").textContent = "Select a marker";
        document.querySelector("#waypointDetailsType").textContent = "No location selected";
        document.querySelector("#waypointDetailsIcon").replaceChildren();
        document.querySelector("#waypointDetailsPreview").hidden = true;
        for (const id of ["waypointDetailsCell", "waypointDetailsWorld", "waypointDetailsDistance"]) document.getElementById(id).textContent = "—";
        liveState = {}; displayedPlayerPosition = null; displayedPlayerHeading = null; targetPlayerHeading = null;
        lastLiveUpdated = 0; pushedLive = null;
        searchQuery = ""; document.querySelector("#locationSearch").value = "";
      }
      const previousSettings = JSON.stringify(settings);
      settings = state.settings || settings;
      const settingsChanged = previousSettings !== JSON.stringify(settings);
      exploration = state.exploration || exploration;
      syncQuickSettings();
      if (!config.ready || !nextImage) {
        displayReady = false;
        api("display-ready", { ready: false }).catch(() => {});
        stage.hidden = true;
        legend.hidden = true;
        mapSidebar.hidden = true;
        setStatus(overlay.message || "Preparing this seed's map…", overlay.progress, { busy: overlay.busy });
        return;
      }
      if (!force && identity === mapIdentity && meta) {
        renderFog(settingsChanged);
        if (settingsChanged) renderFeatureMarkers();
        setStatus(config.mapQuality === "preview" ? "Terrain map ready" : "HD map ready", 1, { mapReady: true });
        return;
      }
      const bounds = Array.isArray(config.cellBounds) ? config.cellBounds.map(Number) : [];
      if (bounds.length !== 4) throw new Error("The cached map bounds were missing");
      clearDetailTiles();
      meta = {
        bounds,
        width: Math.max(1, Number(config.mapWidth) || 1),
        height: Math.max(1, Number(config.mapHeight) || 1),
        worldMinX: Number(config.worldMinX), worldMaxX: Number(config.worldMaxX),
        worldMinY: Number(config.worldMinY), worldMaxY: Number(config.worldMaxY),
        image: nextImage,
        detailTiles: config.detailTiles || null,
      };
      const places = await api("map-features", { kind: "Places", worldId: 1, limit: 2000 });
      features = places.features || [];
      mapIdentity = identity;
      await loadImage(meta.image);
      stage.hidden = false;
      legend.hidden = false;
      mapSidebar.hidden = false;
      for (const node of [stage, detailTiles]) {
        node.style.width = `${meta.width}px`;
        node.style.height = `${meta.height}px`;
      }
      renderLegend();
      renderFog(true);
      renderFeatureMarkers();
      fitMap();
      if(settings.fogOfWar!==false)centerOnPlayer();
      errorBox.hidden = true;
      const detail = meta.detailTiles?.ready
        ? ` · ${Math.round(meta.width * Number(meta.detailTiles.scale || 2))} px tiled zoom`
        : "";
      setStatus(`${config.mapQuality === "preview" ? "Terrain" : "HD"} map · ${meta.width} × ${meta.height}${detail}`, 1, { mapReady: true });
      displayReady = true;
      await api("display-ready", { ready: true });
    } catch (error) {
      errorBox.hidden = false;
      errorBox.textContent = `The generated map could not be prepared.\n\n${error?.message || error}`;
      setStatus(`Map generation failed · ${error?.message || error}`, 0);
    } finally { building = false; if(refreshRequested){refreshRequested=false;refreshActiveSave(true)} }
  }

  async function refreshLiveState() {
    if(liveRequest)return;
    liveRequest=true;
    try {
      const result = pushedLive && performance.now()-pushedLiveAt < 300 ? pushedLive : await api("live-state");
      if (!result.available) return;
      if(Number(result.updated || 0)<lastLiveUpdated)return;
      lastLiveUpdated=Number(result.updated || 0);
      setLiveState(result.state || {});
      if(pendingPlayerCenter)centerOnPlayer();
      const nextRevision = Number(liveState.explorationRevision) || 0;
      if (nextRevision !== Number(exploration.revision || 0)) {
        exploration = (await api("exploration-state")).exploration || exploration;
        renderFog();
        renderFeatureMarkers();
      } else updatePlayerMarker();
    } catch (_) {} finally { liveRequest=false; }
  }

  function centerOnPlayer() {
    const position=displayedPlayerPosition||liveState.position;if(!position||!meta){pendingPlayerCenter=true;return;}
    pendingPlayerCenter=false;
    const p=mapPointWorld(position.x,position.y);
    const left=legend.offsetWidth+40,right=mapSidebar.offsetWidth+40,available=Math.max(180,viewport.clientWidth-left-right);
    const unitsPerPixel=Math.max(1e-6,(meta.worldMaxX-meta.worldMinX)/meta.width);
    view.scale=Math.max(view.fit,Math.min(32,available*unitsPerPixel/Math.max(640,Number(settings.worldSpan||660)*2)));
    view.x=left+available/2-p.x*view.scale;view.y=88+(viewport.clientHeight-136)/2-p.y*view.scale;applyView();
  }

  let closePending = false;
  async function closeMap() {
    if (closePending) return;
    closePending = true;
    try { await api("hide-overlay"); }
    finally { setTimeout(() => { closePending = false; }, 300); }
  }

  function boot() {
    if (booted) return;
    booted = true;
    refreshActiveSave(false);
    setInterval(() => refreshActiveSave(false), 5000);
    setInterval(refreshLiveState, 50);
    requestAnimationFrame(animatePlayer);
  }

  viewport.addEventListener("pointerdown", event => {
    if (!meta || event.button !== 0 || event.target.closest("header,#legend")) return;
    drag = { x: event.clientX, y: event.clientY, left: view.x, top: view.y };
    viewport.classList.add("dragging");
    setInteracting(true);
    viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener("pointermove", event => {
    if (!drag) return;
    view.x = drag.left + event.clientX - drag.x;
    view.y = drag.top + event.clientY - drag.y;
    applyView();
  });
  function finishDrag() {
    drag = null;
    viewport.classList.remove("dragging");
    setInteracting(false, 90);
  }
  viewport.addEventListener("pointerup", finishDrag);
  viewport.addEventListener("pointercancel", finishDrag);
  viewport.addEventListener("wheel", event => {
    event.preventDefault();
    setInteracting(true);
    const bounds = viewport.getBoundingClientRect();
    zoomAt(Math.exp(-event.deltaY * .0012), event.clientX - bounds.left, event.clientY - bounds.top);
    setInteracting(false, 150);
  }, { passive: false });
  document.querySelector("#zoomOut").addEventListener("click", () => zoomAt(.8));
  document.querySelector("#zoomIn").addEventListener("click", () => zoomAt(1.25));
  document.querySelector("#fitWorld").addEventListener("click", fitMap);
  document.querySelector("#centerPlayer").addEventListener("click", centerOnPlayer);
  document.querySelector("#locationSearch").addEventListener("input",event=>{searchQuery=event.target.value.trim().toLocaleLowerCase();renderFeatureMarkers()});
  document.querySelector("#closeMap").addEventListener("click", closeMap);
  document.querySelector("#quickSettings").addEventListener("input", updateQuickSettings);
  document.querySelector("#quickSettings").addEventListener("change", updateQuickSettings);
  window.addEventListener("resize", fitMap);
  window.companionShow = () => { if (!displayReady || !meta) refreshActiveSave(false); refreshLiveState(); };
  window.companionMapUpdated = () => refreshActiveSave(true);
  window.companionSettingsUpdated = () => refreshActiveSave(true);
  window.chrome?.webview?.addEventListener("message",event=>{
    if(event.data?.type!=="minimap-live"||!event.data.live?.available)return;
    const live=event.data.live;if(Number(live.updated||0)<lastLiveUpdated)return;
    lastLiveUpdated=Number(live.updated||0);pushedLive=live;pushedLiveAt=performance.now();setLiveState(live.state);
  });
  window.addEventListener("keydown",event=>{if(event.target.matches("input,select,textarea"))return;if(event.key==="Escape"){event.preventDefault();closeMap()}});
  if (window.pywebview?.api) boot();
  else window.addEventListener("pywebviewready", boot, { once: true });
})();
