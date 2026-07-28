"""Open the desktop UI, load a save, and expose catalogue progress in the title.

This is a manual/diagnostic smoke test; it never writes to the selected save.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import webview

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from scrap_editor_app.desktop import DesktopBridge
from scrap_editor_app.serverless_resources import web_index


def exercise(window: webview.Window, save_path: str) -> None:
    time.sleep(1)
    # Do not return loadSave's Promise to Python: that Promise itself calls the
    # Python bridge, so waiting for it here would deadlock the diagnostic thread.
    window.evaluate_js(f"loadSave({json.dumps(save_path)}); true")
    for _ in range(50):
        time.sleep(1)
        progress = window.evaluate_js(
            "({offset:state.catalog.offset,total:state.catalog.total,"
            "cards:document.querySelectorAll('.item-card').length})"
        )
        window.set_title(
            "Catalogue smoke: "
            f"{progress.get('offset', 0)}/{progress.get('total', 0)} "
            f"({progress.get('cards', 0)} cards)"
        )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_catalog_window.py SAVE.db")
    bridge = DesktopBridge()
    window = webview.create_window(
        "Catalogue smoke",
        web_index().as_uri(),
        js_api=bridge,
        width=1500,
        height=920,
        min_size=(1080, 700),
        background_color="#15171a",
    )
    bridge._window = window
    webview.start(exercise, (window, sys.argv[1]), private_mode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
