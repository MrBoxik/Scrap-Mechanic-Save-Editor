# Survival Map Companion — Workshop Custom Game

This package is the Workshop-safe companion to Scrap Mechanic Save Editor. It is a proper **Custom Game**, not a normal-Survival injection and not an external overlay.

## What it does

- Inherits the complete vanilla Survival game.
- Uses the random seed selected by Scrap Mechanic for each new Custom Game.
- Adds a Map Tool without replacing Logbook or another vanilla key.
- Shows a lightweight exploration minimap while playing.
- Saves discovered cells and points of interest inside that world/player save.
- Opens a full exploration map with three zoom levels.
- Works without the editor, a launcher, telemetry files, or machine-specific paths.

## Controls

Select **Map Tool** in the hotbar:

- Primary use: open or close the full map.
- Secondary use: cycle 1x, 2x, and 4x range.
- Escape: close the full map.

The Scrap Mechanic Workshop API does not expose arbitrary global key registration to a Player script. Using a real tool is deliberate: it avoids taking over `L`, changing vanilla files, or relying on unsupported keyboard injection.

## Build and local test

Run:

```powershell
python companion_mod/generate_workshop_assets.py
python companion_mod/build_workshop_mod.py --install
```

Then open Scrap Mechanic, choose **Custom Game**, select **Survival Map Companion**, and create a new world. For Mod Tool validation, point the official Scrap Mechanic Mod Tool at the installed development copy.

## Workshop publishing

The ready-to-import folder is `companion_mod/dist/SurvivalMapCompanion`; the portable archive is `companion_mod/dist/SurvivalMapCompanion-Workshop-v2.zip`. `companion_mod/dist/SHA256SUMS.txt` contains the archive checksum. Publishing is intentionally not automated because submitting or updating a Workshop item is an external public action.

## Technical limit

Official Workshop Lua cannot capture and serialize a complete high-resolution render of arbitrary generated terrain at runtime. This version therefore uses a performant, seed-safe exploration grid and discovered POIs. It never embeds a map from a different save.
