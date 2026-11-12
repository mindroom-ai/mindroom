"""The router-owned room state clients use to choose a conversation view and recognize MindRoom accounts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import nio
import pytest
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.commands.thread_mode_commands import handle_thread_mode_command
from mindroom.config.agent import AgentConfig, RoomConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import AdmissionFacts, RoomMembershipPosition
from mindroom.matrix.room_accounts import reconcile_room_accounts
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration.rooms import RoomSetup
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.room_thread_modes import clear_room_thread_mode_override, set_room_thread_mode_override
from mindroom.router.duties import RouterDuties, RouterDutiesDeps
from mindroom.runtime_shutdown import ShutdownBudget
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
ROOM_ID = "!room:localhost"

type RoomState = tuple[Config, RuntimePaths, dict[str, MatrixID], dict[tuple[str, str], dict[str, str]], AsyncMock]


@pytest.fixture
def room_state(tmp_path: Path) -> RoomState:
    """Keep a mutable homeserver state behind the router's Matrix API."""
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
            users = {"@admin:localhost": 100, ids[ROUTER_AGENT_NAME].full_id: 100}
            return nio.RoomGetStateEventResponse({"users": users}, event_type, state_key, room_id)
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
        nio.RoomPutStateError("forbidden", "M_FORBIDDEN"),
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
async def test_router_without_the_power_skips_the_writes_until_it_gains_it(room_state: RoomState) -> None:
    """Below the level a state type needs, a start writes nothing and warns of nothing; once raised, the next one publishes."""
    config, paths, ids, events, client = room_state
    router = ids[ROUTER_AGENT_NAME].full_id
    power_levels: dict[str, Any] = {"users": {"@admin:localhost": 100}, "state_default": 50}
    events["m.room.create", ""] = {"room_version": "11"}
    read_event = client.room_get_state_event.side_effect

    async def read_power(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == "m.room.power_levels":
            return nio.RoomGetStateEventResponse(dict(power_levels), event_type, state_key, room_id)
        return await read_event(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = read_power
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock())
    with capture_logs() as logs:
        await setup.publish_room_state()
    client.room_put_state.assert_not_awaited()
    assert sorted((log["event"], log["log_level"]) for log in logs) == [
        ("room_accounts_publish_skipped", "debug"),
        ("room_thread_mode_publish_skipped", "debug"),
    ]

    power_levels["users"][router] = 50
    await setup.publish_room_state()
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}
    assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "agent2"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("power_levels", "create", "writes"),
    [
        # A power-levels event without `state_default` asks 50 for state.
        ({"users": {}}, {"room_version": "11"}, False),
        ({"users": {}, "events": {EVENT_TYPE: 0, ACCOUNTS_EVENT_TYPE: 0}}, {"room_version": "11"}, True),
        ({"users": {}, "state_default": 0}, {"room_version": "11"}, True),
        # Levels or creators the router cannot read leave the answer to the write.
        (None, {"room_version": "11"}, True),
        ({"users": {}}, None, True),
    ],
)
async def test_router_writes_every_state_its_level_or_unreadable_levels_may_allow(
    room_state: RoomState,
    power_levels: dict[str, Any] | None,
    create: dict[str, str] | None,
    *,
    writes: bool,
) -> None:
    """A type whose own level or the state default the router meets, or whose levels it cannot read, is written."""
    config, paths, _, events, client = room_state
    if create is not None:
        events["m.room.create", ""] = create
    read_event = client.room_get_state_event.side_effect

    async def read_power(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == "m.room.power_levels":
            if power_levels is None:
                return nio.RoomGetStateEventError("unavailable", "M_UNKNOWN", room_id=room_id)
            return nio.RoomGetStateEventResponse(dict(power_levels), event_type, state_key, room_id)
        return await read_event(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = read_power
    assert await reconcile_room_thread_mode(client, config, paths, ROOM_ID) is writes
    assert await reconcile_room_accounts(client, config, paths, ROOM_ID) is writes
    assert ((EVENT_TYPE, "") in events) is writes
    assert (_accounts(room_state) is not None) is writes


@pytest.mark.asyncio
async def test_only_router_publishes(room_state: RoomState) -> None:
    """A fallback command owner must never publish through an agent account."""
    config, paths, ids, _, client = room_state
    client.user_id = ids["agent1"].full_id
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.assert_not_awaited()


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
    config, paths, ids, events, client = room_state
    config.matrix_space.enabled = False
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock())
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    snapshot = await read_room_state(client, ROOM_ID)
    assert snapshot is not None
    monkeypatch.setattr(setup, "_reconcile_room_policies", AsyncMock(return_value={ROOM_ID: snapshot}))
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
async def test_live_config_reload_publishes_without_room_setup(room_state: RoomState) -> None:
    """Mode edits apply live, so a reload with no restarts still updates client state."""
    config, paths, _, events, client = room_state
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=config)
    install_bots(orchestrator, {ROUTER_AGENT_NAME: MagicMock(client=client)})
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
    config, paths, _, events, client = room_state
    config.matrix_space.enabled = False
    events[EVENT_TYPE, ""] = {"mode": "mixed"}
    snapshot = await read_room_state(client, ROOM_ID)
    assert snapshot is not None
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock())
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(setup, "_reconcile_room_policies", AsyncMock(return_value={ROOM_ID: snapshot}))
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
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock())
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
    config, paths, _, events, client = room_state
    config.matrix_space.enabled = False
    snapshot = await read_room_state(client, ROOM_ID)
    assert snapshot is not None
    client.room_get_state.reset_mock()
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock(), publication_allowed=lambda: False)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(setup, "_reconcile_room_policies", AsyncMock(return_value={ROOM_ID: snapshot}))
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
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock())
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(setup, "_reconcile_room_policies", AsyncMock(return_value={ROOM_ID: snapshot}))
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


