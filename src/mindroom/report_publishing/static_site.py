"""Static-site snapshot helpers for public report publishing."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.path_confinement import open_directory_within_root

if TYPE_CHECKING:
    from collections.abc import Iterator

_STATIC_SITE_MAX_FILES = 200
_STATIC_SITE_MAX_DIRECTORIES = 200
_STATIC_SITE_MAX_BYTES = 10 * 1024 * 1024
_STATIC_SITE_MAX_DEPTH = 32
_STATIC_SITE_COPY_CHUNK_BYTES = 256 * 1024
_HTML_PAGE_SUFFIXES = frozenset({".html", ".htm"})


class StaticSiteSnapshotError(ValueError):
    """Raised when a static-site snapshot is invalid."""


@dataclass(frozen=True)
class _SnapshotTotals:
    """Running entry totals bounded across one snapshot walk."""

    files: int = 0
    directories: int = 0
    total_bytes: int = 0


def snapshot_static_site(source_root: Path, source_path: Path, destination_dir: Path) -> None:
    """Copy one static-site directory or single HTML page without following links.

    ``source_root`` is the trusted root the source was already authorized against.
    The site lives in an agent workspace that sandboxed tools can write, so every
    entry is reached through pinned directory descriptors, opened with
    ``O_NOFOLLOW`` and classified by ``fstat`` before a byte is read. A link
    swapped in after validation therefore fails the copy instead of redirecting it.
    A hard link is indistinguishable from the regular file it names, so that case
    still relies on the writer only being able to link what its own namespace
    already exposes.
    """
    relative_path = _relative_source_path(source_root, source_path)
    with _open_source_entry(source_root, relative_path) as source_fd:
        entry_mode = os.fstat(source_fd).st_mode
        if stat.S_ISREG(entry_mode):
            _snapshot_single_html_page(source_fd, relative_path.name, destination_dir)
            return
        if not stat.S_ISDIR(entry_mode):
            msg = "Static site source path must be a directory or an HTML file."
            raise StaticSiteSnapshotError(msg)
        _snapshot_site_directory(source_fd, destination_dir)


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


def _snapshot_site_directory(source_fd: int, destination_dir: Path) -> None:
    if not _has_regular_index_page(source_fd):
        msg = "Static site source must contain index.html."
        raise StaticSiteSnapshotError(msg)
    with _published_destination(destination_dir) as destination_fd:
        _copy_directory_entries(source_fd, destination_fd, _SnapshotTotals(), depth=0)


def _snapshot_single_html_page(source_fd: int, name: str, destination_dir: Path) -> None:
    if Path(name).suffix.lower() not in _HTML_PAGE_SUFFIXES:
        msg = "Static site source file must be an HTML page."
        raise StaticSiteSnapshotError(msg)
    with _published_destination(destination_dir) as destination_fd:
        _copy_regular_file(source_fd, destination_fd, "index.html", remaining_bytes=_STATIC_SITE_MAX_BYTES)


@contextmanager
def _published_destination(destination_dir: Path) -> Iterator[int]:
    """Create the snapshot directory, then revalidate or remove whatever was written."""
    destination_dir.mkdir(parents=True, exist_ok=False)
    try:
        with open_directory_within_root(destination_dir) as destination_fd:
            yield destination_fd
            _validate_published_snapshot(destination_fd)
    except (OSError, StaticSiteSnapshotError):
        # A failed or unverifiable snapshot is unreferenced garbage; remove it
        # before surfacing the failure so no public link can ever reach it.
        shutil.rmtree(destination_dir, ignore_errors=True)
        raise


def _copy_directory_entries(
    source_fd: int,
    destination_fd: int,
    totals: _SnapshotTotals,
    *,
    depth: int,
) -> _SnapshotTotals:
    _check_depth(depth)
    for name in sorted(os.listdir(source_fd)):
        entry_fd = _open_source_child(source_fd, name)
        try:
            entry_mode = os.fstat(entry_fd).st_mode
            if stat.S_ISDIR(entry_mode):
                totals = _check_totals(replace(totals, directories=totals.directories + 1))
                with open_directory_within_root(destination_fd, name, create=True) as child_fd:
                    totals = _copy_directory_entries(entry_fd, child_fd, totals, depth=depth + 1)
                continue
            if not stat.S_ISREG(entry_mode):
                msg = f"Static site source must contain only regular files: {name}"
                raise StaticSiteSnapshotError(msg)
            totals = _check_totals(replace(totals, files=totals.files + 1))
            copied = _copy_regular_file(
                entry_fd,
                destination_fd,
                name,
                remaining_bytes=_STATIC_SITE_MAX_BYTES - totals.total_bytes,
            )
            totals = replace(totals, total_bytes=totals.total_bytes + copied)
        finally:
            os.close(entry_fd)
    return totals


def _copy_regular_file(source_fd: int, destination_fd: int, name: str, *, remaining_bytes: int) -> int:
    """Stream one already-verified regular source descriptor into the snapshot directory."""
    copied = 0
    output_fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o644,
        dir_fd=destination_fd,
    )
    with os.fdopen(output_fd, "wb") as output:
        while True:
            chunk = os.read(source_fd, _STATIC_SITE_COPY_CHUNK_BYTES)
            if not chunk:
                return copied
            copied += len(chunk)
            if copied > remaining_bytes:
                msg = f"Static site is larger than {_STATIC_SITE_MAX_BYTES} bytes."
                raise StaticSiteSnapshotError(msg)
            output.write(chunk)


def _validate_published_snapshot(destination_fd: int) -> None:
    """Re-read the written snapshot so only a bounded tree of regular files is ever linked."""
    if not _has_regular_index_page(destination_fd):
        msg = "Published static site must contain index.html."
        raise StaticSiteSnapshotError(msg)
    _validate_published_entries(destination_fd, _SnapshotTotals(), depth=0)


def _validate_published_entries(
    destination_fd: int,
    totals: _SnapshotTotals,
    *,
    depth: int,
) -> _SnapshotTotals:
    _check_depth(depth)
    for name in sorted(os.listdir(destination_fd)):
        entry_stat = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry_stat.st_mode):
            totals = _check_totals(replace(totals, directories=totals.directories + 1))
            with open_directory_within_root(destination_fd, name) as child_fd:
                totals = _validate_published_entries(child_fd, totals, depth=depth + 1)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            msg = f"Published static site must contain only regular files: {name}"
            raise StaticSiteSnapshotError(msg)
        totals = _check_totals(
            replace(
                totals,
                files=totals.files + 1,
                total_bytes=totals.total_bytes + entry_stat.st_size,
            ),
        )
    return totals


def _check_totals(totals: _SnapshotTotals) -> _SnapshotTotals:
    """Fail the snapshot as soon as one running total passes its limit."""
    if totals.files > _STATIC_SITE_MAX_FILES:
        msg = f"Static site contains more than {_STATIC_SITE_MAX_FILES} files."
        raise StaticSiteSnapshotError(msg)
    if totals.directories > _STATIC_SITE_MAX_DIRECTORIES:
        msg = f"Static site contains more than {_STATIC_SITE_MAX_DIRECTORIES} directories."
        raise StaticSiteSnapshotError(msg)
    if totals.total_bytes > _STATIC_SITE_MAX_BYTES:
        msg = f"Static site is larger than {_STATIC_SITE_MAX_BYTES} bytes."
        raise StaticSiteSnapshotError(msg)
    return totals


def _check_depth(depth: int) -> None:
    if depth > _STATIC_SITE_MAX_DEPTH:
        msg = f"Static site directories are nested more than {_STATIC_SITE_MAX_DEPTH} levels deep."
        raise StaticSiteSnapshotError(msg)


def _has_regular_index_page(directory_fd: int) -> bool:
    """Report whether the pinned directory holds a regular index.html of its own."""
    try:
        index_stat = os.stat("index.html", dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(index_stat.st_mode)


def _relative_source_path(source_root: Path, source_path: Path) -> Path:
    msg = "Static site source must stay within its authorized root."
    try:
        relative_path = source_path.relative_to(source_root.expanduser().resolve())
    except ValueError as exc:
        raise StaticSiteSnapshotError(msg) from exc
    # relative_to is lexical, so a traversing source still has to be rejected here.
    if ".." in relative_path.parts:
        raise StaticSiteSnapshotError(msg)
    return relative_path


@contextmanager
def _open_source_entry(source_root: Path, relative_path: Path) -> Iterator[int]:
    """Pin the authorized source entry through a no-follow walk from its trusted root."""
    parts = relative_path.parts
    with open_directory_within_root(source_root, Path(*parts[:-1])) as parent_fd:
        if not parts:
            yield parent_fd
            return
        entry_fd = _open_source_child(parent_fd, parts[-1])
        try:
            yield entry_fd
        finally:
            os.close(entry_fd)


def _open_source_child(directory_fd: int, name: str) -> int:
    """Open one source entry below a pinned directory without following a link into it."""
    try:
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except OSError as exc:
        # Linux reports a refused O_NOFOLLOW open as ELOOP; the BSDs use EMLINK.
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            msg = f"Static site source must not contain symlinks: {name}"
            raise StaticSiteSnapshotError(msg) from exc
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            msg = f"Static site source entry was not found: {name}"
            raise StaticSiteSnapshotError(msg) from exc
        raise
