"""The steward-published room state clients use to choose a conversation view and recognize MindRoom accounts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import nio
import pytest
from aiohttp import web
from structlog.testing import capture_logs

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot_runtime_view import RunPhase
from mindroom.commands.thread_mode_commands import handle_thread_mode_command
from mindroom.config.agent import AgentConfig, RoomConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import AdmissionFacts, RoomMembershipPosition
from mindroom.matrix.client_session import MindRoomAsyncClient
from mindroom.matrix.room_accounts import reconcile_room_accounts, reconcile_room_router_binding
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration import rooms as rooms_module
from mindroom.orchestration.rooms import RoomSetup
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.room_settings import THREAD_MODE, observe_room_settings_event, resolve_room_setting
from mindroom.runtime_shutdown import ShutdownBudget
from mindroom.steward_duties import StewardDuties, StewardDutiesDeps, reconcile_held_room_states
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
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID

EVENT_TYPE = "io.mindroom.thread_mode"
ACCOUNTS_EVENT_TYPE = "io.mindroom.accounts"
ROUTER_EVENT_TYPE = "io.mindroom.router"
# The room's `thread_mode` setting, which the thread mode state is published from.
SETTING = ("io.mindroom.room_settings", "thread_mode")
ROOM_ID = "!room:localhost"

type RoomState = tuple[Config, RuntimePaths, dict[str, MatrixID], dict[tuple[str, str], dict[str, str]], AsyncMock]


@pytest.fixture
def room_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RoomState:
    """Keep a mutable homeserver and use nominal delays unless a test supplies its own jitter draws."""
    monkeypatch.setattr("mindroom.steward_duties.uniform", lambda low, high: min(low * 2, high))
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
    events[SETTING] = {"value": "thread"}
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "thread"}
    events[SETTING] = {}
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
@pytest.mark.parametrize(("status_code", "level"), [("M_FORBIDDEN", "debug"), ("M_UNKNOWN", "warning")])
async def test_a_room_that_refuses_the_routers_state_is_no_warning(
    room_state: RoomState,
    status_code: str,
    level: str,
) -> None:
    """Where the room's power levels refuse the router's state, a start logs the refusal at debug and still tries it."""
    _config, _paths, _, _, client = room_state
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("user_level (0) < send_level (50)", status_code)
    duties = _owner_bot(room_state).steward_duties
    try:
        with capture_logs() as logs:
            await reconcile_held_room_states(duties.deps.runtime.orchestrator)
        if status_code == "M_FORBIDDEN":
            assert not duties._room_state_retries
            assert client.room_put_state.await_count == 3  # Exactly one PUT for each state event.
    finally:
        duties.stopping()
    assert {write.args[1] for write in client.room_put_state.await_args_list} == {
        EVENT_TYPE,
        ACCOUNTS_EVENT_TYPE,
        ROUTER_EVENT_TYPE,
    }
    assert {log["event"] for log in logs} == {
        "room_accounts_publish_failed",
        "room_router_binding_publish_failed",
        "room_thread_mode_reconcile_failed",
    }
    assert sorted(log["log_level"] for log in logs) == ["debug", "debug", level]


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
    duties = _router_duties(room_state)
    try:
        duties.mark_joined_for_room_state(ROOM_ID)
        await _publish_joined_rooms(duties)
        assert not _writes(client, ROUTER_EVENT_TYPE)
        assert not duties._room_state_reruns
    finally:
        duties.stopping()


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
    assert await reconcile_room_router_binding(client, ROOM_ID) is True
    duties = _router_duties(room_state)
    duties.mark_joined_for_room_state(ROOM_ID)
    await _publish_joined_rooms(duties)
    assert not duties._room_state_retries


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
    """Both router room-state paths, a start or reload and a room the router joined, publish the binding."""
    _config, _paths, ids, events, _client = room_state
    binding = {"user_id": ids[ROUTER_AGENT_NAME].full_id}

    await _room_setup(room_state).publish_room_state()
    assert events[ROUTER_EVENT_TYPE, ""] == binding

    del events[ROUTER_EVENT_TYPE, ""]
    duties = _router_duties(room_state)
    duties.mark_joined_for_room_state(ROOM_ID)
    await _publish_joined_rooms(duties)
    assert events[ROUTER_EVENT_TYPE, ""] == binding


@pytest.mark.asyncio
async def test_binding_claim_follows_a_handover_during_accounts_read(room_state: RoomState) -> None:
    """The old steward retains PUT permission but must leave the newly vacant binding to its successor."""
    _, _, ids, events, client = room_state
    outgoing = _owner_bot(room_state).steward_duties
    runtime = outgoing.deps.runtime.orchestrator
    successor = _router_duties(room_state)
    successor.deps.runtime.orchestrator = runtime
    successor.deps.hook_context.agent_name = "agent1"
    next_client = AsyncMock(spec=nio.AsyncClient, user_id=ids["agent1"].full_id, rooms=client.rooms)
    next_client.joined_members.side_effect = client.joined_members.side_effect
    next_client.room_put_state.side_effect = client.room_put_state.side_effect
    read = client.room_get_state_event.side_effect
    next_client.room_get_state_event.side_effect = read
    successor.deps.runtime.client = next_client

    accounts_read = asyncio.Event()
    handover = asyncio.Event()

    async def outgoing_read(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == ACCOUNTS_EVENT_TYPE:
            accounts_read.set()
        if event_type in (ACCOUNTS_EVENT_TYPE, ROUTER_EVENT_TYPE):
            await handover.wait()
        return await read(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = outgoing_read
    outgoing.mark_joined_for_room_state(ROOM_ID)
    frame = _publish_joined_rooms(outgoing)
    try:
        await asyncio.wait_for(accounts_read.wait(), timeout=1)
        runtime.room_steward.return_value = MagicMock(agent_name="agent1", steward_duties=successor)
    finally:
        handover.set()
        await frame

    handed_off = ROOM_ID in successor._joined_room_ids
    successor.publish_joined_room_state(every_room=True)
    (next_frame,) = successor._room_state_publishes
    await next_frame
    assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": next_client.user_id}
    assert not _writes(client, ROUTER_EVENT_TYPE)
    assert handed_off


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding",
    [
        "writable",
        "absent",
        "stopped",
        "stops_during_get",
        "unjoined",
        "underpowered",
        "draining",
        "drains_during_roster_get",
        "members_forbidden",
        "put_forbidden",
    ],
)
async def test_roster_follows_the_binding_after_steward_handover(room_state: RoomState, binding: str) -> None:
    """The steward refreshes the trusted roster through its author when that bot can still write it."""
    _, _, ids, events, bound_client = room_state
    owner = _owner_bot(room_state)
    runtime = owner.steward_duties.deps.runtime.orchestrator
    bound_id, steward_id, joining_id = (ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "agent2"))
    bound_room = bound_client.rooms[ROOM_ID]
    bound_room.power_levels.events[ACCOUNTS_EVENT_TYPE] = 50
    bound_room.power_levels.users[bound_id] = 50
    events["m.room.member", joining_id] = {"membership": "leave"}
    await reconcile_held_room_states(runtime)
    initial_roster = sorted([bound_id, steward_id])
    assert _accounts(room_state) == initial_roster
    assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": bound_id}
    bound_client.room_put_state.reset_mock()
    bound_client.joined_members.reset_mock()

    duties = _roster_successor(room_state, owner)
    steward_client = duties.deps.runtime.client
    assert steward_client is not None
    _configure_roster_handover(room_state, owner, binding)
    events["m.room.member", joining_id] = {"membership": "join"}
    try:
        with capture_logs() as logs:
            if binding == "stops_during_get":
                await _reconcile_accounts_during_bound_bot_stop(duties, steward_client, owner)
            else:
                await duties.member_event(steward_client.rooms[ROOM_ID], _membership_event(joining_id, "join", "leave"))
            if binding == "drains_during_roster_get":
                assert duties._room_state_reruns[ROOM_ID] == {"accounts"}
                assert (ACCOUNTS_EVENT_TYPE, steward_id) not in events
                await asyncio.wait_for(duties._room_state_retries[ROOM_ID], timeout=3)
            assert duties._room_state_reruns == ({ROOM_ID: {"accounts"}} if binding == "members_forbidden" else {})
    finally:
        duties.stopping()

    if binding in {"members_forbidden", "put_forbidden"}:
        _assert_bound_writer_refused(events, bound_client, steward_client, binding, logs)
    else:
        expected_client = bound_client if binding == "writable" else steward_client
        expected_id = expected_client.user_id
        expected_roster = {"accounts": sorted([*initial_roster, joining_id])}
        assert events[ACCOUNTS_EVENT_TYPE, expected_id] == expected_roster
        write = call(ROOM_ID, ACCOUNTS_EVENT_TYPE, expected_roster, state_key=expected_id)
        assert _writes(expected_client, ACCOUNTS_EVENT_TYPE) == [write]
        other_client = steward_client if binding == "writable" else bound_client
        assert not _writes(other_client, ACCOUNTS_EVENT_TYPE)
    if binding != "writable":
        assert _accounts(room_state) == initial_roster
    assert events[ROUTER_EVENT_TYPE, ""] == {"user_id": steward_id if binding == "absent" else bound_id}
    assert len(_writes(steward_client, ROUTER_EVENT_TYPE)) == (binding == "absent")
    assert not _writes(bound_client, ROUTER_EVENT_TYPE)


def _assert_bound_writer_refused(
    events: dict[tuple[str, str], dict[str, Any]],
    bound_client: AsyncMock,
    steward_client: AsyncMock,
    binding: str,
    logs: list[dict[str, Any]],
) -> None:
    assert (ACCOUNTS_EVENT_TYPE, steward_client.user_id) not in events
    assert not _writes(steward_client, ACCOUNTS_EVENT_TYPE)
    assert len(_writes(bound_client, ACCOUNTS_EVENT_TYPE)) == (binding == "put_forbidden")
    if binding == "members_forbidden":
        bound_client.joined_members.assert_awaited_once_with(ROOM_ID)
    event_name = (
        "matrix_room_members_fetch_failed" if binding == "members_forbidden" else "room_accounts_publish_failed"
    )
    expected_level = "warning" if binding == "members_forbidden" else "debug"
    assert [log["log_level"] for log in logs if log["event"] == event_name] == [expected_level]


def _roster_successor(room_state: RoomState, owner: MagicMock) -> StewardDuties:
    """Elect a new steward with its own client, sharing the fake homeserver with the bound bot."""
    _, _, ids, _, bound_client = room_state
    runtime = owner.steward_duties.deps.runtime.orchestrator
    steward_id = ids["agent1"].full_id
    steward_room = nio.MatrixRoom(ROOM_ID, steward_id)
    steward_room.power_levels.users[steward_id] = 100
    steward_client = AsyncMock(spec=nio.AsyncClient, user_id=steward_id, rooms={ROOM_ID: steward_room})
    steward_client.joined_members.side_effect = bound_client.joined_members.side_effect
    steward_client.room_get_state_event.side_effect = bound_client.room_get_state_event.side_effect
    steward_client.room_put_state.side_effect = bound_client.room_put_state.side_effect
    duties = _router_duties(room_state)
    duties.deps.hook_context.agent_name = "agent1"
    duties.deps.runtime.client = steward_client
    duties.deps.runtime.orchestrator = runtime
    steward = MagicMock(client=steward_client, agent_name="agent1", running=True, steward_duties=duties)
    runtime.agent_bots["agent1"] = steward
    runtime.room_steward.return_value = steward
    return duties


def _configure_roster_handover(
    state: RoomState,
    owner: MagicMock,
    binding: str,
) -> None:
    """Make the bound bot ineligible, refuse it on the server, or remove the binding for this case."""
    _, _, _, events, bound_client = state
    owner.running = binding != "stopped"
    owner.accepting_work = binding not in {"stopped", "draining"}
    if binding == "absent":
        del events[ROUTER_EVENT_TYPE, ""]
    elif binding == "unjoined":
        del bound_client.rooms[ROOM_ID]
    elif binding == "underpowered":
        bound_client.rooms[ROOM_ID].power_levels.users[bound_client.user_id] = 0
    elif binding == "drains_during_roster_get":
        read = bound_client.room_get_state_event.side_effect

        async def stop_accepting(room_id: str, event_type: str, state_key: str = "") -> object:
            reply = await read(room_id, event_type, state_key)
            if event_type == ACCOUNTS_EVENT_TYPE:
                owner.accepting_work = False
            return reply

        bound_client.room_get_state_event.side_effect = stop_accepting
    elif binding == "members_forbidden":
        bound_client.joined_members.side_effect = None
        bound_client.joined_members.return_value = nio.JoinedMembersError("Forbidden", "M_FORBIDDEN", room_id=ROOM_ID)
    elif binding == "put_forbidden":
        bound_client.room_put_state.side_effect = None
        bound_client.room_put_state.return_value = nio.RoomPutStateError("Forbidden", "M_FORBIDDEN")


@pytest.mark.asyncio
async def test_bound_writer_draining_during_equal_roster_get_retries(room_state: RoomState) -> None:
    """A bound writer that drains during an equal roster GET triggers a retry instead of reporting success."""
    config, paths, ids, events, steward = room_state
    bound_id = ids["agent1"].full_id
    expected_roster = sorted(identity.full_id for identity in ids.values())
    events[ROUTER_EVENT_TYPE, ""] = {"user_id": bound_id}
    events[ACCOUNTS_EVENT_TYPE, bound_id] = {"accounts": expected_roster}
    bound = AsyncMock(spec=nio.AsyncClient, user_id=bound_id)
    bound.joined_members.side_effect = steward.joined_members.side_effect
    eligible = True
    read = steward.room_get_state_event.side_effect

    async def drain_on_roster_get(room_id: str, event_type: str, state_key: str = "") -> object:
        nonlocal eligible
        reply = await read(room_id, event_type, state_key)
        if event_type == ACCOUNTS_EVENT_TYPE:
            eligible = False
        return reply

    bound.room_get_state_event.side_effect = drain_on_roster_get

    def alternates() -> dict[str, nio.AsyncClient]:
        return {bound_id: bound} if eligible else {}

    with capture_logs() as logs:
        assert await reconcile_room_accounts(steward, config, paths, ROOM_ID, get_alternate_clients=alternates) is False
    assert [log["log_level"] for log in logs if log["event"] == "room_accounts_writer_no_longer_eligible"] == ["debug"]
    bound.room_put_state.assert_not_awaited()
    steward.room_put_state.assert_not_awaited()

    assert await reconcile_room_accounts(steward, config, paths, ROOM_ID, get_alternate_clients=alternates) is True
    assert _accounts(room_state) == expected_roster


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("writer_kind", "response_kind"),
    [
        ("bound", "forbidden_503"),
        ("steward", "forbidden_503"),
        ("steward", "forbidden_403"),
    ],
)
async def test_forbidden_members_read_retries_without_writing(
    room_state: RoomState,
    writer_kind: str,
    response_kind: str,
) -> None:
    """Any failed joined_members read retries for either writer, even with an M_FORBIDDEN body."""
    config, paths, ids, _, _ = room_state
    steward_id, bound_id = ids[ROUTER_AGENT_NAME].full_id, ids["agent1"].full_id
    puts: list[str] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.method == "PUT":
            puts.append(request.path)
            return web.json_response({"event_id": "$put"})
        if ROUTER_EVENT_TYPE in request.path:
            return web.json_response({"user_id": bound_id})
        return _error_http_response(response_kind)

    async with _http_state_clients(handle, steward_id, bound_id) as (steward, bound):
        alternates = (lambda: {bound_id: bound}) if writer_kind == "bound" else dict
        with capture_logs() as logs:
            result = await reconcile_room_accounts(steward, config, paths, ROOM_ID, get_alternate_clients=alternates)
    assert result is False
    assert puts == []
    assert [log["log_level"] for log in logs if log["event"] == "matrix_room_members_fetch_failed"] == ["warning"]


@asynccontextmanager
async def _http_state_clients(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    *user_ids: str,
) -> AsyncIterator[list[MindRoomAsyncClient]]:
    """Run real MindRoomAsyncClient instances against an in-process HTTP handler."""
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    host, port = runner.addresses[0]
    clients = [MindRoomAsyncClient(f"http://{host}:{port}", user_id, "BOT", store_path=None) for user_id in user_ids]
    for client, user_id in zip(clients, user_ids, strict=True):
        client.restore_login(user_id, "BOT", "token")
        client.rooms[ROOM_ID] = nio.MatrixRoom(ROOM_ID, user_id)
    try:
        yield clients
    finally:
        await asyncio.gather(*(client.close() for client in clients))
        await runner.cleanup()


def _error_http_response(kind: str) -> web.StreamResponse:
    if kind == "html_503":
        return web.Response(text="<html>503 Service Unavailable</html>", status=503, content_type="text/html")
    if kind == "forbidden_403":
        return web.json_response({"errcode": "M_FORBIDDEN", "error": "Forbidden"}, status=403)
    if kind == "html_403":
        return web.Response(text="<html>403 Forbidden</html>", status=403, content_type="text/html")
    if kind == "not_found_503":
        return web.json_response({"errcode": "M_NOT_FOUND", "error": "Not found"}, status=503)
    if kind == "forbidden_503":
        return web.json_response({"errcode": "M_FORBIDDEN", "error": "Forbidden"}, status=503)
    return web.json_response({"errcode": "M_UNKNOWN", "error": "Service Unavailable"}, status=503)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["json_503", "html_503", "not_found_503", "forbidden_503"])
async def test_non_200_binding_read_in_accounts_retries_without_writing(
    room_state: RoomState,
    response_kind: str,
) -> None:
    """A decoded 503 binding GET retries without falling back to a steward roster write."""
    config, paths, ids, _, _ = room_state
    steward_id, bound_id = ids[ROUTER_AGENT_NAME].full_id, ids["agent1"].full_id
    joined = {identity.full_id: {} for identity in ids.values()}
    puts: list[str] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.method == "PUT":
            puts.append(request.path)
            return web.json_response({"event_id": "$put"})
        if request.path.endswith("/joined_members"):
            return web.json_response({"joined": joined})
        if ROUTER_EVENT_TYPE in request.path:
            return _error_http_response(response_kind)
        return web.json_response({"errcode": "M_NOT_FOUND", "error": "Not found"}, status=404)

    async with _http_state_clients(handle, steward_id, bound_id) as (steward, bound):
        with capture_logs() as logs:
            result = await reconcile_room_accounts(
                steward,
                config,
                paths,
                ROOM_ID,
                get_alternate_clients=lambda: {bound_id: bound},
            )
    assert result is False
    assert puts == []
    assert [log["log_level"] for log in logs if log["event"] == "room_accounts_publish_failed"] == ["warning"]


@pytest.mark.asyncio
async def test_forbidden_binding_read_in_accounts_writes_the_steward_roster(room_state: RoomState) -> None:
    """A 403 binding GET leaves no bound writer, so the steward writes its own roster as on main."""
    config, paths, ids, _, _ = room_state
    steward_id, bound_id = ids[ROUTER_AGENT_NAME].full_id, ids["agent1"].full_id
    joined = {identity.full_id: {} for identity in ids.values()}
    puts: list[str] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.method == "PUT":
            puts.append(request.path)
            return web.json_response({"event_id": "$put"})
        if request.path.endswith("/joined_members"):
            return web.json_response({"joined": joined})
        if ROUTER_EVENT_TYPE in request.path:
            return _error_http_response("forbidden_403")
        return web.json_response({"errcode": "M_NOT_FOUND", "error": "Not found"}, status=404)

    async with _http_state_clients(handle, steward_id, bound_id) as (steward, bound):
        result = await reconcile_room_accounts(
            steward,
            config,
            paths,
            ROOM_ID,
            get_alternate_clients=lambda: {bound_id: bound},
        )
    assert result is True
    assert len(puts) == 1
    assert puts[0].endswith(f"/{steward_id}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_kind", "expected_done", "expected_level"),
    [
        ("json_503", False, "warning"),
        ("html_503", False, "warning"),
        ("not_found_503", False, "warning"),
        ("forbidden_503", False, "warning"),
        ("forbidden_403", True, "debug"),
        ("html_403", True, "debug"),
    ],
)
async def test_non_200_binding_read_in_claim_path_never_claims(
    room_state: RoomState,
    response_kind: str,
    expected_done: bool,
    expected_level: str,
) -> None:
    """A decoded non-200 binding GET is never mistaken for a malformed binding to claim."""
    _, _, ids, _, _ = room_state
    steward_id = ids[ROUTER_AGENT_NAME].full_id
    puts: list[str] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.method == "PUT":
            puts.append(request.path)
            return web.json_response({"event_id": "$put"})
        return _error_http_response(response_kind)

    async with _http_state_clients(handle, steward_id) as (steward,):
        with capture_logs() as logs:
            result = await reconcile_room_router_binding(steward, ROOM_ID)
    assert result is expected_done
    assert puts == []
    assert [log["log_level"] for log in logs if log["event"] == "room_router_binding_publish_failed"] == [
        expected_level,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("writer_kind", "response_kind", "expected_done", "expected_level"),
    [
        ("bound", "forbidden_403", True, "debug"),
        ("steward", "forbidden_403", True, "debug"),
        ("bound", "html_403", True, "debug"),
        ("steward", "html_403", True, "debug"),
        ("bound", "json_503", False, "warning"),
        ("steward", "json_503", False, "warning"),
        ("bound", "not_found_503", False, "warning"),
        ("steward", "not_found_503", False, "warning"),
        ("bound", "forbidden_503", False, "warning"),
        ("steward", "forbidden_503", False, "warning"),
    ],
)
async def test_non_200_roster_get_stops_before_put(
    room_state: RoomState,
    writer_kind: str,
    response_kind: str,
    expected_done: bool,
    expected_level: str,
) -> None:
    """A decoded 403 or 503 roster GET stops without issuing a PUT for either writer."""
    config, paths, ids, _, _ = room_state
    steward_id, bound_id = ids[ROUTER_AGENT_NAME].full_id, ids["agent1"].full_id
    joined = {identity.full_id: {} for identity in ids.values()}
    puts: list[str] = []
    roster_reads: list[str] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.method == "PUT":
            puts.append(request.path)
            return web.json_response({"event_id": "$put"})
        if ROUTER_EVENT_TYPE in request.path:
            return web.json_response({"user_id": bound_id})
        if request.path.endswith("/joined_members"):
            return web.json_response({"joined": joined})
        roster_reads.append(request.path.rsplit("/", 1)[-1])
        return _error_http_response(response_kind)

    async with _http_state_clients(handle, steward_id, bound_id) as (steward, bound):
        alternates = (lambda: {bound_id: bound}) if writer_kind == "bound" else dict
        with capture_logs() as logs:
            result = await reconcile_room_accounts(steward, config, paths, ROOM_ID, get_alternate_clients=alternates)
    assert result is expected_done
    assert puts == []
    assert roster_reads == [bound_id if writer_kind == "bound" else steward_id]
    assert [log["log_level"] for log in logs if log["event"] == "room_accounts_publish_failed"] == [expected_level]


async def _reconcile_accounts_during_bound_bot_stop(
    duties: StewardDuties,
    steward_client: AsyncMock,
    bound_bot: MagicMock,
) -> None:
    """Stop the bound bot after the roster's binding GET starts and before it returns."""
    read = steward_client.room_get_state_event.side_effect

    async def stop_during_binding_get(room_id: str, event_type: str, state_key: str = "") -> object:
        reply = await read(room_id, event_type, state_key)
        if event_type == ROUTER_EVENT_TYPE:
            bound_bot.running = False
        return reply

    steward_client.room_get_state_event.side_effect = stop_during_binding_get
    # Run only accounts so the binding check's own GET cannot stop the bot first.
    assert bound_bot.running
    assert not await duties._reconcile_room_state(ROOM_ID, needed={"accounts"})
    assert not bound_bot.running


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
        await duties.member_event(client.rooms[ROOM_ID], _membership_event(agent2, membership, previous))
        assert events[EVENT_TYPE, ""] == {"mode": mode}
        assert _accounts(room_state) == sorted(accounts)


