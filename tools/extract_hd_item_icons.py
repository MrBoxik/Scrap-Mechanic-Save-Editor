"""Extract original-resolution Scrap Mechanic item frames for the desktop UI."""

from __future__ import annotations

import argparse
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

from PIL import Image

from scrap_mechanic_save_editor import ItemCatalog, find_game_root


ROOT = Path(__file__).resolve().parent.parent


def normalized(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def atlas_frames(game_root: Path) -> dict[str, tuple[Path, tuple[int, int, int, int]]]:
    frames: dict[str, tuple[Path, tuple[int, int, int, int]]] = {}
    for relative in (
        Path("Survival/Gui/IconMapSurvival.xml"),
        Path("Data/Gui/IconMap.xml"),
        Path("ChallengeData/Gui/IconMapChallenge.xml"),
    ):
        xml_path = game_root / relative
        if not xml_path.is_file():
            continue
        root = ET.parse(xml_path).getroot()
        for group in root.iter("Group"):
            texture = group.get("texture")
            size = group.get("size", "96 96").split()
            if not texture or len(size) != 2:
                continue
            width, height = map(int, size)
            image_path = xml_path.parent / texture
            for index in group.findall("Index"):
                key = normalized(str(index.get("name") or ""))
                frame = index.find("Frame")
                point = frame.get("point", "").split() if frame is not None else []
                if key and image_path.is_file() and len(point) == 2:
                    x, y = map(int, point)
                    frames[key] = (image_path, (x, y, x + width, y + height))
    return frames


def direct_icons(game_root: Path) -> dict[str, Path]:
    icons: dict[str, Path] = {}
    for relative in (
        Path("Survival/Gui/AssetIcons"), Path("Survival/Gui/VoxelMeshIcons"),
        Path("Data/Gui/AssetIcons"), Path("ChallengeData/Gui/AssetIcons"),
    ):
        directory = game_root / relative
        if not directory.is_dir():
            continue
        for path in directory.glob("*.png"):
            key = normalized(path.stem)
            if key:
                icons.setdefault(key, path)
    return icons


def extract(game_root: Path, output: Path) -> tuple[int, int]:
    catalog = ItemCatalog()
    catalog.load(game_root)
    frames = atlas_frames(game_root)
    direct = direct_icons(game_root)
    output.mkdir(parents=True, exist_ok=True)
    atlases: dict[Path, Image.Image] = {}
    written = 0
    missing = 0
    try:
        for key in sorted(catalog.items):
            source = direct.get(key)
            if source:
                with Image.open(source) as image:
                    icon = image.convert("RGBA")
            elif key in frames:
                atlas_path, box = frames[key]
                atlas = atlases.get(atlas_path)
                if atlas is None:
                    atlas = Image.open(atlas_path).convert("RGBA")
                    atlases[atlas_path] = atlas
                icon = atlas.crop(box)
            else:
                missing += 1
                continue
            icon.save(output / f"{key}.png", optimize=True)
            written += 1
    finally:
        for atlas in atlases.values():
            atlas.close()
    return written, missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-root", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "assets" / "item-icons-hd")
    args = parser.parse_args()
    game_root = args.game_root or find_game_root()
    if not game_root:
        raise SystemExit("Scrap Mechanic installation was not found")
    written, missing = extract(game_root.resolve(), args.output.resolve())
    print(f"Wrote {written} original-resolution icons; {missing} catalogue entries have no game frame.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
