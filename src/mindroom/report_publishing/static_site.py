"""Static-site snapshot helpers for public report publishing."""

from __future__ import annotations

import shutil
from pathlib import Path

_STATIC_SITE_MAX_FILES = 200
_STATIC_SITE_MAX_BYTES = 10 * 1024 * 1024


class StaticSiteSnapshotError(ValueError):
    """Raised when a static-site snapshot is invalid."""


def snapshot_static_site(source_path: Path, destination_dir: Path) -> None:
    """Copy one static-site directory or single HTML page without symlinks or path escapes."""
    if source_path.is_symlink():
        msg = "Static site source must not be a symlink."
        raise StaticSiteSnapshotError(msg)
    resolved_source = source_path.resolve()
    if resolved_source.is_file():
        _snapshot_single_html_page(resolved_source, destination_dir)
        return
    if not resolved_source.is_dir():
        msg = "Static site source path must be a directory or an HTML file."
        raise StaticSiteSnapshotError(msg)
    if not (resolved_source / "index.html").is_file():
        msg = "Static site source must contain index.html."
        raise StaticSiteSnapshotError(msg)

    entries = _static_site_entries(resolved_source)
    file_count = sum(1 for source_path, _relative_path in entries if source_path.is_file())
    total_bytes = sum(source_path.stat().st_size for source_path, _relative_path in entries if source_path.is_file())
    if file_count > _STATIC_SITE_MAX_FILES:
        msg = f"Static site contains too many files: {file_count} > {_STATIC_SITE_MAX_FILES}."
        raise StaticSiteSnapshotError(msg)
    if total_bytes > _STATIC_SITE_MAX_BYTES:
        msg = f"Static site is too large: {total_bytes} > {_STATIC_SITE_MAX_BYTES} bytes."
        raise StaticSiteSnapshotError(msg)

    destination_dir.mkdir(parents=True, exist_ok=False)
    for entry_path, relative_path in entries:
        target_path = destination_dir / relative_path
        if entry_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry_path, target_path)


def _snapshot_single_html_page(source_file: Path, destination_dir: Path) -> None:
    if source_file.suffix.lower() not in {".html", ".htm"}:
        msg = "Static site source file must be an HTML page."
        raise StaticSiteSnapshotError(msg)
    total_bytes = source_file.stat().st_size
    if total_bytes > _STATIC_SITE_MAX_BYTES:
        msg = f"Static site is too large: {total_bytes} > {_STATIC_SITE_MAX_BYTES} bytes."
        raise StaticSiteSnapshotError(msg)
    destination_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source_file, destination_dir / "index.html")


def resolve_static_site_asset(site_root: Path, asset_path: str | None) -> Path:
    """Resolve one static-site asset path under a copied site root."""
    relative_asset = Path(asset_path.strip("/") if asset_path else "index.html")
    if relative_asset == Path() or relative_asset.is_absolute() or ".." in relative_asset.parts:
        msg = "Published report asset path is invalid."
        raise StaticSiteSnapshotError(msg)
    resolved_root = site_root.resolve()
    resolved_asset = (site_root / relative_asset).resolve()
    if not resolved_asset.is_relative_to(resolved_root):
        msg = "Published report asset path is invalid."
        raise StaticSiteSnapshotError(msg)
    if not resolved_asset.is_file():
        msg = "Published report asset was not found."
        raise StaticSiteSnapshotError(msg)
    return resolved_asset


def _static_site_entries(source_dir: Path) -> list[tuple[Path, Path]]:
    entries: list[tuple[Path, Path]] = []
    for source_path in sorted(source_dir.rglob("*")):
        if source_path.is_symlink():
            msg = f"Static site source must not contain symlinks: {source_path}"
            raise StaticSiteSnapshotError(msg)
        relative_path = source_path.relative_to(source_dir)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            msg = "Static site source path is invalid."
            raise StaticSiteSnapshotError(msg)
        entries.append((source_path, relative_path))
    return entries
