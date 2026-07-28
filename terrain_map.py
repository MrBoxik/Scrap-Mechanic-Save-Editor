"""Decode and render Scrap Mechanic 1.0 Survival terrain using only stdlib.

The game stores g_cellData as an LZ4-compressed Lua-object bitstream inside a
large ScriptData row. Each cell identifies a terrain tile UUID, rotation, and
(for multi-cell POIs) tile offset. The primary renderer uses a clean top-down
cell atlas; installed .tileson/isometric preview files remain as a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import struct
from typing import Any
import uuid
import zlib


LUA_HEADER = b"LUA\x00\x00\x00\x01"
TERRAIN_KEYS = ("seed", "flags", "bounds", "uid", "xOffset", "yOffset", "rotation", "groupId")
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
        value = 0
        for _ in range(bits):
            if self.bit_offset >= len(self.data) * 8:
                raise TerrainDecodeError("Unexpected end of Lua bitstream")
            byte_index = self.bit_offset // 8
            shift = 7 - self.bit_offset % 8
            value = (value << 1) | ((self.data[byte_index] >> shift) & 1)
            self.bit_offset += 1
        return value

    def read_signed(self, bits: int) -> int:
        value = self.read_unsigned(bits)
        return value - (1 << bits) if value >= (1 << (bits - 1)) else value

    def read_bytes(self, length: int) -> bytes:
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
            return TerrainGrid(int(root["seed"]), (x_min, x_max, y_min, y_max), visible, padding, cells)
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
        rotated_x = tile.cells_y if cell.rotation in (1, 3) else tile.cells_x
        rotated_y = tile.cells_x if cell.rotation in (1, 3) else tile.cells_y
        if not (0 <= cell.x_offset < rotated_x and 0 <= cell.y_offset < rotated_y):
            raise TerrainDecodeError(
                f"Invalid tile offset {cell.x_offset},{cell.y_offset} for {cell.uuid} "
                f"({tile.cells_x}x{tile.cells_y})"
            )
        # The save stores offsets in the already-rotated tile coordinate space.
        # Convert them back to the preview's unrotated coordinates before
        # cropping, then rotate that individual fragment into world alignment.
        if cell.rotation == 1:
            source_x, source_y = tile.cells_x - 1 - cell.y_offset, cell.x_offset
        elif cell.rotation == 2:
            source_x = tile.cells_x - 1 - cell.x_offset
            source_y = tile.cells_y - 1 - cell.y_offset
        elif cell.rotation == 3:
            source_x, source_y = cell.y_offset, tile.cells_y - 1 - cell.x_offset
        else:
            source_x, source_y = cell.x_offset, cell.y_offset
        return rotate_cell(crop_cell(rectified, source_x, source_y, cell_pixels), cell.rotation)

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
