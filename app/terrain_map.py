"""Decode and render Scrap Mechanic 1.0 Survival terrain.

The game stores g_cellData as an LZ4-compressed Lua-object bitstream inside a
large ScriptData row. Each cell identifies a terrain tile UUID, rotation, and
(for multi-cell POIs) tile offset. The primary renderer uses a clean top-down
cell atlas; installed .tileson/isometric preview files remain as a fallback.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import struct
from typing import Any
import uuid
import zlib

from PIL import Image, ImageChops, ImageDraw, ImageFilter


LUA_HEADER = b"LUA\x00\x00\x00\x01"
TERRAIN_KEYS = ("seed", "flags", "bounds", "uid", "xOffset", "yOffset", "rotation", "groupId")
TERRAIN_CORNER_KEYS = ("elevation", "cliffLevel")
REFERENCE_TYPES = {
    10021, 10022, 10023, 10024, 10025, 10027, 10028, 10030,
    10031, 10032, 10036, 10037, 10038, 10039, 10042, 20002,
}


class TerrainDecodeError(RuntimeError):
    pass


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bit_offset = 0

    def read_unsigned(self, bits: int) -> int:
        if bits < 0:
            raise ValueError("Bit count cannot be negative")
        if bits == 0:
            return 0
        start = self.bit_offset
        end = start + bits
        if end > len(self.data) * 8:
            raise TerrainDecodeError("Unexpected end of Lua bitstream")

        byte_start = start >> 3
        bit_start = start & 7
        byte_count = (bit_start + bits + 7) >> 3
        chunk = int.from_bytes(
            self.data[byte_start : byte_start + byte_count], "big"
        )
        trailing_bits = byte_count * 8 - bit_start - bits
        self.bit_offset = end
        return (chunk >> trailing_bits) & ((1 << bits) - 1)

    def read_signed(self, bits: int) -> int:
        value = self.read_unsigned(bits)
        return value - (1 << bits) if value >= (1 << (bits - 1)) else value

    def read_bytes(self, length: int) -> bytes:
        if length < 0:
            raise ValueError("Byte count cannot be negative")
        if self.bit_offset % 8 == 0:
            start = self.bit_offset >> 3
            end = start + length
            if end > len(self.data):
                raise TerrainDecodeError("Unexpected end of Lua bitstream")
            self.bit_offset += length * 8
            return self.data[start:end]
        return bytes(self.read_unsigned(8) for _ in range(length))

    def read_float32(self) -> float:
        return struct.unpack(">f", self.read_bytes(4))[0]

    def align_to_byte(self) -> None:
        self.bit_offset += (-self.bit_offset) % 8


class BitWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.bit_offset = 0

    def write_unsigned(self, value: int, bits: int) -> None:
        if value < 0 or value >= 1 << bits:
            raise ValueError(f"{value} does not fit in {bits} unsigned bits")
        for shift in range(bits - 1, -1, -1):
            byte_index = self.bit_offset // 8
            if byte_index == len(self.data):
                self.data.append(0)
            if (value >> shift) & 1:
                self.data[byte_index] |= 1 << (7 - self.bit_offset % 8)
            self.bit_offset += 1

    def write_signed(self, value: int, bits: int) -> None:
        if not -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
            raise ValueError(f"{value} does not fit in {bits} signed bits")
        self.write_unsigned(value & ((1 << bits) - 1), bits)

    def write_bytes(self, value: bytes) -> None:
        for byte in value:
            self.write_unsigned(byte, 8)

    def write_float32(self, value: float) -> None:
        self.write_bytes(struct.pack(">f", float(value)))

    def write_float64(self, value: float) -> None:
        self.write_bytes(struct.pack(">d", float(value)))

    def align_to_byte(self) -> None:
        while self.bit_offset % 8:
            self.write_unsigned(0, 1)

    def to_bytes(self) -> bytes:
        return bytes(self.data)


@dataclass
class LuaArray:
    offset: int
    values: list[Any]

    def get(self, index: int) -> Any:
        position = index - self.offset
        if position < 0 or position >= len(self.values):
            raise TerrainDecodeError(f"Lua array index {index} is outside the stored range")
        return self.values[position]


@dataclass(frozen=True)
class LuaUuid:
    value: str


def parse_lua_value(reader: BitReader, depth: int = 0, preserve_keys: bool = False) -> Any:
    if depth > 256:
        raise TerrainDecodeError("Lua object nesting is too deep")
    value_type = reader.read_unsigned(8)
    if value_type == 1:
        return None
    if value_type == 2:
        return bool(reader.read_unsigned(1))
    if value_type == 3:
        return reader.read_float32()
    if value_type == 4:
        length = reader.read_unsigned(32)
        reader.align_to_byte()
        return reader.read_bytes(length).decode("utf-8")
    if value_type == 5:
        count = reader.read_unsigned(32)
        if reader.read_unsigned(1):
            offset = reader.read_signed(32)
            return LuaArray(offset, [parse_lua_value(reader, depth + 1, preserve_keys) for _ in range(count)])
        result: dict[Any, Any] = {}
        for _ in range(count):
            key = parse_lua_value(reader, depth + 1, preserve_keys)
            if preserve_keys:
                key_text = key
            elif isinstance(key, LuaUuid):
                key_text = key.value
            elif isinstance(key, (str, int, float, bool)) or key is None:
                key_text = str(key).lower() if isinstance(key, bool) else str(key)
            else:
                raise TerrainDecodeError(f"Unsupported Lua table key: {key!r}")
            result[key_text] = parse_lua_value(reader, depth + 1, preserve_keys)
        return result
    if value_type == 6:
        return reader.read_signed(32)
    if value_type == 7:
        return reader.read_signed(16)
    if value_type == 8:
        return reader.read_signed(8)
    if value_type == 11:
        return struct.unpack(">d", reader.read_bytes(8))[0]
    if value_type == 100:
        userdata_type = reader.read_unsigned(32)
        if userdata_type == 10001:
            return LuaUuid(str(uuid.UUID(bytes=reader.read_bytes(16)[::-1])))
        if userdata_type == 10003:
            return ("vec3", reader.read_float32(), reader.read_float32(), reader.read_float32())
        if userdata_type == 10004:
            return (
                "quat", reader.read_float32(), reader.read_float32(),
                reader.read_float32(), reader.read_float32(),
            )
        if userdata_type == 10005:
            return (
                "color", reader.read_float32(), reader.read_float32(),
                reader.read_float32(), reader.read_float32(),
            )
        if userdata_type in REFERENCE_TYPES:
            return ("reference", userdata_type, reader.read_unsigned(32))
        raise TerrainDecodeError(f"Unsupported Lua userdata type {userdata_type}")
    raise TerrainDecodeError(f"Unsupported Lua value type {value_type} at bit {reader.bit_offset - 8}")


def write_lua_value(writer: BitWriter, value: Any, depth: int = 0) -> None:
    """Serialize the Lua-object value types used by Survival storage rows."""
    if depth > 256:
        raise TerrainDecodeError("Lua object nesting is too deep")
    if value is None:
        writer.write_unsigned(1, 8)
    elif isinstance(value, bool):
        writer.write_unsigned(2, 8)
        writer.write_unsigned(int(value), 1)
    elif isinstance(value, int):
        writer.write_unsigned(6, 8)
        writer.write_signed(value, 32)
    elif isinstance(value, float):
        writer.write_unsigned(11, 8)
        writer.write_float64(value)
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        writer.write_unsigned(4, 8)
        writer.write_unsigned(len(encoded), 32)
        writer.align_to_byte()
        writer.write_bytes(encoded)
    elif isinstance(value, LuaUuid):
        writer.write_unsigned(100, 8)
        writer.write_unsigned(10001, 32)
        writer.write_bytes(uuid.UUID(value.value).bytes[::-1])
    elif isinstance(value, LuaArray):
        writer.write_unsigned(5, 8)
        writer.write_unsigned(len(value.values), 32)
        writer.write_unsigned(1, 1)
        writer.write_signed(value.offset, 32)
        for child in value.values:
            write_lua_value(writer, child, depth + 1)
    elif isinstance(value, dict):
        writer.write_unsigned(5, 8)
        writer.write_unsigned(len(value), 32)
        writer.write_unsigned(0, 1)
        for key, child in value.items():
            write_lua_value(writer, key, depth + 1)
            write_lua_value(writer, child, depth + 1)
    elif isinstance(value, tuple) and value:
        kind = value[0]
        writer.write_unsigned(100, 8)
        if kind == "vec3" and len(value) == 4:
            writer.write_unsigned(10003, 32)
            for component in value[1:]:
                writer.write_float32(component)
        elif kind == "quat" and len(value) == 5:
            writer.write_unsigned(10004, 32)
            for component in value[1:]:
                writer.write_float32(component)
        elif kind == "color" and len(value) == 5:
            writer.write_unsigned(10005, 32)
            for component in value[1:]:
                writer.write_float32(component)
        elif kind == "reference" and len(value) == 3:
            writer.write_unsigned(int(value[1]), 32)
            writer.write_unsigned(int(value[2]), 32)
        else:
            raise TerrainDecodeError(f"Unsupported Lua userdata value: {value!r}")
    else:
        raise TerrainDecodeError(f"Unsupported Lua value: {value!r}")


def serialize_lua_object(value: Any) -> bytes:
    writer = BitWriter()
    write_lua_value(writer, value)
    return LUA_HEADER + writer.to_bytes()


def compress_lz4_literals(source: bytes) -> bytes:
    """Create a valid LZ4 block consisting of a single literal sequence."""
    length = len(source)
    result = bytearray([min(length, 15) << 4])
    if length >= 15:
        remaining = length - 15
        while remaining >= 255:
            result.append(255)
            remaining -= 255
        result.append(remaining)
    result.extend(source)
    return bytes(result)


def wrap_script_data(original_blob: bytes, value: Any) -> bytes:
    """Replace a ScriptData payload while preserving its UID/key/world wrapper."""
    if len(original_blob) < 29:
        raise TerrainDecodeError("ScriptData row is too short")
    key_length = int.from_bytes(original_blob[16:18], "big")
    length_position = 18 + key_length + 2 + 1
    if length_position + 4 > len(original_blob):
        raise TerrainDecodeError("ScriptData wrapper is truncated")
    compressed = compress_lz4_literals(serialize_lua_object(value))
    return original_blob[:length_position] + len(compressed).to_bytes(4, "big") + compressed


def decompress_lz4_block(source: bytes) -> bytes:
    output = bytearray()
    position = 0
    while position < len(source):
        token = source[position]
        position += 1
        literal_length = token >> 4
        if literal_length == 15:
            extra = 255
            while extra == 255:
                if position >= len(source):
                    raise TerrainDecodeError("Truncated LZ4 literal length")
                extra = source[position]
                position += 1
                literal_length += extra
        if position + literal_length > len(source):
            raise TerrainDecodeError("LZ4 literal exceeds the source block")
        output.extend(source[position : position + literal_length])
        position += literal_length
        if position == len(source):
            break
        if position + 2 > len(source):
            raise TerrainDecodeError("Truncated LZ4 match offset")
        offset = source[position] | (source[position + 1] << 8)
        position += 2
        if offset == 0 or offset > len(output):
            raise TerrainDecodeError(f"Invalid LZ4 match offset {offset}")
        match_length = (token & 15) + 4
        if (token & 15) == 15:
            extra = 255
            while extra == 255:
                if position >= len(source):
                    raise TerrainDecodeError("Truncated LZ4 match length")
                extra = source[position]
                position += 1
                match_length += extra
        for _ in range(match_length):
            output.append(output[-offset])
    return bytes(output)


def unwrap_script_data(blob: bytes) -> bytes:
    if len(blob) < 29:
        raise TerrainDecodeError("ScriptData row is too short")
    key_length = int.from_bytes(blob[16:18], "big")
    length_position = 18 + key_length + 2 + 1
    if length_position + 4 > len(blob):
        raise TerrainDecodeError("ScriptData wrapper is truncated")
    compressed_length = int.from_bytes(blob[length_position : length_position + 4], "big")
    compressed = blob[length_position + 4 :]
    if compressed_length != len(compressed):
        raise TerrainDecodeError("ScriptData payload length does not match its wrapper")
    decompressed = decompress_lz4_block(compressed)
    if not decompressed.startswith(LUA_HEADER):
        raise TerrainDecodeError("Invalid Lua object header")
    return decompressed


def find_root_value(decompressed: bytes, key: str) -> Any:
    """Find a byte-aligned root key and parse the value immediately after it.

    Some root tables contain new userdata types unrelated to terrain. Parsing
    values independently keeps the decoder forward-compatible while still
    validating every terrain field used by the renderer.
    """
    encoded = key.encode("utf-8")
    position = len(LUA_HEADER)
    last_error: Exception | None = None
    while True:
        position = decompressed.find(encoded, position)
        if position < 0:
            break
        try:
            return parse_lua_value(BitReader(decompressed[position + len(encoded) :]))
        except Exception as exc:
            last_error = exc
            position += 1
    raise TerrainDecodeError(f"Terrain root key {key!r} could not be decoded: {last_error}")


def array_2d_get(value: Any, x: int, y: int, label: str) -> Any:
    if not isinstance(value, LuaArray):
        raise TerrainDecodeError(f"{label} is not a Lua array")
    row = value.get(y)
    if not isinstance(row, LuaArray):
        raise TerrainDecodeError(f"{label}[{y}] is not a Lua array")
    return row.get(x)


@dataclass(frozen=True)
class TerrainCell:
    x: int
    y: int
    uuid: str
    rotation: int
    x_offset: int
    y_offset: int
    flags: int
    group_id: int = 0

    @property
    def biome(self) -> int:
        return (self.flags & 0xF000) >> 12

    @property
    def has_road(self) -> bool:
        return bool(self.flags & 0x0F00)


@dataclass
class TerrainGrid:
    seed: int
    stored_bounds: tuple[int, int, int, int]
    visible_bounds: tuple[int, int, int, int]
    padding: int
    cells: dict[tuple[int, int], TerrainCell]
    elevation: dict[tuple[int, int], float] = field(default_factory=dict)
    cliff_level: dict[tuple[int, int], int] = field(default_factory=dict)

    @property
    def width(self) -> int:
        x_min, x_max, _y_min, _y_max = self.visible_bounds
        return x_max - x_min + 1

    @property
    def height(self) -> int:
        _x_min, _x_max, y_min, y_max = self.visible_bounds
        return y_max - y_min + 1


def decode_terrain_grid(connection: sqlite3.Connection, world_id: int = 1) -> TerrainGrid:
    rows = connection.execute(
        "SELECT data FROM ScriptData WHERE worldId=? ORDER BY length(data) DESC", (world_id,)
    ).fetchall()
    errors: list[str] = []
    for row in rows:
        blob = row[0]
        if not isinstance(blob, bytes):
            continue
        try:
            decompressed = unwrap_script_data(blob)
            root = {key: find_root_value(decompressed, key) for key in TERRAIN_KEYS}
            for key in TERRAIN_CORNER_KEYS:
                try:
                    root[key] = find_root_value(decompressed, key)
                except TerrainDecodeError:
                    root[key] = None
            bounds = root["bounds"]
            if not isinstance(bounds, dict):
                raise TerrainDecodeError("Terrain bounds is not a Lua dictionary")
            x_min = int(bounds["xMin"])
            x_max = int(bounds["xMax"])
            y_min = int(bounds["yMin"])
            y_max = int(bounds["yMax"])
            try:
                padding = int(find_root_value(decompressed, "padding"))
            except TerrainDecodeError:
                padding = 0
            visible = (x_min + padding, x_max - padding, y_min + padding, y_max - padding)
            cells: dict[tuple[int, int], TerrainCell] = {}
            for y in range(visible[2], visible[3] + 1):
                for x in range(visible[0], visible[1] + 1):
                    item_uuid = array_2d_get(root["uid"], x, y, "uid")
                    if not isinstance(item_uuid, LuaUuid):
                        raise TerrainDecodeError(f"uid[{y}][{x}] is not a UUID")
                    cell = TerrainCell(
                        x=x,
                        y=y,
                        uuid=item_uuid.value,
                        rotation=int(array_2d_get(root["rotation"], x, y, "rotation")),
                        x_offset=int(array_2d_get(root["xOffset"], x, y, "xOffset")),
                        y_offset=int(array_2d_get(root["yOffset"], x, y, "yOffset")),
                        flags=int(array_2d_get(root["flags"], x, y, "flags")),
                        group_id=int(array_2d_get(root["groupId"], x, y, "groupId")),
                    )
                    if not 0 <= cell.rotation <= 3:
                        raise TerrainDecodeError(f"Invalid rotation {cell.rotation} at {x},{y}")
                    cells[(x, y)] = cell
            elevation: dict[tuple[int, int], float] = {}
            cliff_level: dict[tuple[int, int], int] = {}
            for y in range(y_min, y_max + 2):
                for x in range(x_min, x_max + 2):
                    if root["elevation"] is not None:
                        elevation[(x, y)] = float(
                            array_2d_get(root["elevation"], x, y, "elevation")
                        )
                    if root["cliffLevel"] is not None:
                        cliff_level[(x, y)] = int(
                            array_2d_get(root["cliffLevel"], x, y, "cliffLevel")
                        )
            return TerrainGrid(
                int(root["seed"]),
                (x_min, x_max, y_min, y_max),
                visible,
                padding,
                cells,
                elevation,
                cliff_level,
            )
        except Exception as exc:
            errors.append(str(exc))
    raise TerrainDecodeError("Overworld terrain data was not found: " + "; ".join(errors[:3]))


def strip_json_comments(text: str) -> str:
    output: list[str] = []
    for line in text.splitlines():
        quoted = False
        escaped = False
        cut = len(line)
        for index, char in enumerate(line):
            if escaped:
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif char == "/" and not quoted and index + 1 < len(line) and line[index + 1] == "/":
                cut = index
                break
        output.append(line[:cut])
    return "\n".join(output)


@dataclass(frozen=True)
class TerrainTile:
    uuid: str
    cells_x: int
    cells_y: int
    preview_path: Path
    name: str = ""
    category: str = "terrain"


def load_terrain_tiles(game_root: Path) -> dict[str, TerrainTile]:
    result: dict[str, TerrainTile] = {}
    root = game_root / "Survival" / "Terrain" / "Tiles"
    if not root.is_dir():
        return result
    for path in root.rglob("*.tileson"):
        try:
            data = json.loads(strip_json_comments(path.read_text(encoding="utf-8-sig")))
            info = data["info"]
            item_uuid = str(uuid.UUID(str(info["uuid"])))
            preview = path.with_name(f"{item_uuid}.png")
            if not preview.is_file():
                continue
            result[item_uuid] = TerrainTile(
                uuid=item_uuid,
                cells_x=int(info.get("cellsX", 1)),
                cells_y=int(info.get("cellsY", 1)),
                preview_path=preview,
                name=path.stem,
                category=path.parent.name,
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return result


def load_terrain_tile_names(game_root: Path) -> dict[str, dict[str, Any]]:
    """Load bundled searchable tile names, falling back to installed .tileson files."""
    bundled = Path(__file__).resolve().parent / "assets" / "terrain-tile-names.json"
    if bundled.is_file():
        try:
            value = json.loads(bundled.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {
        item_uuid: {
            "name": tile.name,
            "category": tile.category,
            "cellsX": tile.cells_x,
            "cellsY": tile.cells_y,
        }
        for item_uuid, tile in load_terrain_tiles(game_root).items()
    }


@dataclass
class RgbImage:
    width: int
    height: int
    pixels: bytearray

    def get(self, x: int, y: int) -> tuple[int, int, int]:
        x = min(max(x, 0), self.width - 1)
        y = min(max(y, 0), self.height - 1)
        position = (y * self.width + x) * 3
        return tuple(self.pixels[position : position + 3])  # type: ignore[return-value]

    def put(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            position = (y * self.width + x) * 3
            self.pixels[position : position + 3] = bytes(color)


def paeth(left: int, up: int, upper_left: int) -> int:
    prediction = left + up - upper_left
    left_distance = abs(prediction - left)
    up_distance = abs(prediction - up)
    upper_left_distance = abs(prediction - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    return up if up_distance <= upper_left_distance else upper_left


def read_rgb_png(path: Path) -> RgbImage:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise TerrainDecodeError(f"Not a PNG: {path}")
    position = 8
    width = height = 0
    compressed = bytearray()
    while position + 12 <= len(data):
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if (depth, color_type, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                raise TerrainDecodeError(f"Unsupported terrain preview PNG format: {path}")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    raw = zlib.decompress(bytes(compressed))
    stride = width * 3
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise TerrainDecodeError(f"Unexpected PNG data length: {path}")
    pixels = bytearray(width * height * 3)
    previous = bytearray(stride)
    for y in range(height):
        source = y * (stride + 1)
        filter_type = raw[source]
        scanline = bytearray(raw[source + 1 : source + 1 + stride])
        for index in range(stride):
            left = scanline[index - 3] if index >= 3 else 0
            up = previous[index]
            upper_left = previous[index - 3] if index >= 3 else 0
            if filter_type == 1:
                scanline[index] = (scanline[index] + left) & 255
            elif filter_type == 2:
                scanline[index] = (scanline[index] + up) & 255
            elif filter_type == 3:
                scanline[index] = (scanline[index] + ((left + up) // 2)) & 255
            elif filter_type == 4:
                scanline[index] = (scanline[index] + paeth(left, up, upper_left)) & 255
            elif filter_type != 0:
                raise TerrainDecodeError(f"Unsupported PNG filter {filter_type}: {path}")
        pixels[y * stride : (y + 1) * stride] = scanline
        previous = scanline
    return RgbImage(width, height, pixels)


def rectify_preview(preview: RgbImage, cells_x: int, cells_y: int, cell_pixels: int) -> RgbImage:
    width = cells_x * cell_pixels
    height = cells_y * cell_pixels
    output = RgbImage(width, height, bytearray(width * height * 3))
    # All official 1.0 tile previews use the same isometric camera and canvas.
    top = (preview.width * 0.5, preview.height * 0.247)
    right = (preview.width * 0.977, preview.height * 0.613)
    left = (preview.width * 0.018, preview.height * 0.613)
    for y in range(height):
        v = (y + 0.5) / height
        for x in range(width):
            u = (x + 0.5) / width
            source_x = int(top[0] + u * (right[0] - top[0]) + v * (left[0] - top[0]))
            source_y = int(top[1] + u * (right[1] - top[1]) + v * (left[1] - top[1]))
            output.put(x, y, preview.get(source_x, source_y))
    return output


def crop_cell(image: RgbImage, x_offset: int, y_offset: int, cell_pixels: int) -> RgbImage:
    output = RgbImage(cell_pixels, cell_pixels, bytearray(cell_pixels * cell_pixels * 3))
    for y in range(cell_pixels):
        for x in range(cell_pixels):
            output.put(x, y, image.get(x_offset * cell_pixels + x, y_offset * cell_pixels + y))
    return output


def rotate_cell(image: RgbImage, rotation: int) -> RgbImage:
    rotation %= 4
    if rotation == 0:
        return image
    size = image.width
    output = RgbImage(size, size, bytearray(size * size * 3))
    for y in range(size):
        for x in range(size):
            if rotation == 1:
                source_x, source_y = y, size - 1 - x
            elif rotation == 2:
                source_x, source_y = size - 1 - x, size - 1 - y
            else:
                source_x, source_y = size - 1 - y, x
            output.put(x, y, image.get(source_x, source_y))
    return output


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_rgb_png(path: Path, image: RgbImage) -> None:
    rows = b"".join(
        b"\x00" + bytes(image.pixels[y * image.width * 3 : (y + 1) * image.width * 3])
        for y in range(image.height)
    )
    encoded = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", image.width, image.height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(rows, 9))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(encoded)


def blend_cell_seams(image: RgbImage, cell_pixels: int, radius: int = 2) -> RgbImage:
    """Hide the perimeter baked into independently captured atlas cells.

    The source atlas is spatially exact, but each 16-pixel cell was rendered in
    isolation.  Interpolating a very narrow band across those joins removes the
    regular checkerboard without changing the terrain away from cell edges.
    """
    if cell_pixels < 2 or radius < 1 or radius * 2 + 2 >= cell_pixels:
        raise ValueError("Invalid terrain seam dimensions")
    output = RgbImage(image.width, image.height, bytearray(image.pixels))
    pixels = output.pixels
    width = output.width
    denominator = radius * 2 + 1

    for boundary in range(cell_pixels, width, cell_pixels):
        sample_before = boundary - radius - 1
        sample_after = boundary + radius
        for y in range(output.height):
            row = y * width * 3
            before = row + sample_before * 3
            after = row + sample_after * 3
            for x in range(boundary - radius, boundary + radius):
                weight_after = x - sample_before
                weight_before = denominator - weight_after
                target = row + x * 3
                for channel in range(3):
                    pixels[target + channel] = (
                        pixels[before + channel] * weight_before
                        + pixels[after + channel] * weight_after
                        + denominator // 2
                    ) // denominator

    height = output.height
    for boundary in range(cell_pixels, height, cell_pixels):
        sample_before = boundary - radius - 1
        sample_after = boundary + radius
        before_row = sample_before * width * 3
        after_row = sample_after * width * 3
        for y in range(boundary - radius, boundary + radius):
            weight_after = y - sample_before
            weight_before = denominator - weight_after
            target_row = y * width * 3
            for x in range(width):
                before = before_row + x * 3
                after = after_row + x * 3
                target = target_row + x * 3
                for channel in range(3):
                    pixels[target + channel] = (
                        pixels[before + channel] * weight_before
                        + pixels[after + channel] * weight_after
                        + denominator // 2
                    ) // denominator
    return output


def prepare_terrain_source(image: RgbImage) -> Image.Image:
    """Deblock the small exact atlas before any enlargement."""
    source = Image.frombytes("RGB", (image.width, image.height), bytes(image.pixels))
    # Deblock open water before scaling. Keeping these temporary images at the
    # 2048x1536 source size avoids hundreds of megabytes of peak working set.
    red, green, blue = source.split()
    blue_over_green = ImageChops.subtract(blue, green).point(lambda value: 255 if value >= 8 else 0)
    green_over_red = ImageChops.subtract(green, red).point(lambda value: 255 if value >= 22 else 0)
    water_mask = ImageChops.multiply(blue_over_green, green_over_red)
    water_mask = water_mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.5))
    softened_water = source.filter(ImageFilter.GaussianBlur(0.75))
    return Image.composite(softened_water, source, water_mask)


def resize_terrain_source(source: Image.Image, width: int, height: int) -> Image.Image:
    """Enlarge a prepared terrain image and recover coast, road, and building edges."""
    resized = source.resize((width, height), Image.Resampling.LANCZOS)
    return resized.filter(ImageFilter.UnsharpMask(radius=1.1, percent=120, threshold=2))


def prepare_detailed_terrain(image: RgbImage, width: int, height: int) -> Image.Image:
    """Build a sharp large render while doing water deblocking at source size."""
    return resize_terrain_source(prepare_terrain_source(image), width, height)


def upscale_terrain(image: RgbImage, width: int, height: int) -> RgbImage:
    """Quality-resample the exact atlas output and recover small road details."""
    resized = prepare_detailed_terrain(image, width, height)
    return RgbImage(width, height, bytearray(resized.tobytes()))


BIOME_COLORS = {
    1: (76, 132, 68),    # meadow
    2: (35, 91, 48),     # forest
    3: (185, 148, 76),   # desert
    4: (128, 146, 65),   # field
    5: (73, 67, 61),     # burnt forest
    6: (139, 86, 45),    # autumn forest
    8: (45, 105, 137),   # lake
}


def fnv1a_uuid(value: str) -> str:
    """Return the atlas key used for a lowercase terrain UUID."""
    result = 0x811C9DC5
    for character in value.lower():
        result ^= ord(character)
        result = (result * 0x01000193) & 0xFFFFFFFF
    return f"{result:08x}"


class TerrainCellAtlas:
    """Purpose-built top-down terrain cells, including all four rotations."""

    def __init__(self, asset_directory: Path) -> None:
        manifest_path = asset_directory / "terrain-cell-atlas.json"
        image_path = asset_directory / "terrain-cell-atlas.png"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schemaVersion") != 1
            or manifest.get("rotations") != 4
            or manifest.get("cellPixels") != 16
        ):
            raise TerrainDecodeError("The bundled terrain-cell atlas format is unsupported")
        self.game_version = str(manifest.get("gameVersion", "unknown"))
        self.cell_pixels = int(manifest["cellPixels"])
        self.columns = int(manifest["columns"])
        self.rows = int(manifest["rows"])
        self.tiles: dict[str, dict[str, int]] = manifest["tiles"]
        self.image_path = image_path
        self._image: RgbImage | None = None

    @classmethod
    def bundled(cls) -> TerrainCellAtlas | None:
        directory = Path(__file__).resolve().parent / "assets"
        if not (directory / "terrain-cell-atlas.json").is_file():
            return None
        if not (directory / "terrain-cell-atlas.png").is_file():
            return None
        return cls(directory)

    def _load_image(self) -> RgbImage:
        if self._image is None:
            self._image = read_rgb_png(self.image_path)
            expected = (self.columns * self.cell_pixels, self.rows * self.cell_pixels)
            if (self._image.width, self._image.height) != expected:
                raise TerrainDecodeError(
                    f"Terrain atlas image is {self._image.width}x{self._image.height}; expected {expected[0]}x{expected[1]}"
                )
        return self._image

    def render(self, grid: TerrainGrid) -> RgbImage:
        atlas = self._load_image()
        cell_pixels = self.cell_pixels
        output_width = grid.width * cell_pixels
        output_height = grid.height * cell_pixels
        output = RgbImage(output_width, output_height, bytearray((9, 22, 28) * (output_width * output_height)))
        x_min, _x_max, _y_min, y_max = grid.visible_bounds
        missing: set[str] = set()
        rendered = 0
        for cell in grid.cells.values():
            entry = self.tiles.get(fnv1a_uuid(cell.uuid))
            if entry is None:
                missing.add(cell.uuid)
                continue
            cells_x = int(entry["cellsX"])
            cells_y = int(entry["cellsY"])
            if not (
                0 <= cell.rotation < 4
                and 0 <= cell.x_offset < cells_x
                and 0 <= cell.y_offset < cells_y
            ):
                raise TerrainDecodeError(f"Invalid atlas cell values at {cell.x},{cell.y}")
            offset_index = cell.y_offset * cells_x + cell.x_offset
            atlas_index = int(entry["start"]) + offset_index * 4 + cell.rotation
            atlas_x = (atlas_index % self.columns) * cell_pixels
            atlas_y = (atlas_index // self.columns) * cell_pixels
            target_x = (cell.x - x_min) * cell_pixels
            target_y = (y_max - cell.y) * cell_pixels
            for py in range(cell_pixels):
                source_start = ((atlas_y + py) * atlas.width + atlas_x) * 3
                target_start = ((target_y + py) * output_width + target_x) * 3
                output.pixels[target_start : target_start + cell_pixels * 3] = atlas.pixels[
                    source_start : source_start + cell_pixels * 3
                ]
            rendered += 1
        if missing:
            raise TerrainDecodeError(
                f"The save uses {len(missing)} terrain UUIDs missing from atlas version {self.game_version}"
            )
        if rendered != grid.width * grid.height:
            raise TerrainDecodeError("The save does not contain a complete Survival overworld")
        return output

    def render_pillow(self, grid: TerrainGrid) -> Image.Image:
        """Compose the exact atlas through Pillow's native paste path.

        ``render`` intentionally returns the small dependency-free ``RgbImage``
        used by the original command-line editor.  Copying its pixels in Python
        is needlessly expensive for the desktop companion, where Pillow is
        already required: a complete 128 x 96 world took about ten seconds just
        to decode and copy the atlas.  Pillow performs the same lossless cell
        copies in native code and lets the companion produce an immediately
        usable seed-correct map without starting the enormous WebGL world
        reconstruction.
        """
        with Image.open(self.image_path) as source:
            atlas = source.convert("RGB")
        expected = (self.columns * self.cell_pixels, self.rows * self.cell_pixels)
        if atlas.size != expected:
            raise TerrainDecodeError(
                f"Terrain atlas image is {atlas.width}x{atlas.height}; "
                f"expected {expected[0]}x{expected[1]}"
            )

        cell_pixels = self.cell_pixels
        output = Image.new(
            "RGB", (grid.width * cell_pixels, grid.height * cell_pixels), (9, 22, 28)
        )
        x_min, _x_max, _y_min, y_max = grid.visible_bounds
        missing: set[str] = set()
        rendered = 0
        for cell in grid.cells.values():
            entry = self.tiles.get(fnv1a_uuid(cell.uuid))
            if entry is None:
                missing.add(cell.uuid)
                continue
            cells_x = int(entry["cellsX"])
            cells_y = int(entry["cellsY"])
            if not (
                0 <= cell.rotation < 4
                and 0 <= cell.x_offset < cells_x
                and 0 <= cell.y_offset < cells_y
            ):
                raise TerrainDecodeError(
                    f"Invalid atlas cell values at {cell.x},{cell.y}"
                )
            offset_index = cell.y_offset * cells_x + cell.x_offset
            atlas_index = int(entry["start"]) + offset_index * 4 + cell.rotation
            atlas_x = (atlas_index % self.columns) * cell_pixels
            atlas_y = (atlas_index // self.columns) * cell_pixels
            target_x = (cell.x - x_min) * cell_pixels
            target_y = (y_max - cell.y) * cell_pixels
            # Pillow's public ``paste`` API accepts either a source image or a
            # colour plus an optional mask; it does not accept a source box as
            # a fourth positional argument.  Crop is still native code and is
            # dramatically faster than the original Python pixel-row copies.
            tile = atlas.crop(
                (atlas_x, atlas_y, atlas_x + cell_pixels, atlas_y + cell_pixels)
            )
            output.paste(tile, (target_x, target_y))
            rendered += 1
        if missing:
            raise TerrainDecodeError(
                f"The save uses {len(missing)} terrain UUIDs missing from atlas "
                f"version {self.game_version}"
            )
        if rendered != grid.width * grid.height:
            raise TerrainDecodeError(
                "The save does not contain a complete Survival overworld"
            )
        return output


class TerrainRenderer:
    def __init__(self, game_root: Path) -> None:
        self.game_root = game_root
        self.atlas = TerrainCellAtlas.bundled()
        self.tiles: dict[str, TerrainTile] = {}
        self._rectified: dict[tuple[str, int], RgbImage] = {}
        self.missing_tiles: set[str] = set()

    def _load_preview_tiles(self) -> None:
        if not self.tiles:
            self.tiles = load_terrain_tiles(self.game_root)

    def _cell_image(self, cell: TerrainCell, cell_pixels: int) -> RgbImage | None:
        self._load_preview_tiles()
        tile = self.tiles.get(cell.uuid)
        if tile is None:
            self.missing_tiles.add(cell.uuid)
            return None
        cache_key = (tile.uuid, cell_pixels)
        rectified = self._rectified.get(cache_key)
        if rectified is None:
            rectified = rectify_preview(read_rgb_png(tile.preview_path), tile.cells_x, tile.cells_y, cell_pixels)
            self._rectified[cache_key] = rectified
        if not (0 <= cell.x_offset < tile.cells_x and 0 <= cell.y_offset < tile.cells_y):
            raise TerrainDecodeError(
                f"Invalid tile offset {cell.x_offset},{cell.y_offset} for {cell.uuid} "
                f"({tile.cells_x}x{tile.cells_y})"
            )
        if tile.cells_x != tile.cells_y:
            raise TerrainDecodeError(f"Non-square installed terrain preview: {tile.preview_path}")
        # Installed previews and save cells use opposite vertical axes.  Their
        # correct relationship is a transverse offset plus counter-ordered
        # rotations.  This was verified against every matching top-down atlas
        # cell; using the save rotation directly disconnects roads and jumbles
        # multi-cell locations.
        source_x = tile.cells_y - 1 - cell.y_offset
        source_y = tile.cells_x - 1 - cell.x_offset
        preview_rotation = (1 - cell.rotation) % 4
        return rotate_cell(
            crop_cell(rectified, source_x, source_y, cell_pixels),
            preview_rotation,
        )

    def render(self, grid: TerrainGrid, cell_pixels: int = 10, draw_grid: bool = False) -> RgbImage:
        if self.atlas is not None and cell_pixels == self.atlas.cell_pixels and not draw_grid:
            return self.atlas.render(grid)
        width = grid.width * cell_pixels
        height = grid.height * cell_pixels
        output = RgbImage(width, height, bytearray((9, 22, 28) * (width * height)))
        x_min, _x_max, _y_min, y_max = grid.visible_bounds
        for cell in grid.cells.values():
            target_x = (cell.x - x_min) * cell_pixels
            target_y = (y_max - cell.y) * cell_pixels
            fragment = self._cell_image(cell, cell_pixels)
            if fragment is None:
                color = BIOME_COLORS.get(cell.biome, (70, 75, 70))
                for py in range(cell_pixels):
                    for px in range(cell_pixels):
                        output.put(target_x + px, target_y + py, color)
                continue
            for py in range(cell_pixels):
                source_start = py * cell_pixels * 3
                target_start = ((target_y + py) * width + target_x) * 3
                output.pixels[target_start : target_start + cell_pixels * 3] = fragment.pixels[
                    source_start : source_start + cell_pixels * 3
                ]
            if draw_grid:
                grid_color = (25, 34, 37)
                for pixel in range(cell_pixels):
                    output.put(target_x + pixel, target_y, grid_color)
                    output.put(target_x, target_y + pixel, grid_color)
        return output

    def render_png(self, path: Path, grid: TerrainGrid, cell_pixels: int = 12, draw_grid: bool = False) -> Path:
        image = self.render(grid, cell_pixels=cell_pixels, draw_grid=draw_grid)
        write_rgb_png(path, image)
        return path

    def _render_installed_preview_detail(self, grid: TerrainGrid, cell_pixels: int) -> Image.Image:
        """Reproject the game's own isometric tile previews into save space."""
        self._load_preview_tiles()
        width = grid.width * cell_pixels
        height = grid.height * cell_pixels
        output = Image.new("RGB", (width, height), (9, 22, 28))
        x_min, _x_max, _y_min, y_max = grid.visible_bounds
        grouped: dict[str, list[TerrainCell]] = defaultdict(list)
        for cell in grid.cells.values():
            grouped[cell.uuid].append(cell)

        rotations = (
            Image.Transpose.ROTATE_270,
            None,
            Image.Transpose.ROTATE_90,
            Image.Transpose.ROTATE_180,
        )
        for item_uuid, cells in grouped.items():
            tile = self.tiles.get(item_uuid)
            if tile is None or tile.cells_x != tile.cells_y:
                self.missing_tiles.add(item_uuid)
                continue
            with Image.open(tile.preview_path) as preview_file:
                preview = preview_file.convert("RGB")
            preview_width, preview_height = preview.size
            rectified_width = tile.cells_x * cell_pixels
            rectified_height = tile.cells_y * cell_pixels
            top = (preview_width * 0.5, preview_height * 0.247)
            right = (preview_width * 0.977, preview_height * 0.613)
            left = (preview_width * 0.018, preview_height * 0.613)
            affine = (
                (right[0] - top[0]) / rectified_width,
                (left[0] - top[0]) / rectified_height,
                top[0],
                (right[1] - top[1]) / rectified_width,
                (left[1] - top[1]) / rectified_height,
                top[1],
            )
            rectified = preview.transform(
                (rectified_width, rectified_height),
                Image.Transform.AFFINE,
                affine,
                resample=Image.Resampling.BICUBIC,
            )
            for cell in cells:
                if not (
                    0 <= cell.x_offset < tile.cells_x
                    and 0 <= cell.y_offset < tile.cells_y
                ):
                    continue
                source_x = tile.cells_y - 1 - cell.y_offset
                source_y = tile.cells_x - 1 - cell.x_offset
                fragment = rectified.crop((
                    source_x * cell_pixels,
                    source_y * cell_pixels,
                    (source_x + 1) * cell_pixels,
                    (source_y + 1) * cell_pixels,
                ))
                transpose = rotations[cell.rotation]
                if transpose is not None:
                    fragment = fragment.transpose(transpose)
                target_x = (cell.x - x_min) * cell_pixels
                target_y = (y_max - cell.y) * cell_pixels
                output.paste(fragment, (target_x, target_y))
        return output

    @staticmethod
    def _suppress_preview_edges(detail: Image.Image, cell_pixels: int) -> Image.Image:
        """Fade preview-only detail at cell joins without softening the map."""
        mask = Image.new("L", detail.size, 255)
        draw = ImageDraw.Draw(mask)
        half_width = max(1, round(cell_pixels / 16))
        for boundary in range(cell_pixels, detail.width, cell_pixels):
            draw.rectangle(
                (boundary - half_width, 0, boundary + half_width - 1, detail.height - 1),
                fill=0,
            )
        for boundary in range(cell_pixels, detail.height, cell_pixels):
            draw.rectangle(
                (0, boundary - half_width, detail.width - 1, boundary + half_width - 1),
                fill=0,
            )
        mask = mask.filter(ImageFilter.GaussianBlur(max(1.0, cell_pixels / 16)))
        neutral = Image.new("RGB", detail.size, (128, 128, 128))
        return Image.composite(detail, neutral, mask)

    def _render_detailed_image(self, grid: TerrainGrid, cell_pixels: int) -> Image.Image:
        """Combine exact save geometry with aligned detail from game previews."""
        if self.atlas is None:
            rendered = self.render(grid, cell_pixels=cell_pixels)
            return Image.frombytes("RGB", (rendered.width, rendered.height), bytes(rendered.pixels))
        if cell_pixels < self.atlas.cell_pixels:
            raise ValueError("Detailed terrain cannot be smaller than its source atlas")
        source = self.atlas.render(grid)
        seamless = blend_cell_seams(source, self.atlas.cell_pixels, radius=2)
        base = Image.frombytes("RGB", (seamless.width, seamless.height), bytes(seamless.pixels))
        target_size = (grid.width * cell_pixels, grid.height * cell_pixels)
        if base.size != target_size:
            base = base.resize(target_size, Image.Resampling.LANCZOS)

        # The 16-pixel atlas is spatially exact but cannot supply close-zoom
        # texture. Installed 220x150 previews contain real trees, rocks,
        # buildings, and road detail. Transfer their high-frequency content
        # while retaining the atlas for coastlines, color, and tile geometry.
        preview = self._render_installed_preview_detail(grid, cell_pixels)
        low_frequency = preview.filter(ImageFilter.GaussianBlur(max(1.25, cell_pixels / 14)))
        detail = ImageChops.subtract(preview, low_frequency, scale=1.15, offset=128)
        detail = self._suppress_preview_edges(detail, cell_pixels)
        # Isometric water previews contain a baked square reflection pattern.
        # It is not real world detail and becomes conspicuous at overview zoom,
        # so leave lakes and ocean entirely to the exact top-down atlas.
        red, green, blue = base.split()
        blue_over_green = ImageChops.subtract(blue, green).point(
            lambda value: 255 if value >= 8 else 0
        )
        green_over_red = ImageChops.subtract(green, red).point(
            lambda value: 255 if value >= 22 else 0
        )
        water_mask = ImageChops.multiply(blue_over_green, green_over_red)
        water_mask = water_mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1.0))
        detail = Image.composite(Image.new("RGB", detail.size, (128, 128, 128)), detail, water_mask)
        combined = ImageChops.add(base, detail, scale=1.0, offset=-128)
        return combined.filter(ImageFilter.UnsharpMask(radius=0.55, percent=30, threshold=3))

    def render_detailed(self, grid: TerrainGrid, cell_pixels: int = 32) -> RgbImage:
        output = self._render_detailed_image(grid, cell_pixels)
        return RgbImage(output.width, output.height, bytearray(output.tobytes()))

    def render_detailed_png(self, path: Path, grid: TerrainGrid, cell_pixels: int = 32) -> Path:
        output = self._render_detailed_image(grid, cell_pixels)
        output.save(path, format="PNG", compress_level=4)
        return path
