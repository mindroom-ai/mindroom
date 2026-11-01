"""Tests for the room scans that recover an exact delivery from Matrix history."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.event_journal.models import DURABLE_DELIVERY_ID_KEY
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix.room_history_reads import (
    _MAX_EXACT_DELIVERY_SCAN_PAGES,
    _UnresolvedOpaqueRoomHistoryError,
    find_outbox_delivery_event_id_via_room_messages,
    find_response_event_ids_via_room_messages,
    missing_outbox_delivery_copy_indices_via_room_messages,
)


def raw_nio_event(event_source: dict[str, Any]) -> nio.Event:
    """Return a typed nio event that preserves one exact raw source payload."""
    return nio.UnknownEvent(event_source, event_source["type"])


_ROOM_ID = "!room:localhost"


def _parsed_room_message(event_source: dict[str, Any]) -> nio.RoomMessage:
    """Return the class nio itself picks for one `m.room.message` source.

    Built through nio's own msgtype dispatch rather than a named class, so a
    fixture cannot claim a parse the production reader would not get.
    """
    event = nio.RoomMessage.parse_event(event_source)
    assert isinstance(event, nio.RoomMessage)
    return event


def _message_event(
    event_id: str,
    body: str,
    *,
    timestamp: int,
    thread_root_id: str | None = None,
    reply_to_event_id: str | None = None,
    is_falling_back: bool = False,
    sender: str = "@alice:localhost",
    msgtype: str = "m.text",
    extra_content: dict[str, object] | None = None,
) -> nio.RoomMessage:
    content: dict[str, object] = {"body": body, "msgtype": msgtype, **(extra_content or {})}
    if thread_root_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root_id}
    if reply_to_event_id is not None:
        relation = content.setdefault("m.relates_to", {})
        assert isinstance(relation, dict)
        relation["m.in_reply_to"] = {"event_id": reply_to_event_id}
        if is_falling_back:
            relation["is_falling_back"] = True
    return _parsed_room_message(
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
) -> nio.RoomMessage:
    return _parsed_room_message(
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


def _frozen_delivery(*, body: str = "scheduled", delivery_id: str = "scheduled-turn") -> dict[str, object]:
    """Return the content of one frozen outbox delivery, its durable marker included."""
    return {
        "msgtype": "m.text",
        "body": body,
        DURABLE_DELIVERY_ID_KEY: {"principal": "agent@@bot:localhost", "delivery_id": delivery_id, "stage": "final"},
    }


def _opaque_reply_event(event_id: str, *, replies_to: str, timestamp: int) -> nio.Event:
    """Return ciphertext this client could not decrypt, with its reply relation left in cleartext."""
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
@pytest.mark.parametrize("encrypted", [False, True])
async def test_response_recovery_scan_finds_unquoted_room_level_reply(encrypted: bool) -> None:
    """A room-level reply clients do not quote is still the bot response to its source, even as ciphertext."""
    source_event_id = "$source:localhost"
    response_event_id = "$response:localhost"
    content = build_message_content("Done.", reply_to_event_id=source_event_id)
    response_source: dict[str, Any] = {
        "event_id": response_event_id,
        "sender": "@bot:localhost",
        "origin_server_ts": 2000,
        "room_id": _ROOM_ID,
        "type": "m.room.message",
        "content": content,
    }
    if encrypted:
        # nio leaves only the relation in cleartext when it encrypts an event.
        response_source["type"] = "m.room.encrypted"
        response_source["content"] = {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "opaque",
            "m.relates_to": content["m.relates_to"],
        }
    response_event = raw_nio_event(response_source) if encrypted else _parsed_room_message(response_source)
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
    """A stuck homeserver cursor must fail recovery instead of looping forever or reading as an absence."""
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
async def test_exact_outbox_scan_adopts_a_match_when_its_source_exceeds_the_bound() -> None:
    """A recent frozen delivery remains proof even when its older reply target is out of range."""
    source_event_id = "$old-response:localhost"
    delivered = _message_event(
        "$notice:localhost",
        "Approval owner unavailable.",
        timestamp=2000,
        reply_to_event_id=source_event_id,
        sender="@bot:localhost",
        msgtype="m.notice",
    )
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response([delivered], end="page-1"),
            *[
                _messages_response(
                    [_message_event(f"$unrelated-{page}:localhost", "unrelated", timestamp=page)],
                    end=f"page-{page}",
                )
                for page in range(2, _MAX_EXACT_DELIVERY_SCAN_PAGES + 1)
            ],
        ],
    )

    found = await find_outbox_delivery_event_id_via_room_messages(
        client,
        _ROOM_ID,
        delivery_sender="@bot:localhost",
        source_event_ids=(source_event_id,),
        delivery_content=delivered.source["content"],
        delivery_event_type="m.room.message",
    )

    assert found == "$notice:localhost"
    assert client.room_messages.await_count == _MAX_EXACT_DELIVERY_SCAN_PAGES


@pytest.mark.asyncio
async def test_exact_delivery_scan_refuses_to_guess_after_its_recent_history_bound() -> None:
    """An absent delivery is unproven once recent history still has older pages.

    Running out of pages establishes nothing, so the bound raises instead of
    reading as "not delivered".
    """
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [_message_event(f"$unrelated-{page}:localhost", "unrelated", timestamp=page)],
                end=f"page-{page + 1}",
            )
            for page in range(_MAX_EXACT_DELIVERY_SCAN_PAGES)
        ],
    )

    with pytest.raises(RuntimeError, match=r"exact delivery room scan.*absence is unproven"):
        await find_response_event_ids_via_room_messages(
            client,
            _ROOM_ID,
            response_sender="@bot:localhost",
            source_event_ids=(),
            response_content=_frozen_delivery(),
        )

    assert client.room_messages.await_count == _MAX_EXACT_DELIVERY_SCAN_PAGES


@pytest.mark.asyncio
async def test_exact_delivery_scan_continues_after_an_empty_page_with_a_cursor() -> None:
    """A filtered empty page is not proof that older Matrix history is exhausted."""
    response_content = _frozen_delivery()
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response([], end="next-page"),
            _messages_response(
                [
                    _message_event(
                        "$scheduled:localhost",
                        "scheduled",
                        timestamp=1,
                        sender="@bot:localhost",
                        extra_content={DURABLE_DELIVERY_ID_KEY: response_content[DURABLE_DELIVERY_ID_KEY]},
                    ),
                ],
                end=None,
            ),
        ],
    )

    response_event_ids = await find_response_event_ids_via_room_messages(
        client,
        _ROOM_ID,
        response_sender="@bot:localhost",
        source_event_ids=(),
        response_content=response_content,
    )

    assert response_event_ids == frozenset({"$scheduled:localhost"})
    assert client.room_messages.await_count == 2


@pytest.mark.asyncio
async def test_exact_delivery_scan_cannot_prove_absence_past_opaque_bot_ciphertext() -> None:
    """An undecryptable event from the delivery sender could be the frozen payload."""
    opaque = _opaque_reply_event("$opaque:localhost", replies_to="$unrelated:localhost", timestamp=2000)
    opaque.source["sender"] = "@bot:localhost"
    client = AsyncMock()
    client.room_messages = AsyncMock(return_value=_messages_response([opaque], end=None))

    with pytest.raises(_UnresolvedOpaqueRoomHistoryError, match="exact delivery"):
        await find_response_event_ids_via_room_messages(
            client,
            _ROOM_ID,
            response_sender="@bot:localhost",
            source_event_ids=(),
            response_content=_frozen_delivery(),
        )


@pytest.mark.asyncio
async def test_exact_delivery_bound_does_not_override_a_completed_source_scan() -> None:
    """Reaching every source proves the delivery absent even on the final bounded page."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            *[
                _messages_response(
                    [_message_event(f"$unrelated-{page}:localhost", "unrelated", timestamp=page)],
                    end=f"page-{page + 1}",
                )
                for page in range(_MAX_EXACT_DELIVERY_SCAN_PAGES - 1)
            ],
            _messages_response(
                [_message_event("$source:localhost", "source", timestamp=1)],
                end="older-history",
            ),
        ],
    )

    response_event_ids = await find_response_event_ids_via_room_messages(
        client,
        _ROOM_ID,
        response_sender="@bot:localhost",
        source_event_ids=("$source:localhost",),
        response_content=_frozen_delivery(body="answer", delivery_id="$source:localhost"),
    )

    assert response_event_ids == frozenset()
    assert client.room_messages.await_count == _MAX_EXACT_DELIVERY_SCAN_PAGES


