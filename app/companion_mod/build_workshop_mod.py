from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "ScrapMechanicMap"
DIST = HERE / "dist"
PACKAGE = DIST / "SurvivalMapCompanion"
LOCAL_ID = "9bf45fa0-6dc8-4f21-9eb7-cdb06f4312c0"


def excluded(relative: Path) -> bool:
    lowered = tuple(part.lower() for part in relative.parts)
    if "mapcache" in lowered:
        return True
    if relative.name.lower() in {"survivalintegration.lua", "minimap.gui"}:
        return True
    return False


def validate(package: Path) -> None:
    config = json.loads((package / "config.json").read_text(encoding="utf-8"))
    description = json.loads((package / "description.json").read_text(encoding="utf-8"))
    tool_database = json.loads((package / "Tools" / "Database" / "toolsets.tooldb").read_text(encoding="utf-8"))
    tools = json.loads((package / "Tools" / "Database" / "ToolSets" / "map_companion.toolset").read_text(encoding="utf-8"))
    assert config["baseGameContent"] == "Survival"
    assert description["type"] == "Custom Game"
    assert description["version"] == 1
    assert description["localId"] == LOCAL_ID
    assert tool_database["toolSetList"] == ["$CONTENT_DATA/Tools/Database/ToolSets/map_companion.toolset"]
    assert tools["toolList"][0]["script"]["file"] == "$CONTENT_DATA/Scripts/MapTool.lua"

    game_script = (package / "Scripts" / "Game.lua").read_text(encoding="utf-8")
    assert "function Game.cl_n_onJoined" in game_script
    assert "self.cl.playIntroCinematic = false" in game_script
    assert "function Game.client_onLoadingScreenLifted" in game_script
    assert "SurvivalGame.client_onLoadingScreenLifted( self )" in game_script

    map_runtime = (package / "Scripts" / "MapRuntime.lua").read_text(encoding="utf-8")
    assert '$CONTENT_9bf45fa0-6dc8-4f21-9eb7-cdb06f4312c0/Gui/MapCompanion/' in map_runtime
    assert '$CONTENT_DATA/Gui/MapCompanion/' not in map_runtime
    assert '"MapCompanion/cell_' not in map_runtime
    # sm.jsonGui becomes active when render() is called; unlike sm.gui, its
    # userdata has no open() method.
    assert ":open()" not in map_runtime

    forbidden = ("live_state.json", "map_config.json", "survivalintegration")
    absolute_path = re.compile(r"(?:[A-Za-z]:\\|/Users/|\\Users\\)")
    for file in package.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in {".lua", ".json", ".md", ".txt"}:
            continue
        text = file.read_text(encoding="utf-8")
        lowered = text.lower()
        if any(token in lowered for token in forbidden):
            raise RuntimeError(f"Workshop package contains obsolete integration code: {file}")
        if absolute_path.search(text):
            raise RuntimeError(f"Workshop package contains a machine-specific path: {file}")


def build() -> Path:
    if not (SOURCE / "description.json").is_file():
        raise FileNotFoundError(f"Missing source package: {SOURCE}")
    try:
        from .generate_workshop_assets import main as generate_assets
    except ImportError:
        from generate_workshop_assets import main as generate_assets

    generate_assets()
    validate(SOURCE)

    if PACKAGE.exists():
        shutil.rmtree(PACKAGE)
    PACKAGE.mkdir(parents=True)
    for source in SOURCE.rglob("*"):
        relative = source.relative_to(SOURCE)
        if not source.is_file() or excluded(relative):
            continue
        target = PACKAGE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    validate(PACKAGE)
    archive = Path(shutil.make_archive(str(DIST / "SurvivalMapCompanion-Workshop-v2"), "zip", PACKAGE))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (DIST / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive


def find_user_root() -> Path:
    users = Path.home() / "AppData" / "Roaming" / "Axolot Games" / "Scrap Mechanic" / "User"
    candidates = sorted((path for path in users.glob("User_*") if path.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError("No Scrap Mechanic Steam user folder was found. Start the game once first.")
    return candidates[0]


def install(package: Path) -> Path:
    target = find_user_root() / "Mods" / LOCAL_ID
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(package, target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the portable Survival Map Companion Workshop package.")
    parser.add_argument("--install", action="store_true", help="Also install a development copy for the current Steam user.")
    args = parser.parse_args()
    archive = build()
    print(f"Validated Workshop package: {archive}")
    if args.install:
        print(f"Installed development copy: {install(PACKAGE)}")


if __name__ == "__main__":
    main()
