"""Tests for the native todo auto-poke scanner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.custom_tools import todo_poke as todo_poke_module
from mindroom.custom_tools.todo_poke import (
    TodoPokeDeps,
    TodoPokePolicy,
    TodoPokeWorker,
    scan_todo_pokes,
    todo_poke_policy,
)
from mindroom.entity_resolution import current_entity_id
from mindroom.matrix.mentions import parse_mentions_in_text
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

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
    invalid_item = _item("invalid")
    invalid_item["title"] = "   "
    _write_thread(
        todo_root,
        "b-valid",
        items=[
            invalid_item,
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
@pytest.mark.parametrize(
    "invalid_kind",
    ["duplicate-id", "naive-updated-at", "overflow-updated-at"],
)
async def test_scan_skips_duplicate_or_invalid_timestamp_items(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    """A duplicate identity or invalid timestamp invalidates only the affected item."""
    todo_root = tmp_path / "todo"
    valid_item = _item("ready", title="Valid item")
    if invalid_kind == "duplicate-id":
        invalid_item = _item("ready", title="Duplicate item")
    elif invalid_kind == "naive-updated-at":
        invalid_item = _item("invalid", title="Naive item")
        invalid_item["updated_at"] = "2026-07-18T11:50:00"
    else:
        invalid_item = _item("invalid", title="Overflow item")
        invalid_item["updated_at"] = "9999-12-31T23:00:00-05:00"
    _write_thread(todo_root, "scope", items=[valid_item, invalid_item])
    deps, _queried_rooms, sent = _deps(todo_root)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == 1
    assert "`ready` [medium] `Valid item`" in sent[0][1]
    assert "Duplicate item" not in sent[0][1]
    assert "Naive item" not in sent[0][1]
    assert "Overflow item" not in sent[0][1]


@pytest.mark.asyncio
async def test_dependency_on_skipped_malformed_item_stays_blocked(tmp_path: Path) -> None:
    """A dependent must not become actionable when its malformed dependency is skipped."""
    todo_root = tmp_path / "todo"
    malformed_dependency = _item("dependency")
    malformed_dependency["updated_at"] = "2026-07-18T11:50:00"
    _write_thread(
        todo_root,
        "scope",
        items=[
            malformed_dependency,
            _item("dependent", depends_on=["dependency"]),
            _item("healthy"),
        ],
    )
    deps, _queried_rooms, sent = _deps(todo_root)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == 1
    assert "`healthy`" in sent[0][1]
    assert "dependent" not in sent[0][1]


@pytest.mark.asyncio
async def test_scan_rejects_unsafe_assignee_identifiers_before_idle_check(tmp_path: Path) -> None:
    """Persisted assignees that could alter the mention line never reach runtime callbacks."""
    todo_root = tmp_path / "todo"
    _write_thread(
        todo_root,
        "scope",
        items=[
            _item("unsafe", assigned_agent="code @reviewer"),
            _item("safe", assigned_agent="code"),
        ],
    )
    idle_checks: list[str] = []

    def idle_check(agent_name: str) -> bool:
        idle_checks.append(agent_name)
        return True

    deps, _queried_rooms, sent = _deps(
        todo_root,
        idle_check=idle_check,
    )

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == 1
    assert idle_checks == ["code", "code"]
    assert "@code Todo work is ready" in sent[0][1]
    assert "unsafe" not in sent[0][1]


@pytest.mark.asyncio
async def test_poke_titles_cannot_inject_agent_team_or_matrix_mentions(tmp_path: Path) -> None:
    """Only the intentional assignee mention may enter dispatch mention parsing."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="Code"),
                "reviewer": AgentConfig(display_name="Reviewer"),
            },
            teams={
                "dev": TeamConfig(
                    display_name="Dev",
                    role="Develop",
                    agents=["reviewer"],
                ),
            },
        ),
        runtime_paths,
    )
    todo_root = runtime_paths.storage_root / "todo"
    _write_thread(
        todo_root,
        "scope",
        items=[
            _item("agent", title="Ask @reviewer"),
            _item("team", title="Ask @dev"),
            _item("matrix", title="Ask @outside:localhost"),
        ],
    )
    deps, _queried_rooms, sent = _deps(todo_root)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == 1

    body = sent[0][1]
    _plain_text, mentioned_user_ids, _markdown_text = parse_mentions_in_text(body, config, runtime_paths)
    assert mentioned_user_ids == [current_entity_id("code", runtime_paths).full_id]
    assert "`Ask @\u200breviewer`" in body
    assert "`Ask @\u200bdev`" in body
    assert "`Ask @\u200boutside:localhost`" in body


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "`plain`"),
        ("inside`tick", "``inside`tick``"),
        ("inside``ticks", "```inside``ticks```"),
        ("`leading", "`` `leading ``"),
        ("trailing`", "`` trailing` ``"),
    ],
    ids=["plain", "fence-growth", "double-fence-growth", "leading-edge", "trailing-edge"],
)
def test_literal_code_text_uses_safe_fences_and_edge_padding(text: str, expected: str) -> None:
    """Literal todo text must grow its fence and pad ambiguous backtick edges."""
    assert todo_poke_module._literal_code_text(text) == expected


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
async def test_future_updated_at_cannot_indefinitely_suppress_scope(tmp_path: Path) -> None:
    """A clock-skewed future item update must fail open at the quiet gate."""
    todo_root = tmp_path / "todo"
    _write_thread(
        todo_root,
        "future",
        items=[_item("future", updated_at=_NOW + timedelta(days=365))],
    )
    deps, queried_rooms, sent = _deps(todo_root)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=300), deps) == 1
    assert queried_rooms == ["!room:localhost"]
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_quiet_gate_allows_immediate_handoff_for_freshly_unblocked_old_item(tmp_path: Path) -> None:
    """Completing a dependency does not delay an already-quiet item that becomes actionable."""
    todo_root = tmp_path / "todo"
    _write_thread(
        todo_root,
        "scope",
        items=[
            _item("dependency", status="done", updated_at=_NOW),
            _item("ready", depends_on=["dependency"], updated_at=_NOW - timedelta(minutes=10)),
        ],
    )
    deps, queried_rooms, sent = _deps(todo_root)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=300), deps) == 1
    assert queried_rooms == ["!room:localhost"]
    assert "`ready`" in sent[0][1]


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
    ("field", "value"),
    [("status", "done"), ("assigned_agent", "reviewer")],
)
async def test_scan_revalidates_scope_after_schedule_query(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    """Work completed or reassigned during schedule I/O must not receive a stale poke."""
    todo_root = tmp_path / "todo"
    path = _write_thread(todo_root, "scope")
    deps, queried_rooms, sent = _deps(todo_root)

    async def mutate_during_query(room_id: str) -> frozenset[str | None]:
        queried_rooms.append(room_id)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["items"][0][field] = value
        path.write_text(json.dumps(state), encoding="utf-8")
        return frozenset()

    deps = replace(deps, schedule_query=mutate_during_query)

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
async def test_scan_delivers_at_most_one_scope_per_agent(tmp_path: Path) -> None:
    """One scan must not enqueue multiple turns for the same otherwise-idle agent."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "a", room_id="!a:localhost")
    _write_thread(todo_root, "b", room_id="!b:localhost")
    deps, queried_rooms, sent = _deps(todo_root)
    policy = TodoPokePolicy(quiet_seconds=0)

    assert await scan_todo_pokes(policy, deps) == 1
    assert [room_id for room_id, _body, _thread_id in sent] == ["!a:localhost"]

    assert await scan_todo_pokes(policy, deps) == 1
    assert [room_id for room_id, _body, _thread_id in sent] == [
        "!a:localhost",
        "!b:localhost",
    ]
    assert queried_rooms == ["!a:localhost", "!b:localhost", "!b:localhost"]


@pytest.mark.asyncio
async def test_fingerprint_includes_hidden_items_and_cooldown_precedes_repoke(tmp_path: Path) -> None:
    """An unchanged scope stays suppressed normally, while hidden changes observe cooldown."""
    todo_root = tmp_path / "todo"
    path = _write_thread(todo_root, "scope", items=[_item(f"task-{index}") for index in range(6)])
    current_time = [_NOW]
    deps, queried_rooms, sent = _deps(todo_root, clock=lambda: current_time[0])
    policy = TodoPokePolicy(quiet_seconds=0, cooldown_seconds=300)

    assert await scan_todo_pokes(policy, deps) == 1
    assert "Task task-5" not in sent[0][1]
    assert "…and 1 more" in sent[0][1]
    assert await scan_todo_pokes(policy, deps) == 0
    assert queried_rooms == ["!room:localhost"]

    state = json.loads(path.read_text(encoding="utf-8"))
    state["items"][5]["title"] = "Changed hidden task"
    state["items"][5]["updated_at"] = (_NOW + timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")
    current_time[0] = _NOW + timedelta(seconds=299)

    assert await scan_todo_pokes(policy, deps) == 0
    assert queried_rooms == ["!room:localhost"]

    current_time[0] = _NOW + timedelta(seconds=300)
    assert await scan_todo_pokes(policy, deps) == 1
    assert queried_rooms == ["!room:localhost", "!room:localhost"]
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_future_dedup_timestamp_cannot_indefinitely_suppress_changed_work(tmp_path: Path) -> None:
    """A clock-skewed future dedup timestamp must fail open after the cooldown window."""
    todo_root = tmp_path / "todo"
    source_path = _write_thread(todo_root, "scope")
    deps, queried_rooms, sent = _deps(todo_root)
    policy = TodoPokePolicy(quiet_seconds=0, cooldown_seconds=300)

    assert await scan_todo_pokes(policy, deps) == 1
    poke_state_path = todo_root / "poke_state.json"
    poke_state = json.loads(poke_state_path.read_text(encoding="utf-8"))
    record = next(iter(poke_state["scopes"].values()))
    record["last_poked_at"] = (_NOW + timedelta(days=365)).timestamp()
    poke_state_path.write_text(json.dumps(poke_state), encoding="utf-8")

    thread_state = json.loads(source_path.read_text(encoding="utf-8"))
    thread_state["items"][0]["title"] = "Changed work"
    source_path.write_text(json.dumps(thread_state), encoding="utf-8")

    assert await scan_todo_pokes(policy, deps) == 1
    assert queried_rooms == ["!room:localhost", "!room:localhost"]
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_unchanged_fingerprint_retries_are_bounded_and_reset_after_change(tmp_path: Path) -> None:
    """Unchanged work gets three backstop retries, while changed work resets the counter."""
    todo_root = tmp_path / "todo"
    path = _write_thread(todo_root, "scope")
    current_time = [_NOW]
    deps, queried_rooms, sent = _deps(todo_root, clock=lambda: current_time[0])
    policy = TodoPokePolicy(quiet_seconds=0)

    assert await scan_todo_pokes(policy, deps) == 1
    current_time[0] = _NOW + timedelta(seconds=3599)
    assert await scan_todo_pokes(policy, deps) == 0
    assert queried_rooms == ["!room:localhost"]

    for retry_count in range(1, 4):
        current_time[0] = _NOW + timedelta(hours=retry_count)
        assert await scan_todo_pokes(policy, deps) == 1
        poke_state = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
        record = next(iter(poke_state["scopes"].values()))
        assert record["unchanged_repoke_count"] == retry_count

    current_time[0] = _NOW + timedelta(hours=4)
    assert await scan_todo_pokes(policy, deps) == 0

    thread_state = json.loads(path.read_text(encoding="utf-8"))
    thread_state["items"][0]["title"] = "Changed work"
    thread_state["items"][0]["updated_at"] = current_time[0].isoformat()
    path.write_text(json.dumps(thread_state), encoding="utf-8")
    assert await scan_todo_pokes(policy, deps) == 1

    poke_state = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    record = next(iter(poke_state["scopes"].values()))
    assert record["unchanged_repoke_count"] == 0
    assert len(queried_rooms) == 5
    assert len(sent) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_timestamp",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(True, id="bool"),
    ],
)
async def test_invalid_persisted_poke_timestamp_does_not_wedge_scope(
    tmp_path: Path,
    bad_timestamp: float | bool,
) -> None:
    """Non-finite and boolean dedup timestamps must be treated as absent records."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "scope")
    deps, _queried_rooms, sent = _deps(todo_root)
    policy = TodoPokePolicy(quiet_seconds=0)

    assert await scan_todo_pokes(policy, deps) == 1
    poke_state_path = todo_root / "poke_state.json"
    poke_state = json.loads(poke_state_path.read_text(encoding="utf-8"))
    record = next(iter(poke_state["scopes"].values()))
    record["last_poked_at"] = bad_timestamp
    poke_state_path.write_text(json.dumps(poke_state), encoding="utf-8")

    assert await scan_todo_pokes(policy, deps) == 1
    assert len(sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("counter_name", "bad_value"),
    [
        pytest.param("unchanged_repoke_count", -1, id="unchanged-negative"),
        pytest.param("unchanged_repoke_count", "3", id="unchanged-non-int"),
        pytest.param("unchanged_repoke_count", 1_000_000, id="unchanged-absurd"),
        pytest.param("send_failure_count", -1, id="send-failure-negative"),
        pytest.param("send_failure_count", "3", id="send-failure-non-int"),
        pytest.param("send_failure_count", 1_000_000, id="send-failure-absurd"),
    ],
)
async def test_invalid_persisted_poke_counter_is_recovered(
    tmp_path: Path,
    counter_name: str,
    bad_value: object,
) -> None:
    """Invalid persisted counters must be rejected and replaced by a valid record."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "scope")
    deps, _queried_rooms, sent = _deps(todo_root)
    policy = TodoPokePolicy(quiet_seconds=0)

    assert await scan_todo_pokes(policy, deps) == 1
    poke_state_path = todo_root / "poke_state.json"
    poke_state = json.loads(poke_state_path.read_text(encoding="utf-8"))
    record = next(iter(poke_state["scopes"].values()))
    record[counter_name] = bad_value
    poke_state_path.write_text(json.dumps(poke_state), encoding="utf-8")

    assert await scan_todo_pokes(policy, deps) == 1
    repaired_state = json.loads(poke_state_path.read_text(encoding="utf-8"))
    repaired_record = next(iter(repaired_state["scopes"].values()))
    assert repaired_record["unchanged_repoke_count"] == 0
    assert repaired_record["send_failure_count"] == 0
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_persist_failure_remembers_delivery_and_does_not_starve_later_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivered poke stays memory-deduped while later scopes and persistence continue."""
    todo_root = tmp_path / "todo"
    _write_thread(
        todo_root,
        "a",
        room_id="!a:localhost",
        items=[_item("a", assigned_agent="agent_a")],
    )
    _write_thread(
        todo_root,
        "b",
        room_id="!b:localhost",
        items=[_item("b", assigned_agent="agent_b")],
    )
    deps, _queried_rooms, sent = _deps(todo_root)
    real_persist = cast("Callable[..., None]", todo_poke_module._persist_poke)
    persist_attempts = 0

    def fail_first_persist(*args: object) -> None:
        nonlocal persist_attempts
        persist_attempts += 1
        if persist_attempts == 1:
            msg = "disk full"
            raise OSError(msg)
        real_persist(*args)

    monkeypatch.setattr(todo_poke_module, "_persist_poke", fail_first_persist)
    remembered_pokes = {}
    policy = TodoPokePolicy(quiet_seconds=0)

    assert await scan_todo_pokes(policy, deps, pending_poke_records=remembered_pokes) == 2
    assert len(sent) == 2
    assert len(remembered_pokes) == 1

    assert await scan_todo_pokes(policy, deps, pending_poke_records=remembered_pokes) == 0
    assert len(sent) == 2
    assert remembered_pokes == {}
    poke_state = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    assert len(poke_state["scopes"]) == 2


@pytest.mark.asyncio
async def test_delivery_cap_counts_attempts_and_persists_failed_send(tmp_path: Path) -> None:
    """Failed sends consume the attempt cap and persist their bounded-retry state."""
    todo_root = tmp_path / "todo"
    for index in range(5):
        _write_thread(
            todo_root,
            f"scope-{index}",
            room_id=f"!room-{index}:localhost",
            items=[_item(f"task-{index}", assigned_agent=f"agent_{index}")],
        )
    deps, _queried_rooms, sent = _deps(
        todo_root,
        send_results=[None, "$event-1", "$event-2", "$event-3", "$event-4"],
    )

    delivered = await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0, max_pokes_per_scan=3), deps)

    assert delivered == 2
    assert len(sent) == 3
    poke_state = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    assert len(poke_state["scopes"]) == 3
    failed_record = next(record for record in poke_state["scopes"].values() if record["room_id"] == "!room-0:localhost")
    assert failed_record["send_failure_count"] == 1

    retry_deps, _retry_queries, retry_sends = _deps(todo_root)
    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0, max_pokes_per_scan=3), retry_deps) == 3
    assert {entry[0] for entry in retry_sends} == {
        "!room-0:localhost",
        "!room-3:localhost",
        "!room-4:localhost",
    }


