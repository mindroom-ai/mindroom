"""The steward-published room state clients use to choose a conversation view and recognize MindRoom accounts."""

from __future__ import annotations

import asyncio
import gc
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import nio
import pytest
from structlog.testing import capture_logs

from mindroom import room_modes
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.commands.thread_mode_commands import handle_thread_mode_command
from mindroom.config.agent import AgentConfig, RoomConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import AdmissionFacts, RoomMembershipPosition
from mindroom.matrix import room_thread_mode
from mindroom.matrix.room_accounts import reconcile_room_accounts, reconcile_room_router_binding
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration import rooms as rooms_module
from mindroom.orchestration.room_state import RoomStateWorker
from mindroom.orchestration.rooms import RoomSetup
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.room_modes import change_room_thread_mode
from mindroom.room_thread_modes import (
    clear_room_thread_mode_override,
    resolve_room_thread_mode_override,
    set_room_thread_mode_override,
)
from mindroom.steward_duties import StewardDuties, StewardDutiesDeps
from tests.access_schema_support import member_event
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    install_bots,
    install_runtime_journal_support,
    make_config_reload_lifecycle,
    published_config_load,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids
from tests.test_bot_ready_hook import _validated_reported_membership_admission

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID

EVENT_TYPE = "io.mindroom.thread_mode"
ACCOUNTS_EVENT_TYPE = "io.mindroom.accounts"
ROUTER_EVENT_TYPE = "io.mindroom.router"
ROOM_ID = "!room:localhost"

type RoomState = tuple[Config, RuntimePaths, dict[str, MatrixID], dict[tuple[str, str], dict[str, str]], AsyncMock]


@pytest.fixture
def room_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RoomState:
    """Keep a mutable homeserver and use nominal delays unless a test supplies its own jitter draws."""
    monkeypatch.setattr("mindroom.orchestration.room_state.uniform", lambda low, high: min(low * 2, high))
    config = Config(
        agents={
            "agent1": AgentConfig(display_name="Agent 1", rooms=[ROOM_ID], thread_mode="room"),
            "agent2": AgentConfig(display_name="Agent 2", rooms=[ROOM_ID], thread_mode="thread"),
        },
    )
    paths = test_runtime_paths(tmp_path)
    ids = entity_ids(config, paths)
    events = {
        ("m.room.member", ids[name].full_id): {"membership": "join"} for name in (ROUTER_AGENT_NAME, "agent1", "agent2")
    }
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = ids[ROUTER_AGENT_NAME].full_id
    room = nio.MatrixRoom(ROOM_ID, client.user_id)
    for name in (ROUTER_AGENT_NAME, "agent1", "agent2"):
        room.add_member(ids[name].full_id, None, None)
    client.rooms = {ROOM_ID: room}

    async def read(room_id: str) -> nio.RoomGetStateResponse:
        await asyncio.sleep(0)
        return nio.RoomGetStateResponse(
            [
                {"type": event_type, "state_key": key, "content": dict(content)}
                for (event_type, key), content in events.items()
            ],
            room_id,
        )

    async def write(
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> nio.RoomPutStateResponse:
        events[event_type, state_key] = dict(content)
        return nio.RoomPutStateResponse("$state", room_id)

    async def read_event(room_id: str, event_type: str, state_key: str = "") -> object:
        await asyncio.sleep(0)
        if event_type == "m.room.power_levels":
            return nio.RoomGetStateEventResponse({"users": {"@admin:localhost": 100}}, event_type, state_key, room_id)
        content = events.get((event_type, state_key))
        if content is None:
            return nio.RoomGetStateEventError("Absent", "M_NOT_FOUND", room_id=room_id)
        return nio.RoomGetStateEventResponse(dict(content), event_type, state_key, room_id)

    async def joined_members(room_id: str) -> nio.JoinedMembersResponse:
        return nio.JoinedMembersResponse(
            [
                nio.RoomMember(key, None, None)
                for (kind, key), content in events.items()
                if kind == "m.room.member" and content.get("membership") == "join"
            ],
            room_id,
        )

    client.room_get_state_event.side_effect = read_event
    client.joined_members.side_effect = joined_members
    client.room_get_state.side_effect = read
    client.room_put_state.side_effect = write
    return config, paths, ids, events, client


def _sync_live_room(state: RoomState) -> None:
    """Project the fake homeserver's current membership and alias into nio's live room."""
    _, _, _, events, client = state
    room = nio.MatrixRoom(ROOM_ID, client.user_id)
    for (kind, key), content in events.items():
        if kind == "m.room.member" and content.get("membership") == "join":
            room.add_member(key, None, None)
    room.canonical_alias = events.get(("m.room.canonical_alias", ""), {}).get("alias")
    client.rooms[ROOM_ID] = room


def _writes(client: AsyncMock, event_type: str) -> list[Any]:
    """Return the router's state writes of one event type."""
    return [write for write in client.room_put_state.await_args_list if write.args[1] == event_type]


def _accounts(state: RoomState) -> list[str] | None:
    """Return the accounts the router published under its own state key, or None before it published any."""
    _, _, ids, events, _ = state
    content = events.get((ACCOUNTS_EVENT_TYPE, ids[ROUTER_AGENT_NAME].full_id))
    return None if content is None else content["accounts"]


@pytest.fixture
def worker_runtime(room_state: RoomState) -> MagicMock:
    """Supply a live runtime with a replaceable room steward."""
    config, paths, _, _, client = room_state
    runtime = MagicMock(config=config, runtime_paths=paths, running=True)
    runtime.response_admission_gate.closed = False
    runtime.room_steward.return_value = MagicMock(client=client)
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_due", [False, True])
async def test_review_worker_survives_replacement_admission_closure(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    retry_due: bool,
) -> None:
    """Marks and due retries survive a temporary replacement that has no after-reload sweep."""
    config, paths, _, events, client = room_state
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=config)
    orchestrator.running = True
    install_bots(orchestrator, {ROUTER_AGENT_NAME: _owner_bot(room_state)})
    lifecycle = make_config_reload_lifecycle(paths, response_admission_gate=orchestrator.response_admission_gate)
    worker = orchestrator.room_state
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    put = client.room_put_state.side_effect

    async def timeout_roster(room_id: str, kind: str, content: dict, **kwargs: object) -> object:
        if kind == ACCOUNTS_EVENT_TYPE:
            raise TimeoutError
        return await put(room_id, kind, content, **kwargs)

    async def during_replacement() -> None:
        nonlocal clock
        assert lifecycle.response_admission_gate.closed
        if retry_due:
            clock = 1
        else:
            worker.mark(ROOM_ID)
        client.room_put_state.reset_mock()
        await _yield_loop(40)
        client.room_put_state.assert_not_awaited()

    try:
        if retry_due:
            client.room_put_state.side_effect = timeout_roster
            worker.mark(ROOM_ID)
            await _yield_loop(40)
            assert len(_writes(client, ACCOUNTS_EVENT_TYPE)) == 1
            client.room_put_state.side_effect = put
        await lifecycle.apply_with_response_admission(
            during_replacement,
            operation_name="test MCP catalog replacement",
            request_is_current=lambda: True,
        )
        lifecycle.after_reload.assert_not_awaited()
        await _yield_loop(40)
        assert events[EVENT_TYPE, ""] == {"mode": "mixed"}
        assert _accounts(room_state) is not None
    finally:
        worker.stop()
        await _yield_loop(10)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["M_FORBIDDEN", "M_UNKNOWN"])
async def test_review_accounts_does_not_put_when_comparison_fails(
    room_state: RoomState,
    status: str,
) -> None:
    """An unreadable roster is not absent, and only a transient comparison should retry."""
    config, paths, _, _, client = room_state
    read = client.room_get_state_event.side_effect

    async def denied(room_id: str, kind: str, state_key: str = "") -> object:
        if kind == ACCOUNTS_EVENT_TYPE:
            return nio.RoomGetStateEventError("Failed comparison", status, room_id=room_id)
        return await read(room_id, kind, state_key)

    client.room_get_state_event.side_effect = denied
    with capture_logs() as logs:
        assert await reconcile_room_accounts(client, config, paths, ROOM_ID) is (status == "M_FORBIDDEN")
    client.room_put_state.assert_not_awaited()
    assert [log["log_level"] for log in logs] == ["debug" if status == "M_FORBIDDEN" else "warning"]


def _room_state_fleet(room_state: RoomState) -> tuple[_MultiAgentOrchestrator, AgentBot, AgentBot]:
    config, paths, ids, _, outgoing = room_state
    incoming = _successor_client(room_state)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=config)
    orchestrator.running = True
    bots = []
    for name, client, power in ((ROUTER_AGENT_NAME, outgoing, 100), ("agent1", incoming, 50)):
        bot = AgentBot(
            AgentMatrixUser(name, ids[name].full_id, name, TEST_PASSWORD),
            rooms=[],
            orchestrator=orchestrator,
        )
        bot.running = True
        room = nio.MatrixRoom(ROOM_ID, client.user_id)
        room.power_levels.users[client.user_id] = power
        client.rooms = {ROOM_ID: room}
        bot.client = client
        bots.append(install_runtime_journal_support(bot))
    install_bots(orchestrator, {bot.agent_name: bot for bot in bots})
    return orchestrator, bots[0], bots[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("drains_during_read", [False, True])
async def test_review_draining_steward_cannot_sign_through_its_client(
    room_state: RoomState,
    drains_during_read: bool,
) -> None:
    """A draining winner is ineligible for background PUTs even if it still holds the room."""
    _, _, _, events, outgoing = room_state
    orchestrator, owner, runner_up = _room_state_fleet(room_state)
    read = outgoing.room_get_state_event.side_effect

    async def drain(room_id: str, kind: str, state_key: str = "") -> object:
        if kind == EVENT_TYPE:
            owner._runtime_view.begin_draining()
        return await read(room_id, kind, state_key)

    if drains_during_read:
        outgoing.room_get_state_event.side_effect = drain
    else:
        owner._runtime_view.begin_draining()
    orchestrator.room_state.mark(ROOM_ID)
    assert await wait_for_background_tasks(timeout=2, owner=orchestrator.room_state)
    outgoing.room_put_state.assert_not_awaited()
    assert orchestrator.room_steward(ROOM_ID) is owner  # Foreground election is unchanged.
    assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": runner_up.agent_user.user_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("denied_read", ["joined_members", "lazy_roster", "m.room.member", EVENT_TYPE])
async def test_review_forbidden_reads_are_quiet_until_marked(
    room_state: RoomState,
    worker_runtime: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    denied_read: str,
) -> None:
    """A forbidden read does not write or retry that step, but the next mark checks again."""
    config, paths, _, _, client = room_state
    read = client.room_get_state_event.side_effect
    reads = 0
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)

    async def deny_joined(_room_id: str) -> object:
        nonlocal reads
        reads += 1
        return nio.JoinedMembersError("Denied", "M_FORBIDDEN")

    async def deny_event(room_id: str, kind: str, state_key: str = "") -> object:
        nonlocal reads
        if kind == denied_read:
            reads += 1
            return nio.RoomGetStateEventError("Denied", "M_FORBIDDEN", room_id=room_id)
        return await read(room_id, kind, state_key)

    if denied_read == "lazy_roster":
        for agent in config.agents.values():
            agent.rooms = []
    if denied_read in {"joined_members", "lazy_roster"}:
        client.joined_members.side_effect = deny_joined
    else:
        client.room_get_state_event.side_effect = deny_event
    worker = RoomStateWorker(worker_runtime)
    try:
        with capture_logs() as logs:
            worker.mark(ROOM_ID)
            await _yield_loop(40)
            initial = reads
            assert initial > 0
            clock = 2
            await _yield_loop(40)
            assert reads == initial
            if denied_read != "joined_members":
                assert not _writes(client, EVENT_TYPE)
            if denied_read in {"joined_members", "lazy_roster"}:
                assert not _writes(client, ACCOUNTS_EVENT_TYPE)
            worker.mark(ROOM_ID)
            await _yield_loop(40)
            assert reads > initial
        assert {log["log_level"] for log in logs} <= {"debug"}
        if denied_read != "joined_members":
            assert await reconcile_room_thread_mode(client, config, paths, ROOM_ID) is False
    finally:
        worker.stop()
        await _yield_loop(10)


