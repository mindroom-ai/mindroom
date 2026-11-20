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
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.room_steward import room_steward
from mindroom.steward_duties import (
    StewardDuties,
    StewardDutiesDeps,
    moderator_rights_message,
    put_room_state_as_steward,
    steward_may_write_state,
)
from tests.conftest import install_bots, test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.runtime_protocols import OrchestratorRuntime

ROOM_ID = "!room:localhost"
PERSON = "@alice:localhost"
THREAD_MODE = "io.mindroom.thread_mode"
ACCOUNTS = "io.mindroom.accounts"
BINDING = "io.mindroom.router"


class _Room:
    """A room no config names, on a fake homeserver, with one person and the duties of every bot joined to it.

    The steward is elected as in production, from the power each bot's own cache of the room gives its account.
    The first named bot starts as the steward, at power 100; the others start at 50.
    """

    def __init__(self, tmp_path: Path, names: tuple[str, ...]) -> None:
        self.config = Config(agents={"agent1": AgentConfig(display_name="Agent 1", thread_mode="room")})
        self.paths = test_runtime_paths(tmp_path)
        self.ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(self.config, self.paths).items()}
        self.state: dict[tuple[str, str], dict[str, Any]] = {}
        self.writes: list[tuple[str, str, str]] = []
        self.refusal: nio.RoomPutStateError | None = None
        self.joined = [PERSON, *(self.ids[name] for name in names)]
        self.bots: dict[str, Any] = {}
        self.runtime = MagicMock(agent_bots=self.bots)
        self.runtime.room_steward.side_effect = lambda room_id: room_steward(self.bots, room_id)
        for name in names:
            self._add_bot(name)
            self.apply_power(name, {self.ids[names[0]]: 100})

    @property
    def orchestrator(self) -> OrchestratorRuntime:
        return cast("OrchestratorRuntime", self.runtime)

    def duties(self, name: str) -> StewardDuties:
        return self.bots[name].steward_duties

    def apply_power(self, name: str, users: dict[str, int]) -> None:
        """Let one bot's sync apply a power-levels event, which its own cache of the room then shows."""
        room = self.bots[name].client.rooms[ROOM_ID]
        room.power_levels.defaults.users_default = 50
        room.power_levels.users.clear()
        room.power_levels.users.update(users)

    def _add_bot(self, name: str) -> None:
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = user_id = self.ids[name]
        room = nio.MatrixRoom(ROOM_ID, user_id)
        for member in self.joined:
            room.add_member(member, None, None)
        room.members_synced = True
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
        deps = StewardDutiesDeps(
            runtime=MagicMock(config=self.config, runtime_paths=self.paths, client=client, orchestrator=self.runtime),
            logger=MagicMock(),
            hook_context=MagicMock(agent_name=name),
            journal=MagicMock(),
            room_lifecycle=MagicMock(),
            personal_rooms=MagicMock(),
        )
        self.bots[name] = SimpleNamespace(
            agent_name=name,
            running=True,
            client=client,
            steward_duties=StewardDuties(deps),
        )

    async def frame(self, name: str, *, joined: bool = False, power_levels: bool = False) -> None:
        """One bot's sync frame settles, after the bot joined the room or applied a power-levels event in it."""
        duties = self.duties(name)
        if joined:
            duties.mark_joined_for_room_state(ROOM_ID)
        if power_levels:
            await duties.power_levels_event(self.bots[name].client.rooms[ROOM_ID], MagicMock())
        await duties.publish_joined_room_state()


@pytest.mark.asyncio
async def test_only_steward_publishes_room_state(tmp_path: Path) -> None:
    """Two bots share a room, and only its steward writes each state event, once."""
    room = _Room(tmp_path, (ROUTER_AGENT_NAME, "agent1"))
    for name in room.bots:
        await room.frame(name, joined=True)
    for name in room.bots:
        await room.frame(name)
    owner = room.ids[ROUTER_AGENT_NAME]
    assert sorted(room.writes) == [(owner, ACCOUNTS, owner), (owner, BINDING, ""), (owner, THREAD_MODE, "")]


@pytest.mark.asyncio
async def test_agent_steward_publishes_accounts_under_its_own_key(tmp_path: Path) -> None:
    """One agent with one person, in a room no config names and without the owner account, publishes the room's state."""
    room = _Room(tmp_path, ("agent1",))
    await room.frame("agent1", joined=True)
    agent = room.ids["agent1"]
    assert room.state[ACCOUNTS, agent] == {"accounts": [agent]}
    assert room.state[THREAD_MODE, ""] == {"mode": "room"}


