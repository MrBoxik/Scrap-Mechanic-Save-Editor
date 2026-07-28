# Scrap Mechanic Save Editor 4.2.0

This release expands the editor beyond inventory and world mapping with safe,
version-aware player and Survival progression controls.

## New

- Player & World page with editable health, maximum health, oxygen, and maximum oxygen.
- Multi-player-state selector showing Steam ID and linked inventory/carry containers.
- Active raid viewer and **Cancel all raids** action.
- Editable seed tier, encrypted vault total, warehouse console unlock, and beacon crafting state.
- Read-only active/completed quest lists to expose progress without creating inconsistent quest records.
- Player beacons shown as useful world-map landmarks.
- Safer binary Lua serialization that preserves unknown fields and supports double values.

## Existing highlights

- Full 40-slot backpack/hotbar editing and validated world containers.
- 1,352 searchable game catalogue entries with high-resolution icons.
- Detailed zoomable generated world map with meaningful destination filters.
- Difficulty and advanced world settings.
- Save-copy workflow, automatic timestamped backups, and SQLite integrity checks.

Close Scrap Mechanic before editing. Use **Save a Copy** for experimental changes.
