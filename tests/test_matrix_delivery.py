"""Tests for Matrix delivery trust behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

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
)


def _mock_client(*, encrypted: bool = False) -> AsyncMock:
    """Create a mock Matrix client with one room."""
    client = AsyncMock(spec=nio.AsyncClient)
    room = MagicMock()
    room.encrypted = encrypted
    client.rooms = {"!room:localhost": room}
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
    client.room_send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id="!room:localhost")
    return client


@pytest.mark.asyncio
async def test_send_message_outcome_maps_encryption_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing local E2EE support maps to the encryption-guard failure kind."""
    monkeypatch.setattr(nio.crypto, "ENCRYPTION_ENABLED", False)
    client = _mock_client(encrypted=True)

    outcome = await send_message_outcome(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert isinstance(outcome, MatrixDeliveryFailure)
    assert outcome.kind is MatrixDeliveryFailureKind.ENCRYPTION_GUARD
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_outcome_maps_sync_prerequisite() -> None:
    """An encrypted room send without a synced room cache maps to the sync-prerequisite kind."""
    client = _cache_bypass_client(encrypted=True)

    outcome = await send_message_outcome(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert isinstance(outcome, MatrixDeliveryFailure)
    assert outcome.kind is MatrixDeliveryFailureKind.SYNC_PREREQUISITE
    client.room_send.assert_not_awaited()


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
async def test_send_message_outcome_maps_unexpected_response() -> None:
    """A non-send response maps to the unexpected-response kind."""
    client = _mock_client()
    client.room_send.return_value = MagicMock(spec=nio.RoomSendError)

    outcome = await send_message_outcome(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

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
async def test_send_message_result_still_collapses_failures_to_none() -> None:
    """The public result surface keeps its stable None collapse."""
    client = _cache_bypass_client(encrypted=True)

    delivered = await send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})

    assert delivered is None


@pytest.mark.asyncio
async def test_edit_message_result_still_collapses_failures_to_none() -> None:
    """The public edit surface keeps its stable None collapse."""
    client = _cache_bypass_client(encrypted=True)

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
