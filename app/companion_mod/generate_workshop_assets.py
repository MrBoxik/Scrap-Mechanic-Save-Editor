from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent / "ScrapMechanicMap"
RESOLUTIONS = ("1280x720", "1920x1080", "2560x1440", "3840x2160")


def solid(path: Path, colour: tuple[int, int, int, int], size: tuple[int, int] = (32, 32)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", size, colour)
    draw = ImageDraw.Draw(image)
    # A quiet bevel keeps adjacent cells readable without turning the map into
    # a heavy checkerboard at the 12-pixel HUD scale.
    highlight = tuple(min(255, value + 12) for value in colour[:3]) + (colour[3],)
    shadow = tuple(max(0, value - 14) for value in colour[:3]) + (colour[3],)
    draw.line((0, 0, size[0] - 1, 0), fill=highlight, width=1)
    draw.line((0, 0, 0, size[1] - 1), fill=highlight, width=1)
    draw.line((0, size[1] - 1, size[0] - 1, size[1] - 1), fill=shadow, width=1)
    draw.line((size[0] - 1, 0, size[0] - 1, size[1] - 1), fill=shadow, width=1)
    image.save(path, optimize=True)


def arrow(path: Path, index: int) -> None:
    image = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    points = [(48, 5), (80, 78), (48, 62), (16, 78)]
    draw.polygon(points, fill=(255, 191, 28, 255), outline=(12, 22, 27, 255), width=5)
    image = image.rotate(-45 * index, resample=Image.Resampling.BICUBIC, center=(48, 48))
    image.save(path, optimize=True)


def poi(path: Path, colour: tuple[int, int, int, int], symbol: str) -> None:
    image = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((7, 7, 89, 89), fill=(10, 19, 23, 245), outline=(255, 206, 54, 255), width=6)
    draw.ellipse((22, 22, 74, 74), fill=colour)
    if symbol == "square":
        draw.rectangle((36, 36, 60, 60), outline=(255, 255, 255, 255), width=6)
    elif symbol == "diamond":
        draw.polygon([(48, 30), (66, 48), (48, 66), (30, 48)], fill=(255, 255, 255, 255))
    elif symbol == "rings":
        draw.ellipse((34, 34, 62, 62), outline=(255, 255, 255, 255), width=5)
        draw.ellipse((43, 43, 53, 53), fill=(255, 255, 255, 255))
    else:
        draw.polygon([(48, 29), (54, 42), (68, 44), (58, 54), (61, 68), (48, 61), (35, 68), (38, 54), (28, 44), (42, 42)], fill=(255, 255, 255, 255))
    image.save(path, optimize=True)


def panel(path: Path, size: tuple[int, int], radius: int) -> None:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, size[0] - 3, size[1] - 3), radius=radius, fill=(12, 22, 27, 238), outline=(255, 191, 28, 255), width=3)
    draw.rounded_rectangle((10, 30, size[0] - 11, size[1] - 28), radius=max(5, radius // 2), outline=(66, 85, 91, 255), width=2)
    image.save(path, optimize=True)


def workshop_preview(path: Path) -> None:
    source = ROOT / "Gui" / "MapCache" / "world_map.png"
    if source.is_file():
        background = Image.open(source).convert("RGB")
    else:
        background = Image.new("RGB", (1280, 960), (76, 126, 48))
    target = Image.new("RGB", (640, 360), (7, 18, 22))
    scaled = background.resize((480, 360), Image.Resampling.LANCZOS)
    target.paste(scaled, (160, 0))
    draw = ImageDraw.Draw(target, "RGBA")
    draw.rectangle((0, 0, 205, 360), fill=(8, 19, 23, 248))
    draw.polygon([(192, 0), (260, 0), (205, 360), (137, 360)], fill=(8, 19, 23, 190))
    draw.rectangle((0, 0, 640, 12), fill=(255, 187, 27, 255))
    draw.rectangle((22, 42, 134, 154), fill=(9, 28, 32, 245), outline=(255, 191, 28, 255), width=4)
    draw.line((37, 131, 70, 99, 98, 112, 121, 70), fill=(48, 151, 181, 255), width=8)
    draw.polygon([(79, 53), (105, 113), (79, 99), (53, 113)], fill=(255, 191, 28, 255), outline=(8, 19, 23, 255))
    draw.text((22, 183), "SURVIVAL", fill=(255, 191, 28, 255))
    draw.text((22, 207), "MAP", fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(255, 255, 255, 255))
    draw.text((22, 231), "COMPANION", fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(255, 255, 255, 255))
    draw.text((22, 286), "EXPLORE  |  DISCOVER", fill=(185, 199, 202, 255))
    draw.text((22, 309), "SEED-SAFE CUSTOM GAME", fill=(185, 199, 202, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    target.save(path, quality=94, subsampling=0, optimize=True)


def generate_into(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    colours = {
        "cell_unknown.png": (5, 14, 18, 255),
        "cell_land.png": (86, 133, 45, 255),
        "cell_road.png": (107, 113, 111, 255),
        "cell_water.png": (30, 112, 145, 255),
        "cell_dry.png": (188, 141, 72, 255),
        "cell_start.png": (255, 191, 28, 255),
        "cell_poi.png": (121, 77, 155, 255),
        "cell_station.png": (230, 166, 42, 255),
        "cell_packing.png": (46, 170, 178, 255),
        "cell_warehouse.png": (218, 89, 49, 255),
    }
    for name, colour in colours.items():
        solid(folder / name, colour)
    for index in range(8):
        arrow(folder / f"direction_{index}.png", index)
    poi(folder / "poi_start.png", (255, 191, 28, 255), "star")
    poi(folder / "poi_generic.png", (121, 77, 155, 255), "diamond")
    poi(folder / "poi_station.png", (230, 166, 42, 255), "rings")
    poi(folder / "poi_packing.png", (46, 170, 178, 255), "square")
    poi(folder / "poi_warehouse.png", (218, 89, 49, 255), "square")
    panel(folder / "panel.png", (232, 248), 15)
    panel(folder / "full_panel.png", (1280, 720), 20)


def main() -> None:
    generate_into(ROOT / "Gui" / "MapCompanion")
    for resolution in RESOLUTIONS:
        generate_into(ROOT / "Gui" / "Resolutions" / resolution / "MapCompanion")
    workshop_preview(ROOT / "preview.jpg")
    print(f"Generated Workshop GUI assets in {ROOT}")


if __name__ == "__main__":
    main()
