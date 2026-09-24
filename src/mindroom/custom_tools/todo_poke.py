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
from enum import Enum
from functools import cache
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
    "TodoPokeDeliveryUnavailableError",
    "TodoPokeDeps",
    "TodoPokePolicy",
    "TodoPokeRequesterKind",
    "TodoPokeWorker",
    "scan_todo_pokes",
    "todo_poke_policy",
]

type _TodoScheduleQuery = Callable[[str, tuple[str, ...]], Awaitable[frozenset[str | None] | None]]
type _TodoPokeSender = Callable[[str, str, str, str | None, str | None], Awaitable[str | None]]
type _StateWarningKey = tuple[str, str]
type _AgentThreadKey = tuple[str, str, str | None]

_VALID_STATUSES = {"open", *TERMINAL_STATUSES}
# Keep synchronized with config.main._AGENT_NAME_PATTERN without importing the config graph here.
_SAFE_ASSIGNEE_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
_POKE_STATE_FILENAME = "poke_state.json"
_VISIBLE_ITEM_LIMIT = 5
_RETRY_BACKSTOP_SECONDS = 60 * 60
_MAX_UNCHANGED_REPOKES = 3


class TodoPokeDeliveryUnavailableError(RuntimeError):
    """Signal that the runtime could not attempt a todo poke delivery."""


class TodoPokeRequesterKind(Enum):
    """Authority a todo poke may use for the requester recorded on an item."""

    HUMAN = "human"
    INTERNAL = "internal"
    UNSUPPORTED = "unsupported"


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

    state_root: Path
    schedule_query: _TodoScheduleQuery
    idle_check: Callable[[str], bool]
    sender: _TodoPokeSender
    requester_kind: Callable[[str], TodoPokeRequesterKind]
    clock: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class _TodoItemSnapshot:
    item_id: str
    title: str
    status: str
    priority: str
    depends_on: tuple[str, ...]
    assigned_agent: str
    requester_id: str | None
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
    assigned_agent: str
    # The human this poke acts for, or None when it runs as the assignee's own internal turn.
    requester_id: str | None
    room_id: str
    thread_id: str | None
    actionable_items: tuple[_TodoItemSnapshot, ...]
    latest_actionable_update: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _PokeRecord:
    """Dedup state for one scope: `last_poked_at` is the last send attempt, `last_fingerprint` the last delivered work."""

    last_poked_at: float
    last_fingerprint: str
    unchanged_repoke_count: int


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

    # LEGACY_COMPAT: Todo items without a recorded requester_id.
    # Legacy format: A native `todos.json` item with no `requester_id` key.
    # Last legacy release: v2026.9.289 wrote every item without a requester; replacement: the next release records the title author's `requester_id` on every item it writes.
    # Handling: The item keeps the authority every release through v2026.9.289 poked it with, the assignee's own internal turn, and shares the internal poke scope and its unchanged dedup key; the todo tool records the current requester on its next write to the item.
    # Coverage: tests/test_todo_poke.py::test_scan_pokes_legacy_items_as_assignee_turn_with_existing_dedup_state, tests/test_todo_builtin.py::test_todo_write_records_requester_on_legacy_item.
    requester_id = _require_string(item_data, "requester_id") if "requester_id" in item_data else None

    return _TodoItemSnapshot(
        item_id=_require_string(item_data, "id"),
        title=title,
        status=status,
        priority=priority,
        depends_on=tuple(raw_dependencies),
        assigned_agent=_require_string(item_data, "assigned_agent", allow_empty=True),
        requester_id=requester_id,
        updated_at=_parse_updated_at(_require_string(item_data, "updated_at")),
    )


def _warn_state_once(
    event: str,
    source_path: Path,
    error: str,
    seen_warning_keys: set[_StateWarningKey],
    **context: object,
) -> None:
    warning_key = (str(source_path), error)
    if warning_key in seen_warning_keys:
        return
    seen_warning_keys.add(warning_key)
    logger.warning(event, path=str(source_path), error=error, **context)


