"""Native scanner that wakes idle agents with actionable assigned todos."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from mindroom.custom_tools.todo_state import (
    TERMINAL_STATUSES,
    is_actionable,
    locked_update_json,
    read_json,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

__all__ = [
    "TodoPokeDeps",
    "TodoPokePolicy",
    "TodoPokeWorker",
    "scan_todo_pokes",
    "todo_poke_policy",
]

type _TodoScheduleQuery = Callable[[str], Awaitable[frozenset[str | None] | None]]
type _TodoPokeSender = Callable[[str, str, str | None], Awaitable[str | None]]

_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_VALID_STATUSES = {"open", *TERMINAL_STATUSES}
_POKE_STATE_FILENAME = "poke_state.json"
_VISIBLE_ITEM_LIMIT = 5


@dataclass(frozen=True, slots=True)
class TodoPokePolicy:
    """Timing and delivery limits for native todo pokes."""

    interval_seconds: float = 120
    cooldown_seconds: float = 300
    quiet_seconds: float = 300
    max_pokes_per_scan: int = 3


@dataclass(frozen=True, slots=True)
class TodoPokeDeps:
    """Runtime collaborators injected into the todo poke scanner."""

    state_root: Callable[[], Path]
    schedule_query: _TodoScheduleQuery | None
    idle_check: Callable[[str], bool]
    sender: _TodoPokeSender | None
    clock: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class _TodoItemSnapshot:
    item_id: str
    title: str
    status: str
    priority: str
    depends_on: tuple[str, ...]
    assigned_agent: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class _TodoThreadSnapshot:
    room_id: str
    thread_id: str | None
    items: tuple[_TodoItemSnapshot, ...]
    actionable_item_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _TodoPokeScope:
    assigned_agent: str
    room_id: str
    thread_id: str | None
    actionable_items: tuple[_TodoItemSnapshot, ...]
    latest_actionable_update: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _PokeRecord:
    last_poked_at: float
    last_fingerprint: str


def _env_seconds(runtime_paths: RuntimePaths, name: str, default: float) -> float:
    raw_value = runtime_paths.env_value(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        logger.warning("todo_poke_env_value_invalid", env_name=name, value=raw_value, default=default)
        return default
    if value < 0 or not math.isfinite(value):
        logger.warning("todo_poke_env_value_invalid", env_name=name, value=raw_value, default=default)
        return default
    return value


def todo_poke_policy(runtime_paths: RuntimePaths) -> TodoPokePolicy:
    """Build the todo poke policy from runtime-scoped environment values."""
    defaults = TodoPokePolicy()
    return TodoPokePolicy(
        interval_seconds=_env_seconds(
            runtime_paths,
            "MINDROOM_TODO_POKE_INTERVAL_SECONDS",
            defaults.interval_seconds,
        ),
        cooldown_seconds=defaults.cooldown_seconds,
        quiet_seconds=_env_seconds(
            runtime_paths,
            "MINDROOM_TODO_POKE_QUIET_SECONDS",
            defaults.quiet_seconds,
        ),
        max_pokes_per_scan=defaults.max_pokes_per_scan,
    )


def _require_string(data: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        msg = f"{key} must be a {'string' if allow_empty else 'non-empty string'}"
        raise ValueError(msg)
    return value


def _parse_updated_at(value: str) -> datetime:
    updated_at = datetime.fromisoformat(value)
    if updated_at.tzinfo is None:
        msg = "updated_at must include a timezone"
        raise ValueError(msg)
    return updated_at.astimezone(UTC)


def _parse_item(raw_item: object) -> _TodoItemSnapshot:
    if not isinstance(raw_item, dict):
        msg = "todo item must be an object"
        raise TypeError(msg)
    item_data = cast("dict[str, Any]", raw_item)

    status = _require_string(item_data, "status")
    if status not in _VALID_STATUSES:
        msg = f"invalid todo status: {status}"
        raise ValueError(msg)

    raw_dependencies = item_data.get("depends_on")
    if not isinstance(raw_dependencies, list) or not all(isinstance(value, str) for value in raw_dependencies):
        msg = "depends_on must be a list of strings"
        raise ValueError(msg)

    priority = _require_string(item_data, "priority")
    if priority not in _PRIORITY_ORDER:
        msg = f"invalid todo priority: {priority}"
        raise ValueError(msg)

    return _TodoItemSnapshot(
        item_id=_require_string(item_data, "id"),
        title=_require_string(item_data, "title"),
        status=status,
        priority=priority,
        depends_on=tuple(raw_dependencies),
        assigned_agent=_require_string(item_data, "assigned_agent", allow_empty=True),
        updated_at=_parse_updated_at(_require_string(item_data, "updated_at")),
    )


def _parse_thread_snapshot(data: object) -> _TodoThreadSnapshot:
    if not isinstance(data, dict):
        msg = "todo state must be an object"
        raise TypeError(msg)
    thread_data = cast("dict[str, Any]", data)

    room_id = _require_string(thread_data, "room_id")
    stored_thread_id = _require_string(thread_data, "thread_id")
    thread_id = None if stored_thread_id == "main" else stored_thread_id
    raw_items = thread_data.get("items")
    if not isinstance(raw_items, list):
        msg = "items must be a list"
        raise TypeError(msg)

    items = tuple(_parse_item(raw_item) for raw_item in raw_items)
    if len({item.item_id for item in items}) != len(items):
        msg = "todo item ids must be unique"
        raise ValueError(msg)

    items_by_id = {
        item.item_id: {
            "status": item.status,
            "depends_on": item.depends_on,
        }
        for item in items
    }
    actionable_item_ids = frozenset(
        item.item_id for item in items if is_actionable(items_by_id[item.item_id], items_by_id)
    )
    return _TodoThreadSnapshot(
        room_id=room_id,
        thread_id=thread_id,
        items=items,
        actionable_item_ids=actionable_item_ids,
    )


def _read_thread_snapshots(todo_root: Path) -> list[_TodoThreadSnapshot]:
    snapshots: list[_TodoThreadSnapshot] = []
    for path in sorted((todo_root / "threads").glob("*/todos.json")):
        try:
            snapshots.append(_parse_thread_snapshot(read_json(path)))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("todo_poke_state_file_skipped", path=str(path), error=str(exc))
    return snapshots


def _fingerprint(
    snapshot: _TodoThreadSnapshot,
    actionable_items: tuple[_TodoItemSnapshot, ...],
) -> str:
    serialized_items = [
        {
            "id": item.item_id,
            "title": item.title,
            "priority": item.priority,
            "depends_on": sorted(item.depends_on),
            "assigned_agent": item.assigned_agent,
            "updated_at": item.updated_at.isoformat(),
        }
        for item in sorted(actionable_items, key=lambda item: item.item_id)
    ]
    payload = {
        "items": serialized_items,
        "thread_total_count": len(snapshot.items),
        "thread_terminal_count": sum(item.status in TERMINAL_STATUSES for item in snapshot.items),
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _poke_scopes(snapshots: list[_TodoThreadSnapshot]) -> list[_TodoPokeScope]:
    scopes: list[_TodoPokeScope] = []
    for snapshot in snapshots:
        items_by_agent: dict[str, list[_TodoItemSnapshot]] = {}
        for item in snapshot.items:
            if item.item_id not in snapshot.actionable_item_ids or not item.assigned_agent:
                continue
            items_by_agent.setdefault(item.assigned_agent, []).append(item)

        for assigned_agent in sorted(items_by_agent):
            actionable_items = tuple(items_by_agent[assigned_agent])
            scopes.append(
                _TodoPokeScope(
                    assigned_agent=assigned_agent,
                    room_id=snapshot.room_id,
                    thread_id=snapshot.thread_id,
                    actionable_items=actionable_items,
                    latest_actionable_update=max(item.updated_at for item in actionable_items),
                    fingerprint=_fingerprint(snapshot, actionable_items),
                ),
            )
    return scopes


def _scope_key(scope: _TodoPokeScope) -> str:
    canonical = json.dumps(
        [scope.assigned_agent, scope.room_id, scope.thread_id],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _poke_record(state: Mapping[str, Any], scope: _TodoPokeScope) -> _PokeRecord | None:
    scopes = state.get("scopes")
    if not isinstance(scopes, dict):
        return None
    raw_record = scopes.get(_scope_key(scope))
    if not isinstance(raw_record, dict):
        return None
    last_poked_at = raw_record.get("last_poked_at")
    last_fingerprint = raw_record.get("last_fingerprint")
    if not isinstance(last_poked_at, int | float) or not isinstance(last_fingerprint, str):
        return None
    return _PokeRecord(last_poked_at=float(last_poked_at), last_fingerprint=last_fingerprint)


def _persist_poke(todo_root: Path, scope: _TodoPokeScope, now_timestamp: float) -> None:
    path = todo_root / _POKE_STATE_FILENAME

    def update(data: dict[str, Any]) -> None:
        scopes = data.setdefault("scopes", {})
        if not isinstance(scopes, dict):
            msg = "todo poke scopes state must be an object"
            raise TypeError(msg)
        scopes[_scope_key(scope)] = {
            "assigned_agent": scope.assigned_agent,
            "room_id": scope.room_id,
            "thread_id": scope.thread_id,
            "last_poked_at": now_timestamp,
            "last_fingerprint": scope.fingerprint,
        }

    locked_update_json(path, update)


def _format_poke_message(scope: _TodoPokeScope) -> str:
    lines = [f"@{scope.assigned_agent} Todo work is ready. Continue with these actionable items:"]
    ordered_items = sorted(
        scope.actionable_items,
        key=lambda item: (_PRIORITY_ORDER.get(item.priority, 9), item.item_id),
    )
    lines.extend(f"- `{item.item_id}` [{item.priority}] {item.title}" for item in ordered_items[:_VISIBLE_ITEM_LIMIT])
    remaining = len(ordered_items) - _VISIBLE_ITEM_LIMIT
    if remaining > 0:
        lines.append(f"- …and {remaining} more actionable item(s).")
    return "\n".join(lines)


def _quiet_period_elapsed(scope: _TodoPokeScope, now: datetime, quiet_seconds: float) -> bool:
    return (now - scope.latest_actionable_update).total_seconds() >= quiet_seconds


def _idle_quiet_scopes(
    todo_root: Path,
    policy: TodoPokePolicy,
    deps: TodoPokeDeps,
    now: datetime,
) -> list[_TodoPokeScope]:
    return [
        scope
        for scope in _poke_scopes(_read_thread_snapshots(todo_root))
        if _quiet_period_elapsed(scope, now, policy.quiet_seconds) and deps.idle_check(scope.assigned_agent)
    ]


async def _pending_schedules_by_room(
    scopes: list[_TodoPokeScope],
    schedule_query: _TodoScheduleQuery,
) -> dict[str, frozenset[str | None]] | None:
    pending_by_room: dict[str, frozenset[str | None]] = {}
    for room_id in sorted({scope.room_id for scope in scopes}):
        try:
            pending_threads = await schedule_query(room_id)
        except Exception as exc:
            logger.warning(
                "todo_poke_schedule_query_failed",
                room_id=room_id,
                error=str(exc),
                exc_info=True,
            )
            pending_threads = frozenset()
        if pending_threads is None:
            logger.debug("todo_poke_scan_skipped_runtime_unavailable", room_id=room_id)
            return None
        pending_by_room[room_id] = pending_threads
    return pending_by_room


def _dedup_allows_poke(
    scope: _TodoPokeScope,
    poke_state: Mapping[str, Any],
    policy: TodoPokePolicy,
    now_timestamp: float,
) -> bool:
    previous = _poke_record(poke_state, scope)
    if previous is None:
        return True
    if previous.last_fingerprint == scope.fingerprint:
        return False
    return now_timestamp - previous.last_poked_at >= policy.cooldown_seconds


async def _deliver_pokes(
    scopes: list[_TodoPokeScope],
    pending_by_room: Mapping[str, frozenset[str | None]],
    poke_state: dict[str, Any],
    todo_root: Path,
    policy: TodoPokePolicy,
    deps: TodoPokeDeps,
    sender: _TodoPokeSender,
    now_timestamp: float,
) -> int:
    delivered = 0
    for scope in scopes:
        if scope.thread_id in pending_by_room[scope.room_id]:
            continue
        if not _dedup_allows_poke(scope, poke_state, policy, now_timestamp):
            continue
        if not deps.idle_check(scope.assigned_agent):
            continue

        event_id = await sender(scope.room_id, _format_poke_message(scope), scope.thread_id)
        if event_id is None:
            continue

        _persist_poke(todo_root, scope, now_timestamp)
        poke_state.setdefault("scopes", {})[_scope_key(scope)] = {
            "last_poked_at": now_timestamp,
            "last_fingerprint": scope.fingerprint,
        }
        delivered += 1
        if delivered >= policy.max_pokes_per_scan:
            break
    return delivered


async def scan_todo_pokes(policy: TodoPokePolicy, deps: TodoPokeDeps) -> int:
    """Scan native todo state once and return the number of delivered pokes."""
    schedule_query = deps.schedule_query
    sender = deps.sender
    if schedule_query is None or sender is None or policy.max_pokes_per_scan <= 0:
        return 0

    now = deps.clock().astimezone(UTC)
    todo_root = deps.state_root()
    scopes = _idle_quiet_scopes(todo_root, policy, deps, now)
    if not scopes:
        return 0

    pending_by_room = await _pending_schedules_by_room(scopes, schedule_query)
    if pending_by_room is None:
        return 0

    poke_state = read_json(todo_root / _POKE_STATE_FILENAME)
    return await _deliver_pokes(
        scopes,
        pending_by_room,
        poke_state,
        todo_root,
        policy,
        deps,
        sender,
        now.timestamp(),
    )


@dataclass
class TodoPokeWorker:
    """Sleep-first background loop for native todo scans."""

    policy: TodoPokePolicy
    deps: TodoPokeDeps
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def stop(self) -> None:
        """Request graceful shutdown of the worker loop."""
        self._stop_event.set()

    async def run(self) -> None:
        """Run todo scans at the configured interval until stopped."""
        if self.policy.interval_seconds <= 0:
            return
        while not self._stop_event.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.policy.interval_seconds,
                )
            if self._stop_event.is_set():
                return
            try:
                await scan_todo_pokes(self.policy, self.deps)
            except Exception:
                logger.exception("todo_poke_scan_failed")
