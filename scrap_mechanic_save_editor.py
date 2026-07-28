#!/usr/bin/env python3
"""Safe, dependency-free Scrap Mechanic Survival save editor.

The database format is SQLite, but many table payloads are proprietary binary
blobs.  This program edits only the fields whose layouts have been validated:

* the player inventory container (22-byte slots),
* the embedded world settings JSON,
* selected Game table integers (behind an advanced warning), and
* a detailed Survival terrain map decoded from ScriptData, plus saved objects.

Unknown bytes are preserved byte-for-byte.  Overwriting a save always creates
a timestamped backup first.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
import zlib
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Iterable

from terrain_map import (
    BitReader,
    LUA_HEADER,
    LuaArray,
    TerrainDecodeError,
    TerrainGrid,
    TerrainRenderer,
    decode_terrain_grid,
    decompress_lz4_block,
    load_terrain_tile_names,
    parse_lua_value,
    unwrap_script_data,
    wrap_script_data,
)


APP_TITLE = "Scrap Mechanic Survival Save Editor"
APP_VERSION = "3.0"
EMPTY_UUID = "00000000-0000-0000-0000-000000000000"
SLOT_SIZE = 22
CONTAINER_HEADER_SIZE = 11

DEFAULT_GAME_PATHS = (
    Path(r"E:\SteamLibrary\steamapps\common\Scrap Mechanic"),
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Scrap Mechanic"),
    Path(r"C:\Program Files\Steam\steamapps\common\Scrap Mechanic"),
)

DIFFICULTY_LABELS = {0: "Easy", 1: "Normal"}
FRIENDLY_TOOL_NAMES = {
    "Sledgehammer": "Sledgehammer",
    "SurvivalLift": "Lift",
    "ConnectTool": "Connect Tool",
    "PaintTool": "Paint Tool",
    "WeldTool": "Weld Tool",
    "SurvivalSpudgun": "Spud Gun",
    "SurvivalShotgun": "Spud Shotgun",
    "SurvivalGatling": "Spud Gatling Gun",
}


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def strip_json_comments(text: str) -> str:
    """Remove whole-line and trailing // comments used by game JSON files.

    This small state machine leaves // inside quoted strings untouched.
    """
    out: list[str] = []
    for line in text.splitlines():
        quoted = False
        escaped = False
        cut = len(line)
        for index, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == "\\" and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif char == "/" and not quoted and index + 1 < len(line) and line[index + 1] == "/":
                cut = index
                break
        out.append(line[:cut])
    return "\n".join(out)


def load_game_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig")
    return json.loads(strip_json_comments(text))


def is_game_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "Survival" / "Gui" / "Language" / "English" / "inventoryDescriptions.json").is_file()
    )


def steam_library_candidates() -> Iterable[Path]:
    yielded: set[str] = set()

    def emit(path: Path) -> Iterable[Path]:
        key = str(path).casefold()
        if key not in yielded:
            yielded.add(key)
            yield path

    for candidate in DEFAULT_GAME_PATHS:
        yield from emit(candidate)

    steam_roots: list[Path] = []
    try:
        import winreg  # type: ignore

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key_name in (r"Software\Valve\Steam", r"Software\WOW6432Node\Valve\Steam"):
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        value, _ = winreg.QueryValueEx(key, "SteamPath" if hive == winreg.HKEY_CURRENT_USER else "InstallPath")
                        steam_roots.append(Path(value))
                except OSError:
                    pass
    except (ImportError, OSError):
        pass

    for root in steam_roots:
        yield from emit(root / "steamapps" / "common" / "Scrap Mechanic")
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r'"path"\s+"([^"]+)"', text):
                library = Path(match.group(1).replace(r"\\", "\\"))
                yield from emit(library / "steamapps" / "common" / "Scrap Mechanic")
        except OSError:
            pass


def find_game_root() -> Path | None:
    for candidate in steam_library_candidates():
        if is_game_root(candidate):
            return candidate
    return None


def default_save_root() -> Path:
    appdata = os.getenv("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Axolot Games" / "Scrap Mechanic" / "User"


def scan_survival_saves() -> list[Path]:
    root = default_save_root()
    if not root.is_dir():
        return []
    saves = [path for path in root.glob("User_*/Save/Survival/*.db") if path.is_file()]
    return sorted(saves, key=lambda path: path.stat().st_mtime, reverse=True)


@dataclass
class CatalogItem:
    uuid: str
    title: str
    description: str = ""
    internal_name: str = ""
    category: str = "Item"
    stack_size: int | None = None
    icon_path: Path | None = None
    icon_atlas_path: Path | None = None
    icon_point: tuple[int, int] | None = None
    icon_size: tuple[int, int] = (96, 96)

    @property
    def search_text(self) -> str:
        return " ".join((self.title, self.description, self.uuid, self.internal_name, self.category)).casefold()


class ItemCatalog:
    def __init__(self) -> None:
        self.items: dict[str, CatalogItem] = {}
        self.game_root: Path | None = None
        self.load_error = ""

    def get(self, uuid_text: str) -> CatalogItem | None:
        return self.items.get(uuid_text.casefold())

    def display_name(self, uuid_text: str) -> str:
        if uuid_text.casefold() == EMPTY_UUID:
            return "<Empty>"
        item = self.get(uuid_text)
        return item.title if item else f"Unknown ({uuid_text})"

    def load(self, game_root: Path) -> None:
        self.items.clear()
        self.game_root = game_root
        self.load_error = ""
        descriptions = game_root / "Survival" / "Gui" / "Language" / "English" / "inventoryDescriptions.json"
        try:
            raw_descriptions = load_game_json(descriptions)
            for uuid_text, info in raw_descriptions.items():
                try:
                    normalized = str(uuid.UUID(uuid_text))
                except ValueError:
                    continue
                title = str(info.get("title") or normalized)
                self.items[normalized] = CatalogItem(
                    uuid=normalized,
                    title=title,
                    description=str(info.get("description") or ""),
                )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.load_error = f"Could not read item descriptions: {exc}"

        definition_roots = (
            game_root / "Data" / "Objects" / "Database" / "ShapeSets",
            game_root / "Survival" / "Objects" / "Database" / "ShapeSets",
        )
        for root in definition_roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*.shapeset"):
                self._merge_definition_file(path, "Shape")

        tool_roots = (
            game_root / "Data" / "Tools" / "ToolSets",
            game_root / "Survival" / "Tools" / "ToolSets",
        )
        for root in tool_roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*.json"):
                self._merge_definition_file(path, "Tool")

        for item in self.items.values():
            if item.title.startswith(("obj_", "blk_", "jnt_", "tool_")) and item.internal_name:
                item.title = self._title_from_internal(item.internal_name)

        icon_roots = (
            game_root / "Survival" / "Gui" / "AssetIcons",
            game_root / "Survival" / "Gui" / "VoxelMeshIcons",
            game_root / "Data" / "Gui" / "AssetIcons",
            game_root / "ChallengeData" / "Gui" / "AssetIcons",
        )
        for root in icon_roots:
            if not root.is_dir():
                continue
            for path in root.glob("*.png"):
                try:
                    normalized = str(uuid.UUID(path.stem))
                except ValueError:
                    continue
                item = self.items.get(normalized)
                if item and item.icon_path is None:
                    item.icon_path = path

        for relative in (
            Path("Survival/Gui/IconMapSurvival.xml"),
            Path("Data/Gui/IconMap.xml"),
            Path("ChallengeData/Gui/IconMapChallenge.xml"),
        ):
            self._merge_icon_map(game_root / relative)
        self._merge_bundled_editor_icons()

    @staticmethod
    def _title_from_internal(name: str) -> str:
        text = re.sub(r"^(obj|blk|jnt|tool|hvs)_", "", name, flags=re.I)
        text = text.replace("_", " ")
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
        return text.title()

    def _merge_definition_file(self, path: Path, category: str) -> None:
        try:
            data = load_game_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return
        for node in self._walk(data):
            uuid_text = node.get("uuid")
            if not isinstance(uuid_text, str):
                continue
            try:
                normalized = str(uuid.UUID(uuid_text))
            except ValueError:
                continue
            internal = str(node.get("name") or "")
            script_class = ""
            if isinstance(node.get("script"), dict):
                script_class = str(node["script"].get("class") or "")
            title = FRIENDLY_TOOL_NAMES.get(script_class) or self._title_from_internal(internal or script_class or normalized)
            item = self.items.get(normalized)
            if item is None:
                item = CatalogItem(uuid=normalized, title=title)
                self.items[normalized] = item
            item.internal_name = internal or item.internal_name
            item.category = category
            if isinstance(node.get("stackSize"), int):
                item.stack_size = int(node["stackSize"])
            if category == "Tool" and script_class in FRIENDLY_TOOL_NAMES:
                item.title = FRIENDLY_TOOL_NAMES[script_class]

    def _merge_icon_map(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            return
        for group in root.iter("Group"):
            texture = group.get("texture")
            size_text = group.get("size", "96 96").split()
            if not texture or len(size_text) != 2:
                continue
            try:
                icon_size = (int(size_text[0]), int(size_text[1]))
            except ValueError:
                continue
            atlas_path = path.parent / texture
            if not atlas_path.is_file():
                continue
            for index in group.findall("Index"):
                try:
                    normalized = str(uuid.UUID(str(index.get("name"))))
                except ValueError:
                    continue
                frame = index.find("Frame")
                point = frame.get("point", "").split() if frame is not None else []
                if len(point) != 2:
                    continue
                item = self.items.get(normalized)
                if not item:
                    continue
                try:
                    item.icon_point = (int(point[0]), int(point[1]))
                except ValueError:
                    continue
                item.icon_atlas_path = atlas_path
                item.icon_size = icon_size

    def _merge_bundled_editor_icons(self) -> None:
        directory = Path(__file__).resolve().parent / "assets"
        manifest_path = directory / "item-icon-atlas.json"
        atlas_path = directory / "item-icon-atlas.png"
        individual_directory = directory / "item-icons"
        if not manifest_path.is_file() or not atlas_path.is_file():
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            size = int(manifest["cellPixels"])
            icons = manifest["icons"]
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return
        for uuid_text, point in icons.items():
            item = self.items.get(uuid_text)
            if item and isinstance(point, list) and len(point) == 2:
                individual = individual_directory / f"{uuid_text}.png"
                if individual_directory.is_dir():
                    item.icon_path = individual
                    item.icon_atlas_path = None
                    item.icon_point = None
                else:
                    item.icon_atlas_path = atlas_path
                    item.icon_point = (int(point[0]), int(point[1]))
                    item.icon_size = (size, size)

    @classmethod
    def _walk(cls, value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from cls._walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._walk(child)

    def search(self, query: str, limit: int = 500) -> list[CatalogItem]:
        words = query.casefold().split()
        matches = [item for item in self.items.values() if all(word in item.search_text for word in words)]
        matches.sort(key=lambda item: (item.title.casefold(), item.uuid))
        return matches[:limit]


@dataclass
class InventorySlot:
    number: int
    uuid: str
    metadata: bytes
    quantity: int

    @classmethod
    def parse(cls, number: int, raw: bytes) -> "InventorySlot":
        if len(raw) != SLOT_SIZE:
            raise ValueError(f"slot {number} has {len(raw)} bytes, expected {SLOT_SIZE}")
        item_uuid = str(uuid.UUID(bytes=raw[:16][::-1]))
        return cls(number, item_uuid, raw[16:20], int.from_bytes(raw[20:22], "big"))

    def to_bytes(self) -> bytes:
        raw = bytearray(SLOT_SIZE)
        raw[:16] = uuid.UUID(self.uuid).bytes[::-1]
        raw[16:20] = self.metadata
        raw[20:22] = self.quantity.to_bytes(2, "big")
        return bytes(raw)

    @property
    def empty(self) -> bool:
        return self.uuid.casefold() == EMPTY_UUID or self.quantity == 0


@dataclass
class ContainerBlob:
    container_id: int
    header: bytes
    slots: list[InventorySlot]
    footer: bytes
    original: bytes
    dirty: bool = False

    @classmethod
    def try_parse(cls, container_id: int, data: bytes) -> "ContainerBlob | None":
        if not isinstance(data, bytes) or len(data) < CONTAINER_HEADER_SIZE:
            return None
        slot_count = int.from_bytes(data[7:9], "big")
        if not 1 <= slot_count <= 512:
            return None
        body_end = CONTAINER_HEADER_SIZE + slot_count * SLOT_SIZE
        if body_end > len(data) or len(data) - body_end > 64:
            return None
        try:
            slots = [
                InventorySlot.parse(
                    index + 1,
                    data[CONTAINER_HEADER_SIZE + index * SLOT_SIZE : CONTAINER_HEADER_SIZE + (index + 1) * SLOT_SIZE],
                )
                for index in range(slot_count)
            ]
        except (ValueError, AttributeError):
            return None
        return cls(container_id, data[:CONTAINER_HEADER_SIZE], slots, data[body_end:], data)

    def to_bytes(self) -> bytes:
        return self.header + b"".join(slot.to_bytes() for slot in self.slots) + self.footer

    def set_slot(self, slot_number: int, uuid_text: str, quantity: int, catalog: ItemCatalog | None = None) -> None:
        if not 1 <= slot_number <= len(self.slots):
            raise ValueError("Invalid slot number")
        normalized = str(uuid.UUID(uuid_text.strip() or EMPTY_UUID))
        quantity = int(quantity)
        if not 0 <= quantity <= 65535:
            raise ValueError("Quantity must be between 0 and 65,535")
        slot = self.slots[slot_number - 1]
        item = catalog.get(normalized) if catalog else None
        uuid_changed = slot.uuid.casefold() != normalized.casefold()
        if item and item.category == "Tool" and slot.uuid.casefold() != normalized.casefold():
            raise ValueError(
                "Tools need a matching Tool-table instance ID. Creating one by UUID alone is unsafe. "
                "Edit quantities only for an existing tool slot."
            )
        if item and item.category == "Tool" and quantity not in (0, 1):
            raise ValueError("A tool slot must have quantity 1. Tool instances cannot be stacked safely.")
        slot.uuid = normalized if quantity else EMPTY_UUID
        slot.quantity = quantity
        if quantity and (uuid_changed or slot.metadata == b"\x00\x00\x00\x00"):
            slot.metadata = b"\xff\xff\xff\xff"
        if not quantity:
            slot.metadata = b"\xff\xff\xff\xff"
        self.dirty = True

    def clear_slot(self, slot_number: int) -> None:
        self.set_slot(slot_number, EMPTY_UUID, 0)


@dataclass
class SettingsBlock:
    rowid: int
    original_blob: bytes
    start: int
    end: int
    values: dict[str, Any]
    original_values: dict[str, Any]

    @property
    def dirty(self) -> bool:
        return self.values != self.original_values

    def to_bytes(self) -> bytes:
        encoded = json.dumps(self.values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.original_blob[: self.start] + encoded + self.original_blob[self.end :]


@dataclass
class MapPoint:
    x: float
    y: float
    weight: int = 1


@dataclass(frozen=True)
class PlayerLocation:
    steam_id: int
    world_id: int
    x: float
    y: float
    z: float
    yaw: float
    pitch: float
    inventory_container_id: int
    carry_container_id: int


@dataclass
class WorldMapData:
    world_id: int
    harvestables: list[MapPoint] = field(default_factory=list)
    units: list[MapPoint] = field(default_factory=list)
    rigid_bodies: list[MapPoint] = field(default_factory=list)
    portals: list[MapPoint] = field(default_factory=list)

    def all_points(self) -> list[MapPoint]:
        return self.harvestables + self.units + self.rigid_bodies + self.portals


@dataclass(frozen=True)
class MapFeature:
    key: str
    label: str
    kind: str
    world_id: int
    x: float
    y: float
    uuid: str = ""
    cells: int = 1

    @property
    def search_text(self) -> str:
        return f"{self.label} {self.kind} {self.uuid} {self.x:g} {self.y:g}".casefold()


@dataclass
class LuaStorageBlock:
    rowid: int
    key: bytes
    original_blob: bytes
    value: Any
    original_value: Any

    @property
    def dirty(self) -> bool:
        return self.value != self.original_value

    def to_bytes(self) -> bytes:
        return wrap_script_data(self.original_blob, self.value)


class SaveData:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).resolve()
        self.game: dict[str, int] = {}
        self.original_game: dict[str, int] = {}
        self.settings: SettingsBlock | None = None
        self.containers: dict[int, ContainerBlob] = {}
        self.inventory: ContainerBlob | None = None
        self.world_maps: dict[int, WorldMapData] = {}
        self.players: list[PlayerLocation] = []
        self.player_states: list[LuaStorageBlock] = []
        self.raid_state: LuaStorageBlock | None = None
        self.progression_state: LuaStorageBlock | None = None
        self.quest_state: LuaStorageBlock | None = None
        self.beacon_state: LuaStorageBlock | None = None
        self.terrain_grid: TerrainGrid | None = None
        self.terrain_error = ""
        self.table_counts: dict[str, int] = {}
        self.integrity = ""
        self._load()

    @staticmethod
    def _readonly_connection(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _load(self) -> None:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        with self.path.open("rb") as stream:
            sqlite_header = stream.read(16)
        if sqlite_header != b"SQLite format 3\x00":
            raise ValueError("This is not a SQLite 3 database")
        connection = self._readonly_connection(self.path)
        try:
            self.integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if self.integrity != "ok":
                raise RuntimeError(f"SQLite quick_check failed: {self.integrity}")
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"Game", "GenericData", "Container"}
            missing = required - tables
            if missing:
                raise RuntimeError(f"Not a supported Scrap Mechanic save; missing: {', '.join(sorted(missing))}")
            for table in ("Container", "RigidBody", "Harvestable", "Unit", "Portal", "Tool", "ScriptData"):
                if table in tables:
                    self.table_counts[table] = int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
            row = connection.execute("SELECT savegameversion, flags, seed, gametick FROM Game LIMIT 1").fetchone()
            if row is None:
                raise RuntimeError("Game table is empty")
            self.game = {key: int(row[key]) for key in row.keys()}
            self.original_game = dict(self.game)
            self.settings = self._find_settings(connection)
            self._load_containers(connection)
            self._load_players(connection)
            if "ScriptData" in tables:
                self._load_script_state(connection)
            self._load_maps(connection, tables)
            if "ScriptData" in tables:
                try:
                    self.terrain_grid = decode_terrain_grid(connection, world_id=1)
                except TerrainDecodeError as exc:
                    self.terrain_error = str(exc)
        finally:
            connection.close()

    @staticmethod
    def _find_settings(connection: sqlite3.Connection) -> SettingsBlock | None:
        decoder = json.JSONDecoder()
        for row in connection.execute("SELECT rowid, data FROM GenericData"):
            blob = row["data"]
            if not isinstance(blob, bytes):
                continue
            for start, byte in enumerate(blob):
                if byte != ord("{"):
                    continue
                try:
                    text = blob[start:].decode("utf-8")
                    value, end_chars = decoder.raw_decode(text)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(value, dict) or "Difficulty" not in value:
                    continue
                end = start + len(text[:end_chars].encode("utf-8"))
                return SettingsBlock(int(row["rowid"]), blob, start, end, dict(value), dict(value))
        return None

    def _load_containers(self, connection: sqlite3.Connection) -> None:
        for row in connection.execute("SELECT id, data FROM Container ORDER BY id"):
            parsed = ContainerBlob.try_parse(int(row["id"]), row["data"])
            if parsed:
                self.containers[parsed.container_id] = parsed
        candidate = self.containers.get(1)
        if candidate and len(candidate.slots) == 40:
            self.inventory = candidate
            return
        forty_slot = [container for container in self.containers.values() if len(container.slots) == 40]
        if len(forty_slot) == 1:
            self.inventory = forty_slot[0]
        elif candidate:
            self.inventory = candidate

    def _load_players(self, connection: sqlite3.Connection) -> None:
        """Read the engine's compressed player record without modifying it.

        Scrap Mechanic 1.0 added 12 bytes before SteamId64. Older saves use
        the original offsets, so select the layout whose inventory ID points
        to a parsed container.
        """
        for row in connection.execute(
            "SELECT data FROM GenericData WHERE worldId=65534 AND flags=3 ORDER BY key"
        ):
            blob = row["data"]
            if not isinstance(blob, bytes) or len(blob) < 30:
                continue
            try:
                payload = decompress_lz4_block(blob[29:])
            except TerrainDecodeError:
                continue
            if len(payload) < 54:
                continue
            world_id = struct.unpack_from(">H", payload, 0)[0]
            z, y, x = struct.unpack_from(">fff", payload, 2)
            yaw, pitch = struct.unpack_from(">ff", payload, 26)
            candidates = (46, 34) if len(payload) >= 66 else (34,)
            parsed = None
            for steam_offset in candidates:
                if len(payload) < steam_offset + 20:
                    continue
                steam_id = int.from_bytes(payload[steam_offset : steam_offset + 8], "big")
                inventory_id, carry_id = struct.unpack_from(">II", payload, steam_offset + 8)
                if inventory_id in self.containers:
                    parsed = (steam_id, inventory_id, carry_id)
                    break
            if parsed is None:
                continue
            self.players.append(
                PlayerLocation(parsed[0], world_id, x, y, z, yaw, pitch, parsed[1], parsed[2])
            )

    @staticmethod
    def _storage_channel_key(channel: int) -> bytes:
        if not 0 <= channel <= 127:
            raise ValueError("Only compact integer storage keys are supported")
        return LUA_HEADER + b"\x08" + bytes([channel])

    def _load_script_state(self, connection: sqlite3.Connection) -> None:
        channel_by_key = {
            self._storage_channel_key(35): "beacons",
            self._storage_channel_key(45): "raids",
            self._storage_channel_key(79): "progression",
        }
        for row in connection.execute(
            "SELECT rowid,key,worldId,flags,data FROM ScriptData "
            "WHERE (worldId=65534 AND flags=3) OR key IN (?,?,?)",
            tuple(channel_by_key),
        ):
            blob, key = row["data"], row["key"]
            if not isinstance(blob, bytes) or not isinstance(key, bytes):
                continue
            try:
                raw = unwrap_script_data(blob)
                value = parse_lua_value(BitReader(raw[len(LUA_HEADER) :]), preserve_keys=True)
            except (TerrainDecodeError, UnicodeDecodeError, ValueError, TypeError):
                continue
            block = LuaStorageBlock(int(row["rowid"]), key, blob, value, copy.deepcopy(value))
            kind = channel_by_key.get(key)
            if kind == "raids":
                self.raid_state = block
            elif kind == "progression":
                self.progression_state = block
            elif kind == "beacons":
                self.beacon_state = block
            elif isinstance(value, dict) and isinstance(value.get("stats"), dict) and "isConscious" in value:
                self.player_states.append(block)
            elif isinstance(value, dict) and "activeQuests" in value and "completedQuests" in value:
                self.quest_state = block
        self.player_states.sort(
            key=lambda block: int.from_bytes(block.key[:4], "little") if len(block.key) == 4 else 0
        )

    def set_player_status(
        self,
        index: int,
        hp: float,
        max_hp: float,
        breath: float,
        max_breath: float,
        conscious: bool,
        revival_item: bool,
    ) -> None:
        if not 0 <= index < len(self.player_states):
            raise ValueError("Player state not found")
        if not 1 <= max_hp <= 10_000 or not 0 <= hp <= max_hp:
            raise ValueError("Health must be between 0 and maximum health (1–10,000)")
        if not 1 <= max_breath <= 10_000 or not 0 <= breath <= max_breath:
            raise ValueError("Oxygen must be between 0 and maximum oxygen (1–10,000)")
        state = self.player_states[index].value
        stats = state["stats"]
        stats.update(
            {"hp": float(hp), "maxhp": float(max_hp), "breath": float(breath), "maxbreath": float(max_breath)}
        )
        state["isConscious"] = bool(conscious)
        state["hasRevivalItem"] = bool(revival_item)

    def raid_count(self) -> int:
        if self.raid_state is None or not isinstance(self.raid_state.value, dict):
            return 0
        raids = self.raid_state.value.get("worldRaids")
        worlds = raids.values if isinstance(raids, LuaArray) else raids.values() if isinstance(raids, dict) else []
        return sum(len(world) for world in worlds if isinstance(world, dict))

    def clear_raids(self) -> int:
        if self.raid_state is None or not isinstance(self.raid_state.value, dict):
            return 0
        count = self.raid_count()
        raids = self.raid_state.value.get("worldRaids")
        if isinstance(raids, LuaArray):
            raids.values = [{} for _ in raids.values]
        else:
            self.raid_state.value["worldRaids"] = {}
        return count

    def set_progression(self, seed_tier: int, vault_total: int, console_unlocked: bool, beacon_crafted: bool) -> None:
        if self.progression_state is None or not isinstance(self.progression_state.value, dict):
            raise RuntimeError("Progression state was not found in this save")
        if not 0 <= seed_tier <= 3:
            raise ValueError("Seed tier must be between 0 and 3")
        if not 0 <= vault_total <= 1_000_000:
            raise ValueError("Vault total must be between 0 and 1,000,000")
        self.progression_state.value.update(
            {
                "seedTier": int(seed_tier),
                "vaultTotal": int(vault_total),
                "warehouseConsoleUnlocked": bool(console_unlocked),
                "hasCraftedBeacon": bool(beacon_crafted),
            }
        )

    def _load_maps(self, connection: sqlite3.Connection, tables: set[str]) -> None:
        world_ids: set[int] = set()
        for table in ("Harvestable", "Unit", "RigidBody"):
            if table in tables:
                world_ids.update(int(row[0]) for row in connection.execute(f"SELECT DISTINCT worldId FROM {table}"))
        for world_id in sorted(world_ids):
            data = WorldMapData(world_id)
            if "Harvestable" in tables:
                data.harvestables = [
                    MapPoint(float(row[0]), float(row[1]), int(row[2]))
                    for row in connection.execute(
                        "SELECT x, y, count(*) FROM Harvestable WHERE worldId=? GROUP BY x,y", (world_id,)
                    )
                ]
            if "Unit" in tables:
                data.units = [
                    MapPoint(float(row[0]), float(row[1]), int(row[2]))
                    for row in connection.execute("SELECT x,y,count(*) FROM Unit WHERE worldId=? GROUP BY x,y", (world_id,))
                ]
            if "RigidBody" in tables and "RigidBodyBounds" in tables:
                data.rigid_bodies = [
                    MapPoint(float(row[0]) / 64.0, float(row[1]) / 64.0)
                    for row in connection.execute(
                        "SELECT (b.minX+b.maxX)/2.0, (b.minY+b.maxY)/2.0 "
                        "FROM RigidBody r JOIN RigidBodyBounds b ON b.id=r.id WHERE r.worldId=?",
                        (world_id,),
                    )
                ]
            if "Portal" in tables:
                data.portals = [
                    MapPoint(float(row[0]), float(row[1]))
                    for row in connection.execute("SELECT xA,yA FROM Portal WHERE worldIdA=?", (world_id,))
                ]
            if data.all_points():
                self.world_maps[world_id] = data

    @property
    def dirty(self) -> bool:
        return (
            self.game != self.original_game
            or bool(self.settings and self.settings.dirty)
            or any(container.dirty for container in self.containers.values())
            or any(block.dirty for block in self.player_states)
            or bool(self.raid_state and self.raid_state.dirty)
            or bool(self.progression_state and self.progression_state.dirty)
        )

    def set_game_fields(self, flags: int, seed: int, gametick: int) -> None:
        self.game["flags"] = int(flags)
        self.game["seed"] = int(seed)
        self.game["gametick"] = int(gametick)

    def set_settings(self, difficulty: int, multiplayer: int, physics_quality: int) -> None:
        if self.settings is None:
            raise RuntimeError("Settings JSON was not found in this save")
        if difficulty not in DIFFICULTY_LABELS:
            raise ValueError("Difficulty must be Easy (0) or Normal (1)")
        if not 0 <= multiplayer <= 3:
            raise ValueError("Multiplayer must be between 0 and 3")
        if not 0 <= physics_quality <= 10:
            raise ValueError("PhysicsQuality must be between 0 and 10")
        self.settings.values["Difficulty"] = difficulty
        self.settings.values["Multiplayer"] = multiplayer
        self.settings.values["PhysicsQuality"] = physics_quality

    def save_copy(self, destination: str | os.PathLike[str], overwrite_source: bool = False) -> tuple[Path, Path | None]:
        destination_path = Path(destination).resolve()
        same_path = destination_path == self.path
        if same_path and not overwrite_source:
            raise ValueError("Use overwrite_source=True when replacing the loaded save")
        if Path(str(self.path) + "-wal").exists() or Path(str(self.path) + "-journal").exists():
            raise RuntimeError("The save has an active SQLite journal. Close Scrap Mechanic and try again.")

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path: Path | None = None
        if same_path:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = self.path.with_name(f"{self.path.name}.{stamp}.bak")
            shutil.copy2(self.path, backup_path)

        fd, temp_name = tempfile.mkstemp(prefix=f".{destination_path.stem}-", suffix=".tmp", dir=destination_path.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            shutil.copy2(self.path, temp_path)
            connection = sqlite3.connect(temp_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if self.game != self.original_game:
                    connection.execute(
                        "UPDATE Game SET flags=?, seed=?, gametick=?",
                        (self.game["flags"], self.game["seed"], self.game["gametick"]),
                    )
                if self.settings and self.settings.dirty:
                    connection.execute(
                        "UPDATE GenericData SET data=? WHERE rowid=?",
                        (self.settings.to_bytes(), self.settings.rowid),
                    )
                for container in self.containers.values():
                    if container.dirty:
                        connection.execute(
                            "UPDATE Container SET data=? WHERE id=?", (container.to_bytes(), container.container_id)
                        )
                script_blocks = [*self.player_states]
                script_blocks.extend(
                    block for block in (self.raid_state, self.progression_state) if block is not None
                )
                for block in script_blocks:
                    if block.dirty:
                        connection.execute(
                            "UPDATE ScriptData SET data=? WHERE rowid=?", (block.to_bytes(), block.rowid)
                        )
                connection.commit()
                result = connection.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"SQLite integrity_check failed: {result}")
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            os.replace(temp_path, destination_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return destination_path, backup_path

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "game": dict(self.game),
            "settings": dict(self.settings.values) if self.settings else None,
            "inventory_container_id": self.inventory.container_id if self.inventory else None,
            "inventory_slots": len(self.inventory.slots) if self.inventory else 0,
            "parsed_containers": len(self.containers),
            "table_counts": dict(self.table_counts),
            "map_worlds": sorted(self.world_maps),
            "player_state_count": len(self.player_states),
            "raid_count": self.raid_count(),
            "players": [
                {
                    "steam_id": player.steam_id,
                    "world_id": player.world_id,
                    "x": player.x,
                    "y": player.y,
                    "z": player.z,
                    "yaw": player.yaw,
                    "pitch": player.pitch,
                    "inventory_container_id": player.inventory_container_id,
                    "carry_container_id": player.carry_container_id,
                }
                for player in self.players
            ],
            "terrain": {
                "seed": self.terrain_grid.seed,
                "visible_bounds": self.terrain_grid.visible_bounds,
                "size_cells": [self.terrain_grid.width, self.terrain_grid.height],
                "decoded_cells": len(self.terrain_grid.cells),
            } if self.terrain_grid else None,
            "terrain_error": self.terrain_error or None,
            "integrity": self.integrity,
        }


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def save_rgb_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    rows = b"".join(b"\x00" + bytes(pixels[y * width * 3 : (y + 1) * width * 3]) for y in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(rows, 9))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


class MapRenderer:
    COLORS = {
        "background": (17, 24, 29),
        "grid": (38, 54, 59),
        "harvestable": (65, 129, 78),
        "unit": (241, 154, 56),
        "rigid": (79, 164, 224),
        "portal": (239, 83, 80),
    }

    @staticmethod
    def bounds(data: WorldMapData) -> tuple[float, float, float, float]:
        points = data.all_points()
        if not points:
            return (-1, 1, -1, 1)
        min_x = min(point.x for point in points)
        max_x = max(point.x for point in points)
        min_y = min(point.y for point in points)
        max_y = max(point.y for point in points)
        if min_x == max_x:
            min_x -= 1
            max_x += 1
        if min_y == max_y:
            min_y -= 1
            max_y += 1
        return min_x - 2, max_x + 2, min_y - 2, max_y + 2

    @classmethod
    def draw_canvas(cls, canvas: tk.Canvas, data: WorldMapData) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 300)
        height = max(canvas.winfo_height(), 240)
        canvas.configure(background="#11181d")
        min_x, max_x, min_y, max_y = cls.bounds(data)
        margin = 35
        scale = min((width - 2 * margin) / (max_x - min_x), (height - 2 * margin) / (max_y - min_y))

        def xy(x: float, y: float) -> tuple[float, float]:
            px = margin + (x - min_x) * scale
            py = height - margin - (y - min_y) * scale
            return px, py

        for x in range(math.floor(min_x / 10) * 10, math.ceil(max_x / 10) * 10 + 1, 10):
            x1, y1 = xy(x, min_y)
            x2, y2 = xy(x, max_y)
            canvas.create_line(x1, y1, x2, y2, fill="#26363b")
            canvas.create_text(x1 + 2, height - 17, text=str(x), fill="#799098", anchor="w", font=("Segoe UI", 8))
        for y in range(math.floor(min_y / 10) * 10, math.ceil(max_y / 10) * 10 + 1, 10):
            x1, y1 = xy(min_x, y)
            x2, y2 = xy(max_x, y)
            canvas.create_line(x1, y1, x2, y2, fill="#26363b")
            canvas.create_text(4, y1, text=str(y), fill="#799098", anchor="w", font=("Segoe UI", 8))

        cell = max(2.0, min(scale, 10.0))
        for point in data.harvestables:
            px, py = xy(point.x, point.y)
            intensity = min(190, 65 + int(math.log2(point.weight + 1) * 22))
            color = f"#{55:02x}{intensity:02x}{70:02x}"
            canvas.create_rectangle(px - cell / 2, py - cell / 2, px + cell / 2, py + cell / 2, fill=color, outline="")
        for point in data.rigid_bodies:
            px, py = xy(point.x, point.y)
            canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill="#4fa4e0", outline="")
        for point in data.units:
            px, py = xy(point.x, point.y)
            radius = 2 + min(3, math.log2(point.weight + 1))
            canvas.create_oval(px - radius, py - radius, px + radius, py + radius, fill="#f19a38", outline="")
        for point in data.portals:
            px, py = xy(point.x, point.y)
            canvas.create_polygon(px, py - 7, px + 7, py, px, py + 7, px - 7, py, fill="#ef5350", outline="#ffffff")

        legend = (
            f"World {data.world_id}   Green: harvestable cells ({sum(p.weight for p in data.harvestables):,})   "
            f"Blue: rigid bodies ({len(data.rigid_bodies):,})   Orange: units ({sum(p.weight for p in data.units):,})   "
            f"Red: portals ({len(data.portals):,})"
        )
        canvas.create_rectangle(0, 0, width, 26, fill="#0b1115", outline="")
        canvas.create_text(10, 13, text=legend, fill="#e5edf0", anchor="w", font=("Segoe UI", 9))

    @classmethod
    def export_png(cls, path: Path, data: WorldMapData, width: int = 1200, height: int = 900) -> None:
        background = cls.COLORS["background"]
        pixels = bytearray(background * (width * height))
        min_x, max_x, min_y, max_y = cls.bounds(data)
        margin = 30
        scale = min((width - 2 * margin) / (max_x - min_x), (height - 2 * margin) / (max_y - min_y))

        def xy(x: float, y: float) -> tuple[int, int]:
            return (
                int(margin + (x - min_x) * scale),
                int(height - margin - (y - min_y) * scale),
            )

        def pixel(x: int, y: int, color: tuple[int, int, int]) -> None:
            if 0 <= x < width and 0 <= y < height:
                offset = (y * width + x) * 3
                pixels[offset : offset + 3] = bytes(color)

        def square(cx: int, cy: int, radius: int, color: tuple[int, int, int]) -> None:
            for py in range(cy - radius, cy + radius + 1):
                for px in range(cx - radius, cx + radius + 1):
                    pixel(px, py, color)

        for point in data.harvestables:
            px, py = xy(point.x, point.y)
            intensity = min(190, 65 + int(math.log2(point.weight + 1) * 22))
            square(px, py, max(1, int(min(scale, 8) / 2)), (55, intensity, 70))
        for point in data.rigid_bodies:
            square(*xy(point.x, point.y), 2, cls.COLORS["rigid"])
        for point in data.units:
            square(*xy(point.x, point.y), 3, cls.COLORS["unit"])
        for point in data.portals:
            px, py = xy(point.x, point.y)
            for delta in range(-7, 8):
                span = 7 - abs(delta)
                for dx in range(-span, span + 1):
                    pixel(px + dx, py + delta, cls.COLORS["portal"])
        save_rgb_png(path, width, height, pixels)


class EditorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.geometry("1320x820")
        self.minsize(1050, 680)
        self.save_data: SaveData | None = None
        self.catalog = ItemCatalog()
        self.game_root = find_game_root()
        if self.game_root:
            self.catalog.load(self.game_root)
        self.terrain_tile_names = load_terrain_tile_names(self.game_root) if self.game_root else {}
        self.item_icon_cache: dict[tuple[str, int], tk.PhotoImage] = {}
        self.item_icon_atlas_cache: dict[Path, tk.PhotoImage] = {}
        self.selected_slot = 1
        self._map_redraw_job: str | None = None
        self._terrain_renderer: TerrainRenderer | None = None
        self._terrain_render_thread: threading.Thread | None = None
        self._terrain_render_results: queue.Queue[tuple[Path | None, str | None]] = queue.Queue()
        self._terrain_cache_path: Path | None = None
        self._terrain_photo: tk.PhotoImage | None = None
        self._terrain_source_photo: tk.PhotoImage | None = None
        self._terrain_source_path: Path | None = None
        self._map_scale: tuple[int, int] | None = None
        self._map_image_geometry = (0.0, 0.0, 1, 1)
        self._map_features: list[MapFeature] = []
        self._map_feature_by_key: dict[str, MapFeature] = {}
        self._map_search_matches: list[MapFeature] = []
        self._selected_map_feature: MapFeature | None = None

        self.path_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.catalog_status_var = tk.StringVar()
        self.difficulty_var = tk.StringVar()
        self.multiplayer_var = tk.StringVar()
        self.physics_var = tk.StringVar()
        self.flags_var = tk.StringVar()
        self.seed_var = tk.StringVar()
        self.tick_var = tk.StringVar()
        self.advanced_enabled = tk.BooleanVar(value=False)
        self.item_search_var = tk.StringVar()
        self.active_container_var = tk.StringVar()
        self.selected_item_detail_var = tk.StringVar()
        self.quantity_var = tk.StringVar(value="1")
        self.manual_uuid_var = tk.StringVar()
        self.metadata_var = tk.StringVar()
        self.map_world_var = tk.StringVar()
        self.map_mode_var = tk.StringVar(value="Detailed terrain")
        self.map_show_roads = tk.BooleanVar(value=False)
        self.map_show_harvestables = tk.BooleanVar(value=False)
        self.map_show_units = tk.BooleanVar(value=False)
        self.map_show_rigid = tk.BooleanVar(value=False)
        self.map_show_portals = tk.BooleanVar(value=False)
        self.map_info_var = tk.StringVar(value="Load a save to decode its world terrain.")
        self.map_zoom_var = tk.StringVar(value="Fit")
        self.map_coord_var = tk.StringVar(value="Cell —  |  World —")
        self.map_search_var = tk.StringVar()
        self.map_search_kind_var = tk.StringVar(value="POIs / buildings")

        self._build_ui()
        self._update_catalog_status()
        self.scan_saves()

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Save:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.path_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Browse…", command=self.browse).grid(row=0, column=2, padx=3)
        ttk.Button(top, text="Load", command=self.load_path).grid(row=0, column=3, padx=3)
        ttk.Button(top, text="Save copy…", command=self.save_as).grid(row=0, column=4, padx=3)
        ttk.Button(top, text="Overwrite + backup", command=self.overwrite).grid(row=0, column=5, padx=3)
        top.columnconfigure(1, weight=1)

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(pane, padding=4)
        right = ttk.Frame(pane, padding=4)
        pane.add(left, weight=1)
        pane.add(right, weight=4)

        ttk.Label(left, text="Detected Survival saves").pack(anchor="w")
        self.save_tree = ttk.Treeview(left, columns=("modified", "size"), show="tree headings", height=24)
        self.save_tree.heading("#0", text="Name")
        self.save_tree.heading("modified", text="Modified")
        self.save_tree.heading("size", text="Size")
        self.save_tree.column("#0", width=180)
        self.save_tree.column("modified", width=115)
        self.save_tree.column("size", width=70, anchor="e")
        self.save_tree.pack(fill="both", expand=True, pady=6)
        self.save_tree.bind("<<TreeviewSelect>>", self._save_selected)
        self.save_tree.bind("<Double-1>", lambda _event: self.load_path())
        ttk.Button(left, text="Rescan saves", command=self.scan_saves).pack(fill="x")

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)
        self.overview_tab = ttk.Frame(self.notebook, padding=12)
        self.settings_tab = ttk.Frame(self.notebook, padding=12)
        self.inventory_tab = ttk.Frame(self.notebook, padding=8)
        self.map_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.overview_tab, text="Overview")
        self.notebook.add(self.settings_tab, text="World settings")
        self.notebook.add(self.inventory_tab, text="Inventory")
        self.notebook.add(self.map_tab, text="World map")
        self._build_overview()
        self._build_settings()
        self._build_inventory()
        self._build_map()
        self.notebook.bind("<<NotebookTabChanged>>", self._notebook_changed)

        ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken", padding=(6, 3)).pack(fill="x")

    def _build_overview(self) -> None:
        self.overview_text = tk.Text(self.overview_tab, wrap="word", state="disabled", font=("Consolas", 10))
        self.overview_text.pack(fill="both", expand=True)

    def _build_settings(self) -> None:
        safe = ttk.LabelFrame(self.settings_tab, text="Validated world settings", padding=12)
        safe.pack(fill="x")
        ttk.Label(safe, text="Difficulty").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Combobox(safe, textvariable=self.difficulty_var, values=("Easy (0)", "Normal (1)"), state="readonly", width=18).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(safe, text="Multiplayer (raw engine value)").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Spinbox(safe, textvariable=self.multiplayer_var, from_=0, to=3, width=8).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(safe, text="Physics quality (raw engine value)").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Spinbox(safe, textvariable=self.physics_var, from_=0, to=10, width=8).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(
            safe,
            text="Difficulty 0/1 is confirmed by the installed Survival script. Multiplayer and PhysicsQuality are preserved as raw engine values because their labels are not defined in the game scripts.",
            wraplength=760,
            justify="left",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

        advanced = ttk.LabelFrame(self.settings_tab, text="Advanced Game table values", padding=12)
        advanced.pack(fill="x", pady=(14, 0))
        ttk.Checkbutton(advanced, text="Enable advanced header editing", variable=self.advanced_enabled, command=self._toggle_advanced).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(advanced, text="Flags").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(advanced, textvariable=self.flags_var, width=20, state="disabled", name="flags_entry").grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(advanced, text="World seed").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(advanced, textvariable=self.seed_var, width=20, state="disabled", name="seed_entry").grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(advanced, text="Game tick").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(advanced, textvariable=self.tick_var, width=20, state="disabled", name="tick_entry").grid(row=3, column=1, sticky="w", padx=8)
        ttk.Label(advanced, text="Changing seed does not regenerate existing cells. Savegame version is intentionally read-only.", foreground="#9a4e00").grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.advanced_frame = advanced

    def _build_inventory(self) -> None:
        ttk.Style(self).configure("Icon.Treeview", rowheight=38)
        pane = ttk.Panedwindow(self.inventory_tab, orient="horizontal")
        pane.pack(fill="both", expand=True)
        slots_frame = ttk.Frame(pane)
        catalog_frame = ttk.Frame(pane)
        pane.add(slots_frame, weight=3)
        pane.add(catalog_frame, weight=2)

        container_row = ttk.Frame(slots_frame)
        container_row.pack(fill="x", pady=(0, 6))
        ttk.Label(container_row, text="Editable container").pack(side="left")
        self.container_combo = ttk.Combobox(
            container_row, textvariable=self.active_container_var, state="readonly", width=48
        )
        self.container_combo.pack(side="left", fill="x", expand=True, padx=6)
        self.container_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_inventory())

        self.inventory_tree = ttk.Treeview(
            slots_frame,
            columns=("slot", "name", "quantity", "uuid", "metadata"),
            show="tree headings",
            style="Icon.Treeview",
        )
        self.inventory_tree.heading("#0", text="Icon")
        self.inventory_tree.column("#0", width=44, minwidth=44, stretch=False, anchor="center")
        for key, title, width in (
            ("slot", "Slot", 45), ("name", "Item", 210), ("quantity", "Qty", 65), ("uuid", "UUID", 275), ("metadata", "Metadata", 80)
        ):
            self.inventory_tree.heading(key, text=title)
            self.inventory_tree.column(key, width=width, anchor="center" if key in {"slot", "quantity", "metadata"} else "w")
        self.inventory_tree.pack(fill="both", expand=True)
        self.inventory_tree.bind("<<TreeviewSelect>>", self._slot_selected)

        edit = ttk.LabelFrame(slots_frame, text="Selected slot", padding=8)
        edit.pack(fill="x", pady=(8, 0))
        ttk.Label(edit, text="Quantity").grid(row=0, column=0, sticky="w")
        ttk.Entry(edit, textvariable=self.quantity_var, width=10).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(edit, text="Apply quantity", command=self.apply_quantity).grid(row=0, column=2, padx=4)
        ttk.Button(edit, text="Clear slot", command=self.clear_slot).grid(row=0, column=3, padx=4)
        ttk.Label(edit, text="UUID").grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Entry(edit, textvariable=self.manual_uuid_var, width=40).grid(row=1, column=1, columnspan=3, sticky="ew", padx=6, pady=(7, 0))
        ttk.Button(edit, text="Apply UUID + quantity", command=self.apply_manual_uuid).grid(row=1, column=4, padx=4, pady=(7, 0))
        ttk.Label(edit, text="Metadata (preserved)").grid(row=2, column=0, sticky="w", pady=(7, 0))
        ttk.Entry(edit, textvariable=self.metadata_var, width=12, state="readonly").grid(row=2, column=1, sticky="w", padx=6, pady=(7, 0))
        self.selected_item_icon_label = ttk.Label(edit, text="No icon", anchor="center", width=10)
        self.selected_item_icon_label.grid(row=0, column=5, rowspan=3, padx=(12, 2), sticky="nsew")
        edit.columnconfigure(2, weight=1)

        ttk.Label(catalog_frame, text="Game item catalog").pack(anchor="w")
        ttk.Entry(catalog_frame, textvariable=self.item_search_var).pack(fill="x", pady=(5, 5))
        self.item_search_var.trace_add("write", lambda *_args: self.refresh_catalog())
        self.catalog_tree = ttk.Treeview(
            catalog_frame,
            columns=("title", "stack", "type"),
            show="tree headings",
            height=17,
            style="Icon.Treeview",
        )
        self.catalog_tree.heading("#0", text="Icon")
        self.catalog_tree.column("#0", width=44, minwidth=44, stretch=False, anchor="center")
        self.catalog_tree.heading("title", text="Item")
        self.catalog_tree.heading("stack", text="Stack")
        self.catalog_tree.heading("type", text="Type")
        self.catalog_tree.column("title", width=230)
        self.catalog_tree.column("stack", width=55, anchor="center")
        self.catalog_tree.column("type", width=60, anchor="center")
        self.catalog_tree.pack(fill="both", expand=True)
        self.catalog_tree.bind("<Double-1>", lambda _event: self.put_catalog_item())
        self.catalog_tree.bind("<<TreeviewSelect>>", self._catalog_selected)
        ttk.Label(catalog_frame, textvariable=self.selected_item_detail_var, wraplength=350, justify="left").pack(fill="x", pady=(5, 0))
        ttk.Button(catalog_frame, text="Put selected item in selected container slot", command=self.put_catalog_item).pack(fill="x", pady=(6, 0))
        ttk.Label(catalog_frame, textvariable=self.catalog_status_var, wraplength=350, justify="left").pack(fill="x", pady=(6, 0))

    def _build_map(self) -> None:
        controls = ttk.Frame(self.map_tab)
        controls.pack(fill="x", pady=(0, 6))
        ttk.Label(controls, text="World").pack(side="left")
        self.map_world_combo = ttk.Combobox(controls, textvariable=self.map_world_var, state="readonly", width=12)
        self.map_world_combo.pack(side="left", padx=6)
        self.map_world_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: (self.refresh_map_search(), self.redraw_map()),
        )
        ttk.Label(controls, text="View").pack(side="left", padx=(8, 0))
        self.map_mode_combo = ttk.Combobox(
            controls,
            textvariable=self.map_mode_var,
            values=("Detailed terrain", "Object schematic"),
            state="readonly",
            width=18,
        )
        self.map_mode_combo.pack(side="left", padx=6)
        self.map_mode_combo.bind("<<ComboboxSelected>>", lambda _event: self.redraw_map())
        self.render_terrain_button = ttk.Button(controls, text="Build terrain map", command=self.render_terrain_map)
        self.render_terrain_button.pack(side="left", padx=4)
        ttk.Button(controls, text="Export PNG…", command=self.export_map).pack(side="left", padx=4)
        ttk.Separator(controls, orient="vertical").pack(side="left", fill="y", padx=7)
        ttk.Button(controls, text="−", width=3, command=lambda: self.zoom_map(-1)).pack(side="left")
        ttk.Button(controls, text="Fit", width=5, command=self.fit_map).pack(side="left", padx=3)
        ttk.Button(controls, text="+", width=3, command=lambda: self.zoom_map(1)).pack(side="left")
        ttk.Label(controls, textvariable=self.map_zoom_var, width=9, anchor="center").pack(side="left", padx=3)

        layers = ttk.Frame(self.map_tab)
        layers.pack(fill="x", pady=(0, 6))
        ttk.Label(layers, text="Saved-object overlays:").pack(side="left")
        for label, variable in (
            ("Road cell outlines", self.map_show_roads),
            ("Creations / rigid bodies", self.map_show_rigid),
            ("Bots / units", self.map_show_units),
            ("Harvestables", self.map_show_harvestables),
            ("Portals", self.map_show_portals),
        ):
            ttk.Checkbutton(layers, text=label, variable=variable, command=self.redraw_map).pack(side="left", padx=5)
        ttk.Label(layers, textvariable=self.map_info_var).pack(side="right", padx=6)

        body = ttk.Panedwindow(self.map_tab, orient="horizontal")
        body.pack(fill="both", expand=True)
        search_frame = ttk.Frame(body, padding=(0, 0, 6, 0))
        canvas_frame = ttk.Frame(body)
        body.add(search_frame, weight=1)
        body.add(canvas_frame, weight=5)

        ttk.Label(search_frame, text="Find on this save", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Entry(search_frame, textvariable=self.map_search_var).pack(fill="x", pady=(5, 4))
        self.map_search_var.trace_add(
            "write", lambda *_args: (self.refresh_map_search(), self.redraw_map())
        )
        self.map_search_kind_combo = ttk.Combobox(
            search_frame,
            textvariable=self.map_search_kind_var,
            values=(
                "POIs / buildings", "All terrain features", "Roads", "Creations",
                "Bots / units", "Portals", "Harvestables",
            ),
            state="readonly",
            width=23,
        )
        self.map_search_kind_combo.pack(fill="x", pady=(0, 5))
        self.map_search_kind_combo.bind(
            "<<ComboboxSelected>>", lambda _event: (self.refresh_map_search(), self.redraw_map())
        )
        self.map_feature_tree = ttk.Treeview(
            search_frame, columns=("kind", "coords"), show="tree headings", selectmode="browse"
        )
        self.map_feature_tree.heading("#0", text="Feature")
        self.map_feature_tree.heading("kind", text="Type")
        self.map_feature_tree.heading("coords", text="Cell")
        self.map_feature_tree.column("#0", width=180)
        self.map_feature_tree.column("kind", width=85)
        self.map_feature_tree.column("coords", width=75, anchor="center")
        self.map_feature_tree.pack(fill="both", expand=True)
        self.map_feature_tree.bind("<<TreeviewSelect>>", self._map_feature_selected)
        self.map_feature_tree.bind("<Double-1>", self._map_feature_selected)
        ttk.Label(
            search_frame,
            text="Select a result to center it. Drag the map to pan; use the mouse wheel to zoom.",
            wraplength=260,
            justify="left",
        ).pack(fill="x", pady=(5, 0))

        self.map_canvas = tk.Canvas(
            canvas_frame,
            background="#11181d",
            highlightthickness=0,
            xscrollincrement=1,
            yscrollincrement=1,
        )
        x_scroll = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.map_canvas.xview)
        y_scroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.map_canvas.yview)
        self.map_canvas.configure(xscrollcommand=x_scroll.set, yscrollcommand=y_scroll.set)
        self.map_canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        ttk.Label(canvas_frame, textvariable=self.map_coord_var, anchor="w").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(3, 0)
        )
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self.map_canvas.bind("<Configure>", self._schedule_map_redraw)
        self.map_canvas.bind("<MouseWheel>", self._map_mousewheel)
        self.map_canvas.bind("<ButtonPress-1>", lambda event: self.map_canvas.scan_mark(event.x, event.y))
        self.map_canvas.bind("<B1-Motion>", lambda event: self.map_canvas.scan_dragto(event.x, event.y, gain=1))
        self.map_canvas.bind("<Motion>", self._map_pointer_motion)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.update_idletasks()

    def _update_catalog_status(self) -> None:
        if self.game_root:
            icons = sum(item.icon_path is not None or item.icon_atlas_path is not None for item in self.catalog.items.values())
            message = f"{len(self.catalog.items):,} UUIDs and {icons:,} item pictures loaded from {self.game_root}"
            if self.catalog.load_error:
                message += f"\n{self.catalog.load_error}"
        else:
            message = "Scrap Mechanic installation not found. Manual UUID editing remains available."
        self.catalog_status_var.set(message)
        self.refresh_catalog()

    def scan_saves(self) -> None:
        for item_id in self.save_tree.get_children():
            self.save_tree.delete(item_id)
        saves = scan_survival_saves()
        for index, path in enumerate(saves):
            stat = path.stat()
            self.save_tree.insert(
                "", "end", iid=str(index), text=path.name,
                values=(datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"), human_size(stat.st_size)),
                tags=(str(path),),
            )
        if saves and not self.path_var.get():
            self.path_var.set(str(saves[0]))
        self.set_status(f"Found {len(saves)} Survival save databases")

    def _save_selected(self, _event: tk.Event | None = None) -> None:
        selection = self.save_tree.selection()
        if not selection:
            return
        tags = self.save_tree.item(selection[0], "tags")
        if tags:
            self.path_var.set(tags[0])

    def browse(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Open Scrap Mechanic Survival save",
            initialdir=str(default_save_root()),
            filetypes=(("Scrap Mechanic save", "*.db"), ("All files", "*.*")),
        )
        if chosen:
            self.path_var.set(chosen)

    def load_path(self) -> None:
        path = self.path_var.get().strip()
        if not path:
            messagebox.showwarning(APP_TITLE, "Choose a save first.")
            return
        if self.save_data and self.save_data.dirty:
            if not messagebox.askyesno(APP_TITLE, "Discard the unsaved edits currently in memory?"):
                return
        try:
            self.set_status(f"Loading {path}…")
            self.save_data = SaveData(path)
            self._populate_loaded_save()
            self.set_status(f"Loaded {Path(path).name}; SQLite check: {self.save_data.integrity}")
        except Exception as exc:
            self.save_data = None
            messagebox.showerror(APP_TITLE, f"Could not load the save:\n\n{exc}")
            self.set_status("Load failed")

    def _populate_loaded_save(self) -> None:
        assert self.save_data is not None
        data = self.save_data
        self.flags_var.set(str(data.game["flags"]))
        self.seed_var.set(str(data.game["seed"]))
        self.tick_var.set(str(data.game["gametick"]))
        if data.settings:
            difficulty = int(data.settings.values.get("Difficulty", 1))
            self.difficulty_var.set(f"{DIFFICULTY_LABELS.get(difficulty, 'Unknown')} ({difficulty})")
            self.multiplayer_var.set(str(data.settings.values.get("Multiplayer", 0)))
            self.physics_var.set(str(data.settings.values.get("PhysicsQuality", 0)))
        else:
            self.difficulty_var.set("")
            self.multiplayer_var.set("")
            self.physics_var.set("")
        self.advanced_enabled.set(False)
        self._toggle_advanced()
        container_labels: list[str] = []
        inventory_id = data.inventory.container_id if data.inventory else None
        for container_id, container in sorted(data.containers.items(), key=lambda pair: (pair[0] != inventory_id, pair[0])):
            prefix = "Player inventory" if container_id == inventory_id else "Container"
            container_labels.append(f"{prefix} | ID {container_id} | {len(container.slots)} slots")
        self.container_combo.configure(values=container_labels)
        self.active_container_var.set(container_labels[0] if container_labels else "")
        self.refresh_inventory()
        self._terrain_cache_path = None
        self._terrain_photo = None
        self._terrain_source_photo = None
        self._terrain_source_path = None
        self._map_scale = None
        self._selected_map_feature = None
        self._terrain_photo_path: Path | None = None
        self._terrain_photo_factor = 0
        world_ids = set(data.world_maps)
        if data.terrain_grid:
            world_ids.add(1)
            self.map_info_var.set(
                f"Terrain decoded: {data.terrain_grid.width}×{data.terrain_grid.height} cells"
            )
        else:
            self.map_info_var.set(data.terrain_error or "Detailed terrain is not available in this save.")
        worlds = [str(world_id) for world_id in sorted(world_ids)]
        self.map_world_combo.configure(values=worlds)
        self.map_world_var.set("1" if "1" in worlds else (worlds[0] if worlds else ""))
        self._build_map_features()
        self.refresh_map_search()
        self.redraw_map()
        summary = json.dumps(data.summary(), indent=2, ensure_ascii=False)
        catalog_line = f"\n\nItem catalog: {len(self.catalog.items):,} entries from {self.game_root}" if self.game_root else "\n\nItem catalog: game install not found"
        self.overview_text.configure(state="normal")
        self.overview_text.delete("1.0", "end")
        self.overview_text.insert("1.0", summary + catalog_line)
        self.overview_text.configure(state="disabled")

    def _toggle_advanced(self) -> None:
        state = "normal" if self.advanced_enabled.get() else "disabled"
        for child_name in ("flags_entry", "seed_entry", "tick_entry"):
            try:
                self.advanced_frame.nametowidget(child_name).configure(state=state)
            except KeyError:
                pass

    def refresh_inventory(self) -> None:
        for item_id in self.inventory_tree.get_children():
            self.inventory_tree.delete(item_id)
        container = self._active_container()
        if container is None:
            return
        for slot in container.slots:
            photo = self._item_photo(slot.uuid, 32)
            options: dict[str, Any] = {
                "values": (slot.number, self.catalog.display_name(slot.uuid), slot.quantity, slot.uuid, slot.metadata.hex())
            }
            if photo is not None:
                options["image"] = photo
            self.inventory_tree.insert("", "end", iid=str(slot.number), **options)
        selected = str(min(self.selected_slot, len(container.slots)))
        self.inventory_tree.selection_set(selected)
        self.inventory_tree.focus(selected)
        self.inventory_tree.see(selected)
        self._slot_selected()

    def _slot_selected(self, _event: tk.Event | None = None) -> None:
        container = self._active_container()
        if container is None:
            return
        selection = self.inventory_tree.selection()
        if not selection:
            return
        self.selected_slot = int(selection[0])
        slot = container.slots[self.selected_slot - 1]
        self.quantity_var.set(str(slot.quantity))
        self.manual_uuid_var.set(slot.uuid)
        self.metadata_var.set(slot.metadata.hex())
        photo = self._item_photo(slot.uuid, 64)
        self.selected_item_icon_label.configure(
            image=photo,
            text="" if photo else "No icon",
        )

    def _active_container(self) -> ContainerBlob | None:
        if not self.save_data:
            return None
        match = re.search(r"\bID (\d+)\b", self.active_container_var.get())
        if match:
            return self.save_data.containers.get(int(match.group(1)))
        return self.save_data.inventory

    def _item_photo(self, uuid_text: str, target_size: int) -> tk.PhotoImage | None:
        item = self.catalog.get(uuid_text)
        if not item:
            return None
        key = (item.uuid, target_size)
        cached = self.item_icon_cache.get(key)
        if cached is not None:
            return cached
        try:
            if item.icon_atlas_path and item.icon_point:
                source_atlas = self.item_icon_atlas_cache.get(item.icon_atlas_path)
                if source_atlas is None:
                    source_atlas = tk.PhotoImage(file=str(item.icon_atlas_path))
                    self.item_icon_atlas_cache[item.icon_atlas_path] = source_atlas
                source = tk.PhotoImage(width=item.icon_size[0], height=item.icon_size[1])
                x, y = item.icon_point
                source.tk.call(
                    str(source), "copy", str(source_atlas),
                    "-from", x, y, x + item.icon_size[0], y + item.icon_size[1],
                    "-to", 0, 0,
                )
            elif item.icon_path and item.icon_path.is_file():
                source = tk.PhotoImage(file=str(item.icon_path))
            else:
                return None
            source_size = max(source.width(), source.height())
            if source_size < target_size:
                factor = max(1, target_size // source_size)
                photo = source.zoom(factor, factor)
            else:
                factor = max(1, math.ceil(source_size / target_size))
                photo = source if factor == 1 else source.subsample(factor, factor)
            self.item_icon_cache[key] = photo
            return photo
        except tk.TclError:
            return None

    def refresh_catalog(self) -> None:
        if not hasattr(self, "catalog_tree"):
            return
        for item_id in self.catalog_tree.get_children():
            self.catalog_tree.delete(item_id)
        for item in self.catalog.search(
            self.item_search_var.get() if hasattr(self, "item_search_var") else "",
            limit=120,
        ):
            photo = self._item_photo(item.uuid, 32)
            options = {"values": (item.title, item.stack_size or "?", item.category)}
            if photo is not None:
                options["image"] = photo
            self.catalog_tree.insert("", "end", iid=item.uuid, **options)

    def _catalog_selected(self, _event: tk.Event | None = None) -> None:
        selection = self.catalog_tree.selection()
        if not selection:
            self.selected_item_detail_var.set("")
            return
        item = self.catalog.get(selection[0])
        if not item:
            return
        stack = item.stack_size if item.stack_size is not None else "unknown"
        detail = f"{item.title}  •  {item.category}  •  stack {stack}\n{item.uuid}"
        if item.description:
            detail += f"\n{item.description}"
        self.selected_item_detail_var.set(detail)

    def _require_inventory(self) -> ContainerBlob:
        container = self._active_container()
        if container is None:
            raise RuntimeError("Load a save with a detected editable container first")
        return container

    def apply_quantity(self) -> None:
        try:
            inventory = self._require_inventory()
            slot = inventory.slots[self.selected_slot - 1]
            inventory.set_slot(self.selected_slot, slot.uuid, int(self.quantity_var.get()), self.catalog)
            self.refresh_inventory()
            self.set_status(f"Updated slot {self.selected_slot} in memory; save to write it to disk")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def apply_manual_uuid(self) -> None:
        try:
            inventory = self._require_inventory()
            inventory.set_slot(self.selected_slot, self.manual_uuid_var.get(), int(self.quantity_var.get()), self.catalog)
            self.refresh_inventory()
            self.set_status(f"Updated slot {self.selected_slot} in memory; save to write it to disk")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def clear_slot(self) -> None:
        try:
            self._require_inventory().clear_slot(self.selected_slot)
            self.refresh_inventory()
            self.set_status(f"Cleared slot {self.selected_slot} in memory")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def put_catalog_item(self) -> None:
        selection = self.catalog_tree.selection()
        if not selection:
            messagebox.showwarning(APP_TITLE, "Select an item in the catalog first.")
            return
        try:
            inventory = self._require_inventory()
            inventory.set_slot(self.selected_slot, selection[0], int(self.quantity_var.get()), self.catalog)
            self.refresh_inventory()
            self.set_status(f"Placed {self.catalog.display_name(selection[0])} in slot {self.selected_slot} in memory")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _apply_forms(self) -> None:
        if not self.save_data:
            raise RuntimeError("Load a save first")
        if self.save_data.settings:
            match = re.search(r"\((-?\d+)\)$", self.difficulty_var.get())
            if not match:
                raise ValueError("Choose Easy or Normal difficulty")
            self.save_data.set_settings(int(match.group(1)), int(self.multiplayer_var.get()), int(self.physics_var.get()))
        if self.advanced_enabled.get():
            self.save_data.set_game_fields(int(self.flags_var.get()), int(self.seed_var.get()), int(self.tick_var.get()))

    def save_as(self) -> None:
        if not self.save_data:
            messagebox.showwarning(APP_TITLE, "Load a save first.")
            return
        try:
            self._apply_forms()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Invalid edit:\n\n{exc}")
            return
        suggested = self.save_data.path.with_name(f"{self.save_data.path.stem}_edited.db")
        chosen = filedialog.asksaveasfilename(
            title="Save edited copy",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            defaultextension=".db",
            filetypes=(("Scrap Mechanic save", "*.db"), ("All files", "*.*")),
        )
        if not chosen:
            return
        try:
            output, _backup = self.save_data.save_copy(chosen)
            messagebox.showinfo(APP_TITLE, f"Edited copy saved and SQLite integrity verified:\n\n{output}")
            self.set_status(f"Saved copy: {output}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Save failed:\n\n{exc}")

    def overwrite(self) -> None:
        if not self.save_data:
            messagebox.showwarning(APP_TITLE, "Load a save first.")
            return
        try:
            self._apply_forms()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Invalid edit:\n\n{exc}")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "Close Scrap Mechanic before continuing.\n\nOverwrite this save now? A timestamped .bak file will be created first.",
            icon="warning",
        ):
            return
        try:
            output, backup = self.save_data.save_copy(self.save_data.path, overwrite_source=True)
            messagebox.showinfo(APP_TITLE, f"Save overwritten and verified.\n\nBackup: {backup}\nEdited save: {output}")
            self.set_status(f"Overwrote save; backup: {backup}")
            self.save_data = SaveData(output)
            self._populate_loaded_save()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Overwrite failed:\n\n{exc}")

    @staticmethod
    def _friendly_asset_name(value: str) -> str:
        text = value.replace("_", " ").replace("+", " + ")
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text or "Unnamed terrain feature"

    def _build_map_features(self) -> None:
        self._map_features = []
        self._map_feature_by_key = {}
        if not self.save_data:
            return
        grid = self.save_data.terrain_grid
        if grid:
            grouped: dict[tuple[Any, ...], list[Any]] = {}
            for cell in grid.cells.values():
                metadata = self.terrain_tile_names.get(cell.uuid, {})
                raw_name = str(metadata.get("name") or cell.uuid)
                raw_category = str(metadata.get("category") or "terrain")
                searchable = f"{raw_name} {raw_category}".casefold()
                if "road" in searchable:
                    kind = "Road"
                elif any(
                    word in searchable
                    for word in (
                        "poi", "quest", "station", "warehouse", "packing", "hideout",
                        "garage", "growlab", "crashed", "ruin", "shop", "drill",
                        "excavation", "mining", "onboarding", "landmark", "boss",
                    )
                ):
                    kind = "POI / building"
                else:
                    kind = "Terrain"
                if cell.group_id > 0:
                    key: tuple[Any, ...] = (cell.uuid, cell.group_id)
                elif kind in {"Road", "POI / building"}:
                    key = (cell.uuid, cell.x, cell.y)
                else:
                    continue
                grouped.setdefault(key, []).append((cell, raw_name, kind))

            for index, (group_key, entries) in enumerate(grouped.items()):
                cells = [entry[0] for entry in entries]
                raw_name = entries[0][1]
                kind = entries[0][2]
                feature = MapFeature(
                    key=f"terrain-{index}",
                    label=self._friendly_asset_name(raw_name),
                    kind=kind,
                    world_id=1,
                    x=sum(cell.x for cell in cells) / len(cells),
                    y=sum(cell.y for cell in cells) / len(cells),
                    uuid=cells[0].uuid,
                    cells=len(cells),
                )
                self._map_features.append(feature)

        for world_id, data in self.save_data.world_maps.items():
            for kind, label, points in (
                ("Creation", "Saved creation", data.rigid_bodies),
                ("Bot / unit", "Bot or unit", data.units),
                ("Portal", "World portal", data.portals),
                ("Harvestable", "Harvestable", data.harvestables),
            ):
                for index, point in enumerate(points):
                    self._map_features.append(
                        MapFeature(
                            key=f"{kind.casefold().replace(' ', '-')}-{world_id}-{index}",
                            label=f"{label} #{index + 1}",
                            kind=kind,
                            world_id=world_id,
                            x=point.x,
                            y=point.y,
                            cells=point.weight,
                        )
                    )
        self._map_feature_by_key = {feature.key: feature for feature in self._map_features}

    def refresh_map_search(self) -> None:
        if not hasattr(self, "map_feature_tree"):
            return
        for item_id in self.map_feature_tree.get_children():
            self.map_feature_tree.delete(item_id)
        query_words = self.map_search_var.get().casefold().split()
        selected_kind = self.map_search_kind_var.get()
        kind_map = {
            "POIs / buildings": {"POI / building"},
            "All terrain features": {"POI / building", "Terrain", "Road"},
            "Roads": {"Road"},
            "Creations": {"Creation"},
            "Bots / units": {"Bot / unit"},
            "Portals": {"Portal"},
            "Harvestables": {"Harvestable"},
        }
        allowed = kind_map.get(selected_kind, set())
        world_id = int(self.map_world_var.get() or 1)
        matches = [
            feature for feature in self._map_features
            if feature.world_id == world_id
            and feature.kind in allowed
            and all(word in feature.search_text for word in query_words)
        ]
        matches.sort(key=lambda feature: (feature.label.casefold(), feature.x, feature.y))
        self._map_search_matches = matches[:500]
        for feature in matches[:500]:
            self.map_feature_tree.insert(
                "", "end", iid=feature.key, text=feature.label,
                values=(feature.kind, f"{feature.x:.1f}, {feature.y:.1f}"),
            )
        if len(matches) > 500:
            self.map_info_var.set(f"Showing the first 500 of {len(matches):,} search results")

    def _map_feature_selected(self, _event: tk.Event | None = None) -> None:
        selection = self.map_feature_tree.selection()
        if not selection:
            return
        feature = self._map_feature_by_key.get(selection[0])
        if not feature:
            return
        self._selected_map_feature = feature
        self.map_world_var.set(str(feature.world_id))
        if feature.world_id == 1:
            self.map_mode_var.set("Detailed terrain")
            if not self._terrain_cache_path or not self._terrain_cache_path.is_file():
                self.render_terrain_map()
                return
        if self._map_scale is None:
            self._map_scale = (1, 1)
        self.redraw_map()
        self._center_map_on(feature.x, feature.y)
        self.set_status(f"Map centered on {feature.label} at cell {feature.x:.1f}, {feature.y:.1f}")

    def _center_map_on(self, x: float, y: float) -> None:
        if not self.save_data or not self.save_data.terrain_grid:
            return
        left, top, width, height = self._map_image_geometry
        grid = self.save_data.terrain_grid
        x_min, _x_max, _y_min, y_max = grid.visible_bounds
        target_x = left + (x - x_min + 0.5) * width / grid.width
        target_y = top + (y_max - y + 0.5) * height / grid.height
        region = self.map_canvas.cget("scrollregion").split()
        if len(region) != 4:
            return
        region_width = float(region[2]) - float(region[0])
        region_height = float(region[3]) - float(region[1])
        canvas_width = max(self.map_canvas.winfo_width(), 1)
        canvas_height = max(self.map_canvas.winfo_height(), 1)
        self.map_canvas.xview_moveto(max(0.0, min(1.0, (target_x - canvas_width / 2) / max(region_width, 1))))
        self.map_canvas.yview_moveto(max(0.0, min(1.0, (target_y - canvas_height / 2) / max(region_height, 1))))

    @staticmethod
    def _available_map_scales() -> tuple[tuple[int, int], ...]:
        return ((1, 4), (1, 3), (1, 2), (2, 3), (1, 1), (2, 1))

    def _fit_map_scale(self) -> tuple[int, int]:
        if not self.save_data or not self.save_data.terrain_grid:
            return (1, 1)
        grid = self.save_data.terrain_grid
        source_width = grid.width * 16
        source_height = grid.height * 16
        canvas_width = max(self.map_canvas.winfo_width() - 8, 1)
        canvas_height = max(self.map_canvas.winfo_height() - 8, 1)
        wanted = min(canvas_width / source_width, canvas_height / source_height)
        return min(self._available_map_scales(), key=lambda value: abs(value[0] / value[1] - wanted))

    def fit_map(self) -> None:
        self._map_scale = None
        self.redraw_map()
        self.map_canvas.xview_moveto(0)
        self.map_canvas.yview_moveto(0)

    def zoom_map(self, direction: int) -> None:
        if not self.save_data or not self.save_data.terrain_grid:
            return
        grid = self.save_data.terrain_grid
        left, top, width, height = self._map_image_geometry
        center_canvas_x = self.map_canvas.canvasx(self.map_canvas.winfo_width() / 2)
        center_canvas_y = self.map_canvas.canvasy(self.map_canvas.winfo_height() / 2)
        x_min, _x_max, _y_min, y_max = grid.visible_bounds
        center_x = x_min + ((center_canvas_x - left) / max(width, 1)) * grid.width - 0.5
        center_y = y_max - ((center_canvas_y - top) / max(height, 1)) * grid.height + 0.5
        scales = self._available_map_scales()
        current = self._map_scale or self._fit_map_scale()
        index = min(range(len(scales)), key=lambda item: abs(scales[item][0] / scales[item][1] - current[0] / current[1]))
        index = max(0, min(len(scales) - 1, index + (1 if direction > 0 else -1)))
        self._map_scale = scales[index]
        self.redraw_map()
        self._center_map_on(center_x, center_y)

    def _map_mousewheel(self, event: tk.Event) -> str:
        self.zoom_map(1 if event.delta > 0 else -1)
        return "break"

    def _map_pointer_motion(self, event: tk.Event) -> None:
        if not self.save_data or not self.save_data.terrain_grid:
            return
        left, top, width, height = self._map_image_geometry
        px = self.map_canvas.canvasx(event.x)
        py = self.map_canvas.canvasy(event.y)
        if not (left <= px <= left + width and top <= py <= top + height):
            self.map_coord_var.set("Cell —  |  World —")
            return
        grid = self.save_data.terrain_grid
        x_min, _x_max, _y_min, y_max = grid.visible_bounds
        continuous_x = x_min + (px - left) * grid.width / width
        continuous_y = y_max + 1 - (py - top) * grid.height / height
        cell_x = math.floor(continuous_x)
        cell_y = math.floor(continuous_y)
        world_x = continuous_x * 64
        world_y = continuous_y * 64
        self.map_coord_var.set(
            f"Cell {cell_x}, {cell_y}  |  World X {world_x:,.0f}, Y {world_y:,.0f}"
        )

    def _schedule_map_redraw(self, _event: tk.Event | None = None) -> None:
        if self._map_redraw_job:
            self.after_cancel(self._map_redraw_job)
        self._map_redraw_job = self.after(100, self.redraw_map)

    def _notebook_changed(self, _event: tk.Event | None = None) -> None:
        if self.notebook.select() == str(self.map_tab) and self.map_mode_var.get() == "Detailed terrain":
            self.render_terrain_map()

    def _terrain_cache_file(self) -> Path:
        if not self.save_data:
            raise RuntimeError("Load a save first")
        stat = self.save_data.path.stat()
        game_marker = str(self.game_root) if self.game_root else "no-game"
        identity = f"{self.save_data.path}|{stat.st_size}|{stat.st_mtime_ns}|{game_marker}|atlas-v3"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        base = Path(os.getenv("LOCALAPPDATA", tempfile.gettempdir())) / "ScrapMechanicSaveEditor" / "map-cache"
        return base / f"terrain-{digest}-16px.png"

    def render_terrain_map(self) -> None:
        if not self.save_data or not self.save_data.terrain_grid:
            if self.save_data:
                messagebox.showwarning(APP_TITLE, self.save_data.terrain_error or "No decoded terrain is available.")
            return
        if int(self.map_world_var.get() or 0) != 1:
            messagebox.showinfo(APP_TITLE, "Detailed generated terrain is stored for overworld 1. Use the schematic for indoor worlds.")
            return
        if not self.game_root:
            messagebox.showwarning(APP_TITLE, "The Scrap Mechanic installation was not found, so its terrain artwork cannot be loaded.")
            return
        cache_path = self._terrain_cache_file()
        self._terrain_cache_path = cache_path
        if cache_path.is_file():
            self._terrain_render_ready(cache_path)
            return
        if self._terrain_render_thread and self._terrain_render_thread.is_alive():
            return

        grid = self.save_data.terrain_grid
        game_root = self.game_root
        self.render_terrain_button.configure(state="disabled")
        self.map_info_var.set("Building detailed map in the background…")
        self.set_status("Rendering the top-down terrain atlas; the editor remains usable…")

        def worker() -> None:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".rendering.png")
                renderer = TerrainRenderer(game_root)
                renderer.render_png(temporary, grid, cell_pixels=16)
                os.replace(temporary, cache_path)
                self._terrain_render_results.put((cache_path, None))
            except Exception as exc:
                self._terrain_render_results.put((None, str(exc)))

        self._terrain_render_thread = threading.Thread(target=worker, name="terrain-map-render", daemon=True)
        self._terrain_render_thread.start()
        self.after(150, self._poll_terrain_render)

    def _poll_terrain_render(self) -> None:
        try:
            path, error = self._terrain_render_results.get_nowait()
        except queue.Empty:
            if self._terrain_render_thread and self._terrain_render_thread.is_alive():
                self.after(150, self._poll_terrain_render)
            return
        self.render_terrain_button.configure(state="normal")
        if error:
            self.map_info_var.set("Terrain rendering failed")
            self.set_status("Terrain rendering failed")
            messagebox.showerror(APP_TITLE, f"Could not render detailed terrain:\n\n{error}")
            return
        if path and self.save_data and path == self._terrain_cache_file():
            self._terrain_render_ready(path)

    def _terrain_render_ready(self, path: Path) -> None:
        self._terrain_cache_path = path
        self._terrain_photo = None
        self._terrain_source_photo = None
        self._terrain_source_path = None
        self._terrain_photo_path = None
        self.map_info_var.set("Top-down terrain atlas ready — roads, biomes, water and generated POIs")
        self.set_status(f"Detailed world map ready: {path}")
        self.redraw_map()
        if self._selected_map_feature and self._selected_map_feature.world_id == 1:
            self._center_map_on(self._selected_map_feature.x, self._selected_map_feature.y)

    def _draw_terrain_overlays(
        self, left: float, top: float, width: int, height: int, grid: TerrainGrid, data: WorldMapData | None
    ) -> None:
        if not data:
            return
        x_min, _x_max, _y_min, y_max = grid.visible_bounds

        def xy(point: MapPoint) -> tuple[float, float]:
            return (
                left + (point.x - x_min + 0.5) * width / grid.width,
                top + (y_max - point.y + 0.5) * height / grid.height,
            )

        def dots(points: list[MapPoint], color: str, radius: int) -> None:
            for point in points:
                x, y = xy(point)
                if left <= x <= left + width and top <= y <= top + height:
                    self.map_canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")

        if self.map_show_roads.get():
            cell_width = width / grid.width
            cell_height = height / grid.height
            for cell in grid.cells.values():
                if cell.has_road:
                    x = left + (cell.x - x_min) * cell_width
                    y = top + (y_max - cell.y) * cell_height
                    self.map_canvas.create_rectangle(
                        x, y, x + cell_width, y + cell_height,
                        outline="#ffe082", width=1,
                    )
        if self.map_show_harvestables.get():
            dots(data.harvestables, "#52ad65", 1)
        if self.map_show_rigid.get():
            dots(data.rigid_bodies, "#42a5f5", 2)
        if self.map_show_units.get():
            dots(data.units, "#ff9f35", 2)
        if self.map_show_portals.get():
            dots(data.portals, "#ef5350", 4)

    def redraw_map(self) -> None:
        self._map_redraw_job = None
        self.map_canvas.delete("all")
        if not self.save_data or not self.map_world_var.get():
            self.map_canvas.create_text(20, 20, text="Load a save to see its schematic map.", fill="white", anchor="nw")
            return
        world_id = int(self.map_world_var.get())
        data = self.save_data.world_maps.get(world_id)
        grid = self.save_data.terrain_grid if world_id == 1 else None
        if self.map_mode_var.get() == "Detailed terrain" and grid:
            if not self._terrain_cache_path or not self._terrain_cache_path.is_file():
                self.map_canvas.create_text(
                    20, 20,
                    text="The real terrain grid is decoded. Click “Build terrain map” to combine it with the installed game artwork.",
                    fill="white", anchor="nw", width=max(self.map_canvas.winfo_width() - 40, 300),
                    font=("Segoe UI", 11),
                )
                return
            canvas_width = max(self.map_canvas.winfo_width(), 300)
            canvas_height = max(self.map_canvas.winfo_height(), 240)
            scale = self._map_scale or self._fit_map_scale()
            if (
                self._terrain_photo is None
                or self._terrain_photo_path != self._terrain_cache_path
                or self._terrain_photo_factor != scale
            ):
                if self._terrain_source_photo is None or self._terrain_source_path != self._terrain_cache_path:
                    self._terrain_source_photo = tk.PhotoImage(file=str(self._terrain_cache_path))
                    self._terrain_source_path = self._terrain_cache_path
                numerator, denominator = scale
                source = self._terrain_source_photo
                if numerator == denominator:
                    self._terrain_photo = source
                else:
                    self._terrain_photo = source.zoom(numerator, numerator).subsample(denominator, denominator)
                self._terrain_photo_path = self._terrain_cache_path
                self._terrain_photo_factor = scale
            photo = self._terrain_photo
            region_width = max(canvas_width, photo.width())
            region_height = max(canvas_height, photo.height())
            left = (region_width - photo.width()) / 2
            top = (region_height - photo.height()) / 2
            self.map_canvas.configure(scrollregion=(0, 0, region_width, region_height))
            self._map_image_geometry = (left, top, photo.width(), photo.height())
            self.map_canvas.create_image(left, top, image=photo, anchor="nw")
            self._draw_terrain_overlays(left, top, photo.width(), photo.height(), grid, data)
            if self.map_search_var.get().strip():
                x_min, _x_max, _y_min, y_max = grid.visible_bounds
                for feature in self._map_search_matches[:100]:
                    if feature.world_id != world_id:
                        continue
                    marker_x = left + (feature.x - x_min + 0.5) * photo.width() / grid.width
                    marker_y = top + (y_max - feature.y + 0.5) * photo.height() / grid.height
                    self.map_canvas.create_oval(
                        marker_x - 5, marker_y - 5, marker_x + 5, marker_y + 5,
                        fill="#7e57c2", outline="white", width=2,
                    )
            if self._selected_map_feature and self._selected_map_feature.world_id == world_id:
                feature = self._selected_map_feature
                x_min, _x_max, _y_min, y_max = grid.visible_bounds
                marker_x = left + (feature.x - x_min + 0.5) * photo.width() / grid.width
                marker_y = top + (y_max - feature.y + 0.5) * photo.height() / grid.height
                radius = max(8, round(photo.width() / grid.width * 0.7))
                self.map_canvas.create_oval(
                    marker_x - radius, marker_y - radius, marker_x + radius, marker_y + radius,
                    outline="#ffffff", fill="#ef5350", width=3,
                )
                self.map_canvas.create_text(
                    marker_x, marker_y - radius - 5, text=feature.label,
                    fill="white", anchor="s", font=("Segoe UI", 9, "bold"),
                )
            legend: list[str] = []
            if self.map_show_roads.get():
                legend.append("Yellow: road cells")
            if self.map_show_rigid.get():
                legend.append("Blue: creations")
            if self.map_show_units.get():
                legend.append("Orange: bots")
            if self.map_show_harvestables.get():
                legend.append("Green: harvestables")
            if self.map_show_portals.get():
                legend.append("Red: portals")
            if legend:
                self.map_canvas.create_text(
                    left + 8, top + 8, text="  •  ".join(legend),
                    fill="white", anchor="nw", font=("Segoe UI", 9, "bold"),
                )
            fit = self._fit_map_scale()
            percent = round((scale[0] / scale[1]) / (fit[0] / fit[1]) * 100)
            self.map_zoom_var.set("Fit" if self._map_scale is None else f"{percent}%")
        elif data:
            self.map_canvas.configure(scrollregion="")
            MapRenderer.draw_canvas(self.map_canvas, data)
        else:
            self.map_canvas.create_text(20, 20, text="No positional map data is available for this world.", fill="white", anchor="nw")

    def export_map(self) -> None:
        if not self.save_data or not self.map_world_var.get():
            messagebox.showwarning(APP_TITLE, "Load a mapped world first.")
            return
        world_id = int(self.map_world_var.get())
        data = self.save_data.world_maps.get(world_id)
        detailed = self.map_mode_var.get() == "Detailed terrain" and world_id == 1 and self.save_data.terrain_grid
        if detailed and (not self._terrain_cache_path or not self._terrain_cache_path.is_file()):
            self.render_terrain_map()
            messagebox.showinfo(APP_TITLE, "The high-resolution terrain map is being built. Export it when the status says it is ready.")
            return
        if not detailed and not data:
            return
        suffix = "terrain" if detailed else "schematic"
        suggested = self.save_data.path.with_name(f"{self.save_data.path.stem}_world_{world_id}_{suffix}.png")
        chosen = filedialog.asksaveasfilename(
            title=f"Export {suffix} map",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            defaultextension=".png",
            filetypes=(("PNG image", "*.png"),),
        )
        if not chosen:
            return
        try:
            if detailed:
                assert self._terrain_cache_path is not None
                shutil.copyfile(self._terrain_cache_path, chosen)
            else:
                assert data is not None
                MapRenderer.export_png(Path(chosen), data)
            messagebox.showinfo(APP_TITLE, f"Map exported:\n\n{chosen}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not export map:\n\n{exc}")


def cli_inspect(path: str) -> int:
    data = SaveData(path)
    print(json.dumps(data.summary(), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", metavar="SAVE.db", help="print a read-only JSON summary and exit")
    args = parser.parse_args(argv)
    if args.inspect:
        return cli_inspect(args.inspect)
    app = EditorApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        raise
