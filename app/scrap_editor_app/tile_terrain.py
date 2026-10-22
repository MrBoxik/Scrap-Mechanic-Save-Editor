from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path
import struct
from typing import Any
from uuid import UUID

import lz4.block
import numpy as np


@dataclass
class TileTerrainCache:
    """Decode Scrap Mechanic's compiled terrain mip into a WebGL mesh."""

    _memory: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    _asset_memory: dict[tuple[str, int], tuple[dict[str, Any], ...]] = field(
        default_factory=dict
    )
    _harvestable_memory: dict[tuple[str, int], tuple[dict[str, Any], ...]] = field(
        default_factory=dict
    )
    _blueprint_memory: dict[tuple[str, int], tuple[dict[str, Any], ...]] = field(
        default_factory=dict
    )
    _prefab_memory: dict[tuple[str, int], tuple[dict[str, Any], ...]] = field(
        default_factory=dict
    )
    _decal_memory: dict[tuple[str, int], tuple[dict[str, Any], ...]] = field(
        default_factory=dict
    )

    @staticmethod
    def _encoded(array: np.ndarray) -> str:
        return b64encode(array.tobytes(order="C")).decode("ascii")

    def geometry(self, path: Path) -> dict[str, Any]:
        source = path.resolve()
        stamp = source.stat().st_mtime_ns
        key = (str(source).casefold(), stamp)
        if key in self._memory:
            return self._memory[key]

        data = source.read_bytes()
        if data[:4] != b"TILE" or len(data) < 60:
            raise RuntimeError("The installed tile has an unsupported header")
        width, height, headers_offset, header_size = struct.unpack_from("<4I", data, 32)
        if not 0 < width <= 32 or not 0 < height <= 32 or header_size < 0x48:
            raise RuntimeError("The installed tile has invalid terrain dimensions")

        grid_width, grid_height = width * 32 + 1, height * 32 + 1
        position_chunks: list[np.ndarray] = []
        normal_chunks: list[np.ndarray] = []
        uv_chunks: list[np.ndarray] = []
        weight_chunks: list[np.ndarray] = []
        color_chunks: list[np.ndarray] = []
        cell_center_chunks: list[np.ndarray] = []
        material_indices: list[list[int]] = [[] for _ in range(9)]
        all_indices: list[int] = []
        triangle_cells: list[tuple[int, int, int, int]] = []
        minimum_height = float("inf")
        maximum_height = float("-inf")
        vertex_offset = 0
        for cell_y in range(height):
            for cell_x in range(width):
                header = headers_offset + (cell_x + cell_y * width) * header_size
                mip_index = struct.unpack_from("<I", data, header)[0]
                compressed_size = struct.unpack_from("<I", data, header + 0x18)[0]
                uncompressed_size = struct.unpack_from("<I", data, header + 0x30)[0]
                if not compressed_size or uncompressed_size < 42_512:
                    continue
                packed = data[mip_index : mip_index + compressed_size]
                raw = lz4.block.decompress(packed, uncompressed_size=uncompressed_size)
                vertices = np.frombuffer(
                    raw,
                    dtype=np.dtype([("height", "<f4"), ("color", "<u4")]),
                    count=33 * 33,
                )
                cell_heights = vertices["height"].reshape(33, 33).copy()
                vertex_colors = (
                    np.ascontiguousarray(vertices["color"])
                    .view(np.uint8)
                    .reshape(33, 33, 4)
                )
                ground_offset = 33 * 33 * 8
                ground = np.frombuffer(
                    raw, dtype=np.uint8, count=65 * 65 * 8, offset=ground_offset
                ).reshape(65, 65, 8)
                sampled = np.zeros((33, 33, 9), dtype=np.uint8)
                sampled[:, :, :8] = ground[::2, ::2]
                total = sampled[:, :, :8].astype(np.int16).sum(axis=2)
                sampled[:, :, 8] = np.clip(255 - total, 0, 255).astype(np.uint8)
                weight_total = sampled.astype(np.float32).sum(axis=2, keepdims=True)
                normalized_weights = sampled.astype(np.float32) / np.maximum(
                    weight_total, 1.0
                )

                yy, xx = np.mgrid[0:33, 0:33]
                positions = np.ascontiguousarray(
                    np.stack(
                        (
                            cell_x * 64.0 + xx * 2.0,
                            cell_y * 64.0 + yy * 2.0,
                            cell_heights,
                        ),
                        axis=2,
                    ).reshape(-1, 3),
                    dtype="<f4",
                )
                dzdy, dzdx = np.gradient(cell_heights, 2.0, 2.0)
                normals = np.stack((-dzdx, -dzdy, np.ones_like(cell_heights)), axis=2)
                normals /= np.maximum(
                    np.linalg.norm(normals, axis=2, keepdims=True), 1e-6
                )
                normal_chunks.append(
                    np.ascontiguousarray(normals.reshape(-1, 3), dtype="<f4")
                )
                position_chunks.append(positions)
                uv_chunks.append(
                    np.ascontiguousarray(
                        np.stack(
                            (
                                (cell_x * 32.0 + xx) / 8.0,
                                (cell_y * 32.0 + yy) / 8.0,
                            ),
                            axis=2,
                        ).reshape(-1, 2),
                        dtype="<f4",
                    )
                )
                weight_chunks.append(
                    np.ascontiguousarray(normalized_weights.reshape(-1, 9), dtype="<f4")
                )
                color_chunks.append(
                    np.ascontiguousarray(
                        vertex_colors.reshape(-1, 4).astype(np.float32) / 255.0,
                        dtype="<f4",
                    )
                )
                # The game calls getHeightAt with the owning cell and keeps a
                # separate closed 33x33 grid for every cell.  Keeping the
                # center as an attribute lets WebGL select that same cell's
                # cliff base even for a vertex exactly on a shared border.
                cell_center_chunks.append(
                    np.tile(
                        np.asarray(
                            [cell_x * 64.0 + 32.0, cell_y * 64.0 + 32.0],
                            dtype="<f4",
                        ),
                        (33 * 33, 1),
                    )
                )

                for y in range(32):
                    for x in range(32):
                        material = int(
                            np.argmax(
                                sampled[y : y + 2, x : x + 2]
                                .astype(np.uint16)
                                .sum(axis=(0, 1))
                            )
                        )
                        a = vertex_offset + x + y * 33
                        b = a + 1
                        d = a + 33
                        c = d + 1
                        material_indices[material].extend((a, b, c, a, c, d))
                        all_indices.extend((a, b, c, a, c, d))
                        owner = (cell_x, cell_y, cell_x, cell_y)
                        triangle_cells.extend((owner, owner))
                vertex_offset += 33 * 33
                minimum_height = min(minimum_height, float(cell_heights.min()))
                maximum_height = max(maximum_height, float(cell_heights.max()))

        if not position_chunks:
            raise RuntimeError("The installed tile has no decodable terrain cells")
        positions = np.concatenate(position_chunks, axis=0)
        normals = np.concatenate(normal_chunks, axis=0)
        uvs = np.concatenate(uv_chunks, axis=0)
        normalized_weights = np.concatenate(weight_chunks, axis=0)
        vertex_colors = np.concatenate(color_chunks, axis=0)
        cell_centers = np.concatenate(cell_center_chunks, axis=0)

        result = {
            "positions": self._encoded(positions),
            "normals": self._encoded(normals),
            "uvs": self._encoded(uvs),
            "colors": self._encoded(vertex_colors),
            "cellCenters": self._encoded(cell_centers),
            "weights": self._encoded(
                np.ascontiguousarray(normalized_weights, dtype="<f4")
            ),
            "indices": self._encoded(np.asarray(all_indices, dtype="<u4")),
            "indicesCount": len(all_indices),
            "cellOwners": self._encoded(
                np.asarray(triangle_cells, dtype="<i2").reshape(-1, 4)
            ),
            "parts": [
                {
                    "material": material,
                    "indices": self._encoded(np.asarray(indices, dtype="<u4")),
                    "indicesCount": len(indices),
                }
                for material, indices in enumerate(material_indices)
                if indices
            ],
            "bounds": [
                [0.0, 0.0, minimum_height],
                [width * 64.0, height * 64.0, maximum_height],
            ],
            "vertices": int(grid_width * grid_height),
            "triangles": int((grid_width - 1) * (grid_height - 1) * 2),
            "renderVertices": int(len(positions)),
            "renderTriangles": int(len(all_indices) // 3),
            "seamTriangles": 0,
            # Water is authored as an explicit asset/prefab in Survival tiles.
            # Terrain below z=0 is also used for caves, cliffs, and voids, so
            # inferring an ocean from height data creates full-tile water on
            # dry minidungeons and landmarks.
            "waterLevel": None,
        }
        self._memory[key] = result
        return result

    def asset_transforms(self, path: Path) -> tuple[dict[str, Any], ...]:
        """Read the exact transforms stored in the compiled tile.

        ``.tileson`` exposes editor Euler angles.  Those angles are ambiguous
        around 90 degrees and are not safe to reinterpret in WebGL.  The game
        compiles the same placements as quaternions, so use those values as the
        authoritative rotations for rendering.
        """
        source = path.resolve()
        stamp = source.stat().st_mtime_ns
        key = (str(source).casefold(), stamp)
        if key in self._asset_memory:
            return self._asset_memory[key]

        data = source.read_bytes()
        if data[:4] != b"TILE" or len(data) < 60:
            return ()
        version = struct.unpack_from("<I", data, 4)[0]
        width, height, headers_offset, header_size = struct.unpack_from(
            "<4I", data, 32
        )
        if version < 10 or not 0 < width <= 32 or not 0 < height <= 32:
            return ()

        result: list[dict[str, Any]] = []
        for cell in range(width * height):
            cell_x, cell_y = cell % width, cell // width
            header = headers_offset + cell * header_size
            if header + 0x94 > len(data):
                continue
            counts = struct.unpack_from("<4I", data, header + 0x54)
            indexes = struct.unpack_from("<4I", data, header + 0x64)
            compressed_sizes = struct.unpack_from("<4I", data, header + 0x74)
            uncompressed_sizes = struct.unpack_from("<4I", data, header + 0x84)
            for count, index, compressed_size, uncompressed_size in zip(
                counts, indexes, compressed_sizes, uncompressed_sizes
            ):
                if not count or not compressed_size or not uncompressed_size:
                    continue
                packed = data[index : index + compressed_size]
                raw = lz4.block.decompress(
                    packed, uncompressed_size=uncompressed_size
                )
                offset = 0
                for _ in range(count):
                    if offset + 57 > len(raw):
                        break
                    local_position = struct.unpack_from("<3f", raw, offset)
                    position = (
                        local_position[0] + cell_x * 64.0,
                        local_position[1] + cell_y * 64.0,
                        local_position[2],
                    )
                    quaternion = struct.unpack_from("<4f", raw, offset + 12)
                    scale = struct.unpack_from("<3f", raw, offset + 28)
                    asset_uuid = str(UUID(bytes=raw[offset + 40 : offset + 56]))
                    offset += 56
                    material_count = raw[offset]
                    offset += 1
                    colors: dict[str, str] = {}
                    valid = True
                    for _ in range(material_count):
                        if offset >= len(raw):
                            valid = False
                            break
                        name_length = raw[offset]
                        offset += 1
                        if offset + name_length + 4 > len(raw):
                            valid = False
                            break
                        name = raw[offset : offset + name_length].decode(
                            "utf-8", errors="replace"
                        )
                        offset += name_length
                        red, green, blue, alpha = raw[offset : offset + 4]
                        offset += 4
                        colors[name] = f"#{red:02X}{green:02X}{blue:02X}{alpha:02X}"
                    # Tile v15 appends one asset flags byte.
                    if version >= 15 and offset < len(raw):
                        offset += 1
                    if not valid:
                        break
                    result.append({
                        "uuid": asset_uuid,
                        "position": list(position),
                        "quaternion": list(quaternion),
                        "scale": list(scale),
                        "colors": colors,
                    })

        value = tuple(result)
        self._asset_memory[key] = value
        return value

    def harvestable_transforms(self, path: Path) -> tuple[dict[str, Any], ...]:
        """Read exact compiled tree, rock, and plant transforms.

        Tile v15 appends five bytes to the older 60-byte harvestable record.
        Deriving the stride from the decompressed block keeps the parser
        compatible with both layouts while retaining the authoritative game
        quaternion that the editor Euler angles cannot represent reliably.
        """
        source = path.resolve()
        stamp = source.stat().st_mtime_ns
        key = (str(source).casefold(), stamp)
        if key in self._harvestable_memory:
            return self._harvestable_memory[key]
        data = source.read_bytes()
        if data[:4] != b"TILE" or len(data) < 60:
            return ()
        width, height, headers_offset, header_size = struct.unpack_from(
            "<4I", data, 32
        )
        if not 0 < width <= 32 or not 0 < height <= 32:
            return ()

        result: list[dict[str, Any]] = []
        for cell in range(width * height):
            cell_x, cell_y = cell % width, cell // width
            header = headers_offset + cell * header_size
            if header + 0x124 > len(data):
                continue
            counts = struct.unpack_from("<4I", data, header + 0xE4)
            indexes = struct.unpack_from("<4I", data, header + 0xF4)
            compressed_sizes = struct.unpack_from("<4I", data, header + 0x104)
            uncompressed_sizes = struct.unpack_from("<4I", data, header + 0x114)
            for count, index, compressed_size, uncompressed_size in zip(
                counts, indexes, compressed_sizes, uncompressed_sizes
            ):
                if not count or not compressed_size or uncompressed_size < count * 60:
                    continue
                raw = lz4.block.decompress(
                    data[index : index + compressed_size],
                    uncompressed_size=uncompressed_size,
                )
                stride = len(raw) // count if len(raw) % count == 0 else 60
                if stride < 60:
                    continue
                for row_index in range(count):
                    offset = row_index * stride
                    if offset + 60 > len(raw):
                        break
                    local_position = struct.unpack_from("<3f", raw, offset)
                    red, green, blue, alpha = raw[offset + 56 : offset + 60]
                    result.append({
                        "uuid": str(UUID(bytes=raw[offset + 40 : offset + 56])),
                        "position": [
                            local_position[0] + cell_x * 64.0,
                            local_position[1] + cell_y * 64.0,
                            local_position[2],
                        ],
                        "quaternion": list(struct.unpack_from("<4f", raw, offset + 12)),
                        "scale": list(struct.unpack_from("<3f", raw, offset + 28)),
                        "color": f"#{red:02X}{green:02X}{blue:02X}{alpha:02X}",
                    })
        value = tuple(result)
        self._harvestable_memory[key] = value
        return value

    def blueprint_transforms(self, path: Path) -> tuple[dict[str, Any], ...]:
        """Read compiled tile blueprint positions and quaternions."""
        source = path.resolve()
        stamp = source.stat().st_mtime_ns
        key = (str(source).casefold(), stamp)
        if key in self._blueprint_memory:
            return self._blueprint_memory[key]
        data = source.read_bytes()
        if data[:4] != b"TILE" or len(data) < 60:
            return ()
        width, height, headers_offset, header_size = struct.unpack_from(
            "<4I", data, 32
        )
        result: list[dict[str, Any]] = []
        for cell in range(width * height):
            cell_x, cell_y = cell % width, cell // width
            header = headers_offset + cell * header_size
            if header + 0xA4 > len(data):
                continue
            count, index, compressed_size, uncompressed_size = struct.unpack_from(
                "<4I", data, header + 0x94
            )
            if not count or not compressed_size or not uncompressed_size:
                continue
            raw = lz4.block.decompress(
                data[index : index + compressed_size],
                uncompressed_size=uncompressed_size,
            )
            # Version 15 carries nine bytes after each JSON resource. Searching
            # for the resource marker avoids depending on that trailing payload.
            markers: list[int] = []
            cursor = 0
            while len(markers) < count:
                marker = raw.find(b"?JB:", cursor)
                if marker < 32:
                    break
                markers.append(marker)
                cursor = marker + 4
            for marker in markers:
                offset = marker - 32
                local_position = struct.unpack_from("<3f", raw, offset)
                result.append({
                    "position": [
                        local_position[0] + cell_x * 64.0,
                        local_position[1] + cell_y * 64.0,
                        local_position[2],
                    ],
                    "quaternion": list(struct.unpack_from("<4f", raw, offset + 12)),
                })
        value = tuple(result)
        self._blueprint_memory[key] = value
        return value

    def prefab_transforms(self, path: Path) -> tuple[dict[str, Any], ...]:
        """Read exact compiled transforms for top-level tile prefabs."""
        source = path.resolve()
        stamp = source.stat().st_mtime_ns
        key = (str(source).casefold(), stamp)
        if key in self._prefab_memory:
            return self._prefab_memory[key]
        data = source.read_bytes()
        if data[:4] != b"TILE" or len(data) < 60:
            return ()
        version = struct.unpack_from("<I", data, 4)[0]
        width, height, headers_offset, header_size = struct.unpack_from(
            "<4I", data, 32
        )
        result: list[dict[str, Any]] = []
        for cell in range(width * height):
            cell_x, cell_y = cell % width, cell // width
            header = headers_offset + cell * header_size
            if header + 0xD4 > len(data):
                continue
            count, index, compressed_size, uncompressed_size = struct.unpack_from(
                "<4I", data, header + 0xC4
            )
            if not count or not compressed_size or not uncompressed_size:
                continue
            raw = lz4.block.decompress(
                data[index : index + compressed_size],
                uncompressed_size=uncompressed_size,
            )
            offset = 0
            for _ in range(count):
                fixed_size = 40 if version >= 9 else 28
                if offset + fixed_size + 4 > len(raw):
                    break
                local_position = struct.unpack_from("<3f", raw, offset)
                quaternion = struct.unpack_from("<4f", raw, offset + 12)
                scale = (
                    struct.unpack_from("<3f", raw, offset + 28)
                    if version >= 9 else (1.0, 1.0, 1.0)
                )
                offset += fixed_size
                path_length = struct.unpack_from("<I", raw, offset)[0]
                offset += 4
                if path_length > len(raw) - offset:
                    break
                prefab_path = raw[offset : offset + path_length].decode(
                    "utf-8", errors="replace"
                )
                offset += path_length
                if offset >= len(raw):
                    break
                has_flag = raw[offset]
                offset += 1
                if has_flag:
                    if offset >= len(raw):
                        break
                    flag_length = raw[offset]
                    offset += 1 + flag_length
                offset += 4
                result.append({
                    "path": prefab_path,
                    "position": [
                        local_position[0] + cell_x * 64.0,
                        local_position[1] + cell_y * 64.0,
                        local_position[2],
                    ],
                    "quaternion": list(quaternion),
                    "scale": list(scale),
                })
        value = tuple(result)
        self._prefab_memory[key] = value
        return value

    def decal_transforms(self, path: Path) -> tuple[dict[str, Any], ...]:
        """Read exact compiled transforms and tint colors for tile decals."""
        source = path.resolve()
        stamp = source.stat().st_mtime_ns
        key = (str(source).casefold(), stamp)
        if key in self._decal_memory:
            return self._decal_memory[key]
        data = source.read_bytes()
        if data[:4] != b"TILE" or len(data) < 60:
            return ()
        width, height, headers_offset, header_size = struct.unpack_from(
            "<4I", data, 32
        )
        if not 0 < width <= 32 or not 0 < height <= 32:
            return ()

        result: list[dict[str, Any]] = []
        for cell in range(width * height):
            cell_x, cell_y = cell % width, cell // width
            header = headers_offset + cell * header_size
            if header + 0xE4 > len(data):
                continue
            count, index, compressed_size, uncompressed_size = struct.unpack_from(
                "<4I", data, header + 0xD4
            )
            if not count or not compressed_size or uncompressed_size < count * 60:
                continue
            raw = lz4.block.decompress(
                data[index : index + compressed_size],
                uncompressed_size=uncompressed_size,
            )
            stride = len(raw) // count if len(raw) % count == 0 else 60
            if stride < 60:
                continue
            for row_index in range(count):
                offset = row_index * stride
                if offset + 60 > len(raw):
                    break
                local_position = struct.unpack_from("<3f", raw, offset)
                red, green, blue, alpha = raw[offset + 56 : offset + 60]
                result.append({
                    "uuid": str(UUID(bytes=raw[offset + 40 : offset + 56])),
                    "position": [
                        local_position[0] + cell_x * 64.0,
                        local_position[1] + cell_y * 64.0,
                        local_position[2],
                    ],
                    "quaternion": list(struct.unpack_from("<4f", raw, offset + 12)),
                    "scale": list(struct.unpack_from("<3f", raw, offset + 28)),
                    "color": f"#{red:02X}{green:02X}{blue:02X}{alpha:02X}",
                })
        value = tuple(result)
        self._decal_memory[key] = value
        return value
