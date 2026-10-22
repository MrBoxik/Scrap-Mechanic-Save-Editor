from __future__ import annotations

from pathlib import Path
import sys


def bundle_root() -> Path:
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen) if frozen else Path(__file__).resolve().parent.parent


def web_index() -> Path:
    return bundle_root() / "scrap_editor_app" / "web" / "index.html"
