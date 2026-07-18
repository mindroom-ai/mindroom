"""Tests for the native todo auto-poke scanner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.custom_tools.todo_poke import (
    TodoPokeDeps,
    TodoPokePolicy,
    TodoPokeWorker,
    scan_todo_pokes,
    todo_poke_policy,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _item(
    item_id: str,
    *,
    assigned_agent: str = "code",
    title: str | None = None,
    status: str = "open",
    priority: str = "medium",
    depends_on: list[str] | None = None,
    updated_at: datetime = _NOW - timedelta(minutes=10),
) -> dict[str, object]:
    return {
        "id": item_id,
        "title": title or f"Task {item_id}",
        "status": status,
        "priority": priority,
        "depends_on": depends_on or [],
        "assigned_agent": assigned_agent,
        "created_at": updated_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "completed_at": None,
    }


def _write_thread(
    todo_root: Path,
    directory: str,
    *,
    room_id: str = "!room:localhost",
    thread_id: str = "$thread",
    items: list[dict[str, object]] | None = None,
) -> Path:
    path = todo_root / "threads" / directory / "todos.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "room_id": room_id,
                "thread_id": thread_id,
                "created_at": (_NOW - timedelta(hours=1)).isoformat(),
                "updated_at": (_NOW - timedelta(minutes=10)).isoformat(),
                "items": items or [_item(directory)],
            },
        ),
        encoding="utf-8",
    )
    return path


def _deps(
    todo_root: Path,
    *,
    clock: Callable[[], datetime] = lambda: _NOW,
    idle_check: Callable[[str], bool] = lambda _agent_name: True,
    schedule_result: frozenset[str | None] | None = frozenset(),
    schedule_error: Exception | None = None,
    send_results: list[str | None] | None = None,
) -> tuple[TodoPokeDeps, list[str], list[tuple[str, str, str | None]]]:
    queried_rooms: list[str] = []
    sent: list[tuple[str, str, str | None]] = []
    remaining_results = list(send_results) if send_results is not None else []

    async def schedule_query(room_id: str) -> frozenset[str | None] | None:
        queried_rooms.append(room_id)
        if schedule_error is not None:
            raise schedule_error
        return schedule_result

    async def sender(room_id: str, body: str, thread_id: str | None) -> str | None:
        sent.append((room_id, body, thread_id))
        if remaining_results:
            return remaining_results.pop(0)
        return f"$event-{len(sent)}"

    return (
        TodoPokeDeps(
            state_root=lambda: todo_root,
            schedule_query=schedule_query,
            idle_check=idle_check,
            sender=sender,
            clock=clock,
        ),
        queried_rooms,
        sent,
    )


@pytest.mark.asyncio
async def test_scan_skips_malformed_unassigned_and_unconfigured_items(tmp_path: Path) -> None:
    """One bad file or irrelevant assignee must not prevent valid work from being poked."""
    todo_root = tmp_path / "todo"
    malformed = todo_root / "threads" / "a-malformed" / "todos.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{bad json", encoding="utf-8")
    _write_thread(
        todo_root,
        "b-valid",
        items=[
            _item("unassigned", assigned_agent=""),
            _item("removed", assigned_agent="removed"),
            _item("ready", assigned_agent="code"),
            _item("blocked", depends_on=["ready"]),
        ],
    )
    idle_checks: list[str] = []

    def idle_check(agent_name: str) -> bool:
        idle_checks.append(agent_name)
        return agent_name == "code"

    deps, queried_rooms, sent = _deps(todo_root, idle_check=idle_check)

    delivered = await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps)

    assert delivered == 1
    assert queried_rooms == ["!room:localhost"]
    assert [entry[2] for entry in sent] == ["$thread"]
    assert "@code Todo work is ready" in sent[0][1]
    assert "`ready` [medium]" in sent[0][1]
    assert "blocked" not in sent[0][1]
    assert "removed" in idle_checks


@pytest.mark.asyncio
async def test_scan_applies_quiet_and_initial_idle_gates(tmp_path: Path) -> None:
    """Recently changed or currently busy scopes should not query schedules or send."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "recent", items=[_item("recent", updated_at=_NOW - timedelta(seconds=10))])
    quiet_deps, quiet_queries, quiet_sends = _deps(todo_root)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=300), quiet_deps) == 0
    assert quiet_queries == []
    assert quiet_sends == []

    busy_deps, busy_queries, busy_sends = _deps(todo_root, idle_check=lambda _agent_name: False)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), busy_deps) == 0
    assert busy_queries == []
    assert busy_sends == []


@pytest.mark.asyncio
async def test_scan_rechecks_idle_immediately_before_send(tmp_path: Path) -> None:
    """A scope that becomes busy during schedule lookup must not receive a poke."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "scope")
    idle_results = iter([True, False])
    deps, queried_rooms, sent = _deps(todo_root, idle_check=lambda _agent_name: next(idle_results))

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == 0
    assert queried_rooms == ["!room:localhost"]
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schedule_result", "schedule_error", "expected_deliveries"),
    [
        (frozenset({"$thread"}), None, 0),
        (None, None, 0),
        (frozenset(), RuntimeError("state unavailable"), 1),
    ],
    ids=["pending-schedule", "runtime-unavailable", "query-failure-fail-open"],
)
async def test_scan_schedule_failure_postures(
    tmp_path: Path,
    schedule_result: frozenset[str | None] | None,
    schedule_error: Exception | None,
    expected_deliveries: int,
) -> None:
    """Pending work suppresses a poke, startup skips, and read errors fail open."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "scope")
    deps, queried_rooms, sent = _deps(
        todo_root,
        schedule_result=schedule_result,
        schedule_error=schedule_error,
    )

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == expected_deliveries
    assert queried_rooms == ["!room:localhost"]
    assert len(sent) == expected_deliveries