@pytest.mark.asyncio
async def test_consecutive_send_failures_are_bounded_and_reset(tmp_path: Path) -> None:
    """One fingerprint gets three failed retries before a change and success reset it."""
    todo_root = tmp_path / "todo"
    source_path = _write_thread(todo_root, "scope")
    deps, _queried_rooms, sent = _deps(
        todo_root,
        send_results=[None, None, None, None, None, "$event-success"],
    )
    policy = TodoPokePolicy(quiet_seconds=0)
    poke_state_path = todo_root / "poke_state.json"

    for failure_count in range(1, 5):
        assert await scan_todo_pokes(policy, deps) == 0
        poke_state = json.loads(poke_state_path.read_text(encoding="utf-8"))
        record = next(iter(poke_state["scopes"].values()))
        assert record["send_failure_count"] == failure_count

    assert await scan_todo_pokes(policy, deps) == 0
    assert len(sent) == 4

    thread_state = json.loads(source_path.read_text(encoding="utf-8"))
    thread_state["items"][0]["title"] = "Changed work"
    source_path.write_text(json.dumps(thread_state), encoding="utf-8")

    assert await scan_todo_pokes(policy, deps) == 0
    poke_state = json.loads(poke_state_path.read_text(encoding="utf-8"))
    record = next(iter(poke_state["scopes"].values()))
    assert record["send_failure_count"] == 1

    assert await scan_todo_pokes(policy, deps) == 1
    poke_state = json.loads(poke_state_path.read_text(encoding="utf-8"))
    record = next(iter(poke_state["scopes"].values()))
    assert record["send_failure_count"] == 0
    assert len(sent) == 6


