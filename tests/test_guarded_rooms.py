"""A guarded room takes sends from its task context only inside an allowed scope, and every Matrix send checks it."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.matrix.client_delivery import edit_message_result, send_message_result, send_room_event_result
from mindroom.matrix.guarded_rooms import allowed_send, guarded_room
from mindroom.matrix.message_builder import build_reaction_content
from tests.conftest import make_matrix_client_mock

GUARDED = "!contact:localhost"
OTHER = "!other:localhost"


def _client() -> AsyncMock:
    client = make_matrix_client_mock()
    client.room_send = AsyncMock(
        side_effect=lambda **kwargs: nio.RoomSendResponse(f"$sent-{kwargs['room_id']}", kwargs["room_id"]),
    )
    return client


async def _send(client: AsyncMock, room_id: str) -> str | None:
    delivered = await send_message_result(client, room_id, {"msgtype": "m.text", "body": "Hi"})
    return delivered.event_id if delivered is not None else None


@pytest.mark.asyncio
async def test_a_guarded_room_takes_sends_only_inside_an_allowed_scope() -> None:
    """A refused send fails like any other and logs the room; other rooms and later contexts are unaffected."""
    client = _client()

    with guarded_room(GUARDED):
        with capture_logs() as logs:
            refused = await _send(client, GUARDED)
        with allowed_send():
            allowed = await _send(client, GUARDED)
        other = await _send(client, OTHER)
    after = await _send(client, GUARDED)

    assert (refused, allowed, other, after) == (None, f"$sent-{GUARDED}", f"$sent-{OTHER}", f"$sent-{GUARDED}")
    assert [call.kwargs["room_id"] for call in client.room_send.await_args_list] == [GUARDED, OTHER, GUARDED]
    assert [(log["event"], log["room_id"]) for log in logs] == [("matrix_send_refused_in_guarded_room", GUARDED)]


@pytest.mark.asyncio
async def test_edits_reactions_and_tasks_started_in_a_guarded_context_are_refused_too() -> None:
    """Edits and reactions share the one send path, and a task inherits the guard of the context that started it."""
    client = _client()

    with guarded_room(GUARDED):
        edited = await edit_message_result(client, GUARDED, "$original", {"msgtype": "m.text", "body": "Hi"}, "Hi")
        reacted = await send_room_event_result(client, GUARDED, "m.reaction", build_reaction_content("$original", "👍"))
        from_task = await asyncio.create_task(_send(client, GUARDED))

    assert (edited, reacted, from_task) == (None, None, None)
    client.room_send.assert_not_awaited()
