# 4.22.1 Validation

Tested on Windows 11 with Python 3.13 and Scrap Mechanic 1.0.6 Build 889,
launched through Steam. This is a release candidate. The broader map/editor
rework and its real-world measurements are documented in
`RELEASE_4.22.0_VALIDATION.md`; this continuation targets session races.

## Reproduced Defect

A deterministic test paused an exploration writer immediately before atomic
replacement of its file. The old implementation allowed a simultaneous reload
to read the older file and replace newer exploration in memory. The test failed
before the fix and passes with the exploration IO lock. A separate assertion
verifies that the live sampler lock remains available while disk IO is paused.

## Checks

- `python -m unittest discover -q`: 138 tests passed in 209 seconds.
- Six added regressions cover in-flight exploration flushes, failed writes,
  stale save detection, hide-before-flush ordering, stale map preparation,
  and session changes during image preparation.
- Failed writes retain dirty exploration and defer the next world binding.
- `python tools/release_check.py` passed for 4.22.1; Python compilation passed.
- Map navigation and incremental visibility/LOD JavaScript checks passed.
- Chrome/Playwright map checks passed with no browser errors, three image-load
  attempts recovering successfully, native motion producing 84 distinct frames
  out of 85 versus 62/84 for the intentionally broken control, and renderer
  teardown releasing the old geometry, listeners, and WebGL program.
- Portable companion self-check returned exit code 0. Windows file/product
  versions both report 4.22.1. ZIP CRC checks passed for all 1,217 entries;
  all 23 packaged web assets match the source hashes.
- The packaged editor reached its populated save library. A duplicate launch
  returned exit code 0 and left exactly one editor process/window.

## Game Observation

The source companion and then the packaged 4.22.1 companion were each tested
with a full Steam game launch into an existing cached Survival world. The map
and existing exploration appeared without a wake-up click. The packaged native
rounded-square surface had no rectangular backing outside its outline. Runtime
state reported ready, not busy, with live telemetry and native frames updating.
One observed startup window measured about 43 native frames/second and 48
memory samples/second; this is not a sustained-gameplay frame-rate guarantee.
Closing the game removed all companion windows, set both visibility flags to
false, and terminated the packaged companion process.

## Remaining Acceptance Coverage

The automation interface still does not reliably deliver game M/Escape/WASD
input. Physical M-key behavior, sustained walking/turning, fresh typed-world
creation, and menu-to-world switching inside one process require a human
acceptance pass. They are not claimed as verified by the deterministic tests
or the IPC-based full-map test from 4.22.0. A source shutdown printed a pywebview
disposed-WebView warning but exited successfully with no companion windows;
this is recorded rather than treated as a clean diagnostic log.

Browser evidence: `artifacts/release-4.22.1`. Run the browser checker with
`MAP_TEST_OUTPUT` set to that directory to retain version-specific evidence.
