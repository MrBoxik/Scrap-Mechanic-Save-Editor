from __future__ import annotations

import argparse
import importlib.util
import os
import platform
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
APP_NAME = "ScrapMechanicSaveEditor"


def data_arg(source: Path, target: str) -> str:
    return f"{source}{';' if platform.system() == 'Windows' else ':'}{target}"


def build_command(
    onefile: bool = False,
    dist_dir: Path | None = None,
    work_dir: Path | None = None,
) -> list[str]:
    assets = ROOT / "assets"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", APP_NAME,
        "--version-file", str(ROOT / "windows_version_info.txt"),
        "--add-data", data_arg(ROOT / "scrap_editor_app" / "web", "scrap_editor_app/web"),
        "--add-data", data_arg(assets / "item-icon-atlas.png", "assets"),
        "--add-data", data_arg(assets / "item-icon-atlas.json", "assets"),
        "--add-data", data_arg(assets / "terrain-cell-atlas.png", "assets"),
        "--add-data", data_arg(assets / "terrain-cell-atlas.json", "assets"),
        "--add-data", data_arg(assets / "terrain-tile-names.json", "assets"),
        "--hidden-import", "companion_mod.companion_overlay",
        "--hidden-import", "companion_mod.live_process_tracker",
        "--hidden-import", "companion_mod.companion_settings",
        "--collect-submodules", "webview", "--collect-data", "webview",
        "--hidden-import", "tkinter.ttk", "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox", "--hidden-import", "tkinter.simpledialog",
    ]
    if dist_dir is not None:
        command.extend(["--distpath", str(dist_dir)])
    if work_dir is not None:
        command.extend(["--workpath", str(work_dir)])
    for package in ("pythonnet", "clr_loader", "proxy_tools"):
        if importlib.util.find_spec(package):
            command.extend(["--collect-submodules", package])
    icon = ROOT / "app_icon.ico"
    if icon.is_file():
        command.extend(["--icon", str(icon)])
    if onefile:
        command.append("--onefile")
    command.append(str(ROOT / "run_desktop_editor.py"))
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the serverless Scrap Mechanic desktop editor.")
    parser.add_argument("--onefile", action="store_true", help="Build the slower legacy one-file package instead of the release-ready folder package.")
    parser.add_argument("--dist-dir", type=Path, help="Write the packaged application to this directory.")
    parser.add_argument("--work-dir", type=Path, help="Use this directory for temporary PyInstaller build files.")
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args()
    command = build_command(args.onefile, args.dist_dir, args.work_dir)
    if args.print:
        print(subprocess.list2cmdline(command))
        return 0
    env = os.environ.copy()
    try:
        from scrap_editor_app.steam_paths import find_game_root

        game_root = find_game_root()
        release = game_root / "Release" if game_root else None
        if release and (release / "assimp.dll").is_file():
            # PyInstaller imports pyassimp while inspecting its binary
            # dependencies.  The application intentionally uses the DLL from
            # the user's installed game, so make that same DLL discoverable
            # during packaging without copying it into the release.
            env["PATH"] = f"{release}{os.pathsep}{env.get('PATH', '')}"
    except Exception:
        # Ordinary builds without Scrap Mechanic can still package all
        # non-3D features; PyInstaller will report pyassimp itself if needed.
        pass
    return subprocess.call(command, cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