def _parse_thread_snapshot(
    data: object,
    source_path: Path,
    seen_warning_keys: set[_StateWarningKey],
) -> _TodoThreadSnapshot:
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
            _warn_state_once(
                "todo_poke_item_skipped",
                source_path,
                str(exc),
                seen_warning_keys,
                item_index=index,
            )
            continue
        if item.item_id in item_ids:
            skipped_item_ids.add(item.item_id)
            _warn_state_once(
                "todo_poke_item_skipped",
                source_path,
                f"duplicate todo item id: {item.item_id}",
                seen_warning_keys,
                item_index=index,
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


def _read_thread_snapshots(
    todo_root: Path,
    seen_warning_keys: set[_StateWarningKey],
) -> _TodoSnapshotBatch:
    threads_root = todo_root / "threads"
    # Enumerate explicitly: Path.glob would swallow enumeration OSErrors and report an empty store,
    # which downstream pruning would treat as "all scopes completed" and wipe the dedup state.
    try:
        thread_dirs = sorted(entry for entry in threads_root.iterdir() if entry.is_dir())
    except FileNotFoundError:
        return _TodoSnapshotBatch((), had_io_failure=False)
    except OSError as exc:
        _warn_state_once("todo_poke_state_dir_unreadable", threads_root, str(exc), seen_warning_keys)
        return _TodoSnapshotBatch((), had_io_failure=True)

    snapshots: list[_TodoThreadSnapshot] = []
    had_io_failure = False
    for path in (thread_dir / "todos.json" for thread_dir in thread_dirs):
        try:
            snapshot = _parse_thread_snapshot(read_json(path), path, seen_warning_keys)
        except OSError as exc:
            _warn_state_once("todo_poke_state_file_skipped", path, str(exc), seen_warning_keys)
            had_io_failure = True
        except (KeyError, TypeError, ValueError) as exc:
            _warn_state_once("todo_poke_state_file_skipped", path, str(exc), seen_warning_keys)
        else:
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


def _poke_scopes(
    snapshots: list[_TodoThreadSnapshot],
    seen_warning_keys: set[_StateWarningKey],
    requester_kind: Callable[[str], TodoPokeRequesterKind],
) -> list[_TodoPokeScope]:
    scopes: list[_TodoPokeScope] = []
    classify_requester = cache(requester_kind)
    for snapshot in snapshots:
        items_by_owner: dict[tuple[str, str | None], list[_TodoItemSnapshot]] = {}
        for item in snapshot.items:
            if item.item_id not in snapshot.actionable_item_ids or not item.assigned_agent:
                continue
            if _SAFE_ASSIGNEE_PATTERN.fullmatch(item.assigned_agent) is None:
                _warn_state_once(
                    "todo_poke_assignee_skipped",
                    snapshot.source_path,
                    f"invalid assigned_agent: {item.assigned_agent}",
                    seen_warning_keys,
                    item_id=item.item_id,
                    assigned_agent=item.assigned_agent,
                )
                continue
            # A human's work pokes as that human so the assignee applies its access policy to them.
            # Work from internal senders and legacy work shares one poke as the assignee's own turn.
            poke_requester = None
            if item.requester_id is not None:
                kind = classify_requester(item.requester_id)
                if kind is TodoPokeRequesterKind.UNSUPPORTED:
                    _warn_state_once(
                        "todo_poke_requester_unsupported",
                        snapshot.source_path,
                        f"requester {item.requester_id} is neither a human nor an internal sender",
                        seen_warning_keys,
                        item_id=item.item_id,
                        assigned_agent=item.assigned_agent,
                    )
                    continue
                if kind is TodoPokeRequesterKind.HUMAN:
                    poke_requester = item.requester_id
            items_by_owner.setdefault((item.assigned_agent, poke_requester), []).append(item)

        for assigned_agent, requester_id in sorted(items_by_owner, key=lambda owner: (owner[0], owner[1] or "")):
            actionable_items = tuple(items_by_owner[assigned_agent, requester_id])
            scopes.append(
                _TodoPokeScope(
                    assigned_agent=assigned_agent,
                    requester_id=requester_id,
                    room_id=snapshot.room_id,
                    thread_id=snapshot.thread_id,
                    actionable_items=actionable_items,
                    latest_actionable_update=max(item.updated_at for item in actionable_items),
                    fingerprint=_fingerprint(snapshot, actionable_items),
                ),
            )
    return scopes


def _scope_key(scope: _TodoPokeScope) -> str:
    # Internal scopes keep the key every earlier release used, so their dedup state survives upgrades.
    key: list[str | None] = [scope.assigned_agent, scope.room_id, scope.thread_id]
    if scope.requester_id is not None:
        key.append(scope.requester_id)
    return json.dumps(key, separators=(",", ":"))


def _agent_thread_key(scope: _TodoPokeScope) -> _AgentThreadKey:
    return (scope.assigned_agent, scope.room_id, scope.thread_id)


def _poke_record(state: Mapping[str, Any], scope: _TodoPokeScope) -> _PokeRecord | None:
    scopes = state.get("scopes")
    if not isinstance(scopes, dict):
        return None
    raw_record = scopes.get(_scope_key(scope))
    if not isinstance(raw_record, dict):
        return None
    last_poked_at = raw_record.get("last_poked_at")
    last_fingerprint = raw_record.get("last_fingerprint")
    unchanged_repoke_count = raw_record.get("unchanged_repoke_count")
    if (
        isinstance(last_poked_at, bool)
        or not isinstance(last_poked_at, int | float)
        or not math.isfinite(last_poked_at)
        or not isinstance(last_fingerprint, str)
        or isinstance(unchanged_repoke_count, bool)
        or not isinstance(unchanged_repoke_count, int)
        or not 0 <= unchanged_repoke_count <= _MAX_UNCHANGED_REPOKES
    ):
        return None
    return _PokeRecord(
        last_poked_at=float(last_poked_at),
        last_fingerprint=last_fingerprint,
        unchanged_repoke_count=unchanged_repoke_count,
    )


def _previous_record(
    scope: _TodoPokeScope,
    poke_state: Mapping[str, Any],
    session_poke_records: Mapping[str, _PokeRecord],
) -> _PokeRecord | None:
    return session_poke_records.get(_scope_key(scope)) or _poke_record(poke_state, scope)


def _read_poke_state(todo_root: Path) -> dict[str, Any]:
    path = todo_root / _POKE_STATE_FILENAME
    try:
        raw_state: object = read_json(path)
    except ValueError as exc:
        logger.warning("todo_poke_dedup_state_reset", path=str(path), error=str(exc))
        return {}
    if not isinstance(raw_state, dict):
        logger.warning("todo_poke_dedup_state_reset", path=str(path), error="state root must be an object")
        return {}
    return raw_state


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
            "last_poked_at": record.last_poked_at,
            "last_fingerprint": record.last_fingerprint,
            "unchanged_repoke_count": record.unchanged_repoke_count,
        }

    locked_update_json(path, update, recover_invalid=True)


