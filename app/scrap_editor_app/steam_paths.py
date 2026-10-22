from __future__ import annotations

"""Portable Steam, Scrap Mechanic, and user-data discovery.

The editor is distributed to Windows Steam users with many possible Steam
library layouts.  Keep all discovery in this module so the desktop editor,
packaged companion, build script, and diagnostics agree on the same paths.
"""

from functools import lru_cache
import os
from pathlib import Path
import re
import sys
from typing import Iterable


SCRAP_MECHANIC_APP_ID = "387990"
SCRAP_MECHANIC_INSTALL_DIR = "Scrap Mechanic"
MOD_TOOL_APP_ID = "588870"
MOD_TOOL_INSTALL_DIR = "Scrap Mechanic Mod Tool"


def _unique(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            normalized = Path(os.path.expandvars(str(path))).expanduser()
        except (OSError, TypeError, ValueError):
            continue
        key = os.path.normcase(os.path.abspath(str(normalized)))
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _registry_steam_roots() -> list[Path]:
    if sys.platform != "win32":
        return []
    roots: list[Path] = []
    try:
        import winreg  # type: ignore

        keys = (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", ("SteamPath", "InstallPath")),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam", ("InstallPath", "SteamPath")),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", ("InstallPath", "SteamPath")),
        )
        for hive, key_name, value_names in keys:
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    for value_name in value_names:
                        try:
                            value, _ = winreg.QueryValueEx(key, value_name)
                        except OSError:
                            continue
                        if value:
                            roots.append(Path(str(value).replace("/", os.sep)))
                            break
            except OSError:
                continue
    except (ImportError, OSError):
        pass
    return _unique(roots)


def _default_steam_roots() -> list[Path]:
    values: list[Path] = []
    for name in ("ProgramFiles(x86)", "ProgramFiles", "PROGRAMFILES"):
        root = os.getenv(name)
        if root:
            values.append(Path(root) / "Steam")
    return _unique(values)


@lru_cache(maxsize=1)
def steam_roots() -> tuple[Path, ...]:
    """Return possible Steam client roots without assuming a drive letter."""
    override = os.getenv("STEAM_ROOT") or os.getenv("STEAM_PATH")
    candidates: list[Path] = [Path(override)] if override else []
    candidates.extend(_registry_steam_roots())
    candidates.extend(_default_steam_roots())
    return tuple(_unique(candidates))


def _library_paths_from_vdf(vdf: Path) -> list[Path]:
    try:
        text = vdf.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []

    libraries: list[Path] = []
    # Current Steam format: "path" "D:\\SteamLibrary".
    for match in re.finditer(r'"path"\s+"([^"]+)"', text, flags=re.IGNORECASE):
        libraries.append(Path(match.group(1).replace(r"\\", "\\")))
    # Legacy format: "1" "D:\\SteamLibrary".  Restrict this to values
    # that look like absolute paths so app IDs and metadata are not mistaken
    # for libraries.
    for match in re.finditer(r'^\s*"\d+"\s+"([A-Za-z]:\\[^"]+)"', text, flags=re.MULTILINE):
        libraries.append(Path(match.group(1).replace(r"\\", "\\")))
    return _unique(libraries)


@lru_cache(maxsize=1)
def steam_library_roots() -> tuple[Path, ...]:
    """Return Steam library roots, including the client library itself."""
    libraries: list[Path] = list(steam_roots())
    for root in steam_roots():
        libraries.extend(_library_paths_from_vdf(root / "steamapps" / "libraryfolders.vdf"))
    return tuple(_unique(libraries))


def _manifest_install_dir(manifest: Path) -> str | None:
    try:
        text = manifest.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    match = re.search(r'"installdir"\s+"([^"]+)"', text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def app_install_candidates(app_id: str, fallback_install_dir: str) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for library in steam_library_roots():
        steamapps = library / "steamapps"
        manifest = steamapps / f"appmanifest_{app_id}.acf"
        install_dir = _manifest_install_dir(manifest) or fallback_install_dir
        candidates.append(steamapps / "common" / install_dir)
    return tuple(_unique(candidates))


def is_game_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "Release" / "ScrapMechanic.exe").is_file()
        and (path / "Survival").is_dir()
        and (path / "Data").is_dir()
    )


def game_install_candidates() -> tuple[Path, ...]:
    override = os.getenv("SCRAP_MECHANIC_GAME_ROOT")
    candidates: list[Path] = [Path(override)] if override else []
    candidates.extend(app_install_candidates(SCRAP_MECHANIC_APP_ID, SCRAP_MECHANIC_INSTALL_DIR))
    return tuple(_unique(candidates))


def find_game_root() -> Path | None:
    for candidate in game_install_candidates():
        if is_game_root(candidate):
            return candidate.resolve()
    return None


def find_mod_tool_root() -> Path | None:
    override = os.getenv("SCRAP_MECHANIC_MOD_TOOL_ROOT")
    candidates: list[Path] = [Path(override)] if override else []
    candidates.extend(app_install_candidates(MOD_TOOL_APP_ID, MOD_TOOL_INSTALL_DIR))
    for candidate in _unique(candidates):
        if (candidate / "Release" / "ModTool.exe").is_file():
            return candidate.resolve()
    return None


def find_steam_executable() -> Path | None:
    override = os.getenv("STEAM_EXE")
    candidates: list[Path] = [Path(override)] if override else []
    candidates.extend(root / "steam.exe" for root in steam_roots())
    for candidate in _unique(candidates):
        if candidate.is_file():
            return candidate.resolve()
    return None


def scrap_user_root() -> Path:
    appdata = os.getenv("APPDATA")
    roaming = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return roaming / "Axolot Games" / "Scrap Mechanic" / "User"


def scrap_user_directories() -> tuple[Path, ...]:
    root = scrap_user_root()
    if not root.is_dir():
        return ()
    try:
        return tuple(sorted(
            (path.resolve() for path in root.glob("User_*") if path.is_dir()),
            key=lambda path: path.name.casefold(),
        ))
    except OSError:
        return ()


def installation_diagnostics() -> dict[str, object]:
    """Return JSON-safe discovery results for the release-readiness UI."""
    game = find_game_root()
    steam = find_steam_executable()
    tool = find_mod_tool_root()
    users = scrap_user_directories()
    return {
        "platformSupported": sys.platform == "win32",
        "steam": str(steam) if steam else None,
        "game": str(game) if game else None,
        "modTool": str(tool) if tool else None,
        "userRoot": str(scrap_user_root()),
        "users": [str(path) for path in users],
        "gameCandidates": [str(path) for path in game_install_candidates()],
    }
