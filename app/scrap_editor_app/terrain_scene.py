from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Any, Iterable

from terrain_map import strip_json_comments


@dataclass(frozen=True)
class TerrainRenderable:
    """One renderable terrain asset resolved from Scrap Mechanic's UUID data."""

    uuid: str
    name: str
    mesh: Path
    lod_meshes: tuple[Path, ...]
    lod_min_view_sizes: tuple[float | None, ...]
    lod_max_view_distances: tuple[float | None, ...]
    smallest_visible_size: float | None
    impostor_threshold: float | None
    occluder_corners: tuple[tuple[float, float, float], ...]
    textures: tuple[Path, ...]
    diffuse_textures: tuple[Path | None, ...]
    diffuse_by_material: tuple[tuple[str, Path | None], ...]
    asg_textures: tuple[Path | None, ...]
    asg_by_material: tuple[tuple[str, Path | None], ...]
    material_profile_by_material: tuple[tuple[str, str], ...]
    alpha_luma_by_material: tuple[tuple[str, bool], ...]
    tinted_detail_by_material: tuple[tuple[str, bool], ...]
    uv_material_by_material: tuple[tuple[str, bool], ...]
    use_uv1_by_material: tuple[tuple[str, bool], ...]
    color_key_by_material: tuple[tuple[str, str], ...]
    default_colors: tuple[tuple[str, str], ...]
    material_names: tuple[str, ...]
    source: Path


@dataclass(frozen=True)
class BlueprintPrimitive:
    """An engine-generated block or wedge used by local blueprints."""

    uuid: str
    name: str
    kind: str
    texture: Path | None
    color: str
    tiling: float
    glass: bool
    source: Path


@dataclass(frozen=True)
class TerrainDecal:
    """One projected decal resolved from the installed decal atlases."""

    uuid: str
    name: str
    texture: Path
    region: tuple[int, int, int, int]
    material: str


@dataclass(frozen=True)
class TileSceneSummary:
    """The render-relevant inventory of a .tileson scene."""

    path: Path
    tile_uuid: str
    cells_x: int
    cells_y: int
    asset_instances: int
    unique_asset_uuids: tuple[str, ...]
    blueprints: int
    decals: int
    harvestables: int
    prefabs: int


class GamePathResolver:
    """Resolve the two virtual roots used by the installed content files."""

    def __init__(self, game_root: Path) -> None:
        self.game_root = game_root.resolve()
        self.roots = {
            "$GAME_DATA/": self.game_root / "Data",
            "$SURVIVAL_DATA/": self.game_root / "Survival",
        }

    def resolve(self, value: str) -> Path | None:
        normalized = str(value or "").replace("\\", "/")
        upper = normalized.upper()
        for token, root in self.roots.items():
            if upper.startswith(token):
                relative = normalized[len(token) :]
                return (root / Path(relative)).resolve()
        return None


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(strip_json_comments(path.read_text(encoding="utf-8-sig")))
    return value if isinstance(value, dict) else {}


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