@pytest.mark.asyncio
async def test_review_leaving_while_waiting_on_mode_lock_does_not_log_traceback(room_state: RoomState) -> None:
    """A vanished room after the compare lock wait is a normal departure, not an exception."""
    config, paths, _, _, client = room_state
    key = (paths.storage_root, ROOM_ID)
    lock = asyncio.Lock()
    room_thread_mode._LOCKS[key] = lock
    async with lock:
        task = asyncio.create_task(reconcile_room_thread_mode(client, config, paths, ROOM_ID))
        await asyncio.sleep(0)
        assert not task.done()
        del client.rooms[ROOM_ID]
    with capture_logs() as logs:
        await task
    assert not any(log.get("exc_info") for log in logs)
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_non_timeout_failure_warns_on_first_attempt(
    room_state: RoomState,
    worker_runtime: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected server response warns immediately, then logs at debug until recovery."""
    _, _, _, _, client = room_state
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    put = client.room_put_state.side_effect

    async def unavailable(room_id: str, kind: str, content: dict, **kwargs: object) -> object:
        if kind == ACCOUNTS_EVENT_TYPE:
            return nio.RoomPutStateError("Unavailable", "M_UNKNOWN")
        return await put(room_id, kind, content, **kwargs)

    client.room_put_state.side_effect = unavailable
    worker = RoomStateWorker(worker_runtime)
    try:
        with capture_logs() as logs:
            worker.mark(ROOM_ID)
            await _yield_loop(40)
            clock = 1
            await _yield_loop(40)
        assert [log["log_level"] for log in logs if log["event"] == "room_accounts_publish_failed"] == [
            "warning",
            "debug",
        ]
    finally:
        worker.stop()
        await _yield_loop(10)


@pytest.mark.asyncio
async def test_review_mark_replaces_a_done_room_task_before_retirement(
    room_state: RoomState,
    worker_runtime: MagicMock,
) -> None:
    """A new mark is not lost when it lands just before the old task's done callback retires it."""
    _, _, _, events, client = room_state
    worker = RoomStateWorker(worker_runtime)
    retire = worker._retire
    count = 0

    def mark_before_retire(room_id: str, task: asyncio.Task[None]) -> None:
        nonlocal count
        count += 1
        if count == 1:
            assert task.done()
            del events[EVENT_TYPE, ""]
            worker.mark(room_id)
        retire(room_id, task)

    with patch.object(worker, "_retire", side_effect=mark_before_retire):
        worker.mark(ROOM_ID)
        assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert len(_writes(client, EVENT_TYPE)) == 2
    assert not worker._rooms


@pytest.mark.asyncio
async def test_review_backoff_releases_all_slots(worker_runtime: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four retrying rooms do not keep a fifth room waiting for a backoff timer."""
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: 0)
    visited = []
    worker = RoomStateWorker(worker_runtime)
    healthy = "!healthy:localhost"

    async def accounts(_client: object, _config: object, _paths: object, room_id: str, **_kwargs: object) -> bool:
        visited.append(room_id)
        return room_id == healthy

    with (
        patch("mindroom.orchestration.room_state.reconcile_room_accounts", side_effect=accounts),
        patch("mindroom.orchestration.room_state.reconcile_room_thread_mode", return_value=True),
        patch("mindroom.orchestration.room_state.reconcile_room_router_binding", return_value=True),
    ):
        try:
            for i in range(4):
                worker.mark(f"!retry{i}:localhost")
            await _yield_loop(40)
            assert len(visited) == 4
            worker.mark(healthy)
            await _yield_loop(40)
            assert visited[-1] == healthy
        finally:
            worker.stop()
            await _yield_loop(20)


@pytest.mark.asyncio
async def test_review_backoff_and_warning_restart_after_success_in_same_task(
    room_state: RoomState,
    worker_runtime: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovery followed by a dirty iteration resets the timer and warning before another failure."""
    _, _, _, events, client = room_state
    clock = attempts = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    put = client.room_put_state.side_effect
    worker = RoomStateWorker(worker_runtime)

    async def fail_then_recover(room_id: str, kind: str, content: dict, **kwargs: object) -> object:
        nonlocal attempts
        if kind != ACCOUNTS_EVENT_TYPE:
            return await put(room_id, kind, content, **kwargs)
        attempts += 1
        if attempts <= 8 or attempts == 10:
            raise TimeoutError
        response = await put(room_id, kind, content, **kwargs)
        if attempts == 9:
            del events[kind, client.user_id]
            worker.mark(room_id)
        return response

    client.room_put_state.side_effect = fail_then_recover
    try:
        with capture_logs() as logs:
            worker.mark(ROOM_ID)
            for tick in (0, 1, 3, 7, 15, 31, 63, 123, 183, 184):
                clock = tick
                await _yield_loop(40)
        levels = [log["log_level"] for log in logs if log["event"] == "room_accounts_publish_failed"]
        assert levels == ["info"] * 7 + ["warning", "info"]
        assert attempts == 11
        assert _accounts(room_state) is not None
    finally:
        worker.stop()
        await _yield_loop(10)


@pytest.mark.asyncio
async def test_review_local_leave_marks_the_next_already_open_frame(room_state: RoomState) -> None:
    """After an observer opened, a departed client triggers the remaining bot's roster on the next frame."""
    _, _, ids, events, outgoing = room_state
    orchestrator, owner, runner_up = _room_state_fleet(room_state)
    worker = orchestrator.room_state
    orchestrator.handle_bot_ready = AsyncMock()
    owner.mark_sync_loop_started()
    await owner._on_ingestion_frame_completion(True)
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": outgoing.user_id}

    outgoing.rooms.clear()
    events["m.room.member", outgoing.user_id] = {"membership": "leave"}
    admission = _validated_reported_membership_admission(
        owner,
        ROOM_ID,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    principal = MagicMock(ingestion_membership_position=AsyncMock(return_value=RoomMembershipPosition("leave", 1)))
    with patch.object(owner, "journal_principal", return_value=principal):
        await owner._after_ingestion_admission(admission, AdmissionFacts(True, False), None)
    assert (ACCOUNTS_EVENT_TYPE, runner_up.agent_user.user_id) not in events
    await owner._on_ingestion_frame_completion(True)
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert events[ACCOUNTS_EVENT_TYPE, runner_up.agent_user.user_id] == {
        "accounts": sorted([ids["agent1"].full_id, ids["agent2"].full_id]),
    }


@pytest.mark.asyncio
async def test_review_stop_releases_worker_before_closing_its_matrix_client(room_state: RoomState) -> None:
    """A bot's stop cancels and drains its borrowed comparison before closing the Matrix client."""
    _, _, _, events, outgoing = room_state
    orchestrator, owner, runner_up = _room_state_fleet(room_state)
    entered, released = asyncio.Event(), asyncio.Event()
    read = outgoing.room_get_state_event.side_effect
    released_at_close: list[bool] = []

    async def blocked(room_id: str, kind: str, state_key: str = "") -> object:
        if kind == EVENT_TYPE:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                released.set()
        return await read(room_id, kind, state_key)

    async def close() -> None:
        released_at_close.append(released.is_set())

    outgoing.room_get_state_event.side_effect = blocked
    outgoing.close.side_effect = close
    worker = orchestrator.room_state
    try:
        worker.mark(ROOM_ID)
        await asyncio.wait_for(entered.wait(), timeout=2)
        await owner.stop()
        assert released_at_close == [True]
        assert await wait_for_background_tasks(timeout=2, owner=worker)
        assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": runner_up.agent_user.user_id}
    finally:
        worker.stop()
        await wait_for_background_tasks(timeout=2, owner=worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_during_io", [False, True])
async def test_worker_coalesces_marks(
    worker_runtime: MagicMock,
    room_state: RoomState,
    mark_during_io: bool,
) -> None:
    """Coalesce marks before I/O without losing one that arrives during the comparison."""
    _, _, _, _, client = room_state
    entered, release = asyncio.Event(), asyncio.Event()
    read = client.room_get_state_event.side_effect

    async def blocked(room_id: str, kind: str, state_key: str = "") -> object:
        if kind == EVENT_TYPE:
            entered.set()
            await release.wait()
        return await read(room_id, kind, state_key)

    client.room_get_state_event.side_effect = blocked
    worker = RoomStateWorker(worker_runtime)

    async def mark() -> None:
        worker.mark(ROOM_ID)

    # gather queues all of the mark callers before the worker gets to take the dirty flag.
    await asyncio.gather(*(mark() for _ in range(20)))
    async with asyncio.timeout(2):
        await entered.wait()
        if mark_during_io:
            await asyncio.gather(*(mark() for _ in range(20)))
        reads = [c for c in client.room_get_state_event.await_args_list if c.args[1] == EVENT_TYPE]
        assert len(reads) == 1
        release.set()
        assert await wait_for_background_tasks(owner=worker)
    reads = [c for c in client.room_get_state_event.await_args_list if c.args[1] == EVENT_TYPE]
    assert len(reads) == (2 if mark_during_io else 1)
    assert len(_writes(client, EVENT_TYPE)) == 1


def _successor_client(room_state: RoomState) -> AsyncMock:
    """Build a second joined client reading and writing the same fake homeserver state."""
    _, _, ids, _, outgoing = room_state
    incoming = AsyncMock(spec=nio.AsyncClient)
    incoming.user_id = ids["agent1"].full_id
    incoming.rooms = outgoing.rooms
    incoming.room_get_state_event.side_effect = outgoing.room_get_state_event.side_effect
    incoming.joined_members.side_effect = outgoing.joined_members.side_effect
    incoming.room_put_state.side_effect = outgoing.room_put_state.side_effect
    return incoming


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("when", "earlier_writes"),
    [
        ("before_attempt", []),
        (EVENT_TYPE, []),
        (ACCOUNTS_EVENT_TYPE, [EVENT_TYPE]),
        (ROUTER_EVENT_TYPE, [EVENT_TYPE, ACCOUNTS_EVENT_TYPE]),
    ],
)
async def test_worker_uses_current_steward(
    worker_runtime: MagicMock,
    room_state: RoomState,
    when: str,
    earlier_writes: list[str],
) -> None:
    """Use the current client at attempt time and fence an outgoing client before its PUT."""
    _, _, _, events, outgoing = room_state
    incoming = _successor_client(room_state)
    new_steward = MagicMock(client=incoming)
    read = outgoing.room_get_state_event.side_effect

    async def demote(room_id: str, kind: str, state_key: str = "") -> object:
        if kind == when:
            worker_runtime.room_steward.return_value = new_steward
        return await read(room_id, kind, state_key)

    worker = RoomStateWorker(worker_runtime)
    worker.mark(ROOM_ID)
    if when == "before_attempt":
        worker_runtime.room_steward.return_value = new_steward
    else:
        outgoing.room_get_state_event.side_effect = demote
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert [c.args[1] for c in outgoing.room_put_state.await_args_list] == earlier_writes
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}
    assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": incoming.user_id}
    assert (ACCOUNTS_EVENT_TYPE, incoming.user_id) in events


@pytest.mark.asyncio
async def test_worker_rechecks_all_state_when_the_steward_changes_during_backoff(
    worker_runtime: MagicMock,
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new binding writer must publish its own roster even if only binding had failed before the handover."""
    _, _, _, events, outgoing = room_state
    incoming = _successor_client(room_state)
    write = outgoing.room_put_state.side_effect
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)

    async def binding_timeout(room_id: str, kind: str, content: dict, **kwargs: object) -> object:
        if kind == ROUTER_EVENT_TYPE:
            raise TimeoutError
        return await write(room_id, kind, content, **kwargs)

    outgoing.room_put_state.side_effect = binding_timeout
    worker = RoomStateWorker(worker_runtime)
    try:
        worker.mark(ROOM_ID)
        await _yield_loop(40)
        assert (ACCOUNTS_EVENT_TYPE, outgoing.user_id) in events
        assert (ROUTER_EVENT_TYPE, "") not in events
        # A bot can stop during backoff without a Matrix power or membership event.
        worker_runtime.room_steward.return_value = MagicMock(client=incoming)
        clock = 1
        await _yield_loop(40)
        assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": incoming.user_id}
        assert (ACCOUNTS_EVENT_TYPE, incoming.user_id) in events
    finally:
        worker.stop()
        await _yield_loop(10)


@pytest.mark.asyncio
async def test_worker_uses_reloaded_config(worker_runtime: MagicMock, room_state: RoomState) -> None:
    """Do not publish a superseded mode when the config changes mid-comparison."""
    config, _, _, events, client = room_state
    worker = RoomStateWorker(worker_runtime)
    read = client.room_get_state_event.side_effect

    async def reload_config(room_id: str, kind: str, state_key: str = "") -> object:
        if kind == EVENT_TYPE and worker_runtime.config is config:
            new_config = config.model_copy(deep=True)
            new_config.agents["agent2"].thread_mode = "room"
            worker_runtime.config = new_config
            worker.mark(room_id)
        return await read(room_id, kind, state_key)

    client.room_get_state_event.side_effect = reload_config
    worker.mark(ROOM_ID)
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    assert [c.args[2] for c in _writes(client, EVENT_TYPE)] == [{"mode": "room"}]


@pytest.mark.asyncio
async def test_worker_drops_rooms_without_a_steward(worker_runtime: MagicMock, room_state: RoomState) -> None:
    """Retire a room with no eligible client and accept a later mark."""
    _, _, _, _, client = room_state
    steward = worker_runtime.room_steward.return_value
    worker_runtime.room_steward.return_value = None
    worker = RoomStateWorker(worker_runtime)
    worker.mark(ROOM_ID)
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    client.room_get_state_event.assert_not_awaited()
    worker_runtime.room_steward.return_value = steward
    worker.mark(ROOM_ID)
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert len(_writes(client, EVENT_TYPE)) == 1


@pytest.mark.asyncio
async def test_worker_bounds_concurrency_and_does_not_serialize_rooms(worker_runtime: MagicMock) -> None:
    """Let a healthy room finish while slow rooms occupy some slots, up to four at a time."""
    entered: list[str] = []
    three_waiting, release = asyncio.Event(), asyncio.Event()
    healthy, fourth = asyncio.Event(), asyncio.Event()

    async def accounts(_client: object, _config: object, _paths: object, room_id: str, **_kwargs: object) -> bool:
        if room_id == "!healthy:localhost":
            healthy.set()
        else:
            entered.append(room_id)
            if len(entered) == 3:
                three_waiting.set()
            if len(entered) == 4:
                fourth.set()
            await release.wait()
        return True

    worker = RoomStateWorker(worker_runtime)
    with (
        patch("mindroom.orchestration.room_state.reconcile_room_accounts", side_effect=accounts),
        patch("mindroom.orchestration.room_state.reconcile_room_thread_mode", return_value=True),
        patch("mindroom.orchestration.room_state.reconcile_room_router_binding", return_value=True),
    ):
        try:
            for n in range(3):
                worker.mark(f"!slow{n}:localhost")
            async with asyncio.timeout(2):
                await three_waiting.wait()
                worker.mark("!healthy:localhost")
                await healthy.wait()
                for n in range(3, 8):
                    worker.mark(f"!slow{n}:localhost")
                await fourth.wait()
                await asyncio.sleep(0)
                assert len(entered) == 4
        finally:
            release.set()
            assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert len(entered) == 8


@pytest.mark.asyncio
async def test_worker_stop_cancels_in_flight_and_queued_rooms(worker_runtime: MagicMock, room_state: RoomState) -> None:
    """Cancel active and queued rooms when the runtime closes admission."""
    _, _, _, _, client = room_state
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*_args: object, **_kwargs: object) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    client.room_get_state_event.side_effect = blocked
    worker = RoomStateWorker(worker_runtime)
    worker.mark(ROOM_ID)
    async with asyncio.timeout(2):
        await entered.wait()
        worker.mark("!other:localhost")
        worker_runtime.running = False
        worker.stop()
        await cancelled.wait()
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert not worker._rooms
    client.room_get_state_event.assert_awaited_once()
    worker.mark(ROOM_ID)
    await asyncio.sleep(0)
    client.room_get_state_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_stop_cancels_the_room_state_worker(room_state: RoomState) -> None:
    """Process shutdown cancels room-state I/O even without a bot stop to cancel it."""
    config, paths, _, _, client = room_state
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=config)
    orchestrator.running = True
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*_args: object, **_kwargs: object) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    client.room_get_state_event.side_effect = blocked
    with patch.object(orchestrator, "room_steward", return_value=MagicMock(client=client)):
        orchestrator.room_state.mark(ROOM_ID)
        await asyncio.wait_for(entered.wait(), timeout=2)
        await orchestrator.stop()
    assert cancelled.is_set()
    assert await wait_for_background_tasks(timeout=2, owner=orchestrator.room_state)


@pytest.mark.asyncio
async def test_worker_caps_jitter_and_retries_only_transient_steps(
    worker_runtime: MagicMock,
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the jitter bounds and do not retry a refused write without a fresh mark."""
    _, _, _, _, client = room_state
    clock = 0.0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    draws: list[tuple[float, float]] = []
    write = client.room_put_state.side_effect
    attempts: list[str] = []

    def latest(low: float, high: float) -> float:
        assert 0 < low < high <= 60
        draws.append((low, high))
        return high

    async def refuse(room_id: str, kind: str, content: dict, **kwargs: object) -> object:
        attempts.append(kind)
        if kind == EVENT_TYPE:
            return nio.RoomPutStateError("Forbidden", "M_FORBIDDEN")
        if kind == ACCOUNTS_EVENT_TYPE and attempts.count(kind) <= 9:
            raise TimeoutError
        return await write(room_id, kind, content, **kwargs)

    async def settle() -> None:
        for _ in range(40):
            await asyncio.sleep(0)

    monkeypatch.setattr("mindroom.orchestration.room_state.uniform", latest)
    client.room_put_state.side_effect = refuse
    worker = RoomStateWorker(worker_runtime)
    try:
        with capture_logs() as logs:
            worker.mark(ROOM_ID)
            await settle()
            assert attempts == [EVENT_TYPE, ACCOUNTS_EVENT_TYPE, ROUTER_EVENT_TYPE]
            for high in (1.5, 3, 6, 12, 24, 48, 60, 60, 60):
                assert draws[-1][1] == high
                clock += high - 0.25
                count = len(attempts)
                await settle()
                assert len(attempts) == count
                clock += 0.25
                await settle()
            levels = [log["log_level"] for log in logs if log["event"] == "room_accounts_publish_failed"]
        assert levels == ["info"] * 7 + ["warning", "debug"]
        assert attempts.count(EVENT_TYPE) == 1
        assert attempts.count(ACCOUNTS_EVENT_TYPE) == 10
        assert attempts.count(ROUTER_EVENT_TYPE) == 1
        clock += 100
        await settle()
        assert attempts.count(EVENT_TYPE) == 1
        worker.mark(ROOM_ID)
        await settle()
        assert attempts.count(EVENT_TYPE) == 2
    finally:
        worker.stop()
        await settle()


@pytest.mark.asyncio
async def test_member_event_marks_without_blocking_its_room_lane(room_state: RoomState) -> None:
    """Return the membership callback before its room-state I/O completes."""
    _, _, ids, _, client = room_state
    duties = _owner_bot(room_state).steward_duties
    worker = duties.deps.runtime.orchestrator.room_state
    entered = asyncio.Event()
    release = asyncio.Event()
    read = client.room_get_state_event.side_effect

    async def blocked(*args: object, **kwargs: object) -> object:
        entered.set()
        await release.wait()
        return await read(*args, **kwargs)

    client.room_get_state_event.side_effect = blocked
    try:
        async with asyncio.timeout(2):
            await duties.member_event(client.rooms[ROOM_ID], _membership_event(ids["agent2"].full_id, "join", "leave"))
            await entered.wait()
    finally:
        release.set()
        assert await wait_for_background_tasks(timeout=2, owner=worker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        (("room", "room"), "room"),
        (("thread", "thread"), "thread"),
        (("room", "thread"), "mixed"),
    ],
)
async def test_publishes_effective_mode(
    room_state: RoomState,
    modes: tuple[Literal["room", "thread"], Literal["room", "thread"]],
    expected: str,
) -> None:
    """Every joined responder contributes its effective mode, excluding the router."""
    config, paths, _, _, client = room_state
    config.agents["agent1"].thread_mode, config.agents["agent2"].thread_mode = modes
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.assert_awaited_once_with(ROOM_ID, EVENT_TYPE, {"mode": expected}, state_key="")


@pytest.mark.asyncio
async def test_updates_after_reload_membership_and_runtime_overrides(room_state: RoomState) -> None:
    """Keep state current after mode changes, resets, and joins or departures."""
    config, paths, ids, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}
    config.agents["agent2"].room_thread_modes = {ROOM_ID: "room"}
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    set_room_thread_mode_override(paths, room_id=ROOM_ID, mode="thread")
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "thread"}
    clear_room_thread_mode_override(paths, ROOM_ID)
    config.agents["agent2"].room_thread_modes = {}
    events["m.room.member", ids["agent2"].full_id] = {"membership": "leave"}
    _sync_live_room(room_state)
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    events["m.room.member", ids["agent2"].full_id] = {"membership": "join"}
    _sync_live_room(room_state)
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
async def test_deduplicates_concurrent_and_unchanged_triggers(room_state: RoomState) -> None:
    """Concurrent and later unchanged triggers only write a missing value once."""
    config, paths, _, _, client = room_state
    await asyncio.gather(*(reconcile_room_thread_mode(client, config, paths, ROOM_ID) for _ in range(3)))
    client.room_put_state.assert_awaited_once()
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert client.room_put_state.await_count == 1


