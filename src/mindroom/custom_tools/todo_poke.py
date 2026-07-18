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
    PRIORITY_ORDER,
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

_VALID_STATUSES = {"open", *TERMINAL_STATUSES}
# Keep synchronized with config.main._AGENT_NAME_PATTERN without importing the config graph here.
_SAFE_ASSIGNEE_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
_POKE_STATE_FILENAME = "poke_state.json"
_VISIBLE_ITEM_LIMIT = 5
_RETRY_BACKSTOP_SECONDS = 60 * 60
_MAX_UNCHANGED_REPOKES = 3
_MAX_CONSECUTIVE_SEND_FAILURES = _MAX_UNCHANGED_REPOKES + 1


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
class _TodoSnapshotBatch:
    snapshots: tuple[_TodoThreadSnapshot, ...]
    had_io_failure: bool


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
    last_send_failed_at: float
    last_fingerprint: str
    unchanged_repoke_count: int
    send_failure_count: int


def _env_seconds(
    runtime_paths: RuntimePaths,
    name: str,
    default: float,
    *,
    minimum_enabled_seconds: float = 0,
) -> float:
    raw_value = runtime_paths.env_value(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        logger.warning("todo_poke_env_value_invalid", env_name=name, value=raw_value, default=default)
        return default
    if value < 0 or not math.isfinite(value) or 0 < value < minimum_enabled_seconds:
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
            minimum_enabled_seconds=1,
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
    try:
        updated_at = datetime.fromisoformat(value)
        if updated_at.tzinfo is None:
            msg = "updated_at must include a timezone"
            raise ValueError(msg)
        return updated_at.astimezone(UTC)
    except OverflowError as exc:
        msg = "updated_at is outside the supported datetime range"
        raise ValueError(msg) from exc


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
    if priority not in PRIORITY_ORDER:
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
    # Scanner snapshots keep dependents blocked when their persisted dependency was skipped or is missing.
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
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("todo_poke_state_file_skipped", path=str(path), error=str(exc))
        return None


def _read_thread_snapshots(todo_root: Path) -> _TodoSnapshotBatch:
    snapshots: list[_TodoThreadSnapshot] = []
    had_io_failure = False
    for path in sorted((todo_root / "threads").glob("*/todos.json")):
        try:
            snapshot = _read_thread_snapshot(path)
        except OSError as exc:
            logger.warning("todo_poke_state_file_skipped", path=str(path), error=str(exc))
            had_io_failure = True
            continue
        if snapshot is not None:
            snapshots.append(snapshot)
    return _TodoSnapshotBatch(tuple(snapshots), had_io_failure)


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
    last_send_failed_at = raw_record.get("last_send_failed_at")
    last_fingerprint = raw_record.get("last_fingerprint")
    unchanged_repoke_count = raw_record.get("unchanged_repoke_count")
    send_failure_count = raw_record.get("send_failure_count")
    if (
        isinstance(last_poked_at, bool)
        or not isinstance(last_poked_at, int | float)
        or not math.isfinite(last_poked_at)
        or isinstance(last_send_failed_at, bool)
        or not isinstance(last_send_failed_at, int | float)
        or not math.isfinite(last_send_failed_at)
        or not isinstance(last_fingerprint, str)
        or isinstance(unchanged_repoke_count, bool)
        or not isinstance(unchanged_repoke_count, int)
        or not 0 <= unchanged_repoke_count <= _MAX_UNCHANGED_REPOKES
        or isinstance(send_failure_count, bool)
        or not isinstance(send_failure_count, int)
        or not 0 <= send_failure_count <= _MAX_CONSECUTIVE_SEND_FAILURES
    ):
        return None
    return _PokeRecord(
        last_poked_at=float(last_poked_at),
        last_send_failed_at=float(last_send_failed_at),
        last_fingerprint=last_fingerprint,
        unchanged_repoke_count=unchanged_repoke_count,
        send_failure_count=send_failure_count,
    )


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
    except (TypeError, ValueError) as exc:
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


def _persist_poke(todo_root: Path, scope: _TodoPokeScope, record: _PokeRecord) -> None:
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
            "last_poked_at": record.last_poked_at,
            "last_send_failed_at": record.last_send_failed_at,
            "last_fingerprint": record.last_fingerprint,
            "unchanged_repoke_count": record.unchanged_repoke_count,
            "send_failure_count": record.send_failure_count,
        }

    locked_update_json(path, update, recover_invalid=True)


