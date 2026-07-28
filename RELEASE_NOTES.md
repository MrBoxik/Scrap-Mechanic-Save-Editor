# Scrap Mechanic Save Editor 4.3.0

Version 4.3 adds a complete progression and unlock-management workspace based
on the schemas in Scrap Mechanic 1.0.0.867.

## New

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

Close Scrap Mechanic before editing. Use **Save a Copy** for experimental world
changes. Clothing is global across worlds and has its own **Apply to profile**
button with an automatic timestamped backup.
