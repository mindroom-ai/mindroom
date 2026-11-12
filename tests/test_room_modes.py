"""Plugin routes and chat commands change a room's thread and reply modes along one path.

A route changes them only for a room admin, and a thread mode change republishes `io.mindroom.thread_mode`, the state
clients choose a room's view by, so the stored override and the published state agree.
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

from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import client_session_store
from mindroom.commands.thread_mode_commands import handle_thread_mode_command
from mindroom.config.agent import AgentConfig, RoomConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.orchestration.plugin_http_runtime import bind_room_members
from mindroom.plugin_http import RoomModes, RoomReader, require_room_reader
from mindroom.room_reply_modes import resolve_room_reply_mode_override, set_room_reply_mode_override
from mindroom.room_thread_modes import resolve_room_thread_mode_override, set_room_thread_mode_override
from tests.conftest import test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ROOM_ID = "!room:localhost"
ALICE = "@alice:localhost"
BOB = "@bob:localhost"
THREAD_MODE = "io.mindroom.thread_mode"


@dataclass
class _Room:
    """One room on a fake homeserver, its router bot, and the API app plugin routes run in."""

    config: Config
    paths: RuntimePaths
    events: dict[tuple[str, str], dict[str, Any]]
    client: AsyncMock
    router: SimpleNamespace
    app: FastAPI

    async def reader(self, user_id: str = ALICE) -> RoomReader:
        token = client_session_store(self.app).create(user_id).token
        request = Request(
            {"type": "http", "app": self.app, "headers": [(b"authorization", f"Bearer {token}".encode())]},
        )
        return await require_room_reader(request, ROOM_ID)

    def thread_mode_command(self, args_text: str) -> Coroutine[Any, Any, str]:
        """Alice's `!thread_mode` command, as the router answers it."""
        return handle_thread_mode_command(
            args_text,
            client=self.client,
            config=self.config,
            runtime_paths=self.paths,
            room_id=ROOM_ID,
            requester_user_id=ALICE,
            sender_user_id=ALICE,
        )

    def stored(self) -> tuple[str | None, str | None]:
        return (
            resolve_room_thread_mode_override(self.paths, ROOM_ID),
            resolve_room_reply_mode_override(self.paths, ROOM_ID),
        )

    def published(self) -> dict[str, Any] | None:
        return self.events.get((THREAD_MODE, ""))


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
        # The router may send room state, as in a room it created.
        ("m.room.power_levels", ""): {"users": {ALICE: 100, bots[0]: 100}},
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
    # An agent's bot in the room still answers room reads while the router is stopped.
    bind_room_members(app, {ROUTER_AGENT_NAME: router, "agent1": SimpleNamespace(client=client, running=True)})  # type: ignore[dict-item]
    ensure_app_state(app).embedded = SimpleNamespace(agent_bot={ROUTER_AGENT_NAME: router}.get)  # type: ignore[assignment]
    return _Room(config, paths, events, client, router, app)


@pytest.mark.asyncio
async def test_modes_give_each_override_and_the_reply_mode_in_effect(room: _Room) -> None:
    """Each override is None while the room follows the config, and the reply mode is its `rooms` entry's, else auto."""
    reader = await room.reader(BOB)
    assert reader.modes() == RoomModes(thread=None, reply=None, reply_in_effect="auto")
    room.config.rooms[ROOM_ID] = RoomConfig(reply_mode="mentions")
    assert reader.modes() == RoomModes(thread=None, reply=None, reply_in_effect="mentions")
    set_room_thread_mode_override(room.paths, room_id=ROOM_ID, mode="room")
    set_room_reply_mode_override(room.paths, room_id=ROOM_ID, mode="auto")
    assert reader.modes() == RoomModes(thread="room", reply="auto", reply_in_effect="auto")


