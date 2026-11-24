"""Leaf storage, item shape, and actionability primitives for native todo state."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_T = TypeVar("_T")

TERMINAL_STATUSES = frozenset({"done", "cancelled"})
PRIORITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
VALID_STATUSES = frozenset({"open", *TERMINAL_STATUSES})


@dataclass(frozen=True, slots=True)
class NoWriteResult:
    """Mutation result that should not persist a state file."""

    value: Any


def no_write(value: _T) -> NoWriteResult:
    """Return a locked-update result without persisting the mutated data."""
    return NoWriteResult(value)


@dataclass(frozen=True, slots=True)
class TodoItemSnapshot:
    """One stored todo item that passed the strict reader."""

    item_id: str
    title: str
    status: str
    priority: str
    depends_on: tuple[str, ...]
    assigned_agent: str
    requester_id: str | None
    updated_at: datetime


def todo_item(
    item_id: str,
    title: str,
    *,
    priority: str,
    depends_on: list[str],
    assigned_agent: str,
    requester_id: str,
    now: str,
) -> dict[str, Any]:
    """Return one new open todo item in its stored shape."""
    return {
        "id": item_id,
        "title": title,
        "status": "open",
        "priority": priority,
        "depends_on": depends_on,
        "assigned_agent": assigned_agent,
        "requester_id": requester_id,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }


def require_string(data: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    """Return one stored string field, rejecting a missing, non-string, or (unless allowed) empty value."""
    value = data.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        msg = f"{key} must be a {'string' if allow_empty else 'non-empty string'}"
        raise ValueError(msg)
    return value


def _parse_updated_at(value: str) -> datetime:
    try:
        updated_at = datetime.fromisoformat(value)
        if updated_at.tzinfo is None:
            msg = "updated_at must include a timezone"
            raise ValueError(msg)
        return updated_at.astimezone(UTC)
    except OverflowError as exc:
        msg = "updated_at is outside the supported datetime range"
        raise ValueError(msg) from exc


def parse_todo_item(raw_item: object) -> TodoItemSnapshot:
    """Strictly read one stored todo item, raising for any field outside its stored shape."""
    if not isinstance(raw_item, dict):
        msg = "todo item must be an object"
        raise TypeError(msg)
    item_data = cast("dict[str, Any]", raw_item)

    status = require_string(item_data, "status")
    if status not in VALID_STATUSES:
        msg = f"invalid todo status: {status}"
        raise ValueError(msg)

    raw_dependencies = item_data.get("depends_on")
    if not isinstance(raw_dependencies, list) or not all(isinstance(value, str) for value in raw_dependencies):
        msg = "depends_on must be a list of strings"
        raise ValueError(msg)

    priority = require_string(item_data, "priority")
    if priority not in PRIORITY_ORDER:
        msg = f"invalid todo priority: {priority}"
        raise ValueError(msg)

    title = require_string(item_data, "title").strip()
    if not title:
        msg = "title must be a non-empty string"
        raise ValueError(msg)

    # LEGACY_COMPAT: Todo items without a recorded requester_id.
    # Legacy format: A native `todos.json` item with no `requester_id` key.
    # Last legacy release: v2026.9.294 wrote every item without a requester; replacement: v2026.9.295 records the title author's `requester_id` on every item it writes.
    # Handling: The item still parses so its thread's attributed work keeps poking, but it is never poked itself, because its title may come from a human the assignee refuses; its next write by an admitted requester records that requester.
    # Coverage: tests/test_todo_poke.py::test_scan_never_pokes_items_without_a_recorded_requester, tests/test_todo_builtin.py::test_todo_write_records_requester_on_legacy_item.
    requester_id = require_string(item_data, "requester_id") if "requester_id" in item_data else None

    return TodoItemSnapshot(
        item_id=require_string(item_data, "id"),
        title=title,
        status=status,
        priority=priority,
        depends_on=tuple(raw_dependencies),
        assigned_agent=require_string(item_data, "assigned_agent", allow_empty=True),
        requester_id=requester_id,
        updated_at=_parse_updated_at(require_string(item_data, "updated_at")),
    )


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
    """Mutate and durably replace one locked JSON object."""
    with advisory_file_lock(_lock_path(path), exclusive=True):
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
        write_json_file_durable(
            path,
            data,
            strict_atomic_replace=True,
            indent=2,
            sort_keys=True,
            trailing_newline=True,
        )
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
