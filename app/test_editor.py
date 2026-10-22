"""Small non-destructive tests for the save editor core."""

from __future__ import annotations

from array import array
import hashlib
from base64 import b64decode, b64encode
import gzip
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import unquote, urlparse
import uuid

from PIL import Image

from scrap_mechanic_save_editor import ItemCatalog, MapRenderer, SaveData, find_game_root, scan_game_saves
from scrap_editor_app.backup_manager import BackupManager
from scrap_editor_app.cache_manager import cache_state, clear_cache, trim_cache
from scrap_editor_app.backend import EditorApi
from scrap_editor_app.clothing_icons import ClothingIconCache
from scrap_editor_app.mesh_geometry import MeshGeometryCache
from scrap_editor_app.game_unlocks import (
    CustomizationUnlockProfile,
    load_customization_catalog,
    load_log_catalog,
    load_quest_catalog,
    load_recipe_catalog,
)
from scrap_editor_app.profile_data import BlueprintManager, CharacterProfile
from scrap_editor_app.terrain_scene import (
    GamePathResolver,
    TerrainAssetIndex,
    find_tile_scene,
    summarize_tile_scene,
)
from scrap_editor_app.tile_terrain import TileTerrainCache
from terrain_map import (
    BitReader,
    RgbImage,
    TerrainCell,
    TerrainGrid,
    TerrainRenderer,
    blend_cell_seams,
    fnv1a_uuid,
    load_terrain_tile_names,
    load_terrain_tiles,
    upscale_terrain,
)


def find_test_save() -> Path:
    explicit = os.getenv("SCRAP_EDITOR_TEST_SAVE")
    if explicit:
        return Path(explicit)
    appdata = os.getenv("APPDATA")
    if appdata:
        root = Path(appdata) / "Axolot Games" / "Scrap Mechanic" / "User"
        candidates = sorted(
            root.glob("User_*/Save/Survival/*.db"),
            key=lambda path: path.stat().st_size,
            reverse=True,
        )
        # Prefer one representative per database size, then choose a mature
        # world containing the optional player/progression records exercised
        # by the integration tests.  A large freshly generated world can be
        # perfectly valid while those records have not been created yet.
        representatives: list[Path] = []
        seen_sizes: set[int] = set()
        for candidate in candidates:
            size = candidate.stat().st_size
            if size not in seen_sizes:
                seen_sizes.add(size)
                representatives.append(candidate)
        for candidate in representatives[:12]:
            try:
                save = SaveData(candidate)
            except Exception:
                continue
            if (
                len(save.containers) > 100
                and save.inventory is not None
                and save.player_states
                and save.progression_state is not None
                and save.terrain_grid is not None
            ):
                return candidate
        if candidates:
            return candidates[0]
    return Path("__missing_test_save__.db")


SAVE = find_test_save()


def find_creative_save() -> Path:
    return next((path for path, mode in scan_game_saves() if mode == "creative"), Path("__missing_creative_save__.db"))


CREATIVE_SAVE = find_creative_save()


