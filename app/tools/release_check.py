from __future__ import annotations

"""Fast, deterministic checks that must pass before publishing a release."""

import importlib
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REQUIRED_FILES = (
    "windows_version_info.txt",
    "assets/item-icon-atlas.png",
    "assets/item-icon-atlas.json",
    "assets/terrain-cell-atlas.png",
    "assets/terrain-cell-atlas.json",
    "assets/terrain-tile-names.json",
    "scrap_editor_app/web/index.html",
    "scrap_editor_app/web/app.js",
    "scrap_editor_app/web/consumer.css",
    "scrap_editor_app/web/map3d.js",
    "scrap_editor_app/web/companion_minimap.html",
    "scrap_editor_app/web/companion_overlay.html",
    "scrap_editor_app/web/companion_preview.js",
    "scrap_editor_app/web/companion_waypoints.js",
    "scrap_editor_app/web/game-ui/map-preview.png",
    "scrap_editor_app/game_status.py",
)

RUNTIME_SUFFIXES = {".py", ".js", ".html", ".css", ".ps1", ".cmd", ".json"}
RUNTIME_ROOTS = (ROOT / "scrap_editor_app", ROOT / "companion_mod")
PERSONAL_PATH_PATTERNS = (
    re.compile(r"(?i)[a-z]:[\\/]users[\\/][^%<{\\/]+[\\/]"),
    re.compile(r"(?i)user_\d{10,}"),
)


def fail(message: str) -> None:
    raise SystemExit(f"release check failed: {message}")


def check_required_files() -> None:
    missing = [relative for relative in REQUIRED_FILES if not (ROOT / relative).is_file()]
    missing.extend(
        relative for relative in (
            "LICENSE", "README.md", "docs/SUPPORT.md", "docs/SECURITY.md",
            "docs/PRIVACY.md", "docs/THIRD_PARTY_NOTICES.md", "docs/USER_GUIDE.md",
        ) if not (REPOSITORY / relative).is_file()
    )
    if missing:
        fail("missing required files: " + ", ".join(missing))


def check_versions() -> str:
    package = importlib.import_module("scrap_editor_app")
    editor = importlib.import_module("scrap_mechanic_save_editor")
    package_version = str(package.__version__)
    editor_version = str(editor.APP_VERSION)
    if package_version != editor_version:
        fail(f"version drift: package={package_version}, editor={editor_version}")
    if not (REPOSITORY / "docs/RELEASE_NOTES.md").read_text(encoding="utf-8").startswith(
        f"# Scrap Mechanic Save Editor {package_version}"
    ):
        fail("RELEASE_NOTES.md does not start with the current version")
    metadata = (ROOT / "windows_version_info.txt").read_text(encoding="utf-8")
    numeric = tuple(int(part) for part in package_version.split(".")) + (0,)
    for field in ("filevers", "prodvers"):
        match = re.search(rf"{field}=\(([^)]+)\)", metadata)
        if not match or tuple(int(part.strip()) for part in match[1].split(",")) != numeric:
            fail(f"Windows numeric {field} does not match {package_version}")
    for field in ("FileVersion", "ProductVersion"):
        if f"StringStruct('{field}', '{package_version}')" not in metadata:
            fail(f"Windows {field} does not match {package_version}")
    return package_version


def check_runtime_paths() -> None:
    offenders: list[str] = []
    for runtime_root in RUNTIME_ROOTS:
        for path in runtime_root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in RUNTIME_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(pattern.search(text) for pattern in PERSONAL_PATH_PATTERNS):
                offenders.append(str(path.relative_to(ROOT)))
    if offenders:
        fail("personal absolute path or Steam ID in runtime source: " + ", ".join(offenders))


def check_packaging() -> None:
    build = importlib.import_module("build_desktop")
    command = build.build_command(onefile=False)
    command_text = "\n".join(command)
    for required in (
        "companion_mod.companion_overlay",
        "companion_mod.live_process_tracker",
        "scrap_editor_app/web",
        "windows_version_info.txt",
    ):
        if required not in command_text:
            fail(f"packaging command omitted {required}")
    if "--onefile" in command:
        fail("public packaging unexpectedly uses the slow one-file mode")
    importlib.import_module("scrap_editor_app.single_instance")
    importlib.import_module("companion_mod.companion_overlay")


def main() -> int:
    check_required_files()
    version = check_versions()
    check_runtime_paths()
    check_packaging()
    print(f"release check passed for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
