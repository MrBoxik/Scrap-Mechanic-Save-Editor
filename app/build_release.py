from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

from scrap_editor_app import __version__


ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parent
RELEASES = REPOSITORY / "releases"
APP_NAME = "ScrapMechanicSaveEditor"
DOCUMENTS = (
    "LICENSE",
    "README.md",
    "docs/RELEASE_NOTES.md",
    "docs/PRIVACY.md",
    "docs/SECURITY.md",
    "docs/SUPPORT.md",
    "docs/THIRD_PARTY_NOTICES.md",
    "docs/USER_GUIDE.md",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_zip(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, Path(APP_NAME) / path.relative_to(source))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a versioned portable Windows release.")
    parser.add_argument("--output", type=Path, default=RELEASES / __version__)
    parser.add_argument("--replace", action="store_true", help="Replace an existing generated release folder.")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"Release output already exists: {output}")
        if output.parent != RELEASES or output.name != __version__:
            raise SystemExit(f"Refusing to replace a non-version release path: {output}")
        shutil.rmtree(output)

    build_root = ROOT / ".build"
    build_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"release-{__version__}-", dir=build_root))
    try:
        dist = staging / "dist"
        work = staging / "work"
        command = [
            sys.executable,
            str(ROOT / "build_desktop.py"),
            "--dist-dir",
            str(dist),
            "--work-dir",
            str(work),
        ]
        subprocess.run(command, cwd=ROOT, check=True)
        packaged = dist / APP_NAME
        if not (packaged / f"{APP_NAME}.exe").is_file():
            raise RuntimeError("PyInstaller did not create the portable application")

        shutil.copytree(packaged, output)
        for name in DOCUMENTS:
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY / name, destination)

        archive_name = f"{APP_NAME}-{__version__}-Windows-x64.zip"
        archive_temp = staging / archive_name
        create_zip(output, archive_temp)
        archive = output / archive_name
        shutil.copy2(archive_temp, archive)
        executable = output / f"{APP_NAME}.exe"
        checksums = output / "SHA256SUMS.txt"
        checksums.write_text(
            f"{sha256(executable)}  {executable.name}\n"
            f"{sha256(archive)}  {archive.name}\n",
            encoding="ascii",
        )
        print(f"Release folder: {output}")
        print(f"Portable ZIP: {archive}")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
