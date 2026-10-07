"""The threads built-in automations start, so no automation reads their exported conversations back as evidence."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

from mindroom.path_confinement import read_regular_file_within_root, write_file_within_root

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_FILENAME = "threads.json"
_LOCK = threading.Lock()


def _root(runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / "tracking" / "automations"


def automation_threads(runtime_paths: RuntimePaths) -> set[str]:
    """Return the root event IDs of every thread a built-in automation started, for any agent."""
    try:
        return set(json.loads(read_regular_file_within_root(_root(runtime_paths), _FILENAME)))
    except FileNotFoundError:
        return set()


def record_automation_thread(runtime_paths: RuntimePaths, thread_id: str) -> None:
    """Remember a thread a built-in automation started."""
    with _LOCK:
        threads = automation_threads(runtime_paths)
        threads.add(thread_id)
        write_file_within_root(_root(runtime_paths), _FILENAME, json.dumps(sorted(threads)).encode())