async def _try_persist_poke(
    todo_root: Path,
    scope: _TodoPokeScope,
    record: _PokeRecord,
) -> None:
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


def _literal_code_text(text: str) -> str:
    safe_text = " ".join(text.split()).replace("@", "@\u200b")
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


async def _pending_schedules_by_room(
    scopes: list[_TodoPokeScope],
    schedule_query: _TodoScheduleQuery,
) -> dict[str, frozenset[str | None]]:
    pending_by_room: dict[str, frozenset[str | None]] = {}
    for room_id, agent_names in sorted(_agent_names_by_room(scopes).items()):
        try:
            pending_threads = await schedule_query(room_id, agent_names)
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
            continue
        pending_by_room[room_id] = pending_threads
    return pending_by_room


def _agent_names_by_room(scopes: list[_TodoPokeScope]) -> dict[str, tuple[str, ...]]:
    names_by_room: dict[str, set[str]] = {}
    for scope in scopes:
        names_by_room.setdefault(scope.room_id, set()).add(scope.assigned_agent)
    return {room_id: tuple(sorted(agent_names)) for room_id, agent_names in names_by_room.items()}


def _dedup_allows_poke(
    scope: _TodoPokeScope,
    poke_state: Mapping[str, Any],
    session_poke_records: Mapping[str, _PokeRecord],
    policy: TodoPokePolicy,
    now_timestamp: float,
) -> bool:
    previous = _previous_record(scope, poke_state, session_poke_records)
    if previous is None:
        return True
    if previous.last_fingerprint != scope.fingerprint:
        return _period_elapsed(
            now_timestamp,
            previous.last_poked_at,
            policy.cooldown_seconds,
        )
    return previous.unchanged_repoke_count < _MAX_UNCHANGED_REPOKES and _period_elapsed(
        now_timestamp,
        previous.last_poked_at,
        _RETRY_BACKSTOP_SECONDS,
    )