class TerrainAssetIndex:
    """Index native terrain renderables without loading their mesh bytes.

    The game's .tileson files store UUIDs, not model paths.  Asset-set JSON
    files provide the missing UUID -> mesh/material/texture relationship.  A
    future streamed WebGL renderer can use this index to request only models
    visible in the camera and instance repeated objects instead of expanding
    the whole world at once.
    """

    def __init__(self, game_root: Path) -> None:
        self.game_root = game_root.resolve()
        self.paths = GamePathResolver(self.game_root)
        self.renderables: dict[str, TerrainRenderable] = {}
        self.blueprint_primitives: dict[str, BlueprintPrimitive] = {}
        self.asset_sets: tuple[Path, ...] = ()
        self.material_defines: dict[str, frozenset[str]] = {}

    def build(self) -> "TerrainAssetIndex":
        for source in (
            self.game_root / "Data" / "Render" / "Materials"
        ).glob("*.json"):
            try:
                profiles = _read_json(source)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            for profile_name, profile in profiles.items():
                if not isinstance(profile, dict):
                    continue
                defines = profile.get("defines")
                self.material_defines[str(profile_name).casefold()] = frozenset(
                    str(value).upper() for value in defines
                ) if isinstance(defines, list) else frozenset()
        asset_sets: list[Path] = []
        for database in (
            self.game_root / "Data" / "Terrain" / "Database" / "assetsets.json",
            self.game_root / "Survival" / "Terrain" / "Database" / "assetsets.json",
        ):
            if not database.is_file():
                continue
            for row in _read_json(database).get("assetSetList", []):
                if not isinstance(row, dict):
                    continue
                path = self.paths.resolve(str(row.get("assetSet") or ""))
                if path and path.is_file() and path not in asset_sets:
                    asset_sets.append(path)

        # Harvestables and blueprint shapes use the same .rend -> mesh
        # pipeline as terrain assets, but live in separate game databases.
        for database, list_name, value_key in (
            (
                self.game_root / "Survival" / "Harvestables" / "Database"
                / "harvestablesets.json",
                "harvestableSetList",
                "name",
            ),
            (
                self.game_root / "Survival" / "Objects" / "Database"
                / "shapesets.json",
                "shapeSetList",
                "",
            ),
            (
                self.game_root / "Data" / "Objects" / "Database"
                / "shapesets.json",
                "shapeSetList",
                "",
            ),
        ):
            if not database.is_file():
                continue
            for row in _read_json(database).get(list_name, []):
                value = row.get(value_key) if value_key and isinstance(row, dict) else row
                path = self.paths.resolve(str(value or ""))
                if path and path.is_file() and path not in asset_sets:
                    asset_sets.append(path)

        for source in asset_sets:
            data = _read_json(source)
            for list_name, rows in data.items():
                if not isinstance(rows, list):
                    continue
                if list_name in {"blockList", "wedgeList"}:
                    for row in rows:
                        primitive = self._parse_blueprint_primitive(
                            row, source, "wedge" if list_name == "wedgeList" else "block"
                        )
                        if primitive:
                            self.blueprint_primitives[primitive.uuid] = primitive
                    continue
                if not list_name.startswith(
                    ("assetList", "harvestableList", "partList")
                ):
                    continue
                for row in rows:
                    renderable = self._parse_renderable(row, source)
                    if renderable:
                        self.renderables[renderable.uuid] = renderable

        # Units and the mechanic use the same renderable/mesh/material format,
        # but their UUID catalogue lives under Character instead of Terrain or
        # Objects.  Indexing it here lets the world viewer draw the exact
        # installed character meshes at positions stored by the save.
        character_sets: list[Path] = []
        for database in (
            self.game_root / "Survival" / "Character" / "charactersets.json",
            self.game_root / "Data" / "Character" / "charactersets.json",
        ):
            if not database.is_file():
                continue
            for value in _read_json(database).get("characterSetList", []):
                source = self.paths.resolve(str(value or ""))
                if source and source.is_file() and source not in character_sets:
                    character_sets.append(source)
        for source in character_sets:
            for row in _read_json(source).get("characters", []):
                if not isinstance(row, dict):
                    continue
                renderables = row.get("renderables")
                renderable_path = next(
                    (
                        value for value in renderables
                        if isinstance(value, str) and value.casefold().endswith(".rend")
                    ),
                    None,
                ) if isinstance(renderables, list) else None
                if not renderable_path:
                    continue
                renderable = self._parse_renderable(
                    {
                        "uuid": row.get("uuid"),
                        "name": row.get("name") or row.get("uuid"),
                        "renderable": renderable_path,
                        "defaultColors": {
                            "character": (row.get("paint") or {}).get("defaultColor", "ffffff")
                            if isinstance(row.get("paint"), dict) else "ffffff"
                        },
                    },
                    source,
                )
                if renderable:
                    self.renderables[renderable.uuid] = renderable
        self.asset_sets = tuple(asset_sets)
        return self

    def _parse_renderable(
        self, row: Any, source: Path
    ) -> TerrainRenderable | None:
        if not isinstance(row, dict):
            return None
        item_uuid = str(row.get("uuid") or "").casefold()
        renderable = row.get("renderable")
        if not item_uuid:
            return None
        if isinstance(renderable, str):
            renderable_path = self.paths.resolve(renderable)
            if not renderable_path or not renderable_path.is_file():
                return None
            renderable = _read_json(renderable_path)
        if not isinstance(renderable, dict):
            return None
        lod_values = renderable.get("lodList")
        if not isinstance(lod_values, list):
            lod_values = [renderable]
        resolved_lods: list[tuple[dict[str, Any], Path]] = []
        for item in lod_values:
            if not isinstance(item, dict) or not item.get("mesh"):
                continue
            path = self.paths.resolve(str(item.get("mesh") or ""))
            if path and path.suffix.casefold() in {".fbx", ".dae", ".obj", ".mesh"}:
                resolved_lods.append((item, path))
        if not resolved_lods:
            return None
        lod, mesh = resolved_lods[0]
        lod_meshes = tuple(path for _, path in resolved_lods)
        lod_min_view_sizes = tuple(
            _optional_float(item.get("minViewSize")) for item, _ in resolved_lods
        )
        lod_max_view_distances = tuple(
            _optional_float(item.get("maxViewDistance")) for item, _ in resolved_lods
        )
        corners_value = renderable.get("occluderCorners")
        occluder_corners = tuple(
            (float(corner[0]), float(corner[1]), float(corner[2]))
            for corner in corners_value
            if isinstance(corner, list)
            and len(corner) >= 3
            and all(isinstance(value, (int, float)) for value in corner[:3])
        ) if isinstance(corners_value, list) else ()

        textures: list[Path] = []
        diffuse_textures: list[Path | None] = []
        diffuse_by_material: list[tuple[str, Path | None]] = []
        asg_textures: list[Path | None] = []
        asg_by_material: list[tuple[str, Path | None]] = []
        material_profile_by_material: list[tuple[str, str]] = []
        alpha_luma_by_material: list[tuple[str, bool]] = []
        tinted_detail_by_material: list[tuple[str, bool]] = []
        uv_material_by_material: list[tuple[str, bool]] = []
        use_uv1_by_material: list[tuple[str, bool]] = []
        color_key_by_material: list[tuple[str, str]] = []
        materials: list[str] = []
        sub_mesh_map = lod.get("subMeshMap")
        sub_mesh_list = lod.get("subMeshList")
        submeshes: Iterable[tuple[Any, Any]] = (
            sub_mesh_map.items() if isinstance(sub_mesh_map, dict)
            else enumerate(sub_mesh_list) if isinstance(sub_mesh_list, list)
            else ()
        )
        for submesh_name, material in submeshes:
            if not isinstance(material, dict):
                continue
            name = str(material.get("material") or "").strip()
            profile_defines = self.material_defines.get(name.casefold(), frozenset())
            if name and name not in materials:
                materials.append(name)
            material_textures: list[Path] = []
            # Older terrain renderables use ``textureList`` while many newer
            # Survival assets (packing stations, builder-quest structures,
            # crystals, mine props, etc.) use a named ``textures`` object.
            # Ignoring the latter left those submeshes untextured and allowed
            # the renderer's positional fallback to sample an unrelated atlas.
            texture_values = material.get("textures")
            if not isinstance(texture_values, (dict, list)):
                texture_values = material.get("textureList", [])
            for value in _walk_strings(texture_values):
                texture = self.paths.resolve(value)
                if texture:
                    material_textures.append(texture)
                    if texture not in textures:
                        textures.append(texture)
            named_diffuse = (
                self.paths.resolve(str(texture_values.get("diffuse") or ""))
                if isinstance(texture_values, dict) else None
            )
            named_asg = (
                self.paths.resolve(str(texture_values.get("asg") or ""))
                if isinstance(texture_values, dict) else None
            )
            diffuse = named_diffuse or next(
                (
                    texture for texture in material_textures
                    if "_dif" in texture.stem.casefold()
                    or "diffuse" in texture.stem.casefold()
                ),
                material_textures[0] if material_textures else None,
            )
            asg = named_asg or next(
                (
                    texture for texture in material_textures
                    if "_asg" in texture.stem.casefold()
                    or texture.stem.casefold().endswith("asg")
                ),
                None,
            )
            diffuse_textures.append(diffuse)
            asg_textures.append(asg)
            normalized_name = str(submesh_name).strip().casefold()
            aliases = [normalized_name]
            material_alias = name.casefold()
            if material_alias and material_alias not in aliases:
                aliases.append(material_alias)
            custom = material.get("custom")
            color_key = (
                str(custom.get("color") or "").strip()
                if isinstance(custom, dict) else ""
            )
            for alias in aliases:
                if not alias:
                    continue
                diffuse_by_material.append((alias, diffuse))
                asg_by_material.append((alias, asg))
                if name:
                    material_profile_by_material.append((alias, name))
                if (
                    "uvmaterial" in name.casefold()
                ):
                    # UvMaterial is an explicit game shader family.  Do not
                    # infer it merely from a texture path: a few ordinary Dif
                    # materials intentionally sample the same atlas through
                    # UV0 and must keep its literal RGB values.
                    uv_material_by_material.append((alias, True))
                if (
                    color_key
                    and diffuse
                    and diffuse.stem.casefold().startswith("t_tiled_")
                ):
                    # These nearly-black RGB images are detail masks.  The
                    # installed shader supplies the visible base from
                    # ``custom.color`` instead of treating RGB as albedo.
                    tinted_detail_by_material.append((alias, True))
                if (
                    "PS_DIF_UV1" in profile_defines
                    or "uvmaterial" in name.casefold()
                ):
                    # The installed material catalogue defines these profiles
                    # with PS_DIF_UV1.  Do not infer this from ``_UV1`` in a
                    # shader name: profiles such as DifAsgNorNorD_UV1 use UV1
                    # only for the detail normal while their diffuse stays on
                    # UV0.  Switching the diffuse produced the rainbow
                    # mosaic/smeared-atlas corruption on large assets.
                    use_uv1_by_material.append((alias, True))
                if color_key:
                    color_key_by_material.append((alias, color_key))
        defaults = row.get("defaultColors")
        default_colors = tuple(
            (str(key), str(value))
            for key, value in defaults.items()
            if str(key).strip() and str(value).strip()
        ) if isinstance(defaults, dict) else ()
        return TerrainRenderable(
            uuid=item_uuid,
            name=str(row.get("name") or item_uuid),
            mesh=mesh,
            lod_meshes=lod_meshes,
            lod_min_view_sizes=lod_min_view_sizes,
            lod_max_view_distances=lod_max_view_distances,
            smallest_visible_size=_optional_float(
                renderable.get("smallestVisibleSize")
            ),
            impostor_threshold=_optional_float(
                renderable.get("impostorThreshold")
            ),
            occluder_corners=occluder_corners,
            textures=tuple(textures),
            diffuse_textures=tuple(diffuse_textures),
            diffuse_by_material=tuple(diffuse_by_material),
            asg_textures=tuple(asg_textures),
            asg_by_material=tuple(asg_by_material),
            material_profile_by_material=tuple(material_profile_by_material),
            alpha_luma_by_material=tuple(alpha_luma_by_material),
            tinted_detail_by_material=tuple(tinted_detail_by_material),
            uv_material_by_material=tuple(uv_material_by_material),
            use_uv1_by_material=tuple(use_uv1_by_material),
            color_key_by_material=tuple(color_key_by_material),
            default_colors=default_colors,
            material_names=tuple(materials),
            source=source,
        )

    def _parse_blueprint_primitive(
        self, row: Any, source: Path, kind: str
    ) -> BlueprintPrimitive | None:
        if not isinstance(row, dict):
            return None
        item_uuid = str(row.get("uuid") or "").casefold()
        if not item_uuid:
            return None
        texture = self.paths.resolve(str(row.get("dif") or ""))
        if texture is None:
            values = row.get("textureList")
            if isinstance(values, list) and values:
                texture = self.paths.resolve(str(values[0] or ""))
        raw_tiling = row.get("tiling", 1)
        if isinstance(raw_tiling, list):
            raw_tiling = raw_tiling[0] if raw_tiling else 1
        try:
            tiling = max(0.01, float(raw_tiling))
        except (TypeError, ValueError):
            tiling = 1.0
        return BlueprintPrimitive(
            uuid=item_uuid,
            name=str(row.get("name") or item_uuid),
            kind=kind,
            texture=texture if texture and texture.is_file() else None,
            color=str(row.get("color") or "ffffff"),
            tiling=tiling,
            glass=bool(row.get("glass") or "glass" in str(row.get("material") or "").casefold()),
            source=source,
        )

    def get(self, item_uuid: str) -> TerrainRenderable | None:
        return self.renderables.get(str(item_uuid).casefold())

    def get_blueprint_primitive(self, item_uuid: str) -> BlueprintPrimitive | None:
        return self.blueprint_primitives.get(str(item_uuid).casefold())


