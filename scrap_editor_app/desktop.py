from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from .backend import EditorApi


LOG_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ScrapMechanicSaveEditor"
LOG_PATH = LOG_DIR / "editor.log"


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
        self._editor = EditorApi()
        self._window: Any = None

    def request(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            if method == "choose-save":
                import webview
                selection = self._window.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=False,
                    file_types=("Scrap Mechanic saves (*.db)", "All files (*.*)"),
                ) if self._window is not None else None
                return {"ok": True, "path": selection[0] if selection else None}
            result = self._editor.dispatch(str(method), payload or {})
            return {"ok": True, **result}
        except Exception as exc:
            logging.exception("Desktop request failed: %s", method)
            return {"ok": False, "error": str(exc)}


def launch(debug: bool = False) -> int:
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
    bridge._window = window
    startup_save = os.getenv("SCRAP_EDITOR_STARTUP_SAVE")
    if startup_save:
        def load_startup_save() -> None:
            time.sleep(1)
            window.evaluate_js(f"loadSave({json.dumps(startup_save)}); true")

        webview.start(load_startup_save, debug=debug, private_mode=True)
    else:
        webview.start(debug=debug, private_mode=True)
    return 0