@pytest.mark.asyncio
@pytest.mark.parametrize("final_cursor", ["older-history", "page-1"], ids=["more-history", "repeated-cursor"])
async def test_copy_count_settles_on_the_page_that_holds_the_last_copy(final_cursor: str) -> None:
    """Finding the last missing copy settles the count, even on the bound's final page or one whose cursor repeats."""
    continuation = _frozen_delivery(body="continued", delivery_id="segmented-turn")
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            *[
                _messages_response(
                    [_message_event(f"$unrelated-{page}:localhost", "unrelated", timestamp=page)],
                    end=f"page-{page}",
                )
                for page in range(1, _MAX_EXACT_DELIVERY_SCAN_PAGES)
            ],
            _messages_response(
                [
                    _message_event(
                        "$copy:localhost",
                        "continued",
                        timestamp=1,
                        sender="@bot:localhost",
                        extra_content=continuation,
                    ),
                ],
                end=final_cursor,
            ),
        ],
    )

    missing = await missing_outbox_delivery_copy_indices_via_room_messages(
        client,
        _ROOM_ID,
        delivery_sender="@bot:localhost",
        delivery_contents=[continuation],
        delivery_event_type="m.room.message",
    )

    assert missing == []
    assert client.room_messages.await_count == _MAX_EXACT_DELIVERY_SCAN_PAGES


