"""Whether the homeserver applied a redaction, asked before persisted history is destroyed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.main import Config
from mindroom.matrix.redaction_proof import homeserver_applied_redaction

_ROOM_ID = "!room:localhost"
_EVENT_ID = "$target:localhost"


@dataclass
class _Runtime:
    client: Any
    config: Config


def _served(source: dict[str, object]) -> nio.RoomGetEventResponse:
    response = nio.RoomGetEventResponse()
    response.event = nio.Event.parse_event(source)
    return response


def _runtime(response: object) -> _Runtime:
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_get_event = AsyncMock(return_value=response)
    return _Runtime(client=client, config=Config())


_MESSAGE = {
    "type": "m.room.message",
    "event_id": _EVENT_ID,
    "sender": "@alice:localhost",
    "origin_server_ts": 1,
    "content": {"msgtype": "m.text", "body": "still here"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "applied"),
    [
        (
            _served(
                {
                    **_MESSAGE,
                    "content": {},
                    "unsigned": {"redacted_because": {"type": "m.room.redaction", "sender": "@alice:localhost"}},
                },
            ),
            True,
        ),
        (_served(_MESSAGE), False),
        (_served({**_MESSAGE, "event_id": "$other:localhost", "content": {}}), False),
        (nio.RoomGetEventError("not found", "M_NOT_FOUND"), False),
        (nio.RoomGetEventError("forbidden", "M_FORBIDDEN"), False),
    ],
    ids=["redacted", "intact", "wrong-event", "missing", "hidden"],
)
async def test_only_a_served_redacted_event_counts_as_applied(response: object, applied: bool) -> None:
    """The target must come back redacted; an intact or hidden target proves nothing was applied."""
    runtime = _runtime(response)

    assert await homeserver_applied_redaction(runtime, _ROOM_ID, _EVENT_ID) is applied
    runtime.client.room_get_event.assert_awaited_once_with(_ROOM_ID, _EVENT_ID)


@pytest.mark.asyncio
async def test_a_homeserver_that_cannot_answer_now_is_retried() -> None:
    """A transient failure raises instead of deciding either way."""
    runtime = _runtime(nio.RoomGetEventError("slow down", "M_LIMIT_EXCEEDED"))

    with pytest.raises(RuntimeError, match="confirm its redaction"):
        await homeserver_applied_redaction(runtime, _ROOM_ID, _EVENT_ID)
