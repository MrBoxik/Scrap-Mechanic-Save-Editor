"""Small non-destructive tests for the save editor core."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scrap_mechanic_save_editor import ItemCatalog, MapRenderer, SaveData, find_game_root
from scrap_editor_app.game_unlocks import (
    CustomizationUnlockProfile,
    load_customization_catalog,
    load_log_catalog,
    load_quest_catalog,
    load_recipe_catalog,
)
from terrain_map import TerrainRenderer, fnv1a_uuid, load_terrain_tiles


def find_test_save() -> Path:
    explicit = os.getenv("SCRAP_EDITOR_TEST_SAVE")
    if explicit:
        return Path(explicit)
    appdata = os.getenv("APPDATA")
    if appdata:
        root = Path(appdata) / "Axolot Games" / "Scrap Mechanic" / "User"
        candidates = sorted(root.glob("User_*/Save/Survival/*.db"))
        if candidates:
            return candidates[0]
    return Path("__missing_test_save__.db")


SAVE = find_test_save()


class EditorTests(unittest.TestCase):
    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_load_expected_fields(self) -> None:
        save = SaveData(SAVE)
        self.assertEqual(save.integrity, "ok")
        self.assertIsNotNone(save.inventory)
        self.assertEqual(len(save.inventory.slots), 40)  # type: ignore[union-attr]
        self.assertIsNotNone(save.settings)
        self.assertIn("Difficulty", save.settings.values)  # type: ignore[union-attr]
        self.assertIn(1, save.world_maps)
        self.assertGreater(len(save.containers), 100)
        self.assertIsNotNone(save.terrain_grid)
        self.assertEqual(save.terrain_grid.visible_bounds, (-64, 63, -48, 47))  # type: ignore[union-attr]
        self.assertEqual(len(save.terrain_grid.cells), 128 * 96)  # type: ignore[union-attr]
        self.assertEqual(save.terrain_grid.seed, save.game["seed"])  # type: ignore[union-attr]
        self.assertGreaterEqual(len(save.players), 1)
        self.assertEqual(save.players[0].world_id, 1)
        self.assertIn(save.players[0].inventory_container_id, save.containers)
        self.assertGreaterEqual(len(save.player_states), 1)
        self.assertIsNotNone(save.raid_state)
        self.assertIsNotNone(save.progression_state)
        self.assertIsNotNone(save.quest_state)
        self.assertIsNotNone(save.recipe_state)
        self.assertIsNotNone(save.time_state)
        self.assertIsNotNone(save.log_state)

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_unchanged_roundtrip_preserves_editable_blobs(self) -> None:
        save = SaveData(SAVE)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "roundtrip.db"
            save.save_copy(output)
            source = sqlite3.connect(f"file:{SAVE.as_posix()}?mode=ro", uri=True)
            copied = sqlite3.connect(f"file:{output.as_posix()}?mode=ro", uri=True)
            try:
                self.assertEqual(source.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                for table, key in (("Container", "id"), ("GenericData", "rowid")):
                    left = source.execute(f"SELECT {key}, data FROM {table} ORDER BY {key}").fetchall()
                    right = copied.execute(f"SELECT {key}, data FROM {table} ORDER BY {key}").fetchall()
                    self.assertEqual(left, right)
            finally:
                source.close()
                copied.close()

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_edit_only_changes_target_container(self) -> None:
        save = SaveData(SAVE)
        assert save.inventory is not None
        slot = save.inventory.slots[5]
        save.inventory.set_slot(6, "3440440b-d362-4473-aa03-b7c41e1fe7ad", 123)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "edited.db"
            save.save_copy(output)
            source = sqlite3.connect(SAVE)
            copied = sqlite3.connect(output)
            try:
                source_rows = dict(source.execute("SELECT id,data FROM Container"))
                copied_rows = dict(copied.execute("SELECT id,data FROM Container"))
                changed = [key for key in source_rows if source_rows[key] != copied_rows[key]]
                self.assertEqual(changed, [save.inventory.container_id])
                self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                source.close()
                copied.close()

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_player_and_progression_roundtrip(self) -> None:
        save = SaveData(SAVE)
        assert save.player_states and save.progression_state
        player_row = save.player_states[0].rowid
        progression_row = save.progression_state.rowid
        save.set_player_status(0, 88.5, 125, 77, 150, True, True)
        save.set_progression(2, 42, True, True)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "state-edited.db"
            save.save_copy(output)
            loaded = SaveData(output)
            stats = loaded.player_states[0].value["stats"]
            self.assertEqual((stats["hp"], stats["maxhp"]), (88.5, 125.0))
            self.assertEqual((stats["breath"], stats["maxbreath"]), (77.0, 150.0))
            self.assertTrue(loaded.player_states[0].value["hasRevivalItem"])
            self.assertEqual(loaded.progression_state.value["seedTier"], 2)  # type: ignore[union-attr]
            self.assertEqual(loaded.progression_state.value["vaultTotal"], 42)  # type: ignore[union-attr]
            source = sqlite3.connect(SAVE)
            copied = sqlite3.connect(output)
            try:
                left = dict(source.execute("SELECT rowid,data FROM ScriptData"))
                right = dict(copied.execute("SELECT rowid,data FROM ScriptData"))
                changed = {rowid for rowid in left if left[rowid] != right[rowid]}
                self.assertEqual(changed, {player_row, progression_row})
                self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                source.close()
                copied.close()

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_quest_recipe_log_and_time_roundtrip(self) -> None:
        root = find_game_root()
        if not root:
            self.skipTest("local game installation is unavailable")
        catalog = ItemCatalog()
        catalog.load(root)
        quests = load_quest_catalog(root, catalog)
        recipes = load_recipe_catalog(root, catalog)
        logs = load_log_catalog(root)
        save = SaveData(SAVE)
        active_name = save.active_quest_names()[0]
        active_ref = save.quest_state.value["activeQuests"][active_name]  # type: ignore[union-attr]
        active_object_id = int(active_ref[2])
        inactive = next(item["id"] for item in quests if item["id"] not in save.completed_quest_names())
        recipe_id = next(item["uuid"] for item in recipes if item["uuid"] not in save.unlocked_recipe_ids())
        log_id = next(item["uuid"] for item in logs if item["uuid"] not in save.unlocked_log_ids())
        self.assertTrue(save.complete_quest(active_name))
        save.complete_quest(inactive)
        self.assertEqual(save.unlock_recipes([recipe_id]), 1)
        self.assertEqual(save.unlock_logs([log_id]), 1)
        save.set_world_time(12, 18.5, False)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "progress-edited.db"
            save.save_copy(output)
            loaded = SaveData(output)
            self.assertNotIn(active_name, loaded.active_quest_names())
            self.assertIn(active_name, loaded.completed_quest_names())
            self.assertIn(inactive, loaded.completed_quest_names())
            self.assertIn(recipe_id, loaded.unlocked_recipe_ids())
            self.assertIn(log_id, loaded.unlocked_log_ids())
            self.assertEqual(loaded.time_state.value["timeOfDay"], 12 + 18.5 / 24)  # type: ignore[union-attr]
            self.assertFalse(loaded.time_state.value["timeProgress"])  # type: ignore[union-attr]
            copied = sqlite3.connect(output)
            try:
                self.assertEqual(copied.execute("SELECT count(*) FROM ScriptableObject WHERE id=?", (active_object_id,)).fetchone()[0], 0)
                self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                copied.close()

    def test_installed_progression_catalogs(self) -> None:
        root = find_game_root()
        if not root:
            self.skipTest("local game installation is unavailable")
        catalog = ItemCatalog()
        catalog.load(root)
        self.assertEqual(len(load_quest_catalog(root, catalog)), 46)
        self.assertEqual(len(load_recipe_catalog(root, catalog)), 528)
        self.assertEqual(len(load_log_catalog(root)), 27)
        self.assertGreaterEqual(len(load_customization_catalog(root)), 250)

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_customization_unlock_profile_checksum_and_backup(self) -> None:
        root = find_game_root()
        save = SaveData(SAVE)
        if not root or not save.players:
            self.skipTest("local game or player profile is unavailable")
        steam_id = save.players[0].steam_id
        source = next(
            (parent / "unlock" for parent in SAVE.parents if parent.name == f"User_{steam_id}"),
            None,
        )
        if source is None or not source.is_file():
            self.skipTest("local clothing unlock profile is unavailable")
        existing = CustomizationUnlockProfile.load(source, steam_id)
        clothing = load_customization_catalog(root)
        new_id = next(item["uuid"] for item in clothing if item["uuid"] not in existing.uuids)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "unlock"
            profile = CustomizationUnlockProfile(output, steam_id, list(existing.uuids), [])
            profile.unlock(new_id)
            self.assertIsNone(profile.save())
            loaded = CustomizationUnlockProfile.load(output, steam_id)
            self.assertIn(new_id, loaded.uuids)
            second_id = next(item["uuid"] for item in clothing if item["uuid"] not in loaded.uuids)
            loaded.unlock(second_id)
            backup = loaded.save()
            self.assertIsNotNone(backup)
            self.assertTrue(backup.is_file())  # type: ignore[union-attr]

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_replacing_tool_slot_resets_metadata(self) -> None:
        save = SaveData(SAVE)
        assert save.inventory is not None
        slot = next(
            (candidate for candidate in save.inventory.slots if candidate.metadata != b"\xff\xff\xff\xff"),
            None,
        )
        if slot is None:
            self.skipTest("the current live save has no slot-specific metadata")
        save.inventory.set_slot(slot.number, "3440440b-d362-4473-aa03-b7c41e1fe7ad", 20)
        self.assertEqual(save.inventory.slots[slot.number - 1].metadata, b"\xff\xff\xff\xff")

    def test_catalog_and_png(self) -> None:
        root = find_game_root()
        if root:
            catalog = ItemCatalog()
            catalog.load(root)
            self.assertEqual(catalog.display_name("3440440b-d362-4473-aa03-b7c41e1fe7ad"), "Cotton")
            self.assertGreater(len(catalog.items), 500)
            self.assertGreater(
                sum(item.icon_path is not None or item.icon_atlas_path is not None for item in catalog.items.values()),
                1_000,
            )
        if SAVE.is_file():
            save = SaveData(SAVE)
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "map.png"
                MapRenderer.export_png(output, save.world_maps[1], 320, 240)
                self.assertEqual(output.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_every_saved_terrain_uuid_has_installed_artwork(self) -> None:
        root = find_game_root()
        if not root:
            self.skipTest("local game installation is unavailable")
        save = SaveData(SAVE)
        assert save.terrain_grid is not None
        tiles = load_terrain_tiles(root)
        saved_uuids = {cell.uuid for cell in save.terrain_grid.cells.values()}
        self.assertGreaterEqual(len(tiles), 700)
        self.assertEqual(saved_uuids - tiles.keys(), set())

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_top_down_atlas_covers_and_renders_the_save(self) -> None:
        root = find_game_root()
        if not root:
            self.skipTest("local game installation is unavailable")
        save = SaveData(SAVE)
        assert save.terrain_grid is not None
        renderer = TerrainRenderer(root)
        self.assertIsNotNone(renderer.atlas)
        assert renderer.atlas is not None
        self.assertEqual(renderer.atlas.game_version, "1.0.0.867")
        saved_keys = {fnv1a_uuid(cell.uuid) for cell in save.terrain_grid.cells.values()}
        self.assertEqual(saved_keys - renderer.atlas.tiles.keys(), set())
        image = renderer.render(save.terrain_grid, cell_pixels=16)
        self.assertEqual((image.width, image.height), (2048, 1536))


if __name__ == "__main__":
    unittest.main()
