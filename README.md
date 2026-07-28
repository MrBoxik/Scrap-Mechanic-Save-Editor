# Scrap Mechanic Save Editor

A polished offline desktop editor for Scrap Mechanic Survival saves. It combines a Python save engine with an HTML/CSS interface embedded directly in a Windows application—there is no website, localhost server, browser window, account, or save upload.

> [!IMPORTANT]
> Close Scrap Mechanic before editing and keep a backup. The editor creates a timestamped `.bak` before overwriting a save, but experimenting on a copy is still the safest workflow.

## Download

Download the latest `ScrapMechanicSaveEditor.exe` from [Releases](../../releases/latest). The packaged application does not require Python.

## Highlights

- Automatically discovers Survival saves for every local Scrap Mechanic user.
- Edits player health, maximum health, oxygen, and maximum oxygen.
- Shows player Steam ID, inventory container, carry container, consciousness, revival item, and perks.
- Cancels active raids stored by the Survival raid manager.
- Edits progression values including seed tier, encrypted vault total, warehouse console unlock, and beacon crafting state.
- Lists all 46 quests from the installed game data and marks individual quests—or every quest—complete.
- Removes completed active quest objects and tracked references while preserving unrelated quest-manager data.
- Unlocks all 528 installed Craftbot recipe outputs individually or together, with game names and icons.
- Unlocks all 27 logbook entries.
- Reads and safely updates the Steam user's global clothing profile with 253 named customizations and its required checksum.
- Edits world day/time and can pause or resume the day cycle.
- Edits the 40-slot player backpack/hotbar and every other validated container.
- Loads 1,352 game catalogue entries with names, categories, stack limits, descriptions, and high-resolution game icons.
- Supports search, filters, drag and drop, right-click replacement, quantities, and background catalogue loading.
- Renders the real generated 128×96 Survival overworld with roads, terrain, lakes, buildings, destinations, player position, and player beacons.
- Provides map search, destination filters, hover details, wheel zoom, buttons, fit-to-view, and drag panning.
- Edits difficulty and existing multiplayer/physics settings; advanced mode also exposes seed, flags, and game tick.
- Validates SQLite integrity before loading and after saving.
- Saves a copy or overwrites with an automatic timestamped backup.
- Creates a separate timestamped backup before writing the global clothing `unlock` profile.

The full breakdown of known save systems and intentionally read-only fields is in [Editable save data](docs/EDITABLE_SAVE_DATA.md).

## Safe workflow

1. Exit Scrap Mechanic completely.
2. Open the editor and select a Survival save.
3. Make the changes you want.
4. Use **Save a Copy** for experimental changes.
5. Test the edited save in-game.
6. Use **Save to Game** only after confirming it works.

## Run from source

Python 3.11 or newer is recommended.

```powershell
python -m pip install -r requirements-desktop.txt
python .\run_desktop_editor.py
```

The legacy Tkinter interface remains available for diagnostics:

```powershell
python .\scrap_mechanic_save_editor.py
```

Read-only command-line inspection:

```powershell
python .\scrap_mechanic_save_editor.py --inspect "C:\path\to\save.db"
```

## Build the Windows app

Install PyInstaller, then build the single-file release:

```powershell
python -m pip install pyinstaller
python .\build_desktop.py --onefile
```

The result is `dist\ScrapMechanicSaveEditor.exe`. A faster-starting folder build can be made by omitting `--onefile`.

## Technical notes

Survival saves are SQLite databases containing both ordinary tables and compressed, binary Lua state. The editor parses only structures it can validate, preserves unknown fields and keys, and updates exact source rows instead of rebuilding the database.

Offline quest completion updates the quest manager's completed list, removes active quest objects and tracked references, and unlocks known schematic rewards. It cannot replay live scripted world events, Steam achievements, physical item rewards, or automatic next-quest activation. Those limitations are shown beside the quest controls.

Clothing is not stored in a world database. It lives in the Steam user's global binary `unlock` profile. The editor verifies the file's Steam-ID-bound CRC-32 checksum and creates a backup before applying selected clothing.

Inventory slots use UUIDs plus metadata and quantity. Existing tool instances may safely have their quantity changed, but the editor will not fabricate a tool instance from a catalogue UUID because tools also require a corresponding `Tool` table record.

The detailed map is reconstructed from the save's generated terrain cell grid and a version-matched top-down atlas. Player-built creations are deliberately not marked: a single loose block is technically a creation, which makes that layer noisy and misleading.

The included atlas and interface imagery are derived from locally installed Scrap Mechanic assets. Scrap Mechanic and all game artwork are property of Axolot Games. This project is unofficial and is not affiliated with or endorsed by Axolot Games.

## Support and feedback

Read [SUPPORT.md](SUPPORT.md) before reporting a problem. Bug reports are most useful when they include the editor version, game version, steps to reproduce, and the log at `%LOCALAPPDATA%\ScrapMechanicSaveEditor\editor.log`. Never post a private save publicly unless you are comfortable sharing everything stored in it.

Discord: user ID `638802769393745950`<br>
Support development: [buymeacoffee.com/mrboxik](https://www.buymeacoffee.com/mrboxik)

## License

Copyright © 2026 MrBoxik. Source code is available under the [Custom Non-Commercial License](LICENSE). Personal and educational use with attribution is allowed; commercial use requires prior written permission. Scrap Mechanic assets remain subject to their owners' rights.
