from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import re
import shutil
import tempfile
import uuid
import zlib
from typing import Any

from scrap_mechanic_save_editor import ItemCatalog, load_game_json


TAG_PATTERN = re.compile(r"^#\{([^}]+)\}$")
COLOR_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}")


def _friendly(value: str) -> str:
    value = re.sub(r"^(quest|craftbot|obj|blk|jnt|tool|log)_", "", value, flags=re.I)
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    return re.sub(r"\s+", " ", value).strip().title() or "Unknown"


def _load_tags(path: Path) -> dict[str, str]:
    tags: dict[str, str] = {}
    if not path.is_file():
        return tags
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("-"):
            continue
        key, separator, value = line.partition(" ")
        if separator and key and value:
            tags[key] = COLOR_PATTERN.sub("", value.strip())
    return tags


def _resolve_tag(value: Any, tags: dict[str, str], fallback: str) -> str:
    text = str(value or "")
    match = TAG_PATTERN.match(text)
    if match:
        return tags.get(match.group(1), fallback)
    return COLOR_PATTERN.sub("", text).strip() or fallback


def _item_uuid_by_internal(catalog: ItemCatalog) -> dict[str, str]:
    return {item.internal_name: item.uuid for item in catalog.items.values() if item.internal_name}


def load_quest_catalog(game_root: Path, catalog: ItemCatalog) -> list[dict[str, Any]]:
    source = game_root / "Survival" / "ScriptableObjects" / "scriptableObjectSets" / "sob_quests.sobset"
    tags = _load_tags(game_root / "Data" / "Gui" / "Language" / "English" / "QuestInterfaceTags.txt")
    internal = _item_uuid_by_internal(catalog)
    result: list[dict[str, Any]] = []
    for index, entry in enumerate(load_game_json(source).get("scriptableObjectList", [])):
        quest_id = str(entry.get("name") or "")
        if not quest_id:
            continue
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        rewards = []
        for reward in data.get("rewards") or []:
            if not isinstance(reward, dict):
                continue
            reward_type = str(reward.get("type") or "unknown")
            name = str(reward.get("name") or reward.get("uuid") or "")
            rewards.append(
                {
                    "type": reward_type,
                    "name": _friendly(name) if name else reward_type.title(),
                    "internalName": name,
                    "uuid": internal.get(name) or str(reward.get("uuid") or ""),
                }
            )
        quest_type = "Main" if data.get("mainQuest") else "Farmer" if data.get("farmerQuest") else "Side"
        result.append(
            {
                "id": quest_id,
                "title": _resolve_tag(data.get("title"), tags, _friendly(quest_id)),
                "type": quest_type,
                "order": index,
                "uuid": str(entry.get("uuid") or ""),
                "rewards": rewards,
            }
        )
    return result


def load_recipe_catalog(game_root: Path, catalog: ItemCatalog) -> list[dict[str, Any]]:
    craftbot_root = game_root / "Survival" / "CraftingRecipes" / "craftbot"
    sets = load_game_json(craftbot_root / "craftbot.json")
    recipes: dict[str, dict[str, Any]] = {}
    for set_name, virtual_path in sets.items():
        relative = str(virtual_path).replace("$SURVIVAL_DATA/", "")
        path = game_root / "Survival" / Path(relative)
        for entry in load_game_json(path):
            uuid_text = str(entry.get("itemId") or "").casefold()
            if not uuid_text:
                continue
            item = catalog.get(uuid_text)
            record = recipes.setdefault(
                uuid_text,
                {
                    "uuid": uuid_text,
                    "name": item.title if item else _friendly(uuid_text),
                    "description": item.description if item else "",
                    "groups": [],
                },
            )
            group = _friendly(str(set_name))
            if group not in record["groups"]:
                record["groups"].append(group)
    return sorted(recipes.values(), key=lambda item: (item["name"].casefold(), item["uuid"]))


def load_log_catalog(game_root: Path) -> list[dict[str, Any]]:
    source = game_root / "Survival" / "Logs" / "log_entries.json"
    tags = _load_tags(game_root / "Data" / "Gui" / "Language" / "English" / "LogItemInterfaceTags.txt")
    result = []
    for index, entry in enumerate(load_game_json(source).get("logList", [])):
        uuid_text = str(entry.get("uuid") or "")
        if not uuid_text:
            continue
        internal = str(entry.get("name") or uuid_text)
        result.append(
            {
                "uuid": uuid_text,
                "name": _resolve_tag(entry.get("title"), tags, _friendly(internal)),
                "description": _resolve_tag(entry.get("description"), tags, ""),
                "type": str(entry.get("type") or "log").title(),
                "order": index,
            }
        )
    return result