def _one_scope_per_agent_thread(
    eligible_scopes: list[_TodoPokeScope],
    all_scopes: list[_TodoPokeScope],
    poke_state: Mapping[str, Any],
    session_poke_records: Mapping[str, _PokeRecord],
    policy: TodoPokePolicy,
    now_timestamp: float,
) -> list[_TodoPokeScope]:
    """Poke an agent thread at most once per cooldown however many requesters wrote its work."""
    cooling_threads: set[_AgentThreadKey] = set()
    for scope in all_scopes:
        previous = _previous_record(scope, poke_state, session_poke_records)
        if previous is not None and not _period_elapsed(now_timestamp, previous.last_poked_at, policy.cooldown_seconds):
            cooling_threads.add(_agent_thread_key(scope))

    def last_attempt(scope: _TodoPokeScope) -> float:
        previous = _previous_record(scope, poke_state, session_poke_records)
        return previous.last_poked_at if previous is not None else -math.inf

    # The least recently poked requester goes first so one requester's churn cannot starve another's work.
    chosen: dict[_AgentThreadKey, _TodoPokeScope] = {}
    for scope in eligible_scopes:
        thread_key = _agent_thread_key(scope)
        if thread_key in cooling_threads:
            continue
        current = chosen.get(thread_key)
        if current is None or last_attempt(scope) < last_attempt(current):
            chosen[thread_key] = scope
    return list(chosen.values())


def _record_after_attempt(
    scope: _TodoPokeScope,
    previous: _PokeRecord | None,
    now_timestamp: float,
    *,
    delivered: bool,
) -> _PokeRecord:
    if not delivered:
        # Keep the last delivered fingerprint so the retry is throttled by the cooldown, not the backstop.
        return _PokeRecord(
            last_poked_at=now_timestamp,
            last_fingerprint=previous.last_fingerprint if previous is not None else "",
            unchanged_repoke_count=previous.unchanged_repoke_count if previous is not None else 0,
        )
    return _PokeRecord(
        last_poked_at=now_timestamp,
        last_fingerprint=scope.fingerprint,
        unchanged_repoke_count=(
            previous.unchanged_repoke_count + 1
            if previous is not None and previous.last_fingerprint == scope.fingerprint
            else 0
        ),
    )


