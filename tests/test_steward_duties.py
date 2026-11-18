"""Each bot builds the steward duties; only the room's steward at that moment runs them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.steward_duties import (
    StewardDuties,
    StewardDutiesDeps,
    moderator_rights_message,
    put_room_state_as_steward,
)
from tests.conftest import test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.runtime_protocols import OrchestratorRuntime

ROOM_ID = "!room:localhost"
THREAD_MODE = "io.mindroom.thread_mode"
ACCOUNTS = "io.mindroom.accounts"


class _Room:
    """One room on a fake homeserver, with the duties of every bot joined to it and the steward the test names."""

    def __init__(self, tmp_path: Path, names: tuple[str, ...]) -> None:
        self.config = Config(agents={"agent1": AgentConfig(display_name="Agent 1", rooms=[ROOM_ID])})
        self.paths = test_runtime_paths(tmp_path)
        self.ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(self.config, self.paths).items()}
        self.state: dict[tuple[str, str], dict[str, Any]] = {}
        self.writes: list[tuple[str, str, str]] = []
        self.refusal: nio.RoomPutStateError | None = None
        self.steward: str | None = names[0]
        self.joined = [self.ids[name] for name in names]
        self.duties = {name: self._duties(name) for name in names}

    def _duties(self, name: str) -> StewardDuties:
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = user_id = self.ids[name]
        room = nio.MatrixRoom(ROOM_ID, user_id)
        for member in self.joined:
            room.add_member(member, None, None)
        client.rooms = {ROOM_ID: room}

        async def read_event(room_id: str, event_type: str, state_key: str = "") -> object:
            if event_type == "m.room.member" and state_key in self.joined:
                return nio.RoomGetStateEventResponse({"membership": "join"}, event_type, state_key, room_id)
            content = self.state.get((event_type, state_key))
            if content is None:
                return nio.RoomGetStateEventError("Absent", "M_NOT_FOUND", room_id=room_id)
            return nio.RoomGetStateEventResponse(content, event_type, state_key, room_id)

        async def write(room_id: str, event_type: str, content: dict[str, Any], state_key: str = "") -> object:
            if self.refusal is not None:
                return self.refusal
            self.writes.append((user_id, event_type, state_key))
            self.state[event_type, state_key] = content
            return nio.RoomPutStateResponse("$state", room_id)

        client.room_get_state_event.side_effect = read_event
        client.room_put_state.side_effect = write
        client.joined_members.return_value = nio.JoinedMembersResponse(
            [nio.RoomMember(member, None, None) for member in self.joined],
            ROOM_ID,
        )
        runtime = MagicMock(config=self.config, runtime_paths=self.paths, client=client)
        runtime.orchestrator.room_steward.side_effect = lambda _room_id: (
            None if self.steward is None else SimpleNamespace(agent_name=self.steward, client=client)
        )
        deps = StewardDutiesDeps(
            runtime=runtime,
            logger=MagicMock(),
            hook_context=MagicMock(agent_name=name),
            journal=MagicMock(),
            room_lifecycle=MagicMock(),
            personal_rooms=MagicMock(),
        )
        return StewardDuties(deps)

    async def publish(self) -> None:
        """Every bot joined the room and its sync frame settled."""
        for duties in self.duties.values():
            duties.mark_joined_for_room_state(ROOM_ID)
            await duties.publish_joined_room_state()


@pytest.mark.asyncio
async def test_only_steward_publishes_room_state(tmp_path: Path) -> None:
    """Two bots share a room, and only its steward writes each state event, once."""
    room = _Room(tmp_path, (ROUTER_AGENT_NAME, "agent1"))
    await room.publish()
    owner = room.ids[ROUTER_AGENT_NAME]
    assert sorted(room.writes) == [(owner, ACCOUNTS, owner), (owner, THREAD_MODE, "")]


@pytest.mark.asyncio
async def test_agent_steward_publishes_accounts_under_its_own_key(tmp_path: Path) -> None:
    """An agent that stewards a room without the owner account lists the room's accounts under its own user ID."""
    room = _Room(tmp_path, ("agent1",))
    await room.publish()
    agent = room.ids["agent1"]
    assert room.state[ACCOUNTS, agent] == {"accounts": [agent]}
    assert room.state[THREAD_MODE, ""] == {"mode": "thread"}


@pytest.mark.asyncio
async def test_forbidden_background_write_is_silent(tmp_path: Path) -> None:
    """A room whose power levels refuse the steward's state logs at debug level, and the steward sends nothing."""
    room = _Room(tmp_path, ("agent1",))
    room.refusal = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    duties = room.duties["agent1"]
    duties.mark_joined_for_room_state(ROOM_ID)
    with capture_logs() as logs:
        await duties.publish_joined_room_state()
    duties.stopping()
    assert {log["log_level"] for log in logs} == {"debug"}
    cast("AsyncMock", duties.deps.runtime.client.room_send).assert_not_awaited()


@pytest.mark.asyncio
async def test_power_levels_event_makes_new_steward_publish(tmp_path: Path) -> None:
    """A bot that a power-level change makes the room's steward publishes the room's state after that frame."""
    room = _Room(tmp_path, (ROUTER_AGENT_NAME, "agent1"))
    room.steward = None
    await room.publish()
    assert room.writes == []
    room.steward = "agent1"
    for duties in room.duties.values():
        await duties.power_levels_event(duties.deps.runtime.client.rooms[ROOM_ID], MagicMock())
        await duties.publish_joined_room_state()
    agent = room.ids["agent1"]
    assert sorted(room.writes) == [(agent, ACCOUNTS, agent), (agent, THREAD_MODE, "")]


@pytest.mark.asyncio
async def test_put_room_state_as_steward_returns_false_without_power(tmp_path: Path) -> None:
    """The write goes through the steward's client and reports a refusal, or a room without a steward, as False."""
    room = _Room(tmp_path, ("agent1",))
    runtime = cast("OrchestratorRuntime", room.duties["agent1"].deps.runtime.orchestrator)
    assert await put_room_state_as_steward(runtime, ROOM_ID, "io.mindroom.test", {"a": 1}, state_key="k")
    assert room.state["io.mindroom.test", "k"] == {"a": 1}
    room.refusal = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    assert not await put_room_state_as_steward(runtime, ROOM_ID, "io.mindroom.test", {"a": 2})
    room.steward = None
    assert not await put_room_state_as_steward(runtime, ROOM_ID, "io.mindroom.test", {"a": 3})


def test_moderator_rights_message_text() -> None:
    """The refusal a person reads names the feature and asks for moderator rights."""
    assert moderator_rights_message("save schedules") == "I need moderator rights in this room to save schedules."