@pytest.mark.asyncio
async def test_invited_agents_do_not_contribute_and_empty_room_keeps_state(room_state: RoomState) -> None:
    """An invitation cannot answer yet; an empty roster leaves existing state unchanged."""
    config, paths, ids, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    events["m.room.member", ids["agent2"].full_id] = {"membership": "invite"}
    _sync_live_room(room_state)
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    events["m.room.member", ids["agent1"].full_id] = {"membership": "ban"}
    _sync_live_room(room_state)
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_joined_team_uses_team_resolution(room_state: RoomState) -> None:
    """A joined team replies with its own resolved mode, even when its members are absent."""
    config, paths, ids, events, client = room_state
    config.teams["team1"] = TeamConfig(
        display_name="Team 1",
        role="Test team",
        agents=["agent1", "agent2"],
        rooms=[ROOM_ID],
    )
    ids = entity_ids(config, paths)
    for name in ("agent1", "agent2"):
        config.agents[name].rooms = []
        events["m.room.member", ids[name].full_id] = {"membership": "leave"}
    events["m.room.member", ids["team1"].full_id] = {"membership": "join"}
    _sync_live_room(room_state)
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "thread"}
    config.agents["agent2"].thread_mode = "room"
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        nio.RoomPutStateError("unavailable", "M_UNKNOWN"),
        aiohttp.ClientConnectionError("offline"),
        TimeoutError(),
    ],
)
async def test_write_failure_is_nonfatal_and_retried(
    room_state: RoomState,
    failure: object,
) -> None:
    """A failed publication logs a warning and does not poison later reconciliation."""
    config, paths, _, _, client = room_state
    client.room_put_state.side_effect = failure if isinstance(failure, Exception) else None
    client.room_put_state.return_value = failure
    with capture_logs() as logs:
        await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
        await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert client.room_put_state.await_count == 2
    assert len(logs) == 2
    assert all(log["event"] == "room_thread_mode_reconcile_failed" and log["log_level"] == "warning" for log in logs)


