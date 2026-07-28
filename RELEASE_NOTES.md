# Scrap Mechanic Save Editor 4.4.0

Version 4.4 adds external versioned backups and capability-aware support for
the other persistent Scrap Mechanic world modes.

## Backups and restore

- Stores backups under `%LOCALAPPDATA%\ScrapMechanicSaveEditor\Backups`, never inside a Scrap Mechanic save folder.
- Automatically archives the untouched database before **Save to game**.
- Adds **Create backup** for unlimited manual snapshots.
- Changes **Save a Copy** into **Archive edited copy**, also outside the game save list.
- Adds a backup manager with timestamps, source paths, type, size, open-folder, refresh, and restore controls.
- Creates a fresh pre-restore safety backup before replacing a world or Challenge progress file.
- Validates SQLite archives and refuses to copy or restore a database while an active SQLite journal exists.

## World modes

- Discovers Survival worlds under `Save\Survival`.
- Discovers Creative/Sandbox worlds directly under `Save`.
- Discovers Custom Game worlds under `Save\Custom`.
- Detects capabilities from the loaded database and disables unavailable mode-specific controls.
- Supports the real 30-slot Creative/Sandbox inventory and its validated containers/settings.
- Keeps Survival quests, raids, progression, recipes, logs, and terrain controls limited to saves that actually contain them.
- Detects Challenge Mode per-pack JSON under `Progress`, displays its saved-record count, and supports external backup/restore.

## Existing progression tools

- Lists all 46 quests with their real localized titles, type, ID, status, and known rewards.
- Marks one quest or every installed quest complete.
- Safely removes active quest ScriptableObjects and tracking references when completing them.
- Unlocks known schematic rewards while completing quests.
- Lists all 528 Craftbot recipe outputs with item names, groups, UUIDs, and high-resolution icons.
- Unlocks individual Craftbot schematics or all recipes at once.
- Lists and unlocks all 27 logbook entries.
- Lists 253 named clothing customizations from the installed game data.
- Reads, verifies, edits, and backs up the Steam user's global binary clothing `unlock` profile.
- Edits the current world day and time and can freeze or resume the day cycle.
- Loads the large unlock catalogues lazily and renders them in background chunks to keep startup responsive.

## Quest completion limitations

An offline editor cannot execute live game scripts. Quest completion updates the
saved quest manager, removes active quest objects, clears tracked references,
and unlocks known schematic rewards. It does not replay physical item rewards,
world events, achievements, dialogs, or automatic next-quest activation.

Close Scrap Mechanic before editing. Clothing is global across worlds and has
its own **Apply to profile** button and checksum-aware backup behavior.
