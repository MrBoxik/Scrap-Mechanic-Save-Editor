from __future__ import annotations

import argparse
import importlib.util
import platform
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
APP_NAME = "ScrapMechanicSaveEditor"


def data_arg(source: Path, target: str) -> str:
    return f"{source}{';' if platform.system() == 'Windows' else ':'}{target}"


def build_command(onefile: bool = False) -> list[str]:
    assets = ROOT / "assets"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", APP_NAME,
        "--add-data", data_arg(ROOT / "scrap_editor_app" / "web", "scrap_editor_app/web"),
        "--add-data", data_arg(assets / "item-icon-atlas.png", "assets"),
        "--add-data", data_arg(assets / "item-icon-atlas.json", "assets"),
        "--add-data", data_arg(assets / "terrain-cell-atlas.png", "assets"),
        "--add-data", data_arg(assets / "terrain-cell-atlas.json", "assets"),
        "--add-data", data_arg(assets / "terrain-tile-names.json", "assets"),
        "--collect-submodules", "webview", "--collect-data", "webview",
        "--hidden-import", "tkinter.ttk", "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox", "--hidden-import", "tkinter.simpledialog",
    ]
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
    parser.add_argument("--onefile", action="store_true", help="Build one executable instead of the faster one-folder package.")
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args()
    command = build_command(args.onefile)
    if args.print:
        print(subprocess.list2cmdline(command))
        return 0
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
