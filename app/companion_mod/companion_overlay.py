from __future__ import annotations

import argparse
import atexit
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote, unquote, urlparse

try:
    from .live_process_tracker import ScrapMechanicLiveTracker
    from .companion_settings import load_companion_settings, save_companion_settings
except ImportError:
    # Direct-script launch used by the desktop shortcut.
    from live_process_tracker import ScrapMechanicLiveTracker
    from companion_settings import load_companion_settings, save_companion_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scrap_editor_app.steam_paths import find_game_root

if TYPE_CHECKING:
    from scrap_editor_app.backend import EditorApi


FULL_MAP_TITLE = "Scrap Mechanic World Map"
MINIMAP_TITLE = "Scrap Mechanic Minimap"
MINIMAP_RENDERER_TITLE = "Scrap Mechanic Minimap Renderer"
COMPANION_MAP_OPTIONS = {"maxSide": 8192, "variant": "companion-hq-v2"}


COMPANION_READ_METHODS = {
    "map-features",
    "map-3d-index",
    "map-3d-scene",
    "map-3d-scenes",
    "map-3d-scene-packs",
    "map-3d-mesh",
    "map-3d-meshes",
    "map-3d-texture",
    "store-3d-map",
    "store-3d-map-tile",
    "finalize-3d-map-tiles",
}


def directional_reveal_factor(relative_angle: float) -> float:
    """Return the reveal radius multiplier at an angle from player forward."""
    forward = max(0.0, min(1.0, (1.0 + math.cos(relative_angle)) * 0.5))
    return 0.14 + 0.86 * (forward ** 0.65)


FOG_SEAM_WORLD = 4.0


def directional_reveal_contains(
    origin_x: float,
    origin_y: float,
    target_x: float,
    target_y: float,
    direction_x: float,
    direction_y: float,
    radius: float,
) -> bool:
    """Test the same forward-biased visibility footprint drawn by both maps."""
    offset_x, offset_y = target_x - origin_x, target_y - origin_y
    distance = math.hypot(offset_x, offset_y)
    if distance <= max(0.0, radius) * 0.14 + FOG_SEAM_WORLD:
        return True
    direction_length = math.hypot(direction_x, direction_y)
    if distance <= 0.0 or direction_length <= 1e-6:
        return distance <= max(0.0, radius)
    cosine = max(-1.0, min(
        1.0,
        (offset_x * direction_x + offset_y * direction_y)
        / (distance * direction_length),
    ))
    return distance <= (
        max(0.0, radius) * directional_reveal_factor(math.acos(cosine))
        + FOG_SEAM_WORLD
    )


def reveal_contains(
    origin_x: float,
    origin_y: float,
    target_x: float,
    target_y: float,
    direction_x: float,
    direction_y: float,
    radius: float,
    shape: str = "directional",
) -> bool:
    """Test the configured exploration footprint."""
    if shape == "circle":
        return math.hypot(target_x - origin_x, target_y - origin_y) <= (
            max(0.0, radius) + FOG_SEAM_WORLD
        )
    return directional_reveal_contains(
        origin_x, origin_y, target_x, target_y,
        direction_x, direction_y, radius,
    )


def direction_heading_degrees(direction: dict[str, Any] | None) -> int:
    direction = direction or {}
    raw_x = direction.get("x", 0.0)
    raw_y = direction.get("y", 1.0)
    dx = float(0.0 if raw_x is None else raw_x)
    dy = float(1.0 if raw_y is None else raw_y)
    if not math.isfinite(dx) or not math.isfinite(dy) or math.hypot(dx, dy) <= 1e-6:
        return 0
    return int(round(math.degrees(math.atan2(dx, dy)) / 6.0) * 6) % 360


