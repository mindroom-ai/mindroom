"""Plugin routes and chat commands change a room's thread and reply modes along one path.

A route changes them only for a room admin, and a thread mode change republishes `io.mindroom.thread_mode`, the state
clients choose a room's view by, so the stored override and the published state agree. The reply mode override is the
room's `reply_mode` setting in `io.mindroom.room_settings` state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Any
from unittest.mock import AsyncMock
from urllib.parse import quote

import nio
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from mindroom.api.client_sessions import client_session_store
from mindroom.commands.thread_mode_commands import handle_thread_mode_command
from mindroom.config.agent import AgentConfig, RoomConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.orchestration.plugin_http_runtime import bind_room_members
from mindroom.plugin_http import RoomModes, RoomReader, require_room_reader
from mindroom.room_reply_modes import resolve_room_reply_mode_override, write_room_reply_mode
from mindroom.room_thread_modes import resolve_room_thread_mode_override, set_room_thread_mode_override
from tests.conftest import test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from mindroom.config.agent import RoomReplyMode
    from mindroom.constants import RuntimePaths

ROOM_ID = "!room:localhost"
ALICE = "@alice:localhost"
BOB = "@bob:localhost"
THREAD_MODE = "io.mindroom.thread_mode"
REPLY_SETTING = ("io.mindroom.room_settings", "reply_mode")


@dataclass
class _Room:
    """One room on a fake homeserver, its router bot, and the API app plugin routes run in."""

    config: Config
    paths: RuntimePaths
    events: dict[tuple[str, str], dict[str, Any]]
    client: AsyncMock
    router: SimpleNamespace
    app: FastAPI
    agent: SimpleNamespace

    async def reader(self, user_id: str = ALICE) -> RoomReader:
        token = client_session_store(self.app).create(user_id).token
        request = Request(
            {"type": "http", "app": self.app, "headers": [(b"authorization", f"Bearer {token}".encode())]},
        )
        return await require_room_reader(request, ROOM_ID)

    def thread_mode_command(self, args_text: str, put_state: AsyncMock | None = None) -> Coroutine[Any, Any, str]:
        """Alice's `!thread_mode` command, as the router answers it."""
        return handle_thread_mode_command(
            args_text,
            client=self.client,
            config=self.config,
            runtime_paths=self.paths,
            room_id=ROOM_ID,
            requester_user_id=ALICE,
            sender_user_id=ALICE,
            put_state=put_state,
        )

    def stored(self) -> tuple[str | None, str | None]:
        return (
            resolve_room_thread_mode_override(self.paths, ROOM_ID),
            resolve_room_reply_mode_override(self.paths, ROOM_ID),
        )

    def published(self) -> dict[str, Any] | None:
        return self.events.get((THREAD_MODE, ""))

    async def set_reply_setting(self, mode: RoomReplyMode) -> None:
        """A room admin sets the room's reply mode setting, which the runtime then knows."""
        assert await write_room_reply_mode(self.client, self.paths, ROOM_ID, mode)


@pytest.fixture
def room(tmp_path: Path) -> _Room:
    """Alice administers a room with the router and two agents in `thread` mode; Bob is a plain member."""
    config = Config(
        agents={
            "agent1": AgentConfig(display_name="Agent 1", rooms=[ROOM_ID], thread_mode="thread"),
            "agent2": AgentConfig(display_name="Agent 2", rooms=[ROOM_ID], thread_mode="thread"),
        },
    )
    paths = test_runtime_paths(tmp_path)
    ids = entity_ids(config, paths)
    bots = [ids[name].full_id for name in (ROUTER_AGENT_NAME, "agent1", "agent2")]
    events: dict[tuple[str, str], dict[str, Any]] = {
        ("m.room.create", ""): {"room_version": "10"},
        ("m.room.power_levels", ""): {"users": {ALICE: 100}},
        **{("m.room.member", user_id): {"membership": "join"} for user_id in (*bots, ALICE, BOB)},
    }
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = bots[0]
    client.rooms = {ROOM_ID: nio.MatrixRoom(ROOM_ID, client.user_id)}

    async def joined_members(room_id: str) -> nio.JoinedMembersResponse:
        joined = [
            key
            for (kind, key), content in events.items()
            if kind == "m.room.member" and content == {"membership": "join"}
        ]
        return nio.JoinedMembersResponse([nio.RoomMember(user_id, None, None) for user_id in joined], room_id)

    async def read_state(room_id: str) -> nio.RoomGetStateResponse:
        await asyncio.sleep(0)
        listed = [
            {"type": kind, "state_key": key, "sender": ALICE, "content": dict(content)}
            for (kind, key), content in events.items()
        ]
        return nio.RoomGetStateResponse(listed, room_id)

    async def read_event(room_id: str, event_type: str, state_key: str = "") -> object:
        await asyncio.sleep(0)
        content = events.get((event_type, state_key))
        if content is None:
            return nio.RoomGetStateEventError("Absent", "M_NOT_FOUND", room_id=room_id)
        return nio.RoomGetStateEventResponse(dict(content), event_type, state_key, room_id)

    async def write_event(room_id: str, event_type: str, content: dict[str, Any], state_key: str = "") -> object:
        await asyncio.sleep(0)
        events[event_type, state_key] = dict(content)
        return nio.RoomPutStateResponse("$state", room_id)

    client.joined_members.side_effect = joined_members
    client.room_get_state.side_effect = read_state
    client.room_get_state_event.side_effect = read_event
    client.room_put_state.side_effect = write_event
    router = SimpleNamespace(client=client, running=True, config=config, runtime_paths=paths)
    app = FastAPI()
    # An agent's bot in the room is its steward while the router is stopped.
    agent = SimpleNamespace(client=client, running=True, config=config, runtime_paths=paths)
    bind_room_members(app, {ROUTER_AGENT_NAME: router, "agent1": agent})  # type: ignore[dict-item]
    return _Room(config, paths, events, client, router, app, agent)


