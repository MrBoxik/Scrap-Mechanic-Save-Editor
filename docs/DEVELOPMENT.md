# Development

## Repository layout

```text
app/
  scrap_editor_app/   Desktop application and web interface
  companion_mod/      External companion, tests, and experimental Workshop tools
  assets/             Bundled catalogue and terrain atlases
  tools/              Asset tooling and release checks
  test_*.py           Save-format and release regression tests
  build_*.py          Windows packaging scripts
docs/                 Guides, policies, release history, and validation reports
.github/              CI, issue templates, and project metadata
```

Generated maps, local saves, caches, extracted assets, build intermediates, and
release binaries are not source files and must not be committed. Historical
release checksums are in `docs/SHA256SUMS.txt`; downloads are in GitHub Releases.

## Run from source

Use Python 3.11 or newer. From the repository root:

```powershell
cd app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-desktop.txt
python run_desktop_editor.py
```

The supported UI is the embedded desktop application. The older Tkinter UI is
retained for diagnostics and shared save-format tooling, not a separate public
download. Read-only inspection is available with:

```powershell
python scrap_mechanic_save_editor.py --inspect "C:\path\to\save.db"
```

## Check changes

Run these commands from `app/`, with Scrap Mechanic closed:

```powershell
python tools/release_check.py
python -m unittest discover -v
node tools/test_map_navigation.cjs
node tools/test_map_visibility.cjs
```

Tests requiring installed game assets or local saves skip when those fixtures
are absent. Never add a real save to the repository to make a test pass.

Browser regression checks additionally require Node.js, Playwright, and Chrome:

```powershell
npm install --no-save --package-lock=false playwright
$env:MAP_TEST_OUTPUT = "..\artifacts\map-ui"
node tools/check_map_ui.cjs
```

## Build a portable Windows release

From `app/`:

```powershell
python -m pip install pyinstaller
python build_release.py
```

Output goes to `releases/<version>/` at the repository root: a portable folder,
ZIP containing the EXE and its runtime, and SHA-256 checksums. Keep `_internal`
beside the executable. The optional `build_desktop.py --onefile` diagnostic
format has slower cold startup and is not the public release format.

The published 4.22.1 binary is the already-tested portable build from before the
source-tree reorganization. No gameplay behavior changed for this cleanup.
For its tested scope and limitations, see [4.22.1 validation](RELEASE_4.22.1_VALIDATION.md).