@pytest.mark.asyncio
async def test_setup_publishes_after_agents_join(room_state: RoomState, monkeypatch: pytest.MonkeyPatch) -> None:
    """Initial room setup publishes after every bot has joined."""
    config, _paths, ids, events, client = room_state
    config.matrix_space.enabled = False
    setup = _room_setup(room_state)
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
            put_state=client.room_put_state,
        )
        assert events[EVENT_TYPE, ""] == {"mode": expected}


@pytest.mark.asyncio
async def test_the_mode_is_published_from_the_rooms_setting_not_the_runtimes_copy(room_state: RoomState) -> None:
    """A late copy of an older change, which the runtime took from sync, publishes nothing the room no longer holds."""
    config, paths, _, events, client = room_state
    events[SETTING] = {"value": "thread"}
    observe_room_settings_event(
        paths,
        ROOM_ID,
        {"type": SETTING[0], "state_key": SETTING[1], "content": {"value": "room"}},
    )

    assert await reconcile_room_thread_mode(client, config, paths, ROOM_ID) is True

    assert events[EVENT_TYPE, ""] == {"mode": "thread"}
    assert resolve_room_setting(THREAD_MODE, paths, ROOM_ID) == "thread"


@pytest.mark.asyncio
async def test_a_setting_changed_while_its_mode_is_published_is_published_next(room_state: RoomState) -> None:
    """A change that arrives while the mode is written is read and published before the reconciliation ends."""
    config, paths, _, events, client = room_state
    events[SETTING] = {"value": "room"}
    write = client.room_put_state.side_effect

    async def changed_meanwhile(room_id: str, event_type: str, content: dict[str, Any], state_key: str = "") -> object:
        if content == {"mode": "room"}:
            events[SETTING] = {"value": "thread"}
            observe_room_settings_event(paths, ROOM_ID, {"type": SETTING[0], "state_key": SETTING[1], "content": {}})
        return await write(room_id, event_type, content, state_key)

    client.room_put_state.side_effect = changed_meanwhile

    assert await reconcile_room_thread_mode(client, config, paths, ROOM_ID) is True

    assert [call.args[2] for call in _writes(client, EVENT_TYPE)] == [{"mode": "room"}, {"mode": "thread"}]
    assert events[EVENT_TYPE, ""] == {"mode": "thread"}


