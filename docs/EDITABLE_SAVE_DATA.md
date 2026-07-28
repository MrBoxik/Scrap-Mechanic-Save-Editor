# Editable save data

This document records what version 4.4.0 can safely edit across the current
Scrap Mechanic world modes.

## Editable now

| Area | Fields or operations | Safety approach |
| --- | --- | --- |
| Inventory | UUID and quantity in the player backpack/hotbar and validated containers | Slot structure and bounds are checked before writing; unknown slots are preserved |
| Player state | Health, maximum health, oxygen, maximum oxygen | Values are bounded and only the selected player's serialized state row is replaced |
| Raids | Cancel all currently scheduled/active raids | The raid manager's raid collection is emptied while its other state is preserved |
| Progression | Seed tier, encrypted vault total, warehouse console unlocked, beacon crafted | Existing progression keys are updated in place |
| Quests | Mark one or all 46 installed quests complete | Completed list, active object, ScriptData, and tracking references are updated together |
| Craftbot | Unlock one or all 528 installed recipe outputs | UUID keys are added to the RecipeManager's `unlockedRecipes` map |
| Logbook | Unlock one or all 27 installed log entries | UUIDs are added to the persistent ordered log array |
| World time | Day, hour, day-cycle running/frozen | Existing `timeOfDay` and `timeProgress` fields are updated |
| Game settings | Difficulty plus existing multiplayer and physics quality values | Existing typed values are retained unless explicitly changed |
| Advanced game data | Flags, world seed, and game tick | Locked behind advanced mode because extreme values can break a world |
| Clothing profile | Individual or all 253 named customizations | The global per-user file is checksum-verified and backed up before atomic replacement |

## Mode support

| Mode | Persistent location | Supported operations |
| --- | --- | --- |
| Survival | `User_<SteamId>\Save\Survival\*.db` | All validated inventory, player/world, progression, unlock, settings, and terrain tools listed above |
| Creative / Sandbox | `User_<SteamId>\Save\*.db` | Player inventory/hotbar, validated containers, session settings, Game fields, and recognized state channels such as time when present |
| Custom Game | `User_<SteamId>\Save\Custom\*.db` | The same schema-driven inventory, container, settings, and recognized state operations that the individual database exposes |
| Challenge Mode | `User_<SteamId>\Progress\<pack UUID>.json` | External versioned backup and safe restore of engine-created progress/saved-creation records |

Challenge progress is not a normal world database. The game engine owns its
checksums and completion-time state, so the editor does not invent challenge
completion entries or apply Survival inventory structures to it.

Player state also exposes the Steam ID, inventory container ID, carry container
ID, consciousness, revival-item state, and perk count for inspection. Those
identity/link fields are deliberately read-only.

## Read-only now

- Quest stage internals, quest reset, and live quest side effects. Offline
  completion cannot replay world events, physical rewards, achievements,
  dialogs, or automatic activation of the next quest.
- Player beacons, meaningful generated locations, stations, warehouses,
  packing stations, labs/dungeons, and player position on the world map.
- World/container identity and physical location where the format can identify
  them.
- Individual units, harvestables, portals, rigid bodies, joints, and tools used
  for diagnostics and map reconstruction.

## Additional systems discovered

The installed Survival scripts define persistent channels for beacons,
locations, quests, raids, recipes, dialogs, logs, progression, trader state,
and world state. These give useful future directions:

- bed and respawn selection;
- trader unlock/progression state;
- dialog state and queued conversations;
- individual quest repair or reset with stage reconstruction;
- weather forecast queues;
- player perks after their current schema is mapped;
- selected unit or harvestable state with strong identity checks.

The current Survival player script stores health and oxygen but not persistent
hunger or thirst values. The editor therefore does not invent hunger/thirst
controls that would either do nothing or overwrite unrelated binary data.

## Format guarantees

Script state is stored as compressed binary Lua data inside SQLite `ScriptData`
rows. The decoder preserves numeric and string keys, unknown table fields, array
offsets, and supported numeric types. Saving replaces only records marked dirty
and then runs SQLite integrity checks. A valid SQLite database can still contain
values the game does not accept, so backups remain essential.

The desktop application stores automatic, manual, edited-copy, and pre-restore
versions under `%LOCALAPPDATA%\ScrapMechanicSaveEditor\Backups`. Restore is
atomic and first makes another recoverable version of the file being replaced.