@pytest.mark.asyncio
async def test_a_room_that_refuses_the_routers_state_is_no_warning(room_state: RoomState) -> None:
    """A refused state write logs at debug and is not retried on a timer."""
    _, _, _, _, client = room_state
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("user_level (0) < send_level (50)", "M_FORBIDDEN")
    with capture_logs() as logs:
        await _publish_held(room_state)
    assert [write.args[1] for write in client.room_put_state.await_args_list] == [
        EVENT_TYPE,
        ACCOUNTS_EVENT_TYPE,
        ROUTER_EVENT_TYPE,
    ]
    assert [log["log_level"] for log in logs] == ["debug"] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing",
    [
        None,
        {},
        {"user_id": 7},
        {"user_id": ""},
        {"user_id": "router"},
        {"user_id": "@router:"},
        {"user_id": "@:server"},
    ],
)
async def test_router_binding_claims_an_absent_or_malformed_binding_once(
    room_state: RoomState,
    existing: dict[str, object] | None,
) -> None:
    """The router names itself where no valid binding exists, and leaves its own binding alone."""
    _, _, ids, events, client = room_state
    if existing is not None:
        events[ROUTER_EVENT_TYPE, ""] = existing

    assert await reconcile_room_router_binding(client, ROOM_ID) is True
    assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": ids[ROUTER_AGENT_NAME].full_id}
    assert await reconcile_room_router_binding(client, ROOM_ID) is True
    assert len(_writes(client, ROUTER_EVENT_TYPE)) == 1
    assert _writes(client, ROUTER_EVENT_TYPE)[0].kwargs["state_key"] == ""