@pytest.mark.asyncio
async def test_an_unreadable_setting_publishes_nothing_and_is_retried(room_state: RoomState) -> None:
    """While the room's setting cannot be read, no mode is published from what the runtime last knew."""
    config, paths, _, _, client = room_state
    read = client.room_get_state_event.side_effect

    async def setting_unreadable(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == SETTING[0]:
            return nio.RoomGetStateEventError("unavailable", "M_UNKNOWN")
        return await read(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = setting_unreadable

    assert await reconcile_room_thread_mode(client, config, paths, ROOM_ID) is False
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_config_reload_publishes_without_room_setup(room_state: RoomState) -> None:
    """Mode edits apply live, so a reload with no restarts still updates client state."""
    config, paths, _, events, client = room_state
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=config)
    install_bots(orchestrator, {ROUTER_AGENT_NAME: _owner_bot(room_state)})
    config.agents["agent2"].thread_mode = "room"
    await orchestrator.config_reload.after_reload()
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
    setup = _room_setup(room_state)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(rooms_module, "reconcile_managed_rooms", AsyncMock(return_value={ROOM_ID: snapshot}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())
    await setup.setup([])
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
    """Every reload compares one event; unchanged modes do not write."""
    config, paths, _, events, client = room_state
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_get_state.reset_mock()
    client.room_get_state_event.reset_mock()
    client.room_put_state.reset_mock()
    setup = _room_setup(room_state)
    config.agents["agent1"].role = "Updated role"
    await setup.publish_room_state()
    client.room_get_state.assert_not_awaited()
    mode_reads = [read for read in client.room_get_state_event.await_args_list if read.args[1] == EVENT_TYPE]
    assert len(mode_reads) == 1
    assert mode_reads[0].args == (ROOM_ID, EVENT_TYPE)
    assert mode_reads[0].kwargs == {"state_key": ""}
    assert not _writes(client, EVENT_TYPE)
    config.agents["agent2"].thread_mode = "room"
    await setup.publish_room_state()
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
    setup = _room_setup(room_state, publication_allowed=lambda: False)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(rooms_module, "reconcile_managed_rooms", AsyncMock(return_value={ROOM_ID: snapshot}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())
    await setup.setup([])
    client.room_get_state.assert_not_awaited()
    client.room_put_state.assert_not_awaited()
    setup.publication_allowed = lambda: True
    await setup.publish_room_state()
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
    await _router_duties(room_state).member_event(nio.MatrixRoom(ROOM_ID, client.user_id), event)
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
    setup = _room_setup(room_state)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(rooms_module, "reconcile_managed_rooms", AsyncMock(return_value={ROOM_ID: snapshot}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())
    await setup.setup([])
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
            put_state=client.room_put_state,
        )
        assert events[EVENT_TYPE, ""] == {"mode": expected}
        events[EVENT_TYPE, ""] = {"mode": "thread"}
    assert len(_writes(client, EVENT_TYPE)) == 2


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
    await _router_duties(room_state).member_event(
        client.rooms[ROOM_ID],
        _membership_event(ids["agent2"].full_id, "leave", "join"),
    )
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    client.room_get_state.assert_not_awaited()


def _owner_bot(state: RoomState) -> MagicMock:
    """The owner account's bot with its real duties, the steward of every room it holds."""
    duties = _router_duties(state)
    bot = MagicMock(client=state[4], agent_name=ROUTER_AGENT_NAME, steward_duties=duties)
    runtime = duties.deps.runtime.orchestrator
    runtime.agent_bots = {ROUTER_AGENT_NAME: bot}
    runtime.room_steward.return_value = bot
    return bot


def _room_setup(state: RoomState, **options: Any) -> RoomSetup:  # noqa: ANN401
    """A room setup whose sweep goes through the owner account's duties."""
    config, paths, _, _, client = state
    runtime = _owner_bot(state).steward_duties.deps.runtime.orchestrator
    reconcile = partial(reconcile_held_room_states, runtime)
    return RoomSetup(paths, lambda: config, lambda: client, AsyncMock(), reconcile, **options)


def _router_duties(state: RoomState) -> StewardDuties:
    """Build the owner account's duties, the steward of every room."""
    config, paths, _, _, client = state
    deps = StewardDutiesDeps(
        runtime=MagicMock(config=config, runtime_paths=paths, client=client, phase=RunPhase.ACCEPTING),
        logger=MagicMock(),
        hook_context=MagicMock(agent_name=ROUTER_AGENT_NAME),
        journal=MagicMock(),
        room_lifecycle=MagicMock(send_welcome_message_if_empty=AsyncMock(), refresh_welcome=AsyncMock()),
        personal_rooms=MagicMock(),
    )
    deps.hook_context.registry.has_hooks.return_value = False
    deps.personal_rooms.onboards_from.return_value = False
    deps.runtime.orchestrator.room_steward.return_value = MagicMock(agent_name=ROUTER_AGENT_NAME)
    return StewardDuties(deps)


@pytest.mark.asyncio
@pytest.mark.parametrize("steward", [ROUTER_AGENT_NAME, "agent1"])
async def test_only_the_steward_republishes_a_changed_thread_mode_setting(room_state: RoomState, steward: str) -> None:
    """Every bot sees a `thread_mode` setting change in sync; the room's steward alone publishes the mode it gives."""
    _, _, _, events, client = room_state
    events[SETTING] = {"value": "room"}
    duties = _router_duties(room_state)
    duties.deps.runtime.orchestrator.room_steward.return_value = MagicMock(agent_name=steward)

    await duties.thread_mode_setting_changed(ROOM_ID)

    if steward == ROUTER_AGENT_NAME:
        assert events[EVENT_TYPE, ""] == {"mode": "room"}
    else:
        client.room_put_state.assert_not_awaited()


def _publish_joined_rooms(duties: StewardDuties) -> asyncio.Task[None]:
    """Publish the joined rooms in the background, as a completed sync frame does, and return that publish."""
    duties.publish_joined_room_state()
    (task,) = duties._room_state_publishes
    return task


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
    await _room_setup(room_state).publish_room_state()
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
    await _router_duties(room_state).member_event(
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
    setup = _room_setup(room_state)
    write = client.room_put_state.side_effect
    client.room_put_state.side_effect = TimeoutError()
    await setup.publish_room_state()
    client.room_put_state.side_effect = write
    config.agents["agent1"].role = "Updated role"
    await setup.publish_room_state()
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["startup", "router_join"])
async def test_retained_ad_hoc_room_is_reconciled(room_state: RoomState, trigger: str) -> None:
    """Retained invited rooms receive state on startup and on router rejoin."""
    config, _paths, _, events, _client = room_state
    for agent in config.agents.values():
        agent.rooms = []
        agent.thread_mode = "room"
    events[EVENT_TYPE, ""] = {"mode": "mixed"}
    if trigger == "startup":
        await _room_setup(room_state).publish_room_state()
    else:
        duties = _router_duties(room_state)
        duties.mark_joined_for_room_state(ROOM_ID)
        await _publish_joined_rooms(duties)
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
    gate_open = False
    setup = _room_setup(room_state, publication_allowed=lambda: gate_open)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(rooms_module, "reconcile_managed_rooms", AsyncMock(return_value={ROOM_ID: snapshot}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())

    async def join(name: str) -> None:
        events["m.room.member", ids[name].full_id] = {"membership": "join"}
        _sync_live_room(room_state)
        await _router_duties(room_state).member_event(
            client.rooms[ROOM_ID],
            _membership_event(ids[name].full_id, "join", "leave"),
        )

    async def joins() -> None:
        nonlocal gate_open
        await join("agent1")
        gate_open = True
        await setup.publish_room_state()
        await join("agent2")

    bot = AsyncMock(agent_name="agent1", client=client, agent_user=MagicMock(user_id=ids["agent1"].full_id))
    bot.ensure_rooms.side_effect = joins
    await setup.setup([bot])
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
        put_state=client.room_put_state,
    )
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [nio.RoomGetStateEventError("Denied", "M_FORBIDDEN"), TimeoutError()])
async def test_event_read_failure_is_nonfatal_and_retried(room_state: RoomState, failure: object) -> None:
    """A failed comparison cannot write unproven state or disable the next trigger."""
    _config, _paths, _, events, client = room_state
    read = client.room_get_state_event.side_effect
    client.room_get_state_event.side_effect = failure if isinstance(failure, Exception) else None
    client.room_get_state_event.return_value = failure
    await _room_setup(room_state).publish_room_state()
    assert not _writes(client, EVENT_TYPE)
    client.room_get_state_event.side_effect = read
    await _room_setup(room_state).publish_room_state()
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
async def test_same_mode_membership_change_retries_failed_write(room_state: RoomState) -> None:
    """A responder join retries a failed write even when configured modes are unchanged."""
    config, paths, ids, events, client = room_state
    write = client.room_put_state.side_effect
    client.room_put_state.side_effect = TimeoutError()
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.side_effect = write
    await _router_duties(room_state).member_event(
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
    await _router_duties(room_state).member_event(
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
    _config, _paths, ids, events, client = room_state
    for name in ("agent1", "agent2"):
        events["m.room.member", ids[name].full_id] = {"membership": "leave"}
    if override:
        events[SETTING] = {"value": "room"}
    _sync_live_room(room_state)
    with capture_logs() as logs:
        await _room_setup(room_state).publish_room_state()
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
    assert (EVENT_TYPE, "") not in events
    _sync_live_room(room_state)
    await bot._on_ingestion_frame_completion(True)
    assert await wait_for_background_tasks(timeout=5, owner=bot._runtime_view)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    assert _accounts(room_state) == sorted([router_id, ids["agent1"].full_id])
    # The frame publishes a join once; later frames reconcile nothing until another join.
    client.room_get_state_event.reset_mock()
    await bot._on_ingestion_frame_completion(True)
    client.room_get_state_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["startup", "router_join"])
async def test_router_lists_joined_mindroom_accounts_by_any_user_name(room_state: RoomState, trigger: str) -> None:
    """The router lists its joined router, agent, and team accounts, custom user names included, and nobody else."""
    config, paths, ids, events, _client = room_state
    config.agents["mind"] = AgentConfig(display_name="Alice's mind")
    config.teams["team1"] = TeamConfig(display_name="Team 1", role="Test team", agents=["agent1"])
    ids = entity_ids(config, paths, usernames={"mind": "alice.mind"})
    for user_id in (ids["mind"].full_id, ids["team1"].full_id, "@alice:localhost"):
        events["m.room.member", user_id] = {"membership": "join"}
    events["m.room.member", ids["agent2"].full_id] = {"membership": "invite"}
    _sync_live_room(room_state)
    if trigger == "startup":
        await _room_setup(room_state).publish_room_state()
    else:
        duties = _router_duties(room_state)
        duties.mark_joined_for_room_state(ROOM_ID)
        await _publish_joined_rooms(duties)
    assert _accounts(room_state) == sorted(
        ["@alice.mind:localhost", *(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "team1"))],
    )


@pytest.mark.asyncio
async def test_config_reload_drops_an_entity_the_config_no_longer_has(room_state: RoomState) -> None:
    """A reload republishes the list, so a removed agent that is still joined is no longer listed."""
    config, paths, ids, _, _client = room_state
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=config)
    install_bots(orchestrator, {ROUTER_AGENT_NAME: _owner_bot(room_state)})
    await orchestrator.config_reload.after_reload()
    assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "agent2"))
    del config.agents["agent2"]
    await orchestrator.config_reload.after_reload()
    assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1"))


