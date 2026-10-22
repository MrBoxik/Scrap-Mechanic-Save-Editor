# Editable save data

This document records what version 4.21.1 can safely edit across the current
Scrap Mechanic world modes.

## Editable now

| Area | Fields or operations | Safety approach |
| --- | --- | --- |
| Inventory | UUID and quantity in the player backpack/hotbar and validated containers | Slot structure and bounds are checked before writing; unknown slots are preserved |
| Player state | Health, maximum health, oxygen, maximum oxygen | Values are bounded and only the selected player's serialized state row is replaced |
| Player perks | Bonus health, hammer speed, fall protection, and high jump | Only the four installed perk IDs are accepted; bonus health adjusts current/max HP with the game’s +25 rule |
| Player recovery | Exact position, bed/death-bag teleport, placed-bed reset, and stuck-state repair | Coordinates are bounded; saved respawn/bag positions are reused instead of fabricated |
| Host recovery | Clear death markers and toggle creation aggro | Existing UnitManager keys are changed in place |
| Player beacons | Choose one of the game's 24 icons and eight exact colors | The global beacon manager and linked physical beacon storage must both be verified and updated |
| Weather service | Edit any saved quarter-day tile, add future days, or skip the current forecast day | Conditions are limited to the six installed definitions; queue lengths and padding mirror `WeatherManager.lua` |
| Tornado service | Clear saved tornadoes, visited desert-cell cooldowns, and reset the next internal ID | The channel is returned to the exact empty structure created by `TornadoManager.server_onCreate` |
| Raids | Cancel all currently scheduled/active raids | The raid manager's raid collection is emptied while its other state is preserved |
| Progression | Seed tier, encrypted vault total, warehouse console unlocked, beacon crafted | Existing progression keys are updated in place |
| Quests | Mark one or all 46 installed quests complete | Completed list, active object, ScriptData, and tracking references are updated together |
| Craftbot | Unlock one or all 528 installed recipe outputs | UUID keys are added to the RecipeManager's `unlockedRecipes` map |
| Logbook | Unlock one or all 27 installed log entries | UUIDs are added to the persistent ordered log array |
| World time | Day, hour, day-cycle running/frozen | Existing `timeOfDay` and `timeProgress` fields are updated |
| Game settings | Difficulty plus existing multiplayer and physics quality values | Existing typed values are retained unless explicitly changed |
| Advanced game data | Flags, world seed, and game tick | Locked behind advanced mode because extreme values can break a world |
| Clothing profile | Lock/unlock individual or all 253 named customizations | The global per-user file is checksum-verified and backed up before atomic replacement |
| Character profile | Gender, nine appearance/equipment UUIDs, and palette indices | Exact v2 profile length, slot count, UUID categories, and unlocked clothing are validated |
| Local Blueprints | Rename/describe, duplicate, ZIP archive, and ZIP import | Imports require valid blueprint/description JSON, reject unsafe archive paths, and always receive a new local UUID |

## Mode support

| Mode | Persistent location | Supported operations |
| --- | --- | --- |
| Survival | `User_<SteamId>\Save\Survival\*.db` | All validated inventory, player/world, progression, unlock, settings, and terrain tools listed above |
| Creative / Sandbox | `User_<SteamId>\Save\*.db` | Player inventory/hotbar, validated containers, session settings, Game fields, and recognized state channels such as time when present |
| Custom Game | `User_<SteamId>\Save\Custom\*.db` | The same schema-driven inventory, container, settings, and recognized state operations that the individual database exposes |

Player state also exposes the Steam ID, inventory container ID, carry container
ID, consciousness, and revival-item state. Identity and container-link fields
remain deliberately read-only.

## Read-only now

- Quest stage internals, quest reset, and live quest side effects. Offline
  completion cannot replay world events, physical rewards, achievements,
  dialogs, or automatic activation of the next quest.
- Meaningful generated locations, stations, warehouses, packing stations,
  labs/dungeons, and player position are visible on the world map.
- Registered world names and references, world/container identity, and physical
  location where the format can identify them.
- Elevator connections and queues, trader quest state, doused/activated named
  fires, underground service state, and dungeon internals.
- Individual units, protected/scripted harvestables, portals, rigid bodies,
  joints, and tools used for diagnostics and map reconstruction. Arbitrary new
  harvestable spawning remains read-only because the engine owns global IDs
  and generated-terrain identity.

## Additional systems discovered

The installed Survival scripts define persistent channels for beacons,
locations, quests, raids, recipes, dialogs, logs, progression, trader state,
and world state. These give useful future directions:

- trader unlock/progression state;
- dialog state and queued conversations;
- individual quest repair or reset with stage reconstruction;
- guarded elevator recovery after all linked endpoints can be validated;
- selected unit or harvestable state with strong identity checks.
- player-beacon creation/removal, which is not exposed because deleting only
  manager state lets the physical beacon register itself again.

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
