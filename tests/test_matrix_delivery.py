"""Tests for Matrix delivery trust behavior."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from nio import crypto

from mindroom.delivery_gateway import _matrix_delivery_failure_reason
from mindroom.matrix.client_delivery import (
    DeliveredMatrixEvent,
    MatrixDeliveryFailure,
    MatrixDeliveryFailureKind,
    build_edit_event_content,
    edit_message_outcome,
    edit_message_result,
    send_message_outcome,
    send_message_result,
    send_room_event_result,
)
from mindroom.matrix.large_messages import _MATRIX_EVENT_HARD_LIMIT, _calculate_delivery_event_size

if TYPE_CHECKING:
    from collections.abc import Callable
    from io import BytesIO


def _mock_client(*, encrypted: bool = False) -> AsyncMock:
    """Create a mock Matrix client with one room."""
    client = AsyncMock(spec=nio.AsyncClient)
    room = MagicMock()
    room.encrypted = encrypted
    client.rooms = {"!room:localhost": room}
    client.olm = MagicMock() if encrypted else None
    if client.olm is not None:
        client.olm.device_id = "DEVICE"
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


def _cache_bypass_client(*, encrypted: bool | None) -> AsyncMock:
    """Create a mock client without a cached room; encryption state answers as given."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}
    if encrypted is None:
        encryption_state = MagicMock(spec=nio.RoomGetStateEventError)
        encryption_state.status_code = "M_FORBIDDEN"
    elif encrypted:
        encryption_state = MagicMock(spec=nio.RoomGetStateEventResponse)
    else:
        encryption_state = MagicMock(spec=nio.RoomGetStateEventError)
        encryption_state.status_code = "M_NOT_FOUND"
    client.room_get_state_event = AsyncMock(return_value=encryption_state)
    client.olm = MagicMock() if encrypted else None
    if client.olm is not None:
        client.olm.device_id = "DEVICE"
    client.room_send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id="!room:localhost")
    return client


