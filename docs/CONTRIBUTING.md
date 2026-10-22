# Contributing

Thanks for helping improve Scrap Mechanic Save Editor.

Before changing save-writing code, create a disposable Survival world and keep
an untouched copy. New writable fields should have evidence from the installed
game scripts, preserve unknown serialized values, validate their bounds, and
include a round-trip test proving unrelated database rows remain unchanged.

For code changes, run commands from `app/`:

1. Open an issue describing the save format or UI problem.
2. Keep changes focused and avoid committing saves, extracted game files, logs,
   build output, or personally identifying data.
3. Run `python -m unittest discover -v` with Scrap Mechanic closed.
4. Run `node --check` against changed JavaScript files in
   `scrap_editor_app\web`.
5. Run `python tools\release_check.py` before preparing a release.
6. Test **Save a Copy** and load that copy in Scrap Mechanic when write behavior
   changes.

By contributing, you agree that your contribution is distributed under this
repository's Custom Non-Commercial License.
