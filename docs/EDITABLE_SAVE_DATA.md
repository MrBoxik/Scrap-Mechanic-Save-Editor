# Editable save data

This document records what version 4.2.0 can safely edit and what has been found
in the current Scrap Mechanic Survival save format.

## Editable now

| Area | Fields or operations | Safety approach |
| --- | --- | --- |
| Inventory | UUID and quantity in the player backpack/hotbar and validated containers | Slot structure and bounds are checked before writing; unknown slots are preserved |
| Player state | Health, maximum health, oxygen, maximum oxygen | Values are bounded and only the selected player's serialized state row is replaced |
| Raids | Cancel all currently scheduled/active raids | The raid manager's raid collection is emptied while its other state is preserved |
| Progression | Seed tier, encrypted vault total, warehouse console unlocked, beacon crafted | Existing progression keys are updated in place |
| Game settings | Difficulty plus existing multiplayer and physics quality values | Existing typed values are retained unless explicitly changed |
| Advanced game data | Flags, world seed, and game tick | Locked behind advanced mode because extreme values can break a world |

Player state also exposes the Steam ID, inventory container ID, carry container
ID, consciousness, revival-item state, and perk count for inspection. Those
identity/link fields are deliberately read-only.

## Read-only now

- Active and completed quests. Quest completion is spread across manager,
  entity, and progression records; changing only one can corrupt progression.
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
- recipe and crafting unlocks;
- trader unlock/progression state;
- logbook and dialog state;
- individual quest repair or reset with cross-record validation;
- world time and weather where confirmed by the matching game version;
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
