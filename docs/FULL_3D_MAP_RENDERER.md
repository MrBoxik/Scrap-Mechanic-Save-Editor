# Full 3D world-map renderer

The installed game does not contain one complete Survival-world model. Scrap
Mechanic generates the world at runtime from the terrain grid stored in the
save. Each grid placement points to a terrain tile UUID and stores its exact
rotation and multi-cell offsets.

The initial **2D map** composes the exact saved terrain atlas, rotations, and
placement bounds. Selected locations show the installed game's original tile
artwork separately. **Build HD map** captures the geometry-based overhead view
in bounded sections and assembles the PNG in Python, rather than keeping one
full-world canvas alongside the entire mesh scene. **Fit** in 3D returns the
camera to a straight overhead view; there is no separate Top view control.

## Verified source pipeline

The full renderer can rebuild the world from these installed files without
redistributing game assets:

1. Decode the save's `uid`, `rotation`, `xOffset`, `yOffset`, and `groupId`
   terrain arrays.
2. Resolve each tile UUID to its `.tileson` scene.
3. Read asset, blueprint, decal, harvestable, prefab, and terrain entries from
   that scene.
4. Resolve every placed asset UUID through the base-game and Survival
   `.assetset` databases.
5. Follow external `.rend` definitions to their LOD meshes, materials, and
   texture lists.
6. Convert and persistently cache only the scenes, models, textures, and
   texture profiles needed by the loaded save, then
   progressively add all generated placements to one persistent WebGL scene.

The editor now implements the native-tile renderer milestone:

- `map-3d-index` returns lightweight world placements and unique tile scenes.
- `map-3d-scene-packs` resolves requested tiles into bounded gzip chunks of
  instanced transforms and mesh/material/texture definitions. This prevents a
  single 1024-cell scene from blocking an enormous desktop-bridge response.
- `$GAME_DATA` and `$SURVIVAL_DATA` virtual paths are resolved only inside the
  user's installed Scrap Mechanic directory.
- repeated objects are represented as instances rather than duplicated mesh
  data.
- `map-3d-meshes` uses the Assimp 5.0 DLL from the user's installed game to
  decode FBX, DAE, and OBJ models into packed position, normal, UV, and index
  buffers in batches.
- `map-3d-texture` converts installed diffuse TGA/DDS textures into a bounded
  local PNG cache; game content is never included in the release.
- the desktop WebGL 2 viewport loads every model definition required by the
  selected tile or complete generated world,
  uses GPU instancing, applies object transforms and the selected saved tile
  rotation, and supports free camera flight, mouse-look, pan, wheel movement,
  lighting, and fit-to-view.
- the compiled `.tile` mip is LZ4-decoded into its real 33×33 height grid per
  cell and its 65×65 ground-material weights; terrain uses installed ground
  diffuse textures rather than a diagnostic plane.
- the save's generated elevation and cliff-level corner arrays are uploaded as
  a GPU height field. Terrain, objects, and decals use the same
  `detail + elevation + cliff` composition as `terrain_overworld.lua`; water
  follows the game's separate cliff-only placement rule.
- world placement follows the installed game's authoritative helpers rather
  than guessed editor transforms: `GetTileRangesFromCell` in `util.lua`
  supplies placement origins, while `RotateLocal` and `GetRotationQuat` in
  `terrain_util2.lua` define the saved quarter-turn handedness.
- harvestable databases and object shape sets resolve the tile's harvestables
  and simple one-part blueprint props through the same mesh pipeline.
- compiled tile and prefab quaternions replace ambiguous editor Euler angles.
- editable prefab scenes are expanded into their installed asset and simple
  blueprint children recursively, including complete warehouse and packing
  station assemblies.
- both `subMeshMap` and `subMeshList` material definitions are resolved, and
  alpha-tested two-sided foliage no longer appears as white geometry.
- the installed `Data/Render/Materials/asset_materials.json` and
  `part_materials.json` profiles decide which UV channel each texture samples;
  a profile suffix containing `UV1` is not treated as proof that its diffuse
  texture uses UV1.
- diffuse alpha is kept as the game's Paint Tool mask. ASG red, green, blue,
  and alpha channels are used for transparency, specular response, glow, and
  reflectivity instead of being mistaken for visible colour.
- all nine compiled terrain material weights are smoothly blended.
- installed water and chemical meshes keep their real shape and use dedicated
  translucent materials.
- generated build-quest tiles may reference a locked blueprint as part of
  their normal world assembly. Those installed blueprints are expanded even
  though they are unavailable to the player in the build tool.
- compiled decal quaternions are preserved, so road, tire, parking, and ground
  markings keep the same handedness and orientation as the game;
