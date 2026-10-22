from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import os
import struct
import threading
import xml.etree.ElementTree as ET

import lz4.block
from PIL import Image


@dataclass(frozen=True)
class IconFrame:
    x: int
    y: int
    width: int
    height: int


class ClothingIconCache:
    """Extract the game's real customization thumbnails into a persistent PNG cache."""

    def __init__(self, game_root: Path | None, cache_root: Path | None = None) -> None:
        self.game_root = game_root.resolve() if game_root else None
        self.cache_root = cache_root or (
            Path(os.getenv("LOCALAPPDATA") or Path.home())
            / "ScrapMechanicSaveEditor"
            / "clothing-icons"
        )
        self._lock = threading.RLock()
        self._loaded = False
        self._frames: dict[str, IconFrame] = {}
        self._atlas: Image.Image | None = None
        self._atlas_path: Path | None = None
        self._bundle_path: Path | None = None
        self._output_dir: Path | None = None

    @staticmethod
    def _decompress(path: Path, header_size: int, size_offset: int) -> bytes:
        blob = path.read_bytes()
        if len(blob) <= header_size or len(blob) < size_offset + 4:
            raise ValueError(f"Customization cache is truncated: {path.name}")
        size = struct.unpack_from("<I", blob, size_offset)[0]
        return lz4.block.decompress(blob[header_size:], uncompressed_size=size)

    def _load_index(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.game_root:
            return
        textures = self.game_root / "Cache" / "Textures"
        atlases = sorted(textures.glob("CustomizationIconMap_*.tco"), key=lambda path: path.stat().st_mtime_ns)
        bundle = self.game_root / "Cache" / "Bundle" / "customization_data.cbo"
        if not atlases or not bundle.is_file():
            return
        self._atlas_path = atlases[-1]
        self._bundle_path = bundle
        data = self._decompress(bundle, 20, 12)
        start = data.index(b"<?xml version")
        end = data.index(b"</MyGUI>", start) + len(b"</MyGUI>")
        root = ET.fromstring(data[start:end])
        group = next((node for node in root.iter("Group") if node.get("name") == "CustomizationIconMap"), None)
        if group is None:
            raise ValueError("Customization icon index is missing from the game cache")
        width, height = (int(value) for value in group.get("size", "96 96").split())
        for index in group.findall("Index"):
            frame = index.find("Frame")
            name = index.get("name")
            if not name or frame is None or not frame.get("point"):
                continue
            x, y = (int(value) for value in frame.get("point", "0 0").split())
            self._frames[name.casefold()] = IconFrame(x, y, width, height)
        signature_data = "|".join(
            f"{path.name}:{path.stat().st_size}:{path.stat().st_mtime_ns}"
            for path in (self._atlas_path, self._bundle_path)
        )
        signature = sha256(signature_data.encode("utf-8")).hexdigest()[:16]
        self._output_dir = self.cache_root / signature

    def _load_atlas(self) -> Image.Image:
        if self._atlas is not None:
            return self._atlas
        if self._atlas_path is None:
            raise ValueError("Customization icon atlas is unavailable")
        blob = self._atlas_path.read_bytes()
        if len(blob) < 48:
            raise ValueError("Customization icon atlas is truncated")
        width = struct.unpack_from("<I", blob, 28)[0]
        height = struct.unpack_from("<I", blob, 32)[0]
        pixels = self._decompress(self._atlas_path, 48, 20)
        expected = width * height * 4
        if len(pixels) != expected:
            raise ValueError(f"Unexpected customization atlas size ({len(pixels)} bytes, expected {expected})")
        self._atlas = Image.frombytes("RGBA", (width, height), pixels)
        return self._atlas

    def _frame_name(self, clothing_id: str, gender: int | None) -> str | None:
        normalized = clothing_id.casefold()
        preferred = "female" if gender == 1 else "male"
        alternate = "male" if preferred == "female" else "female"
        for name in (f"{normalized}_{preferred}", normalized, f"{normalized}_{alternate}"):
            if name in self._frames:
                return name
        return None

    def icon_url(self, clothing_id: str | None, gender: int | None = None) -> str | None:
        if not clothing_id:
            return None
        with self._lock:
            try:
                self._load_index()
                frame_name = self._frame_name(clothing_id, gender)
                if frame_name is None or self._output_dir is None:
                    return None
                output = self._output_dir / f"{frame_name}.png"
                if not output.is_file():
                    frame = self._frames[frame_name]
                    atlas = self._load_atlas()
                    icon = atlas.crop((frame.x, frame.y, frame.x + frame.width, frame.y + frame.height))
                    output.parent.mkdir(parents=True, exist_ok=True)
                    temporary = output.with_suffix(".tmp")
                    icon.save(temporary, format="PNG", optimize=True)
                    os.replace(temporary, output)
                return output.resolve().as_uri()
            except Exception:
                # Cache files are regenerated by the game and may be absent while it updates.
                # Missing artwork must never prevent the save editor from opening.
                return None

    @property
    def frame_count(self) -> int:
        with self._lock:
            try:
                self._load_index()
            except Exception:
                return 0
            return len(self._frames)
