# Workshop and official Mod Tool feasibility

## Result

The live map can be implemented with Scrap Mechanic's official Lua and GUI APIs
as a **Custom Game**. It cannot be installed as a normal Workshop add-on that
injects executable UI into an arbitrary existing vanilla Survival world.

This distinction matters for the public release:

- The external Map Companion works with existing vanilla Survival saves and is
  therefore the supported path in the editor.
- A Workshop package can provide an integrated minimap only by owning the
  Custom Game's `Game` and `Player` scripts.
- Moving an existing vanilla save into a Custom Game is not a safe or supported
  automatic conversion. Script classes, storage channels, content IDs, and
  future game updates can differ.
- Per-save generated map images cannot be shipped as one static Workshop item.
  They must be produced for the running world or supplied by an external tool.

## What was verified locally

The public Steam installation was tested with:

- Scrap Mechanic app ID `387990`;
- Scrap Mechanic Mod Tool app ID `588870`;
- the installed `Survival Custom Game` template;
- the official GUI, JSON, JSON-GUI, and storage APIs;
- a separate Steam library for the Mod Tool and another for the game.

The repository contains an experimental Custom Game source package under
`companion_mod/ScrapMechanicMap`. It is useful as a development reference, but
it is deliberately not advertised as the supported minimap for existing
Survival saves. The public editor launches the game normally and uses its
external, read-only overlay instead.

## Requirements for a future Workshop edition

Before publishing a Workshop Custom Game, it should pass all of the following:

1. A newly generated Custom Game reaches normal gameplay without delaying the
   opening sequence.
2. The HUD and map survive death, respawn, joining, leaving, and host migration.
3. Every world seed derives its own map; no cached development seed is bundled.
4. The package uses only `$CONTENT_DATA`, `$GAME_DATA`, and `$SURVIVAL_DATA`
   paths accepted by the Mod Tool.
5. Existing vanilla saves are never modified or presented as compatible.
6. The Workshop description clearly identifies the project as unofficial.

## Official references

- [Scrap Mechanic GUI API](https://scrapmechanic.com/api/namespace_Game_sm_gui.html)
- [Scrap Mechanic JSON API](https://scrapmechanic.com/api/namespace_Game_sm_json.html)
- [Scrap Mechanic JSON GUI API](https://scrapmechanic.com/api/namespace_Game_sm_jsonGui.html)
- [Scrap Mechanic storage API](https://scrapmechanic.com/api/namespace_Game_sm_storage.html)
- [Official Scrap Mechanic announcements](https://steamcommunity.com/app/387990/announcements/)
