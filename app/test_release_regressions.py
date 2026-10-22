from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from companion_mod.companion_overlay import OverlayBridge
from scrap_editor_app.backend import EditorApi, place_name
from scrap_mechanic_save_editor import ContainerBlob, InventorySlot


class ReleaseRegressionTests(unittest.TestCase):
    def test_world_exit_hides_windows_before_flushing_history(self):
        with TemporaryDirectory() as temporary:
            bridge = OverlayBridge(Path(temporary) / "runtime" / "signal.json")
            try:
                for action in (bridge.game_left_world, bridge.game_stopped):
                    with (
                        self.subTest(action=action.__name__),
                        patch.object(bridge, "hide") as hide,
                        patch.object(bridge, "set_minimap_visible") as minimap,
                    ):
                        def flush():
                            hide.assert_called_once_with(restore_game_focus=False, force=True)
                            minimap.assert_called_once_with(False, force=True)
                            return True
                        with patch.object(bridge, "_save_exploration", side_effect=flush):
                            action()
            finally:
                bridge.shutdown()

    def test_old_map_prepare_cannot_publish_into_a_new_session(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "map.png"
            Image.new("RGB", (64, 64)).save(image)
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            try:
                bridge._write_config({"sourceSave": "new.db", "ready": False})
                bridge._editor = Mock()
                def map_meta(*args):
                    bridge._activation_serial += 1
                    return {"image": image.as_uri(), "bounds": [-1, 0, -1, 0], "needsCapture": True}
                bridge._editor.dispatch.side_effect = map_meta
                bridge._queue_map_prepare(str(root / "old.db"), {"game": {"seed": 1}})
                self.assertEqual(bridge._read_config(), {"sourceSave": "new.db", "ready": False})
                self.assertFalse(bridge._map_ready)
                self.assertEqual(bridge._map_job_identity, "")
            finally:
                bridge.shutdown()

    def test_map_publication_rechecks_session_after_image_preparation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "map.png"
            Image.new("RGB", (64, 64)).save(image)
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            original_open = Image.open
            def open_in_new_session(*args, **kwargs):
                bridge._activation_serial += 1
                return original_open(*args, **kwargs)
            try:
                bridge._write_config({"sourceSave": "new.db", "ready": False})
                with patch.object(Image, "open", side_effect=open_in_new_session):
                    result = bridge.publish_companion_map({"image": image.as_uri(), "bounds": [-1, 0, -1, 0], "activationSerial": bridge._activation_serial})
                self.assertTrue(result["cancelled"])
                self.assertEqual(bridge._read_config(), {"sourceSave": "new.db", "ready": False})
            finally:
                bridge.shutdown()

    def test_failed_exploration_flush_does_not_replace_unsaved_world_history(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            try:
                bridge._load_exploration(root / "first.db", 1)
                identity = bridge._exploration_identity
                bridge._explored_cells = {"12,15"}
                bridge._exploration_dirty = True
                with patch.object(Path, "replace", side_effect=PermissionError("Simulated disk write failure")):
                    with self.assertRaisesRegex(OSError, "could not be saved"):
                        bridge._load_exploration(root / "second.db", 2)
                self.assertEqual(bridge._exploration_identity, identity)
                self.assertEqual(bridge._explored_cells, {"12,15"})
                self.assertTrue(bridge._exploration_dirty)
            finally:
                bridge.shutdown()

    def test_session_change_during_save_detection_rejects_old_activation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "world.db"
            source.touch()
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            bridge._game_started_wall = 1
            row = {"path": str(source), "modified": 1}

            def change_session():
                bridge._activation_serial += 1
                return [row]

            try:
                with (
                    patch.object(bridge, "_survival_save_rows", side_effect=change_session),
                    patch.object(bridge, "_active_save_from_log", return_value=row),
                    patch.object(bridge, "_load_snapshot") as snapshot,
                ):
                    self.assertTrue(bridge.activate_current_save()["pending"])
                snapshot.assert_not_called()
            finally:
                bridge.shutdown()

    def test_exploration_reload_waits_for_inflight_flush(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            bridge._live_sampler_stop.set()
            bridge._exploration_save_event.set()
            bridge._live_sampler.join(1)
            bridge._exploration_saver.join(1)
            entered, release, loaded = threading.Event(), threading.Event(), threading.Event()
            errors = []
            source = root / "world.db"
            bridge._load_exploration(source, 42)
            bridge._explored_cells = {"0,0"}
            bridge._exploration_dirty = True
            bridge._save_exploration()
            destination = bridge._exploration_path()
            bridge._explored_cells.add("1,1")
            bridge._exploration_dirty = True
            replace = Path.replace

            def paused_replace(path, target):
                if Path(target) == destination:
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("Test did not release the exploration writer")
                return replace(path, target)

            def reload():
                try:
                    bridge._load_exploration(source, 42)
                except Exception as error:
                    errors.append(error)
                finally:
                    loaded.set()

            writer = threading.Thread(target=bridge._save_exploration)
            reader = threading.Thread(target=reload)
            try:
                with patch.object(Path, "replace", paused_replace):
                    writer.start()
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(bridge._lock.acquire(timeout=.1), "Disk IO must not hold the live sampler lock")
                    bridge._lock.release()
                    reader.start()
                    reloaded_before_flush = loaded.wait(.1)
                    release.set()
                    writer.join(2)
                    reader.join(2)
                self.assertFalse(reloaded_before_flush, "Reload read stale fog while the latest history was still being written")
                self.assertFalse(writer.is_alive())
                self.assertFalse(reader.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(bridge._explored_cells, {"0,0", "1,1"})
                self.assertEqual(set(json.loads(destination.read_text())["cells"]), {"0,0", "1,1"})
            finally:
                release.set()
                if writer.ident is not None:
                    writer.join(2)
                if reader.ident is not None:
                    reader.join(2)
                bridge.shutdown()

    def test_drag_swaps_complete_records_and_keeps_slot_numbers(self):
        slots = [InventorySlot(1, "tool-instance", b"\x01\x02\x03\x04", 1),
                 InventorySlot(2, "resource", b"\x11\x22\x33\x44", 27)]
        container = ContainerBlob(9, b"header", slots, b"footer", b"")
        api = object.__new__(EditorApi)
        api.save = SimpleNamespace(containers={9: container}, inventory=container)
        api._slot_data = lambda slot: {"number": slot.number, "uuid": slot.uuid}
        api.move_slot({"containerId": 9, "source": 1, "target": 2})
        self.assertEqual([(s.number, s.uuid, s.metadata, s.quantity) for s in slots], [
            (1, "resource", b"\x11\x22\x33\x44", 27),
            (2, "tool-instance", b"\x01\x02\x03\x04", 1),
        ])
        self.assertTrue(container.dirty)
        with self.assertRaises(ValueError):
            api.move_slot({"containerId": 9, "source": 0, "target": 1})
        self.assertEqual(slots[1].metadata, b"\x01\x02\x03\x04")

    def test_disk_change_blocks_overwrite_before_archive_or_write(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "world.db"
            path.write_bytes(b"changed")
            api = object.__new__(EditorApi)
            api.save = SimpleNamespace(path=path, save_copy=Mock())
            api.backups = Mock()
            api._loaded_source_stamp = (1, 1)
            with patch("scrap_editor_app.game_status.game_is_running", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                    api.overwrite({"confirmation": "OVERWRITE"})
            api.save.save_copy.assert_not_called()
            api.backups.archive.assert_not_called()

    def test_running_game_blocks_all_external_save_writes(self):
        api = object.__new__(EditorApi)
        with patch("scrap_editor_app.game_status.game_is_running", return_value=True):
            for action in (api.overwrite, api.restore_backup, api.save_copy, api.save_clothing, api.save_character,
                           api.create_backup, api.rename_blueprint, api.duplicate_blueprint, api.import_blueprint):
                with self.subTest(action=action.__name__), self.assertRaisesRegex(RuntimeError, "Close Scrap Mechanic"):
                    action({})

    def test_new_world_publishes_terrain_without_starting_a_3d_job(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "terrain.png"
            Image.new("RGB", (128, 96), (30, 90, 50)).save(image)
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            bridge._editor = Mock()
            bridge._editor.dispatch.return_value = {
                "image": image.as_uri(), "bounds": [-4, 3, -3, 2],
                "needsCapture": True, "capture": {"width": 8192, "height": 6144},
            }
            try:
                bridge._loaded_path = str(root / "fresh.db")
                bridge._queue_map_prepare(bridge._loaded_path, {"game": {"seed": 7}})
                self.assertTrue(bridge._map_ready)
                self.assertFalse(bridge._map_busy)
                self.assertFalse(bridge.capture_job()["available"])
                self.assertEqual(bridge._read_config()["mapQuality"], "preview")
                self.assertTrue(bridge._ready_cache_matches(bridge._loaded_path, 7))
                self.assertFalse(bridge._ready_cache_matches(bridge._loaded_path, 8))
            finally:
                bridge.shutdown()

    def test_editor_reuses_companion_capture_with_its_actual_dimensions(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            cached = root / "companion.png"
            cached.touch()
            api = object.__new__(EditorApi)
            api.save = SimpleNamespace(terrain_grid=SimpleNamespace(visible_bounds=(-4, 3, -3, 2)), world_maps={1: None})
            api._detail_tile_meta = lambda _generated: None
            api._generated_3d_map_info = lambda **opts: {
                "path": cached if opts.get("variant") else root / "missing.png",
                "width": 1024, "height": 768, "cellPixels": 128, "revision": 11,
            }
            result = api.map_meta({})
            self.assertFalse(result["needsCapture"])
            self.assertEqual(result["width"], 1024)
            self.assertEqual(result["image"], cached.resolve().as_uri())

    def test_same_save_reentry_rebinds_snapshot_and_player_context(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "world.db"
            source.write_bytes(b"world")
            row = {"path": str(source), "modified": 1}
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            try:
                bridge._loaded_path = str(source.resolve())
                bridge._world_needs_bind = False
                bridge._live_expected = (1, 2, 3)
                bridge.game_left_world()
                bridge._game_started_wall = 1
                bridge._in_game_world = True
                current = {"game": {"seed": 42}}
                with (
                    patch.object(bridge, "_survival_save_rows", return_value=[row]),
                    patch.object(bridge, "_active_save_from_log", return_value=row),
                    patch.object(bridge, "_load_snapshot", return_value={"current": current}) as snapshot,
                    patch.object(bridge, "_refresh_live_context", side_effect=lambda: setattr(bridge, "_live_expected", (1, 2, 3))) as context,
                    patch.object(bridge, "_load_exploration") as fog,
                    patch.object(bridge, "_restore_ready_cache", return_value=True),
                ):
                    self.assertFalse(bridge.activate_current_save()["pending"])
                snapshot.assert_called_once_with(source.resolve())
                context.assert_called_once()
                fog.assert_called_once_with(source.resolve(), 42)
                self.assertFalse(bridge._world_needs_bind)
            finally:
                bridge.shutdown()

    def test_preview_cache_promotes_editor_hd_on_next_bind(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "map.png"
            Image.new("RGB", (128, 96), (30, 90, 50)).save(image)
            bridge = OverlayBridge(root / "runtime" / "signal.json")
            try:
                bridge._loaded_path = str(root / "world.db")
                bridge.publish_companion_map({"image": image.as_uri(), "bounds": [-4, 3, -3, 2],
                                              "sourceSave": "world.db", "sourceSeed": 7, "quality": "preview"})
                bridge._editor = Mock()
                bridge._editor.dispatch.return_value = {"image": image.as_uri(), "bounds": [-4, 3, -3, 2], "needsCapture": False}
                self.assertTrue(bridge._restore_ready_cache(bridge._loaded_path, 7))
                self.assertEqual(bridge._read_config()["mapQuality"], "3d")
                bridge._restore_ready_cache(bridge._loaded_path, 7)
                bridge._editor.dispatch.assert_called_once()
            finally:
                bridge.shutdown()

    def test_map_names_drop_asset_dimensions_without_losing_floor_count(self):
        self.assertEqual(place_name("Warehouse Exterior 4Floors 256 01 New"), "Warehouse · 4 floors")
        self.assertEqual(place_name("Mechanic Station 128 01"), "Mechanic Station")
        self.assertEqual(place_name("Hay Bale Labyrinth 128 01 New"), "Hay Bale Labyrinth")


if __name__ == "__main__":
    unittest.main()
