"""The room state contract clients use to choose a conversation view."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import nio
import pytest
from structlog.testing import capture_logs

from mindroom.commands.thread_mode_commands import handle_thread_mode_command
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.room_thread_mode import publish_room_thread_mode
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.orchestration.rooms import RoomSetup
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.room_thread_modes import clear_room_thread_mode_override, set_room_thread_mode_override
from mindroom.router.duties import RouterDuties, RouterDutiesDeps
from tests.conftest import install_bots, test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID

EVENT_TYPE = "io.mindroom.thread_mode"
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
    client.rooms = {}

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

    client.room_get_state.side_effect = read
    client.room_put_state.side_effect = write
    return config, paths, ids, events, client


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
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.assert_awaited_once_with(ROOM_ID, EVENT_TYPE, {"mode": expected}, state_key="")


@pytest.mark.asyncio
async def test_updates_after_reload_membership_and_runtime_overrides(room_state: RoomState) -> None:
    """Keep state current after mode changes, resets, and joins or departures."""
    config, paths, ids, events, client = room_state
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}
    config.agents["agent2"].room_thread_modes = {ROOM_ID: "room"}
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    set_room_thread_mode_override(paths, room_id=ROOM_ID, mode="thread")
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "thread"}
    clear_room_thread_mode_override(paths, ROOM_ID)
    config.agents["agent2"].room_thread_modes = {}
    events["m.room.member", ids["agent2"].full_id] = {"membership": "leave"}
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    events["m.room.member", ids["agent2"].full_id] = {"membership": "join"}
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
async def test_skips_identical_state_even_with_concurrent_reconciliation(room_state: RoomState) -> None:
    """Setup and live membership reconciliation must not duplicate state events."""
    config, paths, _, _, client = room_state
    await asyncio.gather(*(publish_room_thread_mode(client, config, paths, ROOM_ID) for _ in range(3)))
    client.room_put_state.assert_awaited_once()
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_invited_agents_do_not_contribute_and_empty_room_defaults_to_threads(room_state: RoomState) -> None:
    """An invitation cannot answer yet; an empty roster has a deterministic view."""
    config, paths, ids, events, client = room_state
    events["m.room.member", ids["agent2"].full_id] = {"membership": "invite"}
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "room"}
    events["m.room.member", ids["agent1"].full_id] = {"membership": "ban"}
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "thread"}


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
        events["m.room.member", ids[name].full_id] = {"membership": "leave"}
    events["m.room.member", ids["team1"].full_id] = {"membership": "join"}
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    assert events[EVENT_TYPE, ""] == {"mode": "thread"}
    config.agents["agent2"].thread_mode = "room"
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
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
        await publish_room_thread_mode(client, config, paths, ROOM_ID)
        await publish_room_thread_mode(client, config, paths, ROOM_ID)
    assert client.room_put_state.await_count == 2
    assert len(logs) == 2
    assert all(log["event"] == "room_thread_mode_publish_failed" and log["log_level"] == "warning" for log in logs)


@pytest.mark.asyncio
async def test_unreadable_state_does_not_overwrite_mode(room_state: RoomState) -> None:
    """Unreadable membership cannot establish the effective room mode."""
    config, paths, _, _, client = room_state
    client.room_get_state.side_effect = aiohttp.ClientConnectionError("offline")
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_router_publishes(room_state: RoomState) -> None:
    """A fallback command owner must never publish through an agent account."""
    config, paths, ids, _, client = room_state
    client.user_id = ids["agent1"].full_id
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_membership_callback_publishes_agent_join_and_leave(room_state: RoomState) -> None:
    """Router membership duties update state even without human membership hooks."""
    config, paths, ids, events, client = room_state
    deps = RouterDutiesDeps(
        runtime=MagicMock(config=config, runtime_paths=paths, client=client),
        logger=MagicMock(),
        matrix_id=ids[ROUTER_AGENT_NAME],
        hook_context=MagicMock(),
        journal=MagicMock(),
        room_lifecycle=MagicMock(),
        personal_rooms=MagicMock(),
    )
    deps.hook_context.registry.has_hooks.return_value = False
    deps.personal_rooms.observes_onboarding_joins = False
    duties = RouterDuties(deps)
    room = nio.MatrixRoom(ROOM_ID, client.user_id)
    for membership, expected in (("leave", "room"), ("join", "mixed")):
        events["m.room.member", ids["agent2"].full_id] = {"membership": membership}
        event = nio.RoomMemberEvent.from_dict(
            {
                "type": "m.room.member",
                "event_id": f"${membership}",
                "sender": ids["agent2"].full_id,
                "state_key": ids["agent2"].full_id,
                "origin_server_ts": 1,
                "content": {"membership": membership},
            },
        )
        assert isinstance(event, nio.RoomMemberEvent)
        await duties.member_event(room, event)
        assert events[EVENT_TYPE, ""] == {"mode": expected}


@pytest.mark.asyncio
async def test_setup_publishes_after_agents_join(room_state: RoomState, monkeypatch: pytest.MonkeyPatch) -> None:
    """Initial room setup publishes after every bot has joined."""
    config, paths, ids, events, client = room_state
    config.matrix_space.enabled = False
    setup = RoomSetup(paths, lambda: config, lambda: client, AsyncMock())
    monkeypatch.setattr(setup, "ensure_rooms_exist", AsyncMock(return_value={"room": ROOM_ID}))
    monkeypatch.setattr(setup, "_reconcile_room_policies", AsyncMock(return_value={}))
    monkeypatch.setattr(setup, "_ensure_room_invitations", AsyncMock())
    bot = AsyncMock()
    bot.agent_name = "agent2"
    events["m.room.member", ids["agent2"].full_id] = {"membership": "leave"}

    async def join() -> None:
        events["m.room.member", ids["agent2"].full_id] = {"membership": "join"}

    bot.ensure_rooms.side_effect = join
    await setup.setup([bot])
    assert events[EVENT_TYPE, ""] == {"mode": "mixed"}


@pytest.mark.asyncio
async def test_command_set_and_reset_publish_immediately(room_state: RoomState) -> None:
    """Authorized commands publish changed effective modes before returning."""
    config, paths, _, events, client = room_state
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        {"users": {"@admin:localhost": 100}},
        "m.room.power_levels",
        "",
        ROOM_ID,
    )
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
    plan = ConfigUpdatePlan(new_config=config, live_updated=frozenset({"agent2"}))
    await orchestrator._reconcile_post_update_rooms(plan, set())
    assert events[EVENT_TYPE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_existing_state_is_not_resent_on_first_reconciliation(room_state: RoomState) -> None:
    """A runtime restart keeps an already correct room state event."""
    config, paths, _, events, client = room_state
    events[EVENT_TYPE, ""] = {"mode": "mixed"}
    await publish_room_thread_mode(client, config, paths, ROOM_ID)
    client.room_put_state.assert_not_awaited()