async def _try_persist_poke(
    todo_root: Path,
    scope: _TodoPokeScope,
    record: _PokeRecord,
) -> bool:
    try:
        await asyncio.to_thread(_persist_poke, todo_root, scope, record)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(
            "todo_poke_persistence_failed",
            assigned_agent=scope.assigned_agent,
            room_id=scope.room_id,
            thread_id=scope.thread_id,
            error=str(exc),
            exc_info=True,
        )
        return False
    return True


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
        key=lambda item: (PRIORITY_ORDER.get(item.priority, 9), item.item_id),
    )
    lines.extend(
        f"- {_literal_code_text(item.item_id)} [{item.priority}] {_literal_code_text(item.title)}"
        for item in ordered_items[:_VISIBLE_ITEM_LIMIT]
    )
    remaining = len(ordered_items) - _VISIBLE_ITEM_LIMIT
    if remaining > 0:
        lines.append(f"- …and {remaining} more actionable item(s).")
    return "\n".join(lines)


def _period_elapsed(now_timestamp: float, previous_timestamp: float, period_seconds: float) -> bool:
    elapsed_seconds = now_timestamp - previous_timestamp
    # Future skew beyond the period is not a trustworthy activity signal.
    return elapsed_seconds >= period_seconds or elapsed_seconds <= -period_seconds


def _quiet_period_elapsed(scope: _TodoPokeScope, now: datetime, quiet_seconds: float) -> bool:
    return _period_elapsed(
        now.timestamp(),
        scope.latest_actionable_update.timestamp(),
        quiet_seconds,
    )


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
    pending_poke_records: Mapping[str, _PokeRecord],
    policy: TodoPokePolicy,
    now_timestamp: float,
) -> bool:
    previous = pending_poke_records.get(_scope_key(scope)) or _poke_record(poke_state, scope)
    if previous is None:
        return True
    if previous.last_fingerprint != scope.fingerprint:
        if previous.send_failure_count > 0:
            return True
        return _period_elapsed(
            now_timestamp,
            previous.last_poked_at,
            policy.cooldown_seconds,
        )
    if previous.send_failure_count > 0:
        return previous.send_failure_count < _MAX_CONSECUTIVE_SEND_FAILURES or _period_elapsed(
            now_timestamp,
            previous.last_send_failed_at,
            _RETRY_BACKSTOP_SECONDS,
        )
    return previous.unchanged_repoke_count < _MAX_UNCHANGED_REPOKES and _period_elapsed(
        now_timestamp,
        previous.last_poked_at,
        _RETRY_BACKSTOP_SECONDS,
    )


def _record_after_delivery(
    scope: _TodoPokeScope,
    previous: _PokeRecord | None,
    now_timestamp: float,
) -> _PokeRecord:
    unchanged_repoke_count = (
        previous.unchanged_repoke_count + 1
        if previous is not None and previous.last_fingerprint == scope.fingerprint and previous.last_poked_at > 0
        else 0
    )
    return _PokeRecord(
        last_poked_at=now_timestamp,
        last_send_failed_at=0,
        last_fingerprint=scope.fingerprint,
        unchanged_repoke_count=unchanged_repoke_count,
        send_failure_count=0,
    )


def _record_after_send_failure(
    scope: _TodoPokeScope,
    previous: _PokeRecord | None,
    now_timestamp: float,
) -> _PokeRecord:
    if previous is None or previous.last_fingerprint != scope.fingerprint:
        return _PokeRecord(
            last_poked_at=0,
            last_send_failed_at=now_timestamp,
            last_fingerprint=scope.fingerprint,
            unchanged_repoke_count=0,
            send_failure_count=1,
        )
    return _PokeRecord(
        # Failed attempts do not advance the last successful-delivery time.
        last_poked_at=previous.last_poked_at,
        last_send_failed_at=now_timestamp,
        last_fingerprint=scope.fingerprint,
        unchanged_repoke_count=previous.unchanged_repoke_count,
        send_failure_count=min(
            previous.send_failure_count + 1,
            _MAX_CONSECUTIVE_SEND_FAILURES,
        ),
    )


async def _repair_pending_poke_records(
    todo_root: Path,
    scopes_by_key: Mapping[str, _TodoPokeScope],
    pending_poke_records: dict[str, _PokeRecord],
) -> None:
    for scope_key in sorted(pending_poke_records.keys() & scopes_by_key.keys()):
        record = pending_poke_records[scope_key]
        if await _try_persist_poke(todo_root, scopes_by_key[scope_key], record):
            pending_poke_records.pop(scope_key, None)


async def _refreshed_scope_for_delivery(scope: _TodoPokeScope) -> _TodoPokeScope | None:
    try:
        refreshed_snapshot = await asyncio.to_thread(_read_thread_snapshot, scope.source_path)
    except OSError as exc:
        logger.warning(
            "todo_poke_state_file_skipped",
            path=str(scope.source_path),
            error=str(exc),
        )
        return None
    if refreshed_snapshot is None:
        return None
    refreshed_scope = next(
        (candidate for candidate in _poke_scopes([refreshed_snapshot]) if _scope_key(candidate) == _scope_key(scope)),
        None,
    )
    if refreshed_scope is None or refreshed_scope.fingerprint != scope.fingerprint:
        return None
    return refreshed_scope