@pytest.mark.asyncio
async def test_scan_normalizes_main_and_queries_each_room_once(tmp_path: Path) -> None:
    """The persisted main sentinel maps to None and multiple scopes share one room query."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "a-main", thread_id="main")
    _write_thread(todo_root, "b-thread", thread_id="$other")
    deps, queried_rooms, sent = _deps(todo_root, schedule_result=frozenset({None}))

    delivered = await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps)

    assert delivered == 1
    assert queried_rooms == ["!room:localhost"]
    assert [entry[2] for entry in sent] == ["$other"]


@pytest.mark.asyncio
async def test_fingerprint_includes_hidden_items_and_cooldown_precedes_repoke(tmp_path: Path) -> None:
    """An unchanged scope never repeats, while a hidden change re-arms only after cooldown."""
    todo_root = tmp_path / "todo"
    path = _write_thread(todo_root, "scope", items=[_item(f"task-{index}") for index in range(6)])
    current_time = [_NOW]
    deps, _queried_rooms, sent = _deps(todo_root, clock=lambda: current_time[0])
    policy = TodoPokePolicy(quiet_seconds=0, cooldown_seconds=300)

    assert await scan_todo_pokes(policy, deps) == 1
    assert "Task task-5" not in sent[0][1]
    assert "…and 1 more" in sent[0][1]
    assert await scan_todo_pokes(policy, deps) == 0

    state = json.loads(path.read_text(encoding="utf-8"))
    state["items"][5]["title"] = "Changed hidden task"
    state["items"][5]["updated_at"] = (_NOW + timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")
    current_time[0] = _NOW + timedelta(seconds=299)

    assert await scan_todo_pokes(policy, deps) == 0

    current_time[0] = _NOW + timedelta(seconds=300)
    assert await scan_todo_pokes(policy, deps) == 1
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_delivery_cap_counts_successes_and_failed_send_stays_retryable(tmp_path: Path) -> None:
    """Failed sends neither consume the cap nor persist their fingerprint."""
    todo_root = tmp_path / "todo"
    for index in range(5):
        _write_thread(
            todo_root,
            f"scope-{index}",
            room_id=f"!room-{index}:localhost",
            items=[_item(f"task-{index}")],
        )
    deps, _queried_rooms, sent = _deps(
        todo_root,
        send_results=[None, "$event-1", "$event-2", "$event-3", "$event-4"],
    )

    delivered = await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0, max_pokes_per_scan=3), deps)

    assert delivered == 3
    assert len(sent) == 4
    poke_state = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    assert len(poke_state["scopes"]) == 3
    assert "!room-0:localhost" not in {record["room_id"] for record in poke_state["scopes"].values()}

    retry_deps, _retry_queries, retry_sends = _deps(todo_root)
    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0, max_pokes_per_scan=3), retry_deps) == 2
    assert {entry[0] for entry in retry_sends} == {"!room-0:localhost", "!room-4:localhost"}


@pytest.mark.asyncio
async def test_missing_sender_or_querier_skips_whole_tick(tmp_path: Path) -> None:
    """Startup scans must do no partial work before both I/O collaborators exist."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "scope")
    deps, queried_rooms, sent = _deps(todo_root)

    no_sender = TodoPokeDeps(
        state_root=deps.state_root,
        schedule_query=deps.schedule_query,
        idle_check=deps.idle_check,
        sender=None,
        clock=deps.clock,
    )
    no_querier = TodoPokeDeps(
        state_root=deps.state_root,
        schedule_query=None,
        idle_check=deps.idle_check,
        sender=deps.sender,
        clock=deps.clock,
    )

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), no_sender) == 0
    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), no_querier) == 0
    assert queried_rooms == []
    assert sent == []


def test_policy_reads_interval_and_quiet_runtime_env(tmp_path: Path) -> None:
    """Runtime-scoped env values override timing, including zero disabling interval."""
    runtime_paths = replace(
        test_runtime_paths(tmp_path),
        process_env={
            "MINDROOM_TODO_POKE_INTERVAL_SECONDS": "0",
            "MINDROOM_TODO_POKE_QUIET_SECONDS": "12.5",
        },
    )

    policy = todo_poke_policy(runtime_paths)

    assert policy.interval_seconds == 0
    assert policy.quiet_seconds == 12.5
    assert policy.cooldown_seconds == 300
    assert policy.max_pokes_per_scan == 3


@pytest.mark.asyncio
async def test_worker_sleeps_first_continues_after_failure_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The loop sleeps first, survives a failed scan, and stops without waiting out the interval."""
    scan_continued = asyncio.Event()
    attempts = 0

    async def scan_side_effect(*_args: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            msg = "scan failed"
            raise RuntimeError(msg)
        scan_continued.set()

    scan = AsyncMock(side_effect=scan_side_effect)
    monkeypatch.setattr("mindroom.custom_tools.todo_poke.scan_todo_pokes", scan)
    deps, _queries, _sent = _deps(tmp_path / "todo")
    worker = TodoPokeWorker(policy=TodoPokePolicy(interval_seconds=0.01), deps=deps)

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0)
    scan.assert_not_awaited()
    await asyncio.wait_for(scan_continued.wait(), timeout=1)
    worker.stop()
    await asyncio.wait_for(task, timeout=1)

    assert scan.await_count == 2