@pytest.mark.asyncio
async def test_send_message_outcome_maps_encryption_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing local E2EE support maps to the encryption-guard failure kind."""
    monkeypatch.setattr(crypto, "ENCRYPTION_ENABLED", False)
    client = _mock_client(encrypted=True)

    outcome = await send_message_outcome(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert isinstance(outcome, MatrixDeliveryFailure)
    assert outcome.kind is MatrixDeliveryFailureKind.ENCRYPTION_GUARD
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_outcome_supports_uncached_encrypted_room() -> None:
    """A remotely confirmed encrypted room can send before nio's room cache is populated."""
    client = _cache_bypass_client(encrypted=True)

    outcome = await send_message_outcome(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert isinstance(outcome, DeliveredMatrixEvent)
    assert outcome.event_id == "$event:localhost"


@pytest.mark.asyncio
async def test_send_message_outcome_maps_unknown_encryption_state() -> None:
    """An undeterminable room encryption state maps to the unknown-encryption-state kind."""
    client = _cache_bypass_client(encrypted=None)

    outcome = await send_message_outcome(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert isinstance(outcome, MatrixDeliveryFailure)
    assert outcome.kind is MatrixDeliveryFailureKind.UNKNOWN_ENCRYPTION_STATE
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_outcome_maps_send_exception() -> None:
    """A local send exception maps to the send-exception kind."""
    client = _mock_client()
    client.room_send.side_effect = RuntimeError("boom")

    outcome = await send_message_outcome(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert isinstance(outcome, MatrixDeliveryFailure)
    assert outcome.kind is MatrixDeliveryFailureKind.SEND_EXCEPTION


@pytest.mark.asyncio
async def test_send_message_outcome_maps_an_unrepresentable_payload() -> None:
    """Irreducible metadata is a typed refusal and never reaches Matrix."""
    client = _mock_client()
    client.upload.return_value = (
        nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/impossible-message"}),
        None,
    )
    content = {
        "body": "x" * 70_000,
        "msgtype": "m.text",
        "io.mindroom.required_metadata": "m" * 70_000,
    }

    outcome = await send_message_outcome(client, "!room:localhost", content)

    assert isinstance(outcome, MatrixDeliveryFailure)
    assert outcome.kind is MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_outcome_maps_unexpected_response() -> None:
    """A non-send response maps to the unexpected-response kind."""
    client = _mock_client()
    client.room_send.return_value = MagicMock(spec=nio.RoomSendError)

    outcome = await send_message_outcome(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert isinstance(outcome, MatrixDeliveryFailure)
    assert outcome.kind is MatrixDeliveryFailureKind.UNEXPECTED_RESPONSE


@pytest.mark.asyncio
async def test_send_message_outcome_maps_server_too_large_response() -> None:
    """A definite homeserver size refusal is distinguishable from retryable failures."""
    client = _mock_client()
    client.room_send.return_value = nio.RoomSendError(
        message="event too large",
        status_code="M_TOO_LARGE",
        room_id="!room:localhost",
    )

    outcome = await send_message_outcome(
        client,
        "!room:localhost",
        {"body": "already prepared", "msgtype": "m.text"},
        content_is_prepared=True,
    )

    assert outcome == MatrixDeliveryFailure(
        MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE,
        "event too large",
    )


@pytest.mark.asyncio
async def test_send_message_outcome_keeps_other_server_refusals_retryable() -> None:
    """A server refusal is not permanent merely because it is a client error."""
    client = _mock_client()
    client.room_send.return_value = nio.RoomSendError(
        message="forbidden",
        status_code="M_FORBIDDEN",
        room_id="!room:localhost",
    )

    outcome = await send_message_outcome(
        client,
        "!room:localhost",
        {"body": "already prepared", "msgtype": "m.text"},
        content_is_prepared=True,
    )

    assert isinstance(outcome, MatrixDeliveryFailure)
    assert outcome.kind is MatrixDeliveryFailureKind.UNEXPECTED_RESPONSE


@pytest.mark.asyncio
async def test_send_message_outcome_success_returns_delivered_event() -> None:
    """Successful sends keep returning the delivered event id and sent content."""
    client = _mock_client(encrypted=True)
    content = {"body": "hello", "msgtype": "m.text"}

    outcome = await send_message_outcome(client, "!room:localhost", content)

    assert isinstance(outcome, DeliveredMatrixEvent)
    assert outcome.event_id == "$event:localhost"
    assert outcome.content_sent == content


@pytest.mark.asyncio
async def test_send_message_outcome_fits_for_encryption_enabled_during_sidecar_upload() -> None:
    """A state change while preparation yields must not make the event oversized."""
    client = _mock_client()
    client.device_id = "D"

    async def upload_and_enable_encryption(**kwargs: object) -> tuple[nio.UploadResponse, None]:
        data_provider = cast("Callable[[object, object], BytesIO]", kwargs["data_provider"])
        data_provider(None, None).read()
        client.rooms["!room:localhost"].encrypted = True
        client.olm = MagicMock()
        client.olm.device_id = "D"
        return nio.UploadResponse("mxc://server/direct-sidecar"), None

    client.upload.side_effect = upload_and_enable_encryption

    outcome = await send_message_outcome(
        client,
        "!room:localhost",
        {"body": "x" * 100_000, "msgtype": "m.text"},
    )

    assert isinstance(outcome, DeliveredMatrixEvent)
    assert (
        _calculate_delivery_event_size(
            outcome.content_sent,
            room_id="!room:localhost",
            room_encrypted=True,
            device_id="D",
        )
        <= _MATRIX_EVENT_HARD_LIMIT
    )


@pytest.mark.asyncio
async def test_send_message_outcome_rejects_a_frozen_event_that_no_longer_fits() -> None:
    """Recovery must validate the current encrypted envelope before network I/O."""
    client = _mock_client(encrypted=True)
    client.olm.device_id = "😀" * 512
    frozen = {"body": "x" * 45_000, "msgtype": "m.text"}

    assert (
        _calculate_delivery_event_size(
            frozen,
            room_id="!room:localhost",
            room_encrypted=True,
            device_id="D",
        )
        <= _MATRIX_EVENT_HARD_LIMIT
    )
    assert (
        _calculate_delivery_event_size(
            frozen,
            room_id="!room:localhost",
            room_encrypted=True,
            device_id=client.olm.device_id,
        )
        > _MATRIX_EVENT_HARD_LIMIT
    )

    outcome = await send_message_outcome(
        client,
        "!room:localhost",
        frozen,
        content_is_prepared=True,
    )

    assert isinstance(outcome, MatrixDeliveryFailure)
    assert outcome.kind is MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_result_still_collapses_failures_to_none() -> None:
    """The public result surface keeps its stable None collapse."""
    client = _mock_client()
    client.room_send.side_effect = RuntimeError("boom")

    delivered = await send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert delivered is None


@pytest.mark.asyncio
async def test_edit_message_result_still_collapses_failures_to_none() -> None:
    """The public edit surface keeps its stable None collapse."""
    client = _mock_client()
    client.room_send.side_effect = RuntimeError("boom")

    delivered = await edit_message_result(
        client,
        "!room:localhost",
        "$original",
        {"body": "updated", "msgtype": "m.text"},
        "updated",
    )

    assert delivered is None


@pytest.mark.asyncio
async def test_edit_message_outcome_success_returns_delivered_event() -> None:
    """Successful edits keep returning the delivered event id and edit content."""
    client = _mock_client()

    outcome = await edit_message_outcome(
        client,
        "!room:localhost",
        "$original",
        {"body": "updated", "msgtype": "m.text"},
        "updated",
    )

    assert isinstance(outcome, DeliveredMatrixEvent)
    assert outcome.event_id == "$event:localhost"
    assert outcome.content_sent["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$original"}


def test_gateway_failure_vocabulary_covers_every_failure_kind() -> None:
    """The gateway translation maps every typed failure kind and never guesses from None."""
    reasons = {
        kind: _matrix_delivery_failure_reason(MatrixDeliveryFailure(kind, "detail"))
        for kind in MatrixDeliveryFailureKind
    }
    assert len(set(reasons.values())) == len(MatrixDeliveryFailureKind)
    assert all("detail" in reason for reason in reasons.values())
    assert _matrix_delivery_failure_reason(None) == "matrix delivery failed"