@pytest.mark.asyncio
async def test_forbidden_background_write_is_silent(tmp_path: Path) -> None:
    """A room whose power levels refuse the steward's state logs at debug level, and the steward sends nothing."""
    room = _Room(tmp_path, ("agent1",))
    room.refusal = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    with capture_logs() as logs:
        await room.frame("agent1", joined=True)
    room.duties("agent1").stopping()
    assert {log["log_level"] for log in logs} == {"debug"}
    room.bots["agent1"].client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_power_levels_event_makes_new_steward_publish(tmp_path: Path) -> None:
    """A demotion the successor applies before the old steward does still ends with the successor publishing."""
    room = _Room(tmp_path, (ROUTER_AGENT_NAME, "agent1"))
    await room.frame(ROUTER_AGENT_NAME, joined=True)
    room.writes.clear()
    # The old steward's own cache still shows its old power, so the election still names it.
    room.apply_power("agent1", {})
    await room.frame("agent1", power_levels=True)
    assert room.writes == []
    room.apply_power(ROUTER_AGENT_NAME, {})
    await room.frame(ROUTER_AGENT_NAME, power_levels=True)
    await room.frame("agent1")
    agent = room.ids["agent1"]
    assert room.writes == [(agent, ACCOUNTS, agent)]


@pytest.mark.asyncio
async def test_sync_that_opens_publishes_every_stewarded_room(tmp_path: Path) -> None:
    """A bot's first settled frame publishes the rooms it stewards, as a power change in state alone reaches no callback."""
    room = _Room(tmp_path, (ROUTER_AGENT_NAME, "agent1"))
    await room.duties("agent1").publish_joined_room_state(every_room=True)
    assert room.writes == []
    await room.duties(ROUTER_AGENT_NAME).publish_joined_room_state(every_room=True)
    owner = room.ids[ROUTER_AGENT_NAME]
    assert sorted(room.writes) == [(owner, ACCOUNTS, owner), (owner, BINDING, ""), (owner, THREAD_MODE, "")]


@pytest.mark.asyncio
async def test_sweep_reconciles_each_held_room_once_through_its_steward(tmp_path: Path) -> None:
    """A startup or reload sweep reaches a room without the owner account, and a joined owner that is not steward stays out."""
    room = _Room(tmp_path, ("agent1", ROUTER_AGENT_NAME))
    orchestrator = _MultiAgentOrchestrator(runtime_paths=room.paths, config=room.config)
    install_bots(orchestrator, room.bots)
    await orchestrator.config_reload.after_reload()
    agent = room.ids["agent1"]
    assert sorted(room.writes) == [(agent, ACCOUNTS, agent), (agent, BINDING, ""), (agent, THREAD_MODE, "")]
    # A reload that only changes the config republishes the thread mode of the room it already holds.
    room.config.agents["agent1"].thread_mode = "thread"
    await orchestrator.config_reload.after_reload()
    assert room.state[THREAD_MODE, ""] == {"mode": "thread"}
    assert {writer for writer, _, _ in room.writes} == {agent}


@pytest.mark.asyncio
async def test_put_room_state_as_steward_returns_false_without_power(tmp_path: Path) -> None:
    """The write goes through the steward's client and reports a refusal, or a room without a steward, as False."""
    room = _Room(tmp_path, ("agent1",))
    assert await put_room_state_as_steward(
        room.orchestrator.room_steward,
        ROOM_ID,
        "io.mindroom.test",
        {"a": 1},
        state_key="k",
    )
    assert room.state["io.mindroom.test", "k"] == {"a": 1}
    room.refusal = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    assert not await put_room_state_as_steward(room.orchestrator.room_steward, ROOM_ID, "io.mindroom.test", {"a": 2})
    room.bots.clear()
    assert not await put_room_state_as_steward(room.orchestrator.room_steward, ROOM_ID, "io.mindroom.test", {"a": 3})


def test_steward_may_write_state_reads_the_stewards_own_power() -> None:
    """A refusal is for lack of power when the steward's own view of the room gives it less than the event needs."""
    levels = nio.PowerLevels(defaults=nio.DefaultLevels(state_default=50), users={"@weak:localhost": 0})
    client = SimpleNamespace(user_id="@weak:localhost", rooms={ROOM_ID: SimpleNamespace(power_levels=levels)})
    steward = cast("Any", SimpleNamespace(client=client))

    assert not steward_may_write_state(lambda _room_id: steward, ROOM_ID, "m.room.encryption")
    levels.users["@weak:localhost"] = 50
    assert steward_may_write_state(lambda _room_id: steward, ROOM_ID, "m.room.encryption")
    # No steward, or one that does not hold the room yet, may write nothing.
    assert not steward_may_write_state(lambda _room_id: None, ROOM_ID, "m.room.encryption")
    assert not steward_may_write_state(lambda _room_id: steward, "!other:localhost", "m.room.encryption")


def test_moderator_rights_message_text() -> None:
    """The refusal a person reads names the feature and asks for moderator rights."""
    assert moderator_rights_message("save schedules") == "I need moderator rights in this room to save schedules."