class EditorTests(unittest.TestCase):
    def test_bit_reader_handles_aligned_and_cross_byte_values(self) -> None:
        reader = BitReader(bytes((0b10110110, 0b01101001, 0xAB, 0xCD)))
        self.assertEqual(reader.read_unsigned(3), 0b101)
        self.assertEqual(reader.read_unsigned(7), 0b1011001)
        self.assertEqual(reader.read_unsigned(6), 0b101001)
        self.assertEqual(reader.read_bytes(2), b"\xab\xcd")
        self.assertEqual(reader.bit_offset, 32)

    def test_cache_maintenance_bounds_old_files_and_preserves_recent_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "world-scenes" / "old.json.gz"
            recent = root / "mesh-geometry" / "recent.json.gz"
            temporary = root / "generated-3d" / "capture.tmp"
            for path, size in ((old, 80), (recent, 80), (temporary, 20)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * size)
            now = 2_000_000.0
            os.utime(old, (now - 7200, now - 7200))
            os.utime(recent, (now - 10, now - 10))
            os.utime(temporary, (now - 90000, now - 90000))

            result = trim_cache(root, max_bytes=100, target_bytes=80, now=now)
            self.assertFalse(old.exists())
            self.assertFalse(temporary.exists())
            self.assertTrue(recent.exists())
            self.assertEqual(result["bytes"], 80)
            self.assertEqual(result["removedFiles"], 2)
            self.assertEqual(cache_state(root)["files"], 1)

            cleared = clear_cache(root)
            self.assertEqual(cleared["bytes"], 0)
            self.assertEqual(cleared["removedFiles"], 1)

    def test_game_path_resolver_rejects_non_game_paths(self) -> None:
        resolver = GamePathResolver(Path("C:/Games/Scrap Mechanic"))
        self.assertEqual(
            resolver.resolve("$SURVIVAL_DATA/Terrain/Mesh/test.fbx"),
            Path("C:/Games/Scrap Mechanic/Survival/Terrain/Mesh/test.fbx").resolve(),
        )
        self.assertIsNone(resolver.resolve("C:/untrusted/test.fbx"))

    @unittest.skipUnless(find_game_root(), "local Scrap Mechanic installation is unavailable")
    def test_full_3d_asset_resolution_foundation(self) -> None:
        game_root = find_game_root()
        assert game_root is not None
        index = TerrainAssetIndex(game_root).build()
        dirt_patch = index.get("fcef5bd5-0ec2-42f3-b708-1d0120705d86")
        self.assertIsNotNone(dirt_patch)
        assert dirt_patch is not None
        self.assertEqual(dirt_patch.mesh.suffix.casefold(), ".fbx")
        self.assertTrue(dirt_patch.textures)
        authored_lods = [row for row in index.renderables.values() if len(row.lod_meshes) > 1]
        self.assertGreater(len(authored_lods), 500)
        self.assertEqual(len(authored_lods[0].lod_meshes), len(authored_lods[0].lod_max_view_distances))
        self.assertGreater(len(index.blueprint_primitives), 90)
        self.assertIsNotNone(
            index.get_blueprint_primitive("a6c6ce30-dd47-4587-b475-085d55c6a3b4")
        )

        tile_uuid = "08f0037b-9233-4fe8-b7e1-c1a0c4a2913b"
        metadata = load_terrain_tile_names(game_root)[tile_uuid]
        scene_path = find_tile_scene(game_root, tile_uuid, metadata)
        self.assertIsNotNone(scene_path)
        assert scene_path is not None
        summary = summarize_tile_scene(scene_path)
        self.assertEqual((summary.cells_x, summary.cells_y), (8, 8))
        self.assertGreater(summary.asset_instances, 1000)
        resolved = sum(index.get(item_uuid) is not None for item_uuid in summary.unique_asset_uuids)
        self.assertGreater(resolved, len(summary.unique_asset_uuids) * 0.8)

    @unittest.skipUnless(find_game_root(), "local Scrap Mechanic installation is unavailable")
    def test_compiled_tile_terrain_decodes_real_height_mesh(self) -> None:
        game_root = find_game_root()
        assert game_root is not None
        path = (
            game_root / "Survival" / "Terrain" / "Tiles" / "questtiles"
            / "MechanicStation_QuestTile_01.tile"
        )
        terrain = TileTerrainCache().geometry(path)
        self.assertEqual(terrain["vertices"], 65 * 65)
        self.assertEqual(terrain["triangles"], 64 * 64 * 2)
        self.assertGreater(terrain["renderVertices"], terrain["vertices"])
        self.assertEqual(terrain["renderTriangles"], terrain["triangles"])
        self.assertEqual(terrain["seamTriangles"], 0)
        rendered_positions = array("f")
        rendered_positions.frombytes(b64decode(terrain["positions"]))
        cell_centers = array("f")
        cell_centers.frombytes(b64decode(terrain["cellCenters"]))
        self.assertEqual(len(cell_centers), terrain["renderVertices"] * 2)
        self.assertEqual(set(cell_centers[0::2]), {32.0, 96.0})
        self.assertEqual(set(cell_centers[1::2]), {32.0, 96.0})
        self.assertGreaterEqual(
            min(rendered_positions[2::3]), terrain["bounds"][0][2] - 0.1
        )
        self.assertEqual(
            len(b64decode(terrain["cellOwners"])),
            terrain["renderTriangles"] * 4 * 2,
        )
        self.assertLess(terrain["bounds"][0][2], -6.0)
        self.assertGreater(terrain["bounds"][1][2], 0.5)
        self.assertGreaterEqual(len(terrain["parts"]), 2)
        self.assertIsNone(terrain["waterLevel"])

    @unittest.skipUnless(find_game_root(), "local Scrap Mechanic installation is unavailable")
    def test_compiled_tile_assets_preserve_exact_pipe_quaternion(self) -> None:
        game_root = find_game_root()
        assert game_root is not None
        path = (
            game_root / "Survival" / "Terrain" / "Tiles" / "questtiles"
            / "MechanicStation_QuestTile_01.tile"
        )
        assets = TileTerrainCache().asset_transforms(path)
        pipes = [
            row for row in assets
            if row["uuid"] == "efa95605-3f52-4732-baac-ea1779fa4df2"
        ]
        self.assertEqual(len(assets), 43)
        self.assertEqual(len(pipes), 2)
        self.assertAlmostEqual(abs(pipes[0]["quaternion"][2]), 1.0, places=3)
        self.assertLess(abs(pipes[0]["quaternion"][1]), 0.001)

    @unittest.skipUnless(find_game_root(), "local Scrap Mechanic installation is unavailable")
    def test_compiled_tile_secondary_entities_preserve_exact_transforms(self) -> None:
        game_root = find_game_root()
        assert game_root is not None
        path = (
            game_root / "Survival" / "Terrain" / "Tiles" / "questtiles"
            / "MechanicStation_QuestTile_01.tile"
        )
        cache = TileTerrainCache()
        harvestables = cache.harvestable_transforms(path)
        blueprints = cache.blueprint_transforms(path)
        decals = cache.decal_transforms(path)
        prefabs = cache.prefab_transforms(path)
        self.assertEqual(len(harvestables), 10)
        self.assertEqual(len(blueprints), 14)
        self.assertEqual(len(decals), 212)
        self.assertEqual(len(prefabs), 7)
        self.assertTrue(all(len(row["quaternion"]) == 4 for row in harvestables))
        self.assertTrue(all(len(row["quaternion"]) == 4 for row in decals))
        self.assertTrue(any("Lights_off" in row["path"] for row in prefabs))

    @unittest.skipUnless(find_game_root(), "local Scrap Mechanic installation is unavailable")
    def test_submesh_list_foliage_resolves_diffuse_and_color(self) -> None:
        game_root = find_game_root()
        assert game_root is not None
        index = TerrainAssetIndex(game_root).build()
        foliage = index.get("ccf07c0d-f33b-4218-9324-8acbd6b6e418")
        self.assertIsNotNone(foliage)
        assert foliage is not None
        self.assertTrue(foliage.diffuse_textures[0].is_file())
        self.assertEqual(dict(foliage.color_key_by_material)["0"], "leaves")

    @unittest.skipUnless(
        find_game_root()
        and (find_game_root() / "Release" / "assimp.dll").is_file(),
        "local Scrap Mechanic installation and Assimp are unavailable",
    )
    def test_named_textures_uv_materials_and_collada_scale_are_preserved(self) -> None:
        game_root = find_game_root()
        assert game_root is not None
        index = TerrainAssetIndex(game_root).build()

        packing = index.get("75c11f33-7592-427f-96ac-d89994438042")
        self.assertIsNotNone(packing)
        assert packing is not None
        self.assertEqual(packing.mesh.suffix.casefold(), ".dae")
        self.assertIn(
            "char_packingstation01_dif",
            dict(packing.diffuse_by_material)["texture01"].stem.casefold(),
        )
        with tempfile.TemporaryDirectory() as directory:
            geometry = MeshGeometryCache(
                game_root, Path(directory)
            ).geometry(packing)
        self.assertGreater(
            geometry["bounds"][1][0] - geometry["bounds"][0][0], 40.0
        )

        bridge = index.get("672d8edf-52da-40cd-ad73-d027d1f34a65")
        self.assertIsNotNone(bridge)
        assert bridge is not None
        self.assertTrue(dict(bridge.uv_material_by_material)["m_uvmaterial_industrial"])
        self.assertTrue(dict(bridge.use_uv1_by_material)["m_uvmaterial_industrial"])
        self.assertFalse(dict(bridge.alpha_luma_by_material).get("m_tiled_concrete02", False))
        self.assertIn(
            "tiled_concrete02",
            dict(bridge.diffuse_by_material)["m_tiled_concrete02"].stem.casefold(),
        )
        detail_uv = index.get("ce106c70-b9bd-4347-8b6c-fd8a394e23bb")
        self.assertIsNotNone(detail_uv)
        assert detail_uv is not None
        self.assertEqual(
            dict(detail_uv.material_profile_by_material)["tiling"],
            "DifAsgNorNorD_UV1",
        )
        self.assertNotIn("tiling", dict(detail_uv.use_uv1_by_material))

        crystal = index.get("da59296e-9c61-4ab0-a5ee-ee1715e57128")
        self.assertIsNotNone(crystal)
        assert crystal is not None
        self.assertIn(
            "crystal_medium01",
            dict(crystal.diffuse_by_material)["m_crystal"].stem.casefold(),
        )

    @unittest.skipUnless(find_game_root(), "local Scrap Mechanic installation is unavailable")
    def test_large_native_preview_cache_preserves_more_display_pixels(self) -> None:
        api = EditorApi()
        uri = api._native_preview_uri("7d7556b3-0dc7-4b95-9d92-731013b19fc0")
        self.assertTrue(uri.startswith("file:"))
        path = Path(unquote(urlparse(uri).path).lstrip("/"))
        with Image.open(path) as preview:
            self.assertEqual(preview.size, (880, 600))
            self.assertEqual(preview.mode, "RGBA")

    @unittest.skipUnless(SAVE.is_file(), "local Survival save is unavailable")
    def test_streamed_3d_world_index_uses_grouped_tile_placements(self) -> None:
        api = EditorApi()
        api.load_save({"path": str(SAVE)})
        manifest = api.map_3d_index({})
        self.assertTrue(manifest["available"])
        self.assertGreater(manifest["coverage"]["uniqueScenes"], 1)
        self.assertGreater(manifest["coverage"]["worldPlacements"], manifest["coverage"]["uniqueScenes"])
        self.assertEqual(len(manifest["placements"]), manifest["coverage"]["worldPlacements"])
        self.assertEqual(
            manifest["coverage"]["partialPlacements"],
            sum("cellMask" in placement for placement in manifest["placements"]),
        )
        height_field = manifest["heightField"]
        x_min, x_max, y_min, y_max = manifest["bounds"]
        self.assertEqual(height_field["width"], x_max - x_min + 1)
        self.assertEqual(height_field["height"], y_max - y_min + 1)
        self.assertEqual(
            len(height_field["elevation"]),
            (height_field["width"] + 1) * (height_field["height"] + 1),
        )
        self.assertEqual(
            len(height_field["cliffBase"]),
            height_field["width"] * height_field["height"],
        )
        self.assertEqual(
            len(height_field["flags"]),
            height_field["width"] * height_field["height"],
        )
        self.assertGreater(max(height_field["cliffBase"]), 0)
        covered: dict[tuple[int, int], int] = {}
        for placement in manifest["placements"]:
            scene = manifest["scenes"][placement["scene"]]
            size = int(scene["cellsX"])
            rotation = int(placement["rotation"]) % 4
            local_cells = placement.get("cellMask") or [
                [x, y] for y in range(size) for x in range(size)
            ]
            for local_x, local_y in local_cells:
                if rotation == 0:
                    dx, dy = local_x, local_y
                elif rotation == 1:
                    dx, dy = size - 1 - local_y, local_x
                elif rotation == 2:
                    dx, dy = size - 1 - local_x, size - 1 - local_y
                else:
                    dx, dy = local_y, size - 1 - local_x
                world_cell = (placement["x"] + dx, placement["y"] + dy)
                covered[world_cell] = covered.get(world_cell, 0) + 1
        expected = {
            (x, y)
            for y in range(y_min, y_max + 1)
            for x in range(x_min, x_max + 1)
        }
        self.assertEqual(set(covered), expected)
        self.assertTrue(all(count == 1 for count in covered.values()))

    @unittest.skipUnless(SAVE.is_file(), "local Survival save is unavailable")
    def test_mechanic_station_expands_prefabs_with_compiled_rotations(self) -> None:
        api = EditorApi()
        api.load_save({"path": str(SAVE)})
        manifest = api.map_3d_index({})
        assert api.save is not None and api.save.terrain_grid is not None
        self.assertEqual(
            sum(len(row.get("lakeMask", [])) for row in manifest["placements"]),
            sum(cell.biome == 8 for cell in api.save.terrain_grid.cells.values()),
        )
        scene_index = next(
            index for index, row in enumerate(manifest["scenes"])
            if row.get("name") == "MechanicStation_QuestTile_01"
        )
        placement = next(
            row for row in manifest["placements"] if row["scene"] == scene_index
        )
        self.assertNotIn("lakeMask", placement)
        scene = api.map_3d_scene({
            "uuid": manifest["scenes"][scene_index]["uuid"],
            "x": placement["x"],
            "y": placement["y"],
        })
        batch = api.map_3d_scenes({
            "uuids": [manifest["scenes"][scene_index]["uuid"]],
        })
        self.assertEqual(batch["scenes"][0]["scene"]["uuid"], scene["uuid"])
        packed = api.map_3d_scene_packs({
            "uuids": [manifest["scenes"][scene_index]["uuid"]],
        })
        decoded = json.loads(gzip.decompress(b64decode(packed["scenes"][0]["data"])))
        self.assertEqual(packed["processed"], 1)
        self.assertEqual(decoded["uuid"], scene["uuid"])
        self.assertEqual(scene["coverage"]["expandedPrefabs"], 6)
        self.assertEqual(scene["coverage"]["prefabs"], 0)
        self.assertGreater(scene["coverage"]["instances"], 90)
        self.assertEqual(scene["coverage"]["unsupportedModelFormats"], 0)
        self.assertGreater(scene["coverage"]["renderedDecals"], 100)
        self.assertLessEqual(
            scene["coverage"]["renderedDecals"], scene["coverage"]["decals"]
        )
        self.assertEqual(
            sum(len(group["instances"]) for group in scene["decals"]),
            scene["coverage"]["renderedDecals"],
        )
        blueprint_instances = [
            row for row in scene["instances"] if "blueprint" in row["kind"]
        ]
        self.assertGreater(len(blueprint_instances), 40)
        self.assertTrue(
            any(
                len(row["transformChain"]) >= 2
                and row["transformChain"][-1].get("scale") == [0.25, 0.25, 0.25]
                for row in blueprint_instances
            )
        )
        decal_path = Path(
            unquote(urlparse(scene["decals"][0]["texture"]).path).lstrip("/")
        )
        self.assertTrue(decal_path.is_file())
        with Image.open(decal_path) as decal_image:
            self.assertEqual(decal_image.mode, "RGBA")
            self.assertLess(decal_image.getextrema()[3][0], 255)
        harvestables = [
            row for row in scene["instances"] if row["kind"] == "harvestable"
        ]
        self.assertEqual(len(harvestables), 10)
        self.assertTrue(all("quaternion" in row["transform"] for row in harvestables))
        station = next(
            row for row in scene["instances"]
            if row["uuid"] == "2cd68bd4-c145-4ef0-aabb-01191cbcfc7b"
        )
        self.assertEqual(len(station["transformChain"]), 2)
        self.assertIn("quaternion", station["transformChain"][1])
        station_definition = scene["definitions"][station["asset"]]
        self.assertIn("redmetal", station_definition["materialProfiles"])

    @unittest.skipUnless(SAVE.is_file(), "local Survival save is unavailable")
    def test_warehouse_recursively_expands_nested_building_prefabs(self) -> None:
        api = EditorApi()
        api.load_save({"path": str(SAVE)})
        manifest = api.map_3d_index({})
        scene_index = next(
            index for index, row in enumerate(manifest["scenes"])
            if row.get("name") == "Warehouse_Exterior_2Floors_256_01_NEW"
        )
        placement = next(
            row for row in manifest["placements"] if row["scene"] == scene_index
        )
        scene = api.map_3d_scene({
            "uuid": manifest["scenes"][scene_index]["uuid"],
            "x": placement["x"],
            "y": placement["y"],
        })
        nested = [
            row for row in scene["instances"] if len(row["transformChain"]) >= 3
        ]
        self.assertGreater(scene["coverage"]["expandedPrefabs"], 19)
        self.assertEqual(scene["coverage"]["prefabs"], 0)
        self.assertGreater(scene["coverage"]["instances"], 450)
        self.assertGreater(len(nested), 80)
        self.assertGreater(scene["coverage"]["decals"], 555)

    @unittest.skipUnless(SAVE.is_file(), "local Survival save is unavailable")
    def test_real_3d_defers_projectors_helpers_and_duplicate_runtime_states(self) -> None:
        api = EditorApi()
        api.load_save({"path": str(SAVE)})
        manifest = api.map_3d_index({})
        scenes: dict[str, dict] = {}
        for name in (
            "Ruin_AutumnForest_RuinsQuest_128_01",
            "PackingStation_Vegetable_128_01",
        ):
            scene_index = next(
                index for index, row in enumerate(manifest["scenes"])
                if row.get("name") == name
            )
            placement = next(
                row for row in manifest["placements"] if row["scene"] == scene_index
            )
            scenes[name] = api.map_3d_scene({
                "uuid": manifest["scenes"][scene_index]["uuid"],
                "x": placement["x"],
                "y": placement["y"],
            })

        ruin = scenes["Ruin_AutumnForest_RuinsQuest_128_01"]
        self.assertNotIn(
            "shape_square",
            {group["name"].casefold() for group in ruin["decals"]},
        )
        identities = {
            json.dumps(
                {
                    "uuid": row["uuid"],
                    "transform": row["transform"],
                    "chain": row["transformChain"],
                    "colors": row["colors"],
                    "tint": row["tint"],
                },
                sort_keys=True,
            )
            for row in ruin["instances"]
        }
        self.assertEqual(len(identities), len(ruin["instances"]))

        packing = scenes["PackingStation_Vegetable_128_01"]
        names = {row["name"].casefold() for row in packing["definitions"]}
        self.assertFalse(any("arealight" in name for name in names))
        self.assertTrue(any("packingstation_base" in name for name in names))
        self.assertTrue(any("packingstation_mid" in name for name in names))
        self.assertTrue(any("packingstation_front" in name for name in names))

    @unittest.skipUnless(SAVE.is_file(), "local Survival save is unavailable")
    def test_real_3d_water_mesh_uses_procedural_surface_material(self) -> None:
        api = EditorApi()
        api.load_save({"path": str(SAVE)})
        manifest = api.map_3d_index({})
        scene_index = next(
            index for index, row in enumerate(manifest["scenes"])
            if row.get("name") == "BuilderQuest_Steelbridge_128_01"
        )
        placement = next(
            row for row in manifest["placements"] if row["scene"] == scene_index
        )
        scene = api.map_3d_scene({
            "uuid": manifest["scenes"][scene_index]["uuid"],
            "x": placement["x"],
            "y": placement["y"],
        })
        self.assertGreater(scene["coverage"]["blueprints"], 0)
        self.assertGreater(
            scene["coverage"]["renderedKinds"].get("blueprint", 0), 100
        )
        self.assertTrue(
            any(row["name"] == "bq_steelbridge_01" for row in scene["definitions"])
        )
        water = next(
            row for row in scene["definitions"]
            if row["uuid"] == "990cce84-a683-4ea6-83cc-d0aee5e71e15"
        )
        self.assertEqual(water["specialSurface"], "water")
        burnmark = next(
            group for group in scene["decals"]
            if group["uuid"] == "245fc33a-d856-4650-a66d-e211c761458c"
        )
        self.assertTrue(
            all("quaternion" in instance["transform"] for instance in burnmark["instances"])
        )
        source = (
            Path(__file__).resolve().parent / "scrap_editor_app" / "web" / "map3d.js"
        ).read_text(encoding="utf-8")
        self.assertIn('special==="water"', source)
        self.assertIn("placement.lakeMask", source)
        self.assertIn("part.cliffOnly", source)
        self.assertIn("geometry.textureDetails", source)
        self.assertIn("embedded.asgUri", source)
        self.assertIn("/meshdecal|lightcone/i.test(profile)", source)
        self.assertIn("gl.UNPACK_FLIP_Y_WEBGL,true", source)
        self.assertIn("layout(location=13) in vec2 aUv1", source)
        self.assertIn("uUseUv1", source)
        self.assertIn("uUvMaterial", source)
        self.assertIn("mix(vColor,texel.rgb,texel.a)", source)
        self.assertIn("paintTexture:true,tintTexture:false", source)
        self.assertIn("uUseAsg?asg.r:texel.a", source)
        self.assertIn("this.yaw+=dx*.0045", source)
        self.assertIn("this.pitch+dy*.0045", source)
        self.assertIn('cross(this._forward(),[0,0,1])', source)
        self.assertIn('(this.keys.has("q")?1:0)-(this.keys.has("e")?1:0)', source)
        self.assertIn("async _loadFullWorld(generation,options={})", source)
        self.assertIn("options.entities===false", source)
        self.assertIn("Complete 3D world", source)
        self.assertIn("generatedHeight", source)
        self.assertIn("generatedElevation", source)
        self.assertIn("layout(location=14) in vec2 aTerrainCellCenter", source)
        self.assertIn("uTerrainCellHeight", source)
        self.assertIn('this._uniform("uFlags"),13', source)
        self.assertIn("subdivisions=4", source)
        self.assertIn("deformSurface:true", source)
        self.assertIn("part.terrainBlend||part.deformSurface", source)
        self.assertIn("gl.cullFace(gl.BACK)", source)
        self.assertIn("uCliffOnly", source)
        self.assertIn("_indexSpatialItem", source)
        self.assertIn('this._uniform("uViewProjection")', source)
        self.assertIn('"map-3d-scene-packs"', source)
        self.assertIn("DecompressionStream", source)
        self.assertIn("Date.now()+15000", source)
        self.assertIn('"map-3d-meshes"', source)
        self.assertIn('"q","e"', source)
        self.assertIn("this.camera", source)
        self.assertNotIn("this.target", source)
        self.assertNotIn("continuousSurface", source)
        self.assertIn("_waitForLoads", source)
        self.assertIn('"map-3d-entities"', source)
        self.assertNotIn('row.entityType!=="creation"', source)
        self.assertNotIn("_refreshSpatialItem", source)
        self.assertNotIn("_scheduleWorldStream", source)
        self.assertIn("gl.DYNAMIC_DRAW", source)
        self.assertIn("item.visibleInstances=item.totalInstances", source)
        self.assertIn("_desiredLod", source)
        self.assertIn("_queueLod", source)
        self.assertIn("lodMaxViewDistances", source)
        self.assertIn("EXT_disjoint_timer_query_webgl2", source)
        self.assertIn("this.spatialBuckets", source)
        self.assertIn("_ensureVisibleTextures", source)
        self.assertIn("this.textureBudget=2048*1024*1024", source)
        self.assertIn("meta.liveVisible=true", source)
        self.assertIn("meta.residentUntil=Math.max", source)
        self.assertIn("!meta.loading&&!meta.liveVisible", source)
        self.assertIn("_evictTexture(meta)", source)
        self.assertIn("meta.failures<3", source)
        self.assertIn("meta.retryTimer=setTimeout", source)
        self.assertIn("simplified(8)", source)
        self.assertIn("lodMinViewSizes:[900,360,110,0]", source)
        self.assertIn("terrain.cellOwners", source)
        self.assertIn("float steep=", source)
        self.assertIn("this.fallbackTexture=this._solidTexture", source)
        self.assertIn("terrainTextures:new Array(9).fill(this.fallbackTexture)", source)
        self.assertIn("Math.hypot(...side)<1e-6", source)
        self.assertNotIn("const renderError=gl.getError()", source)
        self.assertNotIn("deformToTerrain", source)
        self.assertNotIn("_addWorldSeal", source)
        self.assertIn("Math.max(.2,Math.min(8,this.distance/1500))", source)
        self.assertIn("async captureTopDown(requestedWidth,requestedHeight)", source)
        self.assertIn("_applyCaptureTileVisibility", source)
        self.assertIn("_prepareCaptureLods", source)
        self.assertIn("_waitForCaptureTextures(generation", source)
        self.assertIn("meta.capturePinned=true", source)
        self.assertIn("part.materialPromise=load", source)
        self.assertIn("if(count===1||item.entity||this.captureViewport)return 0", source)
        wait_for_loads = source[
            source.index("async _waitForLoads") : source.index("instanceCount()")
        ]
        self.assertNotIn("pendingLoads.clear()", wait_for_loads)
        self.assertIn("tileSide=Math.max(512,Math.min(4096", source)
        self.assertIn("gl.polygonOffset(-4,-6)", source)
        self.assertIn("_instanceInMask(instance,mask)", source)
        self.assertIn("if(part.decal){gl.enable(gl.POLYGON_OFFSET_FILL)", source)
        self.assertNotIn("slice(0,180)", source)
        self.assertNotIn("this.clear();this.status(`Streaming", source)
        self.assertIn("WASD", (
            Path(__file__).resolve().parent
            / "scrap_editor_app" / "web" / "index.html"
        ).read_text(encoding="utf-8"))
        self.assertNotIn(")*vTerrainColor.rgb", source)

        html = (
            Path(__file__).resolve().parent / "scrap_editor_app" / "web" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn('data-kind="Enemy"', html)
        self.assertNotIn('data-kind="Creation"', html)
        self.assertNotIn('id="mapDetailToggle"', html)
        self.assertNotIn("Balanced", html)

        app_source = (
            Path(__file__).resolve().parent / "scrap_editor_app" / "web" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("generate2dMapFrom3d", app_source)
        self.assertIn('api("store-3d-map-tile"', app_source)
        self.assertIn('api("finalize-3d-map-tiles"', app_source)
        self.assertIn("onOverviewTile", app_source)
        self.assertIn("await buildDetailedMap(true)", app_source)
        self.assertIn("storeTile=tile=>{if(generation!==state.map.generation)", app_source)
        self.assertIn("meta.needsCapture", app_source)

    @unittest.skipUnless(SAVE.is_file(), "local Survival save is unavailable")
    def test_generated_3d_map_cache_validates_and_persists_png(self) -> None:
        api = EditorApi()
        api.load_save({"path": str(SAVE)})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "generated-map.png"
            api._generated_3d_map_info = lambda **_kwargs: {
                "path": target,
                "width": 8,
                "height": 6,
                "cellPixels": 1,
                "revision": 99,
            }
            api.ensure_companion_preview = lambda _payload: {
                "available": True,
                "width": 4,
                "height": 3,
                "cellPixels": 1,
                "bounds": [0, 3, 0, 2],
                "worlds": [1],
                "image": "file:///preview.png",
                "generated3d": False,
                "preview": True,
            }
            before = api.map_meta({})
            self.assertTrue(before["needsCapture"])
            self.assertTrue(before["preview"])
            self.assertEqual(before["image"], "file:///preview.png")
            self.assertEqual(before["capture"]["width"], 8)

            source = Path(directory) / "source.png"
            Image.new("RGB", (8, 6), (18, 52, 71)).save(source)
            payload = "data:image/png;base64," + b64encode(source.read_bytes()).decode("ascii")
            after = api.store_3d_map({"data": payload})
            self.assertTrue(target.is_file())
            self.assertTrue(after["generated3d"])
            self.assertFalse(after["needsCapture"])
            self.assertEqual(after["image"], target.resolve().as_uri())

            wrong = Path(directory) / "wrong.png"
            Image.new("RGB", (7, 6), (0, 0, 0)).save(wrong)
            invalid = "data:image/png;base64," + b64encode(wrong.read_bytes()).decode("ascii")
            with self.assertRaisesRegex(ValueError, "Expected an 8 x 6 PNG map"):
                api.store_3d_map({"data": invalid})

            target.unlink()
            api._exact_map_water_atlas = lambda: None
            for column, color in enumerate(((180, 40, 30), (20, 90, 180))):
                tile = Path(directory) / f"tile-{column}.png"
                Image.new("RGB", (4, 6), color).save(tile)
                data = "data:image/png;base64," + b64encode(tile.read_bytes()).decode("ascii")
                result = api.store_3d_map_tile({
                    "data": data, "column": column, "row": 0,
                    "columns": 2, "rows": 1, "width": 4, "height": 6,
                    "displayX": column * 4, "displayY": 0,
                    "displayWidth": 4, "displayHeight": 6, "scale": 1,
                })
            self.assertTrue(result["ready"])
            streamed = api.finalize_3d_map_tiles({})
            self.assertTrue(streamed["generated3d"])
            with Image.open(target) as assembled:
                self.assertEqual(assembled.getpixel((1, 2)), (180, 40, 30))
                self.assertEqual(assembled.getpixel((6, 2)), (20, 90, 180))

    @unittest.skipUnless(SAVE.is_file() and find_game_root(), "local Survival save unavailable")
    def test_real_3d_entities_decode_save_units_and_shapes(self) -> None:
        api = EditorApi()
        api.load_save({"path": str(SAVE)})
        data = api.map_3d_entities({})
        self.assertGreaterEqual(data["counts"]["players"], 1)
        self.assertGreater(data["counts"]["units"], 0)
        self.assertGreater(data["counts"]["shapes"], 0)
        self.assertTrue(data["definitions"])
        self.assertTrue(any(row["entityType"] == "creation" for row in data["instances"]))
        creation_scales = [
            float(value)
            for row in data["instances"]
            if row["entityType"] == "creation"
            for transform in row.get("transformChain", [])
            for value in transform.get("scale", [1, 1, 1])
        ]
        self.assertTrue(creation_scales)
        self.assertLessEqual(max(creation_scales), 32.0)
        self.assertEqual(EditorApi._saved_orientation(0), (1, 3))
        self.assertEqual(EditorApi._saved_orientation(0x63), (-2, 3))

    def test_real_3d_fit_returns_to_the_top_down_world_view(self) -> None:
        source = (
            Path(__file__).resolve().parent / "scrap_editor_app" / "web" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("function fitMeshMap(){ensureMeshRenderer().topView()}", source)
        self.assertNotIn('id="mapTop3d"', (
            Path(__file__).resolve().parent / "scrap_editor_app" / "web" / "index.html"
        ).read_text(encoding="utf-8"))
        self.assertIn("toggleWorld3d", source)

    @unittest.skipUnless(
        SAVE.is_file() and find_game_root()
        and (find_game_root() / "Release" / "assimp.dll").is_file(),
        "local Survival save and installed Assimp are unavailable",
    )
    def test_real_3d_mesh_endpoint_returns_packed_triangle_geometry(self) -> None:
        api = EditorApi()
        api.load_save({"path": str(SAVE)})
        geometry = api.map_3d_mesh({
            "uuid": "fcef5bd5-0ec2-42f3-b708-1d0120705d86",
        })
        self.assertGreater(geometry["vertices"], 100)
        self.assertGreater(geometry["triangles"], 100)
        self.assertTrue(geometry["parts"])
        part = geometry["parts"][0]
        self.assertEqual(len(b64decode(part["positions"])), part["vertices"] * 12)
        self.assertEqual(len(b64decode(part["uv1s"])), part["vertices"] * 8)
        self.assertEqual(len(b64decode(part["indices"])), part["indicesCount"] * 4)

        proprietary = api.map_3d_mesh({
            "uuid": "07232236-22eb-4912-8774-ab185f368bb9",
        })
        self.assertEqual(proprietary["format"], "mesh")
        self.assertGreater(proprietary["triangles"], 0)

        rock_geometry = api.map_3d_mesh({
            "uuid": "dabe4e2b-f8f6-49df-8f20-6e31999c887d",
        })
        rock_part = rock_geometry["parts"][0]
        rock_texture = api.map_3d_texture({
            "uuid": "dabe4e2b-f8f6-49df-8f20-6e31999c887d",
            "material": rock_part["material"],
            "materialName": rock_part["materialName"],
        })
        self.assertTrue(rock_texture["uri"].startswith("file:"))
        self.assertTrue(rock_texture["asgUri"].startswith("file:"))
        self.assertTrue(rock_texture["alphaLuma"])

        oak_uuid = "ab2221f1-b86a-45aa-b28f-dac06d2c71d8"
        oak_near = api.map_3d_mesh({"uuid": oak_uuid, "lod": 0})
        oak_far = api.map_3d_mesh({"uuid": oak_uuid, "lod": "coarsest"})
        self.assertGreater(oak_far["lod"], oak_near["lod"])
        self.assertEqual(oak_near["lodCount"], oak_far["lodCount"])
        self.assertLess(oak_far["triangles"], oak_near["triangles"])
        batch = api.map_3d_meshes({
            "requests": [
                {"uuid": oak_uuid, "lod": 0},
                {"uuid": oak_uuid, "lod": oak_far["lod"]},
            ]
        })
        self.assertEqual([row["lod"] for row in batch["meshes"]], [0, oak_far["lod"]])

    def test_native_preview_placement_origin_for_every_rotation(self) -> None:
        size, origin_x, origin_y = 4, 10, -7
        offsets = (1, 2)
        locals_by_rotation = ((1, 2), (1, 1), (2, 1), (2, 2))
        for rotation, (local_x, local_y) in enumerate(locals_by_rotation):
            cell = TerrainCell(
                x=origin_x + local_x,
                y=origin_y + local_y,
                uuid=str(uuid.uuid4()),
                rotation=rotation,
                x_offset=offsets[0],
                y_offset=offsets[1],
                flags=0,
            )
            self.assertEqual(EditorApi._placement_origin(cell, size), (origin_x, origin_y))

    def test_detailed_terrain_blends_cell_edges_and_upscales(self) -> None:
        width, height = 32, 16
        pixels = bytearray()
        for _y in range(height):
            for x in range(width):
                value = 20 if x in (15, 16) else (80 if x < 16 else 120)
                pixels.extend((value, value, value))
        source = RgbImage(width, height, pixels)
        original_jump = abs(source.get(15, 8)[0] - source.get(14, 8)[0])
        seamless = blend_cell_seams(source, 16, radius=2)
        blended_jump = abs(seamless.get(15, 8)[0] - seamless.get(14, 8)[0])
        self.assertLess(blended_jump, original_jump)
        detailed = upscale_terrain(seamless, 64, 32)
        self.assertEqual((detailed.width, detailed.height), (64, 32))

    @unittest.skipUnless(CREATIVE_SAVE.is_file(), "local Creative/Sandbox save is unavailable")
    def test_creative_sandbox_inventory_and_capabilities(self) -> None:
        save = SaveData(CREATIVE_SAVE)
        summary = save.summary()
        self.assertEqual(summary["mode"], "creative")
        self.assertEqual(summary["inventory_slots"], 30)
        self.assertTrue(summary["capabilities"]["inventory"])
        self.assertTrue(summary["capabilities"]["settings"])
        self.assertFalse(summary["capabilities"]["quests"])
        self.assertFalse(summary["capabilities"]["map"])
        assert save.inventory is not None
        save.inventory.set_slot(1, "3440440b-d362-4473-aa03-b7c41e1fe7ad", 12)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "Save" / "creative-edited.db"
            save.save_copy(output)
            loaded = SaveData(output)
            self.assertEqual(loaded.mode, "creative")
            self.assertEqual(loaded.inventory.slots[0].quantity, 12)  # type: ignore[union-attr]
            self.assertEqual(loaded.integrity, "ok")

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_external_backup_versions_and_safe_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "game" / "Save" / "Survival" / "world.db"
            source.parent.mkdir(parents=True)
            source.write_bytes(SAVE.read_bytes())
            manager = BackupManager(root / "external-backups")
            first = manager.archive(source, category="Survival", kind="manual")
            second = manager.archive(source, category="Survival", kind="manual")
            self.assertNotEqual(first["id"], second["id"])
            self.assertTrue(Path(first["path"]).is_relative_to(manager.root))
            original_seed = SaveData(source).game["seed"]
            edited = SaveData(source)
            edited.set_game_fields(edited.game["flags"], original_seed + 1, edited.game["gametick"])
            edited.save_copy(source, overwrite_source=True, create_adjacent_backup=False)
            self.assertEqual(SaveData(source).game["seed"], original_seed + 1)
            restored, safety = manager.restore(first["id"])
            self.assertEqual(restored["sourcePath"], str(source.resolve()))
            self.assertIsNotNone(safety)
            self.assertEqual(SaveData(source).game["seed"], original_seed)
            self.assertEqual(len(manager.list()), 3)
            self.assertEqual(list(source.parent.glob("*.bak")), [])

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
        self.assertGreater(len(save.terrain_grid.elevation), len(save.terrain_grid.cells))  # type: ignore[union-attr]
        self.assertGreater(max(save.terrain_grid.cliff_level.values()), 0)  # type: ignore[union-attr]
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
    def test_player_recovery_perks_and_beacons_roundtrip(self) -> None:
        save = SaveData(SAVE)
        if not save.players or not save.player_states:
            self.skipTest("player location and status records are unavailable")
        player = save.players[0]
        target = (player.x + 1.25, player.y - 2.5, player.z + 0.75)
        save.teleport_player(0, *target)
        save.set_player_perks(0, (2, 4))
        save.repair_player_flags(0)
        if save.unit_manager_state is not None:
            save.set_creation_aggro(False)
            save.clear_death_markers()
        linked_beacon = next((item for item in save.beacon_data() if item["linked"]), None)
        if linked_beacon:
            save.set_beacon_icon(linked_beacon["id"], 7, 4)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "recovery-edited.db"
            save.save_copy(output)
            loaded = SaveData(output)
            self.assertEqual(
                tuple(round(value, 3) for value in (loaded.players[0].x, loaded.players[0].y, loaded.players[0].z)),
                tuple(round(value, 3) for value in target),
            )
            stats = loaded.player_states[0].value["stats"]
            self.assertEqual({int(key) for key, enabled in stats["perks"].items() if enabled}, {2, 4})
            self.assertFalse(loaded.player_states[0].value.get("inOil", False))
            self.assertFalse(loaded.player_states[0].value.get("inChemical", False))
            if loaded.unit_manager_state is not None:
                self.assertEqual(loaded.death_marker_count(), 0)
                self.assertFalse(
                    loaded.unit_manager_state.value.get("hostSettings", {}).get("aggroCreations", True)
                )
            if linked_beacon:
                result = next(item for item in loaded.beacon_data() if item["id"] == linked_beacon["id"])
                self.assertEqual((result["iconIndex"], result["colorIndex"]), (7, 4))
            self.assertEqual(loaded.integrity, "ok")

    @unittest.skipUnless(SAVE.is_file(), "local test save is unavailable")
    def test_world_services_weather_and_tornado_roundtrip(self) -> None:
        save = SaveData(SAVE)
        if save.weather_state is None or save.tornado_state is None:
            self.skipTest("weather or tornado service storage is unavailable")
        self.assertTrue(save.summary()["capabilities"]["worldServices"])
        self.assertTrue(save.registered_worlds())
        save.set_weather_condition("Rain")
        save.set_weather_period(5, "ThunderRain")
        self.assertEqual(save.append_weather_day("Clear"), 3)
        cleared = save.reset_tornado_service()
        self.assertGreaterEqual(cleared["visited"], 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "services-edited.db"
            save.save_copy(output)
            loaded = SaveData(output)
            self.assertEqual(
                loaded.weather_queue(),
                ["Rain"] * 5 + ["ThunderRain"] * 3 + ["Clear"] * 4,
            )
            self.assertEqual(loaded.tornado_state.value["tornadoIdCounter"], 1)  # type: ignore[union-attr]
            self.assertEqual(loaded.tornado_state.value["tornadoSaveData"], {})  # type: ignore[union-attr]
            self.assertEqual(loaded.tornado_state.value["visitedTornadoCells"], {})  # type: ignore[union-attr]
            self.assertEqual(loaded.integrity, "ok")

    def test_beacon_and_weather_game_catalogs(self) -> None:
        api = EditorApi()
        if not api.game_root:
            self.skipTest("local game installation is unavailable")
        self.assertEqual(len(api.weather_catalog["conditions"]), 6)
        self.assertEqual(len(api.weather_catalog["forecasts"]), 5)
        self.assertEqual(len(api.beacon_visuals["colors"]), 8)
        self.assertTrue(
            (api.game_root / "Data" / "Gui" / "Resolutions" / "3840x2160" / "Beacon" / "BeaconIcons@4K.png").is_file()
        )

    def test_character_profile_exact_roundtrip(self) -> None:
        choices = [str(uuid.uuid4()) if index % 2 else None for index in range(9)]
        palettes = list(range(9))
        blob = (
            (2).to_bytes(4, "big")
            + bytes((0, 9))
            + b"".join(uuid.UUID(value).bytes if value else bytes(16) for value in choices)
            + b"".join(value.to_bytes(4, "big") for value in palettes)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "character"
            path.write_bytes(blob)
            profile = CharacterProfile.load(path)
            self.assertEqual(profile.options, choices)
            self.assertEqual(profile.palettes, palettes)
            self.assertFalse(profile.dirty)
            replacement = str(uuid.uuid4())
            profile.set_gender(1)
            profile.set_option(8, replacement, 27)
            self.assertTrue(profile.dirty)
            profile.save()
            loaded = CharacterProfile.load(path)
            self.assertEqual((loaded.gender, loaded.options[8], loaded.palettes[8]), (1, replacement, 27))
            self.assertEqual(len(path.read_bytes()), 186)

    def test_blueprint_manager_duplicate_export_and_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "User_1"
            blueprint_id = str(uuid.uuid4())
            folder = user / "Blueprints" / blueprint_id
            folder.mkdir(parents=True)
            (folder / "blueprint.json").write_text(
                json.dumps({"bodies": [{"childs": []}], "joints": []}), encoding="utf-8"
            )
            (folder / "description.json").write_text(
                json.dumps({"name": "Test vehicle", "description": "Before", "fileId": 123}),
                encoding="utf-8",
            )
            manager = BlueprintManager(user, root / "Archives")
            self.assertEqual(manager.list()[0]["name"], "Test vehicle")
            duplicate_id = manager.duplicate(blueprint_id, "Test copy")
            duplicate = next(item for item in manager.list() if item["id"] == duplicate_id)
            self.assertEqual(duplicate["name"], "Test copy")
            self.assertIsNone(duplicate["fileId"])
            archive = manager.export(duplicate_id, duplicate["name"])
            self.assertTrue(archive.is_file())
            imported_id = manager.import_archive(archive)
            self.assertIn(imported_id, {item["id"] for item in manager.list()})

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
        # Axolot occasionally adds or removes installed recipes between game
        # builds; validate broad catalogue coverage instead of pinning one
        # external installation revision.
        self.assertGreaterEqual(len(load_recipe_catalog(root, catalog)), 500)
        self.assertEqual(len(load_log_catalog(root)), 27)
        self.assertGreaterEqual(len(load_customization_catalog(root)), 250)

    def test_installed_customization_icons_are_real_garment_thumbnails(self) -> None:
        root = find_game_root()
        if not root:
            self.skipTest("local game installation is unavailable")
        clothing = load_customization_catalog(root)
        with tempfile.TemporaryDirectory() as directory:
            icons = ClothingIconCache(root, Path(directory))
            self.assertGreaterEqual(icons.frame_count, 450)
            samples = []
            for category in ("Torso", "Gloves", "Legs", "Shoes", "Hat", "Backpack"):
                item = next(entry for entry in clothing if entry["category"] == category)
                url = icons.icon_url(item["uuid"], 0)
                self.assertIsNotNone(url, item["name"])
                path = Path(url.removeprefix("file:///"))  # paths in this Windows-only game test contain no URL escapes
                if not path.is_file():
                    from urllib.parse import unquote, urlparse
                    path = Path(unquote(urlparse(url).path).lstrip("/"))
                blob = path.read_bytes()
                self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")
                samples.append(hashlib.sha256(blob).digest())
            self.assertEqual(len(set(samples)), len(samples))

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

        x_min, _x_max, _y_min, y_max = save.terrain_grid.visible_bounds
        bounds = (x_min, x_min + 3, y_max - 3, y_max)
        cells = {
            (x, y): save.terrain_grid.cells[(x, y)]
            for y in range(bounds[2], bounds[3] + 1)
            for x in range(bounds[0], bounds[1] + 1)
        }
        sample = TerrainGrid(save.terrain_grid.seed, bounds, bounds, 0, cells)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sample.png"
            renderer.render_detailed_png(
                output,
                sample,
                cell_pixels=32,
            )
            self.assertTrue(output.is_file())
            with Image.open(output) as image:
                self.assertEqual(image.size, (128, 128))


if __name__ == "__main__":
    unittest.main()
