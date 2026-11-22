"""Tests for classifying Matrix state-event errors by HTTP status."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.matrix.state_failures import StateFailure, state_failure

if TYPE_CHECKING:
    from aioresponses import aioresponses

_ROOM_ID = "!room:localhost"
_STATE_URL = re.compile(r"^http://localhost:8008/_matrix/client/v3/rooms/[^/]+/state/")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            nio.RoomGetStateEventError("forbidden", "M_FORBIDDEN", room_id=_ROOM_ID, http_status=403),
            StateFailure.REFUSED,
        ),
        (nio.RoomGetStateEventError("html 403", None, room_id=_ROOM_ID, http_status=403), StateFailure.REFUSED),
        (
            nio.RoomGetStateEventError("not found", "M_NOT_FOUND", room_id=_ROOM_ID, http_status=404),
            StateFailure.MISSING,
        ),
        (
            nio.RoomGetStateEventError("proxy 503", "M_NOT_FOUND", room_id=_ROOM_ID, http_status=503),
            StateFailure.TRANSIENT,
        ),
        (
            nio.RoomGetStateEventError("proxy 503", "M_FORBIDDEN", room_id=_ROOM_ID, http_status=503),
            StateFailure.TRANSIENT,
        ),
        (nio.RoomPutStateError("forbidden", "M_FORBIDDEN", room_id=_ROOM_ID, http_status=403), StateFailure.REFUSED),
        (nio.RoomPutStateError("not found", "M_NOT_FOUND", room_id=_ROOM_ID, http_status=404), StateFailure.TRANSIENT),
        (nio.RoomPutStateError("proxy 503", "M_FORBIDDEN", room_id=_ROOM_ID, http_status=503), StateFailure.TRANSIENT),
        (nio.RoomGetStateEventError("absent", "M_NOT_FOUND", room_id=_ROOM_ID), StateFailure.MISSING),
        (nio.RoomGetStateEventError("forbidden", "M_FORBIDDEN", room_id=_ROOM_ID), StateFailure.REFUSED),
        (nio.RoomPutStateError("forbidden", "M_FORBIDDEN", room_id=_ROOM_ID), StateFailure.REFUSED),
        (nio.RoomPutStateError("not found", "M_NOT_FOUND", room_id=_ROOM_ID), StateFailure.TRANSIENT),
        (
            nio.RoomGetStateEventError("slow down", "M_LIMIT_EXCEEDED", room_id=_ROOM_ID, http_status=429),
            StateFailure.TRANSIENT,
        ),
    ],
    ids=[
        "get-403",
        "get-403-no-errcode",
        "get-404",
        "get-503-m-not-found",
        "get-503-m-forbidden",
        "put-403",
        "put-404",
        "put-503-m-forbidden",
        "hand-built-get-m-not-found",
        "hand-built-get-m-forbidden",
        "hand-built-put-m-forbidden",
        "hand-built-put-m-not-found",
        "get-429",
    ],
)
def test_state_failure_classifies_by_http_status(response: nio.ErrorResponse, expected: StateFailure) -> None:
    """HTTP status decides refusal/missing/transient; hand-built errors fall back to errcode."""
    assert state_failure(response) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "status", "kwargs", "expected"),
    [
        ("GET", 503, {"payload": {"errcode": "M_NOT_FOUND", "error": "Service unavailable"}}, StateFailure.TRANSIENT),
        ("GET", 503, {"payload": {"errcode": "M_FORBIDDEN", "error": "Service unavailable"}}, StateFailure.TRANSIENT),
        ("GET", 403, {"body": "<html>Forbidden</html>", "content_type": "text/html"}, StateFailure.REFUSED),
        ("GET", 404, {"payload": {"errcode": "M_NOT_FOUND", "error": "Not found"}}, StateFailure.MISSING),
        ("PUT", 503, {"payload": {"errcode": "M_FORBIDDEN", "error": "Service unavailable"}}, StateFailure.TRANSIENT),
        ("PUT", 403, {"payload": {"errcode": "M_FORBIDDEN", "error": "Forbidden"}}, StateFailure.REFUSED),
    ],
)
async def test_state_failure_uses_nio_parsed_http_status(
    aioresponse: aioresponses,
    method: str,
    status: int,
    kwargs: dict[str, object],
    expected: StateFailure,
) -> None:
    """Real nio HTTP parsing populates http_status so state_failure classifies every error response."""
    client = nio.AsyncClient("http://localhost:8008", "@bot:localhost")
    client.access_token = "token"  # noqa: S105
    try:
        if method == "GET":
            aioresponse.get(_STATE_URL, status=status, **kwargs)
            response = await client.room_get_state_event(_ROOM_ID, "m.room.encryption")
            assert isinstance(response, nio.RoomGetStateEventError)
        else:
            aioresponse.put(_STATE_URL, status=status, **kwargs)
            response = await client.room_put_state(_ROOM_ID, "com.example.state", {"a": 1})
            assert isinstance(response, nio.RoomPutStateError)
    finally:
        await client.close()

    assert response.http_status == status
    assert state_failure(response) is expected