@pytest.mark.asyncio
async def test_capped_send_failures_retry_after_backstop_window(tmp_path: Path) -> None:
    """A temporary outage cannot permanently mute unchanged actionable work."""
    todo_root = tmp_path / "todo"
    current_time = [_NOW]
    deps, _queried_rooms, sent = _deps(
        todo_root,
        clock=lambda: current_time[0],
        send_results=[None, None, None, None, "$event-recovered"],
    )
    _write_thread(todo_root, "scope")
    policy = TodoPokePolicy(quiet_seconds=0)

    for _attempt in range(4):
        assert await scan_todo_pokes(policy, deps) == 0

    current_time[0] = _NOW + timedelta(seconds=3599)
    assert await scan_todo_pokes(policy, deps) == 0
    assert len(sent) == 4

    current_time[0] = _NOW + timedelta(hours=1)
    assert await scan_todo_pokes(policy, deps) == 1
    poke_state = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    record = next(iter(poke_state["scopes"].values()))
    assert record["send_failure_count"] == 0
    assert record["last_send_failed_at"] == 0
    assert len(sent) == 5


@pytest.mark.asyncio
async def test_failed_scopes_move_behind_fresh_scopes_on_later_scans(tmp_path: Path) -> None:
    """Permanently failing sorted scopes must not starve healthy work behind the cap."""
    todo_root = tmp_path / "todo"
    for index in range(4):
        _write_thread(
            todo_root,
            f"scope-{index}",
            room_id=f"!room-{index}:localhost",
            items=[_item(f"task-{index}", assigned_agent=f"agent_{index}")],
        )
    remembered_failures: set[str] = set()
    first_deps, _first_queries, first_sends = _deps(
        todo_root,
        send_results=[None, None, None],
    )
    policy = TodoPokePolicy(quiet_seconds=0, max_pokes_per_scan=3)

    assert await scan_todo_pokes(policy, first_deps, failed_scope_keys=remembered_failures) == 0
    assert [room_id for room_id, _body, _thread_id in first_sends] == [
        "!room-0:localhost",
        "!room-1:localhost",
        "!room-2:localhost",
    ]

    second_deps, _second_queries, second_sends = _deps(todo_root)
    assert await scan_todo_pokes(policy, second_deps, failed_scope_keys=remembered_failures) == 3
    assert [room_id for room_id, _body, _thread_id in second_sends] == [
        "!room-3:localhost",
        "!room-0:localhost",
        "!room-1:localhost",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_state", [[], {"scopes": []}], ids=["root-list", "scopes-list"])
async def test_corrupt_poke_state_is_treated_as_empty(tmp_path: Path, bad_state: object) -> None:
    """Invalid poke-state container types must not disable scans or repeated persistence."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "scope")
    todo_root.mkdir(parents=True, exist_ok=True)
    (todo_root / "poke_state.json").write_text(json.dumps(bad_state), encoding="utf-8")
    deps, queried_rooms, sent = _deps(todo_root)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == 1
    assert queried_rooms == ["!room:localhost"]
    assert len(sent) == 1
    repaired = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    assert isinstance(repaired["scopes"], dict)
    assert len(repaired["scopes"]) == 1


@pytest.mark.asyncio
async def test_non_utf8_poke_state_is_treated_as_empty(tmp_path: Path) -> None:
    """Byte-corrupt poke state must be recovered by both the read and update boundaries."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "scope")
    todo_root.mkdir(parents=True, exist_ok=True)
    (todo_root / "poke_state.json").write_bytes(b"\xff\xfe")
    deps, queried_rooms, sent = _deps(todo_root)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == 1
    assert queried_rooms == ["!room:localhost"]
    assert len(sent) == 1
    repaired = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    assert len(repaired["scopes"]) == 1


@pytest.mark.asyncio
async def test_scan_prunes_poke_records_without_current_actionable_scope(tmp_path: Path) -> None:
    """Completed or removed todo scopes must not accumulate durable dedup records."""
    todo_root = tmp_path / "todo"
    completed_path = _write_thread(
        todo_root,
        "a",
        room_id="!a:localhost",
        items=[_item("a", assigned_agent="agent_a")],
    )
    _write_thread(
        todo_root,
        "b",
        room_id="!b:localhost",
        items=[_item("b", assigned_agent="agent_b")],
    )
    deps, queried_rooms, _sent = _deps(todo_root)
    policy = TodoPokePolicy(quiet_seconds=0)

    assert await scan_todo_pokes(policy, deps) == 2
    completed = json.loads(completed_path.read_text(encoding="utf-8"))
    completed["items"][0]["status"] = "done"
    completed_path.write_text(json.dumps(completed), encoding="utf-8")

    assert await scan_todo_pokes(policy, deps) == 0
    assert queried_rooms == ["!a:localhost", "!b:localhost"]
    poke_state = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    assert {record["room_id"] for record in poke_state["scopes"].values()} == {"!b:localhost"}


@pytest.mark.asyncio
async def test_transient_source_read_failure_preserves_poke_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temporary source I/O failure must not prune dedup state and cause a duplicate poke."""
    todo_root = tmp_path / "todo"
    source_path = _write_thread(todo_root, "scope")
    deps, _queried_rooms, sent = _deps(todo_root)
    policy = TodoPokePolicy(quiet_seconds=0)

    assert await scan_todo_pokes(policy, deps) == 1
    real_read_json = todo_poke_module.read_json
    fail_source_read = True

    def read_with_transient_failure(path: Path) -> dict[str, Any]:
        if fail_source_read and path == source_path:
            msg = "temporary read failure"
            raise OSError(msg)
        return real_read_json(path)

    monkeypatch.setattr(todo_poke_module, "read_json", read_with_transient_failure)

    assert await scan_todo_pokes(policy, deps) == 0
    poke_state = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    assert len(poke_state["scopes"]) == 1

    fail_source_read = False
    assert await scan_todo_pokes(policy, deps) == 0
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_transient_poke_state_read_failure_skips_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temporary dedup-state read failure must skip sends until state is readable."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "scope")
    deps, queried_rooms, sent = _deps(todo_root)
    policy = TodoPokePolicy(quiet_seconds=0)

    assert await scan_todo_pokes(policy, deps) == 1
    real_read_json = todo_poke_module.read_json
    poke_state_path = todo_root / "poke_state.json"
    fail_poke_state_read = True

    def read_with_transient_failure(path: Path) -> dict[str, Any]:
        if fail_poke_state_read and path == poke_state_path:
            msg = "temporary dedup read failure"
            raise OSError(msg)
        return real_read_json(path)

    monkeypatch.setattr(todo_poke_module, "read_json", read_with_transient_failure)

    assert await scan_todo_pokes(policy, deps) == 0
    assert len(sent) == 1
    assert queried_rooms == ["!room:localhost"]

    fail_poke_state_read = False
    assert await scan_todo_pokes(policy, deps) == 0
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_parse_invalid_source_still_prunes_poke_record(tmp_path: Path) -> None:
    """A parse-invalid source is observable state loss, so its obsolete record may be pruned."""
    todo_root = tmp_path / "todo"
    source_path = _write_thread(todo_root, "scope")
    deps, _queried_rooms, _sent = _deps(todo_root)
    policy = TodoPokePolicy(quiet_seconds=0)

    assert await scan_todo_pokes(policy, deps) == 1
    source_path.write_text("{bad json", encoding="utf-8")

    assert await scan_todo_pokes(policy, deps) == 0
    poke_state = json.loads((todo_root / "poke_state.json").read_text(encoding="utf-8"))
    assert poke_state["scopes"] == {}


@pytest.mark.asyncio
async def test_scan_offloads_all_blocking_storage_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot, dedup, revalidation, pruning, and persistence I/O run off the event loop."""
    todo_root = tmp_path / "todo"
    _write_thread(todo_root, "scope")
    deps, _queried_rooms, _sent = _deps(todo_root)
    real_to_thread = asyncio.to_thread
    offloaded: list[str] = []

    async def track_to_thread(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        offloaded.append(function.__name__)
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr("mindroom.custom_tools.todo_poke.asyncio.to_thread", track_to_thread)

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == 1
    assert offloaded == [
        "_read_thread_snapshots",
        "_prune_poke_state",
        "_read_poke_state",
        "_read_thread_snapshot",
        "_persist_poke",
    ]


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


def test_policy_rejects_subsecond_enabled_interval(tmp_path: Path) -> None:
    """An enabled interval below one second falls back while subsecond quiet remains valid."""
    runtime_paths = replace(
        test_runtime_paths(tmp_path),
        process_env={
            "MINDROOM_TODO_POKE_INTERVAL_SECONDS": "0.5",
            "MINDROOM_TODO_POKE_QUIET_SECONDS": "0.5",
        },
    )

    policy = todo_poke_policy(runtime_paths)

    assert policy.interval_seconds == 120
    assert policy.quiet_seconds == 0.5


@pytest.mark.parametrize(
    "env_name",
    ["MINDROOM_TODO_POKE_INTERVAL_SECONDS", "MINDROOM_TODO_POKE_QUIET_SECONDS"],
)
@pytest.mark.parametrize("bad_value", ["not-a-number", "-1", "nan", "inf", "-inf"])
def test_policy_rejects_invalid_second_overrides(tmp_path: Path, env_name: str, bad_value: str) -> None:
    """Invalid finite nonnegative-second overrides fall back to policy defaults."""
    runtime_paths = replace(test_runtime_paths(tmp_path), process_env={env_name: bad_value})

    policy = todo_poke_policy(runtime_paths)

    assert policy.interval_seconds == 120
    assert policy.quiet_seconds == 300


@pytest.mark.asyncio
async def test_worker_sleeps_first_continues_after_failure_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The loop sleeps first, survives a failed scan, and stops without waiting out the interval."""
    scan_continued = asyncio.Event()
    attempts = 0

    worker: TodoPokeWorker | None = None

    async def scan_side_effect(*_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            msg = "scan failed"
            raise RuntimeError(msg)
        assert worker is not None
        worker.stop()
        scan_continued.set()

    scan = AsyncMock(side_effect=scan_side_effect)
    monkeypatch.setattr("mindroom.custom_tools.todo_poke.scan_todo_pokes", scan)
    deps, _queries, _sent = _deps(tmp_path / "todo")
    worker = TodoPokeWorker(policy=TodoPokePolicy(interval_seconds=0.01), deps=deps)

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0)
    scan.assert_not_awaited()
    await asyncio.wait_for(scan_continued.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    assert scan.await_count == 2
    assert scan.await_args_list[0].kwargs["failed_scope_keys"] is worker._failed_scope_keys
    assert scan.await_args_list[1].kwargs["failed_scope_keys"] is worker._failed_scope_keys
    assert scan.await_args_list[0].kwargs["pending_poke_records"] is worker._pending_poke_records
    assert scan.await_args_list[1].kwargs["pending_poke_records"] is worker._pending_poke_records
