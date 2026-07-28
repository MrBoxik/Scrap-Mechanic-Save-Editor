from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import json
import os
import re
import threading
from typing import Any

from scrap_mechanic_save_editor import (
    EMPTY_UUID,
    CatalogItem,
    ItemCatalog,
    MapFeature,
    SaveData,
    default_save_root,
    find_game_root,
    scan_survival_saves,
)
from terrain_map import LuaArray, TerrainRenderer, load_terrain_tile_names
from .game_unlocks import (
    CustomizationUnlockProfile,
    load_customization_catalog,
    load_log_catalog,
    load_quest_catalog,
    load_recipe_catalog,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent
ASSET_DIR = PROJECT_DIR / "assets"
CACHE_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ScrapMechanicSaveEditor" / "map-cache"


def friendly_name(value: str) -> str:
    value = re.sub(r"^(obj|blk|jnt|tool|hvs|poi|tile)_", "", value, flags=re.I)
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    return re.sub(r"\s+", " ", value).strip().title() or "Unknown"


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

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.game_root = find_game_root()
        self.catalog = ItemCatalog()
        if self.game_root:
            self.catalog.load(self.game_root)
        self.quest_catalog = load_quest_catalog(self.game_root, self.catalog) if self.game_root else []
        self.recipe_catalog = load_recipe_catalog(self.game_root, self.catalog) if self.game_root else []
        self.log_catalog = load_log_catalog(self.game_root) if self.game_root else []
        self.customization_catalog = load_customization_catalog(self.game_root) if self.game_root else []
        self.icon_atlas = self._load_icon_atlas()
        self.tile_names = load_terrain_tile_names(self.game_root) if self.game_root else {}
        self.save: SaveData | None = None
        self.features: list[MapFeature] = []
        self.map_path: Path | None = None
        self.customization_profile: CustomizationUnlockProfile | None = None

    def dispatch(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, method.replace("-", "_"), None)
        if not callable(handler) or method.startswith("_"):
            raise ValueError(f"Unknown API method: {method}")
        with self.lock:
            result = handler(payload)
        return result if isinstance(result, dict) else {"result": result}

    def status(self, _payload: dict[str, Any]) -> dict[str, Any]:
        saves = []
        for path in scan_survival_saves():
            stat = path.stat()
            saves.append({"path": str(path), "name": path.stem, "modified": stat.st_mtime, "size": stat.st_size})
        return {
            "version": "4.3.0",
            "gameRoot": str(self.game_root) if self.game_root else None,
            "catalogCount": len(self.catalog.items),
            "catalogError": self.catalog.load_error or None,
            "saves": saves,
            "current": self._save_summary() if self.save else None,
        }

    def load_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(payload.get("path") or "")).expanduser().resolve()
        self.save = SaveData(path)
        self.customization_profile = self._load_customization_profile(self.save)
        self.features = self._build_features(self.save)
        self.map_path = None
        return {"current": self._save_summary(), "containers": self._containers()}

    @staticmethod
    def _load_customization_profile(save: SaveData) -> CustomizationUnlockProfile | None:
        steam_id = save.players[0].steam_id if save.players else 0
        if steam_id <= 0:
            return None
        user_dir = next((parent for parent in (save.path.parent, *save.path.parents) if parent.name == f"User_{steam_id}"), None)
        if user_dir is None:
            candidate = default_save_root() / f"User_{steam_id}"
            user_dir = candidate if candidate.is_dir() else None
        if user_dir is None:
            return None
        return CustomizationUnlockProfile.load(user_dir / "unlock", steam_id)

    def _require_save(self) -> SaveData:
        if self.save is None:
            raise RuntimeError("Load a Survival save first")
        return self.save

    def _save_summary(self) -> dict[str, Any]:
        save = self._require_save()
        summary = save.summary()
        summary["dirty"] = save.dirty
        summary["fileName"] = save.path.name
        summary["featureCount"] = len(self.features)
        return summary

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
                    "perks": len(stats.get("perks", {})) if isinstance(stats.get("perks"), dict) else 0,
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
        }

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
        clothes = [{**definition, "unlocked": definition["uuid"] in unlocked_clothes} for definition in self.customization_catalog]
        return {
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

    def stage_all_clothing(self, _payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.customization_profile
        if profile is None:
            raise RuntimeError("The Steam user's clothing unlock profile was not found")
        changed = sum(1 for item in self.customization_catalog if profile.unlock(item["uuid"]))
        return {"changed": changed, "state": self.unlocks_state({})}

    def save_clothing(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.customization_profile
        if profile is None:
            raise RuntimeError("The Steam user's clothing unlock profile was not found")
        if payload.get("confirmation") != "APPLY CLOTHING":
            raise ValueError("Clothing confirmation was not supplied")
        backup = profile.save()
        return {"path": str(profile.path), "backup": str(backup) if backup else None, "state": self.unlocks_state({})}

    def save_copy(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        destination = str(payload.get("path") or save.path.with_name(f"{save.path.stem}_edited.db"))
        written, backup = save.save_copy(destination)
        return {"path": str(written), "backup": str(backup) if backup else None}

    def overwrite(self, payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        if payload.get("confirmation") != "OVERWRITE":
            raise ValueError("Overwrite confirmation was not supplied")
        written, backup = save.save_copy(save.path, overwrite_source=True)
        self.save = SaveData(written)
        self.customization_profile = self._load_customization_profile(self.save)
        self.features = self._build_features(self.save)
        self.map_path = None
        return {
            "path": str(written),
            "backup": str(backup) if backup else None,
            "current": self._save_summary(),
            "containers": self._containers(),
        }

    def map_features(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").casefold().split()
        kind = str(payload.get("kind") or "Places")
        world_id = int(payload.get("worldId") or 1)
        features = [feature for feature in self.features if feature.world_id == world_id]
        if kind not in {"All", "Places"}:
            features = [feature for feature in features if feature.kind == kind]
        if query:
            features = [feature for feature in features if all(word in feature.search_text for word in query)]
        features.sort(key=lambda feature: (feature.label.casefold(), feature.x, feature.y))
        total = len(features)
        limit = min(2000, max(1, int(payload.get("limit") or 500)))
        return {"features": [asdict(feature) for feature in features[:limit]], "total": total}

    def ensure_map_image(self) -> Path:
        with self.lock:
            save = self._require_save()
            if not save.terrain_grid:
                raise RuntimeError(save.terrain_error or "This save has no decoded Survival terrain")
            CACHE_DIR.mkdir(exist_ok=True)
            stamp = int(save.path.stat().st_mtime_ns)
            output = CACHE_DIR / f"{save.path.stem}-{stamp}-terrain.png"
            if not output.is_file():
                if not self.game_root:
                    raise RuntimeError("Scrap Mechanic installation was not found")
                TerrainRenderer(self.game_root).render_png(output, save.terrain_grid, cell_pixels=16)
            self.map_path = output
            return output

    def map_meta(self, _payload: dict[str, Any]) -> dict[str, Any]:
        save = self._require_save()
        grid = save.terrain_grid
        if not grid:
            return {"available": False, "error": save.terrain_error or "Terrain unavailable"}
        image = self.ensure_map_image()
        return {
            "available": True,
            "width": grid.width * 16,
            "height": grid.height * 16,
            "cellPixels": 16,
            "bounds": list(grid.visible_bounds),
            "worlds": sorted(save.world_maps),
            "image": image.resolve().as_uri(),
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
