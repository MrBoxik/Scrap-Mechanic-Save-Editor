from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import base64
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import threading
import uuid
from typing import Any
from urllib.parse import unquote, urlparse

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat
import numpy as np

from scrap_mechanic_save_editor import (
    EMPTY_UUID,
    CatalogItem,
    ItemCatalog,
    MapFeature,
    SaveData,
    default_save_root,
    find_game_root,
    load_game_json,
    scan_game_saves,
)

from . import __version__
from .steam_paths import installation_diagnostics
from .game_status import require_game_closed
from terrain_map import LuaArray, TerrainRenderer, load_terrain_tile_names
from .game_unlocks import (
    CustomizationUnlockProfile,
    load_customization_catalog,
    load_log_catalog,
    load_quest_catalog,
    load_recipe_catalog,
)
from .backup_manager import BackupManager
from .cache_manager import cache_state, clear_cache, trim_cache
from .clothing_icons import ClothingIconCache
from .profile_data import (
    BlueprintManager,
    CHARACTER_SLOTS,
    CharacterProfile,
)
from .terrain_scene import (
    TerrainAssetIndex,
    TerrainDecalIndex,
    find_tile_scene,
    summarize_tile_scene,
)
from .mesh_geometry import MeshGeometryCache
from .tile_terrain import TileTerrainCache


PROJECT_DIR = Path(__file__).resolve().parent.parent
ASSET_DIR = PROJECT_DIR / "assets"
CACHE_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ScrapMechanicSaveEditor" / "map-cache"
MAP_CELL_PIXELS = 32
MAP_RENDER_REVISION = 4
GENERATED_3D_MAP_REVISION = 11
GENERATED_3D_MAP_MAX_SIDE = 8192
NATIVE_PREVIEW_REVISION = 2
WORLD_SCENE_CACHE_REVISION = 7
WORLD_ENTITY_CACHE_REVISION = 3
NATIVE_ISO_CELL_WIDTH = 32
NATIVE_ISO_CELL_HEIGHT = 16
WATER_SURFACE_UUID = "990cce84-a683-4ea6-83cc-d0aee5e71e15"
CHEMICAL_SURFACE_UUID = "f0f2db63-1f2e-4aae-8dc4-ab7cd6fe2658"


def _exact_atlas_water_mask(
    image: Image.Image,
    *,
    include_shore_teal: bool = False,
) -> Image.Image:
    """Return the blue-water pixels from the game's exact terrain atlas.

    The WebGL reconstruction is intentionally much more detailed than the
    bundled cell atlas, but water is a special case: the atlas is the game's
    already-resolved top-down coastline stencil.  Asset-level water planes do
    not describe every lake/shore combination reliably (especially large
    excavation and bridge tiles), while biome flags are too broad and flood
    deliberately dry quarries.  Colour separation keeps islands, rocks, and
    structures out of the stencil instead of treating a whole lake cell as
    water.
    """
    rgb = image.convert("RGB")
    red, green, blue = rgb.split()
    blue_over_green = ImageChops.subtract(blue, green).point(
        lambda value: 255 if value >= 8 else 0
    )
    green_over_red = ImageChops.subtract(green, red).point(
        lambda value: 255 if value >= 22 else 0
    )
    strict = ImageChops.multiply(blue_over_green, green_over_red)
    if not include_shore_teal:
        return strict

    # The installed atlas antialiases water into a dark teal fringe.  On a
    # few shore cells that fringe also contains small rectangular helper
    # patches.  Requiring blue to be greater than green rejected those pixels
    # and exposed the green WebGL terrain below.  Admit only teal pixels that
    # touch already-confirmed water; the strong green-over-red test excludes
    # ordinary grass and the proximity gate protects real inland geometry.
    pixels = np.asarray(rgb, dtype=np.int16)
    red_values = pixels[:, :, 0]
    green_values = pixels[:, :, 1]
    blue_values = pixels[:, :, 2]
    teal = (
        (blue_values - green_values >= -8)
        & (green_values - red_values >= 38)
    )
    near_water = np.asarray(
        strict.filter(ImageFilter.MaxFilter(21)), dtype=np.uint8
    ) >= 128
    broadened = (np.asarray(strict, dtype=np.uint8) >= 128) | (teal & near_water)
    return Image.fromarray(np.where(broadened, 255, 0).astype(np.uint8), "L")


def _remove_small_water_mask_holes(mask: Image.Image) -> Image.Image:
    """Fill small atlas fragments that cannot be real islands.

    Survival's top-down atlas contains repeated helper/preview fragments in
    ocean cells.  They are disconnected from every coast and become the dark
    green rectangles reported in the generated map.  The detailed 3D capture
    remains authoritative for large islands and landmarks; this removes only
    small four-connected holes completely surrounded by detected water.
    """
    water = np.asarray(mask.convert("L"), dtype=np.uint8) >= 128
    land = ~water
    height, width = land.shape
    seen = np.zeros((height, width), dtype=np.bool_)
    fill = np.zeros((height, width), dtype=np.bool_)
    for row, column in zip(*np.nonzero(land)):
        row = int(row)
        column = int(column)
        if seen[row, column]:
            continue
        stack = [(row, column)]
        seen[row, column] = True
        component: list[tuple[int, int]] = []
        touches_edge = False
        min_row = max_row = row
        min_column = max_column = column
        too_large = False
        while stack:
            current_row, current_column = stack.pop()
            component.append((current_row, current_column))
            min_row = min(min_row, current_row)
            max_row = max(max_row, current_row)
            min_column = min(min_column, current_column)
            max_column = max(max_column, current_column)
            if (
                current_row == 0
                or current_column == 0
                or current_row == height - 1
                or current_column == width - 1
            ):
                touches_edge = True
            if len(component) > 500:
                too_large = True
            for next_row, next_column in (
                (current_row - 1, current_column),
                (current_row + 1, current_column),
                (current_row, current_column - 1),
                (current_row, current_column + 1),
            ):
                if (
                    0 <= next_row < height
                    and 0 <= next_column < width
                    and land[next_row, next_column]
                    and not seen[next_row, next_column]
                ):
                    seen[next_row, next_column] = True
                    stack.append((next_row, next_column))
        component_height = max_row - min_row + 1
        component_width = max_column - min_column + 1
        # Most components touching the mask edge are real coastlines. The game
        # atlas also contains clipped helper strips on an atlas edge, though,
        # which previously escaped cleanup and became dark green rectangles in
        # open ocean. Remove only tiny or very thin edge fragments.
        clipped_helper = (
            touches_edge
            and len(component) <= 100
            and (
                (component_height <= 16 and component_width <= 16)
                or (component_height <= 6 and component_width <= 40)
                or (component_width <= 6 and component_height <= 40)
            )
        )
        enclosed_helper = (
            not touches_edge
            and not too_large
            and component_height <= 40
            and component_width <= 40
        )
        if clipped_helper or enclosed_helper:
            for component_row, component_column in component:
                fill[component_row, component_column] = True
    if not fill.any():
        return mask
    return Image.fromarray(np.where(water | fill, 255, 0).astype(np.uint8), "L")


def restore_exact_atlas_water(capture: Image.Image, atlas: Image.Image) -> Image.Image:
    """Put exact game coastlines back into a real-3D top-down capture.

    Only pixels positively identified as water by the exact atlas are
    replaced.  Everything else--including real rendered buildings, roads,
    vegetation, rocks, islands, and terrain detail--remains from the 3D
    capture.  A small source-space blur removes atlas cell lines from open
    water before the mask is enlarged with an antialiased shoreline.
    """
    target = capture.convert("RGB")
    source = atlas.convert("RGB")
    source_mask = _exact_atlas_water_mask(source, include_shore_teal=True)
    # The exact atlas itself contains one-pixel cell guide seams.  Morphologic
    # closing fills those internal lines while retaining the outer coastline.
    source_mask = source_mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    source_mask = _remove_small_water_mask_holes(source_mask)
    exact_mask = source_mask.resize(target.size, Image.Resampling.LANCZOS)
    exact_mask = exact_mask.point(
        lambda value: 0 if value <= 24 else 255 if value >= 224 else int((value - 24) * 255 / 200)
    )
    capture_mask = _exact_atlas_water_mask(target)

    # Build one seamless high-resolution water field. Sampling only its colour
    # from the WebGL result retains the renderer's palette without inheriting
    # atlas guide lines, square water planes, rocks, or other baked objects.
    shared_water = ImageChops.multiply(exact_mask, capture_mask)
    mean = ImageStat.Stat(target, shared_water).mean
    if len(mean) != 3 or sum(mean) < 30:
        mean = (24.0, 105.0, 134.0)
    low = tuple(max(0, round(channel - 11)) for channel in mean)
    high = tuple(min(255, round(channel + 11)) for channel in mean)
    texture_size = (min(1024, target.width), min(768, target.height))
    water_noise = Image.effect_noise(texture_size, 18.0).filter(ImageFilter.GaussianBlur(1.0))
    water_art = ImageOps.colorize(water_noise, low, high).resize(
        target.size, Image.Resampling.BICUBIC
    )

    # Remove only broad, solid water spill outside the exact coastline. The
    # opening filter rejects isolated blue props/vegetation that share water's
    # colour but must remain detailed 3D objects.
    solid_capture_water = capture_mask.filter(ImageFilter.MinFilter(9)).filter(
        ImageFilter.MaxFilter(9)
    )
    extra_water = ImageChops.multiply(solid_capture_water, ImageChops.invert(exact_mask))
    exact_land = source.resize(target.size, Image.Resampling.LANCZOS)
    corrected_land = Image.composite(exact_land, target, extra_water)
    return Image.composite(water_art, corrected_land, exact_mask)


def friendly_name(value: str) -> str:
    value = re.sub(r"^(obj|blk|jnt|tool|hvs|poi|tile)_", "", value, flags=re.I)
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    return re.sub(r"\s+", " ", value).strip().title() or "Unknown"


def place_name(value: str) -> str:
    name = friendly_name(value)
    name = re.sub(r"\s+(?:New|Quest)$", "", name)
    name = re.sub(r"(?:\s+\d+)+$", "", name)
    name = re.sub(r"^Warehouse Exterior (\d)Floors$", r"Warehouse · \1 floors", name)
    name = name.replace("Minidungeon Overworld Entrance", "Dungeon entrance")
    name = name.replace("Survival Start Area Crashed Ship", "Crashed ship · starting area")
    return name


def item_group(item: CatalogItem) -> str:
    text = f"{item.internal_name} {item.title} {item.description}".casefold()
    if item.category.casefold() == "tool":
        return "Tools"
    if item.internal_name.casefold().startswith("blk_") or any(
        word in text for word in ("block", "concrete", "wood block", "metal block", "glass block")
    ):
        return "Blocks"
    if any(
        word in text
        for word in (
            "food", "drink", "seed", "ammo", "potato", "tomato", "carrot", "beet", "banana",
            "orange", "blueberry", "broccoli", "pineapple", "milk", "water", "gasoline", "battery",
            "fertilizer", "chemical", "component kit", "ember", "sunshake", "sandwich",
        )
    ):
        return "Consumable"
    if any(
        word in text
        for word in (
            "interactive", "controller", "engine", "seat", "switch", "sensor", "button", "bearing",
            "piston", "thruster", "radio", "light", "vacuum", "chest", "refinery", "craftbot",
            "cookbot", "dressbot", "resource collector", "water cannon", "mounted spudgun",
        )
    ):
        return "Interactive"
    return "Parts"


