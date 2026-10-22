# Scrap Mechanic External Map Companion

The companion runs beside Scrap Mechanic. It does not install a Custom Game,
patch Lua scripts, use developer mode, inject code, write process memory, or
modify a save database.

## Start here

For normal use, open the editor and choose **Launch with Map Companion**.
The Windows release contains everything needed; do not install loose scripts
or use the experimental Workshop package. Source users can run
`python run_desktop_editor.py` from `app/` and use the same launcher button.

## How it works

- A transparent, click-through minimap stays in the upper-left corner.
- `M` opens or closes the external full 2D map; `Esc` closes it.
- `L` and the stock Logbook are untouched.
- Player position and facing are read from a small live player/camera record in
  the running game process. The tracker opens the process read-only and never
  writes to it.
- The tracker finds the record again after every game restart; no fixed memory
  address is stored.
- The active save is identified from Scrap Mechanic's log and save metadata.
- Save data is parsed only from a private, validated snapshot outside the game
  folder, so the game database is never locked or edited.
- Generated map images are stored outside the game saves and keyed to the
  save's real seed and terrain layout.
- The sharp 3D-to-2D map cache is reused after it has been generated.

If live process tracking is temporarily unavailable, the UI labels the
position as a save snapshot instead of pretending it is live.

## Controls

- `M`: open or close the full map.
- `Esc`: close the full map.
- Full map: left-drag to pan; wheel or `+`/`-` to zoom; **Fit world** to reset.

## Maintenance tools

The other files in this folder are implementation, validation, and recovery
tools. They are not the public installation workflow. `ScrapMechanicMap/`
and the Workshop builders are experimental Custom Game tooling retained for
research and regression tests, not a mod required by the external companion.

- `Install Map Companion.cmd` removes obsolete prototypes and validates the
  external companion while the game is closed.
- `Start Map Companion after Scrap Mechanic.cmd` only attaches to an already
  running game.
- `Uninstall Map Companion.cmd` removes obsolete integrations and archives
  their files. It does not touch save databases.