async def _deliver_pokes(
    scopes: list[_TodoPokeScope],
    pending_by_room: Mapping[str, frozenset[str | None]],
    poke_state: Mapping[str, Any],
    todo_root: Path,
    policy: TodoPokePolicy,
    deps: TodoPokeDeps,
    now_timestamp: float,
    failed_scope_keys: set[str],
    pending_poke_records: dict[str, _PokeRecord],
) -> int:
    delivered = 0
    attempts = 0
    poked_agents: set[str] = set()
    for scope in scopes:
        if attempts >= policy.max_pokes_per_scan:
            break
        if scope.assigned_agent in poked_agents:
            continue
        if scope.thread_id in pending_by_room[scope.room_id]:
            continue

        refreshed_scope = await _refreshed_scope_for_delivery(scope)
        if refreshed_scope is None:
            continue
        if not deps.idle_check(refreshed_scope.assigned_agent):
            continue

        attempts += 1
        refreshed_scope_key = _scope_key(refreshed_scope)
        previous = pending_poke_records.get(refreshed_scope_key) or _poke_record(
            poke_state,
            refreshed_scope,
        )
        event_id = await deps.sender(
            refreshed_scope.room_id,
            _format_poke_message(refreshed_scope),
            refreshed_scope.thread_id,
        )
        if event_id is None:
            failed_scope_keys.add(refreshed_scope_key)
            record = _record_after_send_failure(refreshed_scope, previous, now_timestamp)
            pending_poke_records[refreshed_scope_key] = record
            if await _try_persist_poke(todo_root, refreshed_scope, record):
                pending_poke_records.pop(refreshed_scope_key, None)
            continue

        poked_agents.add(refreshed_scope.assigned_agent)
        failed_scope_keys.discard(refreshed_scope_key)
        record = _record_after_delivery(refreshed_scope, previous, now_timestamp)
        pending_poke_records[refreshed_scope_key] = record
        if await _try_persist_poke(todo_root, refreshed_scope, record):
            pending_poke_records.pop(refreshed_scope_key, None)
        delivered += 1
    return delivered


async def scan_todo_pokes(
    policy: TodoPokePolicy,
    deps: TodoPokeDeps,
    *,
    failed_scope_keys: set[str] | None = None,
    pending_poke_records: dict[str, _PokeRecord] | None = None,
) -> int:
    """Scan native todo state once and return the number of delivered pokes."""
    remembered_failures = failed_scope_keys if failed_scope_keys is not None else set()
    remembered_pokes = pending_poke_records if pending_poke_records is not None else {}
    now = deps.clock().astimezone(UTC)
    todo_root = deps.state_root()
    snapshot_batch = await asyncio.to_thread(_read_thread_snapshots, todo_root)
    all_scopes = _poke_scopes(list(snapshot_batch.snapshots))
    active_scope_keys = frozenset(_scope_key(scope) for scope in all_scopes)
    scopes_by_key = {_scope_key(scope): scope for scope in all_scopes}
    if not snapshot_batch.had_io_failure:
        remembered_failures.intersection_update(active_scope_keys)
        for stale_scope_key in remembered_pokes.keys() - active_scope_keys:
            del remembered_pokes[stale_scope_key]
        await asyncio.to_thread(_prune_poke_state, todo_root, active_scope_keys)
    await _repair_pending_poke_records(todo_root, scopes_by_key, remembered_pokes)
    try:
        poke_state = await asyncio.to_thread(_read_poke_state, todo_root)
    except OSError as exc:
        logger.warning(
            "todo_poke_dedup_state_unavailable",
            path=str(todo_root / _POKE_STATE_FILENAME),
            error=str(exc),
        )
        return 0

    scopes = sorted(
        (
            scope
            for scope in _idle_quiet_scopes(all_scopes, policy, deps, now)
            if _dedup_allows_poke(
                scope,
                poke_state,
                remembered_pokes,
                policy,
                now.timestamp(),
            )
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
        poke_state,
        todo_root,
        policy,
        deps,
        now.timestamp(),
        remembered_failures,
        remembered_pokes,
    )


@dataclass
class TodoPokeWorker:
    """Sleep-first background loop for native todo scans."""

    policy: TodoPokePolicy
    deps: TodoPokeDeps
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _failed_scope_keys: set[str] = field(default_factory=set, init=False)
    _pending_poke_records: dict[str, _PokeRecord] = field(default_factory=dict, init=False)

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
                    pending_poke_records=self._pending_poke_records,
                )
            except Exception:
                logger.exception("todo_poke_scan_failed")
