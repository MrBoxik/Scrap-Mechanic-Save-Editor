(() => {
  "use strict";

  // One canonical waypoint language for the full map and minimap.  Both
  // renderers consume these exact 24 x 24 paths, so icons cannot change
  // shape, colour, or crop differently between the two windows.
  const info = {
    quests: {
      label: "Quests", color: "#bc73ff",
      path: "M12 2.8l2.55 5.18 5.72.83-4.14 4.03.98 5.69L12 15.85l-5.11 2.68.98-5.69-4.14-4.03 5.72-.83z",
    },
    stations: {
      label: "Stations", color: "#ffbe30",
      offsetX: 1.25,
      path: "M14.5 5.1a4.25 4.25 0 0 0-5.3 5.3L3.4 16.2a2.1 2.1 0 0 0 3 3l5.8-5.8a4.25 4.25 0 0 0 5.3-5.3l-2.35 2.35-2.7-.9-.9-2.7z",
    },
    warehouses: {
      label: "Warehouses", color: "#ff7652",
      path: "M3.5 20.2h17M5 20V8.2L12 3l7 5.2V20M8.2 11h2.2v2.2H8.2zm5.4 0h2.2v2.2h-2.2zM9.2 20v-4.1h5.6V20",
    },
    packing: {
      label: "Packing stations", color: "#46bfe7",
      path: "M4 7.4L12 3l8 4.4v9.2L12 21l-8-4.4zM4.4 7.6L12 12l7.6-4.4M12 12v9M8 5.2l8 4.5",
    },
    labs: {
      label: "Labs & dungeons", color: "#d677ff",
      path: "M9 3h6M10 3v5.2l-5.2 8.7A2.7 2.7 0 0 0 7.1 21h9.8a2.7 2.7 0 0 0 2.3-4.1L14 8.2V3M7.1 16h9.8M8.8 12.8h6.4",
    },
    landmarks: {
      label: "Landmarks", color: "#70df70",
      path: "M12 21s6-5.6 6-11a6 6 0 1 0-12 0c0 5.4 6 11 6 11zM12 12.2a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4z",
    },
    beacons: {
      label: "Player beacons", color: "#ffc328",
      offsetX: -1.5,
      path: "M8 21h8M12 21V3M12 5h7l-2 3 2 3h-7",
    },
  };

  const player = {
    label: "Player", color: "#ffc42c", fill: "#ffc42c", stroke: "#06171c",
    path: "M12 1.7L21 22l-9-4.8L3 22z",
  };

  function entry(category) {
    if (category === "player") return player;
    return info[category] || info.landmarks;
  }

  function svg(category) {
    const item = entry(category);
    const fill = item.fill || "none";
    const stroke = item.stroke || item.color;
    const width = category === "player" ? "2.1" : "1.9";
    const transform = `translate(${Number(item.offsetX) || 0} ${Number(item.offsetY) || 0})`;
    return `<svg viewBox="0 0 24 24" aria-hidden="true" style="color:${item.color}" fill="${fill}" stroke="${stroke}" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round"><path transform="${transform}" d="${item.path}" fill="${fill}" stroke="${stroke}"/></svg>`;
  }

  const canvasImages = new Map();

  function canvasImage(category) {
    const item = entry(category);
    if (!item.path || typeof Image === "undefined") return null;
    if (!canvasImages.has(category)) {
      const image = new Image();
      const fill = item.fill || "none";
      const stroke = item.stroke || item.color;
      const transform = `translate(${Number(item.offsetX) || 0} ${Number(item.offsetY) || 0})`;
      const markup = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="${fill}" stroke="${stroke}" stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round"><path transform="${transform}" d="${item.path}" fill="${fill}" stroke="${stroke}"/></svg>`;
      image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
      canvasImages.set(category, image);
    }
    return canvasImages.get(category);
  }

  function drawFallback(context, category, x, y, radius) {
    const item = entry(category);
    const r = Math.max(3, radius);
    context.save();
    context.translate(x + (Number(item.offsetX) || 0) * r / 12, y + (Number(item.offsetY) || 0) * r / 12);
    context.strokeStyle = item.color;
    context.fillStyle = item.color;
    context.lineWidth = Math.max(1.6, r * .18);
    context.lineCap = "round";
    context.lineJoin = "round";
    context.beginPath();
    if (category === "player") {
      context.moveTo(0, -r); context.lineTo(r * .82, r); context.lineTo(0, r * .52);
      context.lineTo(-r * .82, r); context.closePath();
      context.strokeStyle = item.stroke || "#06171c"; context.stroke(); context.fill();
    } else if (category === "quests") {
      for (let point = 0; point < 10; point += 1) {
        const angle = -Math.PI / 2 + point * Math.PI / 5;
        const distance = point % 2 ? r * .43 : r;
        const px = Math.cos(angle) * distance, py = Math.sin(angle) * distance;
        if (!point) context.moveTo(px, py); else context.lineTo(px, py);
      }
      context.closePath(); context.stroke();
    } else if (category === "stations") {
      context.arc(-r * .2, r * .2, r * .38, 0, Math.PI * 2); context.stroke();
      context.beginPath(); context.moveTo(r * .05, -r * .05); context.lineTo(r * .75, -r * .75);
      context.moveTo(r * .38, -r * .75); context.lineTo(r * .75, -r * .75); context.lineTo(r * .75, -r * .38); context.stroke();
    } else if (category === "warehouses") {
      context.moveTo(-r, -.25 * r); context.lineTo(0, -r); context.lineTo(r, -.25 * r);
      context.lineTo(r, r); context.lineTo(-r, r); context.closePath(); context.stroke();
      context.strokeRect(-r * .25, r * .25, r * .5, r * .75);
    } else if (category === "packing") {
      context.moveTo(0, -r); context.lineTo(r, -r * .45); context.lineTo(r, r * .5);
      context.lineTo(0, r); context.lineTo(-r, r * .5); context.lineTo(-r, -r * .45); context.closePath(); context.stroke();
      context.beginPath(); context.moveTo(-r, -r * .45); context.lineTo(0, .05 * r); context.lineTo(r, -r * .45); context.stroke();
    } else if (category === "labs") {
      context.moveTo(-r * .35, -r); context.lineTo(r * .35, -r);
      context.moveTo(-r * .2, -r); context.lineTo(-r * .2, -r * .2); context.lineTo(-r, r);
      context.lineTo(r, r); context.lineTo(r * .2, -r * .2); context.lineTo(r * .2, -r); context.stroke();
      context.beginPath(); context.moveTo(-r * .68, r * .45); context.lineTo(r * .68, r * .45); context.stroke();
    } else if (category === "beacons") {
      context.moveTo(0, -r); context.lineTo(0, r);
      context.moveTo(-r * .5, r); context.lineTo(r * .5, r);
      context.moveTo(0, -r * .75); context.lineTo(r, -r * .75);
      context.lineTo(r * .7, -r * .25); context.lineTo(r, r * .25);
      context.lineTo(0, r * .25); context.stroke();
    } else {
      context.moveTo(0, r); context.bezierCurveTo(-r * .15, r * .65, -r, 0, -r, -r * .15);
      context.arc(0, -r * .15, r, Math.PI, 0); context.bezierCurveTo(r, 0, r * .15, r * .65, 0, r); context.stroke();
      context.beginPath(); context.arc(0, -r * .15, r * .28, 0, Math.PI * 2); context.stroke();
    }
    context.restore();
  }

  function drawCanvas(context, category, x, y, radius) {
    const image = canvasImage(category);
    if (image?.complete && image.naturalWidth > 0) {
      context.drawImage(image, x - radius, y - radius, radius * 2, radius * 2);
      return true;
    }
    // WebView2 builds used by pywebview do not consistently implement the
    // SVG-string Path2D constructor.  The old call therefore painted only
    // the dark marker disc.  These explicit primitives keep every category
    // recognisable on the very first frame while the identical SVG warms.
    drawFallback(context, category, x, y, radius);
    return false;
  }

  function category(feature) {
    if (feature.kind === "Player") return "player";
    return feature.category || ({ Quest: "quests", Station: "stations", Warehouse: "warehouses",
      "Packing station": "packing", "Lab / dungeon": "labs" }[feature.kind]) || "landmarks";
  }

  function color(feature) {
    return category(feature) === "beacons"
      ? feature.beaconAtlas?.colors?.[Math.max(0, Number(feature.colorIndex ?? 1) - 1)] || entry("beacons").color
      : entry(category(feature)).color;
  }

  function worldPosition(feature) {
    const center = String(feature.key || "").startsWith("terrain-") ? 32 : 0;
    return { x: Number(feature.worldX ?? Number(feature.x) * 64 + center),
      y: Number(feature.worldY ?? Number(feature.y) * 64 + center) };
  }

  function mapPoint(feature, bounds, width, height) {
    const position = worldPosition(feature);
    const [xmin, xmax, ymin, ymax] = bounds.map(Number);
    return { x: (position.x / 64 - xmin) / (xmax - xmin + 1) * width,
      y: (ymax + 1 - position.y / 64) / (ymax - ymin + 1) * height };
  }

  function icon(feature, size = 22) {
    const node = document.createElement("span");
    node.className = "waypoint-symbol";
    node.style.width = node.style.height = `${size}px`;
    node.innerHTML = svg(category(feature));
    if (category(feature) === "beacons" && feature.beaconAtlas?.atlas) {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = size * 2;
      canvas.style.width = canvas.style.height = `${size}px`;
      const image = new Image(), atlas = feature.beaconAtlas;
      image.onload = () => {
        const frame = Number(atlas.frame) || 132, stride = frame + (Number(atlas.gap) || 12);
        canvas.getContext("2d").drawImage(image, Number(feature.iconIndex || 0) * stride, 0, frame, frame, 0, 0, canvas.width, canvas.height);
        node.replaceChildren(canvas);
      };
      image.src = atlas.atlas;
    }
    return node;
  }

  // Cluster only overlapping screen-space markers; selected locations and the
  // player retain their exact positions and remain independently accessible.
  function layout(points, distance = 30) {
    const groups = [], buckets = new Map(), cell = Math.max(1, distance);
    for (const point of points) {
      if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) continue;
      const gx = Math.floor(point.x / cell), gy = Math.floor(point.y / cell);
      let group = null;
      if (!point.pinned) for (let dx = -1; dx <= 1 && !group; dx++) for (let dy = -1; dy <= 1 && !group; dy++) {
        group = (buckets.get(`${gx + dx},${gy + dy}`) || []).find(row => !row.pinned && Math.hypot(row.anchorX - point.x, row.anchorY - point.y) < distance);
      }
      if (group) {
        group.points.push(point);
        group.x += (point.x - group.x) / group.points.length;
        group.y += (point.y - group.y) / group.points.length;
      } else {
        group = { x: point.x, y: point.y, anchorX: point.x, anchorY: point.y, pinned: point.pinned, points: [point] };
        groups.push(group);
        const key = `${gx},${gy}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(group);
      }
    }
    return groups;
  }

  function edgePoint(dx, dy, halfSize, shape = "circle") {
    const extent = shape === "circle" ? Math.hypot(dx, dy) : Math.max(Math.abs(dx), Math.abs(dy));
    const scale = extent > halfSize ? halfSize / extent : 1;
    return { x: dx * scale, y: dy * scale, offscreen: scale < 1 };
  }

  window.CompanionWaypointSymbols = Object.freeze({ info, entry, svg, drawCanvas, category, color, worldPosition, mapPoint, icon, layout, edgePoint });
})();
