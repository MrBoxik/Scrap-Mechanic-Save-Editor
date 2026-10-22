from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Callable
import uuid


def default_backup_root() -> Path:
    override = os.getenv("SCRAP_EDITOR_BACKUP_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    local = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "ScrapMechanicSaveEditor" / "Backups"


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .")
    return cleaned[:80] or "save"


def _validate_archive(path: Path, expected_suffix: str | None = None) -> None:
    suffix = (expected_suffix or path.suffix).casefold()
    if suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, (dict, list)):
            raise RuntimeError("Backup JSON has an unsupported root value")
        return
    if suffix == ".db":
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if result != "ok":
                raise RuntimeError(f"Backup integrity check failed: {result}")
        finally:
            connection.close()


def _ensure_quiet_database(path: Path) -> None:
    if path.suffix.casefold() != ".db":
        return
    if Path(str(path) + "-wal").exists() or Path(str(path) + "-journal").exists():
        raise RuntimeError("The save has an active SQLite journal. Close Scrap Mechanic and try again.")


class BackupManager:
    """Versioned archives kept outside every Scrap Mechanic user save folder."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_backup_root()).resolve()

    def _destination(self, source: Path, category: str) -> Path:
        now = datetime.now()
        folder = self.root / _safe_name(category) / _safe_name(source.stem)
        suffix = source.suffix if source.suffix else ".bin"
        name = f"{now:%Y-%m-%d_%H-%M-%S-%f}_{uuid.uuid4().hex[:8]}{suffix}"
        return folder / name

    @staticmethod
    def _sidecar(path: Path) -> Path:
        return path.with_name(path.name + ".backup.json")

    def archive(
        self,
        source: str | os.PathLike[str],
        *,
        category: str,
        kind: str,
        label: str | None = None,
        producer: Callable[[Path], Any] | None = None,
    ) -> dict[str, Any]:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        _ensure_quiet_database(source_path)
        destination = self._destination(source_path, category)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if producer:
                producer(destination)
            else:
                shutil.copy2(source_path, destination)
            _validate_archive(destination, source_path.suffix)
            stat = destination.stat()
            metadata = {
                "id": destination.relative_to(self.root).as_posix(),
                "path": str(destination),
                "sourcePath": str(source_path),
                "sourceName": source_path.name,
                "label": label or source_path.stem,
                "category": category,
                "kind": kind,
                "created": datetime.now(timezone.utc).isoformat(),
                "size": stat.st_size,
            }
            sidecar = self._sidecar(destination)
            sidecar.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
            return metadata
        except Exception:
            destination.unlink(missing_ok=True)
            self._sidecar(destination).unlink(missing_ok=True)
            raise

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for sidecar in self.root.rglob("*.backup.json"):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                archive = Path(str(data["path"])).resolve()
                if archive.is_file() and archive.is_relative_to(self.root):
                    data["size"] = archive.stat().st_size
                    records.append(data)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(records, key=lambda item: str(item.get("created", "")), reverse=True)

    def _record(self, backup_id: str) -> dict[str, Any]:
        archive = (self.root / Path(str(backup_id))).resolve()
        if not archive.is_relative_to(self.root) or not archive.is_file():
            raise ValueError("Backup record was not found")
        sidecar = self._sidecar(archive)
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        if Path(str(data.get("path"))).resolve() != archive:
            raise ValueError("Backup metadata does not match its archive")
        return data

    def restore(self, backup_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        record = self._record(backup_id)
        archive = Path(record["path"]).resolve()
        target = Path(record["sourcePath"]).resolve()
        if target.suffix.casefold() not in {".db", ".json"}:
            raise ValueError("Only Scrap Mechanic database and progress backups can be restored")
        _ensure_quiet_database(target)
        safety = None
        if target.is_file():
            safety = self.archive(
                target,
                category=str(record.get("category") or "World"),
                kind="pre-restore",
                label=f"Before restore · {target.stem}",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.stem}-restore-", suffix=".tmp", dir=target.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(archive, temporary)
            _validate_archive(temporary, target.suffix)
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return record, safety
