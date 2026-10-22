from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .steam_paths import find_game_root, find_steam_executable
from companion_mod.companion_settings import (
    companion_settings_path,
    load_companion_settings,
    save_companion_settings,
)


LOG_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ScrapMechanicSaveEditor"
LOG_PATH = LOG_DIR / "editor.log"
WEBVIEW_DATA_DIR = LOG_DIR / "WebView2"
COMPANION_LAUNCH_LOG_PATH = LOG_DIR / "map-companion-launch.log"
COMPANION_RUNTIME_ROOT = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ScrapMechanicMapCompanion" / "Runtime"


def _companion_launcher_path() -> Path:
    """Find the companion beside source or a versioned packaged editor.

    One-file PyInstaller builds execute modules from a temporary extraction
    directory, while the release EXE lives in ``<workspace>/<version>``.  The
    launcher therefore cannot be resolved only relative to ``__file__``.
    Keeping these explicit candidates lets both development runs and the
    versioned desktop EXE launch the same maintained companion installation.
    """
    filename = "Launch Scrap Mechanic with Map Companion.cmd"
    executable_dir = Path(sys.executable).resolve().parent
    candidates = (
        Path(__file__).resolve().parent.parent / "companion_mod" / filename,
        executable_dir / "companion_mod" / filename,
        executable_dir.parent / "companion_mod" / filename,
        Path.cwd().resolve() / "companion_mod" / filename,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _scrap_mechanic_window() -> int:
    """Return the game's top-level window handle without importing UI packages."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.FindWindowW(None, "Scrap Mechanic") or 0)
    except Exception:
        return 0


def _focus_scrap_mechanic_when_ready(timeout: float = 90.0) -> None:
    """Bring the game forward after Steam creates its window.

    This runs on a daemon thread so a slow Steam start never blocks PyWebView.
    The launcher button is an explicit request to open the game, so restoring
    its window is the useful completion state rather than leaving the editor
    in front with no visible result.
    """
    if sys.platform != "win32":
        return
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        handle = _scrap_mechanic_window()
        if handle:
            try:
                import ctypes

                user32 = ctypes.windll.user32
                user32.ShowWindow(handle, 9)  # SW_RESTORE
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
                logging.info("Focused Scrap Mechanic after Map Companion launch")
            except Exception:
                logging.exception("Could not focus Scrap Mechanic after launch")
            return
        time.sleep(0.25)
    logging.warning("Scrap Mechanic window did not appear within %.0f seconds", timeout)


def _launch_map_companion(launcher: Path, *, timeout: float = 15.0) -> dict[str, Any]:
    """Run the external launcher with real stdio and verify its exit status.

    PyInstaller's windowed process has no console handles.  Letting cmd.exe
    inherit those absent handles made the old fire-and-forget request silently
    disappear even though the web UI reported success.  A small persistent log
    gives the child valid handles and leaves a useful diagnostic if it fails.
    """
    if not launcher.is_file():
        raise FileNotFoundError(f"Map Companion launcher was not found: {launcher}")
    if sys.platform != "win32":
        raise RuntimeError("Map Companion can only launch on Windows")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    game_already_running = bool(_scrap_mechanic_window())
    with COMPANION_LAUNCH_LOG_PATH.open("a", encoding="utf-8", errors="replace") as output:
        output.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Launch requested from editor\n")
        output.flush()
        process = subprocess.Popen(
            ["cmd.exe", "/d", "/c", str(launcher)],
            cwd=str(launcher.parent),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=3)
            raise RuntimeError(
                f"Map Companion launcher did not finish within {timeout:.0f} seconds. "
                f"See {COMPANION_LAUNCH_LOG_PATH}"
            ) from exc

    if return_code:
        raise RuntimeError(
            f"Map Companion launcher failed with exit code {return_code}. "
            f"See {COMPANION_LAUNCH_LOG_PATH}"
        )

    logging.info(
        "Map Companion launcher completed: %s (game already running=%s)",
        launcher,
        game_already_running,
    )
    threading.Thread(
        target=_focus_scrap_mechanic_when_ready,
        name="focus-scrap-mechanic",
        daemon=True,
    ).start()
    return {
        "ok": True,
        "started": True,
        "launcher": str(launcher),
        "launchLog": str(COMPANION_LAUNCH_LOG_PATH),
        "gameAlreadyRunning": game_already_running,
    }


def _companion_process_command(signal: Path, game_root: Path) -> list[str]:
    """Return the self-contained companion command for source and frozen runs."""
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--map-companion",
            "--signal",
            str(signal),
            "--game-root",
            str(game_root),
        ]
    return [
        sys.executable,
        "-m",
        "companion_mod.companion_overlay",
        "--signal",
        str(signal),
        "--game-root",
        str(game_root),
    ]


def _start_map_companion() -> dict[str, Any]:
    """Start the bundled overlay and Scrap Mechanic without machine-specific paths."""
    if sys.platform != "win32":
        raise RuntimeError("Map Companion is available only on Windows")
    game_root = find_game_root()
    if game_root is None:
        raise FileNotFoundError(
            "Scrap Mechanic was not found in any registered Steam library. "
            "Install it through Steam or set SCRAP_MECHANIC_GAME_ROOT."
        )

    COMPANION_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    signal = COMPANION_RUNTIME_ROOT / "map_key_signal.json"
    if not signal.is_file():
        signal.write_text('{"sequence":0}\n', encoding="utf-8")

    game_already_running = bool(_scrap_mechanic_window())
    if not game_already_running:
        try:
            os.startfile("steam://rungameid/387990")  # type: ignore[attr-defined]
        except OSError:
            steam = find_steam_executable()
            if steam is None:
                raise FileNotFoundError(
                    "Steam could not be started. Open Steam once, then try again."
                )
            subprocess.Popen(
                [str(steam), "-applaunch", "387990"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    command = _companion_process_command(signal, game_root)
    with COMPANION_LAUNCH_LOG_PATH.open("a", encoding="utf-8", errors="replace") as output:
        output.write(
            f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Bundled companion launch: "
            f"{command[0]}\n"
        )
        output.flush()
        subprocess.Popen(
            command,
            cwd=str(Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
            close_fds=True,
        )

    threading.Thread(
        target=_focus_scrap_mechanic_when_ready,
        name="focus-scrap-mechanic",
        daemon=True,
    ).start()
    return {
        "ok": True,
        "started": True,
        "bundled": True,
        "gameRoot": str(game_root),
        "launchLog": str(COMPANION_LAUNCH_LOG_PATH),
        "gameAlreadyRunning": game_already_running,
    }


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s",
        force=True,
    )

    def log_unhandled(exc_type: type[BaseException], exc: BaseException, traceback: Any) -> None:
        logging.critical("Unhandled desktop exception", exc_info=(exc_type, exc, traceback))

    sys.excepthook = log_unhandled


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ScrapMechanic.SaveEditor.Desktop")
    except Exception:
        pass


class DesktopBridge:
    """The only JavaScript entry point; calls Python directly without HTTP."""

    def __init__(self) -> None:
        # PyWebView recursively exposes public attributes on js_api objects.
        # Keep these private so it never walks the catalogue or native COM tree.
        self._editor: Any = None
        self._editor_lock = threading.Lock()
        self._window: Any = None

    def _get_editor(self) -> Any:
        if self._editor is not None:
            return self._editor
        with self._editor_lock:
            if self._editor is None:
                from .backend import EditorApi

                self._editor = EditorApi()
                try:
                    result = self._editor._maintain_cache()
                    if result.get("removedFiles"):
                        logging.info(
                            "Map cache maintenance removed %s files (%s bytes)",
                            result["removedFiles"],
                            result["removedBytes"],
                        )
                except Exception:
                    logging.exception("Map cache maintenance failed")
        return self._editor

    def request(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if method == "choose-save":
                import webview
                selection = self._window.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=False,
                    file_types=("Scrap Mechanic saves (*.db)", "All files (*.*)"),
                ) if self._window is not None else None
                return {"ok": True, "path": selection[0] if selection else None}
            if method == "choose-blueprint-archive":
                import webview
                selection = self._window.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=False,
                    file_types=("Blueprint archives (*.zip)", "All files (*.*)"),
                ) if self._window is not None else None
                return {"ok": True, "path": selection[0] if selection else None}
            if method == "open-backup-folder":
                folder = self._get_editor().backups.root
                folder.mkdir(parents=True, exist_ok=True)
                if sys.platform == "win32":
                    os.startfile(folder)  # type: ignore[attr-defined]
                return {"ok": True, "path": str(folder)}
            if method == "open-blueprint-folder":
                folder = self._get_editor()._blueprints().root
                folder.mkdir(parents=True, exist_ok=True)
                if sys.platform == "win32":
                    os.startfile(folder)  # type: ignore[attr-defined]
                return {"ok": True, "path": str(folder)}
            if method == "open-blueprint-archive-folder":
                folder = self._get_editor()._blueprints().archive_root
                folder.mkdir(parents=True, exist_ok=True)
                if sys.platform == "win32":
                    os.startfile(folder)  # type: ignore[attr-defined]
                return {"ok": True, "path": str(folder)}
            if method == "companion-settings":
                return {
                    "ok": True,
                    "settings": load_companion_settings(),
                    "path": str(companion_settings_path()),
                }
            if method == "save-companion-settings":
                return {
                    "ok": True,
                    "settings": save_companion_settings(payload or {}),
                    "path": str(companion_settings_path()),
                }
            if method == "launch-map-companion":
                return _start_map_companion()
            result = self._get_editor().dispatch(str(method), payload or {})
            return {"ok": True, **result}
        except Exception as exc:
            logging.exception("Desktop request failed: %s", method)
            return {"ok": False, "error": str(exc)}
        finally:
            elapsed = time.perf_counter() - started
            if elapsed >= 0.25:
                logging.info("Desktop request %s completed in %.3f seconds", method, elapsed)


def launch(debug: bool = False, startup_splash: Any = None) -> int:
    try:
        import webview
    except ImportError:
        print("PyWebView is required. Run: python -m pip install -r requirements-desktop.txt", file=sys.stderr)
        return 1

    _configure_logging()
    _set_windows_app_id()
    from .serverless_resources import web_index

    bridge = DesktopBridge()
    window = webview.create_window(
        "Scrap Mechanic Save Editor",
        web_index().as_uri(),
        js_api=bridge,
        width=1500,
        height=920,
        min_size=(1080, 700),
        background_color="#15171a",
    )
    if startup_splash is not None:
        window.events.shown += startup_splash.close
    bridge._window = window
    def confirm_unsaved_close() -> bool:
        editor = bridge._editor
        if editor is None:
            return True
        dirty = any(bool(getattr(value, "dirty", False)) for value in (
            editor.save, editor.character_profile, editor.customization_profile,
        ))
        if not dirty:
            return True
        return bool(window.create_confirmation_dialog(
            "Unsaved changes", "Close the editor and discard your unsaved changes?"
        ))

    window.events.closing += confirm_unsaved_close
    startup_save = os.getenv("SCRAP_EDITOR_STARTUP_SAVE")
    if startup_save:
        def load_startup_save() -> None:
            time.sleep(1)
            window.evaluate_js(f"loadSave({json.dumps(startup_save)}); true")

        webview.start(
            load_startup_save,
            debug=debug,
            private_mode=False,
            storage_path=str(WEBVIEW_DATA_DIR),
        )
    else:
        webview.start(
            debug=debug,
            private_mode=False,
            storage_path=str(WEBVIEW_DATA_DIR),
        )
    return 0
