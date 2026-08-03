"""Tests for Matrix delivery trust behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.matrix.client_delivery import (
    RoomDeliveryHydrationProof,
    build_edit_event_content,
    hydrate_joined_room_for_delivery,
    send_message_result,
)
from tests.conftest import TEST_ACCESS_TOKEN


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


@pytest.mark.asyncio
async def test_send_message_result_revalidates_hydration_after_content_preparation() -> None:
    """An awaited payload preparation must not outlive its authoritative room proof."""
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    remote_member_id = "@human:remote.example.org"
    hydrated_room = nio.MatrixRoom(room_id, "@bot:localhost")
    hydrated_room.encrypted = True
    hydrated_room.members_synced = True
    hydrated_room.add_member(remote_member_id, "Human", None)
    client.rooms = {room_id: hydrated_room}
    client.olm = MagicMock()
    client.users_for_key_query = set()
    proof = RoomDeliveryHydrationProof(
        encrypted=True,
        joined_user_ids=frozenset({remote_member_id}),
    )
    replacement_room = nio.MatrixRoom(room_id, "@bot:localhost")
    replacement_room.encrypted = True
    replacement_room.members_synced = True

    async def replace_room_during_preparation(
        _client: nio.AsyncClient,
        _room_id: str,
        content: dict[str, object],
    ) -> dict[str, object]:
        client.rooms[room_id] = replacement_room
        return content

    with patch(
        "mindroom.matrix.client_delivery.prepare_large_message",
        new=AsyncMock(side_effect=replace_room_during_preparation),
    ):
        delivered = await send_message_result(
            client,
            room_id,
            {"body": "hello", "msgtype": "m.text"},
            delivery_proof=proof,
        )

    assert delivered is None
    client.room_send.assert_not_awaited()


@pytest.mark.parametrize("cached_plaintext", [False, True])
@pytest.mark.asyncio
async def test_send_message_result_rechecks_plaintext_proof_authoritatively(
    *,
    cached_plaintext: bool,
) -> None:
    """A plaintext proof must detect server-side encryption before exact send."""
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    client.access_token = TEST_ACCESS_TOKEN
    client.rooms = {room_id: nio.MatrixRoom(room_id, "@bot:localhost")} if cached_plaintext else {}
    encrypted_response = nio.RoomGetStateEventResponse(
        content={"algorithm": "m.megolm.v1.aes-sha2"},
        event_type="m.room.encryption",
        state_key="",
        room_id=room_id,
    )
    client.room_get_state_event = AsyncMock(return_value=encrypted_response)
    client.room_send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id=room_id)
    client._send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id=room_id)

    delivered = await send_message_result(
        client,
        room_id,
        {"body": "hello", "msgtype": "m.text"},
        delivery_proof=RoomDeliveryHydrationProof(encrypted=False),
    )

    assert delivered is None
    assert client.room_get_state_event.await_count == 1
    client.room_send.assert_not_awaited()
    client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_plaintext_proof_rechecks_cache_after_final_remote_query() -> None:
    """A sync update during the final remote read must still block plaintext send."""
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    client.access_token = TEST_ACCESS_TOKEN
    client.rooms = {}
    plaintext_response = nio.RoomGetStateEventError(
        "No encryption state",
        "M_NOT_FOUND",
        room_id=room_id,
    )
    remote_reads = 0

    async def remote_encryption_state(*_args: object, **_kwargs: object) -> nio.RoomGetStateEventError:
        nonlocal remote_reads
        remote_reads += 1
        if remote_reads == 1:
            concurrent_room = nio.MatrixRoom(room_id, "@bot:localhost")
            concurrent_room.encrypted = True
            client.rooms[room_id] = concurrent_room
        return plaintext_response

    client.room_get_state_event = AsyncMock(side_effect=remote_encryption_state)
    client._send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id=room_id)

    delivered = await send_message_result(
        client,
        room_id,
        {"body": "hello", "msgtype": "m.text"},
        delivery_proof=RoomDeliveryHydrationProof(encrypted=False),
    )

    assert delivered is None
    assert remote_reads == 1
    client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_hydration_rejects_success_typed_transport_failure() -> None:
    """A non-2xx state-event response must not poison persistent encryption state."""
    client = MagicMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    response = nio.RoomGetStateEventResponse(
        content={"errcode": "M_UNKNOWN", "error": "upstream unavailable"},
        event_type="m.room.encryption",
        state_key="",
        room_id=room_id,
    )
    transport = MagicMock()
    transport.status = 502
    response.transport_response = transport
    client.rooms = {}
    client.encrypted_rooms = set()
    client.store = MagicMock()
    client.room_get_state_event = AsyncMock(return_value=response)
    client.joined_members = AsyncMock()

    proof = await hydrate_joined_room_for_delivery(client, room_id)

    assert proof is None
    assert room_id not in client.rooms
    assert room_id not in client.encrypted_rooms
    client.store.save_encrypted_rooms.assert_not_called()
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_hydration_requires_encryption_in_authoritative_full_state() -> None:
    """A positive state-event probe must agree with the full room state before persistence."""
    client = MagicMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    client.user_id = "@bot:localhost"
    client.rooms = {}
    client.encrypted_rooms = set()
    client.store = MagicMock()
    client.olm = None
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={"algorithm": "m.megolm.v1.aes-sha2"},
            event_type="m.room.encryption",
            state_key="",
            room_id=room_id,
        ),
    )
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(members=[], room_id=room_id),
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(events=[], room_id=room_id),
    )

    proof = await hydrate_joined_room_for_delivery(client, room_id)

    assert proof is None
    assert room_id not in client.rooms
    assert room_id not in client.encrypted_rooms
    client.store.save_encrypted_rooms.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_result_forwards_explicit_transaction_id() -> None:
    """A caller-owned transaction ID must reach nio unchanged."""
    client = _mock_client()

    await send_message_result(
        client,
        "!room:localhost",
        {"body": "hello", "msgtype": "m.text"},
        transaction_id="stable-recovery-transaction",
    )

    assert client.room_send.await_args.kwargs["tx_id"] == "stable-recovery-transaction"


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