class EditorApi:
    """Thread-safe state and JSON-friendly operations for the local web UI."""

    def __init__(self, *, companion_mode: bool = False, enable_3d: bool = False) -> None:
        self.lock = threading.RLock()
        self.companion_mode = bool(companion_mode)
        self.enable_3d = bool(enable_3d)
        self.game_root = find_game_root()
        self.catalog = ItemCatalog()
        if self.game_root and not self.companion_mode:
            self.catalog.load(self.game_root)
        load_editor_catalogs = bool(self.game_root and not self.companion_mode)
        self.quest_catalog = load_quest_catalog(self.game_root, self.catalog) if load_editor_catalogs else []
        self.recipe_catalog = load_recipe_catalog(self.game_root, self.catalog) if load_editor_catalogs else []
        self.log_catalog = load_log_catalog(self.game_root) if load_editor_catalogs else []
        self.customization_catalog = load_customization_catalog(self.game_root) if load_editor_catalogs else []
        self.icon_atlas = self._load_icon_atlas() if not self.companion_mode else None
        self.weather_catalog = self._load_weather_catalog() if not self.companion_mode else {"conditions": {}, "forecasts": {}}
        self.beacon_visuals = self._load_beacon_visuals()
        self.clothing_icons = ClothingIconCache(self.game_root)
        self.tile_names = load_terrain_tile_names(self.game_root) if self.game_root else {}
        self.save: SaveData | None = None
        self._loaded_source_stamp: tuple[int, int] | None = None
        self.features: list[MapFeature] = []
        self.map_path: Path | None = None
        self.native_map_manifest: dict[str, Any] | None = None
        self._map_water_atlas: Image.Image | None = None
        self.terrain_asset_index: TerrainAssetIndex | None = None
        self.terrain_decal_index = (
            TerrainDecalIndex(self.game_root).build()
            if self.game_root and (not self.companion_mode or self.enable_3d)
            else None
        )
        self.terrain_3d_index: dict[str, Any] | None = None
        self.mesh_geometry = (
            MeshGeometryCache(self.game_root, CACHE_DIR)
            if self.game_root and (not self.companion_mode or self.enable_3d)
            else None
        )
        self.tile_terrain = TileTerrainCache()
        self.customization_profile: CustomizationUnlockProfile | None = None
        self.character_profile: CharacterProfile | None = None
        self.user_dir: Path | None = None
        self.backups = BackupManager()

    def dispatch(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, method.replace("-", "_"), None)
        if not callable(handler) or method.startswith("_"):
            raise ValueError(f"Unknown API method: {method}")
        with self.lock:
            result = handler(payload)
        return result if isinstance(result, dict) else {"result": result}

    def status(self, _payload: dict[str, Any]) -> dict[str, Any]:
        saves = []
        mode_labels = {"survival": "Survival", "creative": "Creative / Sandbox", "custom": "Custom Game"}
        for path, mode in scan_game_saves():
            stat = path.stat()
            saves.append({
                "path": str(path), "name": path.stem, "modified": stat.st_mtime, "size": stat.st_size,
                "mode": mode, "modeLabel": mode_labels.get(mode, "World"),
            })
        return {
            "version": __version__,
            "gameRoot": str(self.game_root) if self.game_root else None,
            "catalogCount": len(self.catalog.items),
            "catalogError": self.catalog.load_error or None,
            "saves": saves,
            "backupRoot": str(self.backups.root),
            "current": self._save_summary() if self.save else None,
            "installation": installation_diagnostics(),
        }

    def cache_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return cache_state(CACHE_DIR)

    def clear_map_cache(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        if payload.get("confirmation") != "CLEAR":
            raise ValueError("Cache clear confirmation was not supplied")
        self.map_path = None
        self.native_map_manifest = None
        self._map_water_atlas = None
        self.terrain_3d_index = None
        if self.mesh_geometry is not None:
            self.mesh_geometry.clear_memory()
        return clear_cache(CACHE_DIR)

    def _maintain_cache(self) -> dict[str, Any]:
        return trim_cache(CACHE_DIR)

    def load_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(payload.get("path") or "")).expanduser().resolve()
        self.save = SaveData(path)
        stat = path.stat()
        self._loaded_source_stamp = (stat.st_size, stat.st_mtime_ns)
        self.user_dir = self._user_dir_for_save(self.save)
        self.customization_profile = (
            None if self.companion_mode else self._load_customization_profile(self.save)
        )
        self.character_profile = (
            None if self.companion_mode else self._load_character_profile()
        )
        self.features = self._build_features(self.save)
        self.map_path = None
        self.native_map_manifest = None
        self._map_water_atlas = None
        self.terrain_3d_index = None
        return {"current": self._save_summary(), "containers": self._containers()}

    @staticmethod
    def _user_dir_for_save(save: SaveData) -> Path | None:
        steam_id = save.players[0].steam_id if save.players else 0
        if steam_id <= 0:
            return None
        user_dir = next((parent for parent in (save.path.parent, *save.path.parents) if parent.name == f"User_{steam_id}"), None)
        if user_dir is None:
            candidate = default_save_root() / f"User_{steam_id}"
            user_dir = candidate if candidate.is_dir() else None
        if user_dir is None:
            return None
        return user_dir.resolve()

    def _load_customization_profile(self, save: SaveData) -> CustomizationUnlockProfile | None:
        user_dir = self._user_dir_for_save(save)
        steam_id = save.players[0].steam_id if save.players else 0
        return CustomizationUnlockProfile.load(user_dir / "unlock", steam_id) if user_dir else None

    def _load_character_profile(self) -> CharacterProfile | None:
        path = self.user_dir / "character" if self.user_dir else None
        return CharacterProfile.load(path) if path and path.is_file() else None

    def _blueprints(self) -> BlueprintManager:
        if self.user_dir is None:
            raise RuntimeError("Load a world belonging to a detected Steam user first")
        return BlueprintManager(self.user_dir, self.backups.root)

    def _require_save(self) -> SaveData:
        if self.save is None:
            raise RuntimeError("Load a Scrap Mechanic world save first")
        return self.save

    def _save_summary(self) -> dict[str, Any]:
        save = self._require_save()
        summary = save.summary()
        summary["dirty"] = save.dirty
        summary["fileName"] = save.path.name
        summary["featureCount"] = len(self.features)
        return summary

    def _verify_source_stamp(self) -> None:
        stat = self._require_save().path.stat()
        if self._loaded_source_stamp is not None and self._loaded_source_stamp != (stat.st_size, stat.st_mtime_ns):
            raise RuntimeError("This save changed on disk after it was loaded. Reload it before saving.")

    def overview(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {"current": self._save_summary(), "containers": self._containers()}

    def _containers(self) -> list[dict[str, Any]]:
        save = self._require_save()
        inventory_id = save.inventory.container_id if save.inventory else None
        return [
            {
                "id": container.container_id,
                "slots": len(container.slots),
                "filled": sum(slot.quantity > 0 and slot.uuid != EMPTY_UUID for slot in container.slots),
                "inventory": container.container_id == inventory_id,
                "dirty": container.dirty,
            }
            for container in save.containers.values()
        ]

    def container(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        requested = payload.get("id")
        container = save.inventory if requested in (None, "", 0) else save.containers.get(int(requested))
        if container is None:
            raise ValueError("Container not found")
        return {
            "container": {
                "id": container.container_id,
                "count": len(container.slots),
                "inventory": container is save.inventory,
                "dirty": container.dirty,
            },
            "slots": [self._slot_data(slot) for slot in container.slots],
        }

    def _slot_data(self, slot: Any) -> dict[str, Any]:
        item = self.catalog.get(slot.uuid)
        return {
            "number": slot.number,
            "uuid": slot.uuid,
            "quantity": slot.quantity,
            "empty": slot.quantity == 0 or slot.uuid == EMPTY_UUID,
            "name": item.title if item else ("Empty" if slot.uuid == EMPTY_UUID else "Unknown item"),
            "category": item_group(item) if item else "Unknown",
            **self._icon_data(slot.uuid),
            "metadata": slot.metadata.hex(),
        }

    def catalog_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").casefold().split()
        group = str(payload.get("category") or "All")
        offset = max(0, int(payload.get("offset") or 0))
        limit = min(2000, max(1, int(payload.get("limit") or 180)))
        items = sorted(self.catalog.items.values(), key=lambda item: (item.title.casefold(), item.uuid))
        if group != "All":
            items = [item for item in items if item_group(item) == group]
        if query:
            items = [item for item in items if all(word in item.search_text for word in query)]
        total = len(items)
        page = items[offset : offset + limit]
        return {
            "items": [self._item_data(item) for item in page],
            "offset": offset,
            "loaded": len(page),
            "total": total,
            "nextOffset": offset + len(page) if offset + len(page) < total else None,
        }

    def _item_data(self, item: CatalogItem) -> dict[str, Any]:
        return {
            "uuid": item.uuid,
            "name": item.title,
            "description": item.description,
            "internalName": item.internal_name,
            "category": item_group(item),
            "sourceCategory": item.category,
            "stackSize": item.stack_size,
            **self._icon_data(item.uuid),
            "safeToCreate": item.category.casefold() != "tool",
        }

    @staticmethod
    def _load_icon_atlas() -> dict[str, Any] | None:
        image_path = ASSET_DIR / "item-icon-atlas.png"
        manifest_path = ASSET_DIR / "item-icon-atlas.json"
        if not image_path.is_file() or not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["url"] = image_path.resolve().as_uri()
            return manifest
        except (OSError, ValueError, TypeError):
            return None

    def _icon_data(self, uuid_text: str) -> dict[str, Any]:
        atlas = self.icon_atlas
        if not atlas:
            return {"icon": None, "iconPoint": None, "iconGrid": None}
        point = atlas.get("icons", {}).get(uuid_text.casefold())
        cell = int(atlas.get("cellPixels") or 0)
        if not point or cell <= 0:
            return {"icon": None, "iconPoint": None, "iconGrid": None}
        return {
            "icon": atlas["url"],
            "iconPoint": [int(point[0]) // cell, int(point[1]) // cell],
            "iconGrid": [int(atlas["columns"]), int(atlas["rows"])],
        }

    def _load_weather_catalog(self) -> dict[str, Any]:
        if not self.game_root:
            return {"conditions": {}, "forecasts": {}}
        try:
            data = load_game_json(self.game_root / "Data" / "Weather" / "main.weather")
            return data if isinstance(data, dict) else {"conditions": {}, "forecasts": {}}
        except (OSError, ValueError, TypeError):
            return {"conditions": {}, "forecasts": {}}

    def _load_beacon_visuals(self) -> dict[str, Any]:
        colors = ["#4F6CFF", "#AF7DFF", "#00FFFF", "#90FF78", "#FFD046", "#FFFFC0", "#FF6619", "#FF3737"]
        if not self.game_root:
            return {"atlas": None, "width": 3468, "height": 132, "frame": 132, "gap": 12, "colors": colors}
        atlas = self.game_root / "Data" / "Gui" / "Resolutions" / "3840x2160" / "Beacon" / "BeaconIcons@4K.png"
        return {
            "atlas": atlas.resolve().as_uri() if atlas.is_file() else None,
            "width": 3468,
            "height": 132,
            "frame": 132,
            "gap": 12,
            "colors": colors,
        }

    def edit_slot(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        container = save.containers.get(int(payload["containerId"]))
        if container is None:
            raise ValueError("Container not found")
        container.set_slot(int(payload["slot"]), str(payload.get("uuid") or EMPTY_UUID), int(payload.get("quantity", 1)), self.catalog)
        return {"slot": self._slot_data(container.slots[int(payload["slot"]) - 1]), "dirty": save.dirty}

    def clear_slot(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        payload.update({"uuid": EMPTY_UUID, "quantity": 0})
        return self.edit_slot(payload)

    def move_slot(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        container = save.containers.get(int(payload["containerId"]))
        if container is None:
            raise ValueError("Container not found")
        source, target = int(payload["source"]), int(payload["target"])
        if not 1 <= source <= len(container.slots) or not 1 <= target <= len(container.slots):
            raise ValueError("Invalid slot number")
        if source != target:
            a, b = container.slots[source - 1], container.slots[target - 1]
            a.uuid, b.uuid = b.uuid, a.uuid
            a.metadata, b.metadata = b.metadata, a.metadata
            a.quantity, b.quantity = b.quantity, a.quantity
            container.dirty = True
        return self.container({"id": container.container_id})

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.set_settings(int(payload["difficulty"]), int(payload["multiplayer"]), int(payload["physicsQuality"]))
        if any(key in payload for key in ("flags", "seed", "gametick")):
            save.set_game_fields(
                int(payload.get("flags", save.game["flags"])),
                int(payload.get("seed", save.game["seed"])),
                int(payload.get("gametick", save.game["gametick"])),
            )
        return {"current": self._save_summary()}

    def world_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        players = []
        for index, block in enumerate(save.player_states):
            value = block.value if isinstance(block.value, dict) else {}
            stats = value.get("stats") if isinstance(value.get("stats"), dict) else {}
            location = save.players[index] if index < len(save.players) else None
            players.append(
                {
                    "index": index,
                    "playerId": int.from_bytes(block.key[:4], "little") if len(block.key) == 4 else index + 1,
                    "steamId": location.steam_id if location else None,
                    "inventoryContainerId": location.inventory_container_id if location else None,
                    "carryContainerId": location.carry_container_id if location else None,
                    "hp": float(stats.get("hp", 0)),
                    "maxHp": float(stats.get("maxhp", 100)),
                    "breath": float(stats.get("breath", 100)),
                    "maxBreath": float(stats.get("maxbreath", 100)),
                    "conscious": bool(value.get("isConscious", True)),
                    "revivalItem": bool(value.get("hasRevivalItem", False)),
                    "perks": sorted(
                        int(key) for key, enabled in stats.get("perks", {}).items()
                        if enabled and str(key).isdigit() and 1 <= int(key) <= 4
                    ) if isinstance(stats.get("perks"), dict) else [],
                    "inOil": bool(value.get("inOil", False)),
                    "inChemical": bool(value.get("inChemical", False)),
                    "respawnBlocked": bool(value.get("respawnBlocked", False)),
                    "position": [location.x, location.y, location.z] if location else None,
                    "worldId": location.world_id if location else None,
                    "respawnPoints": save.respawn_points(index),
                    "bags": save.player_bags(index),
                }
            )
        progression = save.progression_state.value if save.progression_state else {}
        if not isinstance(progression, dict):
            progression = {}
        quest = save.quest_state.value if save.quest_state else {}
        if not isinstance(quest, dict):
            quest = {}
        completed = quest.get("completedQuests")
        completed_names = completed.values if isinstance(completed, LuaArray) else []
        active = quest.get("activeQuests")
        active_names = list(active) if isinstance(active, dict) else []
        time_data = save.time_state.value if save.time_state else {}
        if not isinstance(time_data, dict):
            time_data = {}
        time_of_day = float(time_data.get("timeOfDay", 0))
        return {
            "players": players,
            "raids": self._raid_data(save),
            "progression": {
                "available": bool(save.progression_state),
                "seedTier": int(progression.get("seedTier", 0)),
                "vaultTotal": int(progression.get("vaultTotal", 0)),
                "consoleUnlocked": bool(progression.get("warehouseConsoleUnlocked", False)),
                "beaconCrafted": bool(progression.get("hasCraftedBeacon", False)),
                "completedGrowlabs": len(progression.get("completedGrowlabs", {}))
                if isinstance(progression.get("completedGrowlabs"), dict) else 0,
            },
            "quests": {
                "active": [{"id": str(name), "name": friendly_name(str(name))} for name in active_names],
                "completed": [{"id": str(name), "name": friendly_name(str(name))} for name in completed_names],
            },
            "time": {
                "available": bool(save.time_state),
                "day": int(time_of_day),
                "hour": (time_of_day % 1.0) * 24.0,
                "progresses": bool(time_data.get("timeProgress", True)),
            },
            "beaconCount": len(save.beacon_state.value.get("beacons", {}))
            if save.beacon_state and isinstance(save.beacon_state.value, dict)
            and isinstance(save.beacon_state.value.get("beacons"), dict) else 0,
            "beacons": save.beacon_data(),
            "beaconVisuals": self.beacon_visuals,
            "deathMarkers": save.death_marker_count(),
            "aggroCreations": bool(
                save.unit_manager_state.value.get("hostSettings", {}).get("aggroCreations", False)
            ) if save.unit_manager_state and isinstance(save.unit_manager_state.value, dict)
            and isinstance(save.unit_manager_state.value.get("hostSettings"), dict) else False,
        }

    def world_services_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        weather_root = save.weather_state.value if save.weather_state and isinstance(save.weather_state.value, dict) else {}
        tornado_root = save.tornado_state.value if save.tornado_state and isinstance(save.tornado_state.value, dict) else {}
        queue = save.weather_queue()
        condition_data = self.weather_catalog.get("conditions")
        forecast_data = self.weather_catalog.get("forecasts")
        conditions = []
        if isinstance(condition_data, dict):
            for name, definition in condition_data.items():
                definition = definition if isinstance(definition, dict) else {}
                conditions.append({
                    "id": str(name),
                    "name": friendly_name(str(name)),
                    "rain": float(definition.get("rainAmount", 0)),
                    "wind": float(definition.get("wind", 0)),
                    "extinguishesFires": bool(definition.get("extinguishFires", False)),
                })
        forecasts = []
        if isinstance(forecast_data, dict):
            for name, definition in forecast_data.items():
                definition = definition if isinstance(definition, dict) else {}
                timeline = definition.get("timeline") if isinstance(definition.get("timeline"), list) else []
                forecasts.append({
                    "id": str(name),
                    "name": friendly_name(str(name)),
                    "timeline": [str(entry.get("condition")) for entry in timeline if isinstance(entry, dict) and entry.get("condition")],
                })
        saved_tornadoes = tornado_root.get("tornadoSaveData")
        visited_cells = tornado_root.get("visitedTornadoCells")
        return {
            "available": bool(save.weather_state or save.tornado_state or save.world_manager_state),
            "weather": {
                "available": bool(save.weather_state),
                "queue": queue,
                "days": [queue[index:index + 4] for index in range(0, len(queue), 4)],
                "todOffset": float(weather_root.get("todOffset", 0)) if weather_root else 0,
                "conditions": conditions,
                "forecasts": forecasts,
            },
            "tornadoes": {
                "available": bool(save.tornado_state),
                "saved": len(saved_tornadoes) if isinstance(saved_tornadoes, dict) else 0,
                "visitedCells": len(visited_cells) if isinstance(visited_cells, dict) else 0,
                "nextId": int(tornado_root.get("tornadoIdCounter", 1)) if tornado_root else 1,
            },
            "worlds": save.registered_worlds(),
        }

    def set_weather_condition(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        name = str(payload.get("condition") or "")
        valid = self.weather_catalog.get("conditions")
        if not isinstance(valid, dict) or name not in valid:
            raise ValueError("Choose a weather condition from the installed game data")
        save.set_weather_condition(name)
        return {"state": self.world_services_state({}), "dirty": save.dirty}

    def set_weather_forecast(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        name = str(payload.get("forecast") or "")
        forecasts = self.weather_catalog.get("forecasts")
        definition = forecasts.get(name) if isinstance(forecasts, dict) else None
        timeline = definition.get("timeline") if isinstance(definition, dict) else None
        if not isinstance(timeline, list):
            raise ValueError("Choose a forecast from the installed game data")
        save.set_weather_forecast(
            str(entry.get("condition")) for entry in timeline if isinstance(entry, dict) and entry.get("condition")
        )
        return {"state": self.world_services_state({}), "dirty": save.dirty}

    def set_weather_period(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        name = str(payload.get("condition") or "")
        valid = self.weather_catalog.get("conditions")
        if not isinstance(valid, dict) or name not in valid:
            raise ValueError("Choose a weather condition from the installed game data")
        save.set_weather_period(int(payload.get("index", -1)), name)
        return {"state": self.world_services_state({}), "dirty": save.dirty}

    def append_weather_day(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        valid = self.weather_catalog.get("conditions")
        queue = save.weather_queue()
        name = str(payload.get("condition") or (queue[-1] if queue else "Clear"))
        if not isinstance(valid, dict) or name not in valid:
            raise ValueError("Choose a weather condition from the installed game data")
        days = save.append_weather_day(name)
        return {"days": days, "state": self.world_services_state({}), "dirty": save.dirty}

    def advance_weather_day(self, _payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        removed = save.advance_weather_day()
        return {"removed": removed, "state": self.world_services_state({}), "dirty": save.dirty}

    def reset_tornado_service(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmation") != "RESET TORNADOES":
            raise ValueError("Tornado reset confirmation was not supplied")
        save = self._require_save()
        cleared = save.reset_tornado_service()
        return {"cleared": cleared, "state": self.world_services_state({}), "dirty": save.dirty}

    @staticmethod
    def _raid_data(save: SaveData) -> list[dict[str, Any]]:
        if save.raid_state is None or not isinstance(save.raid_state.value, dict):
            return []
        root = save.raid_state.value.get("worldRaids")
        if isinstance(root, LuaArray):
            worlds = [(root.offset + index, value) for index, value in enumerate(root.values)]
        elif isinstance(root, dict):
            worlds = list(root.items())
        else:
            worlds = []
        result = []
        for world_id, raids in worlds:
            if not isinstance(raids, dict):
                continue
            for key, raid in raids.items():
                raid = raid if isinstance(raid, dict) else {}
                center = raid.get("center")
                result.append(
                    {
                        "key": str(key),
                        "worldId": int(world_id),
                        "level": int(raid.get("level", 0)),
                        "value": float(raid.get("value", 0)),
                        "position": list(center[1:4]) if isinstance(center, tuple) and center[:1] == ("vec3",) else None,
                        "attackTick": raid.get("attackTick"),
                    }
                )
        return result

    def update_player(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.set_player_status(
            int(payload.get("index", 0)),
            float(payload["hp"]),
            float(payload["maxHp"]),
            float(payload["breath"]),
            float(payload["maxBreath"]),
            bool(payload.get("conscious")),
            bool(payload.get("revivalItem")),
        )
        return {"state": self.world_state({}), "dirty": save.dirty}

    def update_player_perks(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.set_player_perks(int(payload.get("index", 0)), payload.get("perks") or [])
        return {"state": self.world_state({}), "dirty": save.dirty}

    def repair_player(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.repair_player_flags(int(payload.get("index", 0)))
        return {"state": self.world_state({}), "dirty": save.dirty}

    def update_creation_aggro(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.set_creation_aggro(bool(payload.get("enabled")))
        return {"state": self.world_state({}), "dirty": save.dirty}

    def teleport_player(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.teleport_player(
            int(payload.get("index", 0)), float(payload["x"]), float(payload["y"]), float(payload["z"])
        )
        self.features = self._build_features(save)
        return {"state": self.world_state({}), "dirty": save.dirty}

    def teleport_to_respawn(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.teleport_to_respawn(int(payload.get("index", 0)), str(payload.get("kind") or "temporary"))
        self.features = self._build_features(save)
        return {"state": self.world_state({}), "dirty": save.dirty}

    def teleport_to_bag(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.teleport_to_bag(int(payload.get("index", 0)), int(payload.get("bagIndex", 0)))
        self.features = self._build_features(save)
        return {"state": self.world_state({}), "dirty": save.dirty}

    def reset_temporary_bed(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        changed = save.reset_temporary_bed(int(payload.get("index", 0)))
        return {"changed": changed, "state": self.world_state({}), "dirty": save.dirty}

    def clear_death_markers(self, _payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        removed = save.clear_death_markers()
        return {"removed": removed, "state": self.world_state({}), "dirty": save.dirty}

    def update_beacon(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.set_beacon_icon(str(payload.get("id") or ""), int(payload["iconIndex"]), int(payload["colorIndex"]))
        return {"state": self.world_state({}), "dirty": save.dirty}

    def clear_raids(self, _payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        removed = save.clear_raids()
        return {"removed": removed, "state": self.world_state({}), "dirty": save.dirty}

    def update_progression(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.set_progression(
            int(payload["seedTier"]),
            int(payload["vaultTotal"]),
            bool(payload.get("consoleUnlocked")),
            bool(payload.get("beaconCrafted")),
        )
        return {"state": self.world_state({}), "dirty": save.dirty}

    def update_world_time(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        save.set_world_time(int(payload["day"]), float(payload["hour"]), bool(payload.get("progresses")))
        return {"state": self.world_state({}), "dirty": save.dirty}

    def unlocks_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        active = set(save.active_quest_names())
        completed = set(save.completed_quest_names())
        unlocked_recipes = save.unlocked_recipe_ids()
        unlocked_logs = save.unlocked_log_ids()
        profile = self.customization_profile
        unlocked_clothes = set(profile.uuids) if profile else set()
        quests = []
        for definition in self.quest_catalog:
            quest = dict(definition)
            quest["status"] = "completed" if quest["id"] in completed else "active" if quest["id"] in active else "not-started"
            quests.append(quest)
        recipes = []
        for definition in self.recipe_catalog:
            recipe = dict(definition)
            recipe["unlocked"] = recipe["uuid"] in unlocked_recipes
            recipe.update(self._icon_data(recipe["uuid"]))
            recipes.append(recipe)
        logs = [{**definition, "unlocked": definition["uuid"] in unlocked_logs} for definition in self.log_catalog]
        gender = self.character_profile.gender if self.character_profile else 0
        clothes = [
            {
                **definition,
                "icon": self.clothing_icons.icon_url(definition["uuid"], gender),
                "unlocked": definition["uuid"] in unlocked_clothes,
            }
            for definition in self.customization_catalog
        ]
        return {
            "capabilities": save.summary()["capabilities"],
            "quests": quests,
            "recipes": recipes,
            "logs": logs,
            "clothing": clothes,
            "clothingProfile": {
                "available": profile is not None,
                "path": str(profile.path) if profile else None,
                "steamId": profile.steam_id if profile else None,
                "dirty": profile.dirty if profile else False,
                "unlocked": len(profile.uuids) if profile else 0,
            },
        }

    def _quest_definition(self, quest_id: str) -> dict[str, Any]:
        definition = next((item for item in self.quest_catalog if item["id"] == quest_id), None)
        if definition is None:
            raise ValueError("Quest is not present in the installed game data")
        return definition

    def _unlock_quest_schematic_rewards(self, definitions: list[dict[str, Any]]) -> int:
        recipe_ids = {
            reward["uuid"]
            for definition in definitions
            for reward in definition.get("rewards", [])
            if reward.get("type") == "schematic" and reward.get("uuid")
        }
        return self._require_save().unlock_recipes(recipe_ids) if recipe_ids else 0

    def complete_quest(self, payload: dict[str, Any]) -> dict[str, Any]:
        definition = self._quest_definition(str(payload.get("id") or ""))
        changed = self._require_save().complete_quest(definition["id"])
        rewards = self._unlock_quest_schematic_rewards([definition]) if changed else 0
        return {"changed": changed, "schematicsUnlocked": rewards, "state": self.unlocks_state({})}

    def complete_all_quests(self, _payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        changed = save.complete_all_quests(definition["id"] for definition in self.quest_catalog)
        rewards = self._unlock_quest_schematic_rewards(self.quest_catalog)
        return {"changed": changed, "schematicsUnlocked": rewards, "state": self.unlocks_state({})}

    def unlock_recipe(self, payload: dict[str, Any]) -> dict[str, Any]:
        recipe_id = str(payload.get("id") or "").casefold()
        if not any(item["uuid"] == recipe_id for item in self.recipe_catalog):
            raise ValueError("Recipe is not present in the installed Craftbot data")
        changed = self._require_save().unlock_recipes([recipe_id])
        return {"changed": changed, "state": self.unlocks_state({})}

    def unlock_all_recipes(self, _payload: dict[str, Any]) -> dict[str, Any]:
        changed = self._require_save().unlock_recipes(item["uuid"] for item in self.recipe_catalog)
        return {"changed": changed, "state": self.unlocks_state({})}

    def unlock_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        log_id = str(payload.get("id") or "").casefold()
        if not any(item["uuid"] == log_id for item in self.log_catalog):
            raise ValueError("Log entry is not present in the installed game data")
        changed = self._require_save().unlock_logs([log_id])
        return {"changed": changed, "state": self.unlocks_state({})}

    def unlock_all_logs(self, _payload: dict[str, Any]) -> dict[str, Any]:
        changed = self._require_save().unlock_logs(item["uuid"] for item in self.log_catalog)
        return {"changed": changed, "state": self.unlocks_state({})}

    def stage_clothing(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.customization_profile
        if profile is None:
            raise RuntimeError("The Steam user's clothing unlock profile was not found")
        clothing_id = str(payload.get("id") or "").casefold()
        if not any(item["uuid"] == clothing_id for item in self.customization_catalog):
            raise ValueError("Clothing item is not present in the installed game data")
        changed = profile.unlock(clothing_id)
        return {"changed": changed, "state": self.unlocks_state({})}

    def stage_lock_clothing(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.customization_profile
        if profile is None:
            raise RuntimeError("The Steam user's clothing unlock profile was not found")
        clothing_id = str(payload.get("id") or "").casefold()
        if not any(item["uuid"] == clothing_id for item in self.customization_catalog):
            raise ValueError("Clothing item is not present in the installed game data")
        changed = profile.lock(clothing_id)
        return {"changed": changed, "state": self.unlocks_state({})}

    def reset_staged_clothing(self, _payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.customization_profile
        if profile is None:
            raise RuntimeError("The Steam user's clothing unlock profile was not found")
        changed = profile.reset_staged()
        return {"changed": changed, "state": self.unlocks_state({})}

    def stage_all_clothing(self, _payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.customization_profile
        if profile is None:
            raise RuntimeError("The Steam user's clothing unlock profile was not found")
        changed = sum(1 for item in self.customization_catalog if profile.unlock(item["uuid"]))
        return {"changed": changed, "state": self.unlocks_state({})}

    def save_clothing(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        profile = self.customization_profile
        if profile is None:
            raise RuntimeError("The Steam user's clothing unlock profile was not found")
        if payload.get("confirmation") != "APPLY CLOTHING":
            raise ValueError("Clothing confirmation was not supplied")
        backup = self.backups.archive(
            profile.path, category="Clothing Profile", kind="automatic", label="Before clothing unlock change"
        ) if profile.dirty and profile.path.is_file() else None
        profile.save(create_adjacent_backup=False)
        return {
            "path": str(profile.path),
            "backup": str(backup["path"]) if backup else None,
            "state": self.unlocks_state({}),
        }

    def character_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.character_profile
        unlock = self.customization_profile
        if profile is None:
            return {"available": False, "error": "The global character profile was not found", "slots": []}
        catalog = {item["uuid"]: item for item in self.customization_catalog}
        unlocked = set(unlock.uuids) if unlock else set()
        category_by_slot = {
            "Face": "Face", "Hair": "Hair", "Facial hair": "Facial hair", "Torso": "Torso",
            "Gloves": "Gloves", "Shoes": "Shoes", "Pants": "Legs", "Hat": "Hat", "Backpack": "Backpack",
        }
        slots = []
        for index, slot_name in enumerate(CHARACTER_SLOTS):
            selected = profile.options[index]
            options = []
            for definition in self.customization_catalog:
                if definition["category"] != category_by_slot[slot_name]:
                    continue
                if index >= 3 and definition["uuid"] not in unlocked and definition["uuid"] != selected:
                    continue
                options.append({
                    **definition,
                    "icon": self.clothing_icons.icon_url(definition["uuid"], profile.gender),
                    "unlocked": definition["uuid"] in unlocked,
                })
            current = catalog.get(selected or "")
            slots.append({
                "index": index,
                "name": slot_name,
                "uuid": selected,
                "selectedName": current["name"] if current else ("None" if not selected else selected),
                "selectedInternal": current.get("internal", "") if current else "",
                "icon": self.clothing_icons.icon_url(selected, profile.gender),
                "palette": profile.palettes[index],
                "options": options,
            })
        return {
            "available": True,
            "path": str(profile.path),
            "gender": profile.gender,
            "dirty": profile.dirty,
            "slots": slots,
        }

    def update_character(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.character_profile
        if profile is None:
            raise RuntimeError("The global character profile was not found")
        values = payload.get("slots")
        if not isinstance(values, list) or len(values) != len(CHARACTER_SLOTS):
            raise ValueError("All nine character slots must be supplied")
        allowed_categories = ("Face", "Hair", "Facial hair", "Torso", "Gloves", "Shoes", "Legs", "Hat", "Backpack")
        catalog = {item["uuid"]: item for item in self.customization_catalog}
        unlocked = set(self.customization_profile.uuids) if self.customization_profile else set()
        profile.set_gender(int(payload.get("gender", profile.gender)))
        for index, value in enumerate(values):
            option = str(value.get("uuid") or "") or None
            if option:
                normalized = str(uuid.UUID(option))
                definition = catalog.get(normalized)
                if definition is None or (
                    definition["category"] != allowed_categories[index]
                    and normalized != profile.options[index]
                ):
                    raise ValueError(f"Selected item does not belong in the {CHARACTER_SLOTS[index]} slot")
                if index >= 3 and normalized not in unlocked and normalized != profile.options[index]:
                    raise ValueError("Unlock this clothing item before equipping it")
                option = normalized
            profile.set_option(index, option, int(value.get("palette", 0)))
        return {"state": self.character_state({})}

    def save_character(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        profile = self.character_profile
        if profile is None:
            raise RuntimeError("The global character profile was not found")
        if payload.get("confirmation") != "APPLY CHARACTER":
            raise ValueError("Character confirmation was not supplied")
        backup = self.backups.archive(
            profile.path, category="Character Profile", kind="automatic", label="Before character appearance change"
        ) if profile.dirty else None
        if profile.dirty:
            profile.save()
        return {"backup": backup, "state": self.character_state({})}

    def blueprint_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        manager = self._blueprints()
        return {
            "root": str(manager.root),
            "archiveRoot": str(manager.archive_root),
            "blueprints": manager.list(),
        }

    def rename_blueprint(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        if payload.get("confirmation") != "EDIT BLUEPRINT":
            raise ValueError("Blueprint edit confirmation was not supplied")
        backup = self._blueprints().rename(
            str(payload.get("id") or ""), str(payload.get("name") or ""), str(payload.get("description") or "")
        )
        return {"backup": str(backup), "state": self.blueprint_state({})}

    def duplicate_blueprint(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        if payload.get("confirmation") != "DUPLICATE BLUEPRINT":
            raise ValueError("Blueprint duplicate confirmation was not supplied")
        new_id = self._blueprints().duplicate(str(payload.get("id") or ""), str(payload.get("name") or "") or None)
        return {"id": new_id, "state": self.blueprint_state({})}

    def export_blueprint(self, payload: dict[str, Any]) -> dict[str, Any]:
        blueprint_id = str(payload.get("id") or "")
        entry = next((item for item in self._blueprints().list() if item["id"] == blueprint_id), None)
        if entry is None:
            raise ValueError("Local blueprint was not found")
        path = self._blueprints().export(blueprint_id, entry["name"])
        return {"path": str(path), "state": self.blueprint_state({})}

    def import_blueprint(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        if payload.get("confirmation") != "IMPORT BLUEPRINT":
            raise ValueError("Blueprint import confirmation was not supplied")
        new_id = self._blueprints().import_archive(Path(str(payload.get("path") or "")))
        return {"id": new_id, "state": self.blueprint_state({})}

    def save_copy(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        save = self._require_save()
        self._verify_source_stamp()
        record = self.backups.archive(
            save.path,
            category=save.summary()["mode_label"],
            kind="edited-copy",
            label=f"Edited copy · {save.path.stem}",
            producer=lambda destination: save.save_copy(destination),
        )
        return {"path": record["path"], "backup": record, "backupRoot": str(self.backups.root)}

    def create_backup(self, _payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        save = self._require_save()
        record = self.backups.archive(
            save.path,
            category=save.summary()["mode_label"],
            kind="manual",
            label=save.path.stem,
        )
        return {"backup": record, "state": self.backup_state({})}

    def backup_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "root": str(self.backups.root),
            "backups": self.backups.list(),
        }

    def restore_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        if payload.get("confirmation") != "RESTORE":
            raise ValueError("Restore confirmation was not supplied")
        record, safety = self.backups.restore(str(payload.get("id") or ""))
        target = Path(record["sourcePath"]).resolve()
        current = None
        containers = None
        if self.save and target == self.save.path:
            self.save = SaveData(target)
            stat = target.stat()
            self._loaded_source_stamp = (stat.st_size, stat.st_mtime_ns)
            self.user_dir = self._user_dir_for_save(self.save)
            self.customization_profile = self._load_customization_profile(self.save)
            self.character_profile = self._load_character_profile()
            self.features = self._build_features(self.save)
            self.map_path = None
            self.native_map_manifest = None
            self._map_water_atlas = None
            self.terrain_3d_index = None
            current = self._save_summary()
            containers = self._containers()
        elif self.character_profile and target == self.character_profile.path:
            self.character_profile = self._load_character_profile()
        elif self.customization_profile and target == self.customization_profile.path:
            self.customization_profile = self._load_customization_profile(self._require_save())
        return {
            "restored": record, "safetyBackup": safety, "state": self.backup_state({}),
            "current": current, "containers": containers,
        }

    def overwrite(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_game_closed()
        save = self._require_save()
        if payload.get("confirmation") != "OVERWRITE":
            raise ValueError("Overwrite confirmation was not supplied")
        self._verify_source_stamp()
        backup = self.backups.archive(
            save.path,
            category=save.summary()["mode_label"],
            kind="automatic",
            label=f"Before save · {save.path.stem}",
        )
        written, _adjacent = save.save_copy(save.path, overwrite_source=True, create_adjacent_backup=False)
        self.save = SaveData(written)
        stat = written.stat()
        self._loaded_source_stamp = (stat.st_size, stat.st_mtime_ns)
        self.user_dir = self._user_dir_for_save(self.save)
        self.customization_profile = self._load_customization_profile(self.save)
        self.character_profile = self._load_character_profile()
        self.features = self._build_features(self.save)
        self.map_path = None
        self.native_map_manifest = None
        self._map_water_atlas = None
        self.terrain_3d_index = None
        return {
            "path": str(written),
            "backup": backup,
            "current": self._save_summary(),
            "containers": self._containers(),
        }

    def map_features(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").casefold().split()
        kind = str(payload.get("kind") or "Places")
        world_id = int(payload.get("worldId") or 1)
        features = [feature for feature in self.features if feature.world_id == world_id]
        if kind == "Places":
            features = [
                feature for feature in features
                if feature.kind not in {"Enemy", "Creation"}
            ]
        elif kind != "All":
            features = [feature for feature in features if feature.kind == kind]
        if query:
            features = [feature for feature in features if all(word in feature.search_text for word in query)]
        features.sort(key=lambda feature: (feature.label.casefold(), feature.x, feature.y))
        total = len(features)
        limit = min(2000, max(1, int(payload.get("limit") or 500)))
        rows: list[dict[str, Any]] = []
        beacon_data = {
            str(row.get("id")): row for row in self._require_save().beacon_data()
        }
        for feature in features[:limit]:
            row = asdict(feature)
            if feature.key.startswith("terrain-"):
                row["label"] = place_name(feature.label)
            row["preview"] = self._terrain_preview_uri(feature)
            terrain_center = 32.0 if feature.key.startswith("terrain-") else 0.0
            row["worldX"] = float(feature.x) * 64.0 + terrain_center
            row["worldY"] = float(feature.y) * 64.0 + terrain_center
            kind_text = feature.kind.casefold()
            label_text = feature.label.casefold()
            classification = f"{kind_text} {label_text}"
            if feature.kind == "Player":
                row["category"] = "player"
            elif feature.key.startswith("beacon-"):
                row["category"] = "beacons"
                beacon = beacon_data.get(feature.key.removeprefix("beacon-"), {})
                row["iconIndex"] = int(beacon.get("iconIndex") or 0)
                row["colorIndex"] = int(beacon.get("colorIndex") or 1)
                row["beaconAtlas"] = self.beacon_visuals
            elif "warehouse" in classification:
                row["category"] = "warehouses"
            elif "packing" in classification:
                row["category"] = "packing"
            elif "station" in classification:
                row["category"] = "stations"
            elif feature.kind == "Lab / dungeon":
                row["category"] = "labs"
            elif "quest" in classification:
                row["category"] = "quests"
            else:
                row["category"] = "landmarks"
            rows.append(row)
        return {"features": rows, "total": total}

    def _terrain_preview_uri(self, feature: MapFeature) -> str:
        """Return the original crisp isometric game preview for a map place."""
        path = self._terrain_preview_path(feature.uuid)
        return path.resolve().as_uri() if path else ""

    def _terrain_preview_path(self, item_uuid: str) -> Path | None:
        if not self.game_root or not item_uuid:
            return None
        item_uuid = item_uuid.casefold()
        metadata = self.tile_names.get(item_uuid, {})
        category = str(metadata.get("category") or "").strip()
        root = self.game_root / "Survival" / "Terrain" / "Tiles"
        candidates = [root / f"{item_uuid}.png"]
        if category and category.casefold() not in {"tile", "tiles", "terrain"}:
            candidates.insert(0, root / category / f"{item_uuid}.png")
        return next((path for path in candidates if path.is_file()), None)

    def _native_preview_uri(self, item_uuid: str) -> str:
        """Cache the installed preview with only its flat capture backdrop removed."""
        source = self._terrain_preview_path(item_uuid)
        if source is None:
            return ""
        metadata = self.tile_names.get(item_uuid.casefold(), {})
        largest_side = max(
            1,
            int(metadata.get("cellsX") or 1),
            int(metadata.get("cellsY") or 1),
        )
        # Every installed terrain preview is only 220x150, including enormous
        # 8x8 locations.  Those sprites must cover a much larger footprint on
        # the native map, so prepare a carefully sharpened 4x cache instead of
        # asking Chromium to perform a soft bilinear enlargement every frame.
        upscale = 4 if largest_side >= 4 else 1
        directory = CACHE_DIR / "native-previews"
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / (
            f"{item_uuid}-alpha-r{NATIVE_PREVIEW_REVISION}-{upscale}x.png"
        )
        if not output.is_file() or output.stat().st_mtime_ns < source.stat().st_mtime_ns:
            with Image.open(source) as preview_file:
                preview = preview_file.convert("RGBA")
            background = Image.new("RGBA", preview.size, preview.getpixel((0, 0)))
            difference = ImageChops.difference(preview, background).convert("RGB")
            red, green, blue = difference.split()
            distance = ImageChops.lighter(ImageChops.lighter(red, green), blue)
            # The capture canvas is uniform. A short alpha ramp removes its
            # antialiased fringe without cutting the shadows and artwork away.
            alpha = distance.point(
                lambda value: 0 if value <= 2 else 255 if value >= 10 else (value - 2) * 32
            )
            preview.putalpha(alpha)
            if upscale > 1:
                preview = preview.resize(
                    (preview.width * upscale, preview.height * upscale),
                    Image.Resampling.LANCZOS,
                )
                sharpened = preview.convert("RGB").filter(
                    ImageFilter.UnsharpMask(radius=1.2, percent=135, threshold=3)
                )
                sharpened.putalpha(preview.getchannel("A"))
                preview = sharpened
            preview.save(output, format="PNG", compress_level=6)
        return output.resolve().as_uri()

    @staticmethod
    def _placement_origin(cell: Any, size: int) -> tuple[int, int]:
        rotation = int(cell.rotation) % 4
        x_offset, y_offset = int(cell.x_offset), int(cell.y_offset)
        if rotation == 0:
            local_x, local_y = x_offset, y_offset
        elif rotation == 1:
            local_x, local_y = size - 1 - y_offset, x_offset
        elif rotation == 2:
            local_x, local_y = size - 1 - x_offset, size - 1 - y_offset
        else:
            local_x, local_y = y_offset, size - 1 - x_offset
        return int(cell.x) - local_x, int(cell.y) - local_y

    def map_native_previews(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Describe a depth-ordered map made from every native terrain preview."""
        if self.native_map_manifest is not None:
            return self.native_map_manifest
        save = self._require_save()
        grid = save.terrain_grid
        if not grid or not self.game_root:
            raise RuntimeError(save.terrain_error or "Native terrain previews are unavailable")

        assets: list[dict[str, Any]] = []
        asset_indexes: dict[str, int] = {}
        grouped: dict[tuple[Any, ...], list[Any]] = {}
        for cell in grid.cells.values():
            key = (
                cell.uuid,
                ("group", cell.group_id) if cell.group_id else ("cell", cell.x, cell.y),
            )
            grouped.setdefault(key, []).append(cell)

        placements: list[dict[str, Any]] = []
        for (item_uuid, _group_key), cells in grouped.items():
            metadata = self.tile_names.get(item_uuid, {})
            size_x = max(1, int(metadata.get("cellsX") or 1))
            size_y = max(1, int(metadata.get("cellsY") or 1))
            if size_x != size_y:
                continue
            asset_index = asset_indexes.get(item_uuid)
            if asset_index is None:
                uri = self._native_preview_uri(item_uuid)
                if not uri:
                    asset_indexes[item_uuid] = -1
                    continue
                asset_index = len(assets)
                asset_indexes[item_uuid] = asset_index
                assets.append({
                    "uuid": item_uuid,
                    "image": uri,
                    "sourceWidth": 220,
                    "sourceHeight": 150,
                    "enhanced": max(size_x, size_y) >= 4,
                })
            elif asset_index < 0:
                continue
            first = cells[0]
            origin_x, origin_y = self._placement_origin(first, size_x)
            center_x = origin_x + size_x / 2
            center_y = origin_y + size_y / 2
            placements.append({
                "asset": asset_index,
                "x": center_x,
                "y": center_y,
                "cells": size_x,
                "rotation": int(first.rotation) % 4,
                "depth": center_x + center_y,
            })

        placements.sort(key=lambda item: (float(item["depth"]), float(item["x"])))
        x_min, x_max, y_min, y_max = grid.visible_bounds
        half_width = NATIVE_ISO_CELL_WIDTH / 2
        half_height = NATIVE_ISO_CELL_HEIGHT / 2
        corners = (
            (x_min, y_min), (x_min, y_max + 1),
            (x_max + 1, y_min), (x_max + 1, y_max + 1),
        )
        projected_x = [(x - y) * half_width for x, y in corners]
        projected_y = [(x + y) * half_height for x, y in corners]
        margin = 320
        minimum_x, minimum_y = min(projected_x), min(projected_y)
        maximum_x, maximum_y = max(projected_x), max(projected_y)
        self.native_map_manifest = {
            "available": True,
            "width": maximum_x - minimum_x + margin * 2,
            "height": maximum_y - minimum_y + margin * 2,
            "originX": -minimum_x + margin,
            "originY": -minimum_y + margin,
            "cellWidth": NATIVE_ISO_CELL_WIDTH,
            "cellHeight": NATIVE_ISO_CELL_HEIGHT,
            "assets": assets,
            "placements": placements,
        }
        return self.native_map_manifest

    def map_3d_index(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Return a lightweight, streamable index for the full mesh renderer."""
        if self.terrain_3d_index is not None:
            return self.terrain_3d_index
        save = self._require_save()
        grid = save.terrain_grid
        if not grid or not self.game_root:
            raise RuntimeError(save.terrain_error or "3D terrain data is unavailable")

        grouped: dict[tuple[Any, ...], list[Any]] = {}
        for cell in grid.cells.values():
            key = (
                cell.uuid,
                ("group", cell.group_id) if cell.group_id else ("cell", cell.x, cell.y),
            )
            grouped.setdefault(key, []).append(cell)

        scenes: list[dict[str, Any]] = []
        scene_indexes: dict[str, int] = {}
        placements: list[dict[str, Any]] = []
        for (item_uuid, _group_key), cells in grouped.items():
            metadata = self.tile_names.get(item_uuid, {})
            size_x = max(1, int(metadata.get("cellsX") or 1))
            size_y = max(1, int(metadata.get("cellsY") or 1))
            if size_x != size_y:
                continue
            scene_index = scene_indexes.get(item_uuid)
            if scene_index is None:
                scene_path = find_tile_scene(self.game_root, item_uuid, metadata)
                scene_index = len(scenes)
                scene_indexes[item_uuid] = scene_index
                scenes.append({
                    "uuid": item_uuid,
                    "name": str(metadata.get("name") or item_uuid),
                    "cellsX": size_x,
                    "cellsY": size_y,
                    "available": bool(scene_path),
                })
            first = cells[0]
            origin_x, origin_y = self._placement_origin(first, size_x)
            placement = {
                "scene": scene_index,
                "x": origin_x,
                "y": origin_y,
                "rotation": int(first.rotation) % 4,
            }
            occupied = {(int(cell.x), int(cell.y)) for cell in cells}
            footprint = {
                (x, y)
                for y in range(origin_y, origin_y + size_y)
                for x in range(origin_x, origin_x + size_x)
            }
            rotation = int(first.rotation) % 4

            def local_cell(world_x: int, world_y: int) -> tuple[int, int]:
                dx, dy = world_x - origin_x, world_y - origin_y
                if rotation == 0:
                    return dx, dy
                if rotation == 1:
                    return dy, size_x - 1 - dx
                if rotation == 2:
                    return size_x - 1 - dx, size_y - 1 - dy
                return size_y - 1 - dy, dx

            if occupied != footprint:
                # Some generated landmarks deliberately share one large tile
                # footprint.  The save records which cells from each tile are
                # actually active.  Rendering the complete square is what
                # caused the excavation island's terrain and water to overlap.
                local_cells: list[list[int]] = []
                for world_x, world_y in sorted(occupied):
                    local_x, local_y = local_cell(world_x, world_y)
                    if 0 <= local_x < size_x and 0 <= local_y < size_y:
                        local_cells.append([local_x, local_y])
                placement["cellMask"] = local_cells
            lake_cells = [
                list(local_cell(int(cell.x), int(cell.y)))
                for cell in cells
                if int(cell.biome) == 8
            ]
            if lake_cells:
                # terrain_overworld.lua uses this flag only to place an
                # authored water asset independently of terrain elevation.
                # It does not synthesize a water surface for the cell.
                placement["lakeMask"] = lake_cells
            placements.append(placement)

        available = sum(bool(scene["available"]) for scene in scenes)
        x_min, x_max, y_min, y_max = grid.visible_bounds
        height_width = x_max - x_min + 1
        height_height = y_max - y_min + 1
        elevation = [
            float(grid.elevation.get((x, y), 0.0))
            for y in range(y_min, y_max + 2)
            for x in range(x_min, x_max + 2)
        ]
        cliff_base = [
            float(
                min(
                    grid.cliff_level.get((x, y), 0),
                    grid.cliff_level.get((x + 1, y), 0),
                    grid.cliff_level.get((x, y + 1), 0),
                    grid.cliff_level.get((x + 1, y + 1), 0),
                )
                * 8
            )
            for y in range(y_min, y_max + 1)
            for x in range(x_min, x_max + 1)
        ]
        flags = [
            float(grid.cells[(x, y)].flags) if (x, y) in grid.cells else 0.0
            for y in range(y_min, y_max + 1)
            for x in range(x_min, x_max + 1)
        ]
        self.terrain_3d_index = {
            "available": available > 0,
            "rendererStatus": "foundation",
            "cellSize": 64,
            "bounds": list(grid.visible_bounds),
            "scenes": scenes,
            "placements": placements,
            "heightField": {
                "width": height_width,
                "height": height_height,
                "elevation": elevation,
                "cliffBase": cliff_base,
                "flags": flags,
            },
            "coverage": {
                "availableScenes": available,
                "uniqueScenes": len(scenes),
                "worldPlacements": len(placements),
                "partialPlacements": sum(
                    "cellMask" in placement for placement in placements
                ),
            },
        }
        return self.terrain_3d_index

    def _map_3d_asset_definition(self, asset_uuid: str) -> dict[str, Any] | None:
        """Return the renderer metadata shared by tiles and live save entities."""
        if not self.game_root:
            return None
        if self.terrain_asset_index is None:
            self.terrain_asset_index = TerrainAssetIndex(self.game_root).build()
        normalized = str(asset_uuid or "").casefold()
        renderable = self.terrain_asset_index.get(normalized)
        primitive = self.terrain_asset_index.get_blueprint_primitive(normalized)
        if renderable:
            return {
                "uuid": renderable.uuid,
                "name": renderable.name,
                "mesh": renderable.mesh.resolve().as_uri(),
                "meshFormat": renderable.mesh.suffix.casefold().lstrip("."),
                "meshSupported": renderable.mesh.suffix.casefold() in {".fbx", ".dae", ".obj", ".mesh"},
                "lodCount": len(renderable.lod_meshes),
                "lodMinViewSizes": list(renderable.lod_min_view_sizes),
                "lodMaxViewDistances": list(renderable.lod_max_view_distances),
                "smallestVisibleSize": renderable.smallest_visible_size,
                "impostorThreshold": renderable.impostor_threshold,
                "occluderCorners": [list(corner) for corner in renderable.occluder_corners],
                "textures": [texture.resolve().as_uri() for texture in renderable.textures if texture.is_file()],
                "materials": list(renderable.material_names),
                "diffuseTextures": [
                    texture.resolve().as_uri() if texture and texture.is_file() else ""
                    for texture in renderable.diffuse_textures
                ],
                "diffuseByMaterial": {
                    name: texture.resolve().as_uri() if texture and texture.is_file() else ""
                    for name, texture in renderable.diffuse_by_material
                },
                "asgByMaterial": {
                    name: texture.resolve().as_uri() if texture and texture.is_file() else ""
                    for name, texture in renderable.asg_by_material
                },
                "materialProfiles": dict(renderable.material_profile_by_material),
                "alphaLuma": dict(renderable.alpha_luma_by_material),
                "tintedDetail": dict(renderable.tinted_detail_by_material),
                "uvMaterial": dict(renderable.uv_material_by_material),
                "useUv1": dict(renderable.use_uv1_by_material),
                "colorKeys": dict(renderable.color_key_by_material),
                "defaultColors": dict(renderable.default_colors),
                "specialSurface": "",
            }
        if primitive:
            return {
                "uuid": primitive.uuid,
                "name": primitive.name,
                "mesh": "",
                "meshFormat": "primitive",
                "meshSupported": True,
                "textures": [],
                "materials": ["BlueprintBlock"],
                "diffuseTextures": [],
                "diffuseByMaterial": {},
                "asgByMaterial": {},
                "materialProfiles": {"0": "BlueprintBlock"},
                "alphaLuma": {},
                "tintedDetail": {},
                "uvMaterial": {},
                "useUv1": {},
                "colorKeys": {},
                "defaultColors": {"block": primitive.color},
                "specialSurface": "",
                "primitive": {
                    "kind": primitive.kind,
                    "texture": self.mesh_geometry.image_uri(primitive.texture)
                    if primitive.texture and self.mesh_geometry else "",
                    "tiling": primitive.tiling,
                    "glass": primitive.glass,
                },
            }
        return None

    @staticmethod
    def _saved_orientation(value: int) -> tuple[int, int]:
        """Decode ChildShape's packed x/z axis byte (all 24 cube orientations)."""
        if value == 0:
            return 1, 3
        decode = lambda nibble: nibble if nibble <= 3 else -(nibble - 4)
        return decode((value >> 4) & 0x0F), decode(value & 0x0F)

    def map_3d_entities(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Decode players, units and rigid bodies as real nearby 3D geometry."""
        save = self._require_save()
        signature = f"{save.path}:{save.path.stat().st_mtime_ns}:{WORLD_ENTITY_CACHE_REVISION}"
        cache_path = CACHE_DIR / "world-entities" / f"{hashlib.sha1(signature.encode('utf-8')).hexdigest()[:24]}.json.gz"
        if cache_path.is_file():
            try:
                with gzip.open(cache_path, "rt", encoding="utf-8") as stream:
                    cached = json.load(stream)
                if isinstance(cached, dict) and "definitions" in cached and "instances" in cached:
                    return cached
            except (OSError, ValueError, TypeError):
                pass
        index = self.map_3d_index({})
        xmin, _xmax, ymin, _ymax = [int(value) for value in index.get("bounds", [0, 0, 0, 0])]
        offset_x, offset_y = xmin * 64.0, ymin * 64.0
        grouped: dict[str, list[dict[str, Any]]] = {}
        counts = {"players": 0, "units": 0, "creations": 0, "shapes": 0}

        def append(asset_uuid: str, instance: dict[str, Any]) -> None:
            grouped.setdefault(str(asset_uuid).casefold(), []).append(instance)

        # The active mechanic position is stored separately from Unit rows.
        mechanic_uuid = "00000000-0000-0000-0000-000000000000"
        for player in save.players:
            if int(player.world_id) != 1:
                continue
            append(mechanic_uuid, {
                "transform": {
                    "position": [float(player.x) - offset_x, float(player.y) - offset_y, float(player.z)],
                    "rotation": [0, 0, math.degrees(float(player.yaw))],
                    "scale": [0.25, 0.25, 0.25],
                },
                "tint": "#ffb62e",
                "entityType": "player",
            })
            counts["players"] += 1

        connection = sqlite3.connect(f"file:{save.path.as_posix()}?mode=ro", uri=True)
        try:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "Unit" in tables:
                for world_id, data in connection.execute("SELECT worldId, data FROM Unit"):
                    if int(world_id) != 1 or not isinstance(data, bytes) or len(data) < 56:
                        continue
                    unit_uuid = str(uuid.UUID(bytes=data[18:34][::-1]))
                    z, y, x = struct.unpack_from(">fff", data, 36)
                    yaw = struct.unpack_from(">f", data, 52)[0]
                    append(unit_uuid, {
                        "transform": {
                            "position": [float(x) - offset_x, float(y) - offset_y, float(z)],
                            "rotation": [0, 0, math.degrees(float(yaw))],
                            "scale": [0.25, 0.25, 0.25],
                        },
                        "entityType": "unit",
                    })
                    counts["units"] += 1

            bodies: dict[int, dict[str, Any]] = {}
            if "RigidBody" in tables:
                for body_id, world_id, data in connection.execute("SELECT id, worldId, data FROM RigidBody"):
                    if int(world_id) != 1 or not isinstance(data, bytes) or len(data) < 55:
                        continue
                    quaternion = list(struct.unpack_from(">ffff", data, 27))
                    x, y, z = struct.unpack_from(">fff", data, 43)
                    bodies[int(body_id)] = {
                        "position": [float(x) - offset_x, float(y) - offset_y, float(z)],
                        "quaternion": quaternion,
                    }
                counts["creations"] = len(bodies)
            if bodies and "ChildShape" in tables:
                for body_id, data in connection.execute("SELECT bodyId, data FROM ChildShape"):
                    body = bodies.get(int(body_id))
                    if body is None or not isinstance(data, bytes) or len(data) < 43:
                        continue
                    asset_uuid = str(uuid.UUID(bytes=data[11:27][::-1]))
                    px, py, pz = (int.from_bytes(data[pos:pos + 2], "big", signed=True) for pos in (31, 33, 35))
                    axis_x, axis_z = self._saved_orientation(data[41])
                    definition = self._map_3d_asset_definition(asset_uuid)
                    primitive = bool(definition and definition.get("primitive"))
                    if primitive and len(data) >= 48:
                        # ChildShape stores block extents as little-endian unsigned
                        # 16-bit counts.  Positions in the same record are big-endian,
                        # which made this easy to miss: decoding extents the same way
                        # turned a 14 x 65 x 1 block into 3584 x 16640 x 256 and
                        # produced the world-sized walls seen in the 3D map.
                        bounds = [
                            max(1, int.from_bytes(data[pos:pos + 2], "little")) * 0.25
                            for pos in (42, 44, 46)
                        ]
                    else:
                        bounds = [0.25, 0.25, 0.25]
                    append(asset_uuid, {
                        "transformChain": [
                            body,
                            {
                                "position": [px * 0.25, py * 0.25, pz * 0.25],
                                "axisX": axis_x,
                                "axisZ": axis_z,
                                "scale": bounds,
                            },
                        ],
                        "tint": f"#{data[38:41].hex()}",
                        "entityType": "creation",
                    })
                    counts["shapes"] += 1
        finally:
            connection.close()

        definitions: list[dict[str, Any]] = []
        instances: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for asset_uuid, rows in grouped.items():
            definition = self._map_3d_asset_definition(asset_uuid)
            if not definition or not definition.get("meshSupported"):
                unresolved.append(asset_uuid)
                continue
            asset = len(definitions)
            definitions.append(definition)
            instances.extend({**row, "asset": asset, "uuid": asset_uuid} for row in rows)
        result = {
            "definitions": definitions,
            "instances": instances,
            "counts": counts,
            "unresolved": unresolved,
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=3) as stream:
                json.dump(result, stream, separators=(",", ":"))
            temporary.replace(cache_path)
        except OSError:
            pass
        return result

    def _map_3d_scene_cache_paths(
        self, payload: dict[str, Any]
    ) -> tuple[str, Path, Path, Path]:
        """Resolve a scene and its persistent compressed-cache path."""
        save = self._require_save()
        grid = save.terrain_grid
        item_uuid = str(payload.get("uuid") or "").casefold()
        if not grid or item_uuid not in {cell.uuid for cell in grid.cells.values()}:
            raise ValueError("The requested 3D tile is not part of this world")
        if not self.game_root:
            raise RuntimeError("Scrap Mechanic installation was not found")
        metadata = self.tile_names.get(item_uuid, {})
        scene_path = find_tile_scene(self.game_root, item_uuid, metadata)
        if not scene_path:
            raise RuntimeError("The installed tile has no editable .tileson scene")
        compiled_tile = scene_path.with_suffix(".tile")
        location_key = (
            f"{payload.get('x', '')}:{payload.get('y', '')}"
            if "x" in payload or "y" in payload else "world"
        )
        cache_signature = ":".join(
            (
                str(scene_path.resolve()),
                str(scene_path.stat().st_mtime_ns),
                str(compiled_tile.stat().st_mtime_ns if compiled_tile.is_file() else 0),
                location_key,
                str(WORLD_SCENE_CACHE_REVISION),
            )
        )
        scene_cache_dir = CACHE_DIR / "world-scenes"
        scene_cache_path = scene_cache_dir / (
            hashlib.sha1(cache_signature.encode("utf-8")).hexdigest()[:28] + ".json.gz"
        )
        return item_uuid, scene_path, compiled_tile, scene_cache_path

    def map_3d_scene(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve one tile scene for camera-driven WebGL streaming."""
        item_uuid, scene_path, compiled_tile, scene_cache_path = (
            self._map_3d_scene_cache_paths(payload)
        )
        metadata = self.tile_names.get(item_uuid, {})
        if not metadata:
            metadata = self.tile_names.get(item_uuid.casefold(), {})
        scene_cache_dir = scene_cache_path.parent
        if scene_cache_path.is_file():
            try:
                with gzip.open(scene_cache_path, "rt", encoding="utf-8") as stream:
                    cached_scene = json.load(stream)
                if isinstance(cached_scene, dict) and cached_scene.get("uuid") == item_uuid:
                    return cached_scene
            except (OSError, ValueError, TypeError):
                pass
        scene = load_game_json(scene_path)
        entities = scene.get("entities") if isinstance(scene, dict) else {}
        rows = entities.get("assets") if isinstance(entities, dict) else []
        rows = rows if isinstance(rows, list) else []
        harvestable_rows = (
            entities.get("harvestables") if isinstance(entities, dict) else []
        )
        harvestable_rows = harvestable_rows if isinstance(harvestable_rows, list) else []
        blueprint_rows = entities.get("blueprints") if isinstance(entities, dict) else []
        blueprint_rows = blueprint_rows if isinstance(blueprint_rows, list) else []
        decal_rows = entities.get("decals") if isinstance(entities, dict) else []
        decal_rows = decal_rows if isinstance(decal_rows, list) else []
        prefab_rows = entities.get("prefabs") if isinstance(entities, dict) else []
        prefab_rows = prefab_rows if isinstance(prefab_rows, list) else []

        compiled_assets = (
            list(self.tile_terrain.asset_transforms(compiled_tile))
            if compiled_tile.is_file() else []
        )
        compiled_harvestables = (
            list(self.tile_terrain.harvestable_transforms(compiled_tile))
            if compiled_tile.is_file() else []
        )
        compiled_blueprints = (
            list(self.tile_terrain.blueprint_transforms(compiled_tile))
            if compiled_tile.is_file() else []
        )
        compiled_decals = (
            list(self.tile_terrain.decal_transforms(compiled_tile))
            if compiled_tile.is_file() else []
        )
        compiled_prefabs = (
            list(self.tile_terrain.prefab_transforms(compiled_tile))
            if compiled_tile.is_file() else []
        )

        def with_compiled_transform(
            row: dict[str, Any],
            compiled: list[dict[str, Any]],
            identity_key: str = "",
        ) -> dict[str, Any]:
            """Replace ambiguous editor Euler rotations with saved quaternions."""
            transform = row.get("transform")
            transform = dict(transform) if isinstance(transform, dict) else {}
            position = transform.get("position")
            if not isinstance(position, list) or len(position) < 3:
                return row
            identity = str(row.get(identity_key) or "").replace("\\", "/").casefold()
            matches = compiled
            if identity_key and identity:
                matches = [
                    item for item in compiled
                    if str(item.get(identity_key) or "").replace("\\", "/").casefold()
                    == identity
                ]
            if not matches:
                return row
            match = min(
                matches,
                key=lambda item: sum(
                    (float(item["position"][axis]) - float(position[axis])) ** 2
                    for axis in range(3)
                ),
            )
            if sum(
                (float(match["position"][axis]) - float(position[axis])) ** 2
                for axis in range(3)
            ) > 0.01:
                return row
            transform["quaternion"] = match["quaternion"]
            if match.get("scale"):
                transform["scale"] = match["scale"]
            merged = dict(row)
            merged["transform"] = transform
            if match.get("colors") and not merged.get("colorMap"):
                merged["colorMap"] = match["colors"]
            if match.get("color") and not merged.get("color"):
                merged["color"] = match["color"]
            return merged

        if self.terrain_asset_index is None:
            self.terrain_asset_index = TerrainAssetIndex(self.game_root).build()
        definitions: list[dict[str, Any]] = []
        definition_indexes: dict[str, int] = {}
        unresolved: set[str] = set()
        instances: list[dict[str, Any]] = []
        render_rows: list[dict[str, Any]] = []
        prefab_decal_rows: list[dict[str, Any]] = []

        def visible_by_default(row: dict[str, Any]) -> bool:
            """Hide runtime variants that only appear after a tile event."""
            if row.get("exclude") or row.get("hidden"):
                return False
            tags = row.get("tags")
            return not (
                isinstance(tags, list)
                and any(
                    str(tag).strip().casefold().startswith("ts:show:")
                    for tag in tags
                )
            )

        def blueprint_resource(value: Any) -> dict[str, Any]:
            """Load either an inline blueprint or an installed .blueprint file."""
            if isinstance(value, dict):
                return value
            if not isinstance(value, str):
                return {}
            path = self.terrain_asset_index.paths.resolve(value)
            if not path or not path.is_file():
                return {}
            try:
                data = load_game_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def blueprint_child_transform(
            child: dict[str, Any], primitive: Any
        ) -> dict[str, Any]:
            """Translate Scrap Mechanic's 0.25 m blueprint grid and axis codes."""
            position = child.get("pos")
            position = position if isinstance(position, dict) else {}
            axis_x = int(child.get("xaxis") or 1)
            axis_z = int(child.get("zaxis") or 3)
            bounds = child.get("bounds")
            bounds = bounds if isinstance(bounds, dict) else {}
            if primitive:
                scale = [
                    max(1.0, float(bounds.get(axis) or 1.0)) * 0.25
                    for axis in ("x", "y", "z")
                ]
            else:
                # FBX and raw Collada coordinates in shape renderables use the
                # same quarter-metre construction unit as blueprint positions.
                # Assimp restores Collada's authored centimetre coordinates,
                # therefore all blueprint parts need this local 0.25 scale.
                scale = [0.25, 0.25, 0.25]
            return {
                "position": [
                    float(position.get(axis) or 0.0) * 0.25
                    for axis in ("x", "y", "z")
                ],
                "axisX": axis_x,
                "axisZ": axis_z,
                "scale": scale,
            }

        def append_blueprint_shapes(
            blueprint: dict[str, Any],
            parent_chain: list[dict[str, Any]] | None = None,
            kind: str = "blueprint",
        ) -> int:
            resource = blueprint_resource(blueprint.get("resource"))
            bodies = resource.get("bodies") if isinstance(resource, dict) else []
            bodies = bodies if isinstance(bodies, list) else []
            blueprint_transform = dict(blueprint.get("transform") or {})
            added = 0
            for body in bodies:
                children = body.get("childs") if isinstance(body, dict) else []
                children = children if isinstance(children, list) else []
                for child in children:
                    if not isinstance(child, dict) or not child.get("shapeId"):
                        continue
                    shape_uuid = str(child.get("shapeId") or "").casefold()
                    primitive = self.terrain_asset_index.get_blueprint_primitive(
                        shape_uuid
                    )
                    local_transform = blueprint_child_transform(child, primitive)
                    render_rows.append({
                        "uuid": shape_uuid,
                        "transform": local_transform,
                        "_transform_chain": [
                            *(parent_chain or []),
                            blueprint_transform,
                            local_transform,
                        ],
                        "_kind": (
                            f"{kind} {primitive.kind}" if primitive else kind
                        ),
                        "_tint": child.get("color") or "",
                    })
                    added += 1
            return added

        for row in rows:
            if isinstance(row, dict) and visible_by_default(row):
                render_rows.append(with_compiled_transform(row, compiled_assets, "uuid"))
        for row in harvestable_rows:
            if isinstance(row, dict) and visible_by_default(row):
                exact = with_compiled_transform(row, compiled_harvestables, "uuid")
                render_rows.append({**exact, "_kind": "harvestable"})
        for blueprint in blueprint_rows:
            if not isinstance(blueprint, dict):
                continue
            if not visible_by_default(blueprint) and not (
                blueprint.get("locked") and blueprint.get("resource")
            ):
                continue
            blueprint = with_compiled_transform(blueprint, compiled_blueprints)
            append_blueprint_shapes(blueprint)

        expanded_prefabs = 0
        deferred_prefabs = 0

        def exact_prefab_asset(row: dict[str, Any], data: bytes) -> dict[str, Any]:
            """Recover an internal prefab asset's big-endian quaternion."""
            try:
                needle = uuid.UUID(str(row.get("uuid") or "")).bytes
            except (ValueError, AttributeError):
                return row
            position = (row.get("transform") or {}).get("position")
            if not isinstance(position, list) or len(position) < 3:
                return row
            candidates: list[tuple[float, tuple[float, ...]]] = []
            offset = 0
            while True:
                found = data.find(needle, offset)
                if found < 0:
                    break
                offset = found + 1
                if found < 40:
                    continue
                values = struct.unpack_from(">10f", data, found - 40)
                distance = sum(
                    (float(values[axis]) - float(position[axis])) ** 2
                    for axis in range(3)
                )
                if all(abs(value) < 10_000 for value in values):
                    candidates.append((distance, values))
            if not candidates:
                return row
            distance, values = min(candidates, key=lambda item: item[0])
            if distance > 0.01:
                return row
            transform = dict(row.get("transform") or {})
            transform["quaternion"] = list(values[3:7])
            transform["scale"] = list(values[7:10])
            return {**row, "transform": transform}

        def prefab_blueprint_transforms(data: bytes) -> list[dict[str, Any]]:
            transforms: list[dict[str, Any]] = []
            offset = 0
            while True:
                marker = data.find(b"?JB:", offset)
                if marker < 0:
                    break
                offset = marker + 4
                if marker < 4:
                    continue
                length = struct.unpack_from(">I", data, marker - 4)[0]
                end = marker + length
                if length < 4 or end + 28 > len(data):
                    continue
                values = struct.unpack_from(">7f", data, end)
                transforms.append({
                    "position": list(values[:3]),
                    "quaternion": list(values[3:7]),
                })
            return transforms

        def active_prefabs(rows: Any) -> list[dict[str, Any]]:
            """Prefer intact runtime variants when both prefab states are stored."""
            candidates = (
                [
                    row for row in rows
                    if isinstance(row, dict) and visible_by_default(row)
                ]
                if isinstance(rows, list)
                else []
            )
            paths = {
                str(row.get("path") or "").replace("\\", "/").casefold()
                for row in candidates
            }
            active: list[dict[str, Any]] = []
            for row in candidates:
                path = str(row.get("path") or "").replace("\\", "/").casefold()
                if "(destroyed)" in path and path.replace("(destroyed)", "(intact)") in paths:
                    continue
                active.append(row)
            return active

        def expand_prefab(
            prefab: dict[str, Any],
            parent_chain: list[dict[str, Any]] | None = None,
            ancestry: tuple[str, ...] = (),
            depth: int = 0,
        ) -> None:
            nonlocal expanded_prefabs, deferred_prefabs
            if not visible_by_default(prefab):
                return
            path = self.terrain_asset_index.paths.resolve(str(prefab.get("path") or ""))
            editable = path.with_suffix(".prefabson") if path else None
            identity = str(path.resolve()).casefold() if path else ""
            if (
                not editable
                or not editable.is_file()
                or depth >= 12
                or not identity
                or identity in ancestry
            ):
                deferred_prefabs += 1
                return
            try:
                prefab_data = load_game_json(editable)
            except (OSError, ValueError, json.JSONDecodeError):
                deferred_prefabs += 1
                return
            prefab_entities = (
                prefab_data.get("entities") if isinstance(prefab_data, dict) else {}
            )
            if not isinstance(prefab_entities, dict):
                deferred_prefabs += 1
                return
            try:
                compiled_prefab = path.read_bytes() if path and path.is_file() else b""
            except OSError:
                compiled_prefab = b""
            blueprint_transforms = prefab_blueprint_transforms(compiled_prefab)
            prefab_transform = prefab.get("transform") or {}
            transform_chain = [*(parent_chain or []), prefab_transform]
            for child in prefab_entities.get("assets", []):
                if not isinstance(child, dict) or not visible_by_default(child):
                    continue
                child = exact_prefab_asset(child, compiled_prefab)
                render_rows.append({
                    **child,
                    "_kind": "prefab asset",
                    "_transform_chain": [
                        *transform_chain,
                        child.get("transform") or {},
                    ],
                })
            for blueprint in prefab_entities.get("blueprints", []):
                if not isinstance(blueprint, dict):
                    continue
                if not visible_by_default(blueprint) and not (
                    blueprint.get("locked") and blueprint.get("resource")
                ):
                    continue
                blueprint_transform = dict(blueprint.get("transform") or {})
                position = blueprint_transform.get("position")
                if isinstance(position, list) and len(position) >= 3 and blueprint_transforms:
                    exact = min(
                        blueprint_transforms,
                        key=lambda item: sum(
                            (float(item["position"][axis]) - float(position[axis])) ** 2
                            for axis in range(3)
                        ),
                    )
                    if sum(
                        (float(exact["position"][axis]) - float(position[axis])) ** 2
                        for axis in range(3)
                    ) <= 0.01:
                        blueprint_transform["quaternion"] = exact["quaternion"]
                append_blueprint_shapes(
                    {**blueprint, "transform": blueprint_transform},
                    transform_chain,
                    "prefab blueprint",
                )
            for decal in prefab_entities.get("decals", []):
                if not isinstance(decal, dict) or not visible_by_default(decal):
                    continue
                prefab_decal_rows.append({
                    **decal,
                    "_kind": "prefab decal",
                    "_transform_chain": [
                        *transform_chain,
                        decal.get("transform") or {},
                    ],
                })
            for nested in active_prefabs(prefab_entities.get("prefabs", [])):
                expand_prefab(
                    nested,
                    transform_chain,
                    (*ancestry, identity),
                    depth + 1,
                )
            expanded_prefabs += 1

        for prefab in active_prefabs(prefab_rows):
            prefab = with_compiled_transform(prefab, compiled_prefabs, "path")
            expand_prefab(prefab)

        # Prefabs commonly repeat the same default-state asset through nested
        # groups.  The game resolves those groups at runtime; drawing every
        # copy causes flashing roofs and ruin surfaces in a conventional
        # depth-buffer renderer.
        unique_rows: list[dict[str, Any]] = []
        seen_rows: set[str] = set()
        for row in render_rows:
            if not isinstance(row, dict) or not visible_by_default(row):
                continue
            identity = json.dumps(
                {
                    "uuid": str(row.get("uuid") or "").casefold(),
                    "transform": row.get("transform") or {},
                    "chain": row.get("_transform_chain") or [],
                    "colors": row.get("colorMap") or {},
                    "tint": row.get("_tint") or row.get("color") or "",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            if identity in seen_rows:
                continue
            seen_rows.add(identity)
            unique_rows.append(row)
        render_rows = unique_rows

        rendered_kinds: dict[str, int] = {}
        for row in render_rows:
            asset_uuid = str(row.get("uuid") or "").casefold()
            renderable = self.terrain_asset_index.get(asset_uuid)
            primitive = self.terrain_asset_index.get_blueprint_primitive(asset_uuid)
            if renderable and any(
                token in renderable.name.casefold()
                for token in ("arealight", "lightcone")
            ):
                # These are invisible illumination/projector helpers in the
                # game engine, not physical scene geometry.
                continue
            definition_index = definition_indexes.get(asset_uuid)
            if renderable and definition_index is None:
                definition_index = len(definitions)
                definition_indexes[asset_uuid] = definition_index
                definitions.append({
                    "uuid": renderable.uuid,
                    "name": renderable.name,
                    "mesh": renderable.mesh.resolve().as_uri(),
                    "meshFormat": renderable.mesh.suffix.casefold().lstrip("."),
                    "meshSupported": renderable.mesh.suffix.casefold() in {".fbx", ".dae", ".obj", ".mesh"},
                    "lodCount": len(renderable.lod_meshes),
                    "lodMinViewSizes": list(renderable.lod_min_view_sizes),
                    "lodMaxViewDistances": list(renderable.lod_max_view_distances),
                    "smallestVisibleSize": renderable.smallest_visible_size,
                    "impostorThreshold": renderable.impostor_threshold,
                    "occluderCorners": [
                        list(corner) for corner in renderable.occluder_corners
                    ],
                    "textures": [
                        texture.resolve().as_uri()
                        for texture in renderable.textures
                        if texture.is_file()
                    ],
                    "materials": list(renderable.material_names),
                    "diffuseTextures": [
                        texture.resolve().as_uri() if texture and texture.is_file() else ""
                        for texture in renderable.diffuse_textures
                    ],
                    "diffuseByMaterial": {
                        name: texture.resolve().as_uri()
                        if texture and texture.is_file() else ""
                        for name, texture in renderable.diffuse_by_material
                    },
                    "asgByMaterial": {
                        name: texture.resolve().as_uri()
                        if texture and texture.is_file() else ""
                        for name, texture in renderable.asg_by_material
                    },
                    "materialProfiles": dict(renderable.material_profile_by_material),
                    "alphaLuma": dict(renderable.alpha_luma_by_material),
                    "tintedDetail": dict(renderable.tinted_detail_by_material),
                    "uvMaterial": dict(renderable.uv_material_by_material),
                    "useUv1": dict(renderable.use_uv1_by_material),
                    "colorKeys": dict(renderable.color_key_by_material),
                    "defaultColors": dict(renderable.default_colors),
                    "specialSurface": (
                        "water" if asset_uuid == WATER_SURFACE_UUID
                        else "chemical" if asset_uuid == CHEMICAL_SURFACE_UUID
                        else ""
                    ),
                })
            if primitive and definition_index is None:
                definition_index = len(definitions)
                definition_indexes[asset_uuid] = definition_index
                definitions.append({
                    "uuid": primitive.uuid,
                    "name": primitive.name,
                    "mesh": "",
                    "meshFormat": "primitive",
                    "meshSupported": True,
                    "textures": [],
                    "materials": ["BlueprintBlock"],
                    "diffuseTextures": [],
                    "diffuseByMaterial": {},
                    "asgByMaterial": {},
                    "materialProfiles": {"0": "BlueprintBlock"},
                    "alphaLuma": {},
                    "tintedDetail": {},
                    "uvMaterial": {},
                    "useUv1": {},
                    "colorKeys": {},
                    "defaultColors": {"block": primitive.color},
                    "specialSurface": "",
                    "primitive": {
                        "kind": primitive.kind,
                        "texture": (
                            self.mesh_geometry.image_uri(primitive.texture)
                            if primitive.texture and self.mesh_geometry else ""
                        ),
                        "tiling": primitive.tiling,
                        "glass": primitive.glass,
                    },
                })
            if renderable is None and primitive is None:
                unresolved.add(asset_uuid)
            instances.append({
                "asset": definition_index if renderable or primitive else None,
                "uuid": asset_uuid,
                "transform": row.get("transform") or {},
                "transformChain": row.get("_transform_chain") or [],
                "colors": row.get("colorMap") or {},
                "tint": row.get("_tint") or row.get("color") or "",
                "kind": row.get("_kind") or "asset",
            })
            kind = str(row.get("_kind") or "asset")
            rendered_kinds[kind] = rendered_kinds.get(kind, 0) + 1

        decal_groups: dict[str, dict[str, Any]] = {}
        seen_decals: set[str] = set()
        if self.terrain_decal_index and self.mesh_geometry:
            exact_decal_rows = [
                with_compiled_transform(row, compiled_decals, "uuid")
                for row in decal_rows
                if isinstance(row, dict)
            ]
            for decal_order, row in enumerate(
                [*exact_decal_rows, *prefab_decal_rows]
            ):
                if not isinstance(row, dict) or not visible_by_default(row):
                    continue
                editor_transform = row.get("transform") or {}
                exact = row
                transform = dict(editor_transform)
                position = transform.get("position") or [0, 0, 0]
                scale = [abs(float(value)) for value in transform.get("scale", [1, 1, 1])]
                editor_rotation = editor_transform.get("rotation") or [0, 0, 0]
                pitch = float(editor_rotation[0]) if len(editor_rotation) > 0 else 0.0
                roll = float(editor_rotation[1]) if len(editor_rotation) > 1 else 0.0
                yaw = float(editor_rotation[2]) if len(editor_rotation) > 2 else 0.0
                decal_uuid = str(exact.get("uuid") or "").casefold()
                decal = self.terrain_decal_index.get(decal_uuid)
                if decal is None:
                    continue
                decal_name = decal.name.casefold()
                # These projector helpers are authored as soft circular/box
                # volumes.  Rendering them as flat quads produces the large
                # red dots that are never visible in the game.
                if (
                    decal_name.startswith(("circle_", "box01_", "shape_"))
                    or "projector" in decal_name
                ):
                    continue
                ground_name = any(
                    token in decal_name
                    for token in (
                        "strip",
                        "tire",
                        "road",
                        "track",
                        "parking",
                        "dirt",
                        "patch",
                        "burnmark",
                        "crack",
                        "moss",
                    )
                )
                horizontal = (
                    min(abs(pitch), abs(abs(pitch) - 180.0)) <= 12.0
                    and min(abs(roll), abs(abs(roll) - 180.0)) <= 12.0
                )
                # Scrap Mechanic projects decals through a 3D volume.  A WebGL
                # preview quad is faithful only for authored horizontal ground
                # marks.  Curved, wall, landing-symbol and effect projectors
                # need the game's deferred renderer; showing them as quads
                # creates the giant walls and red dots seen in prior builds.
                if not horizontal or not ground_name:
                    continue
                # The compiled quaternion is the game's authoritative rotation.
                # In particular, an editor pitch near 180 degrees changes the
                # handedness of the projected road mark; reducing that to yaw
                # mirrored and mis-rotated tire marks and lane strips.
                if not (
                    isinstance(transform.get("quaternion"), list)
                    and len(transform["quaternion"]) == 4
                ):
                    transform["rotation"] = [pitch, roll, yaw]
                transform["scale"] = [
                    abs(float(value))
                    for value in transform.get("scale", [1, 1, 1])
                ]
                ground_position = list(position)
                layer = max(0.0, min(120.0, float(row.get("layer") or 0.0)))
                ground_position[2] = (
                    float(ground_position[2])
                    + 0.026
                    + layer * 0.00065
                    + decal_order * 0.00003
                )
                transform["position"] = ground_position
                decal_identity = json.dumps(
                    {
                        "uuid": decal_uuid,
                        "transform": transform,
                        "chain": (row.get("_transform_chain") or [])[:-1],
                        "tint": exact.get("color") or "#FFFFFFFF",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if decal_identity in seen_decals:
                    continue
                seen_decals.add(decal_identity)
                group = decal_groups.get(decal_uuid)
                if group is None:
                    group = {
                        "uuid": decal.uuid,
                        "name": decal.name,
                        "texture": self.mesh_geometry.decal_uri(
                            decal.texture, decal.region
                        ),
                        "instances": [],
                    }
                    decal_groups[decal_uuid] = group
                group["instances"].append({
                    "transform": transform,
                    "transformChain": [
                        *(row.get("_transform_chain") or [])[:-1],
                        transform,
                    ]
                    if row.get("_transform_chain")
                    else [],
                    "tint": exact.get("color") or "#FFFFFFFF",
                    "layer": int(row.get("layer") or 0),
                })

        summary = summarize_tile_scene(scene_path)
        terrain = None
        if compiled_tile.is_file() and self.mesh_geometry and self.game_root:
            try:
                terrain = self.tile_terrain.geometry(compiled_tile)
                ground_root = (
                    self.game_root / "Data" / "Terrain" / "Textures" / "Ground"
                )
                ground_texture_paths = [
                    ground_root / f"gnd_{index}_dif.tga" for index in range(8)
                ] + [ground_root / "gnd_grass_dif.tga"]
                terrain["textures"] = [
                    self.mesh_geometry.image_uri(path) for path in ground_texture_paths
                ]
            except (OSError, ValueError, RuntimeError):
                terrain = None
        standard_meshes = sum(
            definition["meshFormat"] in {"fbx", "dae", "obj", "mesh", "primitive"}
            for definition in definitions
        )
        world_index = self.map_3d_index({})
        scene_number = next(
            (
                index for index, row in enumerate(world_index.get("scenes", []))
                if str(row.get("uuid") or "").casefold() == item_uuid
            ),
            None,
        )
        world_placement = None
        if scene_number is not None:
            candidates = [
                row for row in world_index.get("placements", [])
                if row.get("scene") == scene_number
            ]
            if candidates:
                target_x = float(payload.get("x") or candidates[0].get("x") or 0)
                target_y = float(payload.get("y") or candidates[0].get("y") or 0)
                world_placement = min(
                    candidates,
                    key=lambda row: (
                        (float(row.get("x") or 0) - target_x) ** 2
                        + (float(row.get("y") or 0) - target_y) ** 2
                    ),
                )
        result = {
            "uuid": item_uuid,
            "name": str(metadata.get("name") or item_uuid),
            "cellsX": summary.cells_x,
            "cellsY": summary.cells_y,
            "definitions": definitions,
            "instances": instances,
            "decals": list(decal_groups.values()),
            "terrain": terrain,
            "worldPlacement": world_placement,
            "unresolved": sorted(unresolved),
            "coverage": {
                "instances": len(instances),
                "uniqueAssets": len(summary.unique_asset_uuids),
                "resolvedAssets": len(definitions),
                "standardMeshes": standard_meshes,
                "renderableDefinitions": len(definitions),
                "unsupportedModelFormats": len(definitions) - standard_meshes,
                "unresolvedInstances": sum(
                    instance.get("asset") is None for instance in instances
                ),
                "blueprints": summary.blueprints,
                "decals": summary.decals + len(prefab_decal_rows),
                "renderedDecals": sum(
                    len(group["instances"]) for group in decal_groups.values()
                ),
                "harvestables": summary.harvestables,
                "prefabs": deferred_prefabs,
                "expandedPrefabs": expanded_prefabs,
                "renderedKinds": rendered_kinds,
            },
        }
        try:
            scene_cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = scene_cache_path.with_suffix(".tmp")
            with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=3) as stream:
                json.dump(result, stream, separators=(",", ":"))
            temporary.replace(scene_cache_path)
        except OSError:
            pass
        return result

    def map_3d_scenes(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve a small scene batch with one desktop-bridge round trip.

        PyWebView serializes bridge calls through Python.  The old renderer
        paid that cross-process cost once for every unique tile (often more
        than 400 calls), even when the persistent scene cache was warm.
        Batching keeps progressive loading while cutting that overhead by
        roughly an order of magnitude.
        """
        requested = payload.get("uuids")
        if not isinstance(requested, list):
            raise ValueError("A list of terrain scene UUIDs is required")
        rows: list[dict[str, Any]] = []
        for item_uuid in requested[:16]:
            normalized = str(item_uuid or "").casefold()
            try:
                rows.append({
                    "uuid": normalized,
                    "scene": self.map_3d_scene({"uuid": normalized}),
                })
            except Exception as exc:
                rows.append({"uuid": normalized, "error": str(exc)})
        return {"scenes": rows}

    def map_3d_scene_packs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return bounded, compressed scene chunks for the complete-world view.

        Some of the installed 1024-cell scenes expand to tens of megabytes of
        JSON.  Sending sixteen decoded scenes through the desktop bridge at
        once can leave WebView2 parsing one enormous response for minutes.  A
        gzip/base64 pack keeps the bridge payload compact, while the byte cap
        guarantees that normal responses remain small and resumable.  A
        single unusually large scene is still returned so the cursor always
        advances.
        """
        requested = payload.get("uuids")
        if not isinstance(requested, list):
            raise ValueError("A list of terrain scene UUIDs is required")
        rows: list[dict[str, Any]] = []
        packed_bytes = 0
        maximum_bytes = 6 * 1024 * 1024
        for item_uuid in requested[:8]:
            normalized = str(item_uuid or "").casefold()
            try:
                _uuid, _scene, _compiled, scene_cache_path = (
                    self._map_3d_scene_cache_paths({"uuid": normalized})
                )
                if scene_cache_path.is_file():
                    # Forward the already-compressed persistent cache.  The
                    # old path decoded, serialized, and recompressed every
                    # repeated scene in every capture section.
                    packed = scene_cache_path.read_bytes()
                else:
                    scene = self.map_3d_scene({"uuid": normalized})
                    if scene_cache_path.is_file():
                        packed = scene_cache_path.read_bytes()
                    else:
                        raw = json.dumps(scene, separators=(",", ":")).encode("utf-8")
                        packed = gzip.compress(raw, compresslevel=1)
                if not packed.startswith(b"\x1f\x8b"):
                    raise ValueError("The cached 3D scene is not gzip data")
                if rows and packed_bytes + len(packed) > maximum_bytes:
                    break
                rows.append({
                    "uuid": normalized,
                    "encoding": "gzip-base64",
                    "data": base64.b64encode(packed).decode("ascii"),
                    "packedBytes": len(packed),
                })
                packed_bytes += len(packed)
            except Exception as exc:
                rows.append({
                    "uuid": normalized,
                    "error": str(exc),
                })
        return {
            "scenes": rows,
            "processed": len(rows),
            "packedBytes": packed_bytes,
        }

    def map_3d_mesh(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Decode one installed model into packed arrays for the WebGL renderer."""
        self._require_save()
        if not self.game_root or not self.mesh_geometry:
            raise RuntimeError("Scrap Mechanic installation was not found")
        asset_uuid = str(payload.get("uuid") or "").casefold()
        if not asset_uuid:
            raise ValueError("A terrain asset UUID is required")
        if self.terrain_asset_index is None:
            self.terrain_asset_index = TerrainAssetIndex(self.game_root).build()
        renderable = self.terrain_asset_index.get(asset_uuid)
        if renderable is None:
            raise ValueError("The requested terrain asset is not installed")
        requested_lod = payload.get("lod", 0)
        if isinstance(requested_lod, str) and requested_lod.casefold() == "coarsest":
            lod_index = max(0, len(renderable.lod_meshes) - 1)
        else:
            try:
                lod_index = int(requested_lod)
            except (TypeError, ValueError):
                lod_index = 0
        return self.mesh_geometry.geometry(renderable, lod_index)

    def map_3d_meshes(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve a bounded geometry batch for the complete-world viewer."""
        requests = payload.get("requests")
        if isinstance(requests, list):
            requested = [
                {
                    "uuid": str(row.get("uuid") or "").casefold(),
                    "lod": row.get("lod", 0),
                }
                for row in requests
                if isinstance(row, dict)
            ]
        else:
            uuids = payload.get("uuids")
            if not isinstance(uuids, list):
                raise ValueError("A list of terrain asset UUIDs is required")
            requested = [
                {"uuid": str(value or "").casefold(), "lod": payload.get("lod", 0)}
                for value in uuids
            ]
        rows: list[dict[str, Any]] = []
        for request in requested[:16]:
            normalized = str(request.get("uuid") or "").casefold()
            requested_lod = request.get("lod", 0)
            try:
                geometry = self.map_3d_mesh(
                    {"uuid": normalized, "lod": requested_lod}
                )
                renderable = (
                    self.terrain_asset_index.get(normalized)
                    if self.terrain_asset_index else None
                )
                texture_details: list[dict[str, Any]] = []
                if renderable and self.mesh_geometry:
                    seen_materials: set[tuple[int, str]] = set()
                    for part in geometry.get("parts", []):
                        material = int(part.get("material") or 0)
                        material_name = str(part.get("materialName") or "")
                        key = (material, material_name.casefold())
                        if key in seen_materials:
                            continue
                        seen_materials.add(key)
                        details = self.mesh_geometry.texture_details(
                            renderable, material, material_name
                        )
                        texture_details.append({
                            "material": material,
                            "materialName": material_name,
                            **details,
                        })
                rows.append({
                    "uuid": normalized,
                    "lod": int(geometry.get("lod") or 0),
                    "geometry": geometry,
                    "textures": texture_details,
                })
            except Exception as exc:
                rows.append({
                    "uuid": normalized,
                    "lod": requested_lod,
                    "error": str(exc),
                })
        return {"meshes": rows}

    def map_3d_texture(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Prepare one installed diffuse texture in a browser-readable format."""
        self._require_save()
        if not self.game_root or not self.mesh_geometry:
            raise RuntimeError("Scrap Mechanic installation was not found")
        asset_uuid = str(payload.get("uuid") or "").casefold()
        material = max(0, int(payload.get("material") or 0))
        material_name = str(payload.get("materialName") or "")
        if self.terrain_asset_index is None:
            self.terrain_asset_index = TerrainAssetIndex(self.game_root).build()
        renderable = self.terrain_asset_index.get(asset_uuid)
        if renderable is None:
            raise ValueError("The requested terrain asset is not installed")
        return self.mesh_geometry.texture_details(
            renderable, material, material_name
        )

    def ensure_map_image(self) -> Path:
        with self.lock:
            save = self._require_save()
            if not save.terrain_grid:
                raise RuntimeError(save.terrain_error or "This save has no decoded Survival terrain")
            CACHE_DIR.mkdir(exist_ok=True)
            stamp = int(save.path.stat().st_mtime_ns)
            path = CACHE_DIR / (
                f"{save.path.stem}-{stamp}-terrain-r{MAP_RENDER_REVISION}-{MAP_CELL_PIXELS}px.png"
            )
            if not self.game_root:
                raise RuntimeError("Scrap Mechanic installation was not found")
            if not path.is_file():
                TerrainRenderer(self.game_root).render_detailed_png(
                    path,
                    save.terrain_grid,
                    cell_pixels=MAP_CELL_PIXELS,
                )
            self.map_path = path
            return path

    def ensure_companion_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Build a small, exact and terrain-stable map for the live companion.

        A missing companion map used to instantiate the complete 3D world in a
        hidden WebView while Scrap Mechanic was running.  New worlds can contain
        more than half a million render objects, so that path could consume
        gigabytes and spend many minutes in garbage collection.  The companion
        now publishes this exact top-down atlas first.  A later editor-generated
        3D capture still supersedes it automatically when one exists.

        The cache key comes from ``_generated_3d_map_info`` and therefore follows
        the terrain seed/layout instead of the save database timestamp. Normal
        autosaves never rebuild this image.
        """
        save = self._require_save()
        grid = save.terrain_grid
        if not grid:
            raise RuntimeError(save.terrain_error or "Terrain unavailable")
        if not self.game_root:
            raise RuntimeError("Scrap Mechanic installation was not found")

        renderer = TerrainRenderer(self.game_root)
        cell_pixels = int(renderer.atlas.cell_pixels) if renderer.atlas else MAP_CELL_PIXELS
        signature = self._generated_3d_map_info(
            max_side=int(payload.get("maxSide") or 4096),
            variant="companion-fast",
        )
        path = CACHE_DIR / "companion-preview" / (
            f"{signature['path'].stem}-atlas-r1-{cell_pixels}px.png"
        )
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            if renderer.atlas:
                image = renderer.atlas.render_pillow(grid)
            else:
                rendered = renderer.render(grid, cell_pixels=cell_pixels)
                image = Image.frombytes(
                    "RGB", (rendered.width, rendered.height), bytes(rendered.pixels)
                )
            temporary = path.with_suffix(".tmp.png")
            image.save(temporary, "PNG", compress_level=1)
            temporary.replace(path)
        self.map_path = path
        return {
            "available": True,
            "width": int(grid.width) * cell_pixels,
            "height": int(grid.height) * cell_pixels,
            "cellPixels": cell_pixels,
            "bounds": list(grid.visible_bounds),
            "worlds": sorted(save.world_maps),
            "image": path.resolve().as_uri(),
            "generated3d": False,
            "needsCapture": False,
            "preview": True,
        }

    def ensure_companion_detailed(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Build the bounded high-quality 2D companion map once per terrain.

        This uses the installed high-resolution tile previews and the exact
        top-down atlas; it never expands the complete 3D world.  A matching
        legacy terrain render can be adopted after a low-frequency image
        comparison proves that it belongs to the same terrain layout.
        """
        save = self._require_save()
        grid = save.terrain_grid
        if not grid:
            raise RuntimeError(save.terrain_error or "Terrain unavailable")
        if not self.game_root:
            raise RuntimeError("Scrap Mechanic installation was not found")

        max_side = max(4096, min(8192, int(payload.get("maxSide") or 4096)))
        generated = self._generated_3d_map_info(
            max_side=max_side,
            variant="companion-detailed-r1",
        )
        cell_pixels = int(generated["cellPixels"])
        path = CACHE_DIR / "companion-detailed" / (
            f"{generated['path'].stem}-terrain-r{MAP_RENDER_REVISION}.png"
        )
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            preview = self.ensure_companion_preview({"maxSide": 4096})
            preview_path = Path(unquote(urlparse(str(preview["image"])).path.lstrip("/")))

            # Earlier editor versions keyed this same deterministic render by
            # the database timestamp. Autosaves changed that name despite the
            # terrain being identical. Reuse one only after comparing its
            # blurred layout with this seed's exact atlas.
            adopted: Path | None = None
            expected_size = (int(grid.width) * cell_pixels, int(grid.height) * cell_pixels)
            candidates = sorted(
                CACHE_DIR.glob(
                    f"{save.path.stem}-*-terrain-r{MAP_RENDER_REVISION}-{cell_pixels}px.png"
                ),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
            with Image.open(preview_path) as opened_preview:
                reference = opened_preview.convert("RGB").resize(
                    (256, 192), Image.Resampling.LANCZOS
                ).filter(ImageFilter.GaussianBlur(2.0))
            for candidate in candidates:
                try:
                    with Image.open(candidate) as opened_candidate:
                        if opened_candidate.size != expected_size:
                            continue
                        comparison = opened_candidate.convert("RGB").resize(
                            reference.size, Image.Resampling.LANCZOS
                        ).filter(ImageFilter.GaussianBlur(2.0))
                    mean_error = sum(
                        ImageStat.Stat(ImageChops.difference(reference, comparison)).mean
                    ) / 3.0
                    if mean_error <= 10.0:
                        adopted = candidate
                        break
                except (OSError, ValueError):
                    continue

            temporary = path.with_suffix(".tmp.png")
            if adopted is not None:
                shutil.copyfile(adopted, temporary)
            else:
                TerrainRenderer(self.game_root).render_detailed_png(
                    temporary,
                    grid,
                    cell_pixels=cell_pixels,
                )
            temporary.replace(path)
        self.map_path = path
        return {
            "available": True,
            "width": int(grid.width) * cell_pixels,
            "height": int(grid.height) * cell_pixels,
            "cellPixels": cell_pixels,
            "bounds": list(grid.visible_bounds),
            "worlds": sorted(save.world_maps),
            "image": path.resolve().as_uri(),
            "generated3d": False,
            "needsCapture": False,
            "preview": False,
            "quality": "detailed",
        }

    def _generated_3d_map_info(
        self,
        *,
        max_side: int | None = None,
        variant: str = "",
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Describe the editor-owned orthographic map cache for this save.

        The image deliberately lives outside Scrap Mechanic's save tree.  Its
        key follows the generated terrain rather than the database timestamp.
        Normal autosaves therefore keep using the already-rendered map, while
        a different seed or changed cell layout receives its own capture.
        """
        save = self._require_save()
        grid = save.terrain_grid
        if not grid:
            raise RuntimeError(save.terrain_error or "Terrain unavailable")
        requested_max_side = max(
            1024,
            min(GENERATED_3D_MAP_MAX_SIDE, int(max_side or GENERATED_3D_MAP_MAX_SIDE)),
        )
        safe_variant = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(variant)).strip("-")
        largest_cells = max(1, int(grid.width), int(grid.height))
        cell_pixels = max(
            MAP_CELL_PIXELS,
            min(64, requested_max_side // largest_cells),
        )
        width = int(grid.width) * cell_pixels
        height = int(grid.height) * cell_pixels
        terrain_hash = hashlib.sha1()
        terrain_hash.update(str(int(grid.seed)).encode("ascii"))
        terrain_hash.update(repr(tuple(grid.visible_bounds)).encode("ascii"))
        for cell in sorted(grid.cells.values(), key=lambda item: (item.y, item.x)):
            terrain_hash.update(
                (
                    f"{cell.x},{cell.y},{cell.uuid},{cell.rotation},"
                    f"{cell.x_offset},{cell.y_offset},{cell.flags},{cell.group_id};"
                ).encode("ascii")
            )
        map_revision = int(revision or GENERATED_3D_MAP_REVISION)
        signature = "|".join((
            str(int(grid.seed)),
            terrain_hash.hexdigest(),
            str(map_revision),
            str(cell_pixels),
            safe_variant,
            str(self.game_root.resolve() if self.game_root else ""),
        ))
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:24]
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", save.path.stem).strip("-") or "world"
        variant_suffix = f"-{safe_variant}" if safe_variant else ""
        path = CACHE_DIR / "generated-3d" / (
            f"{safe_stem}-{digest}{variant_suffix}-{cell_pixels}px.png"
        )
        return {
            "path": path,
            "width": width,
            "height": height,
            "cellPixels": cell_pixels,
            "revision": map_revision,
        }

    def _migrate_previous_generated_map(
        self,
        generated: dict[str, Any],
        *,
        max_side: int,
        variant: str,
    ) -> bool:
        """Water-correct the previous 3D capture without rebuilding its world."""
        if GENERATED_3D_MAP_REVISION != 11 or self.game_root is None:
            return False
        previous = self._generated_3d_map_info(
            max_side=max_side,
            variant=variant,
            revision=10,
        )
        old_path = Path(previous["path"])
        new_path = Path(generated["path"])
        if not old_path.is_file():
            return False
        try:
            save = self._require_save()
            grid = save.terrain_grid
            if grid is None:
                return False
            with Image.open(old_path) as old_image:
                if old_image.size != (generated["width"], generated["height"]):
                    return False
                capture = old_image.convert("RGB")
            renderer = TerrainRenderer(self.game_root)
            if renderer.atlas is None:
                return False
            corrected = restore_exact_atlas_water(
                capture,
                renderer.atlas.render_pillow(grid),
            )
            new_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = new_path.with_suffix(".tmp")
            corrected.save(temporary, format="PNG", compress_level=4)
            temporary.replace(new_path)
            return True
        except (OSError, ValueError, RuntimeError):
            return False

    def store_3d_map(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate, water-correct, and atomically persist a generated PNG."""
        info = self._generated_3d_map_info(
            max_side=int(payload.get("maxSide") or GENERATED_3D_MAP_MAX_SIDE),
            variant=str(payload.get("variant") or ""),
        )
        encoded = str(payload.get("data") or "")
        prefix = "data:image/png;base64,"
        if not encoded.startswith(prefix):
            raise ValueError("The generated map was not a PNG image")
        if len(encoded) > 220 * 1024 * 1024:
            raise ValueError("The generated map image is unexpectedly large")
        try:
            raw = base64.b64decode(encoded[len(prefix):], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("The generated map PNG is invalid") from exc
        if len(raw) < 32 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("The generated map PNG is invalid")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (info["width"], info["height"]):
                raise ValueError(
                    f"Expected an {info['width']} x {info['height']} PNG map"
                )
            capture = image.convert("RGB")
        # The installed atlas resolves water at the pixel level after tile
        # rotation, so it supplies the missing shoreline information without
        # replacing any of the detailed 3D land/object render.
        exact_atlas = self._exact_map_water_atlas()
        if exact_atlas is not None:
            capture = restore_exact_atlas_water(capture, exact_atlas)
        encoded_png = io.BytesIO()
        capture.save(encoded_png, format="PNG", compress_level=4)
        raw = encoded_png.getvalue()
        path = info["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
        self.map_path = path
        self._map_water_atlas = None
        return self.map_meta(payload)

    def _exact_map_water_atlas(self) -> Image.Image | None:
        """Build the save's atlas once while a generated map is being captured."""
        if self._map_water_atlas is not None:
            return self._map_water_atlas
        save = self._require_save()
        grid = save.terrain_grid
        if self.game_root is None or grid is None:
            return None
        renderer = TerrainRenderer(self.game_root)
        if renderer.atlas is None:
            return None
        self._map_water_atlas = renderer.atlas.render_pillow(grid).convert("RGB")
        return self._map_water_atlas

    def store_3d_map_tile(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist one high-resolution zoom tile without decoding a huge world PNG."""
        info = self._generated_3d_map_info(
            max_side=int(payload.get("maxSide") or GENERATED_3D_MAP_MAX_SIDE),
            variant=str(payload.get("variant") or ""),
        )
        column = int(payload.get("column") or 0)
        row = int(payload.get("row") or 0)
        columns = int(payload.get("columns") or 0)
        rows = int(payload.get("rows") or 0)
        width = int(payload.get("width") or 0)
        height = int(payload.get("height") or 0)
        display_x = int(payload.get("displayX") or 0)
        display_y = int(payload.get("displayY") or 0)
        display_width = int(payload.get("displayWidth") or 0)
        display_height = int(payload.get("displayHeight") or 0)
        scale = max(1, min(2, int(payload.get("scale") or 1)))
        if (
            columns < 1 or rows < 1 or not (0 <= column < columns) or not (0 <= row < rows)
            or width < 1 or height < 1 or width > 4096 or height > 4096
            or display_width < 1 or display_height < 1
            or display_x < 0 or display_y < 0
            or display_x + display_width > info["width"]
            or display_y + display_height > info["height"]
        ):
            raise ValueError("The generated zoom tile geometry is invalid")
        encoded = str(payload.get("data") or "")
        prefix = "data:image/png;base64,"
        if not encoded.startswith(prefix) or len(encoded) > 100 * 1024 * 1024:
            raise ValueError("The generated zoom tile was not a valid PNG")
        try:
            raw = base64.b64decode(encoded[len(prefix):], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("The generated zoom tile PNG is invalid") from exc
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise ValueError(f"Expected a {width} x {height} zoom tile")
            capture = image.convert("RGB")

        atlas = self._exact_map_water_atlas()
        if atlas is not None:
            crop = atlas.crop((
                round(column / columns * atlas.width),
                round(row / rows * atlas.height),
                round((column + 1) / columns * atlas.width),
                round((row + 1) / rows * atlas.height),
            ))
            capture = restore_exact_atlas_water(capture, crop)

        tile_root = info["path"].parent / f"{info['path'].stem}-detail-{scale}x"
        tile_root.mkdir(parents=True, exist_ok=True)
        tile_path = tile_root / f"r{row:02d}-c{column:02d}.png"
        temporary = tile_path.with_suffix(".tmp")
        capture.save(temporary, format="PNG", compress_level=3)
        temporary.replace(tile_path)

        manifest_path = tile_root / "manifest.json"
        manifest: dict[str, Any] = {}
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                manifest = value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        if (
            manifest.get("columns") != columns
            or manifest.get("rows") != rows
            or manifest.get("scale") != scale
            or manifest.get("revision") != info["revision"]
        ):
            manifest = {
                "columns": columns,
                "rows": rows,
                "scale": scale,
                "revision": info["revision"],
                "tiles": {},
            }
        tiles = manifest.setdefault("tiles", {})
        tiles[f"{column}:{row}"] = {
            "column": column, "row": row, "file": tile_path.name,
            "width": width, "height": height,
            "x": display_x, "y": display_y,
            "displayWidth": display_width, "displayHeight": display_height,
        }
        manifest["ready"] = len(tiles) == columns * rows
        manifest["revision"] = info["revision"]
        manifest_tmp = manifest_path.with_suffix(".tmp")
        manifest_tmp.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
        manifest_tmp.replace(manifest_path)
        return {"stored": True, "ready": bool(manifest["ready"]), "tile": f"{column}:{row}"}

    def finalize_3d_map_tiles(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Assemble streamed 1x capture tiles without sending an 8K data URL."""
        info = self._generated_3d_map_info(
            max_side=int(payload.get("maxSide") or GENERATED_3D_MAP_MAX_SIDE),
            variant=str(payload.get("variant") or ""),
        )
        tile_root = info["path"].parent / f"{info['path'].stem}-detail-1x"
        manifest_path = tile_root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("The generated map tiles are incomplete") from exc
        columns = int(manifest.get("columns") or 0)
        rows = int(manifest.get("rows") or 0)
        entries = manifest.get("tiles") or {}
        if (
            not manifest.get("ready")
            or int(manifest.get("scale") or 0) != 1
            or int(manifest.get("revision") or -1) != info["revision"]
            or columns < 1
            or rows < 1
            or len(entries) != columns * rows
        ):
            raise ValueError("The generated map tiles are incomplete")

        overview = Image.new("RGB", (info["width"], info["height"]))
        covered: set[tuple[int, int]] = set()
        for row in range(rows):
            for column in range(columns):
                entry = entries.get(f"{column}:{row}")
                if not isinstance(entry, dict):
                    raise ValueError("The generated map tile manifest is invalid")
                file_name = str(entry.get("file") or "")
                if Path(file_name).name != file_name:
                    raise ValueError("The generated map tile manifest is invalid")
                x = int(entry.get("x") or 0)
                y = int(entry.get("y") or 0)
                display_width = int(entry.get("displayWidth") or 0)
                display_height = int(entry.get("displayHeight") or 0)
                expected_x = round(column / columns * info["width"])
                expected_y = round(row / rows * info["height"])
                expected_right = round((column + 1) / columns * info["width"])
                expected_bottom = round((row + 1) / rows * info["height"])
                if (
                    (x, y) != (expected_x, expected_y)
                    or display_width != expected_right - expected_x
                    or display_height != expected_bottom - expected_y
                ):
                    raise ValueError("The generated map tile geometry is invalid")
                tile_path = tile_root / file_name
                with Image.open(tile_path) as tile_image:
                    if tile_image.size != (display_width, display_height):
                        raise ValueError("The generated map tile size is invalid")
                    overview.paste(tile_image.convert("RGB"), (x, y))
                covered.add((column, row))
        if len(covered) != columns * rows:
            raise ValueError("The generated map tiles are incomplete")

        path = info["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        overview.save(temporary, format="PNG", compress_level=4)
        temporary.replace(path)
        self.map_path = path
        self._map_water_atlas = None
        return self.map_meta(payload)

    @staticmethod
    def _detail_tile_meta(generated: dict[str, Any]) -> dict[str, Any] | None:
        tile_roots = sorted(generated["path"].parent.glob(f"{generated['path'].stem}-detail-*x"))
        for tile_root in reversed(tile_roots):
            manifest_path = tile_root / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                columns = int(manifest.get("columns") or 0)
                rows = int(manifest.get("rows") or 0)
                entries = manifest.get("tiles") or {}
                if (
                    not manifest.get("ready")
                    or int(manifest.get("revision") or -1) != generated["revision"]
                    or len(entries) != columns * rows
                ):
                    continue
                tiles = []
                for entry in entries.values():
                    tile_path = tile_root / str(entry.get("file") or "")
                    if not tile_path.is_file():
                        raise FileNotFoundError(tile_path)
                    tiles.append({**entry, "image": tile_path.resolve().as_uri()})
                return {
                    "ready": True,
                    "scale": int(manifest.get("scale") or 2),
                    "columns": columns,
                    "rows": rows,
                    "tiles": sorted(tiles, key=lambda tile: (tile["row"], tile["column"])),
                }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return None

    def map_meta(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        grid = save.terrain_grid
        if not grid:
            return {"available": False, "error": save.terrain_error or "Terrain unavailable"}
        generated = self._generated_3d_map_info(
            max_side=int(payload.get("maxSide") or GENERATED_3D_MAP_MAX_SIDE),
            variant=str(payload.get("variant") or ""),
        )
        image = generated["path"]
        # The editor and companion share finished captures of the same terrain.
        # The old companion-only variant forced a second full 3D render.
        if not image.is_file():
            alternate = self._generated_3d_map_info(
                max_side=8192,
                variant="" if payload.get("variant") else "companion-hq-v2",
            )
            if alternate["path"].is_file():
                generated = alternate
                image = generated["path"]
        if not image.is_file() and payload.get("migrate"):
            self._migrate_previous_generated_map(
                generated,
                max_side=int(payload.get("maxSide") or GENERATED_3D_MAP_MAX_SIDE),
                variant=str(payload.get("variant") or ""),
            )
        ready = image.is_file()
        detail_tiles = self._detail_tile_meta(generated) if ready else None
        if not ready:
            preview = self.ensure_companion_preview({"maxSide": 4096})
            return {
                **preview,
                "needsCapture": True,
                "quality": "preview",
                "capture": {
                    "width": generated["width"],
                    "height": generated["height"],
                    "revision": generated["revision"],
                },
            }
        return {
            "available": True,
            "width": generated["width"],
            "height": generated["height"],
            "cellPixels": generated["cellPixels"],
            "bounds": list(grid.visible_bounds),
            "worlds": sorted(save.world_maps),
            "image": image.resolve().as_uri() if ready else "",
            "generated3d": ready,
            "needsCapture": not ready,
            "detailTiles": detail_tiles,
            "capture": {
                "width": generated["width"],
                "height": generated["height"],
                "revision": generated["revision"],
            },
        }

    def _build_features(self, save: SaveData) -> list[MapFeature]:
        features: list[MapFeature] = []
        for index, player in enumerate(save.players):
            features.append(MapFeature(
                key=f"player-{player.steam_id}",
                label="You" if index == 0 else f"Player {index + 1}",
                kind="Player",
                world_id=player.world_id,
                x=player.x / 64.0,
                y=player.y / 64.0,
                cells=1,
            ))
        if save.beacon_state and isinstance(save.beacon_state.value, dict):
            beacons = save.beacon_state.value.get("beacons", {})
            if isinstance(beacons, dict):
                for beacon_id, beacon in beacons.items():
                    if not isinstance(beacon, dict):
                        continue
                    position, world = beacon.get("position"), beacon.get("world")
                    if not (isinstance(position, tuple) and position[:1] == ("vec3",)):
                        continue
                    world_id = int(world[2]) if isinstance(world, tuple) and world[:1] == ("reference",) else 1
                    features.append(MapFeature(
                        key=f"beacon-{beacon_id}",
                        label=f"Player beacon {beacon_id}",
                        kind="Landmark",
                        world_id=world_id,
                        x=float(position[1]) / 64.0,
                        y=float(position[2]) / 64.0,
                    ))
        grid = save.terrain_grid
        if grid:
            grouped: dict[tuple[Any, ...], list[tuple[Any, str, str]]] = {}
            for cell in grid.cells.values():
                metadata = self.tile_names.get(cell.uuid, {})
                raw_name = str(metadata.get("name") or cell.uuid)
                searchable = friendly_name(raw_name).casefold()
                if "warehouse" in searchable:
                    kind = "Warehouse"
                elif "packing station" in searchable:
                    kind = "Packing station"
                elif "mechanic station" in searchable or "schematic station" in searchable:
                    kind = "Station"
                elif any(word in searchable for word in ("growlab", "minidungeon", "dungeon", "excavation", "vault")):
                    kind = "Lab / dungeon"
                elif "quest" in searchable or "bunk investigation" in searchable:
                    kind = "Quest"
                elif any(word in searchable for word in (
                    "crashed ship", "hideout", "farmbot graveyard", "ruin city",
                    "hay bale labyrinth", "camping spot water front",
                )):
                    kind = "Landmark"
                else:
                    continue
                if cell.group_id > 0:
                    key = (cell.uuid, cell.group_id)
                else:
                    key = (cell.uuid, cell.x, cell.y)
                grouped.setdefault(key, []).append((cell, raw_name, kind))
            for index, entries in enumerate(grouped.values()):
                cells = [entry[0] for entry in entries]
                features.append(MapFeature(
                    key=f"terrain-{index}", label=friendly_name(entries[0][1]), kind=entries[0][2], world_id=1,
                    x=sum(cell.x for cell in cells) / len(cells), y=sum(cell.y for cell in cells) / len(cells),
                    uuid=cells[0].uuid, cells=len(cells),
                ))
        return features