@pytest.mark.asyncio
async def test_an_unreadable_router_binding_is_not_claimed(room_state: RoomState) -> None:
    """A failed read is not an absent binding: it warns and retries later instead of overwriting."""
    *_, client = room_state
    read = client.room_get_state_event.side_effect

    async def fail_binding_read(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == ROUTER_EVENT_TYPE:
            return nio.RoomGetStateEventError("Server error", "M_UNKNOWN", room_id=room_id)
        return await read(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = fail_binding_read
    with capture_logs() as logs:
        assert await reconcile_room_router_binding(client, ROOM_ID) is False
    assert not _writes(client, ROUTER_EVENT_TYPE)
    assert [(log["event"], log["log_level"]) for log in logs] == [("room_router_binding_publish_failed", "warning")]


@pytest.mark.asyncio
async def test_a_forbidden_router_binding_read_needs_no_retry(room_state: RoomState) -> None:
    """Do not claim an unreadable binding or keep retrying a forbidden read."""
    *_, client = room_state
    read = client.room_get_state_event.side_effect

    async def refuse_binding(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == ROUTER_EVENT_TYPE:
            return nio.RoomGetStateEventError("Forbidden", "M_FORBIDDEN", room_id=room_id)
        return await read(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = refuse_binding
    await _publish_held(room_state)
    assert not _writes(client, ROUTER_EVENT_TYPE)


@pytest.mark.asyncio
async def test_router_binding_leaves_another_routers_binding_alone(room_state: RoomState) -> None:
    """Two privileged routers must not take turns overwriting one room's binding."""
    *_, events, client = room_state
    events[ROUTER_EVENT_TYPE, ""] = {"user_id": "@other_router:elsewhere.invalid"}
    with capture_logs() as logs:
        assert await reconcile_room_router_binding(client, ROOM_ID) is True
    assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": "@other_router:elsewhere.invalid"}
    assert not _writes(client, ROUTER_EVENT_TYPE)
    assert [(log["event"], log["log_level"]) for log in logs] == [("room_router_binding_held_by_other", "debug")]


@pytest.mark.asyncio
async def test_a_refused_router_binding_needs_no_retry(room_state: RoomState) -> None:
    """A room that denies the optional binding is done; only the other room state decides on a retry."""
    *_, client = room_state
    write = client.room_put_state.side_effect

    async def refuse_binding(room_id: str, event_type: str, content: dict[str, Any], state_key: str = "") -> object:
        if event_type == ROUTER_EVENT_TYPE:
            return nio.RoomPutStateError("user_level (0) < send_level (50)", "M_FORBIDDEN")
        return await write(room_id, event_type, content, state_key=state_key)

    client.room_put_state.side_effect = refuse_binding
    await _publish_held(room_state)
    assert len(_writes(client, ROUTER_EVENT_TYPE)) == 1


@pytest.mark.asyncio
async def test_router_binding_failure_is_nonfatal(room_state: RoomState) -> None:
    """A binding read that raises logs a warning and reports the room as not current instead of raising."""
    *_, client = room_state
    client.room_get_state_event.side_effect = RuntimeError("homeserver unavailable")
    with capture_logs() as logs:
        assert await reconcile_room_router_binding(client, ROOM_ID) is False
    assert [(log["event"], log["log_level"]) for log in logs] == [("room_router_binding_publish_failed", "warning")]


@pytest.mark.asyncio
async def test_router_publishes_its_binding_on_start_and_join(room_state: RoomState) -> None:
    """A sweep or managed membership change publishes a missing binding."""
    _config, _paths, ids, events, _client = room_state
    binding = {"user_id": ids[ROUTER_AGENT_NAME].full_id}

    await _publish_held(room_state)
    assert events[ROUTER_EVENT_TYPE, ""] == binding

    del events[ROUTER_EVENT_TYPE, ""]
    await _member_changed(
        _router_duties(room_state),
        room_state[4].rooms[ROOM_ID],
        _membership_event(ids["agent1"].full_id, "join", "invite"),
    )
    assert events[ROUTER_EVENT_TYPE, ""] == binding


@pytest.mark.asyncio
async def test_router_membership_callback_publishes_agent_join_and_leave(room_state: RoomState) -> None:
    """An account's leave and join update the thread mode and the accounts list, even without membership hooks."""
    config, paths, ids, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    duties = _router_duties(room_state)
    router, agent1, agent2 = (ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "agent2"))
    for membership, previous, mode, accounts in (
        ("leave", "join", "room", [router, agent1]),
        ("join", "leave", "mixed", [router, agent1, agent2]),
    ):
        events["m.room.member", agent2] = {"membership": membership}
        _sync_live_room(room_state)
        await _member_changed(duties, client.rooms[ROOM_ID], _membership_event(agent2, membership, previous))
        assert events[EVENT_TYPE, ""] == {"mode": mode}
        assert _accounts(room_state) == sorted(accounts)


@pytest.mark.asyncio
async def test_setup_publishes_after_agents_join(room_state: RoomState, monkeypatch: pytest.MonkeyPatch) -> None:
    """Initial room setup publishes after every bot has joined."""
    config, _paths, ids, events, client = room_state
    config.matrix_space.enabled = False
    setup, worker = _room_setup(room_state)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    snapshot = await read_room_state(client, ROOM_ID)
    assert snapshot is not None
    monkeypatch.setattr(rooms_module, "reconcile_managed_rooms", AsyncMock(return_value={ROOM_ID: snapshot}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())
    bot = AsyncMock()
    bot.agent_name = "agent2"
    bot.agent_user = MagicMock(user_id=ids["agent2"].full_id)
    bot.client = client
    events["m.room.member", ids["agent2"].full_id] = {"membership": "leave"}

    async def join() -> None:
        events["m.room.member", ids["agent2"].full_id] = {"membership": "join"}
        _sync_live_room(room_state)

    bot.ensure_rooms.side_effect = join
    await setup.setup([bot])
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
async def test_command_set_and_reset_publish_immediately(room_state: RoomState) -> None:
    """Authorized commands publish changed effective modes before returning."""
    config, paths, _, events, client = room_state
    for command, expected in (("room", "room"), ("thread", "thread"), ("reset", "mixed")):
        await handle_thread_mode_command(
            command,
            client=client,
            config=config,
            runtime_paths=paths,
            room_id=ROOM_ID,
            requester_user_id="@admin:localhost",
            sender_user_id="@admin:localhost",
        )
        assert events[EVENT_TYPE, ""] == {"mode": expected}


@pytest.mark.asyncio
async def test_a_refused_mode_change_does_not_undo_one_made_meanwhile(room_state: RoomState) -> None:
    """A refused change restores the override it replaced, and a change made while it waited still takes effect."""
    config, paths, _, events, client = room_state
    refusing, refused = asyncio.Event(), asyncio.Event()

    async def refuse(*_args: object) -> None:
        refusing.set()
        await refused.wait()

    first = asyncio.create_task(change_room_thread_mode(client, config, paths, ROOM_ID, "room", put_state=refuse))
    await refusing.wait()
    second = asyncio.create_task(change_room_thread_mode(client, config, paths, ROOM_ID, "thread"))
    await asyncio.sleep(0)
    refused.set()

    assert await first is None
    assert await second is True
    assert resolve_room_thread_mode_override(paths, ROOM_ID) == "thread"
    assert events[EVENT_TYPE, ""] == {"mode": "thread"}


@pytest.mark.asyncio
async def test_a_finished_thread_mode_change_keeps_no_lock_for_its_room(room_state: RoomState) -> None:
    """A room's change lock lives only while a change holds it, so the locks do not grow with every room changed."""
    config, paths, _, _, client = room_state
    assert await change_room_thread_mode(client, config, paths, ROOM_ID, "thread") is True
    gc.collect()
    assert (str(paths.storage_root), ROOM_ID) not in room_modes._CHANGES


@pytest.mark.asyncio
async def test_live_config_reload_publishes_without_room_setup(room_state: RoomState) -> None:
    """Mode edits apply live, so a reload with no restarts still updates client state."""
    config, paths, _, events, client = room_state
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=config)
    orchestrator.running = True
    install_bots(orchestrator, {ROUTER_AGENT_NAME: _owner_bot(room_state)})
    config.agents["agent2"].thread_mode = "room"
    await orchestrator.config_reload.after_reload()
    assert await wait_for_background_tasks(timeout=2, owner=orchestrator.room_state)
    client.room_get_state.assert_not_awaited()
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_existing_state_is_not_resent_on_reconciliation(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup skips an already correct remote thread-mode event."""
    config, _paths, _, events, client = room_state
    config.matrix_space.enabled = False
    events[EVENT_TYPE, ""] = {"mode": "mixed"}
    snapshot = await read_room_state(client, ROOM_ID)
    assert snapshot is not None
    setup, worker = _room_setup(room_state)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(rooms_module, "reconcile_managed_rooms", AsyncMock(return_value={ROOM_ID: snapshot}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())
    await setup.setup([])
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert not _writes(client, EVENT_TYPE)


@pytest.mark.asyncio
@pytest.mark.parametrize("room_reference", [ROOM_ID, "#room:localhost"])
@pytest.mark.parametrize("visitor_kind", ["agent", "team"])
async def test_joined_unassigned_entities_do_not_change_managed_room_mode(
    room_state: RoomState,
    room_reference: str,
    visitor_kind: str,
) -> None:
    """An invited visitor cannot answer in a room assigned to another entity."""
    config, paths, _, events, client = room_state
    config.agents["agent1"].rooms = [room_reference]
    config.agents["agent2"].rooms = []
    events["m.room.canonical_alias", ""] = {"alias": "#room:localhost"}
    if visitor_kind == "team":
        config.teams["team1"] = TeamConfig(
            display_name="Team 1",
            role="Test team",
            agents=["agent2"],
            rooms=[],
        )
        ids = entity_ids(config, paths)
        events["m.room.member", ids["team1"].full_id] = {"membership": "join"}
    _sync_live_room(room_state)
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_ad_hoc_room_counts_joined_entities(room_state: RoomState) -> None:
    """A room with no configured responders discovers them from joined members."""
    config, paths, _, events, client = room_state
    config.agents["agent1"].rooms = []
    config.agents["agent2"].rooms = []
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
async def test_mention_only_room_keeps_all_assigned_responders(room_state: RoomState) -> None:
    """Metadata includes eligible responders even when they require a mention."""
    config, paths, _, events, client = room_state
    config.rooms[ROOM_ID] = RoomConfig(reply_mode="mentions")
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
async def test_reloads_read_only_the_thread_mode_event(room_state: RoomState) -> None:
    """A reload reads just the mode state event, not a whole state snapshot, for the mode comparison."""
    config, paths, _, events, client = room_state
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_get_state.reset_mock()
    client.room_get_state_event.reset_mock()
    client.room_put_state.reset_mock()
    setup, worker = _room_setup(room_state)
    config.agents["agent1"].role = "Updated role"
    await setup.publish_room_state()
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    client.room_get_state.assert_not_awaited()
    mode_reads = [read for read in client.room_get_state_event.await_args_list if read.args[1] == EVENT_TYPE]
    assert len(mode_reads) == 1
    assert mode_reads[0].args == (ROOM_ID, EVENT_TYPE)
    assert mode_reads[0].kwargs == {"state_key": ""}
    assert not _writes(client, EVENT_TYPE)
    config.agents["agent2"].thread_mode = "room"
    await setup.publish_room_state()
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    client.room_get_state.assert_not_awaited()
    assert _writes(client, EVENT_TYPE) == [call(ROOM_ID, EVENT_TYPE, {"mode": "room"}, state_key="")]
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_setup_defers_reconciliation_until_admission_reopens(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closed reply admission defers publication; reopening compares only the mode event."""
    config, _paths, _, events, client = room_state
    config.matrix_space.enabled = False
    snapshot = await read_room_state(client, ROOM_ID)
    assert snapshot is not None
    client.room_get_state.reset_mock()
    runtime = _router_duties(room_state).deps.runtime.orchestrator
    runtime.response_admission_gate.closed = True
    setup, worker = _room_setup(room_state, runtime)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(rooms_module, "reconcile_managed_rooms", AsyncMock(return_value={ROOM_ID: snapshot}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())
    await setup.setup([])
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    client.room_get_state.assert_not_awaited()
    client.room_put_state.assert_not_awaited()
    runtime.response_admission_gate.closed = False
    await setup.publish_room_state()
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    client.room_get_state.assert_not_awaited()
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
async def test_router_profile_change_does_not_read_or_write_state(room_state: RoomState) -> None:
    """Join-to-join profile updates change neither the responder roster nor the accounts list."""
    _, _, ids, _, client = room_state
    event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": "$profile",
            "sender": ids["agent1"].full_id,
            "state_key": ids["agent1"].full_id,
            "origin_server_ts": 1,
            "content": {"membership": "join", "displayname": "Updated profile"},
            "unsigned": {"prev_content": {"membership": "join", "displayname": "Previous profile"}},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    await _member_changed(_router_duties(room_state), nio.MatrixRoom(ROOM_ID, client.user_id), event)
    client.room_get_state.assert_not_awaited()
    client.room_get_state_event.assert_not_awaited()
    client.joined_members.assert_not_awaited()
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_publishes_after_response_admission_reopens(tmp_path: Path) -> None:
    """Slow presentation publication cannot hold back new replies during a reload."""
    config = Config()
    edited = Config(defaults={"enable_streaming": not config.defaults.enable_streaming})
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        current_config=config,
        load_config=lambda: published_config_load(edited),
    )
    observed = []

    async def publish() -> None:
        observed.append(lifecycle.response_admission_gate.closed)

    lifecycle.after_reload = publish
    await lifecycle._update_config()
    assert observed == [False]


@pytest.mark.asyncio
async def test_startup_publishes_after_response_admission_reopens(tmp_path: Path) -> None:
    """Startup reconciles state after the reply admission fence ends."""
    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path))
    observed = []

    async def publish() -> None:
        observed.append(lifecycle.response_admission_gate.closed)

    lifecycle.after_reload = publish
    async with lifecycle.startup_publication_admission():
        assert lifecycle.response_admission_gate.closed
    assert observed == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_membership", [False, True])
async def test_fresh_setup_repairs_another_writers_mode(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    *,
    changed_membership: bool,
) -> None:
    """A new reconciliation repairs external changes without retaining an earlier value."""
    config, paths, ids, events, client = room_state
    if not changed_membership:
        config.agents["agent2"].thread_mode = "room"
    else:
        for agent in config.agents.values():
            agent.rooms = []
    config.matrix_space.enabled = False
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    if changed_membership:
        events["m.room.member", ids["agent2"].full_id] = {"membership": "leave"}
    else:
        events[EVENT_TYPE, ""] = {"mode": "thread"}
    snapshot = await read_room_state(client, ROOM_ID)
    assert snapshot is not None
    _sync_live_room(room_state)
    client.room_get_state.reset_mock()
    setup, worker = _room_setup(room_state)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(rooms_module, "reconcile_managed_rooms", AsyncMock(return_value={ROOM_ID: snapshot}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())
    await setup.setup([])
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "expected"), [("room", "room"), ("reset", "mixed")])
async def test_repeated_command_repairs_another_writers_mode(
    room_state: RoomState,
    command: str,
    expected: str,
) -> None:
    """An explicit mode command reasserts its result even after another writer changed it."""
    config, paths, _, events, client = room_state
    for _ in range(2):
        await handle_thread_mode_command(
            command,
            client=client,
            config=config,
            runtime_paths=paths,
            room_id=ROOM_ID,
            requester_user_id="@admin:localhost",
            sender_user_id="@admin:localhost",
        )
        assert events[EVENT_TYPE, ""] == {"mode": expected}
        events[EVENT_TYPE, ""] = {"mode": "thread"}
    assert client.room_put_state.await_count == 2


@pytest.mark.asyncio
async def test_membership_recovers_after_initial_state_read_failure(room_state: RoomState) -> None:
    """A failed setup read cannot prevent later membership-triggered publication."""
    config, _, ids, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    read = client.room_get_state.side_effect
    client.room_get_state.side_effect = aiohttp.ClientConnectionError("offline")
    assert await read_room_state(client, ROOM_ID) is None
    client.room_get_state.side_effect = read
    client.room_get_state.reset_mock()
    events["m.room.member", ids["agent2"].full_id] = {"membership": "leave"}
    _sync_live_room(room_state)
    await _member_changed(
        _router_duties(room_state),
        client.rooms[ROOM_ID],
        _membership_event(ids["agent2"].full_id, "leave", "join"),
    )
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    client.room_get_state.assert_not_awaited()


def _owner_bot(state: RoomState) -> MagicMock:
    """The owner account's bot with its real duties, the steward of every room it holds."""
    return _router_duties(state).deps.runtime.orchestrator.room_steward.return_value


def _room_setup(state: RoomState, runtime: MagicMock | None = None) -> tuple[RoomSetup, RoomStateWorker]:
    """A room setup whose sweep marks rooms on the runtime worker."""
    config, paths, _, _, client = state
    if runtime is None:
        runtime = _router_duties(state).deps.runtime.orchestrator
    worker = runtime.room_state
    return RoomSetup(paths, lambda: config, lambda: client, AsyncMock(), worker.mark_held), worker


def _router_duties(state: RoomState) -> StewardDuties:
    """Build the owner account's duties, the steward of every room."""
    config, paths, _, _, client = state
    runtime = MagicMock(config=config, runtime_paths=paths, running=True)
    runtime.response_admission_gate.closed = False
    deps = StewardDutiesDeps(
        runtime=MagicMock(config=config, runtime_paths=paths, client=client, orchestrator=runtime),
        logger=MagicMock(),
        hook_context=MagicMock(agent_name=ROUTER_AGENT_NAME),
        journal=MagicMock(),
        room_lifecycle=MagicMock(send_welcome_message_if_empty=AsyncMock(), refresh_welcome=AsyncMock()),
        personal_rooms=MagicMock(),
    )
    deps.hook_context.registry.has_hooks.return_value = False
    deps.personal_rooms.onboards_from.return_value = False
    duties = StewardDuties(deps)
    bot = MagicMock(client=client, agent_name=ROUTER_AGENT_NAME, steward_duties=duties)
    runtime.agent_bots = {ROUTER_AGENT_NAME: bot}
    runtime.room_steward.return_value = bot
    runtime.room_state = RoomStateWorker(runtime)
    return duties


def _worker(duties: StewardDuties) -> RoomStateWorker:
    return duties.deps.runtime.orchestrator.room_state


async def _publish_held(state: RoomState) -> None:
    setup, worker = _room_setup(state)
    await setup.publish_room_state()
    assert await wait_for_background_tasks(timeout=2, owner=worker)


async def _member_changed(duties: StewardDuties, room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
    await duties.member_event(room, event)
    assert await wait_for_background_tasks(timeout=2, owner=_worker(duties))


def _membership_event(user_id: str, membership: str, previous: str) -> nio.RoomMemberEvent:
    return member_event(user_id, membership, previous, event_id=f"${membership}")


@pytest.mark.asyncio
async def test_the_owner_duties_these_tests_build_onboard_from_no_room(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no join hooks and no onboarding room, a person's join takes no join-baseline path."""
    client = room_state[4]
    emit = AsyncMock()
    monkeypatch.setattr("mindroom.steward_duties.emit_room_member_join_at_least_once", emit)
    await _router_duties(room_state).member_event(
        client.rooms[ROOM_ID],
        _membership_event("@alice:localhost", "join", "leave"),
    )
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_with_partial_membership(room_state: RoomState) -> None:
    """Configured responders remain eligible when lazy sync omits them."""
    config, paths, ids, events, client = room_state
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    client.rooms[ROOM_ID].remove_member(ids["agent2"].full_id)
    config.agents["agent2"].thread_mode = "room"
    await _publish_held(room_state)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_ad_hoc_membership_with_partial_projection(room_state: RoomState) -> None:
    """An unloaded remaining responder still determines an invited room's view."""
    config, _, ids, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    events[EVENT_TYPE, ""] = {"mode": "mixed"}
    client.rooms[ROOM_ID].remove_member(ids["agent1"].full_id)
    events["m.room.member", ids["agent2"].full_id] = {"membership": "leave"}
    await _member_changed(
        _router_duties(room_state),
        client.rooms[ROOM_ID],
        _membership_event(ids["agent2"].full_id, "leave", "join"),
    )
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_unchanged_reload_retries_failed_write(room_state: RoomState) -> None:
    """A role-only reload repairs a previous mode write that timed out."""
    config, paths, _, events, client = room_state
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    config.agents["agent2"].thread_mode = "room"
    setup, worker = _room_setup(room_state)
    write = client.room_put_state.side_effect
    client.room_put_state.side_effect = TimeoutError()
    await setup.publish_room_state()
    await _yield_loop(40)
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}
    client.room_put_state.side_effect = write
    config.agents["agent1"].role = "Updated role"
    await setup.publish_room_state()
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["startup", "router_join"])
async def test_retained_ad_hoc_room_is_reconciled(room_state: RoomState, trigger: str) -> None:
    """Retained invited rooms receive state on startup and on router rejoin."""
    config, _paths, ids, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
        agent.thread_mode = "room"
    events[EVENT_TYPE, ""] = {"mode": "mixed"}
    if trigger == "startup":
        await _publish_held(room_state)
    else:
        await _member_changed(
            _router_duties(room_state),
            client.rooms[ROOM_ID],
            _membership_event(ids["agent1"].full_id, "join", "invite"),
        )
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_startup_admission_reopens_during_joins(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication during setup cannot overwrite current state with a pre-join snapshot."""
    config, _paths, ids, events, client = room_state
    config.agents["agent2"].thread_mode = "room"
    config.matrix_space.enabled = False
    for name in ("agent1", "agent2"):
        events["m.room.member", ids[name].full_id] = {"membership": "leave"}
    _sync_live_room(room_state)
    snapshot = await read_room_state(client, ROOM_ID)
    duties = _router_duties(room_state)
    runtime = duties.deps.runtime.orchestrator
    runtime.response_admission_gate.closed = True
    setup, worker = _room_setup(room_state, runtime)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(rooms_module, "reconcile_managed_rooms", AsyncMock(return_value={ROOM_ID: snapshot}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())

    async def join(name: str) -> None:
        events["m.room.member", ids[name].full_id] = {"membership": "join"}
        _sync_live_room(room_state)
        await duties.member_event(
            client.rooms[ROOM_ID],
            _membership_event(ids[name].full_id, "join", "leave"),
        )

    async def joins() -> None:
        await join("agent1")
        runtime.response_admission_gate.closed = False
        await setup.publish_room_state()
        assert await wait_for_background_tasks(timeout=2, owner=worker)
        await join("agent2")

    bot = AsyncMock(agent_name="agent1", client=client, agent_user=MagicMock(user_id=ids["agent1"].full_id))
    bot.ensure_rooms.side_effect = joins
    await setup.setup([bot])
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_show_does_not_publish(room_state: RoomState) -> None:
    """Reading the runtime override does not mutate room state."""
    config, paths, _, _, client = room_state
    await handle_thread_mode_command(
        "show",
        client=client,
        config=config,
        runtime_paths=paths,
        room_id=ROOM_ID,
        requester_user_id="@admin:localhost",
        sender_user_id="@admin:localhost",
    )
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [nio.RoomGetStateEventError("Denied", "M_FORBIDDEN"), TimeoutError()])
async def test_event_read_failure_is_nonfatal_and_retried(room_state: RoomState, failure: object) -> None:
    """A failed comparison cannot write unproven state or disable the next trigger."""
    _config, _paths, _, events, client = room_state
    setup, worker = _room_setup(room_state)
    read = client.room_get_state_event.side_effect
    client.room_get_state_event.side_effect = failure if isinstance(failure, Exception) else None
    client.room_get_state_event.return_value = failure
    await setup.publish_room_state()
    await _yield_loop(40)
    assert not _writes(client, EVENT_TYPE)
    client.room_get_state_event.side_effect = read
    await setup.publish_room_state()
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
async def test_same_mode_membership_change_retries_failed_write(room_state: RoomState) -> None:
    """A responder join retries a failed write even when configured modes are unchanged."""
    config, paths, ids, events, client = room_state
    write = client.room_put_state.side_effect
    client.room_put_state.side_effect = TimeoutError()
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.side_effect = write
    await _member_changed(
        _router_duties(room_state),
        client.rooms[ROOM_ID],
        _membership_event(ids["agent2"].full_id, "join", "leave"),
    )
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["invite", "leave", "ban"])
async def test_configured_absent_responder_does_not_determine_view(
    room_state: RoomState,
    membership: str,
) -> None:
    """Configured entities that cannot reply do not affect the room view."""
    config, paths, ids, events, client = room_state
    events["m.room.member", ids["agent2"].full_id] = {"membership": membership}
    _sync_live_room(room_state)
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
@pytest.mark.parametrize("project_departing", [False, True])
async def test_configured_departure_with_lazy_projection(
    room_state: RoomState,
    *,
    project_departing: bool,
) -> None:
    """A live departure excludes the leaving entity without losing an unloaded responder."""
    _, _, ids, events, client = room_state
    events[EVENT_TYPE, ""] = {"mode": "mixed"}
    room = nio.MatrixRoom(ROOM_ID, client.user_id)
    if project_departing:
        room.add_member(ids["agent2"].full_id, None, None)
    client.rooms[ROOM_ID] = room
    events["m.room.member", ids["agent2"].full_id] = {"membership": "leave"}
    await _member_changed(
        _router_duties(room_state),
        room,
        _membership_event(ids["agent2"].full_id, "leave", "join"),
    )
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_joined_team_excludes_departed_configured_individual(room_state: RoomState) -> None:
    """A departed room-mode individual cannot change a joined thread-mode team's view."""
    config, paths, ids, events, client = room_state
    config.agents["agent2"].rooms = []
    config.teams["team1"] = TeamConfig(
        display_name="Team 1",
        role="Test team",
        agents=["agent1", "agent2"],
        rooms=[ROOM_ID],
    )
    ids = entity_ids(config, paths)
    events["m.room.member", ids["agent1"].full_id] = {"membership": "leave"}
    events["m.room.member", ids["team1"].full_id] = {"membership": "join"}
    _sync_live_room(room_state)
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "thread"}


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [False, True])
async def test_sweep_skips_rooms_without_joined_responders(room_state: RoomState, *, override: bool) -> None:
    """Spaces and other rooms without responders do not receive presentation state or warnings."""
    _config, paths, ids, events, client = room_state
    for name in ("agent1", "agent2"):
        events["m.room.member", ids[name].full_id] = {"membership": "leave"}
    if override:
        set_room_thread_mode_override(paths, room_id=ROOM_ID, mode="room")
    _sync_live_room(room_state)
    with capture_logs() as logs:
        await _publish_held(room_state)
    assert not _writes(client, EVENT_TYPE)
    assert not any(log["log_level"] == "warning" for log in logs)