class TerrainDecalIndex:
    """Resolve decal UUIDs to their diffuse atlas and inclusive grid region."""

    def __init__(self, game_root: Path) -> None:
        self.game_root = game_root.resolve()
        self.paths = GamePathResolver(self.game_root)
        self.decals: dict[str, TerrainDecal] = {}

    def build(self) -> "TerrainDecalIndex":
        for database in (
            self.game_root / "Data" / "Decals" / "decalsets.json",
            self.game_root / "Survival" / "Decals" / "decalsets.json",
        ):
            if not database.is_file():
                continue
            for row in _read_json(database).get("decalSetList", []):
                if not isinstance(row, dict):
                    continue
                source = self.paths.resolve(str(row.get("set") or ""))
                if not source or not source.is_file():
                    continue
                data = _read_json(source)
                texture = self.paths.resolve(str(data.get("difSheet") or ""))
                decal_list = data.get("decalList")
                if not texture or not texture.is_file() or not isinstance(decal_list, dict):
                    continue
                for name, definition in decal_list.items():
                    if not isinstance(definition, dict):
                        continue
                    item_uuid = str(definition.get("uuid") or "").casefold()
                    region = definition.get("region")
                    if not item_uuid or not isinstance(region, list) or len(region) != 4:
                        continue
                    self.decals[item_uuid] = TerrainDecal(
                        uuid=item_uuid,
                        name=str(name or item_uuid),
                        texture=texture,
                        region=tuple(int(value) for value in region),
                        material=str(definition.get("material") or ""),
                    )
        return self

    def get(self, item_uuid: str) -> TerrainDecal | None:
        return self.decals.get(str(item_uuid).casefold())


