"""Tests for classifying Matrix state-event errors by HTTP status."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.matrix.client_delivery import send_message_outcome
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.client_session import MindRoomAsyncClient, _MatrixTransportShutdownError
from mindroom.matrix.large_messages import upload_json_sidecar
from mindroom.matrix.state_failures import StateFailure, state_failure
from mindroom.message_target import MessageTarget
from mindroom.streaming import ProgressState, StreamingResponse
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

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
            nio.RoomGetStateEventError("consent", "M_CONSENT_NOT_GIVEN", room_id=_ROOM_ID, http_status=403),
            StateFailure.REFUSED,
        ),
        (
            nio.RoomGetStateEventError("no route", "M_UNRECOGNIZED", room_id=_ROOM_ID, http_status=404),
            StateFailure.TRANSIENT,
        ),
        (nio.RoomGetStateEventError("proxy 404", None, room_id=_ROOM_ID, http_status=404), StateFailure.TRANSIENT),
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
        "get-403-other-errcode",
        "get-404-m-unrecognized",
        "get-404-no-errcode",
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
    """HTTP status decides; a 404 is MISSING only with M_NOT_FOUND; hand-built errors fall back to errcode."""
    assert state_failure(response) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "status", "kwargs", "expected"),
    [
        ("GET", 503, {"payload": {"errcode": "M_NOT_FOUND", "error": "Service unavailable"}}, StateFailure.TRANSIENT),
        ("GET", 503, {"payload": {"errcode": "M_FORBIDDEN", "error": "Service unavailable"}}, StateFailure.TRANSIENT),
        ("GET", 403, {"body": "<html>Forbidden</html>", "content_type": "text/html"}, StateFailure.REFUSED),
        ("GET", 403, {"payload": {"errcode": "M_FORBIDDEN", "error": "Forbidden"}}, StateFailure.REFUSED),
        ("GET", 404, {"body": "<html>Not Found</html>", "content_type": "text/html"}, StateFailure.TRANSIENT),
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "expected_level"),
    [(502, "warning"), (503, "warning"), (504, "warning"), (500, "error"), (403, "error"), (None, "error")],
)
async def test_transient_homeserver_failures_log_warning_with_http_status(
    http_status: int | None,
    expected_level: str,
) -> None:
    """502/503/504 Matrix errors log warning with http_status; non-transient/unknown statuses stay error."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@bot:localhost"
    client.rooms = {_ROOM_ID: MagicMock(encrypted=False)}
    client.room_send = AsyncMock(
        return_value=nio.RoomSendError("unknown error", room_id=_ROOM_ID, http_status=http_status),
    )
    client.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsError("unknown error", http_status=http_status))
    client.upload = AsyncMock(return_value=(nio.UploadError("unknown error", http_status=http_status), None))

    with capture_logs() as logs:
        await send_message_outcome(client, _ROOM_ID, {"msgtype": "m.text", "body": "hi"})
        await send_message_outcome(
            client,
            _ROOM_ID,
            {"msgtype": "m.text", "body": "hi"},
            transient_failure_recoverable=True,
        )
        assert await get_joined_rooms(client) is None
        assert await upload_json_sidecar(client, _ROOM_ID, {"body": "x"}, room_encrypted=False, durable=False) == (
            None,
            None,
        )

    send_logs = [entry for entry in logs if entry["event"] == "matrix_message_send_failed"]
    assert [(entry["log_level"], entry["http_status"]) for entry in send_logs] == [
        ("error", http_status),
        (expected_level, http_status),
    ]
    for event in ("matrix_joined_rooms_fetch_failed", "large_message_sidecar_upload_failed"):
        matching = [entry for entry in logs if entry["event"] == event]
        assert len(matching) == 1
        assert matching[0]["log_level"] == expected_level
        assert matching[0]["http_status"] == http_status


@pytest.mark.asyncio
async def test_shutdown_fenced_delivery_exception_and_non_final_stream_edit_log_warning(
    tmp_path: Path,
) -> None:
    """Shutdown-fenced delivery exceptions and non-final stream edit failures log warning, not error."""
    client = MindRoomAsyncClient("http://127.0.0.1:9", "@bot:localhost", "BOT", store_path=None)
    client.begin_process_shutdown_transport_fence()
    client.rooms = {_ROOM_ID: MagicMock(encrypted=False)}
    client.room_send = AsyncMock(side_effect=_MatrixTransportShutdownError())  # type: ignore[method-assign]

    with capture_logs() as logs:
        await send_message_outcome(client, _ROOM_ID, {"msgtype": "m.text", "body": "hi"})
    exc_logs = [entry for entry in logs if entry["event"] == "matrix_message_delivery_exception"]
    assert len(exc_logs) == 1
    assert exc_logs[0]["log_level"] == "warning"

    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config(), runtime_paths)
    stream = StreamingResponse(
        target=MessageTarget.resolve(_ROOM_ID, None, "$src"),
        config=config,
        runtime_paths=runtime_paths,
        progress_write_ahead=AsyncMock(),
        progress_delivered=AsyncMock(),
    )
    stream.event_id = "$msg"
    edit_client = AsyncMock(spec=nio.AsyncClient)
    edit_client.user_id = "@bot:localhost"
    edit_client.rooms = {_ROOM_ID: MagicMock(encrypted=False)}
    edit_client.room_send = AsyncMock(
        return_value=nio.RoomSendError("unknown error", room_id=_ROOM_ID, http_status=502),
    )
    stream.terminal_edit = AsyncMock(return_value=None)
    progress = ProgressState(text="x", tool_trace=(), presentation_state=None, placeholder_only=False)
    with capture_logs() as edit_logs:
        await stream._send_content(
            edit_client,
            content={"msgtype": "m.text", "body": "x"},
            display_text="x",
            is_final=False,
            progress=progress,
        )
        await stream._send_content(
            edit_client,
            content={"msgtype": "m.text", "body": "x"},
            display_text="x",
            is_final=True,
            progress=progress,
        )
    send_edit_logs = [entry for entry in edit_logs if entry["event"] == "matrix_message_send_failed"]
    assert [(entry["log_level"], entry["http_status"]) for entry in send_edit_logs] == [("warning", 502)]
    failed_edits = [entry for entry in edit_logs if entry["event"] == "Failed to edit streaming message"]
    assert [entry["log_level"] for entry in failed_edits] == ["warning", "error"]
