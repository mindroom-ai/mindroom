"""Tests for the bulk thread-cache backfill scan."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.matrix.client_thread_history import (
    OpaqueEncryptedThreadHistoryError,
    bulk_refresh_room_thread_histories,
    fetch_thread_event_sources_via_room_messages,
    find_response_event_ids_via_room_messages,
    thread_ids_needing_refill,
)
from mindroom.matrix.thread_membership import ThreadRoomScanRootNotFoundError
from tests.event_cache_test_support import raw_nio_event, replace_thread_unconditionally

if TYPE_CHECKING:
    from mindroom.matrix.cache import ConversationEventCache

_ROOM_ID = "!room:localhost"


def _message_event(
    event_id: str,
    body: str,
    *,
    timestamp: int,
    thread_root_id: str | None = None,
    reply_to_event_id: str | None = None,
    is_falling_back: bool = False,
    sender: str = "@alice:localhost",
) -> nio.RoomMessageText:
    content: dict[str, object] = {"body": body, "msgtype": "m.text"}
    if thread_root_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root_id}
    if reply_to_event_id is not None:
        relation = content.setdefault("m.relates_to", {})
        assert isinstance(relation, dict)
        relation["m.in_reply_to"] = {"event_id": reply_to_event_id}
        if is_falling_back:
            relation["is_falling_back"] = True
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
            "content": content,
        },
    )


def _edit_event(
    event_id: str,
    original_event_id: str,
    *,
    timestamp: int,
    thread_root_id: str,
) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": "@alice:localhost",
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
            "content": {
                "body": "* edited reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
                "m.new_content": {
                    "body": "edited reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
                },
            },
        },
    )


def _messages_response(chunk: list[nio.Event], *, end: str | None) -> nio.RoomMessagesResponse:
    return nio.RoomMessagesResponse(room_id=_ROOM_ID, chunk=chunk, start="", end=end)


def _opaque_reply_event(event_id: str, *, replies_to: str, timestamp: int) -> nio.Event:
    """Return ciphertext this client could not decrypt, replying to an event the scan never saw.

    A client holding the megolm session decrypts the same event and resolves the relation, so
    whether this lands in ``unresolved_opaque_event_ids`` depends on which client ran the scan.
    """
    return raw_nio_event(
        {
            "event_id": event_id,
            "sender": "@alice:localhost",
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.encrypted",
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "opaque",
                "device_id": "DEVICE",
                "sender_key": "sender-key",
                "session_id": "session",
                "m.relates_to": {"m.in_reply_to": {"event_id": replies_to}},
            },
        },
    )


@pytest.mark.asyncio
async def test_response_recovery_scan_finds_exact_original_before_source() -> None:
    """The recovery scan finds the bot's original reply and ignores other senders and edits."""
    source_event_id = "$source:localhost"
    response_event_id = "$response:localhost"
    edit = _edit_event(
        "$response-edit:localhost",
        response_event_id,
        timestamp=4000,
        thread_root_id=source_event_id,
    )
    edit.source["sender"] = "@bot:localhost"
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [
                    edit,
                    _message_event(
                        "$other-reply:localhost",
                        "other reply",
                        timestamp=3000,
                        reply_to_event_id=source_event_id,
                    ),
                    _message_event(
                        response_event_id,
                        "Thinking...",
                        timestamp=2000,
                        reply_to_event_id=source_event_id,
                        sender="@bot:localhost",
                    ),
                ],
                end="page-2",
            ),
            _messages_response(
                [_message_event(source_event_id, "question", timestamp=1000)],
                end="unused-page",
            ),
        ],
    )

    response_event_ids = await find_response_event_ids_via_room_messages(
        client,
        _ROOM_ID,
        response_sender="@bot:localhost",
        source_event_ids=(source_event_id,),
    )

    assert response_event_ids == frozenset({response_event_id})
    assert client.room_messages.await_count == 2


