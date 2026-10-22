from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from typing import Any


CHARACTER_SLOTS = (
    "Face", "Hair", "Facial hair", "Torso", "Gloves",
    "Shoes", "Pants", "Hat", "Backpack",
)


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


@dataclass
class CharacterProfile:
    path: Path
    version: int
    gender: int
    options: list[str | None]
    palettes: list[int]
    original: bytes

    @classmethod
    def load(cls, path: Path) -> "CharacterProfile":
        blob = path.read_bytes()
        if len(blob) < 6:
            raise ValueError("The character profile is truncated")
        version = int.from_bytes(blob[0:4], "big")
        gender, count = blob[4], blob[5]
        expected = 6 + count * 20
        if version != 2 or count != len(CHARACTER_SLOTS) or len(blob) != expected:
            raise ValueError("The character profile uses an unsupported format")
        option_start = 6
        palette_start = option_start + count * 16
        options = []
        for index in range(count):
            raw = blob[option_start + index * 16 : option_start + (index + 1) * 16]
            options.append(str(uuid.UUID(bytes=raw)) if any(raw) else None)
        palettes = [
            int.from_bytes(blob[palette_start + index * 4 : palette_start + (index + 1) * 4], "big")
            for index in range(count)
        ]
        return cls(path.resolve(), version, gender, options, palettes, blob)

    @property
    def dirty(self) -> bool:
        return self.to_bytes() != self.original

    def set_gender(self, gender: int) -> None:
        if gender not in (0, 1):
            raise ValueError("Gender must be Male (0) or Female (1)")
        self.gender = gender

    def set_option(self, index: int, option: str | None, palette: int) -> None:
        if not 0 <= index < len(CHARACTER_SLOTS):
            raise ValueError("Unknown character customization slot")
        if not 0 <= palette <= 255:
            raise ValueError("Palette index must be between 0 and 255")
        self.options[index] = str(uuid.UUID(option)) if option else None
        self.palettes[index] = int(palette)

    def to_bytes(self) -> bytes:
        values = b"".join(uuid.UUID(value).bytes if value else bytes(16) for value in self.options)
        palettes = b"".join(int(value).to_bytes(4, "big") for value in self.palettes)
        return self.version.to_bytes(4, "big") + bytes((self.gender, len(self.options))) + values + palettes

    def save(self) -> None:
        _atomic_bytes(self.path, self.to_bytes())
        self.original = self.to_bytes()


class BlueprintManager:
    def __init__(self, user_dir: Path, archive_root: Path) -> None:
        self.user_dir = user_dir.resolve()
        self.root = self.user_dir / "Blueprints"
        self.archive_root = archive_root.resolve() / "Blueprint Archives"

    @staticmethod
    def _description(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _blueprint_counts(path: Path) -> tuple[int, int]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            return len(value.get("bodies", [])), len(value.get("joints", []))
        except (OSError, ValueError, TypeError, AttributeError):
            return 0, 0

    def list(self) -> list[dict[str, Any]]:
        result = []
        if not self.root.is_dir():
            return result
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir():
                continue
            try:
                local_id = str(uuid.UUID(folder.name))
            except ValueError:
                continue
            description = self._description(folder / "description.json")
            description_text = str(description.get("description") or "")
            if description_text.startswith("#{") and description_text.endswith("}"):
                description_text = ""
            bodies, joints = self._blueprint_counts(folder / "blueprint.json")
            icon = folder / "icon.png"
            result.append({
                "id": local_id,
                "name": str(description.get("name") or local_id),
                "description": description_text,
                "fileId": description.get("fileId"),
                "bodies": bodies,
                "joints": joints,
                "path": str(folder.resolve()),
                "icon": icon.resolve().as_uri() if icon.is_file() else None,
            })
        return sorted(result, key=lambda item: (item["name"].casefold(), item["id"]))

    def _folder(self, blueprint_id: str) -> Path:
        normalized = str(uuid.UUID(blueprint_id))
        folder = (self.root / normalized).resolve()
        if folder.parent != self.root.resolve() or not folder.is_dir():
            raise ValueError("Local blueprint was not found")
        return folder

    def export(self, blueprint_id: str, label: str | None = None) -> Path:
        folder = self._folder(blueprint_id)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        safe = "".join(character if character.isalnum() or character in " -_" else "_" for character in (label or folder.name)).strip()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.archive_root / f"{safe or folder.name}-{stamp}.zip"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source in folder.rglob("*"):
                if source.is_file():
                    archive.write(source, Path(folder.name) / source.relative_to(folder))
        return target

    def rename(self, blueprint_id: str, name: str, description: str) -> Path:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise ValueError("Blueprint name must contain 1–120 characters")
        if len(description) > 2000:
            raise ValueError("Blueprint description is too long")
        folder = self._folder(blueprint_id)
        backup = self.export(blueprint_id, f"Before rename - {clean_name}")
        path = folder / "description.json"
        value = self._description(path)
        value.update({"name": clean_name, "description": description, "localId": folder.name, "type": "Blueprint"})
        value.setdefault("version", 0)
        _atomic_json(path, value)
        return backup

    def duplicate(self, blueprint_id: str, name: str | None = None) -> str:
        source = self._folder(blueprint_id)
        new_id = str(uuid.uuid4())
        target = self.root / new_id
        shutil.copytree(source, target)
        description_path = target / "description.json"
        value = self._description(description_path)
        value.pop("fileId", None)
        value["localId"] = new_id
        value["name"] = (name or f"{value.get('name') or 'Blueprint'} copy").strip()[:120]
        _atomic_json(description_path, value)
        cache = target / "Cache"
        if cache.is_dir():
            shutil.rmtree(cache)
        return new_id

    def import_archive(self, archive_path: Path) -> str:
        source = archive_path.expanduser().resolve()
        if not source.is_file() or source.suffix.casefold() != ".zip":
            raise ValueError("Choose a Blueprint ZIP archive")
        with zipfile.ZipFile(source) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            blueprint_entry = next((entry for entry in files if Path(entry.filename).name == "blueprint.json"), None)
            description_entry = next((entry for entry in files if Path(entry.filename).name == "description.json"), None)
            if blueprint_entry is None or description_entry is None:
                raise ValueError("The archive does not contain blueprint.json and description.json")
            for entry in files:
                parts = Path(entry.filename).parts
                if entry.filename.startswith(("/", "\\")) or ".." in parts:
                    raise ValueError("The archive contains an unsafe path")
            blueprint = json.loads(archive.read(blueprint_entry).decode("utf-8-sig"))
            description = json.loads(archive.read(description_entry).decode("utf-8-sig"))
            if not isinstance(blueprint, dict) or not isinstance(description, dict):
                raise ValueError("The Blueprint archive contains invalid JSON")
            new_id = str(uuid.uuid4())
            target = self.root / new_id
            target.mkdir(parents=True)
            _atomic_json(target / "blueprint.json", blueprint)
            description.pop("fileId", None)
            description.update({"localId": new_id, "type": "Blueprint"})
            description.setdefault("name", source.stem)
            description.setdefault("version", 0)
            _atomic_json(target / "description.json", description)
            icon_entry = next((entry for entry in files if Path(entry.filename).name == "icon.png"), None)
            if icon_entry:
                _atomic_bytes(target / "icon.png", archive.read(icon_entry))
        return new_id