@pytest.mark.asyncio
async def test_modes_give_each_override_and_the_reply_mode_in_effect(room: _Room) -> None:
    """Each override is None while the room follows the config, and the reply mode is its `rooms` entry's, else auto."""
    reader = await room.reader(BOB)
    assert reader.modes() == RoomModes(thread=None, reply=None, reply_in_effect="auto")
    room.config.rooms[ROOM_ID] = RoomConfig(reply_mode="mentions")
    assert reader.modes() == RoomModes(thread=None, reply=None, reply_in_effect="mentions")
    set_room_thread_mode_override(room.paths, room_id=ROOM_ID, mode="room")
    await room.set_reply_setting("auto")
    assert reader.modes() == RoomModes(thread="room", reply="auto", reply_in_effect="auto")


@pytest.mark.asyncio
async def test_an_admin_sets_and_clears_the_thread_mode_and_the_router_publishes_each(room: _Room) -> None:
    """The router publishes the override, and once it is cleared, the agents' configured mode; the reply mode stays."""
    await room.set_reply_setting("mentions")
    reader = await room.reader()
    await reader.set_modes(thread="room")
    assert room.stored() == ("room", "mentions")
    assert room.published() == {"mode": "room"}
    await reader.set_modes(thread=None)
    assert room.stored() == (None, "mentions")
    assert room.published() == {"mode": "thread"}


@pytest.mark.asyncio
async def test_an_admin_sets_and_clears_the_reply_mode_and_the_thread_mode_stays(room: _Room) -> None:
    """A reply mode change sets the room's reply mode setting, keeps the thread mode override and publishes no view."""
    set_room_thread_mode_override(room.paths, room_id=ROOM_ID, mode="room")
    reader = await room.reader()
    await reader.set_modes(reply="mentions")
    assert reader.modes() == RoomModes(thread="room", reply="mentions", reply_in_effect="mentions")
    assert room.events[REPLY_SETTING] == {"value": "mentions"}
    await reader.set_modes(reply=None)
    assert reader.modes() == RoomModes(thread="room", reply=None, reply_in_effect="auto")
    assert room.events[REPLY_SETTING] == {}
    assert room.published() is None


@pytest.mark.asyncio
async def test_one_call_changes_both_modes(room: _Room) -> None:
    """A change naming both modes stores both, and the router publishes the thread mode."""
    reader = await room.reader()
    await reader.set_modes(thread="room", reply="mentions")
    assert room.stored() == ("room", "mentions")
    assert room.published() == {"mode": "room"}
    assert room.events[REPLY_SETTING] == {"value": "mentions"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("member", (403, "Only room admins change the room's modes.")),
        ("unreadable", (503, "Room state read failed; try again.")),
        ("no_agent_running", (503, "No running MindRoom bot is in this room.")),
    ],
)
async def test_a_refused_change_writes_and_publishes_nothing(
    room: _Room,
    case: str,
    expected: tuple[int, str],
) -> None:
    """A member without admin power gets 403, and an unreadable power read or a room with no running agent 503."""
    reader = await room.reader(BOB if case == "member" else ALICE)
    if case == "unreadable":
        room.client.room_get_state.side_effect = None
        room.client.room_get_state.return_value = nio.RoomGetStateError("unavailable")
    if case == "no_agent_running":
        room.router.running = room.agent.running = False
        with pytest.raises(HTTPException, match=r"^503: No MindRoom agent is running; try again\.$"):
            reader.modes()
    with pytest.raises(HTTPException) as refused:
        await reader.set_modes(thread="room", reply="mentions")
    assert (refused.value.status_code, refused.value.detail) == expected
    assert room.stored() == (None, None)
    room.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["thread", "reply"])