def find_tile_scene(
    game_root: Path, item_uuid: str, metadata: dict[str, Any]
) -> Path | None:
    """Find the editable scene that produced a UUID-named terrain preview."""
    name = str(metadata.get("name") or "").strip()
    if not name:
        return None
    category = str(metadata.get("category") or "").strip()
    root = game_root / "Survival" / "Terrain" / "Tiles"
    candidates = [root / f"{name}.tileson"]
    if category and category.casefold() not in {"tile", "tiles", "terrain"}:
        candidates.insert(0, root / category / f"{name}.tileson")
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path:
        return path
    # A few legacy/editor tiles sit outside their metadata category.  Avoid a
    # recursive scan for normal tiles, but retain a deterministic fallback.
    matches = sorted(root.rglob(f"{name}.tileson"))
    return matches[0] if matches else None


def summarize_tile_scene(path: Path) -> TileSceneSummary:
    data = _read_json(path)
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    entities = data.get("entities") if isinstance(data.get("entities"), dict) else {}
    assets = entities.get("assets") if isinstance(entities.get("assets"), list) else []
    asset_uuids = sorted({
        str(row.get("uuid") or "").casefold()
        for row in assets
        if isinstance(row, dict) and row.get("uuid")
    })

    def count(name: str) -> int:
        rows = entities.get(name)
        return len(rows) if isinstance(rows, list) else 0

    return TileSceneSummary(
        path=path,
        tile_uuid=str(info.get("uuid") or "").casefold(),
        cells_x=max(1, int(info.get("cellsX") or 1)),
        cells_y=max(1, int(info.get("cellsY") or 1)),
        asset_instances=len(assets),
        unique_asset_uuids=tuple(asset_uuids),
        blueprints=count("blueprints"),
        decals=count("decals"),
        harvestables=count("harvestables"),
        prefabs=count("prefabs"),
    )