class OverlayBridge:
    """Bridge for the external minimap and bounded cached 2D map."""

    def __init__(self, signal_path: Path, game_root: Path | None = None) -> None:
        self._editor: EditorApi | None = None
        self._signal_path = signal_path.resolve()
        self._game_root = game_root.resolve() if game_root else None
        self._window: Any = None
        self._minimap_window: Any = None
        self._minimap_renderer_window: Any = None
        self._minimap_bitmap_sink: Any = None
        self._native_capture_enabled = False
        self._direct_renderer_enabled = False
        self._live_push_sink: Any = None
        self._live_push_host: Any = None
        self._full_map_live_push_host: Any = None
        self._shared_frame_sink: Any = None
        self._minimap_bitmap_ready = False
        self._capture_window: Any = None
        self._visible = False
        self._minimap_visible = False
        self._minimap_foreground_active = False
        self._minimap_was_exposed = False
        self._lock = threading.RLock()
        self._exploration_io_lock = threading.RLock()
        self._activation_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._activation_serial = 0
        self._metrics_enabled = os.getenv("SCRAP_COMPANION_METRICS") == "1"
        self._metric_live_requests: deque[float] = deque(maxlen=600)
        self._metric_live_pushes: deque[float] = deque(maxlen=600)
        self._metric_native_frames: deque[float] = deque(maxlen=600)
        self._metric_memory_samples: deque[float] = deque(maxlen=600)
        self._metric_diagnostics: deque[dict[str, Any]] = deque(maxlen=20)
        self._loaded_path = ""
        self._snapshot_path = ""
        self._loaded_stamp = 0
        self._loaded_fingerprint: tuple[tuple[str, int, int], ...] = ()
        self._last_snapshot_attempt = 0.0
        self._game_started_wall: float | None = None
        self._in_game_world = False
        self._world_needs_bind = True
        self._context_revision = 0
        self._last_live_updated = 0.0
        self._live_session_ready_since: float | None = None
        self._session_save_snapshot: dict[str, tuple[tuple[str, int, int], ...]] = {}
        self._map_ready = False
        self._display_ready = False
        self._map_busy = True
        self._map_message = "Starting map companion…"
        self._map_progress = 0.0
        self._map_job_serial = 0
        self._map_job_identity = ""
        self._map_worker: threading.Thread | None = None
        self._capture_job: dict[str, Any] | None = None
        self._capture_claimed = False
        self._request_sequence = 0
        self._ack_sequence = 0
        self._transition_serial = 0
        self._last_transition = 0.0
        self._minimap_handoff_until = 0.0
        self._outside_game_since: float | None = None
        self._live_tracker = ScrapMechanicLiveTracker()
        self._live_pid = 0
        self._live_pid_checked = 0.0
        self._live_expected: tuple[float, float, float] | None = None
        self._live_world_id = 1
        self._live_waypoints: list[dict[str, Any]] = []
        self._exploration_identity = ""
        self._explored_cells: set[str] = set()
        self._explored_points: set[str] = set()
        self._explored_sight: set[str] = set()
        self._discovered_features: set[str] = set()
        self._exploration_revision = 0
        self._exploration_dirty = False
        self._exploration_saved = 0.0
        self._last_exploration_position: tuple[float, float] | None = None
        self._last_exploration_sampled = 0.0
        self._pending_exploration_position: tuple[float, float] | None = None
        self._pending_exploration_started = 0.0
        self._pending_exploration_movement = 0.0
        self._live_memory_state: dict[str, Any] | None = None
        self._live_memory_sampled = 0.0
        self._live_sampler_stop = threading.Event()
        self._exploration_save_event = threading.Event()
        self._exploration_saver = threading.Thread(
            target=self._persist_exploration,
            name="ScrapMechanicExplorationPersistence",
            daemon=True,
        )
        self._live_sampler = threading.Thread(
            target=self._sample_live_process,
            name="ScrapMechanicLivePosition",
            daemon=True,
        )
        self._settings = load_companion_settings()
        self._settings_stamp = 0
        self._settings_checked = 0.0
        self._config_cache: dict[str, Any] = {"schema": 3, "ready": False}
        self._config_stamp = 0
        self._exploration_saver.start()
        self._live_sampler.start()

    @property
    def editor(self) -> EditorApi:
        # Importing the backend also imports the renderer and asset readers.
        # Keep even that import out of Scrap Mechanic's startup window.
        if self._editor is None:
            from scrap_editor_app.backend import EditorApi

            # The external map needs the save parser, terrain names, and atlas
            # only. Loading the inventory, clothing, decal, mesh, weather, and
            # progression catalogs retained hundreds of megabytes beside the
            # game even though the companion never calls those APIs.
            self._editor = EditorApi(companion_mode=True)
        return self._editor

    def request(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            data = payload or {}
            if method == "hide-overlay":
                self.hide()
                return {"ok": True}
            if method == "display-ready":
                self._display_ready = bool(data.get("ready", True))
                return {"ok": True, "ready": self._display_ready}
            if method == "activate-current-save":
                return {"ok": True, **self.activate_current_save()}
            if method == "publish-companion-map":
                return {"ok": True, **self.publish_companion_map(data)}
            if method == "live-state":
                return {"ok": True, **self.live_state()}
            if method == "minimap-state":
                return {"ok": True, **self.minimap_state()}
            if method == "minimap-live":
                if self._metrics_enabled:
                    with self._lock:
                        self._metric_live_requests.append(time.monotonic())
                return {"ok": True, **self.minimap_live()}
            if method == "minimap-ready":
                with self._lock:
                    self._minimap_bitmap_ready = bool(data.get("ready", True))
                return {"ok": True, "ready": self._minimap_bitmap_ready}
            if method == "minimap-bitmap":
                sink = self._minimap_bitmap_sink
                accepted = bool(
                    sink is not None
                    and sink(str(data.get("data") or ""))
                )
                return {"ok": True, "accepted": accepted}
            if method == "minimap-frame-signal":
                sink = self._shared_frame_sink
                accepted = bool(sink and sink())
                if self._metrics_enabled:
                    with self._lock:
                        self._metric_diagnostics.append({
                            "at": round(time.time(), 3),
                            "stage": "frame-signal",
                            "accepted": accepted,
                            "sink": sink is not None,
                        })
                return {"ok": True, "accepted": accepted}
            if method == "minimap-diagnostic":
                if self._metrics_enabled:
                    with self._lock:
                        self._metric_diagnostics.append({
                            "at": round(time.time(), 3),
                            **{str(key): value for key, value in data.items()},
                        })
                return {"ok": True}
            if method == "minimap-frame":
                remember_minimap_content_region(data)
                hwnd = find_window_by_title(MINIMAP_TITLE)
                if hwnd:
                    apply_minimap_window_region(hwnd, 0, self.companion_settings())
                return {"ok": True}
            if method == "exploration-state":
                return {"ok": True, "exploration": self.exploration_state()}
            if method == "companion-settings":
                return {"ok": True, "settings": self.companion_settings()}
            if method == "save-companion-settings":
                merged = {**self._settings, **data}
                if isinstance(data.get("waypointFilters"), dict):
                    merged["waypointFilters"] = {
                        **self._settings.get("waypointFilters", {}),
                        **data["waypointFilters"],
                    }
                self._settings = save_companion_settings(merged)
                # The minimap is already warm underneath the full-map window.
                # Promoting its HWND to TOPMOST here made it jump above the
                # map whenever a legend checkbox was clicked.
                with self._lock:
                    full_map_visible = self._visible
                if not full_map_visible:
                    configure_minimap_window(self._settings, retries=6, promote=False)
                return {"ok": True, "settings": self._settings}
            if method == "companion-progress":
                return {"ok": True, **self.update_companion_progress(data)}
            if method == "companion-status":
                return {"ok": True, **self.editor.status({})}
            if method == "capture-job":
                return {"ok": True, **self.capture_job()}
            if method == "capture-claim":
                return {"ok": True, **self.capture_claim(data)}
            if method == "capture-complete":
                return {"ok": True, **self.capture_complete(data)}
            if method == "capture-failed":
                return {"ok": True, **self.capture_failed(data)}
            if method not in COMPANION_READ_METHODS:
                raise ValueError(f"Method is not available to the read-only companion: {method}")
            result = self.editor.dispatch(str(method), data)
            return {"ok": True, **result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @property
    def config_path(self) -> Path:
        return self._signal_path.parent / "map_config.json"

    @property
    def live_state_path(self) -> Path:
        return self._signal_path.parent / "live_state.json"

    @property
    def overlay_state_path(self) -> Path:
        return self._signal_path.parent / "overlay_state.json"

    @property
    def exploration_root(self) -> Path:
        local = os.getenv("LOCALAPPDATA")
        base = Path(local) if local else self._signal_path.parent
        return (base / "ScrapMechanicMapCompanion" / "Exploration").resolve()

    def _overlay_state(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "visible": self._visible,
            "minimapVisible": self._minimap_visible,
            "inGameWorld": self._in_game_world,
            "nativeCapture": self._native_capture_enabled,
            "directRenderer": self._direct_renderer_enabled,
            "livePush": self._live_push_sink is not None,
            "mapReady": self._map_ready,
            "busy": self._map_busy,
            "message": self._map_message,
            "progress": self._map_progress,
            "requestSequence": self._request_sequence,
            "ackSequence": self._ack_sequence,
            "updated": time.time(),
        }

    def _write_overlay_state(self) -> None:
        state = self._overlay_state()
        try:
            self.overlay_state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.overlay_state_path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
            temporary.replace(self.overlay_state_path)
        except OSError:
            # The status file is advisory. A locked antivirus/indexer handle
            # must never be allowed to destabilize the overlay itself.
            pass

    @staticmethod
    def _rate_metrics(rows: tuple[float, ...], now: float) -> dict[str, float | int]:
        recent = tuple(value for value in rows if now - value <= 5.0)
        if len(recent) < 2:
            return {"count": len(recent), "hz": 0.0, "maxGapMs": 0.0}
        gaps = tuple((right - left) * 1000.0 for left, right in zip(recent, recent[1:]))
        return {
            "count": len(recent),
            "hz": round((len(recent) - 1) / max(0.001, recent[-1] - recent[0]), 2),
            "maxGapMs": round(max(gaps), 2),
        }

    def write_performance_metrics(self) -> None:
        if not self._metrics_enabled:
            return
        now = time.monotonic()
        with self._lock:
            live = tuple(self._metric_live_requests)
            pushes = tuple(self._metric_live_pushes)
            frames = tuple(self._metric_native_frames)
            samples = tuple(self._metric_memory_samples)
        value = {
            "updated": time.time(),
            "liveRequests": self._rate_metrics(live, now),
            "livePushes": self._rate_metrics(pushes, now),
            "nativeFrames": self._rate_metrics(frames, now),
            "memorySamples": self._rate_metrics(samples, now),
            "diagnostics": list(self._metric_diagnostics),
        }
        try:
            path = self._signal_path.parent / "performance.json"
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass

    def native_frame_presented(self) -> None:
        """Record a frame only after the native HUD surface displays it."""
        with self._lock:
            self._minimap_bitmap_ready = True
            if self._metrics_enabled:
                self._metric_native_frames.append(time.monotonic())

    def native_live_pushed(self) -> None:
        if self._metrics_enabled:
            with self._lock:
                self._metric_live_pushes.append(time.monotonic())

    def update_companion_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            requested = str(payload.get("id") or "")
            current_job = str((self._capture_job or {}).get("id") or "")
            # Progress delivery is asynchronous.  Once publication commits,
            # a late renderer packet must not regress ready -> unavailable.
            # A packet from a superseded save job is equally stale.
            if (self._map_ready and not self._map_busy) or (
                requested and requested != current_job
            ):
                return self._overlay_state()
            message = str(payload.get("message") or "Preparing this world's map…")
            try:
                progress = float(payload.get("progress") or 0.0)
            except (TypeError, ValueError):
                progress = 0.0
            self._map_message = message
            self._map_progress = max(0.0, min(1.0, progress))
            self._map_busy = bool(payload.get("busy", self._map_progress < 1.0))
            # Only the final, decoded-and-published image is allowed to set
            # this flag. Renderer progress reaching 100% is not map readiness.
            self._map_ready = bool(payload.get("mapReady", False))
            self._write_overlay_state()
            return self._overlay_state()

    def game_started(self) -> None:
        cached_map_ready = self._cached_map_matches_active_log()
        with self._lock:
            self._activation_serial += 1
            activation_serial = self._activation_serial
            self._game_started_wall = time.time()
            self._in_game_world = True
            self._world_needs_bind = True
            self._live_expected = None
            self._last_live_updated = 0.0
            self._live_session_ready_since = None
            self._live_pid = 0
            self._live_pid_checked = 0.0
            self._live_memory_state = None
            self._live_memory_sampled = 0.0
            self._minimap_bitmap_ready = self._direct_renderer_enabled
            self._last_exploration_position = None
            self._last_exploration_sampled = 0.0
            self._pending_exploration_position = None
            self._pending_exploration_started = 0.0
            self._pending_exploration_movement = 0.0
            self._session_save_snapshot = self._save_file_snapshot()
            self._map_ready = cached_map_ready
            self._display_ready = False
            self._map_busy = not cached_map_ready
            self._map_message = "" if cached_map_ready else "Finding the active Survival world…"
            self._map_progress = 1.0 if cached_map_ready else 0.0
            self._write_overlay_state()
        threading.Thread(
            target=self._activate_world_until_ready,
            args=(activation_serial,),
            name="ScrapMechanicMapActivation",
            daemon=True,
        ).start()

    def _cached_map_matches_active_log(self) -> bool:
        """Reuse a complete map immediately only for the logged active save."""
        saves = self._survival_save_rows()
        active = self._active_save_from_log(saves)
        if active is None:
            return False
        return self._ready_cache_matches(str(active.get("path") or ""))

    def _ready_cache_matches(self, save_path: str, seed: int | None = None) -> bool:
        """Validate both display images without decoding or rewriting them."""
        config = self._read_config()
        image = str(config.get("externalMap") or "")
        minimap = str(config.get("minimapMap") or image)
        if not config.get("ready") or not image or not minimap:
            return False
        if str(config.get("sourceSave") or "").casefold() != Path(save_path).name.casefold():
            return False
        if config.get("sourcePath") and str(Path(config["sourcePath"]).resolve()).casefold() != str(Path(save_path).resolve()).casefold():
            return False
        if seed is not None and int(config.get("sourceSeed") or 0) != int(seed):
            return False
        try:
            return self._path_from_uri(image).is_file() and self._path_from_uri(minimap).is_file()
        except (OSError, ValueError):
            return False

    def _restore_ready_cache(self, save_path: str, seed: int, *, session: int | None = None) -> bool:
        """Keep an already-published world map hot without map-meta/PIL work."""
        session = self._activation_serial if session is None else session
        if session != self._activation_serial:
            return False
        if not self._ready_cache_matches(save_path, seed):
            return False
        identity = f"{str(Path(save_path).resolve()).casefold()}|{int(seed)}"
        if self._map_job_identity != identity and self._read_config().get("mapQuality") == "preview" and self._editor is not None:
            generated = self.editor.dispatch("map-meta", COMPANION_MAP_OPTIONS)
            if generated.get("image") and not generated.get("needsCapture"):
                self.publish_companion_map({
                    "image": generated["image"], "bounds": generated["bounds"],
                    "sourceSave": Path(save_path).name, "sourceSeed": seed,
                    "quality": "3d", "detailTiles": generated.get("detailTiles"),
                    "activationSerial": session,
                })
        with self._lock:
            if session != self._activation_serial:
                return False
            self._map_job_identity = identity
            self._capture_job = None
            self._capture_claimed = False
            changed = not self._map_ready or self._map_busy or self._map_progress != 1.0
            self._map_ready = True
            self._map_busy = False
            self._map_message = ""
            self._map_progress = 1.0
            if changed:
                self._write_overlay_state()
        return True

    def game_left_world(self) -> None:
        """Clear live HUD state when Scrap Mechanic returns to its menu."""
        with self._lock:
            self._activation_serial += 1
            # Stop accepting process-memory samples before saving.  The game
            # briefly exposes menu/camera coordinates during the transition;
            # accepting one used to uncover an isolated circle near the
            # centre of every map.
            self._in_game_world = False
            self._world_needs_bind = True
            self._map_job_serial += 1
            self._map_job_identity = ""
            self._game_started_wall = None
            self._map_ready = False
            self._display_ready = False
            self._map_busy = False
            self._map_message = "No Survival world is loaded"
            self._live_memory_state = None
            self._live_memory_sampled = 0.0
            self._minimap_bitmap_ready = False
            self._live_expected = None
            self._last_exploration_position = None
            self._last_exploration_sampled = 0.0
            self._pending_exploration_position = None
            self._pending_exploration_started = 0.0
            self._pending_exploration_movement = 0.0
            self._write_overlay_state()
        self.hide(restore_game_focus=False, force=True)
        self.set_minimap_visible(False, force=True)
        self._save_exploration()
        self._live_tracker.close()
        self._live_pid = 0
        self._live_pid_checked = 0.0

    def game_stopped(self) -> None:
        with self._lock:
            self._activation_serial += 1
            self._map_job_serial += 1
            self._map_job_identity = ""
            self._game_started_wall = None
            self._in_game_world = False
            self._world_needs_bind = True
            self._live_expected = None
            self._map_ready = False
            self._display_ready = False
            self._map_busy = False
            self._map_message = "Scrap Mechanic is not running"
            self._live_memory_state = None
            self._live_memory_sampled = 0.0
            self._minimap_bitmap_ready = False
            self._write_overlay_state()
        # Hide before disk IO or renderer teardown can delay cleanup.
        self.hide(restore_game_focus=False, force=True)
        self.set_minimap_visible(False, force=True)
        self._save_exploration()
        self._live_tracker.close()
        self._live_pid = 0
        self._live_pid_checked = 0.0
        enable_game_windows(True)

    @property
    def snapshot_root(self) -> Path:
        local_app_data = os.getenv("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else self._signal_path.parent
        return (base / "ScrapMechanicMapCompanion" / "Runtime" / "Snapshots").resolve()

    def _pending(self, message: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
        current: dict[str, Any] = {}
        if self._loaded_path and self._editor is not None:
            try:
                current = self.editor.overview({}).get("current", {})
            except Exception:
                current = {}
        return {
            "changed": False,
            "pending": True,
            "message": message,
            "save": selected or {},
            "current": current,
        }

    @staticmethod
    def _source_fingerprint(path: Path) -> tuple[tuple[str, int, int], ...]:
        """Return metadata for the database and every SQLite sidecar.

        This intentionally uses only filesystem metadata.  It never asks
        SQLite to open the live game database and never acquires a SQLite
        byte-range lock.
        """
        rows: list[tuple[str, int, int]] = []
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = path if not suffix else path.with_name(path.name + suffix)
            try:
                stat = candidate.stat()
            except OSError:
                continue
            rows.append((suffix, int(stat.st_mtime_ns), int(stat.st_size)))
        return tuple(rows)

    def _save_file_snapshot(self) -> dict[str, tuple[tuple[str, int, int], ...]]:
        result: dict[str, tuple[tuple[str, int, int], ...]] = {}
        for row in self._survival_save_rows():
            path = Path(str(row.get("path") or ""))
            try:
                resolved = path.resolve(strict=True)
                result[str(resolved).casefold()] = self._source_fingerprint(resolved)
            except OSError:
                continue
        return result

    @staticmethod
    def _survival_save_rows() -> list[dict[str, Any]]:
        appdata = os.getenv("APPDATA")
        if not appdata:
            return []
        root = Path(appdata) / "Axolot Games" / "Scrap Mechanic" / "User"
        rows: list[dict[str, Any]] = []
        for path in root.glob("User_*/Save/Survival/*.db") if root.is_dir() else []:
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append({
                "path": str(path.resolve()),
                "name": path.stem,
                "mode": "survival",
                "modified": stat.st_mtime,
                "size": stat.st_size,
            })
        return sorted(rows, key=lambda row: float(row["modified"]), reverse=True)

    def _active_save_from_log(self, saves: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Use Scrap Mechanic's own log message to identify the selected save."""
        if self._game_root is None:
            return None
        try:
            log_path = max(
                (self._game_root / "Logs").glob("game-*.log"),
                key=lambda path: path.stat().st_mtime_ns,
            )
            with log_path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - 2 * 1024 * 1024), os.SEEK_SET)
                text = stream.read().decode("utf-8", errors="ignore")
        except (OSError, ValueError):
            return None
        matches = re.findall(r"Opening save file:\s*['\"]([^'\"]+\.db)['\"]", text)
        if not matches:
            return None
        logged = str(Path(matches[-1]).resolve()).casefold()
        return next(
            (row for row in saves if str(Path(str(row["path"])).resolve()).casefold() == logged),
            None,
        )

    @staticmethod
    def _copy_shared_read(source: Path, destination: Path) -> None:
        """Copy a file without taking or participating in SQLite locks."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            shutil.copyfile(source, destination)
            return

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.ReadFile.argtypes = (
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        )
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.GetLastError.restype = wintypes.DWORD

        # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE is essential:
        # the copy must never prevent Scrap Mechanic from writing or replacing
        # its database and journal files.
        handle = kernel32.CreateFileW(
            str(source), 0x80000000, 0x00000007, None, 3, 0x08000080, None
        )
        invalid = wintypes.HANDLE(-1).value
        if handle == invalid:
            raise OSError(int(kernel32.GetLastError()), f"Could not snapshot {source.name}")
        buffer = ctypes.create_string_buffer(1024 * 1024)
        read = wintypes.DWORD()
        try:
            with destination.open("wb") as output:
                while True:
                    if not kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
                        raise OSError(int(kernel32.GetLastError()), f"Could not read {source.name}")
                    if read.value == 0:
                        break
                    output.write(buffer.raw[: read.value])
        finally:
            kernel32.CloseHandle(handle)

    def _remove_snapshot_tree(self, path: Path) -> None:
        root = self.snapshot_root
        resolved = path.resolve()
        if resolved.parent != root or not resolved.name.startswith("snapshot-"):
            raise ValueError(f"Refusing to remove unexpected snapshot path: {resolved}")
        if resolved.is_dir():
            shutil.rmtree(resolved)

    def _create_save_snapshot(self, source: Path, attempts: int = 3) -> Path:
        """Create and validate a private SQLite copy of an active game save.

        Only the private copy is opened with SQLite.  A before/after metadata
        match rejects a copy made across an in-progress game write.
        """
        source = source.resolve(strict=True)
        root = self.snapshot_root
        root.mkdir(parents=True, exist_ok=True)
        source_tag = hashlib.sha1(str(source).casefold().encode("utf-8")).hexdigest()[:12]
        last_error: Exception | None = None

        for attempt in range(max(1, attempts)):
            before = self._source_fingerprint(source)
            if not before or before[0][0] != "" or before[0][2] < 4096:
                raise RuntimeError("The save database is not ready for a safe snapshot")
            folder = root / f"snapshot-{source_tag}-{time.time_ns()}-{attempt}"
            folder.mkdir(parents=False, exist_ok=False)
            copied = folder / source.name
            try:
                for suffix, modified_ns, _size in before:
                    source_file = source if not suffix else source.with_name(source.name + suffix)
                    destination_file = copied if not suffix else copied.with_name(copied.name + suffix)
                    self._copy_shared_read(source_file, destination_file)
                    os.utime(destination_file, ns=(modified_ns, modified_ns))

                after = self._source_fingerprint(source)
                if before != after:
                    raise RuntimeError("Scrap Mechanic changed the save while it was being copied")

                # Normal read/write mode is deliberate on the private copy: it
                # lets SQLite recover a copied hot journal/WAL.  The live file
                # is never passed to sqlite3.connect.
                connection = sqlite3.connect(str(copied), timeout=0.1)
                try:
                    integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                finally:
                    connection.close()
                if integrity != "ok":
                    raise RuntimeError(f"Snapshot quick_check failed: {integrity}")
                return copied
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                last_error = exc
                self._remove_snapshot_tree(folder)
                if attempt + 1 < max(1, attempts):
                    time.sleep(0.05)

        raise RuntimeError(str(last_error or "Could not make a consistent save snapshot"))

    def _prune_snapshots(self, keep: set[Path], maximum: int = 3) -> None:
        root = self.snapshot_root
        keep_resolved = {path.resolve() for path in keep}
        try:
            candidates = sorted(
                (path for path in root.iterdir() if path.is_dir() and path.name.startswith("snapshot-")),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            return
        retained = 0
        for candidate in candidates:
            if candidate.resolve() in keep_resolved or retained < maximum:
                retained += 1
                continue
            try:
                self._remove_snapshot_tree(candidate)
            except OSError:
                continue

    def _load_snapshot(self, source: Path) -> dict[str, Any]:
        snapshot = self._create_save_snapshot(source)
        try:
            loaded = self.editor.load_save({"path": str(snapshot)})
        except Exception:
            self._remove_snapshot_tree(snapshot.parent)
            raise
        previous = Path(self._snapshot_path).parent if self._snapshot_path else None
        self._snapshot_path = str(snapshot)
        keep = {snapshot.parent}
        if previous and previous.is_dir():
            keep.add(previous)
        self._prune_snapshots(keep)
        return loaded

    def _refresh_live_context(self) -> None:
        """Cache the save-derived context outside the rapid minimap path."""
        expected: tuple[float, float, float] | None = None
        world_id = 1
        waypoints: list[dict[str, Any]] = []
        editor = self._editor
        save = getattr(editor, "save", None)
        if save is not None and save.players:
            player = save.players[0]
            expected = (float(player.x), float(player.y), float(player.z))
            world_id = int(player.world_id)
            try:
                rows = editor.map_features({"kind": "Places", "worldId": world_id, "limit": 2000})
                waypoints = [
                    row for row in rows.get("features", [])
                    if row.get("kind") != "Player"
                ]
            except Exception:
                waypoints = []
        with self._lock:
            self._live_expected = expected
            self._live_world_id = world_id
            self._live_waypoints = waypoints

    def _exploration_path(self, identity: str | None = None) -> Path:
        value = identity or self._exploration_identity or "unknown-world"
        digest = hashlib.sha1(value.casefold().encode("utf-8")).hexdigest()[:24]
        return self.exploration_root / f"{digest}.json"

    def _load_exploration(self, source: Path, seed: int) -> None:
        # A session handoff must wait for the last asynchronous flush before
        # reading history, even when that flush already cleared the dirty flag.
        with self._exploration_io_lock:
            if not self._save_exploration():
                raise OSError("Exploration history could not be saved; world binding will retry")
            self._read_exploration(source, seed)

    def _read_exploration(self, source: Path, seed: int) -> None:
        identity = f"{source.resolve()}|{int(seed)}"
        cells: set[str] = set()
        points: set[str] = set()
        sight: set[str] = set()
        discovered: set[str] = set()
        schema = 1
        try:
            value = json.loads(self._exploration_path(identity).read_text(encoding="utf-8"))
            if isinstance(value, dict):
                schema = int(value.get("schema") or 1)
                for item in value.get("cells", []):
                    if not isinstance(item, str):
                        continue
                    try:
                        gx, gy = (int(part) for part in item.split(",", 1))
                    except (TypeError, ValueError):
                        continue
                    cells.add(f"{gx},{gy}")
                for item in value.get("points", []):
                    if not isinstance(item, str):
                        continue
                    try:
                        px, py = (int(part) for part in item.split(",", 1))
                    except (TypeError, ValueError):
                        continue
                    points.add(f"{px},{py}")
                for item in value.get("sight", []):
                    if not isinstance(item, str):
                        continue
                    try:
                        px, py, heading = (int(part) for part in item.split(",", 2))
                    except (TypeError, ValueError):
                        continue
                    sight.add(f"{px},{py},{heading % 360}")
                discovered = {
                    str(item) for item in value.get("discovered", []) if isinstance(item, str)
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        # Versions before schema 2 could record one menu/camera sample as a
        # remote, isolated cell when leaving a world.  Remove only single
        # legacy outliers that are many cells away from every other sample;
        # real explored routes and legitimate clusters remain untouched.
        if schema < 2 and len(cells) >= 4:
            parsed = {
                key: tuple(int(part) for part in key.split(",", 1))
                for key in cells
            }
            isolated = {
                key for key, (gx, gy) in parsed.items()
                if min(
                    max(abs(gx - ox), abs(gy - oy))
                    for other, (ox, oy) in parsed.items() if other != key
                ) > 8
            }
            cells.difference_update(isolated)

        radius = float(self._settings.get("fogRadius") or 192)
        reveal_shape = str(self._settings.get("revealShape") or "directional")
        sight_rows = [tuple(int(part) for part in key.split(",", 2)) for key in sight]
        sight_positions = {f"{px},{py}" for px, py, _heading in sight_rows}
        # Modern histories use exact arrow-centred, directional samples.
        # Retain circular points only for legacy files that have no facing for
        # that position, and cells only for the oldest cell-only schema.
        history = [
            (float(int(key.split(",", 1)[0])), float(int(key.split(",", 1)[1])))
            for key in points if key not in sight_positions
        ]
        if not points and not sight:
            history.extend(
                ((int(key.split(",", 1)[0]) + 0.5) * 64.0,
                 (int(key.split(",", 1)[1]) + 0.5) * 64.0)
                for key in cells
            )
        # Recover POIs for exploration files made before waypoint discovery
        # was implemented. This save-derived pass is small (normally <100
        # POIs) and runs once per world, not in the live sampling loop.
        for row in self._live_waypoints:
            key = str(row.get("key") or "")
            if not key:
                continue
            if row.get("category") == "beacons":
                discovered.add(key)
                continue
            wx = float(row.get("worldX", float(row.get("x") or 0.0) * 64.0))
            wy = float(row.get("worldY", float(row.get("y") or 0.0) * 64.0))
            directional_hit = any(
                reveal_contains(
                    float(px), float(py), wx, wy,
                    math.sin(math.radians(heading)),
                    math.cos(math.radians(heading)),
                    radius,
                    reveal_shape,
                )
                for px, py, heading in sight_rows
            )
            if directional_hit or any(
                math.hypot(wx - hx, wy - hy) <= radius for hx, hy in history
            ):
                discovered.add(key)
        with self._lock:
            self._exploration_identity = identity
            self._explored_cells = cells
            self._explored_points = points
            self._explored_sight = sight
            self._discovered_features = discovered
            # Player beacons are deliberately placed navigation aids and are
            # known immediately, while generated POIs still require discovery.
            self._discovered_features.update(
                str(row.get("key")) for row in self._live_waypoints
                if row.get("category") == "beacons" and row.get("key")
            )
            self._exploration_revision += 1
            self._exploration_dirty = schema < 3
            self._last_exploration_position = None
            self._last_exploration_sampled = 0.0
            self._pending_exploration_position = None
            self._pending_exploration_started = 0.0
            self._pending_exploration_movement = 0.0
            self._context_revision += 1

    def _save_exploration(self) -> bool:
        with self._exploration_io_lock:
            return self._write_exploration()

    def _write_exploration(self) -> bool:
        with self._lock:
            if not self._exploration_dirty or not self._exploration_identity:
                return True
            identity = self._exploration_identity
            # Copy under the live-state lock, then sort/encode/write outside
            # it.  Large explored histories used to pause the 60 Hz sampler
            # while thousands of strings were sorted and JSON encoded.
            cells = tuple(self._explored_cells)
            points = tuple(self._explored_points)
            sight = tuple(self._explored_sight)
            discovered = tuple(self._discovered_features)
            self._exploration_dirty = False
            self._exploration_saved = time.monotonic()
        value = {
            "schema": 3,
            "identity": identity,
            "cellSize": 64,
            "pointSize": 4,
            "cells": sorted(cells),
            "points": sorted(points),
            "sight": sorted(sight),
            "discovered": sorted(discovered),
            "updated": time.time(),
        }
        try:
            path = self._exploration_path(identity)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            with self._lock:
                self._exploration_dirty = True
            return False
        return True

    def _persist_exploration(self) -> None:
        """Persist fog history without ever blocking the live sampler."""
        while not self._live_sampler_stop.is_set():
            self._exploration_save_event.wait(0.5)
            self._exploration_save_event.clear()
            if self._live_sampler_stop.is_set():
                return
            with self._lock:
                dirty = self._exploration_dirty
                remaining = max(
                    0.0, 1.5 - (time.monotonic() - self._exploration_saved)
                )
            if not dirty:
                continue
            if remaining > 0.0 and self._live_sampler_stop.wait(remaining):
                return
            self._save_exploration()

    def _update_exploration(
        self,
        position: dict[str, Any],
        direction: dict[str, Any] | None = None,
    ) -> tuple[list[int], list[str]]:
        x = float(position.get("x") or 0.0)
        y = float(position.get("y") or 0.0)
        gx, gy = math.floor(x / 64.0), math.floor(y / 64.0)
        cell_key = f"{gx},{gy}"
        point_x, point_y = round(x / 4.0) * 4, round(y / 4.0) * 4
        point_key = f"{point_x},{point_y}"
        heading = direction_heading_degrees(direction)
        sight_key = f"{point_x},{point_y},{heading}"
        discovered_now: list[str] = []
        changed = False
        radius = float(self._settings.get("fogRadius") or 192)
        reveal_shape = str(self._settings.get("revealShape") or "directional")
        now = time.monotonic()
        with self._lock:
            if not self._in_game_world or not math.isfinite(x) or not math.isfinite(y):
                last = self._last_exploration_position
                return (
                    [math.floor(last[0] / 64.0), math.floor(last[1] / 64.0)]
                    if last is not None else [gx, gy]
                ), discovered_now

            # A real 60 Hz player sample cannot jump hundreds of metres. The
            # game does briefly publish such camera/menu coordinates while a
            # world closes. Reject that discontinuity so it cannot become a
            # permanent fog hole. A genuine teleport is accepted only after
            # the new position starts moving for a sustained interval.
            last = self._last_exploration_position
            expected = self._live_expected
            if (
                last is None
                and expected is not None
                and math.hypot(x - expected[0], y - expected[1]) > 256.0
            ):
                # The first transform after a load must agree with the player
                # row from that exact Survival save. Menu and unload camera
                # records can otherwise become the session's trusted origin,
                # bypassing the ordinary subsequent-jump quarantine.
                return [gx, gy], discovered_now
            elapsed = max(0.0, now - self._last_exploration_sampled)
            jump_limit = max(160.0, elapsed * 140.0)
            if last is not None and math.hypot(x - last[0], y - last[1]) > jump_limit:
                pending = self._pending_exploration_position
                if pending is None or math.hypot(x - pending[0], y - pending[1]) > 96.0:
                    self._pending_exploration_position = (x, y)
                    self._pending_exploration_started = now
                    self._pending_exploration_movement = 0.0
                    return [math.floor(last[0] / 64.0), math.floor(last[1] / 64.0)], discovered_now
                self._pending_exploration_movement += math.hypot(x - pending[0], y - pending[1])
                self._pending_exploration_position = (x, y)
                if now - self._pending_exploration_started < 2.0 or self._pending_exploration_movement < 4.0:
                    return [math.floor(last[0] / 64.0), math.floor(last[1] / 64.0)], discovered_now
            self._last_exploration_position = (x, y)
            self._last_exploration_sampled = now
            self._pending_exploration_position = None
            self._pending_exploration_started = 0.0
            self._pending_exploration_movement = 0.0
            if cell_key not in self._explored_cells:
                self._explored_cells.add(cell_key)
                changed = True
            if point_key not in self._explored_points:
                self._explored_points.add(point_key)
                changed = True
            if sight_key not in self._explored_sight:
                self._explored_sight.add(sight_key)
                changed = True
            direction_x = math.sin(math.radians(heading))
            direction_y = math.cos(math.radians(heading))
            for row in self._live_waypoints:
                key = str(row.get("key") or "")
                if not key or key in self._discovered_features:
                    continue
                wx = float(row.get("worldX", float(row.get("x") or 0.0) * 64.0))
                wy = float(row.get("worldY", float(row.get("y") or 0.0) * 64.0))
                if row.get("category") == "beacons" or reveal_contains(
                    x, y, wx, wy, direction_x, direction_y, radius, reveal_shape
                ):
                    self._discovered_features.add(key)
                    discovered_now.append(key)
                    changed = True
            if changed:
                self._exploration_revision += 1
                self._exploration_dirty = True
            should_save = self._exploration_dirty and time.monotonic() - self._exploration_saved >= 1.5
        if should_save:
            self._exploration_save_event.set()
        return [gx, gy], discovered_now

    def exploration_state(self) -> dict[str, Any]:
        settings = self.companion_settings()
        with self._lock:
            cells = [list(map(int, item.split(",", 1))) for item in self._explored_cells]
            points = [list(map(int, item.split(",", 1))) for item in self._explored_points]
            samples = [list(map(int, item.split(",", 2))) for item in self._explored_sight]
            return {
                "enabled": bool(settings.get("fogOfWar", True)),
                "cellSize": 64,
                "radius": int(settings.get("fogRadius") or 192),
                "cells": cells,
                "points": points,
                "samples": samples,
                "discovered": sorted(self._discovered_features),
                "revision": self._exploration_revision,
            }

    def _sample_live_process(self) -> None:
        """Sample one validated 40-byte player record independently at 60 Hz.

        WebView RPC calls now only copy this cache. They never enumerate
        processes, scan memory, or wait behind the window-monitor loop.
        """
        period = 1.0 / 60.0
        while not self._live_sampler_stop.is_set():
            started = time.monotonic()
            with self._lock:
                game_running = self._in_game_world and self._game_started_wall is not None and not self._world_needs_bind and self._live_expected is not None
                session = self._activation_serial
                pid = self._live_pid
                pid_checked = self._live_pid_checked
                expected = self._live_expected
                world_id = self._live_world_id
            if game_running:
                now = time.monotonic()
                if not pid or now - pid_checked >= 0.5:
                    process_ids = scrap_mechanic_process_ids()
                    pid = min(process_ids) if process_ids else 0
                    with self._lock:
                        self._live_pid = pid
                        self._live_pid_checked = now
                    if not pid:
                        with self._lock:
                            self._live_memory_state = None
                            self._live_memory_sampled = 0.0
                memory_state = self._live_tracker.read(pid, expected) if pid else None
                if memory_state is not None:
                    with self._lock:
                        if session != self._activation_serial or self._world_needs_bind or not self._in_game_world:
                            continue
                        exploration_cell, _discovered_now = self._update_exploration(
                            memory_state["position"], memory_state["direction"]
                        )
                        discovered = sorted(self._discovered_features)
                        exploration_revision = self._exploration_revision
                    state = {
                        "schema": 4,
                        "source": "game-memory",
                        "position": memory_state["position"],
                        "direction": memory_state["direction"],
                        "worldId": world_id,
                        "minimapHidden": False,
                        "explorationCell": exploration_cell,
                        "explorationSample": [
                            round(float(memory_state["position"]["x"]) / 4.0) * 4,
                            round(float(memory_state["position"]["y"]) / 4.0) * 4,
                            direction_heading_degrees(memory_state["direction"]),
                        ],
                        "discovered": discovered,
                        "explorationRevision": exploration_revision,
                    }
                    with self._lock:
                        if session != self._activation_serial or self._world_needs_bind or not self._in_game_world:
                            continue
                        self._live_memory_state = {
                            "available": True,
                            "state": state,
                            "updated": float(memory_state["updated"]),
                        }
                        self._live_memory_sampled = time.monotonic()
                        live_push_sink = self._live_push_sink
                        if self._metrics_enabled:
                            self._metric_memory_samples.append(self._live_memory_sampled)
                    if live_push_sink is not None:
                        live_push_sink({
                            "live": {
                                "available": True,
                                "state": state,
                                "updated": float(memory_state["updated"]),
                                "age": 0.0,
                            },
                            "contextRevision": self._context_revision,
                            "overlay": self._overlay_state(),
                            "settings": self.companion_settings(),
                        })
            elapsed = time.monotonic() - started
            remaining = period - elapsed
            if remaining > 0.0:
                # ``threading.Event.wait`` is implemented through a Windows
                # kernel-object timeout.  A 16 ms timeout can round up to the
                # next scheduler quantum and was measured here as ~31 ms,
                # halving an otherwise sub-millisecond tracker to ~32 Hz.
                # Python 3.11+ uses a high-resolution waitable timer for
                # ``time.sleep`` on Windows, so it holds the intended cadence.
                # Shutdown still observes at most one 16.7 ms sample period.
                time.sleep(remaining)
            elif self._live_sampler_stop.is_set():
                return
            # Do not force a nominal 1 ms wait after an over-budget cycle.
            # Windows can round that wait to an entire ~15.6 ms scheduler
            # quantum, which turned a 17 ms memory read into a repeatable
            # 32 ms source frame and made the HUD visibly half-rate.  The
            # completed read already prevents this branch from busy-spinning.

    def _activate_world_until_ready(self, serial: int) -> None:
        """Bind the current Survival save without a throttled browser timer."""
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            with self._lock:
                if serial != self._activation_serial or not self._in_game_world:
                    return
            result = self.activate_current_save()
            if not result.get("pending"):
                return
            time.sleep(0.25)

    def activate_current_save(self) -> dict[str, Any]:
        if not self._activation_lock.acquire(blocking=False):
            return self._pending("Binding the active Survival world…")
        try:
            return self._activate_current_save_once()
        finally:
            self._activation_lock.release()

    def _activate_current_save_once(self) -> dict[str, Any]:
        """Find the active save and parse only an external validated snapshot."""
        session = self._activation_serial
        if self._game_started_wall is None:
            return self._pending("Waiting for Scrap Mechanic to start…")

        saves = self._survival_save_rows()
        if not saves:
            return self._pending("Waiting for the new Survival save database to be created…")
        changed = []
        for row in saves:
            candidate_path = Path(str(row["path"])).resolve()
            if not candidate_path.is_file():
                continue
            baseline = self._session_save_snapshot.get(str(candidate_path).casefold())
            current_stamp = self._source_fingerprint(candidate_path)
            if baseline is None or current_stamp != baseline:
                changed.append(row)
        logged = self._active_save_from_log(saves)
        detected = [logged] if logged else changed
        if not detected:
            return self._pending("Waiting for Scrap Mechanic to report the selected Survival save…")
        selected = max(detected, key=lambda row: float(row.get("modified") or 0))
        candidate = Path(str(selected["path"])).resolve()
        try:
            stat = candidate.stat()
        except OSError:
            return self._pending("The new save is still being created…", selected)
        stamp = int(stat.st_mtime_ns)
        different_save = str(candidate).casefold() != self._loaded_path.casefold()
        fingerprint = self._source_fingerprint(candidate)
        changed_on_disk = fingerprint != self._loaded_fingerprint
        if session != self._activation_serial:
            return self._pending("The Survival session changed while finding its save")

        if different_save or self._world_needs_bind:
            try:
                self._last_snapshot_attempt = time.monotonic()
                loaded = self._load_snapshot(candidate)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                return self._pending(f"Waiting for a consistent external save snapshot ({exc})", selected)
            if session != self._activation_serial or self._game_started_wall is None:
                return self._pending("The Survival world closed while its snapshot was being read")
            self._loaded_path = str(candidate)
            self._loaded_stamp = stamp
            self._loaded_fingerprint = fingerprint
            current = loaded["current"]
            self._refresh_live_context()
            if self._live_expected is None:
                return self._pending("Waiting for this Survival world's player record…", selected)
            try:
                self._load_exploration(candidate, int(current.get("game", {}).get("seed") or 0))
            except OSError as exc:
                return self._pending(str(exc), selected)
            with self._lock:
                if session != self._activation_serial:
                    return self._pending("The Survival session changed while binding")
                self._world_needs_bind = False
            self._mark_map_pending(
                str(candidate), int(current.get("game", {}).get("seed") or 0)
            )
            seed = int(current.get("game", {}).get("seed") or 0)
            if not self._restore_ready_cache(str(candidate), seed, session=session):
                if session != self._activation_serial:
                    return self._pending("The Survival session changed while preparing its map")
                self._queue_map_prepare(str(candidate), current, session=session)
            return {"changed": True, "pending": False, "save": selected, "current": current}

        with self._lock:
            capture_in_progress = self._capture_job is not None and self._capture_claimed
        if capture_in_progress:
            # Render every section from one immutable save snapshot.  The
            # game autosaves frequently; swapping EditorApi snapshots during
            # capture reset caches and made progress stall or move backwards.
            current = self.editor.overview({})["current"]
            return {"changed": False, "pending": False, "save": selected, "current": current}

        if changed_on_disk:
            # Autosaves touch the database about every thirty seconds. Parsing
            # a fresh multi-megabyte SQLite snapshot on the WebView bridge
            # thread stalled both JS delivery and Chromium painting for a few
            # seconds, even though the world seed and cached terrain map cannot
            # change during the active session. Keep the immutable snapshot
            # selected when this world was entered and acknowledge only the
            # new disk fingerprint. Live position/direction continue through
            # the dedicated process-memory sampler without any autosave work.
            self._loaded_stamp = stamp
            self._loaded_fingerprint = fingerprint
        current = self.editor.overview({})["current"]
        seed = int(current.get("game", {}).get("seed") or 0)
        if not self._restore_ready_cache(str(candidate), seed, session=session):
            if session != self._activation_serial:
                return self._pending("The Survival session changed while preparing its map")
            self._queue_map_prepare(str(candidate), current, session=session)
        return {"changed": False, "pending": False, "save": selected, "current": current}

    def _read_config(self) -> dict[str, Any]:
        try:
            stamp = self.config_path.stat().st_mtime_ns
            if stamp == self._config_stamp:
                return dict(self._config_cache)
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            self._config_cache = (
                value if isinstance(value, dict) else {"schema": 3, "ready": False}
            )
            self._config_stamp = stamp
            return dict(self._config_cache)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"schema": 3, "ready": False}

    def _write_config(self, config: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
        temporary.replace(self.config_path)
        self._config_cache = dict(config)
        try:
            self._config_stamp = self.config_path.stat().st_mtime_ns
        except OSError:
            self._config_stamp = 0

    def _mark_map_pending(self, save_path: str, seed: int) -> None:
        config = self._read_config()
        save_name = Path(save_path).name
        stale_pending_fields = any(
            config.get(key) not in (None, "", [], {})
            for key in (
                "sourceMap", "externalMap", "minimapMap", "mapTexture",
                "mapWidth", "mapHeight", "minimapWidth", "minimapHeight",
                "worldMinX", "worldMaxX", "worldMinY", "worldMaxY",
                "cellBounds", "detailTiles",
            )
        )
        if (
            config.get("sourceSave") == save_name
            and int(config.get("sourceSeed") or 0) == seed
            and (bool(config.get("ready")) or not stale_pending_fields)
        ):
            return
        # A different save must never inherit the previous world's bitmap,
        # dimensions, bounds, or detail-tile metadata while its own map is
        # being rendered.  Publish one intentionally incomplete identity.
        config = {
            "schema": 3,
            "sourceSave": save_name,
            "sourceSeed": seed,
            "ready": False,
        }
        self._display_ready = False
        self._write_config(config)

    def _set_map_state(
        self,
        message: str,
        progress: float,
        *,
        ready: bool = False,
        busy: bool = True,
    ) -> None:
        with self._lock:
            self._map_message = str(message)
            self._map_progress = max(0.0, min(1.0, float(progress)))
            self._map_ready = bool(ready)
            self._map_busy = bool(busy)
            self._write_overlay_state()
        # The minimap's browser-side pull loop discovers this state without a
        # cross-thread evaluate_js call.  pywebview/WinForms can terminate the
        # entire companion when evaluate_js is issued from a worker thread.
        if ready:
            pass

    def _queue_map_prepare(self, save_path: str, current: dict[str, Any], *, session: int | None = None) -> None:
        """Publish the exact terrain immediately, reusing a finished HD capture.

        HD generation belongs to the editor. Running a second 3D engine beside
        the game made first-use navigation depend on a multi-minute GPU job.
        """
        session = self._activation_serial if session is None else session
        if session != self._activation_serial:
            return
        save_name = Path(save_path).name
        seed = int(current.get("game", {}).get("seed") or 0)
        identity = f"{str(Path(save_path).resolve()).casefold()}|{seed}"
        try:
            generated = self.editor.dispatch("map-meta", COMPANION_MAP_OPTIONS)
            if generated.get("image"):
                self.publish_companion_map({
                    "image": generated["image"],
                    "bounds": generated["bounds"],
                    "sourceSave": save_name,
                    "sourceSeed": seed,
                    "quality": "preview" if generated.get("needsCapture") else "3d",
                    "detailTiles": generated.get("detailTiles"),
                    "activationSerial": session,
                })
                with self._lock:
                    if session != self._activation_serial:
                        return
                    self._map_job_identity = identity
                    self._capture_job = None
                    self._capture_claimed = False
                    self._set_map_state("", 1.0, ready=True, busy=False)
                return
            raise RuntimeError(generated.get("error") or "The terrain map is unavailable")
        except Exception as exc:
            with self._lock:
                if session == self._activation_serial:
                    self._set_map_state(
                        f"Could not load this world's map · {exc}",
                        0.0,
                        ready=False,
                        busy=False,
                    )
            return

    def capture_job(self) -> dict[str, Any]:
        with self._lock:
            if self._capture_job is None:
                return {"available": False}
            return {
                "available": not self._capture_claimed,
                "claimed": self._capture_claimed,
                "job": dict(self._capture_job),
            }

    def capture_claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            requested = str(payload.get("id") or "")
            if self._capture_job is None or requested != self._capture_job.get("id"):
                return {"claimed": False}
            if self._capture_claimed:
                return {"claimed": False}
            self._capture_claimed = True
            self._map_message = "Loading real 3D world sections…"
            self._map_progress = 0.02
            self._write_overlay_state()
            return {"claimed": True, "job": dict(self._capture_job)}

    def capture_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            requested = str(payload.get("id") or "")
            if self._capture_job is None or requested != self._capture_job.get("id"):
                raise ValueError("The completed 3D map belongs to an old world job")
            job = dict(self._capture_job)
        generated = self.editor.dispatch("map-meta", COMPANION_MAP_OPTIONS)
        if not generated.get("image") or generated.get("needsCapture"):
            raise RuntimeError("The real 3D PNG was not stored")
        published = self.publish_companion_map({
            "image": generated["image"],
            "bounds": generated["bounds"],
            "sourceSave": job["saveName"],
            "sourceSeed": job["seed"],
            "quality": "3d",
            "detailTiles": generated.get("detailTiles"),
        })
        with self._lock:
            self._capture_job = None
            self._capture_claimed = False
        self._set_map_state(
            "",
            1.0,
            ready=True,
            busy=False,
        )
        return published

    def capture_failed(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            requested = str(payload.get("id") or "")
            if self._capture_job is None or requested != self._capture_job.get("id"):
                return {"accepted": False}
            self._capture_claimed = False
            # Keep the same immutable save job available for the capture page
            # to retry.  Clearing it made the progress HUD disappear and M
            # permanently stop working after any finalization error, even
            # though the user had just watched rendering reach completion.
            self._capture_job["attempt"] = int(self._capture_job.get("attempt") or 1) + 1
            self._capture_job["finalizeOnly"] = bool(payload.get("stored"))
        self._set_map_state(
            f"Retrying map finalization · {payload.get('error') or 'unknown error'}",
            0.01,
            ready=False,
            busy=True,
        )
        return {"accepted": True, "retrying": True}

    @staticmethod
    def _path_from_uri(value: str) -> Path:
        text = value.strip()
        if not text:
            raise ValueError("The generated map path was empty")
        parsed = urlparse(text)
        if parsed.scheme.casefold() == "file":
            path = unquote(parsed.path)
            if os.name == "nt" and path.startswith("/"):
                path = path[1:]
            return Path(path).resolve()
        if not parsed.scheme or (os.name == "nt" and len(parsed.scheme) == 1):
            return Path(text).resolve()
        raise ValueError("The generated map did not point to a local file")

    @classmethod
    def _virtual_minimap_uri(cls, value: str) -> str:
        """Expose a cached map through WebView2's CORS-enabled local host."""
        try:
            cache_root = (
                Path(os.environ["LOCALAPPDATA"])
                / "ScrapMechanicSaveEditor" / "map-cache"
            ).resolve()
            parsed = urlparse(value)
            relative = cls._path_from_uri(value).relative_to(cache_root)
            virtual = (
                "https://scrap-companion.local/"
                + quote(relative.as_posix(), safe="/")
            )
            if parsed.query:
                virtual = f"{virtual}?{parsed.query}"
            return virtual
        except (KeyError, OSError, ValueError):
            return value

    def publish_companion_map(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Publish a lossless master plus an inexpensive minimap derivative."""
        image_value = str(payload.get("image") or "")
        if not image_value:
            current_meta = self.editor.map_meta(COMPANION_MAP_OPTIONS)
            image_value = str(current_meta.get("image") or "")
        source = self._path_from_uri(image_value)
        if not source.is_file():
            raise FileNotFoundError(source)
        bounds = [int(value) for value in payload.get("bounds", [])]
        if len(bounds) != 4:
            raise ValueError("The generated map bounds were missing")
        source_save = str(payload.get("sourceSave") or Path(self._loaded_path).name)
        source_seed = int(payload.get("sourceSeed") or 0)
        tag = hashlib.sha1(str(source).casefold().encode("utf-8")).hexdigest()[:14]
        texture_name = f"world_map_{tag}.png"

        from PIL import Image

        with Image.open(source) as loaded:
            source_size = loaded.size
        if source_size[0] <= 4096 and source_size[1] <= 4096:
            # The fast atlas and normal companion capture are already bounded.
            # Re-encoding a multi-megapixel PNG here used several more seconds
            # and doubled disk usage without improving the overlay.
            prepared = source
            prepared_size = source_size
        else:
            prepared = source.parent / f"{source.stem}-minimap-4096.png"
            if not prepared.is_file() or prepared.stat().st_mtime_ns < source.stat().st_mtime_ns:
                with Image.open(source) as loaded:
                    image = loaded.convert("RGB")
                image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                temporary = prepared.with_suffix(".tmp.png")
                image.save(temporary, "PNG", compress_level=2)
                temporary.replace(prepared)
            with Image.open(prepared) as loaded:
                prepared_size = loaded.size

        x_min, x_max, y_min, y_max = bounds
        config = {
            "schema": 3,
            "sourceSave": source_save,
            "sourcePath": self._loaded_path,
            "sourceSeed": source_seed,
            "sourceMap": source.name,
            "mapTexture": f"MapCompanion/{texture_name}",
            # The full map consumes the lossless 8192px master.  The always-
            # visible minimap consumes a bounded derivative so sharper zoom
            # does not add a 200+ MB texture to every HUD frame.
            "externalMap": source.resolve().as_uri(),
            "minimapMap": prepared.resolve().as_uri(),
            "ready": True,
            "mapQuality": str(payload.get("quality") or "detailed"),
            "mapWidth": int(source_size[0]),
            "mapHeight": int(source_size[1]),
            "minimapWidth": int(prepared_size[0]),
            "minimapHeight": int(prepared_size[1]),
            "worldMinX": x_min * 64,
            "worldMaxX": (x_max + 1) * 64,
            "worldMinY": y_min * 64,
            "worldMaxY": (y_max + 1) * 64,
            "cellBounds": bounds,
            "exploration": {"enabled": True, "cellSize": 64},
            "detailTiles": payload.get("detailTiles"),
        }
        with self._lock:
            expected_session = payload.get("activationSerial")
            if expected_session is not None and int(expected_session) != self._activation_serial:
                return {"cancelled": True}
            self._write_config(config)
            self._context_revision += 1
            self._display_ready = False
        return {"texture": config["mapTexture"], "image": config["externalMap"], "config": config}

    def live_state(self) -> dict[str, Any]:
        with self._lock:
            memory = dict(self._live_memory_state) if self._live_memory_state else None
            sampled = self._live_memory_sampled
            in_game_world = self._in_game_world
        if memory is not None:
            age = max(0.0, time.monotonic() - sampled)
            if age <= 10.0 and in_game_world:
                memory["age"] = age
                return memory

        # Prefer live client telemetry when the reversible game-side telemetry
        # hook is installed. This is an ordinary JSON file; the companion
        # never opens or locks Scrap Mechanic's live SQLite save.
        telemetry_paths: list[Path] = []
        if in_game_world:
            telemetry_paths.append(self.live_state_path)
        if in_game_world and self._game_root is not None:
            telemetry_paths.append(
                self._game_root
                / "Survival/Scripts/MapCompanion/MapCache/live_state.json"
            )
            telemetry_paths.append(
                self._game_root
                / "Survival/LocalBlueprints/MapCompanionLive.json"
            )
        for telemetry_path in telemetry_paths:
            try:
                stat = telemetry_path.stat()
                value = json.loads(telemetry_path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not isinstance(value.get("position"), dict):
                    continue
                age = max(0.0, time.time() - stat.st_mtime)
                if age > 3.0:
                    continue
                value["source"] = "game-telemetry"
                return {
                    "available": True,
                    "state": value,
                    "updated": stat.st_mtime,
                    "age": age,
                }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

        # Never replace a live transform with the last autosaved player row.
        # If the memory reader briefly loses its record, freezing/withholding
        # the marker is truthful; jumping a minute backwards is not.  Snapshot
        # positions remain useful to the editor but not to a running HUD.
        if in_game_world:
            return {"available": False, "state": {}, "updated": 0, "age": 9999}

        # No world is loaded (for example, Scrap Mechanic's main menu), so an
        # old save snapshot must not create a random minimap marker.
        return {"available": False, "state": {}, "updated": 0, "age": 9999}

    def minimap_state(self) -> dict[str, Any]:
        config = self._read_config()
        live = self.live_state()
        # Static navigation context belongs to the initial/full refresh.  It
        # is deliberately not stored in the 60 Hz transform cache: doing so
        # made every 30 Hz browser pull serialize dozens of full waypoint
        # dictionaries and periodically starved the HUD renderer.
        if isinstance(live.get("state"), dict):
            with self._lock:
                waypoints = list(self._live_waypoints)
                discovered = sorted(self._discovered_features)
            live = {**live, "state": {
                **live["state"],
                "waypoints": waypoints,
                "discovered": discovered,
            }}
        image = str(config.get("externalMap") or "")
        minimap_image = str(config.get("minimapMap") or image)
        if config.get("ready") and not image:
            texture = str(config.get("mapTexture") or "").replace("/", os.sep)
            if self._editor is not None and self.editor.game_root and texture:
                candidate = (
                    self.editor.game_root / "Data/Gui/Resolutions/3840x2160" / texture
                )
                if candidate.is_file():
                    image = candidate.resolve().as_uri()
        # The capture replaces its PNG atomically at a stable path.  Make the
        # file timestamp part of the URI so Chromium cannot retain the prior
        # decoded image after a new world map is published.
        if image:
            try:
                image_path = self._path_from_uri(image)
                if image_path.is_file():
                    separator = "&" if "?" in image else "?"
                    image = f"{image}{separator}v={image_path.stat().st_mtime_ns}"
            except (OSError, ValueError):
                pass
        if minimap_image:
            try:
                minimap_path = self._path_from_uri(minimap_image)
                if minimap_path.is_file():
                    separator = "&" if "?" in minimap_image else "?"
                    minimap_image = (
                        f"{minimap_image}{separator}v={minimap_path.stat().st_mtime_ns}"
                    )
            except (OSError, ValueError):
                pass
        minimap_image = self._virtual_minimap_uri(minimap_image)
        return {
            "contextRevision": self._context_revision,
            "config": config,
            "image": image,
            "minimapImage": minimap_image or image,
            "live": live,
            "overlay": self._overlay_state(),
            "settings": self.companion_settings(),
            "exploration": self.exploration_state(),
        }

    def minimap_live(self) -> dict[str, Any]:
        """Return only rapidly changing state for the 10–30 Hz minimap loop."""
        live = self.live_state()
        if isinstance(live.get("state"), dict):
            # Copy the compact transform before returning it so no browser
            # request can mutate the sampler's shared cache.  ``discovered``
            # is at most a short list of keys; the heavyweight waypoint rows
            # arrive once through ``minimap_state`` and are retained by JS.
            state = dict(live["state"])
            state.pop("waypoints", None)
            live = {**live, "state": state}
        return {
            "contextRevision": self._context_revision,
            "live": live,
            "overlay": self._overlay_state(),
            "settings": self.companion_settings(),
        }

    def companion_settings(self) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._settings_checked < 0.5:
            return dict(self._settings)
        self._settings_checked = now
        try:
            path = self._signal_path.parent.parent / "settings.json"
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        if stamp != self._settings_stamp:
            self._settings = load_companion_settings()
            self._settings_stamp = stamp
        return dict(self._settings)

    def minimap_allowed(self) -> bool:
        config = self._read_config()
        image = str(config.get("externalMap") or "")
        with self._lock:
            in_game_world = self._in_game_world
            bitmap_ready = self._minimap_bitmap_ready
            hidden_by_game = bool(
                (self._live_memory_state or {}).get("state", {}).get("minimapHidden")
            )
        has_map = bool(config.get("ready")) and bool(image)
        # The small window also owns the honest build progress indicator, so
        # it must be allowed before a position or finished map exists.
        return in_game_world and bitmap_ready and not hidden_by_game and (
            self._map_busy or self._map_ready or has_map
        )

    def set_minimap_visible(self, visible: bool, *, force: bool = False) -> None:
        changed = False
        with self._lock:
            if self._minimap_window is None:
                return
            if visible == self._minimap_visible:
                if not force:
                    return
            else:
                self._minimap_visible = visible
                self._minimap_foreground_active = visible
                changed = True
        # SetWindowPos can synchronously dispatch messages to the WinForms UI
        # thread. Never hold the bridge lock across that boundary: the UI
        # thread records completed frames under the same lock.
        if visible:
            show_window_by_title(MINIMAP_TITLE, True)
            configure_minimap_window(
                self.companion_settings(), retries=6, promote=True
            )
        else:
            show_window_by_title(MINIMAP_TITLE, False)
        if changed:
            self._write_overlay_state()
        # The visible minimap polls the cached state itself; this method is
        # intentionally free of pywebview calls from the monitor thread.

    def set_minimap_foreground(self, active: bool) -> None:
        """Move the resident HUD above/below other apps without hiding it.

        Hiding a WebView2 surface destroys its composition target
        asynchronously.  The previous focus monitor did that during every
        full-map handoff, which is the source of the long-lived blink.  A
        z-order transition preserves the rendered surface and is idempotent.
        """
        with self._lock:
            active = bool(active and self._minimap_visible and self._in_game_world)
            if active == self._minimap_foreground_active:
                return
            self._minimap_foreground_active = active
        set_window_topmost(MINIMAP_TITLE, active)

    def expose_minimap_after_full_map(self) -> None:
        """Reveal the already-resident HUD with one atomic z-order change."""
        with self._lock:
            active = bool(self._minimap_visible and self._in_game_world)
            if not active:
                return
            self._minimap_foreground_active = True
            # Record this separately from the logical topmost flag.  The full
            # map keeps the minimap TOPMOST underneath it, so closing the map
            # must still perform one explicit z-order raise even though the
            # flag already reads true.
            self._minimap_was_exposed = True
        set_window_topmost(MINIMAP_TITLE, True)

    def enforce_native_visibility(self) -> None:
        """Keep late WebView2 HWND transitions aligned with logical state."""
        repair_minimap_region = False
        minimap_settings: dict[str, Any] | None = None
        with self._lock:
            hide_full_map = self._window is not None and not self._visible
            hide_minimap = self._minimap_window is not None and (
                not self._minimap_visible or not self._in_game_world
            )
            if self._minimap_window is not None and self._minimap_visible and not hide_minimap:
                repair_minimap_region = True
                minimap_settings = dict(self._settings)
        if hide_full_map:
            show_window_by_title(FULL_MAP_TITLE, False)
        if hide_minimap:
            show_window_by_title(MINIMAP_TITLE, False)
        # WebView2 may replace its composition target shortly after show().
        # Validate the native clip after that asynchronous handoff so the
        # opaque WinForms host can never reappear around the rounded HUD.
        if repair_minimap_region:
            hwnd = find_window_by_title(MINIMAP_TITLE)
            if hwnd:
                _x, _y, size, _height = fit_minimap_bounds(
                    scrap_mechanic_client_bounds(), minimap_settings,
                )
                apply_minimap_window_region(hwnd, size, minimap_settings)

    def can_open_full_map(self) -> bool:
        # ``mapReady`` is published only after the renderer has atomically
        # replaced the final PNG.  Do not make M depend on a second WebView
        # image-decoding callback: hidden WebView2 windows can throttle that
        # callback indefinitely even though the complete local image already
        # exists.  The page keeps its own loading cover for the very short
        # decode, while this check still prevents opening before real map data
        # is available.
        if not self._in_game_world:
            return False
        config = self._read_config()
        image_value = str(config.get("externalMap") or "")
        if not config.get("ready") or not image_value:
            return False
        try:
            ready = self._path_from_uri(image_value).is_file()
            if ready:
                # The final file is the authoritative ready boundary.  The
                # companion can be restarted with a warm map cache, in which
                # case the process-local renderer flag starts false even
                # though the same finished map is already on the minimap.
                self._map_ready = True
                self._map_busy = False
            return ready
        except (OSError, ValueError):
            return False

    def show(self) -> bool:
        with self._lock:
            if self._window is None:
                return False
            if not self.can_open_full_map():
                self._map_message = "Finishing the map before it can be opened…"
                self._write_overlay_state()
                return False
            self._transition_serial += 1
            self._last_transition = time.monotonic()
            self._minimap_handoff_until = 0.0
            self._visible = True
            warm_minimap = self._in_game_world and self.minimap_allowed()
        # Native HWND calls can synchronously dispatch WinForms messages, so
        # they must remain outside the bridge lock used by frame completion.
        if warm_minimap:
            self.set_minimap_visible(True)
            self.set_minimap_foreground(True)
        if not show_window_by_title(FULL_MAP_TITLE, True):
            with self._lock:
                self._visible = False
            self._write_overlay_state()
            return False
        # Positioning is best-effort; the monitor repairs exact client bounds.
        configure_full_map_window(retries=4)
        self._write_overlay_state()
        focus_window_title(FULL_MAP_TITLE)
        return True

    def hide(self, *, restore_game_focus: bool = True, force: bool = False) -> None:
        resume_minimap = False
        with self._lock:
            if self._window is None:
                return
            was_visible = self._visible
            self._transition_serial += 1
            self._last_transition = time.monotonic()
            self._visible = False
            resume_minimap = bool(was_visible and self._in_game_world and self.minimap_allowed())
            if resume_minimap:
                # The HUD stays resident underneath the full map. Closing the
                # opaque map only changes z-order; it never recreates the
                # WebView2 surface, so there is nothing left to flicker.
                self._minimap_handoff_until = time.monotonic() + 2.0
            reveal_minimap = resume_minimap and not self._minimap_visible
        if was_visible or force:
            show_window_by_title(FULL_MAP_TITLE, False)
        if reveal_minimap:
            self.set_minimap_visible(True)
        self._write_overlay_state()
        if restore_game_focus and was_visible and self._in_game_world:
            restore_scrap_mechanic_focus()
        if resume_minimap:
            # Finalize the handoff only after the game owns keyboard/mouse
            # focus.  Promoting the HUD before focus restoration let the
            # subsequent game-window z-order operations cover it repeatedly,
            # producing the distinctive visible/hidden/visible blink train.
            self.expose_minimap_after_full_map()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
        # Make teardown visible immediately, before waiting for worker threads
        # or asking pywebview to dispose its hidden renderer windows.
        self.hide(restore_game_focus=False, force=True)
        self.set_minimap_visible(False, force=True)
        with self._lock:
            self._visible = False
            self._minimap_visible = False
            self._minimap_foreground_active = False
            self._in_game_world = False
            self._map_busy = False
            self._map_ready = False
            self._minimap_bitmap_ready = False
            self._write_overlay_state()
        self._live_sampler_stop.set()
        self._exploration_save_event.set()
        if self._live_sampler.is_alive() and threading.current_thread() is not self._live_sampler:
            self._live_sampler.join(timeout=1.0)
        if (
            self._exploration_saver.is_alive()
            and threading.current_thread() is not self._exploration_saver
        ):
            self._exploration_saver.join(timeout=1.0)
        self._save_exploration()
        self._live_tracker.close()
        enable_game_windows(True)
        if self._live_push_host is not None:
            self._live_push_host.close()
        if self._full_map_live_push_host is not None:
            self._full_map_live_push_host.close()
        try:
            if self._minimap_window is not None:
                self._minimap_window.destroy()
        except Exception:
            pass
        try:
            if self._minimap_renderer_window is not None:
                self._minimap_renderer_window.destroy()
        except Exception:
            pass
        try:
            if self._capture_window is not None:
                self._capture_window.destroy()
        except Exception:
            pass
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass


def _window_text(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def find_window_by_title(title: str) -> int | None:
    if sys.platform != "win32":
        return None
    found: list[int] = []
    user32 = ctypes.windll.user32
    user32.EnumWindows.argtypes = (ctypes.c_void_p, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _lparam: int) -> bool:
        if _window_text(hwnd) == title:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def root_window_handle(hwnd: int | None) -> int | None:
    """Resolve a WebView/WinForms child handle to its native top-level form."""
    if sys.platform != "win32" or not hwnd:
        return hwnd
    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    root = user32.GetAncestor(wintypes.HWND(hwnd), 2)  # GA_ROOT
    return int(root or hwnd)


def show_window_by_title(title: str, visible: bool, retries: int = 3) -> bool:
    """Show or hide a companion HWND without touching WebView from a worker.

    pywebview's ``Window.show``/``hide`` marshals through WinForms.  Calling
    it from the key-monitor thread intermittently raises
    ``InvalidAsynchronousStateException`` and terminates that monitor, after
    which M appears completely dead.  Win32 ShowWindow is safe here and does
    not access WebView2's thread-affine controller.
    """
    if sys.platform != "win32":
        return False
    hwnd = None
    for attempt in range(max(1, retries)):
        hwnd = find_window_by_title(title)
        if hwnd:
            break
        if attempt + 1 < retries:
            time.sleep(0.04)
    if not hwnd:
        return False
    hwnd = root_window_handle(hwnd) or hwnd
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow(hwnd, 8 if visible else 0)  # SW_SHOWNA / SW_HIDE
    # ShowWindow returns the *previous* state, not success. Verify the result.
    for _ in range(4):
        if bool(user32.IsWindowVisible(hwnd)) == bool(visible):
            return True
        time.sleep(0.015)
    return bool(user32.IsWindowVisible(hwnd)) == bool(visible)


class NativeMinimapLivePush:
    """Coalesce 60 Hz state onto WebView2's one-way native message channel."""

    def __init__(self, renderer_form: Any, delivered: Callable[[], None]) -> None:
        import System.Windows.Forms as WinForms

        self._form = renderer_form
        self._method_invoker = WinForms.MethodInvoker
        self._delivered = delivered
        self._lock = threading.Lock()
        self._pending = ""
        self._scheduled = False
        self._closed = False

    def publish(self, state: dict[str, Any]) -> None:
        if self._closed:
            return
        payload = json.dumps(
            {"type": "minimap-live", **state},
            separators=(",", ":"),
        )
        with self._lock:
            self._pending = payload
            if self._scheduled:
                return
            self._scheduled = True
        self._schedule_delivery()

    def bootstrap(self) -> bool:
        """Wake the renderer without relying on focus or animation frames."""
        result = {"ok": False}

        def deliver() -> None:
            try:
                core = self._form.webview.CoreWebView2
                if not self._closed and core is not None:
                    core.PostWebMessageAsJson('{"type":"minimap-bootstrap"}')
                    result["ok"] = True
            except Exception:
                pass

        try:
            if self._form.InvokeRequired:
                self._form.Invoke(self._method_invoker(deliver))
            else:
                deliver()
        except Exception:
            return False
        return bool(result["ok"])

    def _schedule_delivery(self) -> None:
        def deliver() -> None:
            with self._lock:
                payload = self._pending
                self._pending = ""
            try:
                core = self._form.webview.CoreWebView2
                if not self._closed and core is not None and payload:
                    core.PostWebMessageAsJson(payload)
                    self._delivered()
            except Exception:
                pass
            with self._lock:
                again = bool(self._pending) and not self._closed
                if not again:
                    self._scheduled = False
            if again:
                self._schedule_delivery()

        try:
            self._form.BeginInvoke(self._method_invoker(deliver))
        except Exception:
            with self._lock:
                self._scheduled = False

    def close(self) -> None:
        self._closed = True
        with self._lock:
            self._pending = ""


class NativeMinimapHost:
    """GDI-backed HUD surface that respects Win32 ellipse/rounded regions."""

    def __init__(
        self,
        form: Any,
        picture: Any,
        method_invoker: Any,
        renderer_form: Any,
        frame_presented: Callable[[], None],
    ) -> None:
        self.form = form
        self.picture = picture
        self._method_invoker = method_invoker
        self._renderer_form = renderer_form
        self._frame_presented = frame_presented
        self._closed = False
        self._frame_lock = threading.Lock()
        self._pending_data_url = ""
        self._pending_bitmap: Any = None
        self._apply_scheduled = False
        self._frame_event = threading.Event()
        self._decoder = threading.Thread(
            target=self._decode_frames,
            name="ScrapMechanicMinimapDecode",
            daemon=True,
        )
        self._decoder.start()
        self._capture_task: Any = None
        self._capture_stream: Any = None
        self._capture_timer: Any = None
        self._capture_error_reported = False
        self._shared_buffer: Any = None
        self._shared_view = 0
        self._shared_bitmap: Any = None
        self._shared_core: Any = None
        self._shared_width = 0
        self._shared_height = 0
        self._shared_length = 0
        self._shared_frame_waiting = False
        self._shared_present_scheduled = False
        self._shared_timer: Any = None
        self._shared_timer_handler: Any = None

    def prepare_shared_frames(self, resource_forms: tuple[Any, ...] = ()) -> bool:
        """Map cached images and allocate BGRA transport before page navigation."""
        result = {"ok": False}

        def enable() -> None:
            try:
                from Microsoft.Web.WebView2.Core import (
                    CoreWebView2HostResourceAccessKind,
                )
                from System import IntPtr, UInt64
                from System.Drawing import Bitmap
                from System.Drawing.Imaging import PixelFormat
                from System.Windows.Forms import Timer

                core = self._renderer_form.webview.CoreWebView2
                if core is None:
                    return
                cache_root = (
                    Path(os.environ["LOCALAPPDATA"])
                    / "ScrapMechanicSaveEditor" / "map-cache"
                ).resolve()
                cache_root.mkdir(parents=True, exist_ok=True)
                for resource_form in (self._renderer_form, *resource_forms):
                    resource_core = resource_form.webview.CoreWebView2
                    if resource_core is None:
                        return
                    resource_core.SetVirtualHostNameToFolderMapping(
                        "scrap-companion.local",
                        str(cache_root),
                        CoreWebView2HostResourceAccessKind.Allow,
                    )
                viewport_width = max(1, int(self._renderer_form.ClientSize.Width))
                viewport_height = max(1, int(self._renderer_form.ClientSize.Height))
                # The WinForms host can retain asymmetric invisible insets even
                # when frameless. JS deliberately renders a square using the
                # shorter viewport edge, so the shared surface must do the same.
                width = height = min(viewport_width, viewport_height)
                # Four-byte ready flag followed by tightly packed BGRA pixels.
                length = 4 + width * height * 4
                shared = core.Environment.CreateSharedBuffer(UInt64(length))
                kernel32 = ctypes.windll.kernel32
                kernel32.MapViewOfFile.argtypes = (
                    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                    wintypes.DWORD, ctypes.c_size_t,
                )
                kernel32.MapViewOfFile.restype = ctypes.c_void_p
                mapping_handle = shared.FileMappingHandle.DangerousGetHandle().ToInt64()
                view = kernel32.MapViewOfFile(
                    wintypes.HANDLE(mapping_handle), 0x0002 | 0x0004, 0, 0, length
                )
                if not view:
                    shared.Dispose()
                    return
                bitmap = Bitmap(
                    width,
                    height,
                    width * 4,
                    PixelFormat.Format32bppArgb,
                    IntPtr(int(view) + 4),
                )
                old = self.picture.Image
                self.picture.Image = bitmap
                if old is not None:
                    old.Dispose()
                self._shared_buffer = shared
                self._shared_view = int(view)
                self._shared_bitmap = bitmap
                self._shared_core = core
                self._shared_width = width
                self._shared_height = height
                self._shared_length = length
                timer = Timer()
                timer.Interval = 8
                self._shared_timer_handler = self._poll_shared_frame
                timer.Tick += self._shared_timer_handler
                timer.Start()
                self._shared_timer = timer
                result["ok"] = True
            except Exception:
                import traceback

                traceback.print_exc()

        try:
            if self._renderer_form.InvokeRequired:
                self._renderer_form.Invoke(self._method_invoker(enable))
            else:
                enable()
        except Exception:
            return False
        return bool(result["ok"])

    def post_shared_frames(self) -> bool:
        """Attach transport only after the minimap page installs its listener."""
        result = {"ok": False}

        def post() -> None:
            try:
                from Microsoft.Web.WebView2.Core import CoreWebView2SharedBufferAccess

                if self._shared_core is None or self._shared_buffer is None:
                    return
                self._shared_core.PostSharedBufferToScript(
                    self._shared_buffer,
                    CoreWebView2SharedBufferAccess.ReadWrite,
                    json.dumps({
                        "type": "minimap-frame-buffer",
                        "width": self._shared_width,
                        "height": self._shared_height,
                    }, separators=(",", ":")),
                )
                result["ok"] = True
            except Exception:
                import traceback

                traceback.print_exc()

        try:
            if self._renderer_form.InvokeRequired:
                self._renderer_form.Invoke(self._method_invoker(post))
            else:
                post()
        except Exception:
            return False
        return bool(result["ok"])

    def _poll_shared_frame(self, _sender: Any, _event: Any) -> None:
        if self._closed or not self._shared_view:
            return
        if ctypes.c_uint32.from_address(self._shared_view).value != 1:
            return
        self._shared_frame_waiting = True
        self._present_shared_frame()

    def shared_frame_ready(self) -> bool:
        try:
            if self._closed or not self._shared_view:
                return False
            # Return from WebView2's callback before painting. Calling Refresh
            # here re-enters the WinForms message pump and deadlocks current
            # runtimes. Queue the synchronous paint for the next UI turn.
            self._shared_frame_waiting = True
            if not self._shared_present_scheduled:
                self._shared_present_scheduled = True
                self.form.BeginInvoke(self._method_invoker(self._present_shared_frame))
            return True
        except Exception:
            if not self._capture_error_reported:
                import traceback

                traceback.print_exc()
                self._capture_error_reported = True
            return False

    def _present_shared_frame(self) -> None:
        self._shared_present_scheduled = False
        if not self._shared_frame_waiting or not self._shared_view:
            return
        # The PictureBox owns a Bitmap directly over shared memory. Queueing a
        # normal paint is enough; synchronous Refresh can deadlock WebView2's
        # WinForms message loop.
        self.picture.Invalidate()
        self._shared_frame_waiting = False
        self._frame_presented()
        ctypes.c_uint32.from_address(self._shared_view).value = 0

    def set_capture_active(self, active: bool) -> None:
        timer = self._capture_timer
        if timer is None or self._closed:
            return

        def update() -> None:
            if self._closed:
                return
            if active:
                timer.Start()
            else:
                timer.Stop()

        try:
            self.form.BeginInvoke(self._method_invoker(update))
        except Exception:
            return

    def _capture_tick(self, _sender: Any, _event: Any) -> None:
        """Capture WebView2 directly, avoiding browser/Python base64 RPCs."""
        if self._closed:
            return
        try:
            if self._capture_task is not None:
                if not self._capture_task.IsCompleted:
                    return
                stream = self._capture_stream
                task = self._capture_task
                self._capture_task = None
                self._capture_stream = None
                if not task.IsFaulted and not task.IsCanceled and stream is not None:
                    from System.Drawing import Bitmap

                    stream.Position = 0
                    decoded = Bitmap(stream)
                    bitmap = Bitmap(decoded)
                    decoded.Dispose()
                    old = self.picture.Image
                    self.picture.Image = bitmap
                    if old is not None:
                        old.Dispose()
                    self._frame_presented()
                if stream is not None:
                    stream.Dispose()
                # Let WebView2 finish the completed capture's UI-thread
                # continuation before requesting the next one. Starting a new
                # CapturePreviewAsync in the same timer callback can leave that
                # second task permanently incomplete on current runtimes.
                return

            from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
            from System.IO import MemoryStream

            stream = MemoryStream()
            core = self._renderer_form.webview.CoreWebView2
            if core is None:
                stream.Dispose()
                return
            self._capture_stream = stream
            self._capture_task = core.CapturePreviewAsync(
                CoreWebView2CapturePreviewImageFormat.Jpeg,
                stream,
            )
        except Exception:
            if not self._capture_error_reported:
                import traceback

                traceback.print_exc()
                self._capture_error_reported = True
            stream = self._capture_stream
            self._capture_stream = None
            self._capture_task = None
            if stream is not None:
                try:
                    stream.Dispose()
                except Exception:
                    pass

    def present(self, data_url: str) -> bool:
        if self._closed or not data_url.startswith((
            "data:image/jpeg;base64,",
            "data:image/png;base64,",
        )):
            return False
        # A pywebview API method owns one shared request lane. Decoding here
        # blocks every live-position request behind the image and turns a
        # nominal 60 Hz HUD into a visibly stalled 1-6 Hz one. Keep only the
        # newest encoded frame and return immediately; the resident decoder
        # can drop obsolete work while the bridge remains free for telemetry.
        with self._frame_lock:
            self._pending_data_url = data_url
        self._frame_event.set()
        return True

    def _decode_frames(self) -> None:
        try:
            from System.Drawing import Bitmap
            from System.IO import MemoryStream
        except Exception:
            return

        while not self._closed:
            self._frame_event.wait(0.25)
            self._frame_event.clear()
            if self._closed:
                return
            with self._frame_lock:
                data_url = self._pending_data_url
                self._pending_data_url = ""
            if not data_url:
                continue
            try:
                raw = base64.b64decode(data_url.partition(",")[2], validate=True)
                stream = MemoryStream(raw)
                decoded = Bitmap(stream)
                bitmap = Bitmap(decoded)
                decoded.Dispose()
                stream.Dispose()
            except Exception:
                continue

            # If encoding outpaced GDI, discard the decoded historical frame.
            # The following decoder iteration will use the newest payload.
            with self._frame_lock:
                newer_encoded = bool(self._pending_data_url)
            if newer_encoded:
                try:
                    bitmap.Dispose()
                except Exception:
                    pass
                self._frame_event.set()
                continue
            self._queue_bitmap(bitmap)

    def _queue_bitmap(self, bitmap: Any) -> None:
        with self._frame_lock:
            superseded = self._pending_bitmap
            self._pending_bitmap = bitmap
            schedule = not self._apply_scheduled
            if schedule:
                self._apply_scheduled = True
        if superseded is not None:
            try:
                superseded.Dispose()
            except Exception:
                pass
        if not schedule:
            return

        def apply() -> None:
            with self._frame_lock:
                pending = self._pending_bitmap
                self._pending_bitmap = None
            if pending is None:
                with self._frame_lock:
                    self._apply_scheduled = False
                return
            try:
                if self._closed or self.form.IsDisposed:
                    pending.Dispose()
                else:
                    old = self.picture.Image
                    self.picture.Image = pending
                    if old is not None:
                        old.Dispose()
                    self._frame_presented()
            except Exception:
                try:
                    pending.Dispose()
                except Exception:
                    pass
            with self._frame_lock:
                again = self._pending_bitmap is not None
                if not again:
                    self._apply_scheduled = False
            if again:
                try:
                    self.form.BeginInvoke(self._method_invoker(apply))
                except Exception:
                    with self._frame_lock:
                        abandoned = self._pending_bitmap
                        self._pending_bitmap = None
                        self._apply_scheduled = False
                    if abandoned is not None:
                        try:
                            abandoned.Dispose()
                        except Exception:
                            pass

        try:
            self.form.BeginInvoke(self._method_invoker(apply))
        except Exception:
            with self._frame_lock:
                abandoned = self._pending_bitmap
                self._pending_bitmap = None
                self._apply_scheduled = False
            if abandoned is not None:
                try:
                    abandoned.Dispose()
                except Exception:
                    pass
            return False

    def destroy(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._frame_event.set()
        if self._capture_timer is not None:
            try:
                self._capture_timer.Stop()
                self._capture_timer.Dispose()
            except Exception:
                pass
        if self._shared_timer is not None:
            try:
                self._shared_timer.Stop()
                self._shared_timer.Dispose()
                self._shared_timer = None
                self._shared_timer_handler = None
            except Exception:
                pass
        with self._frame_lock:
            self._pending_data_url = ""
            pending = self._pending_bitmap
            self._pending_bitmap = None

        def release_shared_image() -> None:
            try:
                if self.picture.Image is self._shared_bitmap:
                    self.picture.Image = None
                if self._shared_bitmap is not None:
                    self._shared_bitmap.Dispose()
                    self._shared_bitmap = None
            except Exception:
                pass

        try:
            if self.form.InvokeRequired:
                self.form.Invoke(self._method_invoker(release_shared_image))
            else:
                release_shared_image()
            if self._shared_view:
                ctypes.windll.kernel32.UnmapViewOfFile(
                    ctypes.c_void_p(self._shared_view)
                )
                self._shared_view = 0
            if self._shared_buffer is not None:
                self._shared_buffer.Dispose()
        except Exception:
            pass
        if pending is not None:
            try:
                pending.Dispose()
            except Exception:
                pass

        def close() -> None:
            try:
                old = self.picture.Image
                self.picture.Image = None
                if old is not None:
                    old.Dispose()
                self.form.Close()
                self.form.Dispose()
            except Exception:
                return

        try:
            # Teardown is called from the monitor thread when the game exits.
            # Wait for Close/Dispose to complete instead of abandoning a
            # BeginInvoke callback when pywebview's last renderer is destroyed.
            if self.form.InvokeRequired:
                self.form.Invoke(self._method_invoker(close))
            else:
                close()
        except Exception:
            return


def create_native_minimap_host(
    renderer_window: Any,
    frame_presented: Callable[[], None],
) -> NativeMinimapHost | None:
    """Create the visible minimap without a DirectComposition child surface."""
    if sys.platform != "win32" or renderer_window is None:
        return None
    try:
        import System.Windows.Forms as WinForms
        from System.Drawing import Color, Point
        from webview.platforms.winforms import BrowserView

        renderer_form = None
        for _ in range(80):
            renderer_form = BrowserView.instances.get(renderer_window.uid)
            if renderer_form is not None:
                break
            time.sleep(0.05)
        if renderer_form is None:
            return None
        result: dict[str, Any] = {}

        def create() -> None:
            renderer_form.ShowInTaskbar = False
            # WebView2 stops dispatching requestAnimationFrame and bridge
            # events when a top-level renderer is parked outside all monitor
            # bounds. Keep this non-topmost implementation surface inside the
            # game client and place it at the bottom of the z-order instead.
            # Scrap Mechanic covers it; only the clipped GDI host below is
            # promoted above the game.
            game_client = scrap_mechanic_client_bounds()
            renderer_form.Location = Point(
                int(game_client[0]) if game_client else 0,
                int(game_client[1]) if game_client else 0,
            )
            renderer_form.Show()
            ctypes.windll.user32.SetWindowPos(
                int(renderer_form.Handle.ToInt64()),
                wintypes.HWND(1),  # HWND_BOTTOM
                0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0010 | 0x0200,
            )
            form = WinForms.Form()
            form.Text = MINIMAP_TITLE
            form.FormBorderStyle = getattr(WinForms.FormBorderStyle, "None")
            form.ShowInTaskbar = False
            form.StartPosition = WinForms.FormStartPosition.Manual
            form.BackColor = Color.FromArgb(7, 27, 32)
            form.TopMost = True
            picture = WinForms.PictureBox()
            picture.Dock = WinForms.DockStyle.Fill
            picture.BackColor = form.BackColor
            picture.SizeMode = WinForms.PictureBoxSizeMode.StretchImage
            picture.TabStop = False
            form.Controls.Add(picture)
            # Materialize the HWND on pywebview's WinForms UI thread, then
            # leave visibility to the existing Survival-only state machine.
            form.Show()
            form.Hide()
            result["host"] = NativeMinimapHost(
                form,
                picture,
                WinForms.MethodInvoker,
                renderer_form,
                frame_presented,
            )

        if renderer_form.InvokeRequired:
            renderer_form.Invoke(WinForms.MethodInvoker(create))
        else:
            create()
        return result.get("host")
    except Exception:
        import traceback

        traceback.print_exc()
        return None


def configure_minimap_window(
    settings: dict[str, Any] | None = None,
    retries: int = 1,
    *,
    promote: bool = False,
) -> bool:
    if sys.platform != "win32":
        return False
    hwnd = None
    for attempt in range(max(1, retries)):
        hwnd = find_window_by_title(MINIMAP_TITLE)
        if hwnd:
            break
        if attempt + 1 < retries:
            # The physical M key is handled by the dedicated key monitor.
            # This loop only maintains window visibility and therefore does
            # not need to enumerate foreground windows 25 times per second.
            time.sleep(0.08)
    if not hwnd:
        return False
    hwnd = root_window_handle(hwnd) or hwnd
    user32 = ctypes.windll.user32
    user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
    user32.SetWindowPos.argtypes = (
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    )
    gwl_exstyle = -20
    style = user32.GetWindowLongW(hwnd, gwl_exstyle)
    # Keep the HUD click-through/tool/no-activate flags.
    style = style | 0x00000020 | 0x00000080 | 0x08000000
    user32.SetWindowLongW(hwnd, gwl_exstyle, style)
    chosen = settings or load_companion_settings()
    x, y, size, _height = fit_minimap_bounds(scrap_mechanic_client_bounds(), chosen)
    # Do not include SWP_SHOWWINDOW here. This function also runs as a
    # periodic bounds repair; asking Win32 to show an already-visible WebView
    # on every pass caused the minimap to blink after the full map closed.
    flags = 0x0010 | 0x0200  # SWP_NOACTIVATE | SWP_NOSENDCHANGING
    insert_after = wintypes.HWND(-1)
    if not promote:
        # Bounds repairs must not repeatedly churn the compositor's z-order.
        flags |= 0x0004  # SWP_NOZORDER
        insert_after = wintypes.HWND(0)
    user32.SetWindowPos(hwnd, insert_after, x, y, size, size, flags)
    apply_minimap_window_region(hwnd, size, chosen)
    return True


_MINIMAP_REGION_SIGNATURE: tuple[Any, ...] | None = None
_MINIMAP_CONTENT_REGION: dict[str, float] | None = None


def _native_region_matches(
    region_kind: int,
    boxed_kind: int,
    box: tuple[int, int, int, int],
    expected: tuple[int, int, int, int],
    *,
    curved: bool,
) -> bool:
    """Reject a rectangular replacement that only shares the expected bounds."""
    if not region_kind or not boxed_kind:
        return False
    # Win32 reports rectangles as SIMPLEREGION (2), while ellipses and rounded
    # rectangles are COMPLEXREGION (3). WebView2 can replace SetWindowRgn with
    # a full rectangle during a composition-target transition; comparing only
    # GetRgnBox cannot distinguish that regression because both boxes match.
    if curved and (region_kind != 3 or boxed_kind != 3):
        return False
    return all(abs(actual - wanted) <= 1 for actual, wanted in zip(box, expected))


def remember_minimap_content_region(payload: dict[str, Any]) -> None:
    """Remember the frame measured inside WebView2's real CSS viewport."""
    global _MINIMAP_CONTENT_REGION, _MINIMAP_REGION_SIGNATURE
    try:
        left = float(payload["left"])
        top = float(payload["top"])
        right = float(payload["right"])
        bottom = float(payload["bottom"])
        width = float(payload["width"])
        height = float(payload["height"])
        radius = float(payload.get("radius") or 0)
    except (KeyError, TypeError, ValueError):
        return
    values = (left, top, right, bottom, width, height, radius)
    if not all(math.isfinite(value) for value in values):
        return
    if width <= 0 or height <= 0 or right <= left or bottom <= top:
        return
    _MINIMAP_CONTENT_REGION = {
        "left": left, "top": top, "right": right, "bottom": bottom,
        "width": width, "height": height, "radius": max(0.0, radius),
    }
    _MINIMAP_REGION_SIGNATURE = None


def apply_minimap_window_region(
    hwnd: int,
    size: int,
    settings: dict[str, Any] | None,
) -> bool:
    """Clip the native WebView window to the visible minimap frame.

    WebView2 can make its page transparent, but the borderless WinForms host
    beneath it is still an opaque rectangle on some GPUs.  A native window
    region removes those pixels from composition entirely, leaving only the
    rounded Scrap Mechanic frame (or circular frame) visible.
    """
    global _MINIMAP_REGION_SIGNATURE
    if sys.platform != "win32" or not hwnd:
        return False
    hwnd = root_window_handle(hwnd) or hwnd
    chosen = settings or {}
    shape = "circle" if chosen.get("shape") == "circle" else "square"
    border = str(chosen.get("border") or "mechanic")

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
    user32.ClientToScreen.restype = wintypes.BOOL
    client_rect = wintypes.RECT()
    if user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
        client_width = max(1, int(client_rect.right - client_rect.left))
        client_height = max(1, int(client_rect.bottom - client_rect.top))
    else:
        client_width = client_height = max(1, int(size))
    # SetWindowRgn coordinates are relative to the *whole window*, while the
    # CSS measurements are relative to WebView2's client viewport. Even a
    # frameless WinForms form may retain an invisible DPI-scaled resize inset.
    # Ignoring it leaves an opaque rectangular strip beside the rounded HUD.
    window_rect = wintypes.RECT()
    client_origin = wintypes.POINT(0, 0)
    if (
        user32.GetWindowRect(hwnd, ctypes.byref(window_rect))
        and user32.ClientToScreen(hwnd, ctypes.byref(client_origin))
    ):
        client_offset_x = int(client_origin.x - window_rect.left)
        client_offset_y = int(client_origin.y - window_rect.top)
        window_width = max(client_width, int(window_rect.right - window_rect.left))
        window_height = max(client_height, int(window_rect.bottom - window_rect.top))
    else:
        client_offset_x = client_offset_y = 0
        window_width = client_width
        window_height = client_height
    try:
        user32.GetDpiForWindow.argtypes = (wintypes.HWND,)
        user32.GetDpiForWindow.restype = wintypes.UINT
        dpi_scale = max(1.0, float(user32.GetDpiForWindow(hwnd) or 96) / 96.0)
    except (AttributeError, OSError):
        dpi_scale = 1.0
    measured = _MINIMAP_CONTENT_REGION
    if measured:
        scale_x = client_width / max(1.0, measured["width"])
        scale_y = client_height / max(1.0, measured["height"])
        edge_left = max(0, client_offset_x + round(measured["left"] * scale_x))
        edge_top = max(0, client_offset_y + round(measured["top"] * scale_y))
        right = min(window_width, max(edge_left + 1, client_offset_x + round(measured["right"] * scale_x)))
        bottom = min(window_height, max(edge_top + 1, client_offset_y + round(measured["bottom"] * scale_y)))
        measured_corner = max(
            1,
            round(max(0.5, measured["radius"]) * 2 * min(scale_x, scale_y)),
        )
    else:
        content_edge = max(0, round((0 if border == "none" else 0) * dpi_scale))
        edge_left = max(0, client_offset_x + content_edge)
        edge_top = max(0, client_offset_y + content_edge)
        right = min(window_width, max(edge_left + 1, client_offset_x + client_width - content_edge))
        bottom = min(window_height, max(edge_top + 1, client_offset_y + client_height - content_edge))
        measured_corner = max(1, round((1 if border == "none" else 36) * dpi_scale))
    signature = (
        int(hwnd), window_width, window_height, client_width, client_height,
        client_offset_x, client_offset_y, shape, border,
        edge_left, edge_top, right, bottom, measured_corner,
    )
    user32.SetWindowRgn.argtypes = (wintypes.HWND, wintypes.HRGN, wintypes.BOOL)
    user32.SetWindowRgn.restype = ctypes.c_int
    gdi32.CreateEllipticRgn.argtypes = (
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    )
    gdi32.CreateEllipticRgn.restype = wintypes.HRGN
    gdi32.CreateRoundRectRgn.argtypes = (
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int,
    )
    gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.CreateRectRgn.argtypes = (
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    )
    gdi32.CreateRectRgn.restype = wintypes.HRGN
    gdi32.GetRgnBox.argtypes = (wintypes.HRGN, ctypes.POINTER(wintypes.RECT))
    gdi32.GetRgnBox.restype = ctypes.c_int
    user32.GetWindowRgn.argtypes = (wintypes.HWND, wintypes.HRGN)
    user32.GetWindowRgn.restype = ctypes.c_int

    if signature == _MINIMAP_REGION_SIGNATURE:
        existing = gdi32.CreateRectRgn(0, 0, 0, 0)
        if existing:
            region_kind = user32.GetWindowRgn(hwnd, existing)
            box = wintypes.RECT()
            boxed = gdi32.GetRgnBox(existing, ctypes.byref(box)) if region_kind else 0
            gdi32.DeleteObject(existing)
            if _native_region_matches(
                int(region_kind),
                int(boxed),
                (int(box.left), int(box.top), int(box.right), int(box.bottom)),
                (edge_left, edge_top, right, bottom),
                curved=shape == "circle" or measured_corner > 2,
            ):
                return True
    if shape == "circle":
        region = gdi32.CreateEllipticRgn(edge_left, edge_top, right, bottom)
    else:
        region = gdi32.CreateRoundRectRgn(
            edge_left, edge_top, right, bottom, measured_corner, measured_corner,
        )
    if not region:
        return False
    if user32.SetWindowRgn(hwnd, region, True):
        # On success Windows owns the HRGN and frees it when replaced.
        _MINIMAP_REGION_SIGNATURE = signature
        return True
    gdi32.DeleteObject(region)
    return False


def set_window_topmost(title: str, active: bool) -> bool:
    """Change only z-order; never show, hide, resize, or activate a window."""
    if sys.platform != "win32":
        return False
    hwnd = find_window_by_title(title)
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = (
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    )
    insert_after = wintypes.HWND(-1 if active else -2)
    flags = 0x0001 | 0x0002 | 0x0010 | 0x0200
    return bool(user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags))


def fit_minimap_bounds(
    client: tuple[int, int, int, int] | None,
    settings: dict[str, Any] | None,
) -> tuple[int, int, int, int]:
    """Calculate a DPI-independent minimap rectangle for tests and Win32."""
    chosen = settings or {}
    size = max(176, min(420, int(chosen.get("size") or 280)))
    margin_x = max(0, int(chosen.get("marginX") or 0))
    margin_y = max(0, int(chosen.get("marginY") or 0))
    if not client:
        return margin_x, margin_y, size, size
    left, top, right, bottom = client
    width = max(1, right - left)
    height = max(1, bottom - top)
    # Always retain at least a small strip of playable client area in tiny
    # windowed modes, while preserving the user's exact size on normal screens.
    max_size = max(176, min(width - margin_x, height - margin_y))
    size = min(size, max_size)
    return left + margin_x, top + margin_y, size, size


def configure_full_map_window(retries: int = 1) -> bool:
    """Make the external map exactly cover the game's current client area."""
    if sys.platform != "win32":
        return False
    hwnd = None
    for attempt in range(max(1, retries)):
        hwnd = find_window_by_title(FULL_MAP_TITLE)
        if hwnd:
            break
        if attempt + 1 < retries:
            time.sleep(0.04)
    bounds = scrap_mechanic_client_bounds()
    if not hwnd or not bounds:
        return False
    left, top, right, bottom = bounds
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = (
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    )
    user32.SetWindowPos(
        hwnd, wintypes.HWND(-1), left, top, right - left, bottom - top,
        0x0010 | 0x0040,
    )
    return True


def focus_window_title(title: str) -> None:
    hwnd = find_window_by_title(title)
    if not hwnd or sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetActiveWindow.argtypes = (wintypes.HWND,)
    user32.SetFocus.argtypes = (wintypes.HWND,)
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = bool(foreground_thread and foreground_thread != current_thread)
    if attached:
        user32.AttachThreadInput(current_thread, foreground_thread, True)
    try:
        user32.ShowWindow(hwnd, 9)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


def _scrap_mechanic_windows() -> list[int]:
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.EnumWindows.argtypes = (ctypes.c_void_p, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    )
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    wanted: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _lparam: int) -> bool:
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        handle = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not handle:
            return True
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_ulong(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                if Path(buffer.value).name.casefold() == "scrapmechanic.exe":
                    wanted.append(hwnd)
        finally:
            kernel32.CloseHandle(handle)
        return True

    user32.EnumWindows(visit, 0)
    return wanted


def scrap_mechanic_client_bounds() -> tuple[int, int, int, int] | None:
    """Return the visible game's client area in screen coordinates.

    Positioning relative to the client—not the desktop—keeps the minimap in
    the actual top-left corner for bordered windows, borderless fullscreen,
    ultrawide monitors, and a moved 480p/720p test window.
    """
    if sys.platform != "win32":
        return None
    windows = _scrap_mechanic_windows()
    if not windows:
        return None
    user32 = ctypes.windll.user32
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
    user32.ClientToScreen.restype = wintypes.BOOL
    hwnd = next((row for row in windows if user32.IsWindowVisible(row)), windows[0])
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wintypes.POINT(rect.left, rect.top)
    corner = wintypes.POINT(rect.right, rect.bottom)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(corner)):
        return None
    if corner.x <= origin.x or corner.y <= origin.y:
        return None
    return origin.x, origin.y, corner.x, corner.y


def enable_game_windows(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    user32.EnableWindow.argtypes = (wintypes.HWND, wintypes.BOOL)
    for hwnd in _scrap_mechanic_windows():
        user32.EnableWindow(hwnd, bool(enabled))


def focus_scrap_mechanic(retries: int = 1) -> None:
    windows = _scrap_mechanic_windows()
    if not windows or sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    user32.EnableWindow.argtypes = (wintypes.HWND, wintypes.BOOL)
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetActiveWindow.argtypes = (wintypes.HWND,)
    user32.SetFocus.argtypes = (wintypes.HWND,)
    user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
    user32.SwitchToThisWindow.argtypes = (wintypes.HWND, wintypes.BOOL)
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    hwnd = next((row for row in windows if user32.IsWindowVisible(row)), windows[0])
    user32.EnableWindow(hwnd, True)
    user32.AllowSetForegroundWindow(wintypes.DWORD(-1))
    for attempt in range(max(1, retries)):
        foreground = user32.GetForegroundWindow()
        foreground_thread = (
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        )
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        attached: list[tuple[int, int]] = []
        for source in (foreground_thread, target_thread):
            if source and source != current_thread:
                user32.AttachThreadInput(current_thread, source, True)
                attached.append((current_thread, source))
        try:
            user32.ShowWindow(hwnd, 9)
            # Never use a TOPMOST -> NOTOPMOST focus trick here.  The
            # minimap is an always-on-top HUD, so promoting the whole game
            # temporarily covers it.  Repeated focus retries then look like
            # the minimap is being hidden and recreated even though its HWND
            # never changed visibility.  Attached input queues are sufficient
            # to transfer focus without crossing z-order bands.
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SwitchToThisWindow(hwnd, True)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            for source, target in reversed(attached):
                user32.AttachThreadInput(source, target, False)
        if user32.GetForegroundWindow() == hwnd:
            break
        time.sleep(0.035)


def restore_scrap_mechanic_focus() -> None:
    """Return input once, synchronously, without touching HUD z-order.

    Full-map visibility is controlled with native ``ShowWindow`` and is
    complete before this function runs.  The old delayed retry thread was
    both unnecessary and harmful: its four later game promotions matched the
    reported post-close minimap flicker timings exactly.
    """
    focus_scrap_mechanic(retries=3)


def read_sequence(path: Path) -> int | None:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("sequence", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # A writer replaces this file atomically, but security software can
        # still create a momentary sharing violation. Missing data is not a
        # sequence reset and therefore must never be interpreted as a press.
        return None


def read_signal(path: Path) -> tuple[int, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return int(value.get("sequence", 0)), str(value.get("action") or "toggle")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def scrap_mechanic_process_ids() -> set[int]:
    if sys.platform != "win32":
        return {1}
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260),
        ]

    result: set[int] = set()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return result
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.casefold() == "scrapmechanic.exe":
                    result.add(int(entry.th32ProcessID))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def wait_for_game_frontend(game_root: Path, timeout_seconds: float = 900.0) -> bool:
    """Stay effectively idle until the current world finishes loading.

    Starting WebView2 and importing the editor backend while Scrap Mechanic is
    loading makes both programs fight over disk, CPU, and GPU resources.  The
    tiny standard-library bootstrap below waits for the game's own current log
    to report that its world physics bundle has finished loading. A conservative
    fallback handles disabled logs.
    """
    started = time.monotonic()
    process_seen_at: float | None = None
    logs_root = game_root / "Logs"
    while time.monotonic() - started < timeout_seconds:
        process_ids = scrap_mechanic_process_ids()
        if not process_ids:
            if process_seen_at is not None:
                return False
            time.sleep(0.25)
            continue
        if process_seen_at is None:
            process_seen_at = time.monotonic()

        try:
            latest = max(logs_root.glob("game-*.log"), key=lambda path: path.stat().st_mtime)
            # The startup log is small enough at this stage that a complete
            # read is cheaper and safer than maintaining a tailing handle.
            log_text = latest.read_text(encoding="utf-8", errors="ignore")
            # The number in Scrap Mechanic's ``[Main:...]`` log prefix is a
            # thread id, not the Windows process id.  Matching it against the
            # process id made the companion wait for its five-minute fallback.
            # ``Loading screen time`` is also written for the main menu and
            # ``game_shape_physics.cbo`` is unloaded during early bootstrap.
            # Treating either as ready made the renderer compete with the game
            # while the player was still choosing/loading a world.  The Game
            # state is the first unambiguous, save-independent ready marker.
            # Logs can contain more than one frontend transition, so an old
            # Game line must not beat a newer Menu line after returning to the
            # title screen or starting a fresh process.
            states = re.findall(r"Start state:\s*([A-Za-z0-9_]+)", log_text)
            if states and states[-1].casefold() == "game":
                return True
        except (OSError, ValueError):
            pass

        # Logging can be disabled. Avoid the old five-minute apparent hang,
        # while still leaving enough time for an ordinary world load before
        # WebView2 and the renderer begin using CPU/GPU resources.
        if time.monotonic() - process_seen_at >= 60.0:
            return True
        time.sleep(0.5)
    return False


def wait_for_game_process(timeout_seconds: float = 900.0) -> bool:
    """Start the hidden renderer as soon as Scrap Mechanic exists.

    The Survival gate still comes from the current game log in
    ``monitor_signal``. Warming WebView2 during the menu/load transition keeps
    a cached minimap ready for the first playable frame instead of starting
    Chromium only after the world has already appeared.
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        if scrap_mechanic_process_ids():
            return True
        time.sleep(0.25)
    return False


def scrap_mechanic_frontend_context(game_root: Path) -> dict[str, str]:
    """Return the current frontend state and game mode from the newest log.

    ``Start state: Game`` is shared by Survival, Creative, Custom Game, and
    Challenge Mode.  The companion must therefore bind the state to the most
    recent ``Game mode`` line from the same load transition instead of
    treating every playable frontend as a Survival world.
    """
    try:
        latest = max(
            (game_root / "Logs").glob("game-*.log"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        size = latest.stat().st_size
        with latest.open("rb") as handle:
            handle.seek(max(0, size - 1024 * 1024))
            text = handle.read().decode("utf-8", errors="ignore")
        states = list(re.finditer(r"Start state:\s*([A-Za-z0-9_]+)", text))
        if not states:
            return {"state": "", "mode": ""}
        newest = states[-1]
        state = newest.group(1).casefold()
        # A mode line belongs to the load interval that starts after the
        # preceding frontend state.  This prevents an earlier Survival line
        # from leaking into a later Creative/Challenge session while its mode
        # line is still being written.
        interval_start = states[-2].end() if len(states) > 1 else 0
        mode_rows = re.findall(
            r"Game mode:\s*([^\r\n]+)", text[interval_start:newest.end()]
        )
        mode = mode_rows[-1].strip().casefold() if mode_rows else ""
        return {"state": state, "mode": mode}
    except (OSError, ValueError):
        return {"state": "", "mode": ""}


def scrap_mechanic_frontend_state(game_root: Path) -> str:
    """Return the newest logged frontend state for the running process."""
    return scrap_mechanic_frontend_context(game_root)["state"]


def foreground_process_id() -> int:
    if sys.platform != "win32":
        return 1
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    )
    process_id = wintypes.DWORD()
    hwnd = user32.GetForegroundWindow()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return int(process_id.value)


def foreground_window_title() -> str:
    if sys.platform != "win32":
        return ""
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = user32.GetForegroundWindow()
    return _window_text(int(hwnd)) if hwnd else ""


def virtual_key_down(key: int) -> bool:
    """Read a physical key without injecting or suppressing game input."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return bool(user32.GetAsyncKeyState(int(key)) & 0x8000)


def monitor_signal(bridge: OverlayBridge, signal_path: Path) -> None:
    sequence = read_sequence(signal_path) or 0
    bridge._request_sequence = sequence
    bridge._ack_sequence = sequence
    bridge._write_overlay_state()
    game_seen = False
    waiting_since = time.monotonic()
    last_minimap_check = 0.0
    last_minimap_layout: tuple[Any, ...] | None = None
    last_full_map_bounds: tuple[int, int, int, int] | None = None
    last_layout_check = 0.0
    last_native_visibility_check = 0.0
    last_process_check = 0.0
    last_frontend_check = 0.0
    last_metrics_write = 0.0
    frontend_state = ""
    frontend_mode = ""
    game_pids: set[int] = set()
    minimap_allowed = False
    m_was_down = False
    escape_was_down = False
    last_key_action = -1000.0
    try:
        while True:
            now = time.monotonic()
            if now - last_process_check >= 0.5:
                game_pids = scrap_mechanic_process_ids()
                last_process_check = now
            running = bool(game_pids)
            if running and not game_seen:
                game_seen = True
                context = scrap_mechanic_frontend_context(
                    bridge._game_root
                ) if bridge._game_root is not None else {"state": "game", "mode": "survival"}
                frontend_state = context["state"]
                frontend_mode = context["mode"]
                if frontend_state == "game" and frontend_mode == "survival":
                    bridge.game_started()
                else:
                    # Logical state can already be false while WebView2 briefly
                    # materialises its HWND. Reassert the native hidden state
                    # when attaching on the main menu.
                    bridge.game_left_world()
            elif not running and game_seen:
                bridge.game_stopped()
                bridge.shutdown()
                return
            elif not running and time.monotonic() - waiting_since > 900:
                bridge.shutdown()
                return

            if running and now - last_frontend_check >= 0.25:
                last_frontend_check = now
                context = scrap_mechanic_frontend_context(
                    bridge._game_root
                ) if bridge._game_root is not None else {"state": "game", "mode": "survival"}
                observed = context["state"]
                observed_mode = context["mode"]
                survival_world = observed == "game" and observed_mode == "survival"
                was_survival_world = frontend_state == "game" and frontend_mode == "survival"
                if survival_world and not was_survival_world:
                    bridge.game_started()
                elif not survival_world and was_survival_world:
                    bridge.game_left_world()
                if observed:
                    frontend_state = observed
                    frontend_mode = observed_mode

            signal = read_signal(signal_path)
            if signal is not None and signal[0] > sequence:
                current, action = signal
                presses = current - sequence
                sequence = current
                bridge._request_sequence = current
                if action == "close":
                    bridge.hide(force=True)
                elif action == "open":
                    bridge.show()
                elif presses % 2:
                    if bridge._visible:
                        bridge.hide()
                    else:
                        bridge.show()
                bridge._ack_sequence = current
                bridge._write_overlay_state()

            if now - last_minimap_check >= 0.2:
                last_minimap_check = now
                minimap_allowed = bridge.minimap_allowed()
                if not running or not bridge._in_game_world:
                    bridge.set_minimap_visible(False)
            foreground_pid = foreground_process_id()
            foreground_title = foreground_window_title()
            companion_context = foreground_title == FULL_MAP_TITLE
            game_context = foreground_pid in game_pids or companion_context
            # Handle M inside the overlay process.  The former detached
            # PowerShell monitor could exit independently, leaving a healthy
            # minimap but a permanently dead map key.  One state machine now
            # owns the key edge, map state and window transition.
            m_down = virtual_key_down(0x4D)
            escape_down = virtual_key_down(0x1B)
            key_ready = now - last_key_action >= 0.14
            if game_context and m_down and not m_was_down and key_ready:
                if bridge._visible:
                    bridge.hide()
                else:
                    bridge.show()
                last_key_action = now
            elif companion_context and escape_down and not escape_was_down and key_ready:
                bridge.hide()
                last_key_action = now
            m_was_down = m_down
            escape_was_down = escape_down
            if game_context:
                bridge._outside_game_since = None
            elif bridge._outside_game_since is None:
                bridge._outside_game_since = now
            minimap_resident = bool(
                running and bridge._in_game_world and minimap_allowed
            )
            bridge.set_minimap_visible(minimap_resident)
            bridge.set_minimap_foreground(
                minimap_resident and (
                    game_context
                    or bridge._visible
                    or now < bridge._minimap_handoff_until
                )
            )
            if now - last_native_visibility_check >= 1.0:
                last_native_visibility_check = now
                # Native WebView2 transitions complete asynchronously.  This
                # prevents a delayed full-map show from surviving after a
                # close and creating the apparent full-map/minimap "limbo".
                bridge.enforce_native_visibility()
            if now - last_layout_check >= 1.0:
                last_layout_check = now
                client = scrap_mechanic_client_bounds()
                if bridge._minimap_visible:
                    chosen = bridge.companion_settings()
                    layout = (
                        client,
                        int(chosen.get("size") or 280),
                        int(chosen.get("marginX") or 0),
                        int(chosen.get("marginY") or 0),
                        str(chosen.get("shape") or "square"),
                    )
                    if layout != last_minimap_layout:
                        last_minimap_layout = layout
                        configure_minimap_window(chosen, retries=1, promote=False)
                if bridge._visible and client and client != last_full_map_bounds:
                    last_full_map_bounds = client
                    configure_full_map_window(retries=1)
            if now - last_metrics_write >= 1.0:
                last_metrics_write = now
                bridge.write_performance_metrics()
            time.sleep(0.04)
    finally:
        enable_game_windows(True)


def acquire_single_instance() -> Any:
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\ScrapMechanicCompleteMapOverlay")
    if not handle or kernel32.GetLastError() == 183:
        if handle:
            kernel32.CloseHandle(handle)
        raise SystemExit(0)
    return handle


def lower_process_priority() -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000
        )


def main(argv: list[str] | None = None) -> int:
    # The minimap is a non-activating, click-through WebView.  Chromium would
    # normally classify that host as an occluded/background window and clamp
    # requestAnimationFrame plus its message pump to one tick per second.  The
    # result looks exactly like a frozen player arrow even though process
    # memory is being sampled at 60 Hz.  Keep this small HUD on the foreground
    # scheduling path; the hidden full-map/capture views still do no rendering
    # work until explicitly used.
    webview_flags = (
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
    )
    existing_flags = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    for flag in webview_flags:
        if flag not in existing_flags:
            existing_flags = f"{existing_flags} {flag}".strip()
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = existing_flags
    parser = argparse.ArgumentParser(description="Scrap Mechanic external 2D map companion")
    parser.add_argument("--signal", type=Path)
    parser.add_argument(
        "--game-root",
        type=Path,
        default=None,
    )
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    game_root = args.game_root.resolve() if args.game_root else find_game_root()
    web_root = PROJECT_ROOT / "scrap_editor_app" / "web"
    required = (
        web_root / "companion_overlay.html",
        web_root / "companion_overlay.js",
        web_root / "companion_overlay.css",
        web_root / "companion_bootstrap.html",
        web_root / "companion_minimap.html",
        web_root / "companion_minimap.js",
        web_root / "companion_minimap.css",
        web_root / "companion_capture.html",
        web_root / "companion_capture.js",
        web_root / "map3d.js",
    )
    if args.check_only:
        import webview  # noqa: F401
        from scrap_editor_app.backend import EditorApi

        if not all(path.is_file() for path in required):
            return 3
        EditorApi()
        return 0
    if game_root is None:
        parser.error(
            "Scrap Mechanic was not found in any registered Steam library. "
            "Set SCRAP_MECHANIC_GAME_ROOT or pass --game-root."
        )
    if args.signal is None:
        parser.error("--signal is required unless --check-only is used")
    instance = acquire_single_instance()
    lower_process_priority()
    if not wait_for_game_process():
        if instance and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(instance)
        return 0
    try:
        import webview
    except ImportError:
        return 2

    atexit.register(enable_game_windows, True)
    bridge = OverlayBridge(args.signal, game_root)
    minimap_settings = bridge.companion_settings()
    minimap_size = int(minimap_settings.get("size") or 236)
    minimap_x = int(minimap_settings.get("marginX") or 8)
    minimap_y = int(minimap_settings.get("marginY") or 8)
    bootstrap_uri = (web_root / "companion_bootstrap.html").as_uri()
    full_map_uri = (web_root / "companion_overlay.html").as_uri()
    minimap_uri = (web_root / "companion_minimap.html").as_uri()
    window = webview.create_window(
        FULL_MAP_TITLE,
        bootstrap_uri,
        js_api=bridge,
        fullscreen=True,
        hidden=True,
        frameless=True,
        easy_drag=False,
        on_top=True,
        background_color="#06171c",
    )
    minimap = webview.create_window(
        MINIMAP_RENDERER_TITLE,
        bootstrap_uri,
        js_api=bridge,
        width=minimap_size,
        height=minimap_size,
        x=-32000,
        y=-32000,
        min_size=(176, 176),
        resizable=False,
        hidden=False,
        frameless=True,
        easy_drag=False,
        shadow=False,
        focus=False,
        on_top=False,
        background_color="#000000",
        transparent=False,
    )
    bridge._window = window
    bridge._minimap_renderer_window = minimap

    def started() -> None:
        # pywebview/Win32 can briefly materialize a window that was created
        # with ``hidden=True`` while WebView2 finishes attaching.  The bridge's
        # logical flag still starts false, so the ordinary idempotent setter
        # would otherwise leave that native window visible on the main menu.
        # Reassert both hidden surfaces after creation, independently of their
        # logical state, before the monitor is allowed to show either one.
        if not minimap.events.loaded.wait(20.0) or not window.events.loaded.wait(20.0):
            raise RuntimeError("The map renderer bootstrap did not finish loading")
        from webview.platforms.winforms import BrowserView

        renderer_form = None
        full_map_form = None
        for _ in range(80):
            renderer_form = BrowserView.instances.get(minimap.uid)
            full_map_form = BrowserView.instances.get(window.uid)
            if renderer_form is not None and full_map_form is not None:
                break
            time.sleep(0.05)
        if renderer_form is None or full_map_form is None:
            raise RuntimeError("Could not attach the native map renderers")
        native_host = create_native_minimap_host(minimap, bridge.native_frame_presented)
        if native_host is None:
            raise RuntimeError("Could not create the native minimap surface")
        bridge._minimap_window = native_host
        bridge._shared_frame_sink = native_host.shared_frame_ready
        # Virtual-host mappings belong to each CoreWebView2 instance. Install
        # them before either real page can request the cached PNG; otherwise
        # Chromium negatively caches the first failed host lookup and a warm
        # save remains blank. Allocate transport now, but post it only after
        # the minimap script has installed sharedbufferreceived.
        if not native_host.prepare_shared_frames((full_map_form,)):
            raise RuntimeError("Could not initialize shared minimap frames")
        minimap.load_url(minimap_uri)
        window.load_url(full_map_uri)
        if not minimap.events.loaded.wait(20.0) or not window.events.loaded.wait(20.0):
            raise RuntimeError("The map renderer pages did not finish loading")
        if not native_host.post_shared_frames():
            raise RuntimeError("Could not attach shared minimap frames")
        live_push = NativeMinimapLivePush(renderer_form, bridge.native_live_pushed)
        full_map_push = NativeMinimapLivePush(full_map_form, lambda: None)
        bridge._live_push_host = live_push
        bridge._full_map_live_push_host = full_map_push

        def publish_live(state: dict[str, Any]) -> None:
            live_push.publish(state)
            if bridge._visible:
                full_map_push.publish(state)

        bridge._live_push_sink = publish_live
        if not live_push.bootstrap():
            raise RuntimeError("Could not wake the minimap renderer")
        bridge._write_overlay_state()
        configure_minimap_window(bridge.companion_settings(), retries=12)
        # Position first: SetWindowPos/WebView2 attachment can materialise an
        # HWND. Hiding after configuration keeps menus genuinely overlay-free.
        bridge.hide(restore_game_focus=False, force=True)
        bridge.set_minimap_visible(False, force=True)
        threading.Thread(
            target=monitor_signal, args=(bridge, args.signal.resolve()), daemon=True
        ).start()

    try:
        webview.start(started, private_mode=True)
    finally:
        bridge.shutdown()
        enable_game_windows(True)
        if instance and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