@pytest.mark.asyncio
async def test_an_admin_sets_and_clears_the_thread_mode_and_the_router_publishes_each(room: _Room) -> None:
    """The router publishes the override, and once it is cleared, the agents' configured mode; the reply mode stays."""
    set_room_reply_mode_override(room.paths, room_id=ROOM_ID, mode="mentions")
    reader = await room.reader()
    await reader.set_modes(thread="room")
    assert room.stored() == ("room", "mentions")
    assert room.published() == {"mode": "room"}
    await reader.set_modes(thread=None)
    assert room.stored() == (None, "mentions")
    assert room.published() == {"mode": "thread"}


@pytest.mark.asyncio
async def test_an_admin_sets_and_clears_the_reply_mode_and_the_thread_mode_stays(room: _Room) -> None:
    """A reply mode change keeps the thread mode override and publishes nothing."""
    set_room_thread_mode_override(room.paths, room_id=ROOM_ID, mode="room")
    reader = await room.reader()
    await reader.set_modes(reply="mentions")
    assert reader.modes() == RoomModes(thread="room", reply="mentions", reply_in_effect="mentions")
    await reader.set_modes(reply=None)
    assert reader.modes() == RoomModes(thread="room", reply=None, reply_in_effect="auto")
    room.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_call_changes_both_modes(room: _Room) -> None:
    """A change naming both modes stores both, and the router publishes the thread mode."""
    reader = await room.reader()
    await reader.set_modes(thread="room", reply="mentions")
    assert room.stored() == ("room", "mentions")
    assert room.published() == {"mode": "room"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("member", (403, "Only room admins change the room's modes.")),
        ("unreadable", (503, "Room state read failed; try again.")),
        ("router_stopped", (503, "The router is not running in this room.")),
        ("router_missing", (503, "The router is not running in this room.")),
        ("router_absent", (503, "The router is not running in this room.")),
    ],
)
async def test_a_refused_change_writes_and_publishes_nothing(
    room: _Room,
    case: str,
    expected: tuple[int, str],
) -> None:
    """A member without admin power gets 403, and an unreadable power read or a router not running in the room 503.

    A router that runs but is not in the room could not republish the thread mode, as in a direct message with one
    agent, where that agent's bot still answers the membership and power reads.
    """
    reader = await room.reader(BOB if case == "member" else ALICE)
    if case == "unreadable":
        room.client.room_get_state.side_effect = None
        room.client.room_get_state.return_value = nio.RoomGetStateError("unavailable")
    if case == "router_stopped":
        room.router.running = False
    if case == "router_missing":
        ensure_app_state(room.app).embedded = SimpleNamespace(agent_bot=lambda _name: None)  # type: ignore[assignment]
        with pytest.raises(HTTPException, match=r"^503: The router is not running in this room\.$"):
            reader.modes()
    if case == "router_absent":
        room.router.client = AsyncMock(spec=nio.AsyncClient, rooms={})
    with pytest.raises(HTTPException) as refused:
        await reader.set_modes(thread="room", reply="mentions")
    assert (refused.value.status_code, refused.value.detail) == expected
    assert room.stored() == (None, None)
    room.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_change_the_router_cannot_publish_is_saved_and_answers_503(room: _Room) -> None:
    """When the homeserver refuses the state write, both modes stay saved and the route says the thread mode is not published."""
    room.client.room_put_state.side_effect = None
    room.client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    reader = await room.reader()
    with pytest.raises(HTTPException) as unpublished:
        await reader.set_modes(thread="room", reply="mentions")
    assert (unpublished.value.status_code, unpublished.value.detail) == (
        503,
        "The modes were saved, but the room's thread mode is not published yet; try again.",
    )
    assert room.stored() == ("room", "mentions")
    assert room.published() is None


@pytest.mark.asyncio
async def test_a_thread_mode_command_the_router_cannot_publish_says_so(room: _Room) -> None:
    """`!thread_mode` keeps the override when the state write is refused, and its answer says the view may lag."""
    room.client.room_put_state.side_effect = None
    room.client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    assert await room.thread_mode_command("room") == (
        "⚠️ This room now uses `room` thread mode for future agent replies.\n\n"
        "Use `!thread_mode reset` to restore configured modes.\n\n"
        "Clients may still show the old thread mode in this room because publishing the new one failed. "
        "Run the command again to retry."
    )
    assert room.stored() == ("room", None)
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