def load_customization_catalog(game_root: Path) -> list[dict[str, str]]:
    source = game_root / "Data" / "Gui" / "Language" / "English" / "CustomizationDescriptions.json"
    item_source = game_root / "Survival" / "Scripts" / "game" / "survival_items.lua"
    internal_by_uuid: dict[str, str] = {}
    try:
        item_text = item_source.read_text(encoding="utf-8", errors="replace")
        for internal, uuid_text in re.findall(
            r'^\s*(outfit_[A-Za-z0-9_]+)\s*=\s*sm\.uuid\.new\(\s*"([0-9a-fA-F-]{36})"',
            item_text,
            flags=re.MULTILINE,
        ):
            internal_by_uuid[str(uuid.UUID(uuid_text))] = internal
    except OSError:
        pass
    result = []
    for uuid_text, entry in load_game_json(source).items():
        try:
            normalized = str(uuid.UUID(str(uuid_text)))
        except ValueError:
            continue
        title = str(entry.get("title") or normalized)
        lower = title.casefold()
        hair = {
            "army", "bob", "bun", "cornrows", "hawk", "hinge", "mechanic",
            "natural", "princess", "samurai", "side bang", "uppercut", "waffle",
        }
        facial_hair = {
            "baguette", "bearing", "classic", "continental", "duckietail", "fat walrus",
            "gentlemen", "handlebar", "king beard", "lumberjack", "piston", "scarf",
            "schnitzel", "villain",
        }
        category = next(
            (name for keyword, name in (
                ("face ", "Face"),
                ("backpack", "Backpack"), ("hat", "Hat"), ("helmet", "Hat"),
                ("jacket", "Torso"), ("shirt", "Torso"), ("vest", "Torso"), ("sweat", "Torso"),
                ("glove", "Gloves"), ("pants", "Legs"), ("shorts", "Legs"),
                ("shoe", "Shoes"), ("boot", "Shoes"),
            ) if keyword in lower),
            "Hair" if lower in hair else "Facial hair" if lower in facial_hair else "Other",
        )
        result.append({
            "uuid": normalized,
            "name": title,
            "category": category,
            "internal": internal_by_uuid.get(normalized, ""),
        })
    return sorted(result, key=lambda item: (item["name"].casefold(), item["uuid"]))


@dataclass
class CustomizationUnlockProfile:
    path: Path
    steam_id: int
    uuids: list[str]
    original_uuids: list[str]

    MAGIC = b"\x00\x00\x00\x01"

    @classmethod
    def load(cls, path: Path, steam_id: int) -> "CustomizationUnlockProfile":
        values: list[str] = []
        if path.is_file():
            blob = path.read_bytes()
            if len(blob) < 12 or blob[:4] != cls.MAGIC:
                raise ValueError("The clothing unlock file has an unsupported header")
            count = int.from_bytes(blob[8:12], "big")
            data = blob[12:]
            if len(data) != count * 16:
                raise ValueError("The clothing unlock file length does not match its item count")
            expected = zlib.crc32(int(steam_id).to_bytes(8, "little") + data).to_bytes(4, "big")
            if blob[4:8] != expected:
                raise ValueError("The clothing unlock checksum does not match this Steam user")
            values = [str(uuid.UUID(bytes=data[index : index + 16])) for index in range(0, len(data), 16)]
        return cls(path.resolve(), int(steam_id), values, list(values))

    @property
    def dirty(self) -> bool:
        return self.uuids != self.original_uuids

    def unlock(self, uuid_text: str) -> bool:
        normalized = str(uuid.UUID(uuid_text))
        if normalized in self.uuids:
            return False
        self.uuids.append(normalized)
        return True

    def lock(self, uuid_text: str) -> bool:
        normalized = str(uuid.UUID(uuid_text))
        if normalized not in self.uuids:
            return False
        self.uuids.remove(normalized)
        return True

    def reset_staged(self) -> int:
        changed = len(set(self.uuids) ^ set(self.original_uuids))
        self.uuids = list(self.original_uuids)
        return changed

    def save(self, create_adjacent_backup: bool = True) -> Path | None:
        if not self.dirty:
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if self.path.is_file() and create_adjacent_backup:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"{self.path.name}.{stamp}.bak")
            shutil.copy2(self.path, backup)
        data = b"".join(uuid.UUID(value).bytes for value in self.uuids)
        checksum = zlib.crc32(self.steam_id.to_bytes(8, "little") + data).to_bytes(4, "big")
        blob = self.MAGIC + checksum + len(self.uuids).to_bytes(4, "big") + data
        descriptor, temp_name = tempfile.mkstemp(prefix=".unlock-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            Path(temp_name).unlink(missing_ok=True)
            raise
        self.original_uuids = list(self.uuids)
        return backup