@pytest.mark.asyncio
async def test_membership_lookup_race_logs_debug(room_state: RoomState) -> None:
    """A lookup that a join interrupts on every read is observable and leaves publication to a later trigger."""
    config, paths, _, _, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    read = client.joined_members.side_effect
    joins = iter(range(3))

    async def human_joins(room_id: str) -> nio.JoinedMembersResponse:
        response = await read(room_id)
        client.rooms[room_id].add_member(f"@human{next(joins)}:localhost", None, None)
        return response

    client.joined_members.side_effect = human_joins
    with capture_logs() as logs:
        await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.assert_not_awaited()
    assert any(
        log["event"] == "room_thread_mode_membership_unavailable" and log["log_level"] == "debug" for log in logs
    )


@pytest.mark.asyncio
async def test_router_invite_join_after_agent_publishes_on_next_frame(room_state: RoomState) -> None:
    """A router that joins after an agent publishes once the next sync frame carries the room's members."""
    config, paths, ids, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    del events["m.room.member", ids["agent2"].full_id]
    router_id = ids[ROUTER_AGENT_NAME].full_id
    user = AgentMatrixUser(ROUTER_AGENT_NAME, router_id, "Router", TEST_PASSWORD)
    bot = install_runtime_journal_support(make_test_agent_bot(user, config=config, runtime_paths=paths))
    bot.client = client
    bot.orchestrator.handle_bot_ready = AsyncMock()
    bot.mark_sync_loop_started()
    client.rooms.clear()
    await bot._on_ingestion_frame_completion(True)
    # A local join leaves nio an empty room until the next sync frame.
    client.rooms[ROOM_ID] = nio.MatrixRoom(ROOM_ID, router_id)
    admission = _validated_reported_membership_admission(
        bot,
        ROOM_ID,
        previous_membership="invite",
        membership="join",
        previous_epoch=0,
    )
    principal = MagicMock(ingestion_membership_position=AsyncMock(return_value=RoomMembershipPosition("join", 0)))
    with patch.object(bot, "journal_principal", return_value=principal):
        await bot._after_ingestion_admission(admission, AdmissionFacts(True, False), None)
    await _yield_loop(30)
    assert (EVENT_TYPE, "") not in events
    _sync_live_room(room_state)
    await bot._on_ingestion_frame_completion(True)
    assert await wait_for_background_tasks(timeout=2, owner=bot.orchestrator.room_state)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    assert _accounts(room_state) == sorted([router_id, ids["agent1"].full_id])
    # The frame publishes a join once; later frames reconcile nothing until another join.
    client.room_get_state_event.reset_mock()
    await bot._on_ingestion_frame_completion(True)
    assert await wait_for_background_tasks(timeout=2, owner=bot.orchestrator.room_state)
    client.room_get_state_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["startup", "router_join"])
