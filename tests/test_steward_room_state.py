"""The steward-published room state clients use to choose a conversation view and recognize MindRoom accounts."""

from __future__ import annotations

import asyncio
import gc
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import nio
import pytest
from structlog.testing import capture_logs

from mindroom import room_modes
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot_runtime_view import RunPhase
from mindroom.commands.thread_mode_commands import handle_thread_mode_command
from mindroom.config.agent import AgentConfig, RoomConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import AdmissionFacts, RoomMembershipPosition
from mindroom.matrix.room_accounts import reconcile_room_accounts, reconcile_room_router_binding
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration import rooms as rooms_module
from mindroom.orchestration.rooms import RoomSetup
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.room_modes import change_room_thread_mode
from mindroom.room_thread_modes import (
    clear_room_thread_mode_override,
    resolve_room_thread_mode_override,
    set_room_thread_mode_override,
)
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
    from collections.abc import Awaitable, Callable
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
    monkeypatch.setattr("mindroom.steward_duties.uniform", lambda low, high: min(low * 2, high), raising=False)
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
    _config, paths, ids, events, client = room_state
    for name in ("agent1", "agent2"):
        events["m.room.member", ids[name].full_id] = {"membership": "leave"}
    if override:
        set_room_thread_mode_override(paths, room_id=ROOM_ID, mode="room")
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
    await blocked.wait()
    if sweep:
        other = "!other:localhost"
        client.rooms[other] = nio.MatrixRoom(other, client.user_id)
        work = reconcile_held_room_states(duties.deps.runtime.orchestrator)
    else:
        work = duties.member_event(client.rooms[ROOM_ID], _membership_event(ids["agent1"].full_id, "leave", "join"))
    caller = asyncio.create_task(work)
    try:
        for tick in (0, 9, 10):
            clock = tick
            for _ in range(40):
                await asyncio.sleep(0)
            if tick < 10:
                assert not caller.done()
        assert caller.done()
        await caller
        assert duties._room_state_retries.keys() == client.rooms.keys()
    finally:
        duties.stopping()
        caller.cancel()
        await asyncio.gather(caller, frame, return_exceptions=True)


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
    draws = iter((1.5, 0.5))  # A backs off longer; B backs off less.
    monkeypatch.setattr("mindroom.steward_duties.uniform", lambda _low, high: next(draws, high), raising=False)
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
    """A later success or power refusal supersedes a queued retry of that same operation."""
    _, _, _, _, client = room_state
    clock = 0.0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock)
    write = client.room_put_state.side_effect

    async def put_state(room_id: str, event_type: str, content: dict, **kwargs: object) -> object:
        if event_type == ACCOUNTS_EVENT_TYPE:
            if clock == 0:
                raise TimeoutError
            if forbidden:
                return nio.RoomPutStateError("Forbidden", "M_FORBIDDEN")
        return await write(room_id, event_type, content, **kwargs)

    client.room_put_state.side_effect = put_state
    duties = _router_duties(room_state)
    try:
        for tick in (0.0, 0.25):
            clock = tick
            duties.mark_joined_for_room_state(ROOM_ID)
            await _publish_joined_rooms(duties)
            await asyncio.sleep(0)
        clock = 2
        for _ in range(30):
            await asyncio.sleep(0)
        assert client.joined_members.await_count == len(_writes(client, ACCOUNTS_EVENT_TYPE)) == 2
        assert not duties._room_state_reruns
        assert not duties._room_state_retries
    finally:
        duties.stopping()


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
