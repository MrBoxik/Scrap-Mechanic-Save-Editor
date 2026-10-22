"""Build the save-specific assets consumed by Scrap Mechanic Map Companion."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrap_editor_app.backend import CACHE_DIR, EditorApi  # noqa: E402
from companion_overlay import scrap_mechanic_process_ids  # noqa: E402

MOD_ROOT = Path(__file__).resolve().parent / "ScrapMechanicMap"
BLOCK_UUID = "a6c6ce30-dd47-4587-b475-085d55c6a3b4"


@dataclass(frozen=True)
class Rectangle:
    x: int
    y: int
    width: int
    height: int
    z: int
    color: str


def newest_file(folder: Path, pattern: str) -> Path:
    files = [path for path in folder.glob(pattern) if path.is_file()]
    if not files:
        raise FileNotFoundError(f"No {pattern} files found in {folder}")
    return max(files, key=lambda path: path.stat().st_mtime_ns)


def default_save() -> Path:
    root = Path.home() / "AppData/Roaming/Axolot Games/Scrap Mechanic/User"
    candidates = list(root.glob("User_*/Save/Survival/*.db"))
    if not candidates:
        raise FileNotFoundError("No Survival .db save was found")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def load_world_index(save_path: Path) -> dict:
    if scrap_mechanic_process_ids():
        raise RuntimeError(
            "Close Scrap Mechanic before running the legacy offline map builder; "
            "it is not allowed to open a live game database."
        )
    api = EditorApi()
    loaded = api.load_save({"path": str(save_path)})
    index = api.map_3d_index({})
    index["sourceSeed"] = int(loaded["current"]["game"]["seed"])
    return index


def build_direction_icons() -> None:
    """Create crisp eight-way player arrows used by the live minimap."""
    target = MOD_ROOT / "Gui/MapCache"
    target.mkdir(parents=True, exist_ok=True)
    base = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base, "RGBA")
    draw.polygon([(48, 4), (78, 48), (59, 42), (59, 78), (37, 78), (37, 42), (18, 48)],
                 fill=(255, 194, 32, 255), outline=(13, 22, 25, 255), width=5)
    draw.ellipse((39, 67, 57, 85), fill=(255, 255, 255, 255), outline=(13, 22, 25, 255), width=4)
    for index in range(8):
        icon = base.rotate(-45 * index, resample=Image.Resampling.BICUBIC)
        icon.save(target / f"direction_{index}.png", "PNG", optimize=True)


def quantized_grid(image: Image.Image, columns: int, rows: int, colors: int) -> Image.Image:
    reduced = image.convert("RGB").resize((columns, rows), Image.Resampling.LANCZOS)
    reduced = ImageEnhance.Color(reduced).enhance(0.92)
    reduced = reduced.filter(ImageFilter.GaussianBlur(radius=0.18))
    return reduced.quantize(colors=colors, method=Image.Quantize.FASTOCTREE).convert("RGB")


def height_grid(index: dict, columns: int, rows: int) -> list[list[int]]:
    field = index.get("heightField") or {}
    source_width = max(1, int(field.get("width") or 1))
    source_height = max(1, int(field.get("height") or 1))
    elevation = list(field.get("elevation") or [])
    stride = source_width + 1
    result: list[list[float]] = []
    for y in range(rows):
        source_y = min(source_height, round((rows - 1 - y) * source_height / max(1, rows - 1)))
        line: list[float] = []
        for x in range(columns):
            source_x = min(source_width, round(x * source_width / max(1, columns - 1)))
            offset = source_y * stride + source_x
            line.append(float(elevation[offset]) if offset < len(elevation) else 0.0)
        result.append(line)
    minimum = min((value for row in result for value in row), default=0.0)
    maximum = max((value for row in result for value in row), default=minimum)
    span = max(1.0, maximum - minimum)
    return [[round((value - minimum) / span * 10) for value in row] for row in result]


def merge_rectangles(image: Image.Image, heights: list[list[int]]) -> list[Rectangle]:
    width, height = image.size
    pixels = image.load()
    active: dict[tuple[int, int, str, int], list[int]] = {}
    finished: list[Rectangle] = []
    for y in range(height):
        runs: list[tuple[int, int, str, int]] = []
        x = 0
        while x < width:
            rgb = pixels[x, y]
            color = "{:02X}{:02X}{:02X}".format(*rgb)
            z = heights[y][x]
            end = x + 1
            while end < width and pixels[end, y] == rgb and heights[y][end] == z:
                end += 1
            runs.append((x, end, color, z))
            x = end
        live = set(runs)
        for key in list(active):
            if key not in live:
                x0, x1, color, z = key
                start, last = active.pop(key)
                finished.append(Rectangle(x0, start, x1 - x0, last - start + 1, z, color))
        for key in runs:
            if key in active:
                active[key][1] = y
            else:
                active[key] = [y, y]
    for (x0, x1, color, z), (start, last) in active.items():
        finished.append(Rectangle(x0, start, x1 - x0, last - start + 1, z, color))
    return finished


def build_blueprint(rectangles: list[Rectangle]) -> dict:
    children = []
    for rect in rectangles:
        children.append({
            "bounds": {"x": rect.width, "y": rect.height, "z": 1},
            "color": rect.color,
            "pos": {"x": rect.x, "y": rect.y, "z": rect.z},
            "shapeId": BLOCK_UUID,
            "xaxis": 1,
            "zaxis": 3,
        })
    return {"bodies": [{"childs": children}], "version": 4}


def build(source: Path, save_path: Path, grid: int, colors: int, minimap_size: int) -> dict:
    index = load_world_index(save_path)
    bounds = [int(value) for value in index["bounds"]]
    with Image.open(source) as loaded:
        original = loaded.convert("RGB")
    image_target = MOD_ROOT / "Gui/MapCache/world_map.png"
    image_target.parent.mkdir(parents=True, exist_ok=True)
    map_copy = original.copy()
    map_copy.thumbnail((minimap_size, minimap_size), Image.Resampling.LANCZOS)
    map_copy.save(image_target, "PNG", optimize=True, compress_level=9)
    build_direction_icons()

    preview = original.copy()
    preview.thumbnail((640, 360), Image.Resampling.LANCZOS)
    backdrop = Image.new("RGB", (640, 360), (9, 25, 29))
    backdrop.paste(preview, ((640 - preview.width) // 2, (360 - preview.height) // 2))
    backdrop.save(MOD_ROOT / "preview.jpg", "JPEG", quality=90, optimize=True)

    x_min, x_max, y_min, y_max = bounds
    config = {
        "schema": 1,
        "sourceSave": save_path.name,
        "sourceSeed": int(index.get("sourceSeed") or 0),
        "sourceMap": source.name,
        "mapTexture": "MapCompanion/world_map.png",
        "worldMinX": x_min * 64,
        "worldMaxX": (x_max + 1) * 64,
        "worldMinY": y_min * 64,
        "worldMaxY": (y_max + 1) * 64,
        "cellBounds": bounds,
        "exploration": {"enabled": False, "cellSize": 64},
    }
    config_target = MOD_ROOT / "MapCache/map_config.json"
    config_target.parent.mkdir(parents=True, exist_ok=True)
    config_target.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Editor-generated orthographic PNG")
    parser.add_argument("--save", type=Path, help="Matching Survival save database")
    parser.add_argument("--grid", type=int, default=96, help="Maximum low-poly width (64-192)")
    parser.add_argument("--colors", type=int, default=32, help="Colour palette size (8-64)")
    parser.add_argument("--minimap-size", type=int, default=4096, help="Maximum minimap texture side")
    args = parser.parse_args()
    source = (args.source or newest_file(CACHE_DIR / "generated-3d", "*.png")).resolve()
    save_path = (args.save or default_save()).resolve()
    if not source.is_file():
        parser.error(f"Map image does not exist: {source}")
    if not save_path.is_file():
        parser.error(f"Save does not exist: {save_path}")
    grid = max(64, min(192, args.grid))
    colors = max(8, min(64, args.colors))
    config = build(source, save_path, grid, colors, max(512, min(4096, args.minimap_size)))
    print(f"Built map companion for {config['sourceSave']}")
    print(f"Minimap cache: {config['sourceMap']} ({config['sourceSave']})")
    print("The companion displays this generated 2D map; no in-game 3D view is installed.")
    print(f"Package: {MOD_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