async def test_router_lists_joined_mindroom_accounts_by_any_user_name(room_state: RoomState, trigger: str) -> None:
    """The router lists its joined router, agent, and team accounts, custom user names included, and nobody else."""
    config, paths, _, events, client = room_state
    config.agents["mind"] = AgentConfig(display_name="Alice's mind")
    config.teams["team1"] = TeamConfig(display_name="Team 1", role="Test team", agents=["agent1"])
    ids = entity_ids(config, paths, usernames={"mind": "alice.mind"})
    for user_id in (ids["mind"].full_id, ids["team1"].full_id, "@alice:localhost"):
        events["m.room.member", user_id] = {"membership": "join"}
    events["m.room.member", ids["agent2"].full_id] = {"membership": "invite"}
    _sync_live_room(room_state)
    if trigger == "startup":
        await _publish_held(room_state)
    else:
        await _member_changed(
            _router_duties(room_state),
            client.rooms[ROOM_ID],
            _membership_event(ids["agent1"].full_id, "join", "invite"),
        )
    assert _accounts(room_state) == sorted(
        ["@alice.mind:localhost", *(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "team1"))],
    )


@pytest.mark.asyncio
async def test_config_reload_drops_an_entity_the_config_no_longer_has(room_state: RoomState) -> None:
    """A reload republishes the list, so a removed agent that is still joined is no longer listed."""
    config, paths, ids, _, _client = room_state
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=config)
    orchestrator.running = True
    install_bots(orchestrator, {ROUTER_AGENT_NAME: _owner_bot(room_state)})
    await orchestrator.config_reload.after_reload()
    assert await wait_for_background_tasks(timeout=2, owner=orchestrator.room_state)
    assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "agent2"))
    del config.agents["agent2"]
    await orchestrator.config_reload.after_reload()
    assert await wait_for_background_tasks(timeout=2, owner=orchestrator.room_state)
    assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1"))


@pytest.mark.asyncio
async def test_unchanged_accounts_are_written_once(room_state: RoomState) -> None:
    """Later triggers with unchanged membership do not rewrite the list."""
    _config, _paths, ids, _, client = room_state
    setup, worker = _room_setup(room_state)
    for _ in range(3):
        await setup.publish_room_state()
        assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert [write.kwargs for write in _writes(client, ACCOUNTS_EVENT_TYPE)] == [
        {"state_key": ids[ROUTER_AGENT_NAME].full_id},
    ]


@pytest.mark.asyncio
async def test_slower_earlier_trigger_cannot_restore_a_departed_account(room_state: RoomState) -> None:
    """Triggers in one room run one at a time, so the list ends with the latest membership."""
    _, _, ids, events, client = room_state
    duties = _router_duties(room_state)
    worker = _worker(duties)
    agent2 = ids["agent2"].full_id
    read_members = client.joined_members.side_effect

    async def depart_after_first_read(room_id: str) -> nio.JoinedMembersResponse:
        client.joined_members.side_effect = read_members
        response = await read_members(room_id)
        events["m.room.member", agent2] = {"membership": "leave"}
        await duties.member_event(client.rooms[ROOM_ID], _membership_event(agent2, "leave", "join"))
        return response

    client.joined_members.side_effect = depart_after_first_read
    worker.mark(ROOM_ID)
    assert await wait_for_background_tasks(timeout=2, owner=worker)
    assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        nio.JoinedMembersError("unavailable"),
        nio.RoomPutStateError("forbidden", "M_FORBIDDEN"),
        TimeoutError(),
    ],
)
async def test_accounts_failure_is_nonfatal_and_retried(room_state: RoomState, failure: object) -> None:
    """A failed member read or write leaves the list unpublished without failing the trigger, and the next one publishes."""
    _, _, ids, _, client = room_state
    mock = client.joined_members if isinstance(failure, nio.JoinedMembersError) else client.room_put_state
    original = mock.side_effect
    mock.side_effect = failure if isinstance(failure, Exception) else None
    mock.return_value = failure
    duties = _router_duties(room_state)
    agent2 = ids["agent2"].full_id
    await duties.member_event(client.rooms[ROOM_ID], _membership_event(agent2, "join", "leave"))
    await _yield_loop(40)
    assert _accounts(room_state) is None
    mock.side_effect = original
    await _member_changed(duties, client.rooms[ROOM_ID], _membership_event(agent2, "join", "leave"))
    assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "agent2"))


