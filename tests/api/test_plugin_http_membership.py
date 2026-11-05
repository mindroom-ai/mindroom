"""Plugin routes can require live room membership, read through the router bot's client."""

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


def _request(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


def _router_client(*user_ids: str) -> SimpleNamespace:
    members = [nio.RoomMember(user_id, None, None) for user_id in user_ids]
    return SimpleNamespace(joined_members=AsyncMock(return_value=nio.JoinedMembersResponse(members, ROOM)))


@pytest.mark.asyncio
async def test_member_passes() -> None:
    """A joined user passes, and the router client is asked about that room."""
    app = FastAPI()
    client = _router_client("@alice:example.org", "@bob:example.org")
    _bind_room_members(app, client)  # type: ignore[arg-type]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    client.joined_members.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
async def test_non_member_403() -> None:
    """A user who is not joined gets 403."""
    app = FastAPI()
    _bind_room_members(app, _router_client("@bob:example.org"))  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@alice:example.org")
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
    app = FastAPI()
    client = SimpleNamespace(joined_members=AsyncMock(return_value=nio.JoinedMembersError("boom")))
    _bind_room_members(app, client)  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")


@pytest.mark.asyncio
async def test_stopped_router_503() -> None:
    """A router bot that stopped after binding is not asked."""
    app = main.app
    client = _router_client("@alice:example.org")
    router = SimpleNamespace(client=client, running=True)
    bind_room_members_if_ready({"router": router}, api_enabled=True)  # type: ignore[arg-type]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    router.running = False
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@alice:example.org")
    assert caught.value.status_code == 503
    bind_room_members_if_ready({"router": router}, api_enabled=True)  # type: ignore[arg-type]
    assert ensure_app_state(app).room_members is None
    _unbind_room_members(app)


@pytest.mark.asyncio
async def test_restarted_router_uses_its_new_client() -> None:
    """A router bot that got a new client in place is read through that client, not the old one."""
    app = main.app
    old = _router_client("@alice:example.org")
    new = _router_client("@bob:example.org")
    router = SimpleNamespace(client=old, running=True)
    bind_room_members_if_ready({"router": router}, api_enabled=True)  # type: ignore[arg-type]
    router.client = new
    await require_room_member(_request(app), ROOM, "@bob:example.org")
    new.joined_members.assert_awaited_once_with(ROOM)
    old.joined_members.assert_not_awaited()
    router.client = None
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@bob:example.org")
    assert caught.value.status_code == 503
    _unbind_room_members(app)


def test_bind_then_unbind_clears() -> None:
    """Unbinding removes the reader."""
    app = FastAPI()
    _bind_room_members(app, _router_client())  # type: ignore[arg-type]
    assert ensure_app_state(app).room_members is not None
    _unbind_room_members(app)
    assert ensure_app_state(app).room_members is None