def _approval_card_event(
    event_id: str,
    *,
    approval_id: str,
    timestamp: int,
    sender: str = "@router:localhost",
    replaces_event_id: str | None = None,
    status: str = "pending",
) -> nio.Event:
    """Return one approval card, or the terminal edit that replaces one."""
    content: dict[str, Any] = {
        "msgtype": "io.mindroom.tool_approval",
        "approval_id": approval_id,
        "status": status,
    }
    if replaces_event_id is not None:
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": replaces_event_id}
    return raw_nio_event(
        {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "io.mindroom.tool_approval",
            "content": content,
        },
    )


async def _find_approval_card(client: nio.AsyncClient, *, approval_id: str) -> str | None:
    """Locate one exact frozen approval delivery through the generic outbox scanner."""
    return await find_outbox_delivery_event_id_via_room_messages(
        client,
        _ROOM_ID,
        delivery_sender="@router:localhost",
        source_event_ids=(),
        delivery_content={
            "msgtype": "io.mindroom.tool_approval",
            "approval_id": approval_id,
            "status": "pending",
        },
        delivery_event_type="io.mindroom.tool_approval",
    )


@pytest.mark.asyncio
async def test_approval_card_scan_finds_the_exact_frozen_payload() -> None:
    """A changed-device lookup adopts the exact card frozen in the outbox."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [_approval_card_event("$other:localhost", approval_id="wanted", status="denied", timestamp=3000)],
                end="page-2",
            ),
            _messages_response(
                [_approval_card_event("$card:localhost", approval_id="wanted", timestamp=2000)],
                end="page-3",
            ),
        ],
    )

    found = await _find_approval_card(client, approval_id="wanted")

    assert found == "$card:localhost"
    # Stops the moment it has the answer rather than walking the whole room.
    assert client.room_messages.await_count == 2


@pytest.mark.asyncio
async def test_approval_card_scan_reports_absence_only_after_seeing_all_history() -> None:
    """No card and no history left is the one answer that retires a row."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        return_value=_messages_response(
            [_approval_card_event("$other:localhost", approval_id="other", timestamp=2000)],
            end=None,
        ),
    )

    found = await _find_approval_card(client, approval_id="wanted")

    assert found is None


@pytest.mark.asyncio
async def test_approval_card_scan_ignores_a_card_another_sender_wrote() -> None:
    """Only this bot's own cards are its to expire."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        return_value=_messages_response(
            [
                _approval_card_event(
                    "$impostor:localhost",
                    approval_id="wanted",
                    timestamp=2000,
                    sender="@someone-else:localhost",
                ),
            ],
            end=None,
        ),
    )

    found = await _find_approval_card(client, approval_id="wanted")

    assert found is None


@pytest.mark.asyncio
async def test_approval_card_scan_ignores_the_edit_that_replaces_the_card() -> None:
    """A terminal edit carries the same approval id as the card it replaces.

    Adopting the edit would bind every later write to an event the room renders
    as part of another, so the original is the only match.
    """
    client = AsyncMock()
    client.room_messages = AsyncMock(
        return_value=_messages_response(
            [
                _approval_card_event(
                    "$edit:localhost",
                    approval_id="wanted",
                    timestamp=3000,
                    replaces_event_id="$card:localhost",
                ),
                _approval_card_event("$card:localhost", approval_id="wanted", timestamp=2000),
            ],
            end=None,
        ),
    )

    found = await _find_approval_card(client, approval_id="wanted")

    assert found == "$card:localhost"


@pytest.mark.asyncio
async def test_approval_card_scan_asks_for_the_encrypted_wrapper_too() -> None:
    """In an encrypted room the card is `m.room.encrypted` on the wire.

    nio decrypts the chunk in place and the plaintext type reappears on the
    event source, but a server-side filter naming only the plaintext type would
    have excluded the event before that could happen.
    """
    client = AsyncMock()
    client.room_messages = AsyncMock(return_value=_messages_response([], end=None))

    await _find_approval_card(client, approval_id="wanted")

    message_filter = client.room_messages.await_args.kwargs["message_filter"]
    assert set(message_filter["types"]) == {"io.mindroom.tool_approval", "m.room.encrypted"}