@pytest.mark.asyncio
async def test_blocked_accounts_get_does_not_delay_other_room_state(room_state: RoomState) -> None:
    """A slow accounts state read in one room must not delay mode or binding in another."""
    config, _, ids, _, client = room_state
    other = "!other:localhost"
    client.rooms[other] = nio.MatrixRoom(other, client.user_id)
    config.agents["agent1"].rooms.append(other)
    blocked = asyncio.Event()
    release = asyncio.Event()
    other_written = asyncio.Event()
    write = client.room_put_state.side_effect
    read = client.room_get_state_event.side_effect

    async def read_state(room_id: str, event_type: str, state_key: str = "") -> object:
        if room_id == ROOM_ID and event_type == ACCOUNTS_EVENT_TYPE:
            blocked.set()
            await release.wait()
        if room_id == other and event_type == ROUTER_EVENT_TYPE:
            return nio.RoomGetStateEventError("Absent", "M_NOT_FOUND", room_id=room_id)
        return await read(room_id, event_type, state_key)

    async def write_state(room_id: str, event_type: str, content: dict, **kwargs: object) -> object:
        result = await write(room_id, event_type, content, **kwargs)
        if room_id == other and event_type == ROUTER_EVENT_TYPE:
            other_written.set()
        return result

    client.room_get_state_event.side_effect = read_state
    client.room_put_state.side_effect = write_state
    duties = _router_duties(room_state)
    worker = _worker(duties)
    worker.mark(ROOM_ID)
    try:
        await asyncio.wait_for(blocked.wait(), timeout=1)
        user_id = ids["agent1"].full_id
        await asyncio.wait_for(
            duties.member_event(client.rooms[other], _membership_event(user_id, "join", "leave")),
            timeout=1,
        )
        await asyncio.wait_for(other_written.wait(), timeout=1)
        assert call(other, EVENT_TYPE, {"mode": "room"}, state_key="") in _writes(client, EVENT_TYPE)
        assert call(other, ROUTER_EVENT_TYPE, {"user_id": client.user_id}, state_key="") in _writes(
            client,
            ROUTER_EVENT_TYPE,
        )
    finally:
        release.set()
        assert await wait_for_background_tasks(timeout=2, owner=worker)


async def _yield_loop(turns: int) -> None:
    """Let ready tasks run without advancing the fake clock or waiting indefinitely."""
    for _ in range(turns):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_thread_mode_command_times_out_while_a_frame_uses_the_room_lock(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual command has one publication deadline even behind an ad-hoc roster lookup."""
    config, paths, _, _, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)

    async def unanswered(_room_id: str) -> object:
        await asyncio.Future()

    client.joined_members.side_effect = unanswered
    worker = _worker(_router_duties(room_state))
    worker.mark(ROOM_ID)
    await _yield_loop(30)
    client.joined_members.assert_awaited_once()
    command = asyncio.create_task(
        handle_thread_mode_command(
            "thread",
            client=client,
            config=config,
            runtime_paths=paths,
            room_id=ROOM_ID,
            requester_user_id="@admin:localhost",
            sender_user_id="@admin:localhost",
        ),
    )
    try:
        for tick in (0, 9, 10):
            clock = tick
            for _ in range(30):
                await asyncio.sleep(0)
        assert command.done()
        assert "publishing the new one failed" in await command
    finally:
        worker.stop()
        command.cancel()
        await asyncio.gather(command, return_exceptions=True)
        await _yield_loop(10)


@pytest.mark.asyncio
async def test_setup_sweep_logs_a_real_roster_deadline(room_state: RoomState, monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-flight request takes the retry path and logs its own deadline, not silent caller cancellation."""
    _, _, _, _, client = room_state
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)

    async def unanswered(_room_id: str) -> object:
        await asyncio.Future()

    client.joined_members.side_effect = unanswered
    setup, worker = _room_setup(room_state)
    try:
        with capture_logs() as logs:
            await setup.publish_room_state()
            client.joined_members.assert_not_awaited()
            for tick in (0, 9, 10):
                clock = tick
                await _yield_loop(30)
            assert len(_writes(client, EVENT_TYPE)) == 1
            assert len(_writes(client, ROUTER_EVENT_TYPE)) == 1
            assert [entry for entry in logs if entry["event"] == "room_accounts_publish_failed"] == [
                {
                    "event": "room_accounts_publish_failed",
                    "room_id": ROOM_ID,
                    "log_level": "info",
                    "error": "TimeoutError",
                    "phase": "joined_members",
                },
            ]
    finally:
        worker.stop()
        await _yield_loop(10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "diagnostic"),
    [
        (ACCOUNTS_EVENT_TYPE, "room_accounts_publish_failed"),
        (EVENT_TYPE, "room_thread_mode_reconcile_failed"),
        (ROUTER_EVENT_TYPE, "room_router_binding_publish_failed"),
    ],
)
async def test_room_state_timeout_is_retryable_and_logs_one_compact_warning(
    room_state: RoomState,
    event_type: str,
    diagnostic: str,
) -> None:
    """All three operations report a retryable timeout without a traceback."""
    config, paths, _, _, client = room_state
    client.room_put_state.side_effect = TimeoutError
    with capture_logs() as logs:
        if event_type == ROUTER_EVENT_TYPE:
            current = await reconcile_room_router_binding(client, ROOM_ID)
        else:
            reconcile = reconcile_room_accounts if event_type == ACCOUNTS_EVENT_TYPE else reconcile_room_thread_mode
            current = await reconcile(client, config, paths, ROOM_ID)
    assert current is False
    assert logs == [
        {
            "event": diagnostic,
            "log_level": "warning",
            "room_id": ROOM_ID,
            "error": "TimeoutError",
            "phase": "state_put",
        },
    ]


@pytest.mark.asyncio
async def test_healthy_room_gets_a_turn_between_slow_roster_retries(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jitter lets B publish even though A keeps consuming the shared crypto lock's entire deadline."""
    config, _, _, _, client = room_state
    healthy = "!healthy:localhost"
    client.rooms[healthy] = nio.MatrixRoom(healthy, client.user_id)
    config.agents["agent1"].rooms.append(healthy)
    take_high = iter((True, False))  # A takes its upper bound; B takes its lower bound.
    monkeypatch.setattr(
        "mindroom.orchestration.room_state.uniform",
        lambda low, high: high if next(take_high, True) else low,
    )
    clock = 0.0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    crypto = asyncio.Lock()
    read = client.joined_members.side_effect

    async def roster(room_id: str) -> nio.JoinedMembersResponse:
        async with crypto:
            await asyncio.sleep(11 if room_id == ROOM_ID else 1)
            return await read(room_id)

    client.joined_members.side_effect = roster
    worker = _worker(_router_duties(room_state))
    try:
        for quarter_second in range(121):
            clock = quarter_second / 4
            if quarter_second < 2:
                worker.mark(ROOM_ID if quarter_second == 0 else healthy)
            for _ in range(20):
                await asyncio.sleep(0)
            if _writes(client, ACCOUNTS_EVENT_TYPE):
                break
        assert [write.args[0] for write in _writes(client, ACCOUNTS_EVENT_TYPE)] == [healthy]
        assert clock < 30
    finally:
        worker.stop()
        await _yield_loop(10)


@pytest.mark.asyncio
async def test_ad_hoc_room_publishes_mixed_mode_on_its_first_retry(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out membership lookup leaves mode retryable while accounts and binding finish."""
    config, _, _, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    events[EVENT_TYPE, ""] = {"mode": "room"}
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    read = client.joined_members.side_effect

    async def roster(room_id: str) -> nio.JoinedMembersResponse:
        client.joined_members.side_effect = read
        await asyncio.Future()
        return await read(room_id)

    client.joined_members.side_effect = roster
    worker = _worker(_router_duties(room_state))
    try:
        worker.mark(ROOM_ID)
        await _yield_loop(30)
        clock = 10
        await _yield_loop(30)
        assert events[EVENT_TYPE, ""] == {"mode": "room"}
        assert len(_writes(client, ACCOUNTS_EVENT_TYPE)) == 1
        assert len(_writes(client, ROUTER_EVENT_TYPE)) == 1
        clock = 11
        await _yield_loop(30)
        assert events[EVENT_TYPE, ""] == {"mode": "mixed"}
        assert client.joined_members.await_count == 3
    finally:
        worker.stop()
        await _yield_loop(10)


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_stewardship", [False, True])
async def test_sweep_retries_failed_room_with_backoff(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    lose_stewardship: bool,
) -> None:
    """Two timeouts do not strand a room; losing stewardship aborts the next attempt."""
    _, _, _, _, client = room_state
    duties = _router_duties(room_state)
    worker = _worker(duties)
    loop = asyncio.get_running_loop()
    now = 0
    monkeypatch.setattr(loop, "time", lambda: now)
    write = client.room_put_state.side_effect
    times: list[int] = []

    async def fail_twice(room_id: str, event_type: str, content: dict, **kwargs: object) -> object:
        if event_type == ACCOUNTS_EVENT_TYPE:
            times.append(now)
            if len(times) < 3:
                raise TimeoutError
        return await write(room_id, event_type, content, **kwargs)

    client.room_put_state.side_effect = fail_twice
    with capture_logs() as logs:
        await worker.mark_held()
        for now in range(6):
            if now == 2 and lose_stewardship:
                duties.deps.runtime.orchestrator.room_steward.return_value = None
            for _ in range(30):
                await asyncio.sleep(0)
    assert times == ([0, 1] if lose_stewardship else [0, 1, 3])
    failures = [log for log in logs if log["event"] == "room_accounts_publish_failed"]
    assert [log["log_level"] for log in failures] == ["info", "info"]
    assert all(log["error"] == "TimeoutError" and "exc_info" not in log for log in failures)
    assert (_accounts(room_state) is None) == lose_stewardship


@pytest.mark.asyncio
async def test_slower_earlier_trigger_cannot_restore_a_replaced_thread_mode(room_state: RoomState) -> None:
    """Triggers in one room run one at a time, so the mode ends with the latest override."""
    config, paths, _, events, client = room_state
    read_event = client.room_get_state_event.side_effect

    async def override_during_first_compare(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == EVENT_TYPE:
            client.room_get_state_event.side_effect = read_event
            set_room_thread_mode_override(paths, room_id=ROOM_ID, mode="room")
            for _ in range(10):
                await asyncio.sleep(0)
        return await read_event(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = override_during_first_compare
    await asyncio.gather(*(reconcile_room_thread_mode(client, config, paths, ROOM_ID) for _ in range(2)))
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["room", "reset"])
async def test_command_reports_a_failed_publication(room_state: RoomState, command: str) -> None:
    """The command changes nothing and asks for moderator rights when the room refuses the state its clients read."""
    config, paths, _, _, client = room_state
    set_room_thread_mode_override(paths, room_id=ROOM_ID, mode="thread")
    for _ in range(2):  # A second failure answers the same.
        reply = await handle_thread_mode_command(
            command,
            client=client,
            config=config,
            runtime_paths=paths,
            room_id=ROOM_ID,
            requester_user_id="@admin:localhost",
            sender_user_id="@admin:localhost",
            put_state=AsyncMock(return_value=None),
        )
        assert reply == "I need moderator rights in this room to change the thread mode."
        assert resolve_room_thread_mode_override(paths, ROOM_ID) == "thread"
