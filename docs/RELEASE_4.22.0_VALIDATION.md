# 4.22.0 Validation

Tested on Windows 11 with Python 3.13 and Scrap Mechanic 1.0.6 Build 889,
launched normally through Steam. This is a release candidate, not a claim that
every hardware configuration or world has been tested.

## Automated Checks

- `python -m unittest discover -q`: 132 tests passed, including save round trips,
  inventory metadata swaps, stale-source/write protection, snapshots, cached
  map promotion, same-save session rebinding, and exploration persistence.
- `python tools/release_check.py`: version, packaging, assets, and personal-path
  checks passed. Both numeric and string Windows version fields are checked.
- Python compilation and JavaScript syntax checks passed.
- Portable executable companion self-check returned exit code 0; executable
  file/product metadata both report 4.22.0. ZIP CRC verification passed for
  all 1,217 entries, and all 23 packaged web assets match the source hashes.
- Packaged startup reached first content paint in approximately 1.7 seconds
  on this machine. A duplicate launch returned exit code 0 and left exactly
  one editor process/window. Packaged minimap/full-map previews opened.
- `node tools/test_map_navigation.cjs`: projection, player alignment, screen
  clustering, and circle/square offscreen clipping passed.
- `node tools/check_map_ui.cjs` with Playwright and Chrome: real minimap/full-map
  renderers, search, selection, shapes, hint settings, failed-image recovery,
  native bitmap motion, and repeated WebGL renderer teardown passed.
- Native frame regression: 85 distinct moving frames out of 85. A deliberately
  broken transform control produced only 64 distinct frames out of 84, proving
  the test detects repeated terrain frames rather than merely counting calls.
- `node tools/test_map_visibility.cjs`: seven incremental scan slices produce
  the same complete visibility set as an immediate scan. Cancellation and
  capture isolation pass; 1,000 LOD choices match the mathematical reference
  without per-object layout reads.

## Desktop and Game Checks

- Survival terrain overview appeared without input after load, including a
  second full game launch. Existing exploration remained visible.
- Built an uncaptured 8192 x 6144 HD map through all 12 sections in the real
  editor. Returned to the cached HD image successfully.
- The next companion session automatically promoted that world from its cached
  terrain overview to the editor's completed HD image.
- Native minimap frames measured approximately 44-57 Hz and player sampling
  approximately 53-60 Hz in observed windows. Build tools and UI capture were
  also running during some measurements; these are observations, not a fixed
  frame-rate guarantee.
- Verified actual native circle and rounded-square clipping over the game,
  with no rectangular backing outside the selected outline.
- Opened the in-game full map through its native IPC toggle path, selected the
  crashed-ship waypoint, and verified its original tile artwork loaded in the
  details sidebar. Closing the map returned to the live minimap.
- Loaded a Creative save: neither minimap nor full map appeared; runtime state
  correctly reported no active Survival world.
- Game exits removed all companion windows and terminated the companion.
- Exercised HD cancellation through the actual button; the terrain overview
  remained usable and stale renderer work did not replace it.
- Opened every editor section without browser errors or horizontal page
  overflow. Checked the map and companion at 1080 x 700 and a larger viewport.
- Tested the native unsaved-close prompt and cancellation. Only an in-memory
  test edit was staged and then discarded; no user save/profile was written.
- Loaded the packaged full 3D world: 11,114 placements and 571,034 objects,
  including the player, units, and saved creations. Fit reached exactly
  -90-degree pitch, the canvas had no focus outline, camera movement worked,
  and returning to 2D reused the HD map.
- Profiled the interactive overview, then compared old/new visibility logic on
  that same loaded world: zero differences across 16,773 LOD decisions and
  86,507 visible instances in 1,553 groups. Matched three-second navigation
  runs measured CPU mean 16.98 -> 4.91 ms, CPU p95 78.2 -> 6.0 ms, and frame-gap
  p95 78.8 -> 11.7 ms. These are local measurements, not a hardware guarantee.

## Remaining Hands-On Coverage

The Windows automation interface did not reliably deliver physical game key
presses or text input. Consequently, physical M-key handling, newly typed world
creation, sustained WASD gameplay, and menu-to-world switching inside one game
process still need a human acceptance pass. Full process restart, an existing
world without an HD capture, native map IPC, and deterministic session-switch
tests were exercised instead. Challenge-mode exclusion has automated coverage;
Creative exclusion was additionally checked in the running game.

Browser evidence is generated locally under `artifacts/release-4.22.0`.
The browser checker requires `playwright` and an installed Chrome browser;
`MAP_TEST_BROWSER` can select another installed Playwright browser channel.