- authored-horizontal ground decals are included; unsupported curved and
  effect projector volumes, `MeshDecal` passes, and light-cone helpers are
  safely deferred.
- partial multi-cell POIs use their actual occupied-cell masks instead of a
  rectangular footprint. This prevents Excavation Island's mountain, bridge,
  and beach scenes from drawing terrain or water over one another.
- explicit water remains depth-tested but is not pulled toward the camera with
  decal polygon offset, preventing shoreline and ocean z-fighting.
- compiled terrain keeps one independent 33-by-33 vertex grid per owning cell,
  matching `sm.terrainTile.getHeightAt`. Shared borders occupy the same world
  position but retain separate owner metadata, so an exact-border vertex never
  receives its neighbor's cliff base. No synthetic skirts or aprons are used;
- generated elevation follows Survival's cubic Bezier control points and road
  flatness flags instead of bilinear corner interpolation;
- supported horizontal decals use a tessellated projection mesh whose vertices
  follow generated terrain height. Wall, effect, and arbitrary curved projector
  volumes remain deferred rather than being rendered as misleading planes.
- `map-3d-entities` decodes real Unit, RigidBody, and ChildShape records. It
  resolves installed character meshes and reconstructs nearby creation shapes
  with saved colors, dimensions, body quaternions, and all 24 child axes.
- runtime state tags are honored so event-only effects and destroyed variants
  do not overlap the default scene.

On the current test world, the index finds all **445 unique tile scenes** used
by **11,105 terrain placements**. A grid audit found all **12,288 expected
cells covered**, with no missing, duplicate, or out-of-bounds cells. Only nine
of 24,352 shared borders differ by more than four metres; those are authored
special-location transitions rather than missing world data. The large 8x8 chemical-dungeon tile alone
contains 1,471 asset instances, 420 decals, 47 harvestables, 21 prefabs, and 8
blueprints. All 102 unique asset UUIDs in that tile resolve; 98 point directly
to FBX, DAE, or OBJ meshes and four use the game's `.mesh` format.

## Current milestone boundary

The direct **3D world** mode renders the persistent complete generated world.
It indexes the full grid, groups
placements by scene and asset definition, and progressively adds every
placement without clearing earlier cells when the camera moves. A validation
run loaded all 11,105 placements and 571,785 object instances, plus one player,
767 units, and 975 saved creation instances from the current test save while
keeping the desktop application responsive.

The camera is a true free camera rather than an orbit target: drag-to-look and
Q/E rotate the eye in place, camera-relative WASD flies, Space/Ctrl moves
vertically, Shift boosts, and right-drag pans. Fit-to-view and the
geometry-based top view establish new camera poses. Switching to 2D does not
dispose the complete-world renderer.

Scene resolution, packed mesh geometry, converted PNG textures, validated
texture-profile metadata, and decoded save-entity assemblies persist below
the editor's local map cache. The
renderer batches Python bridge requests, prioritizes common repeated models,
keeps immutable instance buffers resident on the GPU, groups scene instances
in a single pass, back-face culls only the authored terrain surface, and caches
shader uniform locations. Camera movement changes only the view matrix; it no longer
rebuilds or re-uploads hundreds of thousands of transforms. It also reuses
world geometry for creation parts and waits for image decode/GPU upload before
declaring the world complete. Full detail is now the single default mode, so
distant props are not silently sampled away.

## Remaining renderer work

The scene is progressively assembled instead of blocking startup. Further
renderer improvements can still add:

- broader coverage for any remaining proprietary `.mesh` variants;
- additional distance-based mesh LOD and explicit GPU-memory limits for
  unusually large or modded saves;
- proprietary prefab-grid, complex multi-body blueprint, kinematic, particle,
  and clutter adapters;
- a true deferred projector implementation for decals that wrap arbitrary
  vertical meshes (safe horizontal ground decals already tessellate and conform
  to generated terrain; unsupported projector volumes are deferred);
- skeletal animation for the installed player/enemy meshes (their static
  meshes and reconstructed player-built creations are already in 3D);
- game-runtime particles, volumetric lighting, clutter grass, smoke, and
  engine shader passes. Static WebGL rendering can approximate their installed
  materials, but cannot execute Scrap Mechanic's runtime renderer directly.

The compiled-ground, generated-height, material-profile, occupied-cell
masking, direct persistent complete-world, and geometry top-view milestones
are implemented. Repeated geometry
continues to use GPU instancing rather than duplicating a raw mesh per object.

## Asset policy

The editor should read and cache assets from the user's own Scrap Mechanic
installation. Releases must not bundle Axolot's meshes, textures, tile scenes,
or other game content.
