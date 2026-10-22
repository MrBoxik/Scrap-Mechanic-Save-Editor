from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_COMPANION_SETTINGS: dict[str, Any] = {
    "schema": 5,
    "size": 280,
    "marginX": 6,
    "marginY": 6,
    "worldSpan": 650,
    "orientation": "map-up",
    "shape": "square",
    "border": "mechanic",
    "showCompass": True,
    "showKeyHint": True,
    "updateHz": 60,
    "fogOfWar": True,
    "fogRadius": 192,
    "revealShape": "directional",
    "waypointFilters": {
        "quests": {"fullMap": True, "minimap": True},
        "stations": {"fullMap": True, "minimap": True},
        "warehouses": {"fullMap": True, "minimap": True},
        "packing": {"fullMap": True, "minimap": True},
        "labs": {"fullMap": True, "minimap": True},
        "landmarks": {"fullMap": True, "minimap": True},
        "beacons": {"fullMap": True, "minimap": True},
    },
}


def companion_settings_path() -> Path:
    local = os.getenv("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / "AppData" / "Local"
    return (root / "ScrapMechanicMapCompanion" / "settings.json").resolve()


def normalize_companion_settings(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_COMPANION_SETTINGS)
    try:
        source_schema = int(source.get("schema") or 0)
    except (TypeError, ValueError):
        source_schema = 0

    def integer(name: str, minimum: int, maximum: int) -> int:
        try:
            number = int(source.get(name, result[name]))
        except (TypeError, ValueError):
            number = int(result[name])
        return max(minimum, min(maximum, number))

    result["size"] = integer("size", 176, 420)
    result["marginX"] = integer("marginX", 0, 240)
    result["marginY"] = integer("marginY", 0, 240)
    result["worldSpan"] = integer("worldSpan", 320, 1800)
    result["updateHz"] = integer("updateHz", 10, 60)
    # Earlier builds could persist a 24 Hz HUD rate.  The renderer now keeps
    # the expensive map layer independent from the player arrow, so there is
    # no longer a reason to carry that visibly stepped legacy default into a
    # new build.  Schema-4 users may still deliberately choose a lower rate.
    if source_schema < 4:
        result["updateHz"] = 60
    result["fogRadius"] = integer("fogRadius", 96, 512)
    result["orientation"] = (
        source.get("orientation")
        if source.get("orientation") in {"map-up", "player-up"}
        else result["orientation"]
    )
    result["shape"] = (
        source.get("shape")
        if source.get("shape") in {"square", "circle"}
        else result["shape"]
    )
    result["border"] = (
        source.get("border")
        if source.get("border") in {"mechanic", "compact", "none"}
        else result["border"]
    )
    result["showCompass"] = bool(source.get("showCompass", result["showCompass"]))
    result["showKeyHint"] = bool(source.get("showKeyHint", result["showKeyHint"]))
    result["fogOfWar"] = bool(source.get("fogOfWar", result["fogOfWar"]))
    result["revealShape"] = (
        source.get("revealShape")
        if source.get("revealShape") in {"directional", "circle"}
        else result["revealShape"]
    )
    supplied_filters = source.get("waypointFilters")
    supplied_filters = supplied_filters if isinstance(supplied_filters, dict) else {}
    filters: dict[str, dict[str, bool]] = {}
    for category, defaults in DEFAULT_COMPANION_SETTINGS["waypointFilters"].items():
        supplied = supplied_filters.get(category)
        supplied = supplied if isinstance(supplied, dict) else {}
        filters[category] = {
            "fullMap": bool(supplied.get("fullMap", defaults["fullMap"])),
            "minimap": bool(supplied.get("minimap", defaults["minimap"])),
        }
    result["waypointFilters"] = filters
    return result


def load_companion_settings() -> dict[str, Any]:
    path = companion_settings_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        value = {}
    return normalize_companion_settings(value if isinstance(value, dict) else {})


def save_companion_settings(value: dict[str, Any] | None) -> dict[str, Any]:
    settings = normalize_companion_settings(value)
    path = companion_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temporary.replace(path)
    return settings
