import unittest
import inspect
import math
import os
import sqlite3
import json
from PIL import Image
from tempfile import TemporaryDirectory
from unittest.mock import call, patch
from pathlib import Path
from scrap_editor_app.backend import (
    _exact_atlas_water_mask,
    _remove_small_water_mask_holes,
    restore_exact_atlas_water,
)
from scrap_editor_app.desktop import _companion_launcher_path, _launch_map_companion

try:
    from .companion_overlay import (
        OverlayBridge,
        _native_region_matches,
        directional_reveal_contains,
        directional_reveal_factor,
        reveal_contains,
        fit_minimap_bounds,
        focus_scrap_mechanic,
        read_sequence,
        read_signal,
        restore_scrap_mechanic_focus,
        scrap_mechanic_frontend_context,
        scrap_mechanic_frontend_state,
        wait_for_game_frontend,
        wait_for_game_process,
    )
    from .live_process_tracker import ScrapMechanicLiveTracker, _valid_row
    from .companion_settings import normalize_companion_settings
except ImportError:
    from companion_overlay import (
        OverlayBridge,
        _native_region_matches,
        directional_reveal_contains,
        directional_reveal_factor,
        reveal_contains,
        fit_minimap_bounds,
        focus_scrap_mechanic,
        read_sequence,
        read_signal,
        restore_scrap_mechanic_focus,
        scrap_mechanic_frontend_context,
        scrap_mechanic_frontend_state,
        wait_for_game_frontend,
        wait_for_game_process,
    )
    from live_process_tracker import ScrapMechanicLiveTracker, _valid_row
    from companion_settings import normalize_companion_settings


ROOT = Path(__file__).resolve().parent