@pytest.mark.asyncio
async def test_response_recovery_scan_finds_opaque_encrypted_reply() -> None:
    """The exposed reply relation recovers a bot response even without its Megolm key."""
    source_event_id = "$source:localhost"
    response_event_id = "$response:localhost"
    response_event = _opaque_reply_event(response_event_id, replies_to=source_event_id, timestamp=2000)
    response_event.source["sender"] = "@bot:localhost"
    client = AsyncMock()
    client.room_messages = AsyncMock(
        return_value=_messages_response(
            [response_event, _message_event(source_event_id, "question", timestamp=1000)],
            end=None,
        ),
    )

    response_event_ids = await find_response_event_ids_via_room_messages(
        client,
        _ROOM_ID,
        response_sender="@bot:localhost",
        source_event_ids=(source_event_id,),
    )

    assert response_event_ids == frozenset({response_event_id})


@pytest.mark.asyncio
async def test_response_recovery_scan_ignores_thread_fallback_relation() -> None:
    """A thread continuation is not the bot response merely because fallback targets the source."""
    source_event_id = "$source:localhost"
    client = AsyncMock()
    client.room_messages = AsyncMock(
        return_value=_messages_response(
            [
                _message_event(
                    "$scheduled:localhost",
                    "scheduled continuation",
                    timestamp=2000,
                    thread_root_id="$thread:localhost",
                    reply_to_event_id=source_event_id,
                    is_falling_back=True,
                    sender="@bot:localhost",
                ),
                _message_event(source_event_id, "question", timestamp=1000),
            ],
            end=None,
        ),
    )

    response_event_ids = await find_response_event_ids_via_room_messages(
        client,
        _ROOM_ID,
        response_sender="@bot:localhost",
        source_event_ids=(source_event_id,),
    )

    assert response_event_ids == frozenset()


@pytest.mark.asyncio
async def test_response_recovery_scan_rejects_repeated_pagination_token() -> None:
    """A stuck homeserver cursor must fail recovery instead of looping forever."""
    client = AsyncMock()
    page = _messages_response(
        [_message_event("$unrelated:localhost", "unrelated", timestamp=2000)],
        end="stuck-token",
    )
    client.room_messages = AsyncMock(return_value=page)

    with pytest.raises(RuntimeError, match="repeated pagination token"):
        await find_response_event_ids_via_room_messages(
            client,
            _ROOM_ID,
            response_sender="@bot:localhost",
            source_event_ids=("$missing-source:localhost",),
        )

    assert client.room_messages.await_count == 2


@pytest.mark.asyncio
async def test_bulk_refresh_scans_room_once_and_stores_each_thread() -> None:
    """One backward walk should recover and store every requested thread's rows root-first."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [
                    _edit_event(
                        "$a1-edit:localhost",
                        "$a1:localhost",
                        timestamp=5000,
                        thread_root_id="$a:localhost",
                    ),
                    _message_event("$b1:localhost", "reply b", timestamp=4000, thread_root_id="$b:localhost"),
                    _message_event("$a1:localhost", "reply a", timestamp=3000, thread_root_id="$a:localhost"),
                ],
                end="t1",
            ),
            _messages_response(
                [
                    _message_event("$b:localhost", "root b", timestamp=2000),
                    _message_event("$a:localhost", "root a", timestamp=1000),
                    _message_event("$solo:localhost", "no thread", timestamp=500),
                ],
                end="t2",
            ),
        ],
    )
    event_cache = AsyncMock()
    event_cache.room_membership_epoch = AsyncMock(return_value=7)
    event_cache.replace_thread = AsyncMock(side_effect=[True, True])

    stats = await bulk_refresh_room_thread_histories(
        client,
        _ROOM_ID,
        event_cache,
        thread_root_ids=["$a:localhost", "$b:localhost"],
        caller_label="test",
    )

    assert client.room_messages.await_count == 2
    assert stats.requested_threads == 2
    assert stats.usable_threads == 2
    assert stats.missing_root_ids == frozenset()
    assert stats.room_scan_pages == 2

    stored = {
        call.args[1]: [source["event_id"] for source in call.args[2]]
        for call in event_cache.replace_thread.await_args_list
    }
    assert stored == {
        "$a:localhost": ["$a:localhost", "$a1:localhost", "$a1-edit:localhost"],
        "$b:localhost": ["$b:localhost", "$b1:localhost"],
    }
    assert all(call.kwargs["expected_membership_epoch"] == 7 for call in event_cache.replace_thread.await_args_list)


@pytest.mark.asyncio
async def test_bulk_refresh_reports_missing_roots_without_storing_partial_threads() -> None:
    """Roots absent from a drained scan must be reported and never stored."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [
                    _message_event("$a1:localhost", "reply a", timestamp=3000, thread_root_id="$a:localhost"),
                    _message_event("$a:localhost", "root a", timestamp=1000),
                ],
                end=None,
            ),
        ],
    )
    event_cache = AsyncMock()
    event_cache.room_departure_epoch = Mock(return_value=3)
    event_cache.replace_thread = AsyncMock(return_value=True)

    stats = await bulk_refresh_room_thread_histories(
        client,
        _ROOM_ID,
        event_cache,
        thread_root_ids=["$a:localhost", "$ghost:localhost"],
        caller_label="test",
    )

    assert stats.usable_threads == 1
    assert stats.missing_root_ids == frozenset({"$ghost:localhost"})
    event_cache.replace_thread.assert_awaited_once()
    assert event_cache.replace_thread.await_args.args[1] == "$a:localhost"


