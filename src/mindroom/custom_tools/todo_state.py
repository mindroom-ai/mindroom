"""Leaf storage and actionability primitives for native todo state."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast

from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_T = TypeVar("_T")

TERMINAL_STATUSES = frozenset({"done", "cancelled"})
PRIORITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(frozen=True, slots=True)
class NoWriteResult:
    """Mutation result that should not persist a state file."""

    value: Any


def no_write(value: _T) -> NoWriteResult:
    """Return a locked-update result without persisting the mutated data."""
    return NoWriteResult(value)


def _safe_slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]", "_", value)).strip("_")


def _thread_key(room_id: str, thread_id: str | None) -> str:
    """Return the collision-resistant on-disk key for one todo scope."""
    resolved = thread_id or "main"
    digest = hashlib.sha256(f"{room_id}\0{resolved}".encode()).hexdigest()[:16]
    room_slug = _safe_slug(room_id) or "room"
    thread_slug = _safe_slug(resolved) or "thread"
    return f"{room_slug}_{thread_slug}_{digest}"


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object while holding its shared advisory lock."""
    if not path.exists():
        return {}

    with advisory_file_lock(_lock_path(path), exclusive=False):
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))


def locked_update_json(
    path: Path,
    mutate: Callable[[dict[str, Any]], _T | NoWriteResult],
    *,
    recover_invalid: bool = False,
) -> _T:
    """Mutate and atomically replace one locked JSON object."""
    with advisory_file_lock(_lock_path(path), exclusive=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            loaded: object = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except ValueError:
            if not recover_invalid:
                raise
            loaded = {}
        if not isinstance(loaded, dict):
            if not recover_invalid:
                msg = "JSON state root must be an object"
                raise TypeError(msg)
            loaded = {}
        data = cast("dict[str, Any]", loaded)
        result = mutate(data)
        if isinstance(result, NoWriteResult):
            return result.value
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(path)
        return result


def state_root(runtime_paths: RuntimePaths) -> Path:
    """Return the native todo state root."""
    return runtime_paths.storage_root / "todo"


def todos_path(todo_root: Path, room_id: str, thread_id: str | None) -> Path:
    """Return the todo state path for one room/thread scope."""
    key = _thread_key(room_id, thread_id)
    return todo_root / "threads" / key / "todos.json"


def is_blocked(
    item: Mapping[str, Any],
    items_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return whether one open item is blocked by unfinished dependencies."""
    for dep_id in item.get("depends_on", []):
        dep = items_by_id.get(dep_id)
        if dep is None:
            continue
        if dep["status"] not in TERMINAL_STATUSES:
            return True
    return False


def is_actionable(
    item: Mapping[str, Any],
    items_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return whether one todo is open and unblocked."""
    return item["status"] == "open" and not is_blocked(item, items_by_id)
