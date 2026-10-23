"""Shared helpers for the agent rename tests."""

from __future__ import annotations

import os
from pathlib import Path


def tree_snapshot(root: Path) -> dict[str, bytes | str]:
    """Map every path under ``root`` to its file bytes, ``"dir"``, or ``"symlink:<target>"``."""
    snapshot: dict[str, bytes | str] = {}
    for current, directory_names, file_names in os.walk(root):
        for name in (*directory_names, *file_names):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                snapshot[relative] = f"symlink:{path.readlink()}"
            elif path.is_dir():
                snapshot[relative] = "dir"
            else:
                snapshot[relative] = path.read_bytes()
    return snapshot
