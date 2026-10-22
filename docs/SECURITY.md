# Security policy

## Supported version

Security fixes are made for the newest published release. Older executables
should be replaced rather than kept beside the latest version.

## Reporting a security issue

Do not publish a save database, Steam profile file, access token, or private
path in a public issue. Contact the maintainer privately using the support
details in [SUPPORT.md](SUPPORT.md) and include only the minimum reproduction
information required.

Useful reports identify the affected version, the operation performed, and
whether the issue can overwrite a file outside the selected save or the
editor's own data directory.

## Safety boundaries

- The editor is offline and does not upload saves.
- Save writes require an explicit action and create an external backup first.
- Restores archive the current target before replacing it.
- Game paths are discovered from Steam manifests and validated by expected game
  files; environment overrides are validated the same way.
- The Map Companion is read-only. It uses private SQLite snapshots and does not
  write to the active game database.
- Generated files, logs, caches, settings, and backups are kept below
  `%LOCALAPPDATA%\ScrapMechanicSaveEditor` and
  `%LOCALAPPDATA%\ScrapMechanicMapCompanion`.

Always download releases from this repository and verify the SHA-256 checksum
published with the release.
