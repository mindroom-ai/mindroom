"""Test helpers for scheduled-task state written by MindRoom bot accounts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlsplit

import nio

from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.state import MatrixState

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

SCHEDULE_WRITER_ID = "@mindroom_router:server"
SCHEDULED_TASK_EVENT_TYPE = "com.mindroom.scheduled.task"
_STATE_EVENT_PATH_PREFIX = "/_matrix/client/v3/rooms/"


def persist_schedule_writer(
    runtime_paths: RuntimePaths,
    writer_id: str = SCHEDULE_WRITER_ID,
    *,
    entity_name: str = ROUTER_AGENT_NAME,
) -> None:
    """Persist one managed bot account as ``writer_id`` so the scheduler trusts its task state."""
    matrix_id = MatrixID.parse(writer_id)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account(managed_account_key(entity_name), matrix_id.username, "test-password", domain=matrix_id.domain)
    state.save(runtime_paths=runtime_paths)


def schedule_runtime_paths(tmp_path: Path, writer_id: str = SCHEDULE_WRITER_ID) -> RuntimePaths:
    """Return isolated runtime paths whose persisted router account writes scheduled-task state."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    persist_schedule_writer(runtime_paths, writer_id)
    return runtime_paths


def scheduled_task_state_event(
    task_id: str,
    content: dict[str, Any],
    *,
    room_id: str = "!test:server",
    sender: str = SCHEDULE_WRITER_ID,
) -> dict[str, Any]:
    """Return one scheduled-task state event as full room-state reads report it."""
    return {
        "type": SCHEDULED_TASK_EVENT_TYPE,
        "state_key": task_id,
        "content": content,
        "sender": sender,
        "event_id": f"$state_{task_id}",
        "origin_server_ts": 1234567890,
        "room_id": room_id,
    }


def room_create_state_response(room_id: str, *, full_event: bool) -> nio.RoomGetStateEventResponse:
    """Answer the scheduler's homeserver check on a room's create event as a server honouring ``format=event``."""
    content = {"room_version": "11"}
    body = (
        {
            "type": "m.room.create",
            "state_key": "",
            "content": content,
            "sender": SCHEDULE_WRITER_ID,
            "event_id": "$create",
            "origin_server_ts": 1,
            "room_id": room_id,
        }
        if full_event
        else content
    )
    return nio.RoomGetStateEventResponse(body, "m.room.create", "", room_id)


def joined_member_state(room_id: str, event_type: str, state_key: str = "") -> nio.RoomGetStateEventResponse:
    """Report every requested member as joined, for runners that check creator membership."""
    assert event_type == "m.room.member"
    return nio.RoomGetStateEventResponse({"membership": "join"}, event_type, state_key, room_id)


def serve_task_state_events(client: Any, *, sender: str = SCHEDULE_WRITER_ID) -> AsyncMock:  # noqa: ANN401
    """Answer the scheduler's ``format=event`` task reads from the client's ``room_get_state_event`` mock.

    Homeservers return the whole state event for ``format=event``.
    This wraps each mocked state content in that envelope with ``sender``, so the existing
    ``room_get_state_event`` mock stays the single source of room state and its call count
    still measures state-event requests.
    The scheduler's check of the room's create event is answered as a homeserver honouring ``format=event``.
    Other private sends fall through to the previous mock.
    """
    fallback = client._send

    async def send(response_class: type, method: str, path: str, *args: object, **kwargs: object) -> object:
        url = urlsplit(path)
        full_event = parse_qs(url.query) == {"format": ["event"]}
        parts = [unquote(part) for part in url.path.removeprefix(_STATE_EVENT_PATH_PREFIX).split("/")]
        if url.path.startswith(_STATE_EVENT_PATH_PREFIX) and parts[1:] == ["state", "m.room.create"]:
            return room_create_state_response(parts[0], full_event=full_event)
        if not full_event or not url.path.startswith(_STATE_EVENT_PATH_PREFIX):
            return await fallback(response_class, method, path, *args, **kwargs)
        assert response_class is nio.RoomGetStateEventResponse
        assert method == "GET"
        room_id, state, event_type, state_key = (
            unquote(part) for part in url.path.removeprefix(_STATE_EVENT_PATH_PREFIX).split("/")
        )
        assert state == "state"
        response = await client.room_get_state_event(room_id=room_id, event_type=event_type, state_key=state_key)
        if not isinstance(response, nio.RoomGetStateEventResponse):
            return response
        event = scheduled_task_state_event(state_key, response.content, room_id=room_id, sender=sender)
        return nio.RoomGetStateEventResponse(event, event_type, state_key, room_id)

    client._send = AsyncMock(side_effect=send)
    return client._send