class CompanionInputWiringTests(unittest.TestCase):
    def test_desktop_editor_can_find_the_companion_launcher(self):
        expected = (ROOT / "Launch Scrap Mechanic with Map Companion.cmd").resolve()
        self.assertEqual(_companion_launcher_path().resolve(), expected)
        packaged_executable = ROOT.parent / "4.16.3" / "ScrapMechanicSaveEditor.exe"
        with patch("scrap_editor_app.desktop.sys.executable", str(packaged_executable)):
            self.assertEqual(_companion_launcher_path().resolve(), expected)

    def test_desktop_launcher_uses_valid_stdio_and_checks_the_exit_code(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "launcher with spaces.cmd"
            launch_log = root / "launch.log"
            launcher.write_text("@echo off\necho verified-launch\nexit /b 0\n", encoding="utf-8")
            with (
                patch("scrap_editor_app.desktop.COMPANION_LAUNCH_LOG_PATH", launch_log),
                patch("scrap_editor_app.desktop._scrap_mechanic_window", return_value=0),
                patch("scrap_editor_app.desktop.threading.Thread") as thread,
            ):
                result = _launch_map_companion(launcher, timeout=5)
            self.assertTrue(result["started"])
            self.assertFalse(result["gameAlreadyRunning"])
            self.assertIn("verified-launch", launch_log.read_text(encoding="utf-8"))
            thread.return_value.start.assert_called_once_with()

    def test_desktop_launcher_returns_a_visible_error_for_batch_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "failed launcher.cmd"
            launch_log = root / "launch.log"
            launcher.write_text("@echo off\necho failed-launch\nexit /b 7\n", encoding="utf-8")
            with (
                patch("scrap_editor_app.desktop.COMPANION_LAUNCH_LOG_PATH", launch_log),
                patch("scrap_editor_app.desktop._scrap_mechanic_window", return_value=0),
            ):
                with self.assertRaisesRegex(RuntimeError, "exit code 7"):
                    _launch_map_companion(launcher, timeout=5)
            self.assertIn("failed-launch", launch_log.read_text(encoding="utf-8"))

    def test_installer_removes_old_hooks_and_uses_external_tracking(self):
        installer = (ROOT / "install_companion_mod.ps1").read_text(encoding="utf-8")
        self.assertIn("& $CleanupScript -GameRoot $GameRoot", installer)
        self.assertNotIn("install_live_tracking.ps1", installer)
        self.assertNotIn("$ExternalOnly", installer)
        self.assertNotIn("SurvivalIntegration.lua", installer)
        self.assertNotIn("Copy-Item -LiteralPath $sourceFolder", installer)
        self.assertIn("Disabled-CustomGame-$stamp", installer)
        self.assertIn("Read-only live player tracking is enabled", installer)

        uninstaller = (ROOT / "uninstall_companion_mod.ps1").read_text(encoding="utf-8")
        self.assertIn('$survivalPackageTarget = Join-Path $GameRoot "Survival\\MapCompanion"', uninstaller)
        self.assertIn("Move-VerifiedCompanionDirectory", uninstaller)
        self.assertNotIn("Remove-Item -LiteralPath $integrationTarget -Recurse", uninstaller)

        builder = (ROOT / "build_companion_map.py").read_text(encoding="utf-8")
        self.assertIn("scrap_mechanic_process_ids", builder)
        self.assertIn("not allowed to open a live game database", builder)

    def test_overlay_owns_m_key_and_launcher_has_no_detached_monitor(self):
        launcher = (ROOT / "Launch Scrap Mechanic with Map Companion.cmd").read_text(encoding="utf-8")
        attach_only = (ROOT / "Start Map Companion after Scrap Mechanic.cmd").read_text(encoding="utf-8")
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        self.assertNotIn("map_key_monitor.ps1", launcher)
        self.assertNotIn("-StartDetached", launcher)
        self.assertNotIn("powershell.exe", launcher.casefold())
        self.assertIn("steam://rungameid/387990", launcher)
        self.assertIn("--check-only", launcher)
        self.assertNotIn("LOGBOOK_SCRIPT", launcher)
        self.assertIn("companion_overlay.py", launcher)
        self.assertIn("ScrapMechanicMapOverlay.exe", launcher)
        self.assertNotIn("GAME_ROOT", launcher)
        self.assertIn('"%OVERLAY_SCRIPT%" --check-only', launcher)
        self.assertNotIn("-applaunch", launcher)
        self.assertNotIn("387990 -dev", launcher)
        self.assertNotIn("install_live_tracking.ps1", launcher)
        self.assertIn("tasklist", launcher)
        self.assertNotIn("Reinstalling it before launch", launcher)
        self.assertIn("ScrapMechanic.exe", attach_only)
        self.assertIn("companion_overlay.py", attach_only)
        self.assertNotIn("map_key_monitor.ps1", attach_only)
        self.assertNotIn("powershell.exe", attach_only.casefold())
        self.assertNotIn("steam.exe", attach_only.casefold())
        self.assertNotIn("-applaunch", attach_only)
        self.assertNotIn("-dev", attach_only)
        self.assertIn("virtual_key_down(0x4D)", overlay)
        self.assertIn("virtual_key_down(0x1B)", overlay)
        self.assertIn("if game_context and m_down", overlay)

    def test_live_tracker_recognizes_the_player_view_record(self):
        valid = (-2346.6, -2628.7, 9.3, 0.1, 2048.0, 1440.0, -0.55, -0.833, -0.039, 1.0)
        self.assertTrue(_valid_row(valid))
        self.assertFalse(_valid_row((*valid[:3], 0.2, *valid[4:])))
        self.assertEqual(ScrapMechanicLiveTracker._distance(valid, None), 0.0)
        origin = (0.0, 0.0, 0.0, *valid[3:])
        self.assertEqual(ScrapMechanicLiveTracker._distance(origin, valid[:3]), float("inf"))

    def test_live_tracker_switches_from_snapshot_to_changing_record(self):
        tracker = ScrapMechanicLiveTracker()
        frozen = (-2346.6, -2628.7, 9.3, 0.1, 2048.0, 1440.0, -0.55, -0.833, -0.039, 1.0)
        active = (-2346.2, -2628.6, 9.3, *frozen[3:])
        tracker._address = 10
        tracker._candidates = [10, 20]
        tracker._candidate_values = {10: frozen, 20: frozen}
        tracker._selected_quiet_since = 0.5
        selected = frozen
        with patch("companion_mod.live_process_tracker.time.monotonic", side_effect=(1.0, 1.1, 1.2)):
            for step in range(3):
                moving = (active[0] + step * 0.2, *active[1:])
                with patch.object(
                    tracker,
                    "_read_row",
                    side_effect=lambda address, value=moving: frozen if address == 10 else value,
                ):
                    selected = tracker._probe_candidates(selected)
        self.assertEqual(tracker._address, 20)
        self.assertGreater(selected[0], active[0])

    def test_live_tracker_ignores_one_shot_autosave_snapshot_jump(self):
        tracker = ScrapMechanicLiveTracker()
        live = (-100.0, -100.0, 9.0, 0.1, 1920.0, 1080.0, 0.0, 1.0, 0.0, 1.0)
        stale = (-900.0, -900.0, 9.0, *live[3:])
        tracker._address = 10
        tracker._candidates = [10, 20]
        tracker._candidate_values = {10: live, 20: live}
        tracker._candidate_motion_streaks = {10: 0, 20: 0}
        tracker._selected_quiet_since = 0.5
        moved = (-99.5, -100.0, 9.0, *live[3:])
        with (
            patch.object(tracker, "_read_row", side_effect=lambda address: moved if address == 10 else stale),
            patch("companion_mod.live_process_tracker.time.monotonic", return_value=1.0),
        ):
            selected = tracker._probe_candidates(live)
        self.assertEqual(tracker._address, 10)
        self.assertEqual(selected, moved)

    def test_live_tracker_rejects_a_selected_autosave_rollback(self):
        tracker = ScrapMechanicLiveTracker()
        live = (-100.0, -100.0, 9.0, 0.1, 1920.0, 1080.0, 0.0, 1.0, 0.0, 1.0)
        stale = (-900.0, -900.0, 9.0, *live[3:])
        tracker._address = 10
        tracker._candidates = [10, 20]
        tracker._candidate_values = {10: live, 20: live}
        tracker._candidate_motion_streaks = {10: 4, 20: 0}
        tracker._selected_motion_samples = 10
        tracker._last_accepted_values = live
        with (
            patch.object(tracker, "_read_row", side_effect=lambda address: stale if address == 10 else live),
            patch("companion_mod.live_process_tracker.time.monotonic", return_value=1.0),
        ):
            selected = tracker._probe_candidates(live)
        self.assertEqual(tracker._address, 10)
        self.assertEqual(selected, live)

    def test_live_tracker_never_accepts_a_repeated_static_autosave_rollback(self):
        tracker = ScrapMechanicLiveTracker()
        live = (-100.0, -100.0, 9.0, 0.1, 1920.0, 1080.0, 0.0, 1.0, 0.0, 1.0)
        stale = (-900.0, -900.0, 9.0, *live[3:])
        tracker._address = 10
        tracker._candidates = [10, 20]
        tracker._candidate_values = {10: live, 20: live}
        tracker._candidate_motion_streaks = {10: 4, 20: 0}
        tracker._selected_motion_samples = 10
        tracker._last_accepted_values = live
        selected = live
        for probe in range(24):
            if not tracker._address:
                break
            with (
                patch.object(tracker, "_read_row", side_effect=lambda address: stale if address == 10 else live),
                patch(
                    "companion_mod.live_process_tracker.time.monotonic",
                    return_value=1.0 + probe * 0.06,
                ),
            ):
                # ``read`` passes the row freshly read from the selected
                # address, which remains the stale snapshot after its first
                # one-shot update.
                selected = tracker._probe_candidates(stale)
            self.assertEqual(selected, live)
        self.assertEqual(tracker._address, 10)
        self.assertEqual(tracker._last_accepted_values, live)

    def test_live_tracker_slices_large_candidate_audits(self):
        tracker = ScrapMechanicLiveTracker()
        live = (-100.0, -100.0, 9.0, 0.1, 1920.0, 1080.0, 0.0, 1.0, 0.0, 1.0)
        tracker._address = 1000
        tracker._candidates = list(range(1000, 1256))
        tracker._candidate_values = {address: live for address in tracker._candidates}
        tracker._candidate_motion_streaks = {address: 0 for address in tracker._candidates}
        tracker._last_accepted_values = live
        with (
            patch.object(tracker, "_read_row", return_value=live) as read_row,
            patch("companion_mod.live_process_tracker.time.monotonic", return_value=1.0),
        ):
            self.assertEqual(tracker._probe_candidates(live), live)
        self.assertLessEqual(read_row.call_count, 9)
        self.assertEqual(len(tracker._candidates), 256)
        self.assertEqual(tracker._candidate_probe_cursor, 8)

    def test_live_tracker_recovers_when_proven_row_becomes_stale(self):
        tracker = ScrapMechanicLiveTracker()
        live = (-100.0, -100.0, 9.0, 0.1, 1920.0, 1080.0, 0.0, 1.0, 0.0, 1.0)
        stale = (-900.0, -900.0, 9.0, *live[3:])
        tracker._address = 10
        tracker._candidates = [10, 20]
        tracker._candidate_values = {10: live, 20: live}
        tracker._candidate_motion_streaks = {10: 4, 20: 0}
        tracker._selected_motion_samples = 10
        tracker._last_accepted_values = live
        selected = live
        for probe, alternate_x in enumerate((-99.8, -99.6), start=1):
            alternate = (alternate_x, *live[1:])
            with (
                patch.object(
                    tracker,
                    "_read_row",
                    side_effect=lambda address, value=alternate: stale if address == 10 else value,
                ),
                patch(
                    "companion_mod.live_process_tracker.time.monotonic",
                    return_value=1.0 + probe * 0.02,
                ),
            ):
                selected = tracker._probe_candidates(stale)
        self.assertEqual(tracker._address, 20)
        self.assertEqual(selected[0], -99.6)

    def test_live_tracker_uses_tiny_quiet_audits_after_record_is_proven(self):
        tracker = ScrapMechanicLiveTracker()
        live = (-100.0, -100.0, 9.0, 0.1, 1920.0, 1080.0, 0.0, 1.0, 0.0, 1.0)
        tracker._address = 1000
        tracker._candidates = list(range(1000, 1256))
        tracker._candidate_values = {address: live for address in tracker._candidates}
        tracker._candidate_motion_streaks = {address: 0 for address in tracker._candidates}
        tracker._selected_motion_samples = 8
        tracker._selected_quiet_since = 0.0
        tracker._last_accepted_values = live
        with (
            patch.object(tracker, "_read_row", return_value=live) as read_row,
            patch("companion_mod.live_process_tracker.time.monotonic", return_value=1.0),
        ):
            self.assertEqual(tracker._probe_candidates(live), live)
        self.assertLessEqual(read_row.call_count, 3)
        self.assertEqual(tracker._candidate_probe_cursor, 2)

    def test_live_tracker_advances_when_only_one_live_record_exists(self):
        tracker = ScrapMechanicLiveTracker()
        first = (-100.0, -100.0, 9.0, 0.1, 1920.0, 1080.0, 0.0, 1.0, 0.0, 1.0)
        moved = (-99.5, -100.0, 9.0, *first[3:])
        tracker._address = 10
        tracker._candidates = [10]
        tracker._last_accepted_values = first
        with patch("companion_mod.live_process_tracker.time.monotonic", return_value=1.0):
            selected = tracker._probe_candidates(moved)
        self.assertEqual(selected, moved)
        self.assertEqual(tracker._last_accepted_values, moved)

    def test_live_tracker_rejects_single_record_autosave_rollback(self):
        tracker = ScrapMechanicLiveTracker()
        live = (-100.0, -100.0, 9.0, 0.1, 1920.0, 1080.0, 0.0, 1.0, 0.0, 1.0)
        stale = (-900.0, -900.0, 9.0, *live[3:])
        tracker._address = 10
        tracker._candidates = [10]
        tracker._selected_motion_samples = 10
        tracker._last_accepted_values = live
        with patch("companion_mod.live_process_tracker.time.monotonic", return_value=1.0):
            selected = tracker._probe_candidates(stale)
        self.assertEqual(selected, live)
        self.assertEqual(tracker._address, 10)

    def test_map_key_monitor_debounces_rapid_transitions(self):
        monitor = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        self.assertIn("last_key_action", monitor)
        self.assertIn(">= 0.14", monitor)
        self.assertIn("m_was_down", monitor)
        self.assertIn("companion_context and escape_down", monitor)
        page = (ROOT.parent / "scrap_editor_app" / "web" / "companion_overlay.js").read_text(encoding="utf-8")
        self.assertNotIn('event.key === "Escape"', page)

    def test_external_minimap_is_click_through_and_directional(self):
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        page = (ROOT.parent / "scrap_editor_app" / "web" / "companion_minimap.html").read_text(encoding="utf-8")
        script = (ROOT.parent / "scrap_editor_app" / "web" / "companion_minimap.js").read_text(encoding="utf-8")
        css = (ROOT.parent / "scrap_editor_app" / "web" / "companion_minimap.css").read_text(encoding="utf-8")
        self.assertIn("class NativeMinimapHost", overlay)
        self.assertIn("0x08000000", overlay)
        self.assertIn("focus=False", overlay)
        self.assertIn("configure_minimap_window", overlay)
        self.assertIn("0x08000000", overlay)
        self.assertIn('id="minimap"', page)
        self.assertIn('id="progressBar"', page)
        self.assertIn("drawPlayer", script)
        self.assertIn("drawDiamond", script)
        self.assertIn("overlay.progress", script)
        self.assertIn("overlay.mapReady", script)
        self.assertIn("visibleWorldSpan", script)
        self.assertNotIn("Waiting for live position", script)
        self.assertNotIn("coordinate", page.casefold())
        self.assertRegex(css, r"width:\s*100%")
        self.assertIn("minimap-live", script)
        self.assertIn("requestAnimationFrame", script)
        self.assertIn("target=self._sample_live_process", overlay)
        self.assertNotIn("target=self._push_live_minimap", overlay)
        self.assertIn("window.companionMinimapLive", script)
        self.assertIn("window.companionMinimapState", script)
        self.assertNotIn("_push_minimap_state", overlay)
        self.assertIn("Math.min(60", script)
        self.assertIn("shortestAngle", script)
        self.assertIn("worldFogCanvas", script)
        self.assertIn("worldFogSampleKeys", script)
        self.assertIn("explorationSampleKeys", script)
        self.assertIn("waypointDiscoveryCache", script)
        self.assertIn("worldFogPendingSamples", script)
        self.assertNotIn("const known = new Set((exploration.samples", script)
        self.assertIn('waypointSymbols?.drawCanvas?.(playerContext, "player", 0, 0, 15)', script)
        self.assertNotIn("Real 3D map ready", script)
        self.assertIn("period = 1.0 / 60.0", overlay)
        self.assertIn("now - last_process_check >= 0.5", overlay)

    def test_minimap_focus_changes_z_order_without_hiding_the_surface(self):
        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            bridge._minimap_visible = True
            bridge._minimap_foreground_active = True
            bridge._in_game_world = True
            try:
                with patch(f"{OverlayBridge.__module__}.set_window_topmost") as topmost:
                    bridge.set_minimap_foreground(True)
                    topmost.assert_not_called()
                    bridge.set_minimap_foreground(False)
                    topmost.assert_called_once_with("Scrap Mechanic Minimap", False)
                    bridge.set_minimap_foreground(False)
                    self.assertEqual(topmost.call_count, 1)
                    bridge.set_minimap_foreground(True)
                    self.assertEqual(topmost.call_count, 2)
            finally:
                bridge.shutdown()

    def test_monitor_does_not_gate_minimap_show_hide_on_transient_focus(self):
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        self.assertNotIn("context_visible", overlay)
        self.assertIn("minimap_resident", overlay)
        self.assertIn("bridge.set_minimap_foreground", overlay)
        self.assertIn("configure_minimap_window(chosen, retries=1, promote=False)", overlay)

    def test_full_map_uses_light_overview_during_interaction_and_idle_detail(self):
        page = (ROOT.parent / "scrap_editor_app" / "web" / "companion_overlay.js").read_text(encoding="utf-8")
        css = (ROOT.parent / "scrap_editor_app" / "web" / "companion_overlay.css").read_text(encoding="utf-8")
        self.assertIn("state.minimapImage || state.image", page)
        self.assertIn("setInteracting(true)", page)
        self.assertIn("sourceDemand < 1", page)
        self.assertIn("detailTileNodes.size > keep", page)
        self.assertNotIn("interacting #detailTiles", css)
        self.assertIn("content-visibility:auto", css)

    def test_waypoint_symbols_are_shared_between_both_map_views(self):
        web = ROOT.parent / "scrap_editor_app" / "web"
        symbols = (web / "companion_waypoints.js").read_text(encoding="utf-8")
        full_page = (web / "companion_overlay.html").read_text(encoding="utf-8")
        mini_page = (web / "companion_minimap.html").read_text(encoding="utf-8")
        full_script = (web / "companion_overlay.js").read_text(encoding="utf-8")
        mini_script = (web / "companion_minimap.js").read_text(encoding="utf-8")
        self.assertIn("companion_waypoints.js", full_page)
        self.assertIn("companion_waypoints.js", mini_page)
        self.assertIn("waypointSymbols.svg(category)", full_script)
        self.assertIn("waypointSymbols.drawCanvas", mini_script)
        self.assertIn("canvasImage(category)", symbols)
        self.assertIn("drawFallback(context", symbols)
        self.assertIn('const player = {', symbols)
        self.assertIn('waypointSymbols.svg("player")', full_script)
        self.assertIn('waypointSymbols?.drawCanvas?.(playerContext, "player"', mini_script)
        self.assertNotIn("clip-path:polygon", (web / "companion_overlay.css").read_text(encoding="utf-8"))
        for command in (
            "context.arc", "context.moveTo", "context.lineTo",
            "context.bezierCurveTo", "context.strokeRect",
        ):
            self.assertIn(command, symbols)
        self.assertNotIn("new Path2D(item.path)", symbols)
        self.assertNotIn('context.clip();\n    if (waypoint.category', mini_script)

    def test_minimap_keeps_waypoints_outside_the_volatile_player_feed(self):
        script = (
            ROOT.parent / "scrap_editor_app" / "web" / "companion_minimap.js"
        ).read_text(encoding="utf-8")
        self.assertIn("let staticWaypoints = [];", script)
        self.assertIn("staticWaypoints = waypoints;", script)
        self.assertIn("for (const waypoint of staticWaypoints)", script)

    def test_fog_and_waypoint_settings_are_normalized_without_losing_categories(self):
        settings = normalize_companion_settings({
            "fogOfWar": False,
            "fogRadius": 9999,
            "waypointFilters": {
                "quests": {"fullMap": False, "minimap": True},
                "beacons": {"fullMap": True, "minimap": False},
            },
        })
        self.assertFalse(settings["fogOfWar"])
        self.assertEqual(settings["fogRadius"], 512)
        self.assertEqual(settings["revealShape"], "directional")
        self.assertFalse(settings["waypointFilters"]["quests"]["fullMap"])
        self.assertFalse(settings["waypointFilters"]["beacons"]["minimap"])
        self.assertTrue(settings["waypointFilters"]["stations"]["fullMap"])
        self.assertEqual(normalize_companion_settings({"revealShape": "circle"})["revealShape"], "circle")
        self.assertEqual(normalize_companion_settings({"revealShape": "wide"})["revealShape"], "directional")

    def test_legacy_hud_rate_is_migrated_and_player_has_an_independent_layer(self):
        self.assertEqual(normalize_companion_settings({
            "schema": 2, "updateHz": 24,
        })["updateHz"], 60)
        self.assertEqual(normalize_companion_settings({
            "schema": 3, "updateHz": 24,
        })["updateHz"], 60)
        self.assertEqual(normalize_companion_settings({
            "schema": 4, "updateHz": 24,
        })["updateHz"], 24)
        web = ROOT.parent / "scrap_editor_app" / "web"
        page = (web / "companion_minimap.html").read_text(encoding="utf-8")
        script = (web / "companion_minimap.js").read_text(encoding="utf-8")
        self.assertIn('id="playerLayer"', page)
        self.assertIn("const playerContext", script)
        self.assertNotIn("1000 / 30", script)
        self.assertIn("mapLayerDirty", script)
        self.assertNotIn("refreshFull(); },5000", script)
        self.assertIn("targetVelocity", script)
        self.assertIn("targetAngularVelocity", script)
        self.assertIn("playerContext.clearRect", script)
        self.assertIn("const pullHz=configuredHz", script)
        self.assertNotIn("Math.min(30,configuredHz)", script)
        self.assertIn("nextLivePoll+=liveInterval", script)
        self.assertNotIn("nextLivePoll=now+Math.max", script)
        self.assertIn("exploration.samples.push(normalized)", script)
        self.assertNotIn("exploration.samples = [...(exploration.samples", script)
        self.assertNotIn("worldFogPendingSamples.push(normalized);\n        waypointDiscoveryCache.clear()", script)
        self.assertIn("presentMapBetweenDraws", script)
        self.assertIn("const mapInterval = 1000 / 60", script)
        self.assertIn("will-change: transform", (web / "companion_minimap.css").read_text(encoding="utf-8"))
        self.assertNotIn('mapImage.decoding = "async"', script)
        self.assertNotIn("imageLoadStartedAt", script)
        self.assertIn("if (uri === imageUri && imageLoading) return;", script)
        self.assertIn("mapImage.naturalWidth > 0", script)
        self.assertIn("previousOverlay.mapReady !== nextOverlay.mapReady", script)
        self.assertIn("priorOverlay.mapReady!==baseState.overlay?.mapReady", script)
        self.assertIn("requestAnimationFrame(frame);\n    renderFrame(now);", script)
        self.assertIn("function renderFrame(now) {\n    try {", script)
        self.assertIn("window.__minimapLastFrameError", script)
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        self.assertIn("remaining = period - elapsed", overlay)
        self.assertIn("time.sleep(remaining)", overlay)
        self.assertNotIn("wait(max(0.001, period - elapsed))", overlay)

    def test_minimap_webview_is_never_background_throttled(self):
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        for flag in (
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "CalculateNativeWinOcclusion",
            "IntensiveWakeUpThrottling",
        ):
            self.assertIn(flag, overlay)

    def test_full_map_markers_are_screen_space_vectors_at_every_zoom(self):
        web = ROOT.parent / "scrap_editor_app" / "web"
        page = (web / "companion_overlay.html").read_text(encoding="utf-8")
        script = (web / "companion_overlay.js").read_text(encoding="utf-8")
        css = (web / "companion_overlay.css").read_text(encoding="utf-8")
        stage_end = page.index('</div>\n    <div id="markers"')
        stage_start = page.index('<div id="mapStage">')
        self.assertGreater(stage_end, stage_start)
        self.assertIn("view.x + mapX * view.scale", script)
        self.assertIn("view.y + mapY * view.scale", script)
        self.assertIn("updateMarkerScreenPositions()", script)
        self.assertNotIn("--marker-scale", script)
        self.assertNotIn("scale(var(--marker-scale", css)
        self.assertIn("#markers { position:fixed", css)

    def test_live_tracker_does_not_repeat_a_trusted_row_for_fifty_ms(self):
        tracker = ScrapMechanicLiveTracker()
        before = (1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        after = (1.25, 2.0, 3.0, 0.0, 0.0, 0.0, 0.01, 0.99995, 0.0, 0.0)
        tracker._last_accepted_values = before
        tracker._last_candidate_probe = 1.0
        tracker._candidates = [1234]
        tracker._selected_motion_samples = 4
        with patch("companion_mod.live_process_tracker.time.monotonic", return_value=1.016):
            selected = tracker._probe_candidates(after)
        self.assertEqual(selected, after)

    def test_minimap_uses_shared_native_renderer_and_live_push(self):
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        self.assertIn("class NativeMinimapLivePush", overlay)
        self.assertIn("class NativeMinimapHost", overlay)
        self.assertIn("core.PostWebMessageAsJson(payload)", overlay)
        self.assertIn("core.PostSharedBufferToScript(", overlay)
        self.assertIn("Environment.CreateSharedBuffer", overlay)
        self.assertIn("PixelFormat.Format32bppArgb", overlay)
        self.assertIn("self.picture.Invalidate()", overlay)
        self.assertIn("bridge._live_push_sink = publish_live", overlay)
        self.assertIn("full_map_push.publish(state)", overlay)
        self.assertIn("bridge._minimap_window = native_host", overlay)
        self.assertIn("bridge._shared_frame_sink = native_host.shared_frame_ready", overlay)
        self.assertIn("native_host.prepare_shared_frames((full_map_form,))", overlay)
        self.assertIn("native_host.post_shared_frames()", overlay)
        self.assertLess(
            overlay.index("native_host.prepare_shared_frames((full_map_form,))"),
            overlay.index("minimap.load_url(minimap_uri)"),
        )
        self.assertLess(
            overlay.index("window.load_url(full_map_uri)"),
            overlay.index("native_host.post_shared_frames()"),
        )
        self.assertGreaterEqual(overlay.count("bootstrap_uri,"), 2)
        self.assertIn('MINIMAP_RENDERER_TITLE = "Scrap Mechanic Minimap Renderer"', overlay)
        self.assertIn("MINIMAP_RENDERER_TITLE,\n        bootstrap_uri", overlay)
        script = (
            ROOT.parent / "scrap_editor_app" / "web" / "companion_minimap.js"
        ).read_text(encoding="utf-8")
        self.assertIn('event.data?.type === "minimap-live"', script)
        self.assertIn('addEventListener("sharedbufferreceived"', script)
        self.assertIn("nativeSharedWords[0] = 1", script)
        self.assertIn("swapRedBlue(pixels * pixels)", script)
        self.assertIn("if (sharedTransportReady && nativeSharedWords[0] !== 0) return;", script)
        self.assertIn('api("minimap-frame-signal")', script)
        self.assertIn("!nativeSignalInFlight", script)
        self.assertLess(
            script.index("if (sharedTransportReady && nativeSharedWords[0] !== 0) return;"),
            script.index("if (!Number.isFinite(nextNativeFrameAt)"),
        )
        self.assertIn("do { nextNativeFrameAt += nativeInterval; } while (nextNativeFrameAt <= now);", script)
        self.assertNotIn("now - lastNativeFrameAt < 1000 / configuredHz", script)
        self.assertIn("setTarget(event.data.live)", script)
        self.assertIn("!baseState.overlay?.livePush", script)
        self.assertIn("publishNativeFrame(now);", script)
        self.assertIn("baseState.overlay?.mapReady && !baseState.config?.ready", script)
        self.assertIn("if (fullStateRequest) return fullStateRequest;", script)
        self.assertNotIn('apiWithin("minimap-state"', script)
        self.assertIn("if (uri === imageUri && imageLoading) return;", script)
        self.assertNotIn("imageLoadStartedAt", script)
        self.assertNotIn('api("minimap-frame"', script)
        self.assertNotIn('api("minimap-diagnostic"', script)
        self.assertNotIn('api("minimap-ready"', script)
        self.assertIn("bitmap_ready = self._minimap_bitmap_ready", overlay)
        self.assertIn("self._minimap_bitmap_ready = True", overlay)
        minimap_css = (
            ROOT.parent / "scrap_editor_app" / "web" / "companion_minimap.css"
        ).read_text(encoding="utf-8")
        self.assertIn("background: transparent !important", minimap_css)

    def test_exploration_is_persistent_and_discovers_nearby_pois(self):
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
            signal = Path(temporary) / "runtime" / "signal.json"
            save = Path(temporary) / "world.db"
            first = OverlayBridge(signal)
            try:
                first._live_waypoints = [
                    {"key": "near", "category": "stations", "worldX": 100.0, "worldY": 0.0},
                    {"key": "far", "category": "stations", "worldX": 900.0, "worldY": 0.0},
                    {"key": "beacon-1", "category": "beacons", "worldX": 2000.0, "worldY": 0.0},
                ]
                first._in_game_world = True
                first._load_exploration(save, 42)
                cell, discovered = first._update_exploration({"x": 0.0, "y": 0.0})
                first._save_exploration()
                self.assertEqual(cell, [0, 0])
                self.assertIn("near", discovered)
                self.assertNotIn("far", first._discovered_features)
                self.assertIn("beacon-1", first._discovered_features)
            finally:
                first.shutdown()

            second = OverlayBridge(signal)
            try:
                second._live_waypoints = []
                second._load_exploration(save, 42)
                self.assertIn("0,0", second._explored_cells)
                self.assertIn("near", second._discovered_features)
                self.assertIn("beacon-1", second._discovered_features)
            finally:
                second.shutdown()

    def test_directional_exploration_is_arrow_centered_and_persistent(self):
        radius = 192.0
        self.assertAlmostEqual(directional_reveal_factor(0.0), 1.0)
        self.assertAlmostEqual(directional_reveal_factor(math.pi), 0.14)
        self.assertTrue(directional_reveal_contains(0, 0, 0, 190, 0, 1, radius))
        self.assertFalse(directional_reveal_contains(0, 0, 0, -40, 0, 1, radius))
        self.assertTrue(directional_reveal_contains(0, 0, 100, 0, 0, 1, radius))

        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
            save = Path(temporary) / "world.db"
            first = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            try:
                first._in_game_world = True
                first._load_exploration(save, 88)
                first._update_exploration(
                    {"x": 13.2, "y": -7.4}, {"x": 1.0, "y": 0.0},
                )
                first._save_exploration()
                self.assertIn("12,-8,90", first._explored_sight)
            finally:
                first.shutdown()
            second = OverlayBridge(Path(temporary) / "runtime2" / "signal.json")
            try:
                second._load_exploration(save, 88)
                self.assertIn("12,-8,90", second._explored_sight)
                self.assertIn([12, -8, 90], second.exploration_state()["samples"])
            finally:
                second.shutdown()

    def test_both_maps_draw_the_same_directional_fog_footprint(self):
        web = ROOT.parent / "scrap_editor_app" / "web"
        full = (web / "companion_overlay.js").read_text(encoding="utf-8")
        mini = (web / "companion_minimap.js").read_text(encoding="utf-8")
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        for script in (full, mini):
            self.assertIn("function traceDirectionalReveal", script)
            self.assertIn("exploration.samples", script)
            self.assertIn(".14 + .86 * Math.pow(forward, .65)", script)
            self.assertIn("FOG_SEAM_WORLD", script)
            self.assertIn("legacyCellCount", script)
        self.assertIn('"explorationSample"', overlay)
        self.assertIn('"sight": sorted(sight)', overlay)

    def test_circle_reveal_is_shared_and_full_map_has_live_controls(self):
        self.assertTrue(reveal_contains(0, 0, 0, -190, 0, 1, 192, "circle"))
        self.assertFalse(reveal_contains(0, 0, 0, -200, 0, 1, 192, "circle"))
        web = ROOT.parent / "scrap_editor_app" / "web"
        for name in ("companion_overlay.js", "companion_minimap.js"):
            script = (web / name).read_text(encoding="utf-8")
            self.assertIn('revealShape === "circle"', script)
            self.assertIn("function traceReveal", script)
        full_page = (web / "companion_overlay.html").read_text(encoding="utf-8")
        editor_page = (web / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="quickSettings"', full_page)
        self.assertIn('id="waypointTooltip"', full_page)
        self.assertIn('id="waypointDetails"', full_page)
        self.assertIn('id="waypointDetailsPreview"', full_page)
        self.assertIn('src="companion_minimap.html?preview=1"', editor_page)
        self.assertIn('src="companion_waypoints.js"', editor_page)
        self.assertIn(".companion-preview-key[hidden]", (
            web / "desktop.css"
        ).read_text(encoding="utf-8"))

    def test_native_minimap_region_removes_opaque_host_rectangle(self):
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        self.assertIn("bridge._minimap_window = native_host", overlay)
        self.assertIn("form.Text = MINIMAP_TITLE", overlay)
        self.assertIn("picture = WinForms.PictureBox()", overlay)
        self.assertIn("apply_minimap_window_region(hwnd, size, chosen)", overlay)
        self.assertIn("CreateRoundRectRgn", overlay)
        self.assertIn("CreateEllipticRgn", overlay)
        self.assertIn("SetWindowRgn", overlay)
        self.assertIn("GetWindowRgn", overlay)
        self.assertIn("GetClientRect", overlay)
        self.assertIn("ClientToScreen", overlay)
        self.assertIn("GetAncestor", overlay)
        self.assertIn("client_offset_x", overlay)
        self.assertIn("GetDpiForWindow", overlay)
        self.assertNotIn('api("minimap-frame"', (
            ROOT.parent / "scrap_editor_app" / "web" / "companion_minimap.js"
        ).read_text(encoding="utf-8"))

    def test_native_minimap_region_rejects_same_size_plain_rectangle(self):
        bounds = (0, 0, 280, 280)
        self.assertFalse(
            _native_region_matches(2, 2, bounds, bounds, curved=True)
        )
        self.assertTrue(
            _native_region_matches(3, 3, bounds, bounds, curved=True)
        )
        self.assertTrue(
            _native_region_matches(2, 2, bounds, bounds, curved=False)
        )
        self.assertFalse(
            _native_region_matches(3, 3, (0, 0, 282, 280), bounds, curved=True)
        )

    def test_full_map_smooths_player_and_uses_larger_vector_markers(self):
        web = ROOT.parent / "scrap_editor_app" / "web"
        script = (web / "companion_overlay.js").read_text(encoding="utf-8")
        css = (web / "companion_overlay.css").read_text(encoding="utf-8")
        self.assertIn("function animatePlayer", script)
        self.assertIn("requestAnimationFrame(animatePlayer)", script)
        self.assertIn("setInterval(refreshLiveState, 50)", script)
        self.assertIn("width:34px; height:43px", css)
        self.assertIn("width:42px; height:42px", css)

    def test_full_map_removes_only_small_enclosed_fog_components(self):
        script = (
            ROOT.parent / "scrap_editor_app" / "web" / "companion_overlay.js"
        ).read_text(encoding="utf-8")
        self.assertIn("function clearEnclosedFogIslands", script)
        self.assertIn("touchesBoundary", script)
        self.assertIn("new Uint8Array", script)
        self.assertIn("new Int32Array", script)
        self.assertIn("clearEnclosedFogIslands(null, 1536)", script)
        self.assertIn("neighbours <= 3", script)
        self.assertIn("alpha <= 24", script)

    def test_legacy_isolated_menu_cell_is_migrated_and_jump_is_rejected(self):
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
            signal = Path(temporary) / "runtime" / "signal.json"
            save = Path(temporary) / "world.db"
            bridge = OverlayBridge(signal)
            identity = f"{save.resolve()}|73"
            exploration = bridge._exploration_path(identity)
            exploration.parent.mkdir(parents=True, exist_ok=True)
            exploration.write_text(json.dumps({
                "schema": 1,
                "cells": ["-30,-27", "-31,-27", "-31,-28", "-32,-28", "2,0"],
                "discovered": [],
            }), encoding="utf-8")
            try:
                bridge._in_game_world = True
                bridge._load_exploration(save, 73)
                self.assertNotIn("2,0", bridge._explored_cells)
                bridge._update_exploration({"x": -1950.0, "y": -1750.0})
                returned, _ = bridge._update_exploration({"x": 128.0, "y": 0.0})
                self.assertEqual(returned, [-31, -28])
                self.assertNotIn("2,0", bridge._explored_cells)
            finally:
                bridge.shutdown()

    def test_first_unload_camera_sample_cannot_become_exploration_origin(self):
        with TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ):
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            try:
                bridge._in_game_world = True
                bridge._live_expected = (-1950.0, -1750.0, 25.0)
                bridge._load_exploration(Path(temporary) / "world.db", 81)
                bridge._update_exploration({"x": 128.0, "y": 0.0})
                self.assertFalse(bridge._explored_cells)
                bridge._update_exploration({"x": -1950.0, "y": -1750.0})
                self.assertIn("-31,-28", bridge._explored_cells)
            finally:
                bridge.shutdown()

    def test_published_full_map_keeps_lossless_master_and_bounded_minimap(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "master.png"
            Image.new("RGB", (5000, 16), (20, 100, 130)).save(source, "PNG")
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            bridge._loaded_path = str(root / "world.db")
            try:
                published = bridge.publish_companion_map({
                    "image": source.resolve().as_uri(),
                    "bounds": [-2, 2, -1, 1],
                    "sourceSave": "world.db",
                    "sourceSeed": 7,
                })
                config = published["config"]
                self.assertEqual(config["mapWidth"], 5000)
                self.assertEqual(config["minimapWidth"], 4096)
                self.assertEqual(bridge._path_from_uri(config["externalMap"]), source.resolve())
                self.assertTrue(bridge._path_from_uri(config["minimapMap"]).is_file())
            finally:
                bridge.shutdown()

    def test_full_map_uses_exact_world_coordinates_and_discovery_filters(self):
        script = (ROOT.parent / "scrap_editor_app" / "web" / "companion_overlay.js").read_text(encoding="utf-8")
        minimap = (ROOT.parent / "scrap_editor_app" / "web" / "companion_minimap.js").read_text(encoding="utf-8")
        self.assertIn("function mapPointWorld", script)
        self.assertIn("mapPointWorld(position.x, position.y)", script)
        self.assertNotIn("mapPointCells", script)
        self.assertIn("waypointFilters", script)
        self.assertIn("exploration.discovered", script)
        self.assertIn("exploration.points", script)
        self.assertIn('fogContext.fillStyle = "#000"', script)
        self.assertIn("drawFog", minimap)
        self.assertIn("drawWaypoints", minimap)
        self.assertIn("visibleRadiusWorld", minimap)

    def test_exact_atlas_water_restores_lakes_and_clips_square_spill(self):
        capture = Image.new("RGB", (64, 32), (110, 170, 55))
        # Simulate the WebGL failure: a large square water plane crosses land
        # while the real lake on the left did not render at all.
        capture.paste((20, 105, 140), (42, 8, 62, 28))
        atlas = Image.new("RGB", (32, 16), (110, 170, 55))
        atlas.paste((12, 90, 125), (0, 0, 13, 16))
        result = restore_exact_atlas_water(capture, atlas)
        lake = result.getpixel((8, 16))
        clipped_land = result.getpixel((54, 16))
        self.assertGreater(lake[2], lake[1])
        self.assertGreater(lake[1], lake[0])
        self.assertGreater(clipped_land[1], clipped_land[2])

    def test_small_enclosed_atlas_fragments_are_removed_from_ocean(self):
        mask = Image.new("L", (64, 48), 255)
        # A helper rectangle embedded in open water is an atlas artifact.
        mask.paste(0, (20, 14, 28, 21))
        # Edge-connected land is a real coastline and must survive.
        mask.paste(0, (0, 8, 7, 35))
        # Thin helper strips can be clipped by an atlas edge. They are not a
        # coastline and must not turn into green rectangles in open ocean.
        mask.paste(0, (25, 0, 58, 1))
        cleaned = _remove_small_water_mask_holes(mask)
        self.assertEqual(cleaned.getpixel((23, 17)), 255)
        self.assertEqual(cleaned.getpixel((2, 16)), 0)
        self.assertEqual(cleaned.getpixel((40, 0)), 255)

    def test_teal_atlas_helpers_touching_water_join_the_water_mask(self):
        atlas = Image.new("RGB", (48, 24), (110, 170, 55))
        atlas.paste((12, 90, 125), (0, 0, 20, 24))
        # Dark teal fringe/helper: blue is slightly below green, but it is
        # adjacent to confirmed water and must not become a green rectangle.
        atlas.paste((45, 111, 109), (18, 8, 31, 16))
        strict = _exact_atlas_water_mask(atlas)
        broadened = _exact_atlas_water_mask(atlas, include_shore_teal=True)
        self.assertEqual(strict.getpixel((26, 12)), 0)
        self.assertEqual(broadened.getpixel((26, 12)), 255)
        self.assertEqual(broadened.getpixel((40, 12)), 0)

    def test_overlay_builds_only_bounded_real_3d_capture_sections(self):
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        page = (ROOT.parent / "scrap_editor_app" / "web" / "companion_overlay.html").read_text(encoding="utf-8")
        script = (ROOT.parent / "scrap_editor_app" / "web" / "companion_overlay.js").read_text(encoding="utf-8")
        capture = (ROOT.parent / "scrap_editor_app" / "web" / "companion_capture.js").read_text(encoding="utf-8")
        self.assertIn("fullscreen=True", overlay)
        self.assertIn("hidden=True", overlay)
        self.assertNotIn("enable_game_windows(False)", overlay)
        self.assertIn("focus_window_title(FULL_MAP_TITLE)", overlay)
        self.assertIn("restore_scrap_mechanic_focus()", overlay)
        self.assertIn("SwitchToThisWindow", overlay)
        self.assertIn("display-ready", overlay)
        self.assertNotIn('src="map3d.js"', page)
        self.assertNotIn('id="generator"', page)
        self.assertIn('id="mapImage"', page)
        self.assertNotIn("captureTopDown", script)
        self.assertNotIn("ScrapMap3D", script)
        self.assertNotIn("publish-companion-map", script)
        self.assertIn("active.pending", script)
        self.assertNotIn("allowGenerate", script)
        self.assertNotIn("refreshActiveSave(false, true)", script)
        self.assertNotIn('api("companion-progress"', script)
        self.assertIn("refreshActiveSave(false)", script)
        self.assertNotIn("ensure-companion-preview", overlay)
        self.assertNotIn("ensure-companion-detailed", overlay)
        self.assertIn("captureWorldTopDownChunked", capture)
        self.assertIn('maxSide: Number(job.maxSide) || 8192', capture)
        self.assertIn('variant: String(job.variant || "companion-hq-v2")', capture)
        self.assertIn('api("store-3d-map-tile"', capture)
        self.assertIn('api("finalize-3d-map-tiles"', capture)
        self.assertIn("onOverviewTile", capture)
        self.assertIn("onDetailTile", capture)
        self.assertIn('id="detailTiles"', page)
        self.assertIn("updateDetailTiles", script)
        self.assertIn("view.fit * 32", script)
        self.assertNotIn("Math.min(Math.max(view.fit, 1)", script)
        self.assertIn('id: activeJobId', capture)
        self.assertIn('if (job.finalizeOnly)', capture)
        self.assertIn('"map-3d-scene-packs"', overlay)
        self.assertIn("_queue_map_prepare", overlay)
        self.assertIn("capture_in_progress", overlay)
        self.assertIn("one immutable save snapshot", overlay)
        self.assertIn("EditorApi(companion_mode=True)", overlay)
        self.assertNotIn('"Scrap Mechanic Map Renderer",', overlay)
        self.assertIn('"mapQuality"', overlay)
        self.assertNotIn('"source": "save-snapshot"', overlay)
        self.assertIn("_in_game_world", overlay)
        self.assertIn("not running or not bridge._in_game_world", overlay)
        self.assertIn("enforce_native_visibility", overlay)
        self.assertIn("mapReady: true", script)
        self.assertNotIn('event.key.toLowerCase() === "m"', script)
        self.assertNotIn("_is_open_by_game", overlay)
        self.assertIn("_copy_shared_read", overlay)
        self.assertIn("FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE", overlay)
        self.assertNotIn('self.editor.load_save({"path": str(candidate)})', overlay)
        self.assertNotIn('"source": "save-snapshot"', overlay)
        self.assertIn("wait_for_game_frontend", overlay)
        self.assertIn("if not wait_for_game_process():", overlay)
        self.assertIn("signal is not None and signal[0] > sequence", overlay)
        self.assertIn("can_open_full_map", overlay)
        self.assertNotIn('self._window.evaluate_js("window.companionShow', overlay)
        self.assertIn("companion_context", overlay)
        self.assertNotIn("from scrap_editor_app.backend import EditorApi  # noqa", overlay)
        backend = (ROOT.parent / "scrap_editor_app" / "backend.py").read_text(encoding="utf-8")
        self.assertIn("scene_cache_path.read_bytes()", backend)
        map3d = (ROOT.parent / "scrap_editor_app" / "web" / "map3d.js").read_text(encoding="utf-8")
        self.assertIn("`${label} · ${message}`", map3d)
        self.assertIn("available.slice(start,start+8)", map3d)
        self.assertNotIn("_generatedLakeWater", map3d)
        self.assertIn("does not create a water plane", map3d)

    def test_transient_signal_read_is_not_a_false_keypress(self):
        with TemporaryDirectory() as temporary:
            missing = Path(temporary) / "temporarily-unavailable.json"
            self.assertIsNone(read_sequence(missing))
            self.assertIsNone(read_signal(missing))

    def test_signal_actions_are_explicit_and_survive_fast_key_sequences(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "signal.json"
            path.write_text('{"sequence":17,"action":"close"}', encoding="utf-8")
            self.assertEqual(read_signal(path), (17, "close"))

    def test_minimap_bounds_follow_game_client_at_common_aspects(self):
        settings = {"size": 280, "marginX": 6, "marginY": 6}
        cases = [
            ((100, 80, 740, 560), (106, 86, 280, 280)),   # 640x480
            ((120, 60, 840, 780), (126, 66, 280, 280)),   # 720x720 / 1:1
            ((40, 30, 1320, 750), (46, 36, 280, 280)),    # 1280x720
            ((0, 0, 1920, 1080), (6, 6, 280, 280)),       # 16:9
            ((0, 0, 3440, 1440), (6, 6, 280, 280)),       # 21:9
        ]
        for client, expected in cases:
            with self.subTest(client=client):
                self.assertEqual(fit_minimap_bounds(client, settings), expected)

    def test_full_map_refuses_to_open_before_final_image_is_ready(self):
        class FakeWindow:
            def __init__(self):
                self.shown = 0

            def show(self):
                self.shown += 1

        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            bridge._window = FakeWindow()
            self.assertFalse(bridge.show())
            self.assertEqual(bridge._window.shown, 0)
            self.assertFalse(bridge._visible)

    def test_full_map_opens_from_finished_warm_cache_after_companion_restart(self):
        """A ready minimap must imply that M can open the same cached map."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            signal = root / "runtime" / "signal.json"
            signal.parent.mkdir(parents=True, exist_ok=True)
            image = root / "complete-world.png"
            Image.new("RGB", (16, 16), (30, 90, 120)).save(image, "PNG")
            (signal.parent / "map_config.json").write_text(json.dumps({
                "schema": 3,
                "ready": True,
                "externalMap": image.resolve().as_uri(),
                "cellBounds": [-1, 1, -1, 1],
            }), encoding="utf-8")
            bridge = OverlayBridge(signal)
            bridge._window = object()
            bridge._in_game_world = True
            bridge._map_ready = False
            bridge.minimap_allowed = lambda: True
            try:
                with patch(
                    f"{OverlayBridge.__module__}.show_window_by_title",
                    return_value=True,
                ) as native_visibility, patch(
                    f"{OverlayBridge.__module__}.configure_full_map_window",
                    return_value=False,
                ), patch(
                    f"{OverlayBridge.__module__}.focus_window_title"
                ):
                    self.assertTrue(bridge.show())
                self.assertTrue(bridge._visible)
                self.assertTrue(bridge._map_ready)
                native_visibility.assert_any_call("Scrap Mechanic World Map", True)
            finally:
                bridge.shutdown()

    def test_direct_renderer_remains_ready_when_survival_world_starts(self):
        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            bridge._direct_renderer_enabled = True
            try:
                with patch.object(bridge, "_save_file_snapshot", return_value={}), patch.object(
                    bridge, "_activate_world_until_ready", return_value=None,
                ):
                    bridge.game_started()
                self.assertTrue(bridge._in_game_world)
                self.assertTrue(bridge._minimap_bitmap_ready)
            finally:
                bridge.shutdown()

    def test_cached_map_is_immediate_only_for_logged_active_save(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            signal = root / "runtime" / "signal.json"
            signal.parent.mkdir(parents=True)
            image = root / "cached.png"
            Image.new("RGB", (8, 8), (20, 80, 100)).save(image, "PNG")
            (signal.parent / "map_config.json").write_text(json.dumps({
                "schema": 3,
                "ready": True,
                "sourceSave": "active.db",
                "externalMap": image.resolve().as_uri(),
            }), encoding="utf-8")
            bridge = OverlayBridge(signal)
            active = {"path": str(root / "active.db")}
            try:
                with patch.object(bridge, "_survival_save_rows", return_value=[active]), patch.object(
                    bridge, "_active_save_from_log", return_value=active,
                ):
                    self.assertTrue(bridge._cached_map_matches_active_log())
                active["path"] = str(root / "different.db")
                with patch.object(bridge, "_survival_save_rows", return_value=[active]), patch.object(
                    bridge, "_active_save_from_log", return_value=active,
                ):
                    self.assertFalse(bridge._cached_map_matches_active_log())
            finally:
                bridge.shutdown()

    def test_warm_cache_requires_its_minimap_derivative_and_matching_seed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            signal = root / "runtime" / "signal.json"
            signal.parent.mkdir(parents=True)
            image = root / "world.png"
            minimap = root / "world-minimap.png"
            Image.new("RGB", (8, 8), (20, 80, 100)).save(image, "PNG")
            Image.new("RGB", (4, 4), (20, 80, 100)).save(minimap, "PNG")
            (signal.parent / "map_config.json").write_text(json.dumps({
                "schema": 3,
                "ready": True,
                "sourceSave": "active.db",
                "sourceSeed": 42,
                "externalMap": image.resolve().as_uri(),
                "minimapMap": minimap.resolve().as_uri(),
            }), encoding="utf-8")
            bridge = OverlayBridge(signal)
            try:
                self.assertTrue(bridge._ready_cache_matches(str(root / "active.db"), 42))
                self.assertFalse(bridge._ready_cache_matches(str(root / "active.db"), 43))
                minimap.unlink()
                self.assertFalse(bridge._ready_cache_matches(str(root / "active.db"), 42))
            finally:
                bridge.shutdown()

    def test_warm_cache_activation_skips_map_meta_and_republication(self):
        class FakeEditor:
            def overview(self, _payload):
                return {"current": {"fileName": "world.db", "game": {"seed": 42}}}

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            signal = root / "runtime" / "signal.json"
            signal.parent.mkdir(parents=True)
            source = root / "world.db"
            source.write_bytes(b"SQLite format 3\x00" + b"\0" * 4096)
            image = root / "world.png"
            Image.new("RGB", (8, 8), (20, 80, 100)).save(image, "PNG")
            (signal.parent / "map_config.json").write_text(json.dumps({
                "schema": 3,
                "ready": True,
                "sourceSave": source.name,
                "sourceSeed": 42,
                "externalMap": image.resolve().as_uri(),
                "minimapMap": image.resolve().as_uri(),
            }), encoding="utf-8")
            row = {
                "path": str(source), "name": "world", "mode": "survival",
                "modified": source.stat().st_mtime, "size": source.stat().st_size,
            }
            bridge = OverlayBridge(signal)
            bridge._editor = FakeEditor()
            bridge._game_started_wall = 1.0
            bridge._loaded_path = str(source.resolve())
            bridge._world_needs_bind = False
            bridge._loaded_fingerprint = bridge._source_fingerprint(source)
            bridge._map_busy = True
            try:
                with (
                    patch.object(bridge, "_survival_save_rows", return_value=[row]),
                    patch.object(bridge, "_active_save_from_log", return_value=row),
                    patch.object(bridge, "_queue_map_prepare") as queue,
                ):
                    result = bridge.activate_current_save()
                self.assertFalse(result["pending"])
                queue.assert_not_called()
                self.assertTrue(bridge._map_ready)
                self.assertFalse(bridge._map_busy)
                self.assertEqual(bridge._map_progress, 1.0)
            finally:
                bridge.shutdown()

    def test_window_monitor_never_calls_thread_affine_pywebview_show_or_hide(self):
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        self.assertNotIn("self._window.show()", overlay)
        self.assertNotIn("self._window.hide()", overlay)
        self.assertNotIn("self._minimap_window.show()", overlay)
        self.assertNotIn("self._minimap_window.hide()", overlay)
        self.assertNotIn(".evaluate_js(", overlay)
        self.assertIn("show_window_by_title(FULL_MAP_TITLE, True)", overlay)

    def test_capture_failure_stays_visible_and_retryable(self):
        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            try:
                bridge._in_game_world = True
                bridge._minimap_bitmap_ready = True
                bridge._capture_job = {
                    "id": "job-1",
                    "attempt": 1,
                    "maxSide": 8192,
                    "variant": "companion-hq",
                }
                bridge._capture_claimed = True
                result = bridge.capture_failed({"id": "job-1", "error": "publish failed"})
                self.assertTrue(result["accepted"])
                self.assertTrue(result["retrying"])
                self.assertIsNotNone(bridge._capture_job)
                self.assertFalse(bridge._capture_claimed)
                self.assertTrue(bridge._map_busy)
                self.assertFalse(bridge._map_ready)
                self.assertTrue(bridge.minimap_allowed())
                self.assertTrue(bridge.capture_job()["available"])
            finally:
                bridge.shutdown()

    def test_late_capture_progress_cannot_regress_a_published_map(self):
        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            try:
                bridge._map_ready = True
                bridge._map_busy = False
                bridge._map_progress = 1.0
                state = bridge.update_companion_progress({
                    "id": "retired-job",
                    "message": "Saving…",
                    "progress": 0.998,
                    "busy": True,
                    "mapReady": False,
                })
                self.assertTrue(state["mapReady"])
                self.assertFalse(state["busy"])
                self.assertEqual(state["progress"], 1.0)
            finally:
                bridge.shutdown()

    def test_full_map_close_hands_directly_to_warm_minimap(self):
        class FakeWindow:
            def __init__(self):
                self.shown = 0
                self.hidden = 0
                self.scripts = []

            def show(self):
                self.shown += 1

            def hide(self):
                self.hidden += 1

            def evaluate_js(self, script):
                self.scripts.append(script)

        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            full = FakeWindow()
            mini = FakeWindow()
            bridge._window = full
            bridge._minimap_window = mini
            bridge._visible = True
            bridge._in_game_world = True
            bridge._map_ready = True
            bridge.minimap_allowed = lambda: True
            try:
                with patch(f"{OverlayBridge.__module__}.configure_minimap_window"), patch(
                    f"{OverlayBridge.__module__}.show_window_by_title"
                ) as native_visibility, patch(
                    f"{OverlayBridge.__module__}.restore_scrap_mechanic_focus"
                ):
                    bridge.hide()
                self.assertFalse(bridge._visible)
                self.assertTrue(bridge._minimap_visible)
                self.assertIn(call("Scrap Mechanic World Map", False), native_visibility.call_args_list)
                self.assertIn(call("Scrap Mechanic Minimap", True), native_visibility.call_args_list)
                self.assertEqual(full.hidden, 0)
                self.assertEqual(mini.shown, 0)
                self.assertEqual(mini.hidden, 0)
                self.assertGreater(bridge._minimap_handoff_until, 0)
            finally:
                bridge.shutdown()

    def test_warm_minimap_close_does_not_reconfigure_or_redraw(self):
        class FakeWindow:
            def __init__(self):
                self.hidden = 0
                self.scripts = []

            def hide(self):
                self.hidden += 1

            def evaluate_js(self, script):
                self.scripts.append(script)

        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            bridge._window = FakeWindow()
            bridge._minimap_window = FakeWindow()
            bridge._visible = True
            bridge._minimap_visible = True
            bridge._in_game_world = True
            bridge.minimap_allowed = lambda: True
            try:
                with patch(f"{OverlayBridge.__module__}.configure_minimap_window") as configure, patch(
                    f"{OverlayBridge.__module__}.restore_scrap_mechanic_focus"
                ):
                    bridge.hide()
                configure.assert_not_called()
                self.assertEqual(bridge._minimap_window.scripts, [])
            finally:
                bridge.shutdown()

    def test_full_map_close_focuses_game_before_final_minimap_raise(self):
        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            bridge._window = object()
            bridge._minimap_window = object()
            bridge._visible = True
            bridge._minimap_visible = True
            bridge._in_game_world = True
            bridge._map_ready = True
            bridge.minimap_allowed = lambda: True
            events = []
            try:
                with patch(
                    f"{OverlayBridge.__module__}.show_window_by_title"
                ), patch(
                    f"{OverlayBridge.__module__}.restore_scrap_mechanic_focus",
                    side_effect=lambda: events.append("focus"),
                ), patch(
                    f"{OverlayBridge.__module__}.set_window_topmost",
                    side_effect=lambda *_args: events.append("raise-minimap"),
                ):
                    bridge.hide()
                self.assertEqual(events, ["focus", "raise-minimap"])
            finally:
                bridge.shutdown()

    def test_focus_restore_has_no_delayed_retry_thread(self):
        module_name = OverlayBridge.__module__
        with patch(f"{module_name}.focus_scrap_mechanic") as focus, patch(
            f"{module_name}.threading.Thread"
        ) as thread:
            restore_scrap_mechanic_focus()
        focus.assert_called_once_with(retries=3)
        thread.assert_not_called()

    def test_game_focus_restore_never_changes_topmost_z_order(self):
        source = inspect.getsource(focus_scrap_mechanic)
        self.assertNotIn("SetWindowPos", source)
        self.assertNotIn("HWND_TOPMOST", source)
        self.assertNotIn("HWND_NOTOPMOST", source)

    def test_local_map_paths_accept_uri_and_windows_path(self):
        uri = OverlayBridge._path_from_uri("file:///C:/Temp/map.png")
        direct = OverlayBridge._path_from_uri(r"C:\Temp\map.png")
        self.assertEqual(uri, direct)

    def test_frontend_wait_uses_the_current_game_process_log(self):
        with TemporaryDirectory() as temporary:
            game_root = Path(temporary)
            logs = game_root / "Logs"
            logs.mkdir()
            (logs / "game-test.log").write_text(
                "12:00:00 [Main:9999] [Default] Start state: Game\n",
                encoding="utf-8",
            )
            module = wait_for_game_frontend.__module__
            with patch(f"{module}.scrap_mechanic_process_ids", return_value={4321}):
                self.assertTrue(wait_for_game_frontend(game_root, timeout_seconds=0.25))

    def test_renderer_warmup_waits_for_process_not_world_frontend(self):
        module = wait_for_game_process.__module__
        with patch(
            f"{module}.scrap_mechanic_process_ids",
            side_effect=[set(), {4321}],
        ):
            started = wait_for_game_process(timeout_seconds=1.0)
        self.assertTrue(started)

    def test_frontend_wait_does_not_reuse_an_old_game_state(self):
        with TemporaryDirectory() as temporary:
            game_root = Path(temporary)
            logs = game_root / "Logs"
            logs.mkdir()
            (logs / "game-test.log").write_text(
                "12:00:00 [Main:9999] [Default] Start state: Game\n"
                "12:05:00 [Main:9999] [Default] Start state: Menu\n",
                encoding="utf-8",
            )
            module = wait_for_game_frontend.__module__
            with patch(f"{module}.scrap_mechanic_process_ids", return_value={4321}):
                self.assertFalse(wait_for_game_frontend(game_root, timeout_seconds=0.05))
                self.assertEqual(scrap_mechanic_frontend_state(game_root), "menu")

    def test_frontend_context_does_not_reuse_survival_for_creative(self):
        with TemporaryDirectory() as temporary:
            game_root = Path(temporary)
            logs = game_root / "Logs"
            logs.mkdir()
            (logs / "game-test.log").write_text(
                "Start state: Load\n"
                "Game mode: Survival\n"
                "Start state: Game\n"
                "Start state: Load\n"
                "Game mode: FlatTerrain\n"
                "Start state: Game\n",
                encoding="utf-8",
            )
            self.assertEqual(scrap_mechanic_frontend_context(game_root), {
                "state": "game", "mode": "flatterrain",
            })

    def test_minimap_refreshes_static_context_after_save_activation(self):
        web = ROOT.parent / "scrap_editor_app" / "web"
        script = (web / "companion_minimap.js").read_text(encoding="utf-8")
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        self.assertIn('"contextRevision": self._context_revision', overlay)
        self.assertIn("state.contextRevision", script)
        self.assertIn("refreshFull()", script)

    def test_game_stop_flushes_dirty_exploration(self):
        with TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ):
            save = Path(temporary) / "world.db"
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            try:
                bridge._in_game_world = True
                bridge._load_exploration(save, 9)
                bridge._update_exploration({"x": 64.0, "y": 128.0})
                bridge.game_stopped()
                self.assertTrue(bridge._exploration_path().is_file())
            finally:
                bridge.shutdown()

    def test_active_save_is_identified_from_game_log(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = root / "game"
            save = root / "User_1" / "Save" / "Survival" / "world.db"
            save.parent.mkdir(parents=True)
            save.write_bytes(b"SQLite format 3\x00" + b"\0" * 4096)
            logs = game_root / "Logs"
            logs.mkdir(parents=True)
            (logs / "game-current.log").write_text(
                f"Opening save file: '{save.as_posix()}'\n",
                encoding="utf-8",
            )
            row = {"path": str(save), "name": "world", "modified": save.stat().st_mtime}
            bridge = OverlayBridge(root / "signal.json", game_root)
            self.assertIs(bridge._active_save_from_log([row]), row)

    def test_activate_loads_private_snapshot_never_live_database(self):
        class FakeEditor:
            def __init__(self) -> None:
                self.loaded_paths: list[str] = []

            def load_save(self, payload):
                self.loaded_paths.append(payload["path"])
                return {"current": {"fileName": "world.db", "game": {"seed": 42}}}

            def overview(self, _payload):
                return {"current": {"fileName": "world.db", "game": {"seed": 42}}}

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "live" / "world.db"
            private = root / "private" / "world.db"
            source.parent.mkdir()
            private.parent.mkdir()
            source.write_bytes(b"SQLite format 3\x00" + b"\0" * 4096)
            private.write_bytes(source.read_bytes())
            row = {
                "path": str(source), "name": "world", "mode": "survival",
                "modified": source.stat().st_mtime, "size": source.stat().st_size,
            }
            editor = FakeEditor()
            bridge = OverlayBridge(root / "signal.json")
            bridge._editor = editor
            bridge._game_started_wall = 1.0
            with (
                patch.object(bridge, "_survival_save_rows", return_value=[row]),
                patch.object(bridge, "_active_save_from_log", return_value=row),
                patch.object(bridge, "_create_save_snapshot", return_value=private),
                patch.object(bridge, "_refresh_live_context", side_effect=lambda: setattr(bridge, "_live_expected", (1, 2, 3))),
                patch.object(bridge, "_prune_snapshots"),
                patch.object(bridge, "_queue_map_prepare"),
            ):
                result = bridge.activate_current_save()
            self.assertFalse(result["pending"])
            self.assertEqual(editor.loaded_paths, [str(private)])
            self.assertNotEqual(editor.loaded_paths[0], str(source))
            self.assertEqual(bridge._loaded_path, str(source.resolve()))
            self.assertEqual(bridge._snapshot_path, str(private))

    def test_same_world_autosave_does_not_reload_snapshot(self):
        class FakeEditor:
            def overview(self, _payload):
                return {"current": {"fileName": "world.db", "game": {"seed": 42}}}

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "world.db"
            source.write_bytes(b"SQLite format 3\x00" + b"\0" * 4096)
            row = {
                "path": str(source), "name": "world", "mode": "survival",
                "modified": source.stat().st_mtime, "size": source.stat().st_size,
            }
            bridge = OverlayBridge(root / "signal.json")
            bridge._editor = FakeEditor()
            bridge._game_started_wall = 1.0
            bridge._loaded_path = str(source.resolve())
            bridge._world_needs_bind = False
            bridge._loaded_fingerprint = (("", 1, source.stat().st_size),)
            with (
                patch.object(bridge, "_survival_save_rows", return_value=[row]),
                patch.object(bridge, "_active_save_from_log", return_value=row),
                patch.object(bridge, "_load_snapshot") as reload_snapshot,
                patch.object(bridge, "_queue_map_prepare"),
            ):
                result = bridge.activate_current_save()
            self.assertFalse(result["pending"])
            reload_snapshot.assert_not_called()
            self.assertEqual(bridge._loaded_fingerprint, bridge._source_fingerprint(source))

    def test_external_snapshot_is_sqlite_valid_and_separate(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "live.db"
            connection = sqlite3.connect(source)
            try:
                connection.execute("CREATE TABLE sample(value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('safe')")
                connection.commit()
            finally:
                connection.close()
            bridge = OverlayBridge(root / "MapCache" / "signal.json")
            with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}, clear=False):
                snapshot = bridge._create_save_snapshot(source)
            self.assertNotEqual(snapshot, source.resolve())
            self.assertTrue(snapshot.is_file())
            copied = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
            try:
                self.assertEqual(copied.execute("SELECT value FROM sample").fetchone()[0], "safe")
                self.assertEqual(copied.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                copied.close()

    def test_hot_journal_snapshot_does_not_block_the_writer(self):
        """A live write is recovered only in the copy; the source can commit."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "live.db"
            writer = sqlite3.connect(source)
            writer.execute("PRAGMA journal_mode=DELETE")
            writer.execute("CREATE TABLE sample(value TEXT)")
            writer.execute("INSERT INTO sample VALUES ('committed')")
            writer.commit()
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE sample SET value='pending'")
            self.assertTrue(Path(str(source) + "-journal").is_file())

            bridge = OverlayBridge(root / "MapCache" / "signal.json")
            with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}, clear=False):
                snapshot = bridge._create_save_snapshot(source)

            # If the companion had participated in SQLite locking, this is the
            # operation that could fail in the game. It must remain writable.
            writer.commit()
            writer.close()

            copied = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
            try:
                self.assertEqual(copied.execute("SELECT value FROM sample").fetchone()[0], "committed")
                self.assertEqual(copied.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                copied.close()
            live = sqlite3.connect(source)
            try:
                self.assertEqual(live.execute("SELECT value FROM sample").fetchone()[0], "pending")
            finally:
                live.close()

    def test_companion_rejects_editor_mutation_methods(self):
        bridge = OverlayBridge(Path("signal.json"))
        result = bridge.request("load-save", {"path": "live.db"})
        self.assertFalse(result["ok"])
        self.assertIn("read-only companion", result["error"])

    def test_new_world_pending_config_drops_every_stale_map_field(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            bridge._write_config({
                "schema": 3,
                "sourceSave": "old.db",
                "sourceSeed": 1,
                "sourceMap": "old.png",
                "externalMap": "file:///old.png",
                "minimapMap": "file:///old-mini.png",
                "mapWidth": 8192,
                "cellBounds": [-20, 20, -20, 20],
                "detailTiles": {"root": "old"},
                "ready": True,
            })
            try:
                bridge._mark_map_pending(str(root / "new.db"), 42)
                config = bridge._read_config()
                self.assertEqual(config, {
                    "schema": 3,
                    "sourceSave": "new.db",
                    "sourceSeed": 42,
                    "ready": False,
                })
            finally:
                bridge.shutdown()

    def test_same_pending_world_repairs_stale_fields_from_older_runtime(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            bridge._write_config({
                "schema": 3,
                "sourceSave": "new.db",
                "sourceSeed": 42,
                "externalMap": "",
                "minimapMap": "file:///wrong-world.png",
                "cellBounds": [-64, 63, -48, 47],
                "detailTiles": {"root": "wrong"},
                "ready": False,
            })
            try:
                bridge._mark_map_pending(str(root / "new.db"), 42)
                self.assertEqual(bridge._read_config(), {
                    "schema": 3,
                    "sourceSave": "new.db",
                    "sourceSeed": 42,
                    "ready": False,
                })
            finally:
                bridge.shutdown()

    def test_shutdown_owns_and_destroys_hidden_minimap_renderer(self):
        class FakeWindow:
            def __init__(self):
                self.destroyed = 0

            def destroy(self):
                self.destroyed += 1

        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            full = FakeWindow()
            native = FakeWindow()
            renderer = FakeWindow()
            capture = FakeWindow()
            bridge._window = full
            bridge._minimap_window = native
            bridge._minimap_renderer_window = renderer
            bridge._capture_window = capture
            with patch(f"{OverlayBridge.__module__}.show_window_by_title"):
                bridge.shutdown()
                bridge.shutdown()
            self.assertEqual(native.destroyed, 1)
            self.assertEqual(renderer.destroyed, 1)
            self.assertEqual(capture.destroyed, 1)
            self.assertEqual(full.destroyed, 1)
            state = json.loads(bridge.overlay_state_path.read_text(encoding="utf-8"))
            self.assertFalse(state["visible"])
            self.assertFalse(state["minimapVisible"])
            self.assertFalse(state["inGameWorld"])

    def test_minimap_boot_and_progress_do_not_depend_on_focus(self):
        overlay = (ROOT / "companion_overlay.py").read_text(encoding="utf-8")
        script = (ROOT.parent / "scrap_editor_app" / "web" / "companion_minimap.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("bridge._minimap_renderer_window = minimap", overlay)
        self.assertIn("if not live_push.bootstrap():", overlay)
        self.assertIn('event.data?.type === "minimap-bootstrap"', script)
        self.assertIn("if (!booted) bootWhenBridgeReady(performance.now());", script)
        self.assertIn("drawNativeBuildStatus(nativeContext, settings);", script)
        self.assertIn("message.split(/\\s*[·|]\\s*/)", script)
        self.assertNotIn('label += "..."', script)
        self.assertIn("settings.showKeyHint !== false && nativeMapReady", script)
        self.assertNotIn(
            "!baseState.overlay?.mapReady || !baseState.config?.ready || !imageReady",
            script,
        )


if __name__ == "__main__":
    unittest.main()
