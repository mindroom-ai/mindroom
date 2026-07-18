"""Native scanner that wakes idle agents with actionable assigned todos.

Ad-hoc team activity outside configured team bots is not visible to idle checks and can result in one extra serialized turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from mindroom.custom_tools.todo_state import (
    TERMINAL_STATUSES,
    NoWriteResult,
    is_actionable,
    locked_update_json,
    no_write,
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
_SAFE_ASSIGNEE_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
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
    schedule_query: _TodoScheduleQuery
    idle_check: Callable[[str], bool]
    sender: _TodoPokeSender
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
    source_path: Path
    room_id: str
    thread_id: str | None
    items: tuple[_TodoItemSnapshot, ...]
    actionable_item_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _TodoPokeScope:
    source_path: Path
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

    title = _require_string(item_data, "title").strip()
    if not title:
        msg = "title must be a non-empty string"
        raise ValueError(msg)

    return _TodoItemSnapshot(
        item_id=_require_string(item_data, "id"),
        title=title,
        status=status,
        priority=priority,
        depends_on=tuple(raw_dependencies),
        assigned_agent=_require_string(item_data, "assigned_agent", allow_empty=True),
        updated_at=_parse_updated_at(_require_string(item_data, "updated_at")),
    )


def _parse_thread_snapshot(data: object, source_path: Path) -> _TodoThreadSnapshot:
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

    parsed_items: list[_TodoItemSnapshot] = []
    item_ids: set[str] = set()
    skipped_item_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        try:
            item = _parse_item(raw_item)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(raw_item, dict):
                skipped_item_id = cast("dict[str, Any]", raw_item).get("id")
                if isinstance(skipped_item_id, str) and skipped_item_id:
                    skipped_item_ids.add(skipped_item_id)
            logger.warning(
                "todo_poke_item_skipped",
                path=str(source_path),
                item_index=index,
                error=str(exc),
            )
            continue
        if item.item_id in item_ids:
            skipped_item_ids.add(item.item_id)
            logger.warning(
                "todo_poke_item_skipped",
                path=str(source_path),
                item_index=index,
                error=f"duplicate todo item id: {item.item_id}",
            )
            continue
        parsed_items.append(item)
        item_ids.add(item.item_id)
    items = tuple(parsed_items)

    items_by_id = {
        item.item_id: {
            "status": item.status,
            "depends_on": item.depends_on,
        }
        for item in items
    }
    actionable_item_ids = frozenset(
        item.item_id
        for item in items
        if not skipped_item_ids.intersection(item.depends_on)
        and all(dependency_id in items_by_id for dependency_id in item.depends_on)
        and is_actionable(items_by_id[item.item_id], items_by_id)
    )
    return _TodoThreadSnapshot(
        source_path=source_path,
        room_id=room_id,
        thread_id=thread_id,
        items=items,
        actionable_item_ids=actionable_item_ids,
    )


def _read_thread_snapshot(path: Path) -> _TodoThreadSnapshot | None:
    try:
        return _parse_thread_snapshot(read_json(path), path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("todo_poke_state_file_skipped", path=str(path), error=str(exc))
        return None


def _read_thread_snapshots(todo_root: Path) -> list[_TodoThreadSnapshot]:
    return [
        snapshot
        for path in sorted((todo_root / "threads").glob("*/todos.json"))
        if (snapshot := _read_thread_snapshot(path)) is not None
    ]


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
            if _SAFE_ASSIGNEE_PATTERN.fullmatch(item.assigned_agent) is None:
                logger.warning(
                    "todo_poke_assignee_skipped",
                    path=str(snapshot.source_path),
                    item_id=item.item_id,
                    assigned_agent=item.assigned_agent,
                )
                continue
            items_by_agent.setdefault(item.assigned_agent, []).append(item)

        for assigned_agent in sorted(items_by_agent):
            actionable_items = tuple(items_by_agent[assigned_agent])
            scopes.append(
                _TodoPokeScope(
                    source_path=snapshot.source_path,
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


def _validated_poke_state(raw_state: object) -> dict[str, Any]:
    if not isinstance(raw_state, dict):
        msg = "todo poke state root must be an object"
        raise TypeError(msg)
    state = cast("dict[str, Any]", raw_state)
    scopes = state.get("scopes")
    if scopes is not None and not isinstance(scopes, dict):
        msg = "todo poke scopes state must be an object"
        raise TypeError(msg)
    return state


def _read_poke_state(todo_root: Path) -> dict[str, Any]:
    path = todo_root / _POKE_STATE_FILENAME
    try:
        state = _validated_poke_state(read_json(path))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("todo_poke_dedup_state_reset", path=str(path), error=str(exc))
        return {}
    else:
        return state


def _prune_poke_state(todo_root: Path, active_scope_keys: frozenset[str]) -> None:
    path = todo_root / _POKE_STATE_FILENAME
    if not path.exists():
        return

    def update(data: dict[str, Any]) -> NoWriteResult | None:
        scopes = data.get("scopes")
        if not isinstance(scopes, dict):
            data["scopes"] = {}
            return None
        stale_keys = scopes.keys() - active_scope_keys
        if not stale_keys:
            return no_write(None)
        for key in stale_keys:
            del scopes[key]
        return None

    locked_update_json(path, update, recover_invalid=True)


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

    locked_update_json(path, update, recover_invalid=True)


def _literal_code_text(text: str) -> str:
    safe_text = text.replace("@", "@\u200b")
    fence = "`"
    while fence in safe_text:
        fence += "`"
    if safe_text.startswith("`") or safe_text.endswith("`"):
        safe_text = f" {safe_text} "
    return f"{fence}{safe_text}{fence}"


def _format_poke_message(scope: _TodoPokeScope) -> str:
    lines = [f"@{scope.assigned_agent} Todo work is ready. Continue with these actionable items:"]
    ordered_items = sorted(
        scope.actionable_items,
        key=lambda item: (_PRIORITY_ORDER.get(item.priority, 9), item.item_id),
    )
    lines.extend(
        f"- {_literal_code_text(item.item_id)} [{item.priority}] {_literal_code_text(item.title)}"
        for item in ordered_items[:_VISIBLE_ITEM_LIMIT]
    )
    remaining = len(ordered_items) - _VISIBLE_ITEM_LIMIT
    if remaining > 0:
        lines.append(f"- …and {remaining} more actionable item(s).")
    return "\n".join(lines)


def _quiet_period_elapsed(scope: _TodoPokeScope, now: datetime, quiet_seconds: float) -> bool:
    return (now - scope.latest_actionable_update).total_seconds() >= quiet_seconds


def _idle_quiet_scopes(
    scopes: list[_TodoPokeScope],
    policy: TodoPokePolicy,
    deps: TodoPokeDeps,
    now: datetime,
) -> list[_TodoPokeScope]:
    return [
        scope
        for scope in scopes
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
    todo_root: Path,
    policy: TodoPokePolicy,
    deps: TodoPokeDeps,
    now_timestamp: float,
    failed_scope_keys: set[str],
) -> int:
    delivered = 0
    attempts = 0
    for scope in scopes:
        if attempts >= policy.max_pokes_per_scan:
            break
        if scope.thread_id in pending_by_room[scope.room_id]:
            continue

        refreshed_snapshot = await asyncio.to_thread(_read_thread_snapshot, scope.source_path)
        if refreshed_snapshot is None:
            continue
        refreshed_scope = next(
            (
                candidate
                for candidate in _poke_scopes([refreshed_snapshot])
                if _scope_key(candidate) == _scope_key(scope)
            ),
            None,
        )
        if refreshed_scope is None or refreshed_scope.fingerprint != scope.fingerprint:
            continue
        if not deps.idle_check(refreshed_scope.assigned_agent):
            continue

        attempts += 1
        refreshed_scope_key = _scope_key(refreshed_scope)
        event_id = await deps.sender(
            refreshed_scope.room_id,
            _format_poke_message(refreshed_scope),
            refreshed_scope.thread_id,
        )
        if event_id is None:
            failed_scope_keys.add(refreshed_scope_key)
            continue

        failed_scope_keys.discard(refreshed_scope_key)
        await asyncio.to_thread(_persist_poke, todo_root, refreshed_scope, now_timestamp)
        delivered += 1
    return delivered


async def scan_todo_pokes(
    policy: TodoPokePolicy,
    deps: TodoPokeDeps,
    *,
    failed_scope_keys: set[str] | None = None,
) -> int:
    """Scan native todo state once and return the number of delivered pokes."""
    remembered_failures = failed_scope_keys if failed_scope_keys is not None else set()
    now = deps.clock().astimezone(UTC)
    todo_root = deps.state_root()
    snapshots = await asyncio.to_thread(_read_thread_snapshots, todo_root)
    all_scopes = _poke_scopes(snapshots)
    poke_state = await asyncio.to_thread(_read_poke_state, todo_root)
    active_scope_keys = frozenset(_scope_key(scope) for scope in all_scopes)
    remembered_failures.intersection_update(active_scope_keys)
    await asyncio.to_thread(_prune_poke_state, todo_root, active_scope_keys)

    scopes = sorted(
        (
            scope
            for scope in _idle_quiet_scopes(all_scopes, policy, deps, now)
            if _dedup_allows_poke(scope, poke_state, policy, now.timestamp())
        ),
        key=lambda scope: _scope_key(scope) in remembered_failures,
    )
    if not scopes:
        return 0

    pending_by_room = await _pending_schedules_by_room(scopes, deps.schedule_query)
    if pending_by_room is None:
        return 0

    return await _deliver_pokes(
        scopes,
        pending_by_room,
        todo_root,
        policy,
        deps,
        now.timestamp(),
        remembered_failures,
    )


@dataclass
class TodoPokeWorker:
    """Sleep-first background loop for native todo scans."""

    policy: TodoPokePolicy
    deps: TodoPokeDeps
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _failed_scope_keys: set[str] = field(default_factory=set, init=False)

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
                await scan_todo_pokes(
                    self.policy,
                    self.deps,
                    failed_scope_keys=self._failed_scope_keys,
                )
            except Exception:
                logger.exception("todo_poke_scan_failed")