async def test_room_mode_refusal_is_403_moderator_rights(room: _Room, mode: str) -> None:
    """With no owner account running, the agent is the steward: a room that refuses its write changes nothing, 403."""
    room.router.running = False
    room.client.room_put_state.side_effect = None
    room.client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    reader = await room.reader()
    with pytest.raises(HTTPException) as refused:
        await (reader.set_modes(thread="room") if mode == "thread" else reader.set_modes(thread="room", reply="auto"))
    assert (refused.value.status_code, refused.value.detail) == (
        403,
        f"I need moderator rights in this room to change the {mode} mode.",
    )
    assert room.stored() == (None, None)
    assert room.published() is None
    assert REPLY_SETTING not in room.events


@pytest.mark.asyncio
async def test_an_agent_publishes_the_thread_mode_while_the_router_is_stopped(room: _Room) -> None:
    """The room's steward publishes the change, whichever account that is."""
    room.router.running = False
    await (await room.reader()).set_modes(thread="room")
    assert (room.stored(), room.published()) == (("room", None), {"mode": "room"})


@pytest.mark.asyncio
async def test_a_change_the_steward_cannot_publish_is_saved_and_answers_503(room: _Room) -> None:
    """When the state write fails for another reason than power, the thread mode stays saved and the route says so."""
    room.client.room_put_state.side_effect = None
    room.client.room_put_state.return_value = nio.RoomPutStateError("down", "M_UNKNOWN")
    reader = await room.reader()
    with pytest.raises(HTTPException) as unpublished:
        await reader.set_modes(thread="room")
    assert (unpublished.value.status_code, unpublished.value.detail) == (
        503,
        "The modes were saved, but the room's thread mode is not published yet; try again.",
    )
    assert room.stored() == ("room", None)
    assert room.published() is None


@pytest.mark.asyncio
async def test_a_reply_mode_the_room_does_not_take_changes_nothing(room: _Room) -> None:
    """When the reply mode write fails for another reason than power, the route answers 503 before the thread mode."""
    room.client.room_put_state.side_effect = None
    room.client.room_put_state.return_value = nio.RoomPutStateError("down", "M_UNKNOWN")
    reader = await room.reader()
    with pytest.raises(HTTPException) as refused:
        await reader.set_modes(thread="room", reply="mentions")
    assert (refused.value.status_code, refused.value.detail) == (
        503,
        "The room did not take its reply mode; nothing was changed.",
    )
    assert room.stored() == (None, None)
    assert REPLY_SETTING not in room.events


@pytest.mark.asyncio
async def test_a_thread_mode_command_the_room_refuses_changes_nothing(room: _Room) -> None:
    """`!thread_mode` keeps the old mode when the state write is refused, and its answer asks for the rights."""
    refused = AsyncMock(return_value=None)
    assert (
        await room.thread_mode_command("room", refused)
        == "I need moderator rights in this room to change the thread mode."
    )
    assert room.stored() == (None, None)
    assert room.published() is None


def test_a_route_returns_the_modes_as_its_typed_response(room: _Room) -> None:
    """A route annotated to return `RoomModes` answers them as JSON."""

    @room.app.get("/rooms/{room_id}/modes")
    async def modes(reader: Annotated[RoomReader, Depends(require_room_reader)]) -> RoomModes:
        return reader.modes()

    set_room_thread_mode_override(room.paths, room_id=ROOM_ID, mode="room")
    token = client_session_store(room.app).create(BOB).token
    response = TestClient(room.app).get(
        f"/rooms/{quote(ROOM_ID, safe='')}/modes",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (response.status_code, response.json()) == (
        200,
        {"thread": "room", "reply": None, "reply_in_effect": "auto"},
    )


@pytest.mark.asyncio
async def test_thread_mode_reset_without_an_override_says_so_and_still_republishes(room: _Room) -> None:
    """`!thread_mode reset` answers as before through the shared path, and still publishes the configured mode."""
    assert await room.thread_mode_command("reset") == "This room has no thread mode override."
    assert room.published() == {"mode": "thread"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route_mode", "command"),
    [("room", "thread"), ("thread", "room"), (None, "room"), ("room", "reset")],
)
async def test_a_route_and_a_command_at_once_leave_the_override_and_published_state_agreeing(
    room: _Room,
    route_mode: str | None,
    command: str,
) -> None:
    """Whichever change lands last, the published mode is the stored override, else the agents' configured mode."""
    set_room_thread_mode_override(room.paths, room_id=ROOM_ID, mode="thread")
    reader = await room.reader()
    await asyncio.gather(
        reader.set_modes(thread=route_mode),  # type: ignore[arg-type]
        room.thread_mode_command(command),
    )
    stored, _ = room.stored()
    assert room.published() == {"mode": stored or "thread"}