@pytest.mark.asyncio
async def test_bulk_refresh_page_budget_stores_found_threads_and_reports_remaining_roots() -> None:
    """A capped startup scan should preserve partial success without reading another page."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [
                    _message_event("$b1:localhost", "reply b", timestamp=3000, thread_root_id="$b:localhost"),
                    _message_event("$a:localhost", "root a", timestamp=1000),
                ],
                end="t1",
            ),
            _messages_response(
                [_message_event("$b:localhost", "root b", timestamp=500)],
                end=None,
            ),
        ],
    )
    event_cache = AsyncMock()
    event_cache.room_membership_epoch = AsyncMock(return_value=7)
    event_cache.replace_thread = AsyncMock(return_value=True)

    stats = await bulk_refresh_room_thread_histories(
        client,
        _ROOM_ID,
        event_cache,
        thread_root_ids=["$a:localhost", "$b:localhost"],
        caller_label="test",
        max_scan_pages=1,
    )

    client.room_messages.assert_awaited_once()
    assert stats.usable_threads == 1
    assert stats.missing_root_ids == frozenset({"$b:localhost"})
    assert stats.room_scan_pages == 1
    assert stats.scan_truncated is True
    event_cache.replace_thread.assert_awaited_once()
    assert event_cache.replace_thread.await_args.args[1] == "$a:localhost"


@pytest.mark.asyncio
async def test_scan_failure_log_names_the_acting_client() -> None:
    """A rejected scan must name the client whose credentials the homeserver refused.

    The control plane runs several clients against one homeserver, so a permission
    failure is only actionable if the log says which one was refused.
    """
    client = AsyncMock()
    client.user_id = "@agent:localhost"
    client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesError.from_dict(
            {"errcode": "M_FORBIDDEN", "error": "You don't have permission to view this room."},
            _ROOM_ID,
        ),
    )
    event_cache = AsyncMock()
    event_cache.room_membership_epoch = AsyncMock(return_value=7)

    with capture_logs() as logs, pytest.raises(RuntimeError, match="bulk room scan failed"):
        await bulk_refresh_room_thread_histories(
            client,
            _ROOM_ID,
            event_cache,
            thread_root_ids=["$a:localhost"],
            caller_label="test",
        )

    failures = [entry for entry in logs if entry["event"] == "Failed bulk thread history scan"]
    assert len(failures) == 1
    assert failures[0]["user_id"] == "@agent:localhost"
    assert failures[0]["room_id"] == _ROOM_ID
    assert "M_FORBIDDEN" in failures[0]["error"]


@pytest.mark.asyncio
async def test_root_not_found_log_names_the_acting_client() -> None:
    """A scan that never sees the thread root must also name the acting client.

    A client that can only see part of a room's history produces the same symptom,
    so this log needs the same identity to be diagnosable.
    """
    client = AsyncMock()
    client.user_id = "@agent:localhost"
    client.room_messages = AsyncMock(
        return_value=_messages_response(
            [_message_event("$other:localhost", "unrelated", timestamp=1000)],
            end=None,
        ),
    )

    with capture_logs() as logs, pytest.raises(ThreadRoomScanRootNotFoundError):
        await fetch_thread_event_sources_via_room_messages(client, _ROOM_ID, "$missing:localhost")

    misses = [entry for entry in logs if entry["event"] == "Thread room scan ended without finding root"]
    assert len(misses) == 1
    assert misses[0]["user_id"] == "@agent:localhost"


@pytest.mark.asyncio
async def test_unresolved_opaque_scan_log_names_the_acting_client() -> None:
    """A scan blocked by undecryptable relations must name the client that could not decrypt them.

    Whether ciphertext stays opaque is a property of the acting client's megolm store, not of the
    room, so this is the failure where identity is the only thing separating a key-distribution
    problem from a server one.
    """
    client = AsyncMock()
    client.user_id = "@agent:localhost"
    client.room_messages = AsyncMock(
        return_value=_messages_response(
            [
                _opaque_reply_event("$opaque:localhost", replies_to="$unscanned:localhost", timestamp=2000),
                _message_event("$root:localhost", "root", timestamp=1000),
            ],
            end=None,
        ),
    )

    with capture_logs() as logs, pytest.raises(OpaqueEncryptedThreadHistoryError):
        await fetch_thread_event_sources_via_room_messages(client, _ROOM_ID, "$root:localhost")

    opaque = [entry for entry in logs if "opaque encrypted relations with unresolved impact" in entry["event"]]
    assert len(opaque) == 1
    assert opaque[0]["user_id"] == "@agent:localhost"
    assert opaque[0]["unresolved_opaque_event_ids"] == ["$opaque:localhost"]


@pytest.mark.asyncio
async def test_bulk_refresh_unresolved_opaque_log_names_the_acting_client() -> None:
    """The bulk path gap-marks the whole room on opaque relations, so it needs the identity too."""
    client = AsyncMock()
    client.user_id = "@agent:localhost"
    client.room_messages = AsyncMock(
        return_value=_messages_response(
            [
                _opaque_reply_event("$opaque:localhost", replies_to="$unscanned:localhost", timestamp=2000),
                _message_event("$root:localhost", "root", timestamp=1000),
            ],
            end=None,
        ),
    )
    event_cache = AsyncMock()
    event_cache.room_membership_epoch = AsyncMock(return_value=7)

    with capture_logs() as logs:
        stats = await bulk_refresh_room_thread_histories(
            client,
            _ROOM_ID,
            event_cache,
            thread_root_ids=["$root:localhost"],
            caller_label="test",
        )

    assert stats.usable_threads == 0
    event_cache.replace_thread.assert_not_awaited()
    opaque = [entry for entry in logs if "opaque encrypted relations with unresolved impact" in entry["event"]]
    assert len(opaque) == 1
    assert opaque[0]["user_id"] == "@agent:localhost"
    assert opaque[0]["unresolved_opaque_event_ids"] == ["$opaque:localhost"]


def _cached_message_source(event_id: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "sender": "@alice:localhost",
        "origin_server_ts": 1_000,
        "room_id": _ROOM_ID,
        "type": "m.room.message",
        "content": {"body": event_id, "msgtype": "m.text"},
    }


@pytest.mark.asyncio
async def test_prewarm_probe_selects_cold_threads_as_well_as_gapped_ones(
    event_cache: ConversationEventCache,
) -> None:
    """The probe that drives startup prewarm must not read a never-cached thread as warm.

    Two independent ways a thread fails to serve, and only one of them writes a marker. A probe that
    asks about the marker alone answers "warm" for every thread that was never cached, so prewarm
    selects nothing on a cold start and quietly does no work at all. Nothing downstream notices,
    because live reads still refill on demand - they just each pay for it.
    """
    warm_thread_id = "$warm:localhost"
    gapped_thread_id = "$gapped:localhost"
    cold_thread_id = "$cold:localhost"

    for thread_id in (warm_thread_id, gapped_thread_id):
        await replace_thread_unconditionally(
            event_cache,
            _ROOM_ID,
            thread_id,
            [_cached_message_source(thread_id)],
        )
    await event_cache.mark_thread_gap(_ROOM_ID, gapped_thread_id, reason="live_thread_mutation")

    needs_refill = await thread_ids_needing_refill(
        event_cache,
        _ROOM_ID,
        [warm_thread_id, gapped_thread_id, cold_thread_id],
    )

    assert needs_refill == (gapped_thread_id, cold_thread_id), (
        "startup prewarm would skip the cold thread and warm nothing"
    )
