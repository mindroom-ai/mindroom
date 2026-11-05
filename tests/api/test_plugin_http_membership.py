"""Plugin routes can require live room membership, read through a running bot in the room, the router first."""

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nio
import pytest
from fastapi import FastAPI, HTTPException, Request

from mindroom.api import main
from mindroom.api.app_state import ensure_app_state
from mindroom.orchestration.plugin_http_runtime import (
    _bind_room_members,
    _unbind_room_members,
    bind_room_members_if_ready,
)
from mindroom.plugin_http import require_room_member

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"


@pytest.fixture
def main_app() -> Iterator[FastAPI]:
    """The global API app, with whatever room-members reader it had put back afterwards, even after a failure."""
    state = ensure_app_state(main.app)
    previous = state.room_members
    try:
        yield main.app
    finally:
        state.room_members = previous


def _request(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


def _client(*user_ids: str, rooms: tuple[str, ...] = (ROOM,)) -> SimpleNamespace:
    """A Matrix client joined to ``rooms`` that answers ``user_ids`` as the members of ``ROOM``."""
    members = [nio.RoomMember(user_id, None, None) for user_id in user_ids]
    return SimpleNamespace(
        rooms={room_id: object() for room_id in rooms},
        joined_members=AsyncMock(return_value=nio.JoinedMembersResponse(members, ROOM)),
    )


def _bot(client: SimpleNamespace | None, *, running: bool = True) -> SimpleNamespace:
    return SimpleNamespace(client=client, running=running)


def _bound(client: SimpleNamespace) -> FastAPI:
    """An app whose reader asks only a running router bot with ``client``."""
    app = FastAPI()
    _bind_room_members(app, {"router": _bot(client)})  # type: ignore[dict-item]
    return app


@pytest.mark.asyncio
async def test_member_passes() -> None:
    """A joined user passes, and the router client is asked about that room."""
    client = _client("@alice:example.org", "@bob:example.org")
    await require_room_member(_request(_bound(client)), ROOM, "@alice:example.org")
    client.joined_members.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
async def test_non_member_403() -> None:
    """A user who is not joined gets 403."""
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(_bound(_client("@bob:example.org"))), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (403, "Not a member of this room.")


@pytest.mark.asyncio
async def test_unbound_503() -> None:
    """Without a bound reader the answer is 503."""
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(FastAPI()), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")


@pytest.mark.asyncio
async def test_joined_members_error_503() -> None:
    """A non-JoinedMembersResponse from the client is 503, never a pass."""
    client = _client()
    client.joined_members = AsyncMock(return_value=nio.JoinedMembersError("boom"))
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(_bound(client)), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")


@pytest.mark.asyncio
async def test_joined_members_raising_503() -> None:
    """A Matrix call that raises is 503 like an error response, never a pass and never a bare 500."""
    client = _client()
    client.joined_members = AsyncMock(side_effect=TimeoutError("homeserver timed out"))
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(_bound(client)), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")


@pytest.mark.asyncio
async def test_router_in_room_is_asked_first() -> None:
    """The router answers when it is in the room, even when an agent bot is in it too."""
    router = _client("@alice:example.org")
    agent = _client("@alice:example.org")
    app = FastAPI()
    _bind_room_members(app, {"general": _bot(agent), "router": _bot(router)})  # type: ignore[dict-item]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    router.joined_members.assert_awaited_once_with(ROOM)
    agent.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_bot_in_room_answers_without_the_router() -> None:
    """In a room the router is not in, such as a personal room or a DM, a running agent bot in it answers."""
    router = _client("@alice:example.org", rooms=(OTHER_ROOM,))
    stopped = _client("@alice:example.org")
    agent = _client("@alice:example.org")
    app = FastAPI()
    bots = {"router": _bot(router), "stopped": _bot(stopped, running=False), "assistant": _bot(agent)}
    _bind_room_members(app, bots)  # type: ignore[arg-type]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    agent.joined_members.assert_awaited_once_with(ROOM)
    router.joined_members.assert_not_awaited()
    stopped.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_bot_answers_while_the_router_is_down() -> None:
    """A stopped router, or one without a client, leaves the room to a running agent bot in it."""
    agent = _client("@alice:example.org")
    app = FastAPI()
    bots = {"router": _bot(_client("@alice:example.org"), running=False), "assistant": _bot(agent)}
    _bind_room_members(app, bots)  # type: ignore[arg-type]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    bots["router"] = _bot(None)
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    assert agent.joined_members.await_count == 2


@pytest.mark.asyncio
async def test_no_bot_in_room_503() -> None:
    """When no running bot is in the room the answer is 503, and no bot is asked about a room it is not in."""
    router = _client("@alice:example.org", rooms=(OTHER_ROOM,))
    agent = _client("@alice:example.org", rooms=())
    app = FastAPI()
    _bind_room_members(app, {"router": _bot(router), "assistant": _bot(agent)})  # type: ignore[dict-item]
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")
    router.joined_members.assert_not_awaited()
    agent.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_bots_503(main_app: FastAPI) -> None:
    """A bot that stopped after binding is not asked, and with no bot running the reader is withdrawn."""
    app = main_app
    router = _bot(_client("@alice:example.org"))
    bots = {"router": router}
    bind_room_members_if_ready(bots, api_enabled=True)  # type: ignore[arg-type]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    router.running = False
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@alice:example.org")
    assert caught.value.status_code == 503
    bind_room_members_if_ready(bots, api_enabled=True)  # type: ignore[arg-type]
    assert ensure_app_state(app).room_members is None


@pytest.mark.asyncio
async def test_restarted_bot_uses_its_new_client(main_app: FastAPI) -> None:
    """A bot that got a new client in place, or was replaced in the mapping, is read through its new client."""
    app = main_app
    old = _client("@alice:example.org")
    new = _client("@bob:example.org")
    agent = _bot(old)
    bots = {"router": _bot(_client(rooms=())), "assistant": agent}
    bind_room_members_if_ready(bots, api_enabled=True)  # type: ignore[arg-type]
    agent.client = new
    await require_room_member(_request(app), ROOM, "@bob:example.org")
    new.joined_members.assert_awaited_once_with(ROOM)
    old.joined_members.assert_not_awaited()
    replacement = _client("@carol:example.org")
    bots["assistant"] = _bot(replacement)
    await require_room_member(_request(app), ROOM, "@carol:example.org")
    replacement.joined_members.assert_awaited_once_with(ROOM)
    bots["assistant"].client = None
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@carol:example.org")
    assert caught.value.status_code == 503


def test_bind_then_unbind_clears() -> None:
    """Unbinding removes the reader."""
    app = _bound(_client())
    assert ensure_app_state(app).room_members is not None
    _unbind_room_members(app)
    assert ensure_app_state(app).room_members is None