def _router_duties(state: RoomState) -> RouterDuties:
    config, paths, ids, _, client = state
    deps = RouterDutiesDeps(
        runtime=MagicMock(config=config, runtime_paths=paths, client=client),
        logger=MagicMock(),
        matrix_id=ids[ROUTER_AGENT_NAME],
        hook_context=MagicMock(),
        journal=MagicMock(),
        room_lifecycle=MagicMock(send_welcome_message_if_empty=AsyncMock()),
        personal_rooms=MagicMock(),
    )
    deps.hook_context.registry.has_hooks.return_value = False
    deps.personal_rooms.observes_onboarding_joins = False
    return RouterDuties(deps)


def _membership_event(user_id: str, membership: str, previous: str) -> nio.RoomMemberEvent:
    event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": f"${membership}",
            "origin_server_ts": 1,
            "sender": user_id,
            "state_key": user_id,
            "content": {"membership": membership},
            "unsigned": {"prev_content": {"membership": previous}},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    return event


@pytest.mark.asyncio
async def test_reload_with_partial_membership(room_state: RoomState) -> None:
    """Configured responders remain eligible when lazy sync omits them."""
    config, paths, ids, events, client = room_state
    await reconcile_room_thread_mode(client, config, paths, ROOM_ID)
    client.rooms[ROOM_ID].remove_member(ids["agent2"].full_id)
    config.agents["agent2"].thread_mode = "room"
    await RoomSetup(paths, lambda: config, lambda: client, AsyncMock()).publish_room_state()
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
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock())
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
    config, paths, _, events, client = room_state
    for agent in config.agents.values():
        agent.rooms = []
        agent.thread_mode = "room"
    events[EVENT_TYPE, ""] = {"mode": "mixed"}
    if trigger == "startup":
        await RoomSetup(paths, lambda: config, lambda: client, AsyncMock()).publish_room_state()
    else:
        duties = _router_duties(room_state)
        duties.mark_joined_for_room_state(ROOM_ID)
        await duties.publish_joined_room_state()
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_startup_admission_reopens_during_joins(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication during setup cannot overwrite current state with a pre-join snapshot."""
    config, paths, ids, events, client = room_state
    config.agents["agent2"].thread_mode = "room"
    config.matrix_space.enabled = False
    for name in ("agent1", "agent2"):
        events["m.room.member", ids[name].full_id] = {"membership": "leave"}
    _sync_live_room(room_state)
    snapshot = await read_room_state(client, ROOM_ID)
    gate_open = False
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock(), publication_allowed=lambda: gate_open)
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(setup, "_reconcile_room_policies", AsyncMock(return_value={ROOM_ID: snapshot}))
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
    config, paths, _, events, client = room_state
    read = client.room_get_state_event.side_effect
    client.room_get_state_event.side_effect = failure if isinstance(failure, Exception) else None
    client.room_get_state_event.return_value = failure
    await RoomSetup(paths, lambda: config, lambda: client, AsyncMock()).publish_room_state()
    assert not _writes(client, EVENT_TYPE)
    client.room_get_state_event.side_effect = read
    await RoomSetup(paths, lambda: config, lambda: client, AsyncMock()).publish_room_state()
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
    config, paths, ids, events, client = room_state
    for name in ("agent1", "agent2"):
        events["m.room.member", ids[name].full_id] = {"membership": "leave"}
    if override:
        set_room_thread_mode_override(paths, room_id=ROOM_ID, mode="room")
    _sync_live_room(room_state)
    with capture_logs() as logs:
        await RoomSetup(paths, lambda: config, lambda: client, AsyncMock()).publish_room_state()
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
    await bot._on_ingestion_frame_completion()
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    assert _accounts(room_state) == sorted([router_id, ids["agent1"].full_id])
    # The frame publishes a join once; later frames reconcile nothing until another join.
    client.room_get_state_event.reset_mock()
    await bot._on_ingestion_frame_completion()
    client.room_get_state_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["startup", "router_join"])
async def test_router_lists_joined_mindroom_accounts_by_any_user_name(room_state: RoomState, trigger: str) -> None:
    """The router lists its joined router, agent, and team accounts, custom user names included, and nobody else."""
    config, paths, ids, events, client = room_state
    config.agents["mind"] = AgentConfig(display_name="Alice's mind")
    config.teams["team1"] = TeamConfig(display_name="Team 1", role="Test team", agents=["agent1"])
    ids = entity_ids(config, paths, usernames={"mind": "alice.mind"})
    for user_id in (ids["mind"].full_id, ids["team1"].full_id, "@alice:localhost"):
        events["m.room.member", user_id] = {"membership": "join"}
    events["m.room.member", ids["agent2"].full_id] = {"membership": "invite"}
    _sync_live_room(room_state)
    if trigger == "startup":
        await RoomSetup(paths, lambda: config, lambda: client, AsyncMock()).publish_room_state()
    else:
        duties = _router_duties(room_state)
        duties.mark_joined_for_room_state(ROOM_ID)
        await duties.publish_joined_room_state()
    assert _accounts(room_state) == sorted(
        ["@alice.mind:localhost", *(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "team1"))],
    )


@pytest.mark.asyncio
async def test_config_reload_drops_an_entity_the_config_no_longer_has(room_state: RoomState) -> None:
    """A reload republishes the list, so a removed agent that is still joined is no longer listed."""
    config, paths, ids, _, client = room_state
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=config)
    install_bots(orchestrator, {ROUTER_AGENT_NAME: MagicMock(client=client)})
    await orchestrator.config_reload.after_reload()
    assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "agent2"))
    del config.agents["agent2"]
    await orchestrator.config_reload.after_reload()
    assert _accounts(room_state) == sorted(ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1"))


@pytest.mark.asyncio
async def test_unchanged_accounts_are_written_once(room_state: RoomState) -> None:
    """Later triggers with unchanged membership do not rewrite the list."""
    config, paths, ids, _, client = room_state
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock())
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
@pytest.mark.parametrize("earlier_triggers", [1, 2])
async def test_sync_frame_waits_at_most_ten_seconds_behind_slow_triggers_in_its_room(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    earlier_triggers: int,
) -> None:
    """The reconcile timeout covers the wait for the room's lock, so the router's sync frame stalls at most 10 seconds."""
    config, paths, _, _, client = room_state

    async def unanswered(*_args: object, **_kwargs: object) -> None:
        await asyncio.Future()

    client.room_get_state_event.side_effect = unanswered
    client.joined_members.side_effect = unanswered
    loop = asyncio.get_running_loop()
    now = 0
    monkeypatch.setattr(loop, "time", lambda: now)
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock())
    triggers = [asyncio.create_task(setup.publish_room_state()) for _ in range(earlier_triggers)]
    duties = _router_duties(room_state)
    duties.mark_joined_for_room_state(ROOM_ID)
    frame = asyncio.create_task(duties.publish_joined_room_state())
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
@pytest.mark.parametrize("second_trigger", [None, "leave", "frame"])
async def test_reconcile_that_times_out_behind_a_slow_one_is_retried(
    room_state: RoomState,
    monkeypatch: pytest.MonkeyPatch,
    second_trigger: str | None,
) -> None:
    """A member change whose reconcile times out in the room's queue still reaches the room state without another trigger.

    Every accounts request takes 3 s: the reload holds the lock until 9 s, so agent2's leave times out at 14 s and its
    retry starts. A second trigger at 6 s, agent1 leaving or a sync frame for the room, queues ahead of that retry, times
    out at 16 s holding the lock and leaves the retry too little time, so the retry runs once more.
    """
    config, paths, ids, events, client = room_state
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
    tasks = [asyncio.create_task(RoomSetup(paths, lambda: config, lambda: client, AsyncMock()).publish_room_state())]
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
            tasks.append(asyncio.create_task(duties.publish_joined_room_state()))
        # Drain ready callbacks and timeout cancellation without wall-clock sleeps.
        for _ in range(50):
            await asyncio.sleep(0)
    assert all(task.done() for task in tasks)
    remaining = {ROUTER_AGENT_NAME, "agent1", "agent2"} - set(departures.values())
    assert _accounts(room_state) == sorted(ids[name].full_id for name in remaining)


@pytest.mark.asyncio
async def test_failed_reconciles_are_retried_once_per_room(room_state: RoomState) -> None:
    """Failures schedule one background retry per room, not one per trigger and not a loop."""
    _, _, ids, _, client = room_state
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    duties = _router_duties(room_state)
    event = _membership_event(ids["agent2"].full_id, "join", "leave")
    await asyncio.gather(*(duties.member_event(client.rooms[ROOM_ID], event) for _ in range(2)))
    for _ in range(50):
        await asyncio.sleep(0)
    # Two triggers, the retry, and one rerun for the trigger that failed while the retry ran; the retry's own failures
    # schedule nothing.
    assert len(_writes(client, ACCOUNTS_EVENT_TYPE)) == 4


@pytest.mark.asyncio
async def test_a_later_failure_gets_its_own_retry(room_state: RoomState) -> None:
    """A failure after the room's earlier retry finished schedules another retry."""
    _, _, ids, _, client = room_state
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    duties = _router_duties(room_state)
    event = _membership_event(ids["agent2"].full_id, "join", "leave")
    for expected_writes in (2, 4):
        await duties.member_event(client.rooms[ROOM_ID], event)
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(_writes(client, ACCOUNTS_EVENT_TYPE)) == expected_writes


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
    """The command saves the override but does not claim success when clients cannot see the new mode."""
    config, paths, _, _, client = room_state
    set_room_thread_mode_override(paths, room_id=ROOM_ID, mode="thread")
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    for _ in range(2):  # The reply asks to run the command again; a second failure warns again.
        reply = await handle_thread_mode_command(
            command,
            client=client,
            config=config,
            runtime_paths=paths,
            room_id=ROOM_ID,
            requester_user_id="@admin:localhost",
            sender_user_id="@admin:localhost",
        )
        assert reply.startswith("⚠️")
        assert "failed" in reply


async def _hang_a_room_state_retry(duties: RouterDuties, state: RoomState) -> None:
    """Fail one trigger's reconcile, so it schedules a retry, and hang that retry on its first write."""
    _, _, ids, _, client = state
    trigger_done = False

    async def refuse_then_hang(*_args: object, **_kwargs: object) -> nio.RoomPutStateError:
        if trigger_done:
            await asyncio.Future()
        return nio.RoomPutStateError("forbidden", "M_FORBIDDEN")

    client.room_put_state.side_effect = refuse_then_hang
    await duties.member_event(client.rooms[ROOM_ID], _membership_event(ids["agent2"].full_id, "join", "leave"))
    trigger_done = True


async def _hang_a_membership_refresh(duties: RouterDuties, orchestrator: Any) -> asyncio.Task[None]:  # noqa: ANN401
    """Start a membership refresh that never finishes and return the sync frame waiting for it."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.needs_refresh.return_value = True
    orchestrator.agent_reply_membership_sync = AgentReplyMembershipSync(memberships, lambda: ())
    started = asyncio.Event()

    async def hung_refresh() -> None:
        started.set()
        await asyncio.Future()

    orchestrator.refresh_agent_reply_memberships = hung_refresh
    frame = asyncio.create_task(duties.refresh_memberships_if_needed())
    await started.wait()
    return frame


@pytest.mark.asyncio
async def test_the_router_owns_its_refresh_and_room_state_retry(room_state: RoomState) -> None:
    """The router's runtime owns both background tasks, so its shutdown drain covers them."""
    duties = _router_duties(room_state)
    router = duties.deps.runtime
    await _hang_a_room_state_retry(duties, room_state)
    assert not await wait_for_background_tasks(timeout=0, owner=router)
    frame = await _hang_a_membership_refresh(duties, router.orchestrator)
    assert not await wait_for_background_tasks(timeout=0, owner=router)
    assert isinstance((await asyncio.gather(frame, return_exceptions=True))[0], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_router_stop_cancels_its_refresh_and_room_state_retry_without_waiting(room_state: RoomState) -> None:
    """A stopping router discards both, so they leave its stop budget to the replies it drains."""
    config, paths, ids, _, client = room_state
    user = AgentMatrixUser(ROUTER_AGENT_NAME, ids[ROUTER_AGENT_NAME].full_id, "Router", TEST_PASSWORD)
    bot = install_runtime_journal_support(make_test_agent_bot(user, config=config, runtime_paths=paths))
    bot.client = client
    duties = bot._router
    assert duties is not None
    await _hang_a_room_state_retry(duties, room_state)
    frame = await _hang_a_membership_refresh(duties, bot.orchestrator)
    budget = bot._sync_shutdown_budget = ShutdownBudget.start(5.0)
    await bot.prepare_for_sync_shutdown()
    assert budget.remaining_seconds() > 4
    assert isinstance((await asyncio.gather(frame, return_exceptions=True))[0], asyncio.CancelledError)
