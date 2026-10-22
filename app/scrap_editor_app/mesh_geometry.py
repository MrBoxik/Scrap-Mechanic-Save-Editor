from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from PIL import Image

from .terrain_scene import TerrainRenderable


MAX_MESH_TRIANGLES = 300_000
MAX_TEXTURE_SIDE = 2048
MESH_CACHE_REVISION = 10


@dataclass
class MeshGeometryCache:
    """Decode installed game meshes into compact WebGL-ready buffers.

    Scrap Mechanic ships Assimp beside the game executables.  We deliberately
    load that user-installed DLL at runtime rather than bundling or copying any
    Axolot binaries or assets into the editor release.
    """

    game_root: Path
    cache_root: Path

    def __post_init__(self) -> None:
        self.game_root = self.game_root.resolve()
        self.cache_root = self.cache_root.resolve()
        self._memory: dict[tuple[str, int], dict[str, Any]] = {}
        self._empty_texture_memory: set[tuple[str, int]] = set()
        self._texture_profile_memory: dict[
            tuple[str, int], dict[str, Any]
        ] = {}
        self._dll_handle: Any = None
        self._pyassimp: Any = None

    def clear_memory(self) -> None:
        self._memory.clear()
        self._empty_texture_memory.clear()
        self._texture_profile_memory.clear()

    def _assimp(self) -> Any:
        if self._pyassimp is not None:
            return self._pyassimp
        release = self.game_root / "Release"
        library = release / "assimp.dll"
        if not library.is_file():
            raise RuntimeError("The installed game does not provide Release/assimp.dll")
        os.environ["PATH"] = f"{release}{os.pathsep}{os.environ.get('PATH', '')}"
        if hasattr(os, "add_dll_directory"):
            self._dll_handle = os.add_dll_directory(str(release))
        try:
            import pyassimp
        except Exception as exc:
            raise RuntimeError(f"The 3D mesh importer could not start: {exc}") from exc
        self._pyassimp = pyassimp
        return pyassimp

    @staticmethod
    def _encoded(array: Any) -> str:
        return b64encode(array.tobytes(order="C")).decode("ascii")

    def geometry(
        self, renderable: TerrainRenderable, lod_index: int = 0
    ) -> dict[str, Any]:
        """Decode one installed LOD mesh.

        Scrap Mechanic's renderable files contain the same distance/screen-size
        LOD chain used by the game.  Keeping the selected source in the normal
        geometry cache means the desktop viewer can stream those authored
        meshes without inventing or simplifying any geometry itself.
        """
        lods = renderable.lod_meshes or (renderable.mesh,)
        selected_lod = max(0, min(int(lod_index), len(lods) - 1))
        source = lods[selected_lod].resolve()
        if source.suffix.casefold() not in {".fbx", ".dae", ".obj", ".mesh"}:
            raise RuntimeError(f"{source.suffix or 'This'} mesh format is not decoded yet")
        stamp = source.stat().st_mtime_ns
        key = (str(source).casefold(), stamp)
        if key in self._memory:
            return self._memory[key]
        signature = f"{source}:{stamp}:{MESH_CACHE_REVISION}"
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:24]
        geometry_dir = self.cache_root / "mesh-geometry"
        geometry_cache = geometry_dir / f"{digest}.json.gz"
        if geometry_cache.is_file():
            try:
                with gzip.open(geometry_cache, "rt", encoding="utf-8") as stream:
                    cached = json.load(stream)
                if isinstance(cached, dict) and cached.get("parts"):
                    self._memory[key] = cached
                    return cached
            except (OSError, ValueError, TypeError):
                # A partial cache entry is harmless; rebuild it from the
                # installed mesh and atomically replace it below.
                pass

        pyassimp = self._assimp()
        import numpy as np
        from pyassimp import postprocess

        flags = (
            postprocess.aiProcess_Triangulate
            | postprocess.aiProcess_JoinIdenticalVertices
            | postprocess.aiProcess_GenSmoothNormals
            | postprocess.aiProcess_PreTransformVertices
            | postprocess.aiProcess_ImproveCacheLocality
            | postprocess.aiProcess_SortByPType
        )
        parts: list[dict[str, Any]] = []
        total_triangles = 0
        total_vertices = 0
        minimum = np.array([np.inf, np.inf, np.inf], dtype=np.float32)
        maximum = np.array([-np.inf, -np.inf, -np.inf], dtype=np.float32)
        with pyassimp.load(str(source), processing=flags) as scene:
            material_names: list[str] = []
            for material in scene.materials:
                properties = getattr(material, "properties", {}) or {}
                value = properties.get(("name", 0), properties.get("name", ""))
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                material_names.append(str(value or "").strip())
            for mesh in scene.meshes:
                positions = np.ascontiguousarray(mesh.vertices, dtype="<f4")
                if not len(positions):
                    continue
                if source.suffix.casefold() == ".dae":
                    # Scrap Mechanic's animated Collada assets declare
                    # ``unit meter="0.01"``.  Assimp converts that geometry to
                    # metres, while the compiled shape/prefab transforms use
                    # the game's centimetre-sized mesh coordinates.  Restore
                    # the authored scale or packing-station pieces and other
                    # character meshes appear at only 1/100 of their size.
                    positions = np.ascontiguousarray(positions * 100.0, dtype="<f4")
                faces = np.asarray(mesh.faces)
                if faces.ndim != 2 or faces.shape[1] != 3:
                    continue
                remaining = MAX_MESH_TRIANGLES - total_triangles
                if remaining <= 0:
                    break
                faces = np.ascontiguousarray(faces[:remaining].reshape(-1), dtype="<u4")
                normals_value = getattr(mesh, "normals", None)
                if normals_value is not None and len(normals_value) == len(positions):
                    normals = np.ascontiguousarray(normals_value, dtype="<f4")
                else:
                    normals = np.zeros_like(positions, dtype="<f4")
                    normals[:, 2] = 1.0
                texture_channels = getattr(mesh, "texturecoords", None)
                if texture_channels is not None and len(texture_channels):
                    texture_values = np.asarray(texture_channels[0])
                    uvs = np.ascontiguousarray(texture_values[:, :2], dtype="<f4")
                else:
                    uvs = np.zeros((len(positions), 2), dtype="<f4")
                if texture_channels is not None and len(texture_channels) > 1:
                    texture_values_1 = np.asarray(texture_channels[1])
                    uv1s = np.ascontiguousarray(texture_values_1[:, :2], dtype="<f4")
                else:
                    uv1s = np.ascontiguousarray(uvs, dtype="<f4")
                minimum = np.minimum(minimum, positions.min(axis=0))
                maximum = np.maximum(maximum, positions.max(axis=0))
                triangle_count = len(faces) // 3
                material_index = int(getattr(mesh, "materialindex", 0) or 0)
                parts.append({
                    "positions": self._encoded(positions),
                    "normals": self._encoded(normals),
                    "uvs": self._encoded(uvs),
                    "uv1s": self._encoded(uv1s),
                    "indices": self._encoded(faces),
                    "vertices": len(positions),
                    "indicesCount": len(faces),
                    "material": material_index,
                    "materialName": (
                        material_names[material_index]
                        if 0 <= material_index < len(material_names) else ""
                    ),
                })
                total_vertices += len(positions)
                total_triangles += triangle_count

        if not parts:
            raise RuntimeError(f"No triangle geometry was decoded from {source.name}")
        result = {
            "uuid": renderable.uuid,
            "name": renderable.name,
            "lod": selected_lod,
            "lodCount": len(lods),
            "format": source.suffix.casefold().lstrip("."),
            "parts": parts,
            "bounds": [minimum.tolist(), maximum.tolist()],
            "vertices": total_vertices,
            "triangles": total_triangles,
            "truncated": total_triangles >= MAX_MESH_TRIANGLES,
        }
        self._memory[key] = result
        try:
            geometry_dir.mkdir(parents=True, exist_ok=True)
            temporary = geometry_cache.with_suffix(".tmp")
            with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=3) as stream:
                json.dump(result, stream, separators=(",", ":"))
            temporary.replace(geometry_cache)
        except OSError:
            pass
        return result

    @staticmethod
    def _texture_source(
        renderable: TerrainRenderable, material: int, material_name: str = ""
    ) -> Path | None:
        """Resolve Assimp's material back to the asset's installed diffuse."""
        normalized_name = str(material_name or "").strip().casefold()
        named = dict(renderable.diffuse_by_material)
        # A known material with no diffuse is intentional (mesh decals, glass,
        # glow-only layers).  Do not replace it with a texture from a different
        # submesh: that was the main cause of mosaic/atlas corruption.
        if normalized_name and normalized_name in named:
            return named[normalized_name]
        source = None
        candidates = renderable.diffuse_textures
        if source is None:
            source = candidates[material] if 0 <= material < len(candidates) else None
        if source is None and len([item for item in candidates if item is not None]) == 1:
            source = next((item for item in candidates if item is not None), None)
        return source

    @staticmethod
    def _asg_source(
        renderable: TerrainRenderable, material: int, material_name: str = ""
    ) -> Path | None:
        """Resolve the ASG companion used by the installed material shader."""
        normalized_name = str(material_name or "").strip().casefold()
        named = dict(renderable.asg_by_material)
        if normalized_name and normalized_name in named:
            return named[normalized_name]
        candidates = renderable.asg_textures
        source = candidates[material] if 0 <= material < len(candidates) else None
        if source is None and len([item for item in candidates if item is not None]) == 1:
            source = next((item for item in candidates if item is not None), None)
        return source

    def texture_details(
        self, renderable: TerrainRenderable, material: int, material_name: str = ""
    ) -> dict[str, Any]:
        """Return a browser texture plus the game's channel interpretation."""
        source = self._texture_source(renderable, material, material_name)
        if source is None or not source.is_file():
            return {"uri": "", "asgUri": "", "alphaLuma": False}
        asg_source = self._asg_source(renderable, material, material_name)
        asg_uri = (
            self.image_uri(asg_source)
            if asg_source is not None and asg_source.is_file()
            else ""
        )
        source_key = (str(source.resolve()).casefold(), source.stat().st_mtime_ns)
        if source_key in self._empty_texture_memory:
            return {"uri": "", "asgUri": asg_uri, "alphaLuma": False}
        remembered = self._texture_profile_memory.get(source_key)
        if remembered is not None:
            return {**remembered, "asgUri": asg_uri}
        signature = f"{source.resolve()}:{source.stat().st_mtime_ns}:{MESH_CACHE_REVISION}"
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:20]
        output_dir = self.cache_root / "mesh-textures"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{digest}.png"
        profile_path = output_dir / f"{digest}.profile.json"
        if profile_path.is_file():
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                empty = bool(profile.get("empty"))
                details = {
                    "uri": "" if empty or not output.is_file() else output.resolve().as_uri(),
                    "alphaLuma": bool(profile.get("alphaLuma")),
                }
                if empty:
                    self._empty_texture_memory.add(source_key)
                else:
                    self._texture_profile_memory[source_key] = details
                return {**details, "asgUri": asg_uri}
            except (OSError, ValueError, TypeError):
                pass
        with Image.open(source) as image_file:
            image = image_file.convert("RGBA")
        extrema = image.getextrema()
        rgb_max = max(channel[1] for channel in extrema[:3])
        alpha_min, alpha_max = extrema[3]
        if rgb_max <= 4 and alpha_max <= 4:
            self._empty_texture_memory.add(source_key)
            profile_path.write_text(
                json.dumps({"empty": True, "alphaLuma": False}),
                encoding="utf-8",
            )
            return {"uri": "", "asgUri": asg_uri, "alphaLuma": False}
        if rgb_max <= 4 and alpha_min == alpha_max:
            # Uniform black/alpha maps are game placeholders.  The object gets
            # its appearance from its material tint, so sampling the map would
            # turn otherwise valid pieces black or solid white.
            self._empty_texture_memory.add(source_key)
            profile_path.write_text(
                json.dumps({"empty": True, "alphaLuma": False}),
                encoding="utf-8",
            )
            return {"uri": "", "asgUri": asg_uri, "alphaLuma": False}
        alpha_luma = rgb_max <= 4 and alpha_max > 4
        # Keep diffuse alpha intact.  Scrap Mechanic uses it as a paint mask:
        # dark pixels receive the instance/material colour and light pixels
        # retain the authored diffuse.  Converting it to grayscale destroyed
        # that mask and caused flat grey rocks and oily/mosaic building faces.
        if not output.is_file():
            if max(image.size) > MAX_TEXTURE_SIDE:
                image.thumbnail((MAX_TEXTURE_SIDE, MAX_TEXTURE_SIDE), Image.Resampling.LANCZOS)
            image.save(output, "PNG", compress_level=4)
        details = {
            "uri": output.resolve().as_uri(),
            "asgUri": asg_uri,
            "alphaLuma": alpha_luma,
        }
        profile_path.write_text(
            json.dumps({"empty": False, "alphaLuma": alpha_luma}),
            encoding="utf-8",
        )
        self._texture_profile_memory[source_key] = {
            "uri": details["uri"],
            "alphaLuma": details["alphaLuma"],
        }
        return dict(details)

    def texture_uri(
        self, renderable: TerrainRenderable, material: int, material_name: str = ""
    ) -> str:
        """Backward-compatible URI-only texture lookup."""
        return str(
            self.texture_details(renderable, material, material_name).get("uri") or ""
        )

    def image_uri(self, source: Path) -> str:
        """Convert another installed texture through the browser-safe cache."""
        source = source.resolve()
        if not source.is_file():
            return ""
        signature = f"{source}:{source.stat().st_mtime_ns}:{MESH_CACHE_REVISION}"
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:20]
        output_dir = self.cache_root / "mesh-textures"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{digest}.png"
        if not output.is_file():
            with Image.open(source) as image_file:
                image = image_file.convert("RGBA")
            if max(image.size) > MAX_TEXTURE_SIDE:
                image.thumbnail((MAX_TEXTURE_SIDE, MAX_TEXTURE_SIDE), Image.Resampling.LANCZOS)
            image.save(output, "PNG", compress_level=4)
        return output.resolve().as_uri()

    def decal_uri(self, source: Path, region: tuple[int, int, int, int]) -> str:
        """Crop one decal from Scrap Mechanic's 16x16 transparent atlas."""
        source = source.resolve()
        if not source.is_file():
            return ""
        signature = (
            f"{source}:{source.stat().st_mtime_ns}:{region}:{MESH_CACHE_REVISION}"
        )
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:20]
        output_dir = self.cache_root / "decal-textures"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{digest}.png"
        if not output.is_file():
            with Image.open(source) as image_file:
                image = image_file.convert("RGBA")
            cell_width = image.width // 16
            cell_height = image.height // 16
            x0, x1, y0, y1 = region
            box = (
                max(0, x0 * cell_width),
                max(0, y0 * cell_height),
                min(image.width, (x1 + 1) * cell_width),
                min(image.height, (y1 + 1) * cell_height),
            )
            image.crop(box).save(output, "PNG", compress_level=4)
        return output.resolve().as_uri()
