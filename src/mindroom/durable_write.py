"""Durable file-write helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from mindroom.constants import safe_replace


def write_json_file_durable(
    path: Path,
    payload: object,
    *,
    temp_dir: Path | None = None,
    indent: int | None = None,
    sort_keys: bool = False,
) -> None:
    """Write JSON through fsynced temp file, bind-mount-safe replace, and directory fsync."""
    directory = temp_dir or path.parent
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
            temp_file.flush()
            os.fsync(temp_file.fileno())
        safe_replace(temp_path, path)
        fsync_directory(path.parent)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def fsync_directory(directory: Path) -> None:
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