async def _deliver_pokes(
    scopes: list[_TodoPokeScope],
    pending_by_room: Mapping[str, frozenset[str | None]],
    poke_state: Mapping[str, Any],
    todo_root: Path,
    policy: TodoPokePolicy,
    deps: TodoPokeDeps,
    session_poke_records: dict[str, _PokeRecord],
) -> int:
    delivered = 0
    attempts = 0
    poked_agents: set[str] = set()
    for scope in scopes:
        if attempts >= policy.max_pokes_per_scan:
            break
        if scope.assigned_agent in poked_agents:
            continue
        pending_threads = pending_by_room.get(scope.room_id)
        if pending_threads is None or scope.thread_id in pending_threads:
            continue

        scope_key = _scope_key(scope)
        previous = _previous_record(scope, poke_state, session_poke_records)
        try:
            event_id = await deps.sender(
                scope.assigned_agent,
                scope.room_id,
                _format_poke_message(scope),
                scope.thread_id,
                scope.requester_id,
            )
        except TodoPokeDeliveryUnavailableError:
            logger.debug(
                "todo_poke_delivery_skipped_runtime_unavailable",
                assigned_agent=scope.assigned_agent,
                room_id=scope.room_id,
            )
            continue
        attempts += 1
        outcome_timestamp = deps.clock().astimezone(UTC).timestamp()
        record = _record_after_attempt(scope, previous, outcome_timestamp, delivered=event_id is not None)
        session_poke_records[scope_key] = record
        await _try_persist_poke(todo_root, scope, record)
        if event_id is not None:
            poked_agents.add(scope.assigned_agent)
            delivered += 1
    return delivered


async def scan_todo_pokes(
    policy: TodoPokePolicy,
    deps: TodoPokeDeps,
    *,
    session_poke_records: dict[str, _PokeRecord] | None = None,
    seen_warning_keys: set[_StateWarningKey] | None = None,
) -> int:
    """Scan native todo state once and return the number of delivered pokes."""
    remembered_pokes = session_poke_records if session_poke_records is not None else {}
    remembered_warnings = seen_warning_keys if seen_warning_keys is not None else set()
    todo_root = deps.state_root
    snapshot_batch = await asyncio.to_thread(_read_thread_snapshots, todo_root, remembered_warnings)
    try:
        all_scopes = _poke_scopes(list(snapshot_batch.snapshots), remembered_warnings, deps.requester_kind)
    except TodoPokeDeliveryUnavailableError:
        logger.debug("todo_poke_scan_skipped_runtime_unavailable")
        return 0
    active_scope_keys = frozenset(_scope_key(scope) for scope in all_scopes)
    if not snapshot_batch.had_io_failure:
        for stale_scope_key in remembered_pokes.keys() - active_scope_keys:
            del remembered_pokes[stale_scope_key]
        await asyncio.to_thread(_prune_poke_state, todo_root, active_scope_keys)
    try:
        poke_state = await asyncio.to_thread(_read_poke_state, todo_root)
    except OSError as exc:
        logger.warning(
            "todo_poke_dedup_state_unavailable",
            path=str(todo_root / _POKE_STATE_FILENAME),
            error=str(exc),
        )
        return 0

    now_timestamp = deps.clock().astimezone(UTC).timestamp()
    eligible_scopes = [
        scope
        for scope in all_scopes
        if _period_elapsed(now_timestamp, scope.latest_actionable_update.timestamp(), policy.quiet_seconds)
        and deps.idle_check(scope.assigned_agent)
        and _dedup_allows_poke(scope, poke_state, remembered_pokes, policy, now_timestamp)
    ]
    scopes = _one_scope_per_agent_thread(
        eligible_scopes,
        all_scopes,
        poke_state,
        remembered_pokes,
        policy,
        now_timestamp,
    )
    if not scopes:
        return 0

    pending_by_room = await _pending_schedules_by_room(scopes, deps.schedule_query)

    return await _deliver_pokes(
        scopes,
        pending_by_room,
        poke_state,
        todo_root,
        policy,
        deps,
        remembered_pokes,
    )


@dataclass
class TodoPokeWorker:
    """Sleep-first background loop for native todo scans."""

    policy: TodoPokePolicy
    deps: TodoPokeDeps
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _session_poke_records: dict[str, _PokeRecord] = field(default_factory=dict, init=False)
    _seen_warning_keys: set[_StateWarningKey] = field(default_factory=set, init=False)

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
                    session_poke_records=self._session_poke_records,
                    seen_warning_keys=self._seen_warning_keys,
                )
            except Exception:
                logger.exception("todo_poke_scan_failed")
