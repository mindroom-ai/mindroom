"""Tests for the bulk thread backfill room scan."""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.event_journal import replacement_target
from mindroom.matrix.room_history_reads import (
    _MAX_APPROVAL_CARD_SCAN_PAGES,
    OpaqueEncryptedThreadHistoryError,
    fetch_thread_event_sources_via_room_messages,
    fetch_thread_messages_from_source,
    find_approval_card_event_id_via_room_messages,
    find_response_event_ids_via_room_messages,
)
from mindroom.matrix.thread_membership import ThreadRoomScanRootNotFoundError


def raw_nio_event(event_source: dict[str, Any]) -> nio.Event:
    """Return a typed nio event that preserves one exact raw source payload."""
    event_type = event_source.get("type")
    if not isinstance(event_type, str):
        msg = "Test Matrix event is missing type"
        raise TypeError(msg)
    return nio.UnknownEvent(event_source, event_type)


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
    sender: str = "@alice:localhost",
    new_body: str = "edited reply",
    msgtype: str = "m.text",
    extra_content: dict[str, object] | None = None,
) -> nio.RoomMessage:
    # `extra_content` lands on both halves, because a replacement is a whole
    # event content: a client editing an image caption resends `url` and `info`
    # alongside the fallback body, and nio refuses to type an `m.image` without
    # them.
    return _parsed_room_message(
        {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
            "content": {
                "body": f"* {new_body}",
                "msgtype": msgtype,
                **(extra_content or {}),
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
                "m.new_content": {
                    "body": new_body,
                    "msgtype": msgtype,
                    **(extra_content or {}),
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
    with capture_logs() as logs, pytest.raises(RuntimeError, match="bulk room scan failed"):
        await fetch_thread_event_sources_via_room_messages(client, _ROOM_ID, "$a:localhost")

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
async def test_thread_messages_from_source_resolves_edits_without_touching_a_store() -> None:
    """The freshness readers get resolved messages, and no local store is consulted.

    Both callers exist to observe a write another runtime just made, so a
    result assembled with any help from local state would defeat them. The
    assertion that matters is the second one: this client has no cache
    attached at all, and the read still produces the edited body.
    """
    root_id = "$root:localhost"
    reply_id = "$reply:localhost"
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [
                    _edit_event("$reply-edit:localhost", reply_id, timestamp=4000, thread_root_id=root_id),
                    _message_event("first draft", "first draft", timestamp=3000, thread_root_id=root_id),
                    _message_event(reply_id, "first draft", timestamp=2000, thread_root_id=root_id),
                    _message_event(root_id, "the question", timestamp=1000),
                ],
                end=None,
            ),
        ],
    )

    messages = await fetch_thread_messages_from_source(client, _ROOM_ID, root_id)

    assert next(message.event_id for message in messages) == root_id
    edited = next(message for message in messages if message.event_id == reply_id)
    assert edited.body == "edited reply"
    # Structural, not incidental: the reader takes no store to consult. An
    # `AsyncMock` client would satisfy any assertion phrased about attributes,
    # so the parameter list is the thing worth pinning.
    parameters = inspect.signature(fetch_thread_messages_from_source).parameters
    assert not [name for name in parameters if "cache" in name or "store" in name]


def _one_page_thread_client(chunk: list[nio.Event]) -> AsyncMock:
    """Return a client serving one page of room history and nothing else."""
    client = AsyncMock()
    client.room_messages = AsyncMock(side_effect=[_messages_response(chunk, end=None)])
    return client


@pytest.mark.asyncio
async def test_a_rebuilt_thread_contains_the_emote_the_projection_kept() -> None:
    """`/me` is ordinary user input, so a rebuilt thread has to contain it.

    Journal admission matches `m.room.message` at the base class, so a watched
    conversation holds emotes. A server-paginated read that dropped them would
    make one conversation read two ways depending on whether this process saw
    it happen -- which is the divergence the projection exists to remove.
    """
    root_id = "$root:localhost"
    emote_id = "$emote:localhost"
    client = _one_page_thread_client(
        [
            _message_event(
                emote_id,
                "waves at the bot",
                timestamp=3000,
                thread_root_id=root_id,
                msgtype="m.emote",
            ),
            _message_event(root_id, "the question", timestamp=1000),
        ],
    )

    messages = await fetch_thread_messages_from_source(client, _ROOM_ID, root_id)

    emote = next(message for message in messages if message.event_id == emote_id)
    assert emote.body == "waves at the bot"
    # Carried through rather than flattened to text: nothing downstream renders
    # an action differently, but a read that silently relabelled the msgtype
    # would be inventing a fact the room never stated.
    assert emote.to_dict()["msgtype"] == "m.emote"


@pytest.mark.asyncio
async def test_a_rebuilt_thread_applies_an_emote_replacement() -> None:
    """An edit of a `/me` message used to be dropped, showing the pre-edit body.

    The projection applies `m.replace` off the relation alone and never looks
    at the msgtype, so it showed the correction while this read showed the
    original. Both halves failed the same way: the replacement was excluded
    from the candidate pool, and an excluded replacement is also skipped as a
    message, so the read produced the stale body with nothing logged.
    """
    root_id = "$root:localhost"
    emote_id = "$emote:localhost"
    client = _one_page_thread_client(
        [
            _edit_event(
                "$emote-edit:localhost",
                emote_id,
                timestamp=4000,
                thread_root_id=root_id,
                new_body="waves politely",
                msgtype="m.emote",
            ),
            _message_event(
                emote_id,
                "waves at the bot",
                timestamp=3000,
                thread_root_id=root_id,
                msgtype="m.emote",
            ),
            _message_event(root_id, "the question", timestamp=1000),
        ],
    )

    messages = await fetch_thread_messages_from_source(client, _ROOM_ID, root_id)

    edited = next(message for message in messages if message.event_id == emote_id)
    assert edited.body == "waves politely"
    assert edited.latest_event_id == "$emote-edit:localhost"
    assert [message.event_id for message in messages] == [root_id, emote_id]


@pytest.mark.asyncio
async def test_a_rebuilt_thread_collapses_to_the_newest_revision_across_msgtypes() -> None:
    """A message edited from text into an action collapses to the action.

    A client may change the msgtype in a replacement, and the newest one wins
    whatever it is. While emotes were unrankable this read answered with the
    older text revision, which is the ranking half of the same defect.
    """
    root_id = "$root:localhost"
    reply_id = "$reply:localhost"
    client = _one_page_thread_client(
        [
            _edit_event(
                "$edit-emote:localhost",
                reply_id,
                timestamp=5000,
                thread_root_id=root_id,
                new_body="shrugs",
                msgtype="m.emote",
            ),
            _edit_event(
                "$edit-text:localhost",
                reply_id,
                timestamp=4000,
                thread_root_id=root_id,
                new_body="superseded correction",
            ),
            _message_event(reply_id, "first draft", timestamp=3000, thread_root_id=root_id),
            _message_event(root_id, "the question", timestamp=1000),
        ],
    )

    messages = await fetch_thread_messages_from_source(client, _ROOM_ID, root_id)

    collapsed = next(message for message in messages if message.event_id == reply_id)
    assert collapsed.body == "shrugs"
    assert collapsed.latest_event_id == "$edit-emote:localhost"


@pytest.mark.asyncio
async def test_a_msgtype_nio_cannot_type_stays_out_of_the_replacement_pool() -> None:
    """`RoomMessageUnknown` carries no body, which is why it is not widened in.

    It is the one exclusion the pool names, and the same line admission draws:
    the conversation still contains the event, but nothing treats it as a
    visible message. A pool that said `RoomMessage` and stopped would reach
    `.body` on a class that has none.
    """
    root_id = "$root:localhost"
    reply_id = "$reply:localhost"
    location_edit = _edit_event(
        "$edit-location:localhost",
        reply_id,
        timestamp=9000,
        thread_root_id=root_id,
        new_body="somewhere else",
        msgtype="m.location",
    )
    assert isinstance(location_edit, nio.RoomMessageUnknown), "fixture must reach nio's unknown-msgtype class"
    client = _one_page_thread_client(
        [
            location_edit,
            _message_event(reply_id, "first draft", timestamp=3000, thread_root_id=root_id),
            _message_event(root_id, "the question", timestamp=1000),
        ],
    )

    messages = await fetch_thread_messages_from_source(client, _ROOM_ID, root_id)

    unedited = next(message for message in messages if message.event_id == reply_id)
    assert unedited.body == "first draft"
    assert unedited.latest_event_id == reply_id


_PICTURE = {"url": "mxc://localhost/picture", "info": {"mimetype": "image/png", "w": 8, "h": 8}}


@pytest.mark.asyncio
async def test_a_rebuilt_thread_applies_an_image_caption_edit() -> None:
    """The projection applies a media replacement, so a rebuilt read has to as well.

    `event_journal.projection.project` decides an event is an edit from
    `replacement_target(content)` alone and never looks at a msgtype, so a
    watched conversation shows the corrected caption. This read matched the
    candidate pool on textual msgtypes, so it dropped the replacement -- and a
    replacement excluded from the pool is also skipped as a message, so the
    correction vanished entirely and the read answered with the caption the
    sender had already fixed.
    """
    root_id = "$root:localhost"
    image_id = "$image:localhost"
    caption_edit = _edit_event(
        "$caption-edit:localhost",
        image_id,
        timestamp=4000,
        thread_root_id=root_id,
        new_body="the corrected caption",
        msgtype="m.image",
        extra_content=dict(_PICTURE),
    )
    # The live side's own rule, asserted here rather than assumed: this content
    # is an edit to the projection, which is exactly why the read may not
    # disagree about it.
    assert replacement_target(caption_edit.source["content"]) == image_id
    client = _one_page_thread_client(
        [
            caption_edit,
            _message_event(
                image_id,
                "the original caption",
                timestamp=3000,
                thread_root_id=root_id,
                msgtype="m.image",
                extra_content=dict(_PICTURE),
            ),
            _message_event(root_id, "the question", timestamp=1000),
        ],
    )

    messages = await fetch_thread_messages_from_source(client, _ROOM_ID, root_id)

    edited = next(message for message in messages if message.event_id == image_id)
    assert edited.body == "the corrected caption"
    assert edited.latest_event_id == "$caption-edit:localhost"
    # Still the picture it was. A caption edit corrects the words attached to
    # the media, so losing `url` here would turn an edited image into text.
    assert edited.content["url"] == _PICTURE["url"]
    assert edited.to_dict()["msgtype"] == "m.image"
    assert [message.event_id for message in messages] == [root_id, image_id]


@pytest.mark.asyncio
async def test_a_rebuilt_thread_collapses_a_text_message_edited_into_an_image() -> None:
    """A replacement may change the msgtype, and the newest one wins whatever it is.

    Matrix places no constraint that a replacement keep its original's msgtype,
    and the projection applies whichever arrives newest. While media
    replacements were unrankable this read answered with the older text
    revision.
    """
    root_id = "$root:localhost"
    reply_id = "$reply:localhost"
    client = _one_page_thread_client(
        [
            _edit_event(
                "$edit-image:localhost",
                reply_id,
                timestamp=5000,
                thread_root_id=root_id,
                new_body="a picture instead",
                msgtype="m.image",
                extra_content=dict(_PICTURE),
            ),
            _edit_event(
                "$edit-text:localhost",
                reply_id,
                timestamp=4000,
                thread_root_id=root_id,
                new_body="superseded correction",
            ),
            _message_event(reply_id, "first draft", timestamp=3000, thread_root_id=root_id),
            _message_event(root_id, "the question", timestamp=1000),
        ],
    )

    messages = await fetch_thread_messages_from_source(client, _ROOM_ID, root_id)

    collapsed = next(message for message in messages if message.event_id == reply_id)
    assert collapsed.body == "a picture instead"
    assert collapsed.latest_event_id == "$edit-image:localhost"
    assert collapsed.to_dict()["msgtype"] == "m.image"


@pytest.mark.asyncio
async def test_a_rebuilt_thread_collapses_an_image_edited_into_text() -> None:
    """The other direction, which this read already got right.

    A thread scan kept media originals all along -- only replacements were
    filtered by msgtype -- so an image corrected into text always collapsed
    here. Pinned rather than dropped because it is the half of the widening
    that has to change nothing: media originals now resolve through the same
    extraction as every other visible message, and this is what says that swap
    kept the placement, the ordering timestamp, and the body it used to
    produce. The room read had no such luck, and
    `test_matrix_message_read_room_folds_a_text_edit_onto_the_picture_it_corrects`
    covers the direction that was broken there.
    """
    root_id = "$root:localhost"
    image_id = "$image:localhost"
    client = _one_page_thread_client(
        [
            _edit_event(
                "$edit-text:localhost",
                image_id,
                timestamp=5000,
                thread_root_id=root_id,
                new_body="words instead",
            ),
            _message_event(
                image_id,
                "the original caption",
                timestamp=3000,
                thread_root_id=root_id,
                msgtype="m.image",
                extra_content=dict(_PICTURE),
            ),
            _message_event(root_id, "the question", timestamp=1000),
        ],
    )

    messages = await fetch_thread_messages_from_source(client, _ROOM_ID, root_id)

    collapsed = next(message for message in messages if message.event_id == image_id)
    assert collapsed.body == "words instead"
    assert collapsed.latest_event_id == "$edit-text:localhost"
    assert collapsed.thread_id == root_id, "the original was read, so its placement is a fact rather than a guess"


@pytest.mark.asyncio
async def test_a_picture_that_is_not_an_edit_shows_the_body_the_room_published() -> None:
    """`m.new_content` on an event that replaces nothing is not a body.

    Media used to be resolved by a fallback path built for events nobody could
    type, and that path reads the visible layer as "`m.new_content` if there is
    one". Applied to an event with no `m.replace` relation, that let any sender
    ship a second body inside a message that revises nothing and have the read
    show it -- while `content` went on carrying the body the room actually
    displays, so a model was handed a message disagreeing with itself. Textual
    messages never had the hole, because they were already resolved by the
    extraction that only honours `m.new_content` for a trusted sender's real
    replacement.
    """
    root_id = "$root:localhost"
    image_id = "$image:localhost"
    client = _one_page_thread_client(
        [
            _message_event(
                image_id,
                "the caption the room shows",
                timestamp=3000,
                thread_root_id=root_id,
                sender="@impostor:localhost",
                msgtype="m.image",
                extra_content={
                    **_PICTURE,
                    "m.new_content": {"msgtype": "m.image", "body": "a body nothing published", **_PICTURE},
                },
            ),
            _message_event(root_id, "the question", timestamp=1000),
        ],
    )

    messages = await fetch_thread_messages_from_source(client, _ROOM_ID, root_id)

    picture = next(message for message in messages if message.event_id == image_id)
    assert picture.body == "the caption the room shows"
    assert picture.body == picture.content["body"], "the read may not disagree with the content it returns"
    assert picture.latest_event_id == image_id, "nothing was replaced, so nothing is a later revision"


def _injected_edit_scan_client(root_id: str, reply_id: str) -> AsyncMock:
    """Return a room whose newest event is an edit of something the scan never sees."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [
                    _edit_event(
                        "$injection:localhost",
                        "$never-scanned:localhost",
                        timestamp=4000,
                        thread_root_id=root_id,
                        sender="@intruder:localhost",
                        new_body="injected text",
                    ),
                    _message_event(reply_id, "a real reply", timestamp=2000, thread_root_id=root_id),
                    _message_event(root_id, "the question", timestamp=1000),
                ],
                end=None,
            ),
        ],
    )
    return client


@pytest.mark.asyncio
async def test_thread_event_sources_exclude_an_edit_whose_original_the_scan_never_saw() -> None:
    """A scan buckets a replacement by its original's thread, never by the thread it names.

    These sources are what proves whether a candidate event is a real thread root, so a foreign
    replacement filed into the wrong bucket does not merely add a row: it can promote any
    relation-free message in the room into a thread that only the replacement's author asked for.
    """
    root_id = "$root:localhost"
    reply_id = "$reply:localhost"

    scan_result = await fetch_thread_event_sources_via_room_messages(
        _injected_edit_scan_client(root_id, reply_id),
        _ROOM_ID,
        root_id,
    )

    assert [source["event_id"] for source in scan_result.event_sources] == [root_id, reply_id]


@pytest.mark.asyncio
async def test_thread_read_excludes_an_edit_whose_original_the_scan_never_saw() -> None:
    """An edit naming a thread must not publish its content into that thread's read.

    Applying ``m.new_content`` keeps the original event's relation and ignores every
    ``m.relates_to`` written inside the replacement, so the thread named there is a claim about an
    event this scan never read. Honouring it lets anyone who can send an edit put text of their
    choosing into the answer to "what is in this thread" - reporting that text's placement as
    unknown does not help, because being in the answer at all is the injection.
    """
    root_id = "$root:localhost"
    reply_id = "$reply:localhost"

    messages = await fetch_thread_messages_from_source(
        _injected_edit_scan_client(root_id, reply_id),
        _ROOM_ID,
        root_id,
    )

    assert [message.event_id for message in messages] == [root_id, reply_id]
    assert all(message.sender != "@intruder:localhost" for message in messages)
    assert all("injected text" not in message.body for message in messages)


@pytest.mark.asyncio
async def test_thread_messages_from_source_raises_rather_than_returning_a_partial_thread() -> None:
    """A scan that never finds the root must raise, not answer with what it saw.

    The auto-resume freshness check dropped its explicit completeness guard
    because this raises. If it returned the partial page instead, a thread
    whose root scrolled past the scan window would look like it had no newer
    human activity, and a stale turn would resume on top of one.
    """
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response([_message_event("$unrelated:localhost", "hi", timestamp=1000)], end=None),
        ],
    )

    with pytest.raises(ThreadRoomScanRootNotFoundError):
        await fetch_thread_messages_from_source(client, _ROOM_ID, "$missing-root:localhost")


def _approval_card_event(
    event_id: str,
    *,
    approval_id: str,
    timestamp: int,
    sender: str = "@router:localhost",
    replaces_event_id: str | None = None,
) -> nio.Event:
    """Return one approval card, or the terminal edit that replaces one."""
    content: dict[str, Any] = {
        "msgtype": "io.mindroom.tool_approval",
        "approval_id": approval_id,
        "status": "pending",
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


@pytest.mark.asyncio
async def test_approval_card_scan_finds_the_card_by_its_approval_id() -> None:
    """The card is located by the id frozen into its body, never by a transaction.

    A transaction ID is scoped to the device that used it, which is the exact
    reason this lookup is being made at all.
    """
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [_approval_card_event("$other:localhost", approval_id="other", timestamp=3000)],
                end="page-2",
            ),
            _messages_response(
                [_approval_card_event("$card:localhost", approval_id="wanted", timestamp=2000)],
                end="page-3",
            ),
        ],
    )

    found = await find_approval_card_event_id_via_room_messages(
        client,
        _ROOM_ID,
        card_sender="@router:localhost",
        approval_id="wanted",
    )

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

    found = await find_approval_card_event_id_via_room_messages(
        client,
        _ROOM_ID,
        card_sender="@router:localhost",
        approval_id="wanted",
    )

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

    found = await find_approval_card_event_id_via_room_messages(
        client,
        _ROOM_ID,
        card_sender="@router:localhost",
        approval_id="wanted",
    )

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

    found = await find_approval_card_event_id_via_room_messages(
        client,
        _ROOM_ID,
        card_sender="@router:localhost",
        approval_id="wanted",
    )

    assert found == "$card:localhost"


@pytest.mark.asyncio
async def test_approval_card_scan_refuses_to_call_a_bounded_walk_an_absence() -> None:
    """Running out of pages establishes nothing, and must not read as "no card".

    An unproven absence retires the row, and the card it belongs to then stays
    clickable with nothing behind it forever. So the bound raises, the row is
    kept, and the sweep comes back.
    """
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [_approval_card_event(f"$other-{page}:localhost", approval_id="other", timestamp=page)],
                end=f"page-{page}",
            )
            for page in range(1, 40)
        ],
    )

    with pytest.raises(RuntimeError, match="absence is unproven"):
        await find_approval_card_event_id_via_room_messages(
            client,
            _ROOM_ID,
            card_sender="@router:localhost",
            approval_id="wanted",
        )

    assert client.room_messages.await_count == _MAX_APPROVAL_CARD_SCAN_PAGES


@pytest.mark.asyncio
async def test_approval_card_scan_rejects_a_repeated_pagination_token() -> None:
    """A stuck cursor is a failed lookup, not an absence."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        return_value=_messages_response(
            [_approval_card_event("$other:localhost", approval_id="other", timestamp=2000)],
            end="stuck-token",
        ),
    )

    with pytest.raises(RuntimeError, match="repeated pagination token"):
        await find_approval_card_event_id_via_room_messages(
            client,
            _ROOM_ID,
            card_sender="@router:localhost",
            approval_id="wanted",
        )


@pytest.mark.asyncio
async def test_approval_card_scan_asks_for_the_encrypted_wrapper_too() -> None:
    """In an encrypted room the card is `m.room.encrypted` on the wire.

    nio decrypts the chunk in place and the plaintext type reappears on the
    event source, but a server-side filter naming only the plaintext type would
    have excluded the event before that could happen.
    """
    client = AsyncMock()
    client.room_messages = AsyncMock(return_value=_messages_response([], end=None))

    await find_approval_card_event_id_via_room_messages(
        client,
        _ROOM_ID,
        card_sender="@router:localhost",
        approval_id="wanted",
    )

    message_filter = client.room_messages.await_args.kwargs["message_filter"]
    assert set(message_filter["types"]) == {"io.mindroom.tool_approval", "m.room.encrypted"}