@pytest.mark.asyncio
async def test_unchanged_accounts_are_written_once(room_state: RoomState) -> None:
    """Later triggers with unchanged membership do not rewrite the list."""
    _config, _paths, ids, _, client = room_state
    setup = _room_setup(room_state)
    for _ in range(3):
        await setup.publish_room_state()
    assert [write.kwargs for write in _writes(client, ACCOUNTS_EVENT_TYPE)] == [
        {"state_key": ids[ROUTER_AGENT_NAME].full_id},
    ]


@pytest.mark.asyncio
async def test_slower_earlier_trigger_cannot_restore_a_departed_account(room_state: RoomState) -> None:
    """Triggers in one room run one at a time, so the list ends with the latest membership."""
    config, paths, ids, events, client = room_state
    agent2 = ids["agent2"].full_id
    read_members = client.joined_members.side_effect

    async def depart_after_first_read(room_id: str) -> nio.JoinedMembersResponse:
        client.joined_members.side_effect = read_members
        response = await read_members(room_id)
        events["m.room.member", agent2] = {"membership": "leave"}
        for _ in range(10):
            await asyncio.sleep(0)
        return response

    client.joined_members.side_effect = depart_after_first_read
    await asyncio.gather(*(reconcile_room_accounts(client, config, paths, ROOM_ID) for _ in range(2)))
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
    assert _accounts(room_state) is None
    mock.side_effect = original
    await duties.member_event(client.rooms[ROOM_ID], _membership_event(agent2, "join", "leave"))
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
    read = client.room_get_state_event.side_effect

    async def read_state(room_id: str, event_type: str, state_key: str = "") -> object:
        if room_id == ROOM_ID and event_type == ACCOUNTS_EVENT_TYPE:
            blocked.set()
            await release.wait()
        if room_id == other and event_type == ROUTER_EVENT_TYPE:
            return nio.RoomGetStateEventError("Absent", "M_NOT_FOUND", room_id=room_id)
        return await read(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = read_state
    duties = _router_duties(room_state)
    duties.mark_joined_for_room_state(ROOM_ID)
    frame = _publish_joined_rooms(duties)
    try:
        await asyncio.wait_for(blocked.wait(), timeout=1)
        user_id = ids["agent1"].full_id
        await asyncio.wait_for(
            duties.member_event(client.rooms[other], _membership_event(user_id, "join", "leave")),
            timeout=1,
        )
        assert call(other, EVENT_TYPE, {"mode": "room"}, state_key="") in _writes(client, EVENT_TYPE)
        assert call(other, ROUTER_EVENT_TYPE, {"user_id": client.user_id}, state_key="") in _writes(
            client,
            ROUTER_EVENT_TYPE,
        )
    finally:
        release.set()
        await frame


async def _yield_loop(turns: int) -> None:
    """Let ready tasks run without advancing the fake clock or waiting indefinitely."""
    for _ in range(turns):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", [False, True])
async def test_waiting_room_state_work_releases_the_caller_at_ten_seconds(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    sweep: bool,
) -> None:
    """The journal lane and the setup sweep leave unfinished rooms to background retries."""
    _, _, ids, _, client = room_state
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    blocked = asyncio.Event()

    async def stalled_roster(_room_id: str) -> object:
        blocked.set()
        await asyncio.Future()

    client.joined_members.side_effect = stalled_roster
    bot = _owner_bot(room_state)
    duties = bot.steward_duties
    duties.mark_joined_for_room_state(ROOM_ID)
    frame = _publish_joined_rooms(duties)
    caller = None
    try:
        await _yield_loop(40)
        assert blocked.is_set(), "The frame did not start its roster lookup"
        if sweep:
            other = "!other:localhost"
            client.rooms[other] = nio.MatrixRoom(other, client.user_id)
            work = reconcile_held_room_states(duties.deps.runtime.orchestrator)
        else:
            work = duties.member_event(client.rooms[ROOM_ID], _membership_event(ids["agent1"].full_id, "leave", "join"))
        caller = asyncio.create_task(work)
        for tick in (0, 9, 10):
            clock = tick
            await _yield_loop(40)
            if tick < 10:
                assert not caller.done()
        assert caller.done()
        await caller
        assert duties._room_state_retries.keys() == client.rooms.keys()
    finally:
        duties.stopping()
        if caller is not None:
            caller.cancel()
        await asyncio.gather(*(task for task in (caller, frame) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_triggers", [1, 2])
async def test_joined_room_publish_waits_at_most_ten_seconds_behind_slow_triggers_in_its_room(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    earlier_triggers: int,
) -> None:
    """The reconcile timeout covers the wait for the room's lock, so a joined room's publish ends within 10 seconds."""
    _config, _paths, _, _, client = room_state

    async def unanswered(*_args: object, **_kwargs: object) -> None:
        await asyncio.Future()

    client.room_get_state_event.side_effect = unanswered
    client.joined_members.side_effect = unanswered
    loop = asyncio.get_running_loop()
    now = 0
    monkeypatch.setattr(loop, "time", lambda: now)
    setup = _room_setup(room_state)
    triggers = [asyncio.create_task(setup.publish_room_state()) for _ in range(earlier_triggers)]
    duties = _router_duties(room_state)
    duties.mark_joined_for_room_state(ROOM_ID)
    frame = _publish_joined_rooms(duties)
    while True:
        # Drain ready callbacks and timeout cancellation without wall-clock sleeps.
        for _ in range(20):
            await asyncio.sleep(0)
        if frame.done() or now == 60:
            break
        now += 1
    assert frame.done()
    assert now == 10
    assert all(trigger.done() for trigger in triggers)


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
    duties = _router_duties(room_state)
    duties.mark_joined_for_room_state(ROOM_ID)
    frame = _publish_joined_rooms(duties)
    for _ in range(30):
        await asyncio.sleep(0)
    assert not frame.done()
    command = asyncio.create_task(
        handle_thread_mode_command(
            "thread",
            client=client,
            config=config,
            runtime_paths=paths,
            room_id=ROOM_ID,
            requester_user_id="@admin:localhost",
            sender_user_id="@admin:localhost",
            put_state=client.room_put_state,
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
        duties.stopping()
        command.cancel()
        await asyncio.gather(frame, command, return_exceptions=True)


@pytest.mark.asyncio
async def test_steward_roster_spends_the_same_budget_on_lock_wait_and_io(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acquiring the lock just before the deadline must not extend an inline membership callback."""
    config, paths, ids, _, client = room_state
    duties = _owner_bot(room_state).steward_duties
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    started, release, stalled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    read_members = client.joined_members.side_effect

    async def members(room_id: str) -> nio.JoinedMembersResponse:
        if started.is_set():
            stalled.set()
            return await asyncio.Future()
        started.set()
        await release.wait()
        return await read_members(room_id)

    client.joined_members.side_effect = members
    first = asyncio.create_task(reconcile_room_accounts(client, config, paths, ROOM_ID))
    event = _membership_event(ids["agent1"].full_id, "join", "leave")
    callback = asyncio.create_task(duties.member_event(client.rooms[ROOM_ID], event))
    try:
        await _yield_loop(40)
        assert started.is_set()
        assert not stalled.is_set()
        assert not callback.done()
        clock = 9
        release.set()
        await _yield_loop(40)
        assert first.done()
        assert await first is True
        assert stalled.is_set()
        assert not callback.done()
        clock = 10
        await _yield_loop(40)
        assert callback.done(), "The lock wait and the first writer must share one deadline"
        await callback
        assert duties._room_state_reruns[ROOM_ID] == {"accounts"}
    finally:
        duties.stopping()
        first.cancel()
        callback.cancel()
        await asyncio.gather(first, callback, return_exceptions=True)


@pytest.mark.asyncio
async def test_setup_sweep_logs_a_real_roster_deadline(room_state: RoomState, monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-flight request takes the retry path and logs its own deadline, not silent caller cancellation."""
    _, _, _, _, client = room_state
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)

    async def unanswered(_room_id: str) -> object:
        await asyncio.Future()

    client.joined_members.side_effect = unanswered
    duties = _owner_bot(room_state).steward_duties
    with capture_logs() as logs:
        sweep = asyncio.create_task(reconcile_held_room_states(duties.deps.runtime.orchestrator))
        try:
            for tick in (0, 9, 10):
                clock = tick
                for _ in range(30):
                    await asyncio.sleep(0)
            assert sweep.done()
            await sweep
            assert ROOM_ID in duties._room_state_retries
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
            duties.stopping()
            sweep.cancel()
            await asyncio.gather(sweep, return_exceptions=True)


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
@pytest.mark.parametrize("second_trigger", [None, "leave", "frame"])
async def test_queued_reconciles_publish_latest_membership(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    second_trigger: str | None,
) -> None:
    """A reload holding the room's lock does not prevent queued changes from publishing the latest accounts."""
    _config, _paths, ids, events, client = room_state
    loop = asyncio.get_running_loop()
    now = 0
    monkeypatch.setattr(loop, "time", lambda: now)

    def slow(answer: Callable[..., Awaitable[object]], *, only: str | None = None) -> Callable[..., Awaitable[object]]:
        async def answer_after_three_seconds(*args: object, **kwargs: object) -> object:
            if only is None or only in args:
                await asyncio.sleep(3)
            return await answer(*args, **kwargs)

        return answer_after_three_seconds

    client.joined_members.side_effect = slow(client.joined_members.side_effect)
    for mock in (client.room_get_state_event, client.room_put_state):
        mock.side_effect = slow(mock.side_effect, only=ACCOUNTS_EVENT_TYPE)
    duties = _router_duties(room_state)
    departures = {4: "agent2", 6: "agent1"} if second_trigger == "leave" else {4: "agent2"}
    tasks = [asyncio.create_task(_room_setup(room_state).publish_room_state())]
    for now in range(60):
        if now in departures:
            user_id = ids[departures[now]].full_id
            events["m.room.member", user_id] = {"membership": "leave"}
            tasks.append(
                asyncio.create_task(
                    duties.member_event(client.rooms[ROOM_ID], _membership_event(user_id, "leave", "join")),
                ),
            )
        if now == 6 and second_trigger == "frame":
            duties.mark_joined_for_room_state(ROOM_ID)
            tasks.append(_publish_joined_rooms(duties))
        # Drain ready callbacks and timeout cancellation without wall-clock sleeps.
        for _ in range(50):
            await asyncio.sleep(0)
    assert all(task.done() for task in tasks)
    remaining = {ROUTER_AGENT_NAME, "agent1", "agent2"} - set(departures.values())
    assert _accounts(room_state) == sorted(ids[name].full_id for name in remaining)


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
    monkeypatch.setattr("mindroom.steward_duties.uniform", lambda low, high: high if next(take_high, True) else low)
    clock = 0.0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    crypto = asyncio.Lock()
    read = client.joined_members.side_effect

    async def roster(room_id: str) -> nio.JoinedMembersResponse:
        async with crypto:
            await asyncio.sleep(11 if room_id == ROOM_ID else 1)
            return await read(room_id)

    client.joined_members.side_effect = roster
    duties = _router_duties(room_state)
    tasks = []
    try:
        for quarter_second in range(121):
            clock = quarter_second / 4
            if quarter_second < 2:
                duties.mark_joined_for_room_state(ROOM_ID if quarter_second == 0 else healthy)
                duties.publish_joined_room_state()
                tasks.extend(duties._room_state_publishes - set(tasks))
            for _ in range(20):
                await asyncio.sleep(0)
            if _writes(client, ACCOUNTS_EVENT_TYPE):
                break
        assert [write.args[0] for write in _writes(client, ACCOUNTS_EVENT_TYPE)] == [healthy]
        assert ROOM_ID in duties._room_state_retries  # A has not recovered.
        assert clock < 30
    finally:
        duties.stopping()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_ad_hoc_room_publishes_mixed_mode_on_its_first_retry(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give mode its own roster turn before accounts consumes the client's crypto lock again."""
    config, _, _, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    events[EVENT_TYPE, ""] = {"mode": "room"}
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    crypto = asyncio.Lock()
    roster = client.joined_members.side_effect
    read = client.room_get_state_event.side_effect
    write = client.room_put_state.side_effect
    steps: list[tuple[int, str]] = []

    async def slow_roster(room_id: str) -> nio.JoinedMembersResponse:
        steps.append((clock, "roster"))
        async with crypto:
            await asyncio.sleep(6)
            return await roster(room_id)

    async def read_state(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == ACCOUNTS_EVENT_TYPE:
            await asyncio.sleep(5)
        return await read(room_id, event_type, state_key)

    async def write_state(room_id: str, event_type: str, content: dict, **kwargs: object) -> object:
        if event_type == EVENT_TYPE:
            steps.append((clock, "mode"))
        return await write(room_id, event_type, content, **kwargs)

    client.joined_members.side_effect = slow_roster
    client.room_get_state_event.side_effect = read_state
    client.room_put_state.side_effect = write_state
    duties = _router_duties(room_state)
    duties.mark_joined_for_room_state(ROOM_ID)
    frame = _publish_joined_rooms(duties)
    try:
        for tick in range(18):
            clock = tick
            for _ in range(30):
                await asyncio.sleep(0)
            if clock == 10:
                assert frame.done()  # The foreground has not inherited the retry's sequential budget.
        assert events[EVENT_TYPE, ""] == {"mode": "mixed"}
        assert steps == [(0, "roster"), (0, "roster"), (11, "roster"), (17, "mode"), (17, "roster")]
    finally:
        duties.stopping()
        await asyncio.gather(frame, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reload", "handover", "left", "shutdown-asleep", "shutdown-in-flight"])
async def test_retry_rechecks_room_inputs_after_a_slow_mode_turn(  # noqa: PLR0915 - drive each phase of the same interleaving
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """An accounts retry must not use the config or eligibility from before the mode turn."""
    config, _, ids, _, client = room_state
    removed_member = ids["agent2"].full_id  # Still joined, for example for approval recovery.
    duties = _owner_bot(room_state).steward_duties
    runtime = duties.deps.runtime
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    mode_paused = asyncio.Event()
    release = asyncio.Event()
    first = {EVENT_TYPE, ACCOUNTS_EVENT_TYPE}
    write = client.room_put_state.side_effect

    async def write_state(room_id: str, event_type: str, content: dict, **kwargs: object) -> object:
        if event_type in first:
            first.remove(event_type)
            return nio.RoomPutStateError("Temporarily unavailable", "M_UNKNOWN")
        if event_type == EVENT_TYPE and not mode_paused.is_set():
            mode_paused.set()
            await release.wait()
        return await write(room_id, event_type, content, **kwargs)

    client.room_put_state.side_effect = write_state
    await duties._publish_room_state(ROOM_ID)
    retry = duties._room_state_retries[ROOM_ID]
    sweep = None
    try:
        await asyncio.sleep(0)  # Let the retry start sleeping at t=0.
        if change == "shutdown-asleep":
            runtime.phase = RunPhase.SHUTTING_DOWN
        clock = 1
        await _yield_loop(40)
        assert mode_paused.is_set() == (change != "shutdown-asleep")
        if change == "reload":
            runtime.config = config.model_copy(deep=True)
            del runtime.config.agents["agent2"]
            sweep = asyncio.create_task(reconcile_held_room_states(runtime.orchestrator))
            await _yield_loop(50)
            assert _accounts(room_state) is not None
            assert removed_member not in (_accounts(room_state) or ())
        elif change == "handover":
            runtime.orchestrator.room_steward.return_value = MagicMock(agent_name="agent1")
        elif change == "left":
            del client.rooms[ROOM_ID]
        elif change == "shutdown-in-flight":
            runtime.phase = RunPhase.SHUTTING_DOWN
        release.set()
        await retry
        if sweep is not None:
            await sweep
            assert removed_member not in (_accounts(room_state) or ())
        else:
            assert client.joined_members.await_count == len(_writes(client, ACCOUNTS_EVENT_TYPE)) == 1
        assert not duties._room_state_retries
    finally:
        release.set()
        duties.stopping()
        await asyncio.gather(retry, *([sweep] if sweep is not None else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_stewardship", [False, True])
async def test_sweep_retries_failed_room_with_backoff(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    lose_stewardship: bool,
) -> None:
    """Two timeouts do not strand a room; losing stewardship aborts the next attempt."""
    _, _, _, _, client = room_state
    bot = _owner_bot(room_state)
    duties = bot.steward_duties
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
        await reconcile_held_room_states(duties.deps.runtime.orchestrator)
        for now in range(6):
            if now == 2 and lose_stewardship:
                duties.deps.runtime.orchestrator.room_steward.return_value = None
            for _ in range(30):
                await asyncio.sleep(0)
    assert times == ([0, 1] if lose_stewardship else [0, 1, 3])
    assert not duties._room_state_retries
    failures = [log for log in logs if log["event"] == "room_accounts_publish_failed"]
    assert [log["log_level"] for log in failures] == ["info", "info"]
    assert all(log["error"] == "TimeoutError" and "exc_info" not in log for log in failures)
    assert (_accounts(room_state) is None) == lose_stewardship


@pytest.mark.asyncio
async def test_raced_membership_trigger_is_retried_after_an_older_binding_retry(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful binding retry does not cover a newer failed accounts write."""
    _, _, ids, events, client = room_state
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    agent = ids["agent2"].full_id
    binding_paused = asyncio.Event()
    release = asyncio.Event()
    read = client.room_get_state_event.side_effect
    write = client.room_put_state.side_effect

    async def read_state(room_id: str, kind: str, state_key: str = "") -> object:
        if kind == ROUTER_EVENT_TYPE:
            if clock == 0:
                raise TimeoutError
            if not binding_paused.is_set():
                binding_paused.set()
                await release.wait()
        return await read(room_id, kind, state_key)

    async def write_state(room_id: str, kind: str, content: dict, **kwargs: object) -> object:
        if kind == ACCOUNTS_EVENT_TYPE and clock == 1:
            raise TimeoutError
        return await write(room_id, kind, content, **kwargs)

    client.room_get_state_event.side_effect = read_state
    client.room_put_state.side_effect = write_state
    duties = _owner_bot(room_state).steward_duties
    try:
        await reconcile_held_room_states(duties.deps.runtime.orchestrator)
        for tick in range(6):
            clock = tick
            for _ in range(30):
                await asyncio.sleep(0)
            if clock == 1:
                assert binding_paused.is_set()
                events["m.room.member", agent] = {"membership": "leave"}
                await duties.member_event(client.rooms[ROOM_ID], _membership_event(agent, "leave", "join"))
                assert agent in (_accounts(room_state) or ())  # The new write failed once.
                release.set()
        assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1"))
        assert not duties._room_state_retries
    finally:
        release.set()
        duties.stopping()


@pytest.mark.asyncio
async def test_trigger_failing_during_slow_roster_retry_is_not_lost(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An eight-second roster read snapshots before a leave and contends with the newer trigger."""
    config, _, ids, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    read = client.joined_members.side_effect
    crypto_lock = asyncio.Lock()

    async def slow_roster(room_id: str) -> object:
        if clock == 0:
            return nio.JoinedMembersError("First roster unavailable")
        async with crypto_lock:
            snapshot = await read(room_id)
            await asyncio.sleep(8)
            return snapshot

    client.joined_members.side_effect = slow_roster
    duties = _owner_bot(room_state).steward_duties
    agent = ids["agent2"].full_id
    try:
        await reconcile_held_room_states(duties.deps.runtime.orchestrator)
        for tick in range(121):
            clock = tick
            if tick == 2:
                events["m.room.member", agent] = {"membership": "leave"}
                _sync_live_room(room_state)
                change = asyncio.create_task(
                    duties.member_event(client.rooms[ROOM_ID], _membership_event(agent, "leave", "join")),
                )
            for _ in range(25):
                await asyncio.sleep(0)
        assert change.done()
        await change
        assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1"))
        assert not duties._room_state_retries
    finally:
        duties.stopping()


@pytest.mark.asyncio
async def test_persistent_accounts_failure_backs_off_to_sixty_seconds(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent timeouts warn once at the cap, then go quiet until a successful retry retires."""
    _, _, _, _, client = room_state
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    write = client.room_put_state.side_effect
    attempts: list[int] = []

    async def write_state(room_id: str, kind: str, content: dict, **kwargs: object) -> object:
        if kind == ACCOUNTS_EVENT_TYPE:
            attempts.append(clock)
            if len(attempts) < 10:
                raise TimeoutError
        return await write(room_id, kind, content, **kwargs)

    client.room_put_state.side_effect = write_state
    duties = _owner_bot(room_state).steward_duties
    try:
        with capture_logs() as logs:
            await reconcile_held_room_states(duties.deps.runtime.orchestrator)
            for tick in range(244):
                clock = tick
                for _ in range(20):
                    await asyncio.sleep(0)
        assert attempts == [0, 1, 3, 7, 15, 31, 63, 123, 183, 243]
        assert [log["log_level"] for log in logs if log["event"] == "room_accounts_publish_failed"] == ["info"] * 7 + [
            "warning",
            "debug",
        ]
        assert not duties._room_state_retries
    finally:
        duties.stopping()


@pytest.mark.asyncio
async def test_retry_does_not_repeat_a_siblings_forbidden_put(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient accounts failure retries only accounts, not a refused mode or binding PUT."""
    _, _, _, _, client = room_state
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    write = client.room_put_state.side_effect

    async def write_state(room_id: str, kind: str, content: dict, **kwargs: object) -> object:
        if kind in (ROUTER_EVENT_TYPE, EVENT_TYPE):
            return nio.RoomPutStateError("Forbidden", "M_FORBIDDEN")
        if clock == 0:
            raise TimeoutError
        return await write(room_id, kind, content, **kwargs)

    client.room_put_state.side_effect = write_state
    duties = _owner_bot(room_state).steward_duties
    try:
        await reconcile_held_room_states(duties.deps.runtime.orchestrator)
        for tick in (0, 1, 2):
            clock = tick
            for _ in range(30):
                await asyncio.sleep(0)
        assert len(_writes(client, ACCOUNTS_EVENT_TYPE)) == 2
        assert len(_writes(client, EVENT_TYPE)) == len(_writes(client, ROUTER_EVENT_TYPE)) == 1
        assert not duties._room_state_retries
    finally:
        duties.stopping()


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_room_state_failure_warns_again_after_recovery(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    background: bool,
) -> None:
    """Recovery by a fresh trigger or by the background retry clears a room's warning latch."""
    _, _, _, events, client = room_state
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    duties = _router_duties(room_state)
    write = client.room_put_state.side_effect
    failing = True

    async def put_state(room_id: str, event_type: str, content: dict, **kwargs: object) -> object:
        if event_type == ACCOUNTS_EVENT_TYPE and failing:
            return nio.RoomPutStateError("unavailable", "M_UNKNOWN")
        return await write(room_id, event_type, content, **kwargs)

    client.room_put_state.side_effect = put_state
    try:
        with capture_logs() as logs:
            for tick in (0, 1, 3, 4):
                if background:
                    clock = tick
                failing = tick != 3
                if not background or tick in (0, 4):
                    events.pop((ACCOUNTS_EVENT_TYPE, client.user_id), None)
                    duties.mark_joined_for_room_state(ROOM_ID)
                    await _publish_joined_rooms(duties)
                for _ in range(30):
                    await asyncio.sleep(0)
                if tick == 3:
                    assert _accounts(room_state) is not None
        assert [log["log_level"] for log in logs if log["event"] == "room_accounts_publish_failed"] == [
            "warning",
            "debug",
            "warning",
        ]
    finally:
        duties.stopping()


@pytest.mark.asyncio
async def test_delegated_roster_failures_share_a_room_warning_latch(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accounts and ad-hoc mode roster errors share one warning, including during retries."""
    config, _, _, _, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
    clock = 0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    client.joined_members.side_effect = None
    client.joined_members.return_value = nio.JoinedMembersError("unavailable")
    duties = _router_duties(room_state)
    try:
        with capture_logs() as logs:
            duties.mark_joined_for_room_state(ROOM_ID)
            await _publish_joined_rooms(duties)
            await asyncio.sleep(0)
            clock = 1
            for _ in range(30):
                await asyncio.sleep(0)
        diagnostics = {"matrix_room_members_fetch_failed", "authoritative_room_membership_fetch_failed"}
        failures = [log for log in logs if log["event"] in diagnostics]
        assert [log["event"] for log in failures].count("matrix_room_members_fetch_failed") == 2
        assert [log["event"] for log in failures].count("authoritative_room_membership_fetch_failed") == 2
        assert sorted(log["log_level"] for log in failures) == ["debug", "debug", "debug", "warning"]
    finally:
        duties.stopping()


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", [False, True])
async def test_later_terminal_accounts_result_retires_older_pending_retry(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    forbidden: bool,
) -> None:
    """After a sweep succeeds or gets a power refusal, a new outage gets its own backoff."""
    _, _, _, events, client = room_state
    clock = 0.0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    write = client.room_put_state.side_effect
    attempts = []

    async def put_state(room_id: str, event_type: str, content: dict, **kwargs: object) -> object:
        if event_type == ACCOUNTS_EVENT_TYPE:
            attempts.append(clock)
            if clock in (0, 1, 1.5):
                raise TimeoutError
            if forbidden:
                return nio.RoomPutStateError("Forbidden", "M_FORBIDDEN")
        return await write(room_id, event_type, content, **kwargs)

    client.room_put_state.side_effect = put_state
    duties = _router_duties(room_state)
    try:
        for tick in (0, 1, 1.25, 1.5, 2.5):
            clock = tick
            if tick in (0, 1.25, 1.5):
                events.pop((ACCOUNTS_EVENT_TYPE, client.user_id), None)
                duties.mark_joined_for_room_state(ROOM_ID)
                await _publish_joined_rooms(duties)
            await _yield_loop(30)
        assert attempts == [0, 1, 1.25, 1.5, 2.5]
        assert not duties._room_state_reruns
        assert not duties._room_state_retries
    finally:
        duties.stopping()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_in_flight", [False, True])
async def test_stale_success_does_not_discard_a_newer_queued_failure(  # noqa: PLR0915 - drive each phase of the same interleaving
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    retry_in_flight: bool,
) -> None:
    """Keep a newer failure queued and its warning latched when an older trigger finishes late."""
    _, _, ids, events, client = room_state
    departing = ids["agent2"].full_id
    clock = 0.0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    release = asyncio.Event()
    retry_put = asyncio.Event()
    release_retry = asyncio.Event()
    read = client.room_get_state_event.side_effect
    write = client.room_put_state.side_effect

    async def read_state(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == ROUTER_EVENT_TYPE and clock == 0.25:
            await release.wait()
        return await read(room_id, event_type, state_key)

    async def write_state(room_id: str, event_type: str, content: dict, **kwargs: object) -> object:
        if event_type == ACCOUNTS_EVENT_TYPE:
            if clock == 0:
                raise TimeoutError
            if clock == 1 and retry_in_flight:
                retry_put.set()
                await release_retry.wait()
            if clock in (0.5, 1):
                return nio.RoomPutStateError("Unavailable", "M_UNKNOWN")
        return await write(room_id, event_type, content, **kwargs)

    client.room_get_state_event.side_effect = read_state
    client.room_put_state.side_effect = write_state
    duties = _router_duties(room_state)
    duties.mark_joined_for_room_state(ROOM_ID)
    await _publish_joined_rooms(duties)  # Seed the older pending set and its sleeping timer.
    clock = 0.25
    duties.mark_joined_for_room_state(ROOM_ID)
    old = _publish_joined_rooms(duties)
    try:
        await _yield_loop(30)
        assert not old.done()
        assert departing in (_accounts(room_state) or ())
        with capture_logs() as logs:
            events["m.room.member", departing] = {"membership": "leave"}
            clock = 0.5
            await duties.member_event(client.rooms[ROOM_ID], _membership_event(departing, "leave", "join"))
            assert departing in (_accounts(room_state) or ())  # The newer write failed.
            if retry_in_flight:
                clock = 1
                await _yield_loop(30)
                assert retry_put.is_set()
            release.set()
            await old
            clock = 1
            release_retry.set()
            await _yield_loop(30)
        assert [log["log_level"] for log in logs if log["event"] == "room_accounts_publish_failed"] == [
            "warning",
            "debug",
        ]
        clock = 3
        await _yield_loop(30)
        assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1"))
        assert not duties._room_state_retries
    finally:
        release.set()
        duties.stopping()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_slower_earlier_trigger_cannot_restore_a_replaced_thread_mode(room_state: RoomState) -> None:
    """Triggers in one room run one at a time, so the mode ends with the latest override."""
    config, paths, _, events, client = room_state
    read_event = client.room_get_state_event.side_effect

    async def override_during_first_compare(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == EVENT_TYPE:
            client.room_get_state_event.side_effect = read_event
            events[SETTING] = {"value": "room"}
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
    config, paths, _, events, client = room_state
    events[SETTING] = {"value": "thread"}
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
        assert resolve_room_setting(THREAD_MODE, paths, ROOM_ID) == "thread"


async def _hang_a_room_state_retry(duties: StewardDuties, state: RoomState) -> None:
    """Fail one trigger's reconcile, so it schedules a retry, and hang that retry on its first write."""
    _, _, ids, _, client = state
    trigger_done = False

    async def refuse_then_hang(*_args: object, **_kwargs: object) -> nio.RoomPutStateError:
        if trigger_done:
            await asyncio.Future()
        return nio.RoomPutStateError("unavailable", "M_UNKNOWN")

    client.room_put_state.side_effect = refuse_then_hang
    await duties.member_event(client.rooms[ROOM_ID], _membership_event(ids["agent2"].full_id, "join", "leave"))
    trigger_done = True


@pytest.mark.asyncio
async def test_the_router_owns_its_room_state_retry(room_state: RoomState) -> None:
    """The router's runtime owns the background retry, so its shutdown drain covers it."""
    duties = _router_duties(room_state)
    router = duties.deps.runtime
    await _hang_a_room_state_retry(duties, room_state)
    assert not await wait_for_background_tasks(timeout=0, owner=router)


@pytest.mark.asyncio
async def test_sweep_failed_while_stopping_cannot_register_a_retry(room_state: RoomState) -> None:
    """A sweep still draining when the bot stops must not add work after the cancellation pass."""
    _, _, _, _, client = room_state
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_after_stop(_room_id: str) -> nio.JoinedMembersError:
        entered.set()
        await release.wait()
        return nio.JoinedMembersError("unavailable")

    client.joined_members.side_effect = fail_after_stop
    duties = _owner_bot(room_state).steward_duties
    sweep = asyncio.create_task(reconcile_held_room_states(duties.deps.runtime.orchestrator))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        duties.deps.runtime.phase = RunPhase.DRAINING
        duties.stopping()
        release.set()
        await sweep
        assert not duties._room_state_retries
    finally:
        release.set()
        duties.stopping()
        await sweep


@pytest.mark.asyncio
async def test_router_stop_releases_a_retry_cancelled_before_it_starts(room_state: RoomState) -> None:
    """Stopping before the retry coroutine gets its first step must still remove it from the registry."""
    duties = _router_duties(room_state)
    await _hang_a_room_state_retry(duties, room_state)
    retry = duties._room_state_retries[ROOM_ID]
    duties.stopping()
    with pytest.raises(asyncio.CancelledError):
        await retry
    assert not duties._room_state_retries
    assert not duties._room_state_reruns


@pytest.mark.asyncio
async def test_router_stop_cancels_its_room_state_tasks_without_waiting(room_state: RoomState) -> None:
    """A stopping router discards its publish and retry, so they leave its stop budget to the replies it drains."""
    config, paths, ids, _, client = room_state
    user = AgentMatrixUser(ROUTER_AGENT_NAME, ids[ROUTER_AGENT_NAME].full_id, "Router", TEST_PASSWORD)
    bot = install_runtime_journal_support(make_test_agent_bot(user, config=config, runtime_paths=paths))
    bot.client = client
    duties = bot.steward_duties
    await _hang_a_room_state_retry(duties, room_state)
    duties.mark_joined_for_room_state(ROOM_ID)
    publish = _publish_joined_rooms(duties)
    budget = bot._sync_shutdown_budget = ShutdownBudget.start(5.0)
    await bot.prepare_for_sync_shutdown()
    assert budget.remaining_seconds() > 4
    assert publish.cancelled()
