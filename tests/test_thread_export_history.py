"""Tests for thread export's own direct Matrix pagination path.

These exercise ``fetch_exported_thread_history`` against a fake homeserver rather than through the
export writer, because the reconstruction rules -- how far the walk goes, which revision wins, what
a redaction leaves behind -- are this module's behaviour and nothing above it can observe them
without also observing YAML serialization.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import nio
import pytest

from mindroom.thread_export.history import ThreadExportHistoryError, fetch_exported_thread_history
from tests.event_cache_test_support import raw_nio_event

if TYPE_CHECKING:
    from collections.abc import Sequence

_ROOM_ID = "!room:localhost"
_ROOT_ID = "$root:localhost"
_ALICE = "@alice:localhost"
_SIDECAR_TEXT = "the whole long message, all of it, well past any preview"


def _text_event(
    event_id: str,
    body: str,
    *,
    timestamp: int,
    thread_root_id: str | None = None,
    sender: str = _ALICE,
    unsigned: dict[str, Any] | None = None,
) -> nio.Event:
    """Return one plain room message, optionally threaded under a root."""
    content: dict[str, Any] = {"body": body, "msgtype": "m.text"}
    if thread_root_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root_id}
    source: dict[str, Any] = {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": timestamp,
        "room_id": _ROOM_ID,
        "type": "m.room.message",
        "content": content,
    }
    if unsigned is not None:
        source["unsigned"] = unsigned
    return nio.RoomMessageText.from_dict(source)


def _edit_event(
    event_id: str,
    original_event_id: str,
    *,
    body: str,
    timestamp: int,
    sender: str = _ALICE,
) -> nio.Event:
    """Return one ``m.replace`` of an event inside the exported thread."""
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
            "content": {
                "body": f"* {body}",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
                "m.new_content": {
                    "body": body,
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": _ROOT_ID},
                },
            },
        },
    )


def _redacted_event(event_id: str, *, timestamp: int) -> nio.Event:
    """Return the stripped shell a homeserver serves for a redacted message."""
    return nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": _ALICE,
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
            "content": {},
            "unsigned": {
                "redacted_because": {
                    "event_id": "$redaction:localhost",
                    "sender": _ALICE,
                    "origin_server_ts": timestamp + 1,
                    "room_id": _ROOM_ID,
                    "type": "m.room.redaction",
                    "redacts": event_id,
                    "content": {"reason": "oops"},
                },
            },
        },
    )


def _sidecar_event(event_id: str, *, timestamp: int) -> nio.Event:
    """Return one message whose visible body is only a preview of an MXC sidecar."""
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": _ALICE,
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
            "content": {
                "body": "the whole long mes…",
                "msgtype": "m.text",
                "url": "mxc://localhost/abc123",
                "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
                "m.relates_to": {"rel_type": "m.thread", "event_id": _ROOT_ID},
            },
        },
    )


def _opaque_event(event_id: str, *, timestamp: int, replies_to: str) -> nio.Event:
    """Return ciphertext this client cannot decrypt whose relation is still exposed."""
    return raw_nio_event(
        {
            "event_id": event_id,
            "sender": _ALICE,
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.encrypted",
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "opaque",
                "device_id": "DEVICE",
                "sender_key": "sender-key",
                "session_id": "session",
                "m.relates_to": {"rel_type": "m.thread", "event_id": replies_to},
            },
        },
    )


def _fake_client(pages: Sequence[tuple[list[nio.Event], str | None]]) -> Mock:
    """Return a client serving fixed ``/messages`` pages, repeating the last one forever.

    Repeating rather than exhausting is deliberate: a walk with no stopping rule would hang the
    test suite instead of failing it, so the fake never lies about having more history.
    """
    responses = [
        nio.RoomMessagesResponse(room_id=_ROOM_ID, chunk=chunk, start="start", end=end) for chunk, end in pages
    ]
    served = 0

    async def room_messages(*_args: object, **_kwargs: object) -> nio.RoomMessagesResponse:
        nonlocal served
        response = responses[min(served, len(responses) - 1)]
        served += 1
        return response

    client = Mock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_general:localhost"
    client.room_messages = AsyncMock(side_effect=room_messages)
    client.download = AsyncMock(
        return_value=nio.DownloadResponse(
            body=json.dumps({"body": _SIDECAR_TEXT, "msgtype": "m.text"}).encode(),
            content_type="application/json",
            filename=None,
        ),
    )
    return client


async def _history(client: Mock) -> list[dict[str, Any]]:
    """Run the export read and return its messages in serialized form."""
    messages = await fetch_exported_thread_history(client, room_id=_ROOM_ID, thread_id=_ROOT_ID)
    return [message.to_dict() for message in messages]


@pytest.mark.asyncio
async def test_walk_spans_every_page_up_to_the_root_and_stops_there() -> None:
    """A thread split across pages exports whole, and the walk ends at the root it came for."""
    client = _fake_client(
        [
            ([_text_event("$c:localhost", "third", timestamp=300, thread_root_id=_ROOT_ID)], "token-1"),
            ([_text_event("$b:localhost", "second", timestamp=200, thread_root_id=_ROOT_ID)], "token-2"),
            ([_text_event(_ROOT_ID, "root", timestamp=100)], "token-3"),
        ],
    )

    history = await _history(client)

    assert [message["event_id"] for message in history] == [_ROOT_ID, "$b:localhost", "$c:localhost"]
    assert [message["body"] for message in history] == ["root", "second", "third"]
    # The third page carried the root and still offered a continuation token; the walk is done
    # regardless, and paging past the root would re-read the room for nothing.
    assert client.room_messages.await_count == 3


@pytest.mark.asyncio
async def test_an_edited_message_exports_its_current_revision() -> None:
    """The newest replacement is the exported body; the original keeps its place in the thread."""
    client = _fake_client(
        [
            (
                [
                    _edit_event("$edit-2:localhost", "$b:localhost", body="v3", timestamp=500),
                    _edit_event("$edit-1:localhost", "$b:localhost", body="v2", timestamp=400),
                    _text_event("$b:localhost", "v1", timestamp=200, thread_root_id=_ROOT_ID),
                    _text_event(_ROOT_ID, "root", timestamp=100),
                ],
                None,
            ),
        ],
    )

    history = await _history(client)

    assert [message["event_id"] for message in history] == [_ROOT_ID, "$b:localhost"]
    edited = history[1]
    assert edited["body"] == "v3"
    assert edited["latest_event_id"] == "$edit-2:localhost"
    # The ordering key stays the original's, and the edit's own time is reported separately.
    assert edited["timestamp"] == 200
    assert edited["edited_timestamp"] == 500
    # No standalone edit event is exported in its own right.
    assert "$edit-1:localhost" not in {message["event_id"] for message in history}
    assert "$edit-2:localhost" not in {message["event_id"] for message in history}


@pytest.mark.asyncio
async def test_a_replacement_from_another_sender_is_not_an_edit() -> None:
    """A thread is rebuilt from raw timeline events, so the author rule is applied here or nowhere."""
    client = _fake_client(
        [
            (
                [
                    _edit_event(
                        "$edit:localhost",
                        "$b:localhost",
                        body="hijacked",
                        timestamp=400,
                        sender="@mallory:localhost",
                    ),
                    _text_event("$b:localhost", "second", timestamp=200, thread_root_id=_ROOT_ID),
                    _text_event(_ROOT_ID, "root", timestamp=100),
                ],
                None,
            ),
        ],
    )

    history = await _history(client)

    # Neither applied as a revision, nor smuggled in as a message of its own.
    assert [message["event_id"] for message in history] == [_ROOT_ID, "$b:localhost"]
    assert history[1]["body"] == "second"
    assert history[1]["latest_event_id"] == "$b:localhost"
    assert "edited_timestamp" not in history[1]


@pytest.mark.asyncio
async def test_a_bundled_replacement_is_applied_when_the_edit_event_is_gone() -> None:
    """The MindRoom homeserver forks purge superseded replacements, leaving only the aggregation."""
    client = _fake_client(
        [
            (
                [
                    _text_event(
                        "$b:localhost",
                        "stale preview",
                        timestamp=200,
                        thread_root_id=_ROOT_ID,
                        unsigned={
                            "m.relations": {
                                "m.replace": {
                                    "event": {
                                        "event_id": "$edit:localhost",
                                        "sender": _ALICE,
                                        "origin_server_ts": 400,
                                        "room_id": _ROOM_ID,
                                        "type": "m.room.message",
                                        "content": {
                                            "body": "* current revision",
                                            "msgtype": "m.text",
                                            "m.relates_to": {
                                                "rel_type": "m.replace",
                                                "event_id": "$b:localhost",
                                            },
                                            "m.new_content": {
                                                "body": "current revision",
                                                "msgtype": "m.text",
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    ),
                    _text_event(_ROOT_ID, "root", timestamp=100),
                ],
                None,
            ),
        ],
    )

    history = await _history(client)

    assert history[1]["body"] == "current revision"
    assert history[1]["latest_event_id"] == "$edit:localhost"


@pytest.mark.asyncio
async def test_a_redacted_reply_leaves_the_export_and_a_redacted_root_keeps_its_place() -> None:
    """Redaction removes content, and the two positions differ in what removal can mean.

    A redacted reply has lost the relation that put it in the thread, so it is no longer a member
    of one and drops out. A redacted root is still the thing the export was asked for, so it keeps
    its position and carries the empty body the homeserver now serves -- exporting a placeholder
    rather than silently renumbering the conversation around a hole.
    """
    redacted_reply_client = _fake_client(
        [
            (
                [
                    _text_event("$c:localhost", "third", timestamp=300, thread_root_id=_ROOT_ID),
                    _redacted_event("$b:localhost", timestamp=200),
                    _text_event(_ROOT_ID, "root", timestamp=100),
                ],
                None,
            ),
        ],
    )

    reply_history = await _history(redacted_reply_client)

    assert [message["event_id"] for message in reply_history] == [_ROOT_ID, "$c:localhost"]
    assert [message["body"] for message in reply_history] == ["root", "third"]

    redacted_root_client = _fake_client(
        [
            (
                [
                    _text_event("$c:localhost", "third", timestamp=300, thread_root_id=_ROOT_ID),
                    _redacted_event(_ROOT_ID, timestamp=100),
                ],
                None,
            ),
        ],
    )

    root_history = await _history(redacted_root_client)

    assert [message["event_id"] for message in root_history] == [_ROOT_ID, "$c:localhost"]
    assert root_history[0]["body"] == ""


@pytest.mark.asyncio
async def test_a_repeated_pagination_token_stops_the_walk_instead_of_spinning() -> None:
    """A homeserver handing back a token it already gave has stopped making progress."""
    client = _fake_client(
        [
            (
                [_text_event("$c:localhost", "third", timestamp=300, thread_root_id=_ROOT_ID)],
                "stuck-token",
            ),
        ],
    )

    with pytest.raises(ThreadExportHistoryError, match="repeated pagination token"):
        await _history(client)

    # One request established the token, the second proved it had not moved. Anything higher means
    # the guard is counting rather than detecting, and an unbounded walk is what it exists to stop.
    assert client.room_messages.await_count == 2


@pytest.mark.asyncio
async def test_an_empty_page_with_a_token_is_exhaustion_rather_than_a_loop() -> None:
    """The mirror image of the guard above, and the shape a real homeserver produces.

    A server at the start of its visible history answers with an empty chunk and, on some
    implementations, the same token it was given. Reading that as a stuck server would turn an
    ordinary short room into a permanent export failure of the wrong kind, so exhaustion is checked
    before the token is examined at all. The thread still fails -- its root was never found -- and
    that is the honest reason to report.
    """
    client = _fake_client(
        [
            ([_text_event("$c:localhost", "third", timestamp=300, thread_root_id=_ROOT_ID)], "same-token"),
            ([], "same-token"),
        ],
    )

    with pytest.raises(ThreadExportHistoryError, match="not found during room scan"):
        await _history(client)


@pytest.mark.asyncio
async def test_a_long_text_sidecar_is_resolved_from_the_homeserver() -> None:
    """Export writes the message, not its preview, and has no cache to resolve it from."""
    client = _fake_client(
        [
            (
                [
                    _sidecar_event("$b:localhost", timestamp=200),
                    _text_event(_ROOT_ID, "root", timestamp=100),
                ],
                None,
            ),
        ],
    )

    history = await _history(client)

    assert history[1]["body"] == _SIDECAR_TEXT
    client.download.assert_awaited_once_with(mxc="mxc://localhost/abc123")


@pytest.mark.asyncio
async def test_undecryptable_thread_content_fails_the_thread_rather_than_exporting_around_it() -> None:
    """An export that quietly omits ciphertext it could not read looks complete and is not."""
    client = _fake_client(
        [
            (
                [
                    _opaque_event("$enc:localhost", timestamp=200, replies_to=_ROOT_ID),
                    _text_event(_ROOT_ID, "root", timestamp=100),
                ],
                None,
            ),
        ],
    )

    with pytest.raises(ThreadExportHistoryError, match="still-undecryptable encrypted events"):
        await _history(client)


@pytest.mark.asyncio
async def test_a_failed_pagination_request_fails_the_thread() -> None:
    """A partial walk must not be reported as a whole thread."""
    client = Mock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_general:localhost"
    client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesError.from_dict(
            {"errcode": "M_FORBIDDEN", "error": "nope"},
            _ROOM_ID,
        ),
    )

    with pytest.raises(ThreadExportHistoryError, match="room scan failed"):
        await _history(client)
