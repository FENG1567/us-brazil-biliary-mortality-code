"""Build a path-portable package manifest, ZIP, and SHA-256 sidecar."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import zipfile


MANIFEST_NAME = "package_manifest.json"
DEFAULT_PACKAGE_NAME = "USBR_MCOD_reproducibility_20261025"
ARCHIVE_TIMESTAMP = (2026, 10, 25, 0, 0, 0)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_role(relative_path: str) -> str:
    if relative_path == "package_manifest.json":
        return "documentation_or_metadata"
    if relative_path == "03_figures_tables/figures/SOURCE_DATA_HASH_MAP.csv":
        return "figure_source_map"
    if relative_path.startswith("03_figures_tables/figures/source_data/"):
        return "figure_source_data"
    if relative_path.startswith("figure_scripts/") and relative_path.endswith(".R"):
        return "figure_code"
    if relative_path.startswith("03_figures_tables/source_data/"):
        return "aggregate_publication_table"
    if relative_path.startswith("02_analysis/formal/"):
        if relative_path.endswith(".csv"):
            return "formal_summary_input"
        return "documentation_or_metadata"
    if relative_path.startswith("02_analysis/reused/"):
        return "reused_summary_input"
    if relative_path == "06_review/CO_2023_INTEGRATION.json":
        return "integration_result"
    if relative_path.startswith("analysis/"):
        return "analysis_code"
    if relative_path.startswith("helpers/"):
        return "source_helper"
    if relative_path.startswith("docs/") or relative_path.endswith(".md"):
        return "documentation_or_metadata"
    return "documentation_or_metadata"


def package_files(package_dir: Path, *, include_manifest: bool = False) -> list[tuple[Path, str]]:
    package_dir = package_dir.resolve(strict=True)
    if not package_dir.is_dir():
        raise ValueError(f"Package path is not a directory: {package_dir}")

    files: list[tuple[Path, str]] = []
    manifest_path = package_dir / MANIFEST_NAME
    for path in package_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlinks are not allowed in a portable package: {path}")
        if not path.is_file() or (path == manifest_path and not include_manifest):
            continue
        resolved = path.resolve(strict=True)
        if os.path.commonpath([str(package_dir), str(resolved)]) != str(package_dir):
            raise ValueError(f"Package file resolves outside package directory: {path}")
        relative = path.relative_to(package_dir).as_posix()
        files.append((path, relative))
    return sorted(files, key=lambda item: item[1])


def build_manifest(package_dir: Path, package_name: str = DEFAULT_PACKAGE_NAME) -> dict:
    files = package_files(package_dir)
    entries = []
    for path, relative in files:
        entries.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "role": package_role(relative),
            }
        )
    return {
        "schema": "portable_package_manifest_v1",
        "package": package_name,
        "hash_algorithm": "SHA-256",
        "manifest_scope": "All package files except package_manifest.json itself; includes the relative package path and byte count.",
        "file_count": len(entries),
        "files": entries,
    }


def write_manifest(package_dir: Path, package_name: str = DEFAULT_PACKAGE_NAME) -> dict:
    manifest = build_manifest(package_dir, package_name)
    manifest_path = package_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def write_archive(package_dir: Path, archive_path: Path) -> str:
    package_dir = package_dir.resolve(strict=True)
    archive_path = archive_path.resolve()
    if archive_path.is_relative_to(package_dir):
        raise ValueError("Archive must be outside the package directory.")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    directories = [package_dir]
    directories.extend(path for path in package_dir.rglob("*") if path.is_dir())
    files = [path for path, _ in package_files(package_dir, include_manifest=True)]

    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for directory in sorted(
            directories,
            key=lambda path: path.relative_to(package_dir).as_posix(),
        ):
            relative = directory.relative_to(package_dir).as_posix()
            name = "package/" + (relative + "/" if relative != "." else "")
            info = zipfile.ZipInfo(name, date_time=ARCHIVE_TIMESTAMP)
            info.create_system = 3
            info.external_attr = (0o40755 << 16) | 0x10
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, b"")

        for path in files:
            relative = path.relative_to(package_dir).as_posix()
            info = zipfile.ZipInfo(f"package/{relative}", date_time=ARCHIVE_TIMESTAMP)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    return sha256_file(archive_path)


def build_package(package_dir: Path, archive_path: Path, package_name: str = DEFAULT_PACKAGE_NAME) -> tuple[int, str, Path]:
    manifest = write_manifest(package_dir, package_name)
    archive_hash = write_archive(package_dir, archive_path)
    archive_path = archive_path.resolve()
    checksum_path = Path(str(archive_path) + ".sha256")
    checksum_path.write_text(f"{archive_hash}  {archive_path.name}\n", encoding="ascii", newline="\n")
    return manifest["file_count"], archive_hash, checksum_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, required=True, help="Package root directory to manifest and archive.")
    parser.add_argument("--archive-path", type=Path, required=True, help="ZIP output path, outside the package directory.")
    parser.add_argument("--package-name", default=DEFAULT_PACKAGE_NAME)
    args = parser.parse_args()

    file_count, archive_hash, checksum_path = build_package(args.package_dir, args.archive_path, args.package_name)
    print(f"file_count={file_count}")
    print(f"sha256={archive_hash}")
    print(f"checksum={checksum_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
