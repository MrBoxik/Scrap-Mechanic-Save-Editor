"""Pack extracted 96px item icons into one WebView-friendly texture atlas."""

from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "item-icons-hd"
ATLAS_PATH = ROOT / "assets" / "item-icon-atlas.png"
MANIFEST_PATH = ROOT / "assets" / "item-icon-atlas.json"
CELL = 96
COLUMNS = 64


def main() -> int:
    icons = sorted(SOURCE.glob("*.png"))
    if not icons:
        raise SystemExit(f"No PNG icons found in {SOURCE}")
    rows = math.ceil(len(icons) / COLUMNS)
    atlas = Image.new("RGBA", (COLUMNS * CELL, rows * CELL), (0, 0, 0, 0))
    positions: dict[str, list[int]] = {}
    for index, path in enumerate(icons):
        x = index % COLUMNS * CELL
        y = index // COLUMNS * CELL
        with Image.open(path) as source:
            icon = source.convert("RGBA")
            if icon.size != (CELL, CELL):
                icon.thumbnail((CELL, CELL), Image.Resampling.LANCZOS)
                centered = Image.new("RGBA", (CELL, CELL), (0, 0, 0, 0))
                centered.alpha_composite(icon, ((CELL - icon.width) // 2, (CELL - icon.height) // 2))
                icon = centered
            atlas.alpha_composite(icon, (x, y))
        positions[path.stem.casefold()] = [x, y]
    atlas.save(ATLAS_PATH, optimize=True)
    MANIFEST_PATH.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "cellPixels": CELL,
                "columns": COLUMNS,
                "rows": rows,
                "icons": positions,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Packed {len(icons)} icons into {ATLAS_PATH.name} ({atlas.width}x{atlas.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
