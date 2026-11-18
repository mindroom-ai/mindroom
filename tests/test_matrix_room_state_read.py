"""A room's full state reads without nio's JSON schema validation, keeping nio's answers for bad bodies."""

from __future__ import annotations

import nio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from mindroom.matrix.client_session import MindRoomAsyncClient


def _state_event(state_key: str) -> dict[str, object]:
    return {
        "type": "com.example.tag",
        "state_key": state_key,
        "sender": "@user:localhost",
        "event_id": f"$event-{state_key}",
        "origin_server_ts": 1,
        "content": {"value": state_key},
    }


def _without(field: str) -> dict[str, object]:
    event = _state_event(field)
    del event[field]
    return event


async def test_room_state_drops_malformed_events_and_answers_bad_bodies_with_errors() -> None:
    """Well-formed events pass, entries missing a required field are dropped, and a non-list body is an error."""
    good = _state_event("good")
    malformed = [
        "not an event",
        _without("type"),
        _without("state_key"),
        _without("sender"),
        _without("event_id"),
        {**_state_event("time"), "origin_server_ts": "1"},
        {**_state_event("key"), "state_key": 7},
    ]
    bodies: dict[str, tuple[int, object]] = {
        "!ok:localhost": (200, [good, *malformed]),
        "!denied:localhost": (403, {"errcode": "M_FORBIDDEN", "error": "You are not in this room."}),
        "!odd:localhost": (200, {"events": [good]}),
    }

    async def room_state(request: web.Request) -> web.Response:
        status, body = bodies[request.match_info["room_id"]]
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_get("/_matrix/client/{version}/rooms/{room_id}/state", room_state)
    async with TestServer(app) as server:
        client = MindRoomAsyncClient(str(server.make_url("")), "@bot:localhost")
        try:
            with pytest.raises(nio.LocalProtocolError):
                await client.room_get_state("!ok:localhost")
            client.access_token = "token"  # noqa: S105
            ok = await client.room_get_state("!ok:localhost")
            denied = await client.room_get_state("!denied:localhost")
            odd = await client.room_get_state("!odd:localhost")
        finally:
            await client.close()

    assert isinstance(ok, nio.RoomGetStateResponse)
    assert (ok.room_id, ok.events) == ("!ok:localhost", [good])
    assert isinstance(denied, nio.RoomGetStateError)
    assert denied.status_code == "M_FORBIDDEN"
    assert isinstance(odd, nio.RoomGetStateError)
