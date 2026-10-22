from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import time


DEFAULT_MAX_BYTES = 4 * 1024**3
DEFAULT_TARGET_BYTES = 3 * 1024**3
TEMP_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class CacheEntry:
    path: Path
    size: int
    modified: float


def _entries(root: Path) -> list[CacheEntry]:
    rows: list[CacheEntry] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            rows.append(CacheEntry(
                                Path(entry.path),
                                int(stat.st_size),
                                float(stat.st_mtime),
                            ))
                    except OSError:
                        continue
        except OSError:
            continue
    return rows


def cache_state(root: Path) -> dict[str, int | str]:
    rows = _entries(root)
    return {
        "path": str(root),
        "bytes": sum(row.size for row in rows),
        "files": len(rows),
        "limitBytes": DEFAULT_MAX_BYTES,
    }


def trim_cache(
    root: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    now: float | None = None,
) -> dict[str, int | str]:
    """Remove abandoned temporary files and bound old regenerable cache data."""
    current_time = time.time() if now is None else float(now)
    rows = _entries(root)
    removed_files = 0
    removed_bytes = 0

    retained: list[CacheEntry] = []
    for row in rows:
        temporary = row.path.name.endswith((".tmp", ".tmp.png"))
        if temporary and current_time - row.modified >= TEMP_MAX_AGE_SECONDS:
            try:
                row.path.unlink()
                removed_files += 1
                removed_bytes += row.size
                continue
            except OSError:
                pass
        retained.append(row)

    total = sum(row.size for row in retained)
    if total > max(0, int(max_bytes)):
        goal = max(0, min(int(target_bytes), int(max_bytes)))
        # Leave files from the current session alone. They may still be open in
        # WebView2 or be part of an in-progress 3D capture.
        eligible = [
            row for row in retained
            if current_time - row.modified >= 60 * 60
        ]
        for row in sorted(eligible, key=lambda item: (item.modified, item.path.name)):
            if total <= goal:
                break
            try:
                row.path.unlink()
            except OSError:
                continue
            total -= row.size
            removed_files += 1
            removed_bytes += row.size

    # Removing empty directories keeps old revision folders from accumulating.
    if root.is_dir():
        for directory, _children, _files in os.walk(root, topdown=False):
            path = Path(directory)
            if path == root:
                continue
            try:
                path.rmdir()
            except OSError:
                pass

    return {
        **cache_state(root),
        "removedBytes": removed_bytes,
        "removedFiles": removed_files,
    }


def clear_cache(root: Path) -> dict[str, int | str]:
    before = cache_state(root)
    if root.is_dir():
        for child in root.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError:
                continue
    after = cache_state(root)
    return {
        **after,
        "removedBytes": int(before["bytes"]) - int(after["bytes"]),
        "removedFiles": int(before["files"]) - int(after["files"]),
    }
