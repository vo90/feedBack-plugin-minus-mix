#!/usr/bin/env python3
"""Build the standalone MinusMix plugin archive used for manual installs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import zipfile


PLUGIN_FILES = (
    "CHANGELOG.md",
    "README.md",
    "batch.py",
    "exporter.py",
    "plugin.json",
    "routes.py",
    "screen.html",
    "screen.js",
    "separator_client.py",
    "single.py",
)
PLUGIN_DIRECTORIES = ("assets",)
ARCHIVE_ROOT = "minus_mix"


def _runtime_files(repo_root: Path) -> list[Path]:
    files = [repo_root / name for name in PLUGIN_FILES]
    for directory in PLUGIN_DIRECTORIES:
        files.extend(path for path in (repo_root / directory).rglob("*") if path.is_file())

    missing = [path.relative_to(repo_root).as_posix() for path in files if not path.is_file()]
    if missing:
        raise RuntimeError(f"Missing release files: {', '.join(missing)}")
    return sorted(files, key=lambda path: path.relative_to(repo_root).as_posix())


def build_release(
    repo_root: Path,
    output_dir: Path,
    expected_version: str | None = None,
) -> Path:
    manifest = json.loads((repo_root / "plugin.json").read_text(encoding="utf-8"))
    version = str(manifest.get("version", "")).strip()
    if not version:
        raise RuntimeError("plugin.json does not contain a version")
    if expected_version and version != expected_version.removeprefix("v"):
        raise RuntimeError(
            f"Release version mismatch: plugin.json is {version}, "
            f"but the requested release is {expected_version}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"MinusMix-{version}.zip"
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for source in _runtime_files(repo_root):
            relative = source.relative_to(repo_root).as_posix()
            archive.write(source, f"{ARCHIVE_ROOT}/{relative}")

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        manifest_path = f"{ARCHIVE_ROOT}/plugin.json"
        if manifest_path not in names:
            raise RuntimeError(f"Release archive is missing {manifest_path}")
        if any(name.startswith(f"{ARCHIVE_ROOT}/{ARCHIVE_ROOT}/") for name in names):
            raise RuntimeError("Release archive contains an unexpected nested plugin folder")

    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--expected-version")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    archive = build_release(repo_root, args.output_dir.resolve(), args.expected_version)
    print(archive)


if __name__ == "__main__":
    main()
