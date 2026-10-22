# Privacy

Scrap Mechanic Save Editor is an offline desktop application. It has no account,
analytics service, advertising SDK, telemetry upload, or localhost web server.

The application reads only local data needed for the selected feature:

- Scrap Mechanic world saves and user profiles;
- installed game files used for names, icons, meshes, and map rendering;
- Steam library manifests used to locate the game and optional Mod Tool;
- the running game process for read-only live Map Companion coordinates.

It writes editor settings, logs, backups, and map caches below
`%LOCALAPPDATA%\ScrapMechanicSaveEditor`. The Map Companion keeps its settings,
exploration, generated map, and private runtime snapshots below
`%LOCALAPPDATA%\ScrapMechanicMapCompanion`. Backups are intentionally outside
the game's save folders so Scrap Mechanic does not list them as playable worlds.

World saves can contain Steam IDs, names, positions, inventory, creations, and
other private gameplay data. Review a copy before sharing it in an issue.
