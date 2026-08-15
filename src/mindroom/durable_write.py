"""Durable file-write helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, cast

from mindroom.constants import safe_replace

if TYPE_CHECKING:
    from collections.abc import Callable

type OverrideRecord = dict[str, str]
_override_load_cache: dict[Path, tuple[int, dict[str, OverrideRecord]]] = {}


def create_directory_durable(path: Path, *, mode: int) -> None:
    """Create one directory and durably publish its private entry."""
    path.mkdir(mode=mode, exist_ok=True)
    path.chmod(mode)
    _fsync_directory_durable(path)
    _fsync_directory_durable(path.parent)


def delete_file_durable(path: Path) -> bool:
    """Unlink one file and durably publish its directory update."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _fsync_directory_durable(path.parent)
    return True


def replace_file_durable(source: Path, target: Path) -> None:
    """Atomically replace one file and durably publish its directory update."""
    source.replace(target)
    _fsync_directory_durable(target.parent)


def write_json_file_durable(
    path: Path,
    payload: object,
    *,
    temp_dir: Path | None = None,
    strict_atomic_replace: bool = False,
    indent: int | None = None,
    sort_keys: bool = False,
    trailing_newline: bool = False,
) -> None:
    """Write JSON through an fsynced temp file, durable replacement, and directory fsync."""
    directory = temp_dir or path.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    if directory != path.parent:
        directory.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = directory / Path(temp_file.name).name
            json.dump(payload, temp_file, indent=indent, sort_keys=sort_keys)
            if trailing_newline:
                temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if strict_atomic_replace:
            replace_file_durable(temp_path, path)
        else:
            safe_replace(temp_path, path)
            _fsync_directory(path.parent)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def load_cached_override_records(
    path: Path,
    is_valid: Callable[[str, dict[object, object]], bool],
) -> dict[str, OverrideRecord]:
    """Load validated override records, caching parsed data by file mtime."""
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _override_load_cache.get(path)
    if cached is not None and cached[0] == mtime_ns:
        return _copy_override_records(cached[1])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    records = {
        record_id: cast("OverrideRecord", record)
        for record_id, record in data.items()
        if isinstance(record_id, str) and isinstance(record, dict) and is_valid(record_id, record)
    }
    _override_load_cache[path] = (mtime_ns, records)
    return _copy_override_records(records)


def _copy_override_records(records: dict[str, OverrideRecord]) -> dict[str, OverrideRecord]:
    """Return mutable records without exposing the cache's nested dictionaries."""
    return {record_id: record.copy() for record_id, record in records.items()}


def write_bounded_override_records(
    path: Path,
    records: dict[str, OverrideRecord],
    *,
    max_records: int,
) -> None:
    """Prune and durably replace one bounded override record file."""
    if len(records) > max_records:
        newest = sorted(records.items(), key=lambda item: item[1].get("set_at", ""), reverse=True)
        records = dict(newest[:max_records])
    write_override_records(path, records)


def write_override_records(path: Path, records: dict[str, OverrideRecord]) -> None:
    """Durably replace one override record file without evicting active records."""
    write_json_file_durable(path, records, indent=2, sort_keys=True)
    _override_load_cache.pop(path, None)


def _fsync_directory(directory: Path) -> None:
    """Best-effort flush of one directory entry after replace/copy fallback."""
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(directory_fd)
        except OSError:
            return
    finally:
        os.close(directory_fd)


def _fsync_directory_durable(directory: Path) -> None:
    """Flush one directory update and propagate durability failures."""
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
