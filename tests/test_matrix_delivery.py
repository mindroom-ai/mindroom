"""Tests for Matrix delivery trust behavior."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.matrix.client_delivery import build_edit_event_content, send_message_result, send_room_event_result


def _mock_client(*, encrypted: bool = False) -> AsyncMock:
    """Create a mock Matrix client with one room."""
    client = AsyncMock(spec=nio.AsyncClient)
    room = MagicMock()
    room.encrypted = encrypted
    client.rooms = {"!room:localhost": room}
    client.olm = MagicMock() if encrypted else None
    client.room_send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id="!room:localhost")
    return client


@pytest.mark.asyncio
async def test_send_message_result_ignores_unverified_devices() -> None:
    """Bots cannot interactively verify devices, so delivery always ignores device trust."""
    client = _mock_client()

    await send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is True


@pytest.mark.asyncio
async def test_send_message_result_ignores_unverified_devices_in_encrypted_room() -> None:
    """Encrypted-room sends must not be blocked by nio's device-trust checks."""
    client = _mock_client(encrypted=True)

    await send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is True


@pytest.mark.asyncio
async def test_room_event_waits_for_classic_room_cache_rebuild() -> None:
    """Non-message events use the same bounded recovery seam as visible messages."""
    client = _mock_client(encrypted=True)
    room = client.rooms.pop("!room:localhost")
    client.room_send.side_effect = [
        nio.SendRetryError("Classic Sync room state is being rebuilt."),
        nio.RoomSendResponse(event_id="$reaction:localhost", room_id="!room:localhost"),
    ]

    async def restore_room_cache(_delay: float) -> None:
        client.rooms["!room:localhost"] = room

    with patch("mindroom.matrix.client_delivery.asyncio.sleep", new=restore_room_cache):
        response = await send_room_event_result(
            client,
            "!room:localhost",
            "m.reaction",
            {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$event", "key": "👍"}},
            transaction_id="reaction-1",
            operation="test_reaction",
        )

    assert isinstance(response, nio.RoomSendResponse)
    assert response.event_id == "$reaction:localhost"
    assert client.room_send.await_count == 2
    assert all(call.kwargs["tx_id"] == "reaction-1" for call in client.room_send.await_args_list)


def test_outbound_matrix_events_use_delivery_boundary() -> None:
    """Production code must not bypass bounded recovery with direct room_send calls."""
    source_root = Path(__file__).parents[1] / "src" / "mindroom"
    violations: list[str] = []
    for source_path in source_root.rglob("*.py"):
        if source_path.name == "client_delivery.py":
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        violations.extend(
            f"{source_path.relative_to(source_root)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "room_send"
        )

    assert violations == []


def test_edit_fallback_preserves_replacement_message_type() -> None:
    """A notice replacement must also be a notice to suppress edit mention pushes."""
    content = build_edit_event_content(
        event_id="$original:localhost",
        new_content={
            "body": "Streaming answer",
            "msgtype": "m.notice",
            "m.mentions": {"user_ids": ["@user:localhost"]},
        },
        new_text="Streaming answer",
    )

    assert content["msgtype"] == "m.notice"
    assert content["m.new_content"]["msgtype"] == "m.notice"
    assert content["m.mentions"] == {"user_ids": ["@user:localhost"]}
    # The fallback body carries the replacement text too, not just its msgtype:
    # a client that does not understand m.replace renders this and nothing else.
    assert content["body"] == "* Streaming answer"


def test_edit_fallback_body_is_the_new_text_behind_the_edit_marker() -> None:
    """The outer body is what a client that ignores ``m.replace`` renders.

    Two layers carry the replacement: ``m.new_content`` for clients that
    understand edits, and the outer ``body`` for every client that does not.
    Only the second is ever seen by the second group, so a stale, empty, or
    unmarked one is a wrong message on screen rather than a formatting detail.
    Asserted by equality because each way of getting it wrong -- keeping the
    superseded text, blanking it, dropping the ``"* "`` marker -- produces a
    different string, and only equality rejects all three.
    """
    content = build_edit_event_content(
        event_id="$original:localhost",
        new_content={"body": "the corrected answer", "msgtype": "m.text"},
        new_text="the corrected answer",
    )

    assert content["body"] == "* the corrected answer"
    assert content["m.new_content"]["body"] == "the corrected answer"


def test_edit_fallback_body_follows_the_new_text_not_the_replacement_body() -> None:
    """The marked body is built from ``new_text``, which is not the replacement body.

    The two differ in every edit that mentions somebody: the replacement body
    is the mention-resolved text built by ``format_message_with_mentions``,
    while ``new_text`` is the text it was built from. Distinct strings here so
    the assertion identifies which one the fallback tracks instead of passing
    on a value both would produce.
    """
    content = build_edit_event_content(
        event_id="$original:localhost",
        new_content={"body": "hello @mindroom_code:localhost", "msgtype": "m.text"},
        new_text="hello @code",
    )

    assert content["body"] == "* hello @code"
    assert content["m.new_content"]["body"] == "hello @mindroom_code:localhost"


def test_edit_fallback_formatted_body_is_the_new_html_without_the_marker() -> None:
    """Only the plain-text fallback is marked; the HTML one deliberately is not.

    The envelope declares ``org.matrix.custom.html`` at top level, so a client
    that renders the fallback rich shows ``formatted_body``. Ours is the new
    HTML verbatim -- the replacement's when it has one, the raw new text when
    it does not -- and never gains the ``"* "`` the plain body gets. Pinned
    because the asymmetry is invisible from either half alone.
    """
    with_html = build_edit_event_content(
        event_id="$original:localhost",
        new_content={
            "body": "the corrected answer",
            "msgtype": "m.text",
            "format": "org.matrix.custom.html",
            "formatted_body": "<p>the corrected answer</p>",
        },
        new_text="the corrected answer",
    )

    assert with_html["format"] == "org.matrix.custom.html"
    assert with_html["formatted_body"] == "<p>the corrected answer</p>"
    assert with_html["body"] == "* the corrected answer"

    without_html = build_edit_event_content(
        event_id="$original:localhost",
        new_content={"body": "the corrected answer", "msgtype": "m.text"},
        new_text="the corrected answer",
    )

    assert without_html["format"] == "org.matrix.custom.html"
    assert without_html["formatted_body"] == "the corrected answer"


def test_edit_fallback_body_survives_extra_content_metadata() -> None:
    """Custom metadata is merged over the envelope, and merges can overwrite.

    ``extra_content`` is applied to the finished envelope after the fallback is
    installed, so anything it carries wins. Pinned so a future caller adding a
    key cannot silently replace the only text a non-edit-aware client shows.
    """
    content = build_edit_event_content(
        event_id="$original:localhost",
        new_content={"body": "the corrected answer", "msgtype": "m.text"},
        new_text="the corrected answer",
        extra_content={"io.mindroom.stream_status": "complete"},
    )

    assert content["body"] == "* the corrected answer"
    assert content["io.mindroom.stream_status"] == "complete"
    assert content["m.new_content"]["io.mindroom.stream_status"] == "complete"


def test_edit_envelope_discards_thread_relation() -> None:
    """An edit must discard any caller thread relation before adding m.replace."""
    replacement_with_fallback = {
        "msgtype": "m.text",
        "body": "edited",
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": "$thread_root",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$latest"},
        },
    }

    edit_content = build_edit_event_content(
        event_id="$original",
        new_content=replacement_with_fallback,
        new_text="edited",
    )

    assert "m.relates_to" not in edit_content["m.new_content"]
    assert edit_content["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$original"}
