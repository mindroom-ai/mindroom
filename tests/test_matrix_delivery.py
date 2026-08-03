"""Tests for Matrix delivery trust behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.matrix.client_delivery import (
    DeliveredMatrixEvent,
    RoomDeliveryHydrationProof,
    build_edit_event_content,
    hydrate_joined_room_for_delivery,
    send_message_result,
    send_room_event_result,
)
from mindroom.matrix.client_session import _MindRoomAsyncClient
from tests.conftest import TEST_ACCESS_TOKEN


def _mock_client(*, encrypted: bool = False) -> AsyncMock:
    """Create a mock Matrix client with one room."""
    client = AsyncMock(spec=nio.AsyncClient)
    room = nio.MatrixRoom("!room:localhost", "@bot:localhost", encrypted=encrypted)
    room.members_synced = True
    room.add_member("@bot:localhost", "Bot", None)
    client.rooms = {"!room:localhost": room}
    client.encrypted_rooms = set()
    client.sharing_session = {}
    client.olm = None
    client.store = None
    client.should_query_keys = False
    client.users_for_key_query = set()
    client.joined_members.return_value = nio.JoinedMembersResponse(
        [nio.RoomMember("@bot:localhost", "Bot", "")],
        "!room:localhost",
    )
    client.room_get_state_event.return_value = nio.RoomGetStateEventError(
        "not found",
        status_code="M_NOT_FOUND",
    )
    client.room_send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id="!room:localhost")
    return client


def _encrypted_client_with_outbound_session(
    *,
    room_id: str,
    user_ids: frozenset[str],
    session_shared: bool = True,
) -> tuple[nio.AsyncClient, nio.MatrixRoom]:
    """Return one real nio room backed by an existing outbound session."""
    bot_user_id = "@bot:localhost"
    client = _MindRoomAsyncClient("https://localhost", bot_user_id)
    client.access_token = TEST_ACCESS_TOKEN
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    for user_id in user_ids:
        room.add_member(user_id, user_id, None)
    client.rooms[room_id] = room
    client.encrypted_rooms.add(room_id)
    client.store = MagicMock()
    client.olm = MagicMock()
    client.olm.outbound_group_sessions = {
        room_id: SimpleNamespace(shared=session_shared),
    }
    client.olm.users_for_key_query = set()
    client.olm.should_query_keys = False
    return client, room


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
async def test_cached_plaintext_room_is_hydrated_before_encrypted_delivery() -> None:
    """A pre-sync join cache cannot make an encrypted room send plaintext."""
    room_id = "!room:localhost"
    bot_user_id = "@bot:localhost"
    client = _MindRoomAsyncClient("https://localhost", bot_user_id)
    client.access_token = TEST_ACCESS_TOKEN
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=False)
    client.rooms[room_id] = room
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            {"algorithm": "m.megolm.v1.aes-sha2"},
            "m.room.encryption",
            "",
            room_id,
        ),
    )
    joined_response = nio.JoinedMembersResponse(
        [nio.RoomMember(bot_user_id, "Bot", "")],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(joined_response)
        return joined_response

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.olm = MagicMock()
    client.olm.users_for_key_query = set()
    client.olm.outbound_group_sessions = {}
    client.olm.should_query_keys = False
    client.olm.should_share_group_session.return_value = True
    new_session = SimpleNamespace(shared=True)

    async def share_group_session(*_args: object, **_kwargs: object) -> nio.ShareGroupSessionResponse:
        client.olm.outbound_group_sessions[room_id] = new_session
        return nio.ShareGroupSessionResponse(room_id, {bot_user_id})

    client.share_group_session = AsyncMock(side_effect=share_group_session)
    client.encrypt = MagicMock(
        return_value=("m.room.encrypted", {"ciphertext": "encrypted-welcome"}),
    )
    client._send = AsyncMock(return_value=nio.RoomSendResponse("$sent", room_id))

    response = await send_room_event_result(
        client,
        room_id,
        "m.room.message",
        {"body": "welcome", "msgtype": "m.text"},
    )

    assert isinstance(response, nio.RoomSendResponse)
    assert room.encrypted
    assert room.members_synced
    assert set(room.users) == {bot_user_id}
    client.room_get_state_event.assert_awaited_once_with(room_id, "m.room.encryption")
    client.encrypt.assert_called_once()
    assert "encrypted-welcome" in client._send.await_args.args[3]


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
    client.encrypted_rooms = {room_id}
    client.sharing_session = {}
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
        **_kwargs: object,
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


@pytest.mark.asyncio
async def test_encrypted_proof_refreshes_membership_after_preparation() -> None:
    """Proof-bound encrypted sends must refresh membership after payload preparation."""
    room_id = "!room:localhost"
    before = frozenset({"@bot:localhost", "@departed:localhost"})
    client, _room = _encrypted_client_with_outbound_session(room_id=room_id, user_ids=before)
    proof = RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=before)
    response = nio.JoinedMembersResponse(
        [nio.RoomMember("@bot:localhost", "Bot", "")],
        room_id,
    )
    observed: list[str] = []

    async def prepare(
        _client: nio.AsyncClient,
        _room_id: str,
        content: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        observed.append("prepare")
        return content

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        assert observed == ["prepare"]
        observed.append("members")
        client._handle_joined_members(response)
        return response

    async def send_prepared(*_args: object, **_kwargs: object) -> nio.RoomSendResponse:
        assert client.olm is not None
        assert room_id not in client.olm.outbound_group_sessions
        observed.append("send")
        return nio.RoomSendResponse(event_id="$event:localhost", room_id=room_id)

    client.joined_members = AsyncMock(side_effect=joined_members)
    with (
        patch(
            "mindroom.matrix.client_delivery.prepare_large_message",
            new=AsyncMock(side_effect=prepare),
        ),
        patch(
            "mindroom.matrix.client_delivery._send_prepared_room_message",
            new=AsyncMock(side_effect=send_prepared),
        ),
    ):
        delivered = await send_message_result(
            client,
            room_id,
            {"body": "resume", "msgtype": "m.text"},
            delivery_proof=proof,
        )

    assert delivered is not None
    assert delivered.event_id == "$event:localhost"
    assert observed == ["prepare", "members", "send"]


@pytest.mark.asyncio
async def test_implicit_encrypted_send_refreshes_membership_after_preparation() -> None:
    """Ordinary encrypted sends must not trust a stale complete-looking roster."""
    room_id = "!room:localhost"
    before = frozenset({"@bot:localhost", "@departed:localhost"})
    client, _room = _encrypted_client_with_outbound_session(room_id=room_id, user_ids=before)
    response = nio.JoinedMembersResponse(
        [nio.RoomMember("@bot:localhost", "Bot", "")],
        room_id,
    )
    observed: list[str] = []

    async def prepare(
        _client: nio.AsyncClient,
        _room_id: str,
        content: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        observed.append("prepare")
        await asyncio.sleep(0)
        return content

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        observed.append("members")
        client._handle_joined_members(response)
        return response

    async def send_prepared(*_args: object, **_kwargs: object) -> nio.RoomSendResponse:
        assert client.olm is not None
        assert room_id not in client.olm.outbound_group_sessions
        observed.append("send")
        return nio.RoomSendResponse(event_id="$event:localhost", room_id=room_id)

    client.joined_members = AsyncMock(side_effect=joined_members)
    with (
        patch(
            "mindroom.matrix.client_delivery.prepare_large_message",
            new=AsyncMock(side_effect=prepare),
        ),
        patch(
            "mindroom.matrix.client_delivery._send_prepared_room_message",
            new=AsyncMock(side_effect=send_prepared),
        ),
    ):
        delivered = await send_message_result(
            client,
            room_id,
            {"body": "resume", "msgtype": "m.text"},
        )

    assert delivered is not None
    assert observed == ["prepare", "members", "send"]
    client.joined_members.assert_awaited_once_with(room_id)


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
    client.encrypted_rooms = set()
    client.sharing_session = {}
    client.olm = None
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
async def test_stale_plaintext_proof_rejects_before_large_message_upload() -> None:
    """A stale plaintext proof must fail before preparation uploads content."""
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    client.rooms = {}
    client.encrypted_rooms = set()
    client.access_token = TEST_ACCESS_TOKEN
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={"algorithm": "m.megolm.v1.aes-sha2"},
            event_type="m.room.encryption",
            state_key="",
            room_id=room_id,
        ),
    )
    upload = AsyncMock(
        return_value=(nio.UploadResponse("mxc://localhost/orphan"), None),
    )

    with patch("mindroom.matrix.large_messages.upload_media_bytes", upload):
        delivered = await send_message_result(
            client,
            room_id,
            {"body": "private recovery payload " * 5000, "msgtype": "m.text"},
            delivery_proof=RoomDeliveryHydrationProof(encrypted=False),
        )

    assert delivered is None
    upload.assert_not_awaited()
    client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_message_rejects_plaintext_sidecar_after_encryption_transition() -> None:
    """A sidecar prepared as plaintext must not be referenced after encryption turns on."""
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    room = nio.MatrixRoom(room_id, "@bot:localhost")
    room.members_synced = True
    room.add_member("@bot:localhost", "Bot", None)
    client.rooms = {room_id: room}
    client.encrypted_rooms = set()
    client.sharing_session = {}
    client.olm = None
    client.room_get_state_event.return_value = nio.RoomGetStateEventError(
        "not found",
        status_code="M_NOT_FOUND",
    )
    client.room_send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id=room_id)
    uploaded_bytes: bytes | None = None

    async def upload_and_enable_encryption(
        _client: nio.AsyncClient,
        data: bytes,
        **_kwargs: object,
    ) -> tuple[nio.UploadResponse, None]:
        nonlocal uploaded_bytes
        uploaded_bytes = data
        room.encrypted = True
        client.encrypted_rooms.add(room_id)
        return nio.UploadResponse("mxc://localhost/plaintext-sidecar"), None

    with patch(
        "mindroom.matrix.large_messages.upload_media_bytes",
        new=AsyncMock(side_effect=upload_and_enable_encryption),
    ):
        delivered = await send_message_result(
            client,
            room_id,
            {"body": "private recovery payload " * 5000, "msgtype": "m.text"},
        )

    assert uploaded_bytes is not None
    assert b"private recovery payload" in uploaded_bytes
    assert delivered is None
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_plaintext_proof_rechecks_cache_after_final_remote_query() -> None:
    """A sync update during the final remote read must still block plaintext send."""
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    client.access_token = TEST_ACCESS_TOKEN
    client.rooms = {}
    client.encrypted_rooms = set()
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


@pytest.mark.parametrize("cached_plaintext", [False, True])
@pytest.mark.asyncio
async def test_initial_plaintext_hydration_rechecks_cache_before_large_message_upload(
    *,
    cached_plaintext: bool,
) -> None:
    """A sync encryption update during the initial probe must precede payload upload."""
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    room = nio.MatrixRoom(room_id, "@bot:localhost")
    room.members_synced = True
    room.add_member("@bot:localhost", "Bot", None)
    client.rooms = {room_id: room} if cached_plaintext else {}
    client.encrypted_rooms = set()
    client.sharing_session = {}
    client.olm = None
    client.store = None
    client.access_token = TEST_ACCESS_TOKEN

    async def stale_plaintext_probe(*_args: object, **_kwargs: object) -> nio.RoomGetStateEventError:
        if not cached_plaintext:
            client.rooms[room_id] = room
        room.encrypted = True
        client.encrypted_rooms.add(room_id)
        return nio.RoomGetStateEventError(
            "No encryption state",
            "M_NOT_FOUND",
            room_id=room_id,
        )

    client.room_get_state_event = AsyncMock(side_effect=stale_plaintext_probe)
    uploaded_payloads: list[bytes] = []

    async def capture_upload(_client: nio.AsyncClient, payload: bytes, **_kwargs: object) -> tuple[object, None]:
        uploaded_payloads.append(payload)
        return nio.UploadResponse("mxc://localhost/unsafe"), None

    upload = AsyncMock(side_effect=capture_upload)

    with patch("mindroom.matrix.large_messages.upload_media_bytes", upload):
        delivered = await send_message_result(
            client,
            room_id,
            {"body": "SENSITIVE " * 20_000, "msgtype": "m.text"},
        )

    assert delivered is None
    assert all(b"SENSITIVE" not in payload for payload in uploaded_payloads)
    if not cached_plaintext:
        upload.assert_not_awaited()
    client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncached_known_encrypted_room_never_uses_plaintext_cache_bypass() -> None:
    """Nio's monotonic encrypted-room record must dominate a stale negative probe."""
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    client.access_token = TEST_ACCESS_TOKEN
    client.rooms = {}
    client.encrypted_rooms = {room_id}
    client.sharing_session = {}
    client.olm = None
    client.store = None
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventError("not found", status_code="M_NOT_FOUND"),
    )
    client._send.return_value = nio.RoomSendResponse("$unsafe", room_id)

    response = await send_room_event_result(
        client,
        room_id,
        "m.room.message",
        {"body": "SECRET", "msgtype": "m.text"},
    )

    assert response is None
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
async def test_hidden_room_hydration_rejects_newer_full_state_membership() -> None:
    """A newer full-state departure must supersede an older joined-members roster."""
    client = MagicMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    bot_user_id = "@bot:localhost"
    departed_user_id = "@departed:localhost"
    client.user_id = bot_user_id
    client.rooms = {}
    client.encrypted_rooms = set()
    client.sharing_session = {}
    client.store = MagicMock()
    client.olm = None
    client.should_query_keys = False
    client.users_for_key_query = set()
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            {"algorithm": "m.megolm.v1.aes-sha2"},
            "m.room.encryption",
            "",
            room_id,
        ),
    )
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            [
                nio.RoomMember(bot_user_id, "Bot", ""),
                nio.RoomMember(departed_user_id, "Departed", ""),
            ],
            room_id,
        ),
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            [
                {
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                    "event_id": "$encryption",
                    "origin_server_ts": 1,
                    "sender": bot_user_id,
                    "state_key": "",
                    "type": "m.room.encryption",
                },
                {
                    "content": {"displayname": "Bot", "membership": "join"},
                    "event_id": "$bot-join",
                    "origin_server_ts": 2,
                    "sender": bot_user_id,
                    "state_key": bot_user_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"membership": "leave"},
                    "event_id": "$departure",
                    "origin_server_ts": 3,
                    "sender": departed_user_id,
                    "state_key": departed_user_id,
                    "type": "m.room.member",
                },
            ],
            room_id,
        ),
    )

    proof = await hydrate_joined_room_for_delivery(client, room_id)

    assert proof is None
    assert room_id not in client.rooms
    client.store.save_encrypted_rooms.assert_not_called()


@pytest.mark.asyncio
async def test_hidden_room_hydration_rejects_membership_change_during_full_state_query() -> None:
    """The joined roster must remain current until hidden-room state is validated."""
    room_id = "!room:localhost"
    bot_user_id = "@bot:localhost"
    departed_user_id = "@departed:localhost"
    client = _MindRoomAsyncClient("https://localhost", bot_user_id)
    client.access_token = TEST_ACCESS_TOKEN
    client.encrypted_rooms.add(room_id)
    client.store = MagicMock()
    client.olm = None
    stale_members = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(departed_user_id, "Departed", ""),
        ],
        room_id,
    )
    newer_members = nio.JoinedMembersResponse(
        [nio.RoomMember(bot_user_id, "Bot", "")],
        room_id,
    )
    state_query_started = asyncio.Event()
    release_state_query = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(stale_members)
        return stale_members

    async def room_get_state(_room_id: str) -> nio.RoomGetStateResponse:
        state_query_started.set()
        await release_state_query.wait()
        return nio.RoomGetStateResponse(
            [
                {
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                    "event_id": "$encryption",
                    "origin_server_ts": 1,
                    "sender": bot_user_id,
                    "state_key": "",
                    "type": "m.room.encryption",
                },
                {
                    "content": {"displayname": "Bot", "membership": "join"},
                    "event_id": "$bot-join",
                    "origin_server_ts": 2,
                    "sender": bot_user_id,
                    "state_key": bot_user_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"displayname": "Departed", "membership": "join"},
                    "event_id": "$departed-join",
                    "origin_server_ts": 3,
                    "sender": departed_user_id,
                    "state_key": departed_user_id,
                    "type": "m.room.member",
                },
            ],
            room_id,
        )

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.room_get_state = AsyncMock(side_effect=room_get_state)

    hydration = asyncio.create_task(hydrate_joined_room_for_delivery(client, room_id))
    await state_query_started.wait()
    client._handle_joined_members(newer_members)
    release_state_query.set()

    assert await hydration is None
    assert room_id not in client.rooms
    client.store.save_encrypted_rooms.assert_not_called()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            frozenset({"@bot:localhost", "@departed:localhost"}),
            frozenset({"@bot:localhost"}),
        ),
        (
            frozenset({"@bot:localhost"}),
            frozenset({"@bot:localhost", "@joined:localhost"}),
        ),
    ],
)
@pytest.mark.parametrize("session_shared", [False, True])
@pytest.mark.asyncio
async def test_encrypted_hydration_retires_outbound_session_when_membership_changes(
    before: frozenset[str],
    after: frozenset[str],
    *,
    session_shared: bool,
) -> None:
    """Authoritative membership changes must retire every exposed session."""
    room_id = "!room:localhost"
    client, room = _encrypted_client_with_outbound_session(
        room_id=room_id,
        user_ids=before,
        session_shared=session_shared,
    )
    response = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, user_id, "") for user_id in sorted(after)],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    client.joined_members = AsyncMock(side_effect=joined_members)

    proof = await hydrate_joined_room_for_delivery(client, room_id)

    assert proof == RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=after)
    assert frozenset(user_id for user_id, user in room.users.items() if not user.invited) == after
    assert client.olm is not None
    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.asyncio
async def test_encrypted_hydration_preserves_session_when_membership_is_unchanged() -> None:
    """An authoritative no-op membership refresh must preserve the shared session."""
    room_id = "!room:localhost"
    joined_user_ids = frozenset({"@bot:localhost", "@joined:localhost"})
    client, _room = _encrypted_client_with_outbound_session(
        room_id=room_id,
        user_ids=joined_user_ids,
    )
    assert client.olm is not None
    old_session = client.olm.outbound_group_sessions[room_id]
    response = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, user_id, "") for user_id in sorted(joined_user_ids)],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    client.joined_members = AsyncMock(side_effect=joined_members)

    proof = await hydrate_joined_room_for_delivery(client, room_id)

    assert proof == RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=joined_user_ids)
    assert client.olm.outbound_group_sessions[room_id] is old_session


@pytest.mark.asyncio
async def test_membership_change_fences_sends_before_failed_key_query() -> None:
    """A changed room must retire its shared session before key readiness awaits."""
    room_id = "!room:localhost"
    joined_user_id = "@joined:localhost"
    before = frozenset({"@bot:localhost", "@departed:localhost"})
    after = frozenset({"@bot:localhost", joined_user_id})
    client, room = _encrypted_client_with_outbound_session(room_id=room_id, user_ids=before)
    response = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, user_id, "") for user_id in sorted(after)],
        room_id,
    )
    assert client.olm is not None
    client.olm.users_for_key_query = {joined_user_id}
    client.olm.should_query_keys = True
    query_started = asyncio.Event()
    release_query = asyncio.Event()
    state_during_query: tuple[bool, bool] | None = None

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    async def keys_query() -> nio.KeysQueryError:
        nonlocal state_during_query
        state_during_query = (
            room_id in client.olm.outbound_group_sessions,
            room.members_synced,
        )
        query_started.set()
        await release_query.wait()
        return nio.KeysQueryError("upstream unavailable", "M_UNKNOWN")

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.keys_query = AsyncMock(side_effect=keys_query)

    hydration = asyncio.create_task(hydrate_joined_room_for_delivery(client, room_id))
    await query_started.wait()

    assert state_during_query == (False, False)
    release_query.set()
    assert await hydration is None
    assert room_id not in client.olm.outbound_group_sessions
    assert room.members_synced is False


@pytest.mark.asyncio
async def test_concurrent_send_waits_for_failed_membership_key_readiness() -> None:
    """Every application send must wait for and honor room key readiness."""
    room_id = "!room:localhost"
    joined_user_id = "@joined:localhost"
    before = frozenset({"@bot:localhost", "@departed:localhost"})
    after = frozenset({"@bot:localhost", joined_user_id})
    client, _room = _encrypted_client_with_outbound_session(room_id=room_id, user_ids=before)
    response = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, user_id, "") for user_id in sorted(after)],
        room_id,
    )
    assert client.olm is not None
    client.olm.users_for_key_query = {joined_user_id}
    client.olm.should_query_keys = True
    first_query_started = asyncio.Event()
    release_first_query = asyncio.Event()
    key_query_count = 0

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    async def keys_query() -> nio.KeysQueryError:
        nonlocal key_query_count
        key_query_count += 1
        if key_query_count == 1:
            first_query_started.set()
            await release_first_query.wait()
        return nio.KeysQueryError("upstream unavailable", "M_UNKNOWN")

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.keys_query = AsyncMock(side_effect=keys_query)
    client._send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$unsafe:localhost", room_id=room_id),
    )
    client.encrypt = MagicMock(return_value=("m.room.encrypted", {"ciphertext": "unsafe"}))

    hydration = asyncio.create_task(hydrate_joined_room_for_delivery(client, room_id))
    await first_query_started.wait()
    send = asyncio.create_task(
        send_message_result(client, room_id, {"body": "secret", "msgtype": "m.text"}),
    )
    await asyncio.sleep(0)
    raw_send_count_while_blocked = client._send.await_count

    release_first_query.set()
    hydration_result = await hydration
    send_result = await send

    assert raw_send_count_while_blocked == 0
    assert hydration_result is None
    assert send_result is None
    assert key_query_count == 2
    client.encrypt.assert_not_called()
    client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_send_runs_after_successful_membership_key_readiness() -> None:
    """A queued real-nio send may reach the wire only after readiness succeeds."""
    room_id = "!room:localhost"
    joined_user_ids = frozenset({"@bot:localhost", "@joined:localhost"})
    client, _room = _encrypted_client_with_outbound_session(room_id=room_id, user_ids=joined_user_ids)
    response = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, user_id, "") for user_id in sorted(joined_user_ids)],
        room_id,
    )
    assert client.olm is not None
    client.olm.users_for_key_query = {"@joined:localhost"}
    client.olm.should_query_keys = True
    client.olm.should_share_group_session.return_value = False
    query_started = asyncio.Event()
    release_query = asyncio.Event()
    order: list[str] = []

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    async def keys_query() -> nio.KeysQueryResponse:
        order.append("query_started")
        query_started.set()
        await release_query.wait()
        client.olm.users_for_key_query.clear()
        client.olm.should_query_keys = False
        order.append("query_ready")
        return nio.KeysQueryResponse(device_keys={}, failures={})

    async def wire_send(*_args: object, **_kwargs: object) -> nio.RoomSendResponse:
        order.append("wire_send")
        return nio.RoomSendResponse(event_id="$sent:localhost", room_id=room_id)

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.keys_query = AsyncMock(side_effect=keys_query)
    client.encrypt = MagicMock(return_value=("m.room.encrypted", {"ciphertext": "safe"}))
    client._send = AsyncMock(side_effect=wire_send)

    hydration = asyncio.create_task(hydrate_joined_room_for_delivery(client, room_id))
    await query_started.wait()
    send = asyncio.create_task(
        send_message_result(client, room_id, {"body": "secret", "msgtype": "m.text"}),
    )
    await asyncio.sleep(0)
    client._send.assert_not_awaited()

    release_query.set()

    assert await hydration == RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=joined_user_ids)
    assert await send == DeliveredMatrixEvent(
        event_id="$sent:localhost",
        content_sent={"body": "secret", "msgtype": "m.text"},
    )
    assert order == ["query_started", "query_ready", "wire_send"]


@pytest.mark.asyncio
async def test_key_query_accepts_its_own_device_session_invalidation() -> None:
    """A successful key response may rotate its own stale outbound session once."""
    room_id = "!room:localhost"
    human_user_id = "@human:localhost"
    joined_user_ids = frozenset({"@bot:localhost", human_user_id})
    client, _room = _encrypted_client_with_outbound_session(
        room_id=room_id,
        user_ids=joined_user_ids,
    )
    assert client.olm is not None
    pending_key_user_ids = {human_user_id}
    client.olm.users_for_key_query = pending_key_user_ids
    client.olm.should_query_keys = True
    joined_response = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, user_id, "") for user_id in sorted(joined_user_ids)],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(joined_response)
        return joined_response

    async def keys_query() -> nio.KeysQueryResponse:
        response = nio.KeysQueryResponse({}, {})
        response.changed = {human_user_id: {}}
        client.olm.handle_response.side_effect = lambda _response: pending_key_user_ids.clear()
        await client.receive_response(response)
        client.olm.should_query_keys = False
        return response

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.keys_query = AsyncMock(side_effect=keys_query)

    proof = await hydrate_joined_room_for_delivery(client, room_id)

    assert proof == RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=joined_user_ids)
    assert not pending_key_user_ids
    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.asyncio
async def test_blocked_hydration_does_not_block_another_room() -> None:
    """Per-room delivery locks must not serialize unrelated rooms."""
    encrypted_room_id = "!encrypted:localhost"
    plaintext_room_id = "!plaintext:localhost"
    bot_user_id = "@bot:localhost"
    client, _room = _encrypted_client_with_outbound_session(
        room_id=encrypted_room_id,
        user_ids=frozenset({bot_user_id}),
    )
    client.rooms[plaintext_room_id] = nio.MatrixRoom(plaintext_room_id, bot_user_id)
    response = nio.JoinedMembersResponse([nio.RoomMember(bot_user_id, "Bot", "")], encrypted_room_id)
    assert client.olm is not None
    client.olm.users_for_key_query = {bot_user_id}
    client.olm.should_query_keys = True
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    async def keys_query() -> nio.KeysQueryError:
        query_started.set()
        await release_query.wait()
        return nio.KeysQueryError("upstream unavailable", "M_UNKNOWN")

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.keys_query = AsyncMock(side_effect=keys_query)
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventError("not found", status_code="M_NOT_FOUND"),
    )
    client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse("$plaintext:localhost", plaintext_room_id),
    )

    hydration = asyncio.create_task(hydrate_joined_room_for_delivery(client, encrypted_room_id))
    await query_started.wait()

    try:
        plaintext_result = await asyncio.wait_for(
            send_room_event_result(
                client,
                plaintext_room_id,
                "m.reaction",
                {"m.relates_to": {}},
            ),
            timeout=1.0,
        )
    finally:
        release_query.set()

    assert isinstance(plaintext_result, nio.RoomSendResponse)
    assert await hydration is None


@pytest.mark.asyncio
async def test_sync_recovery_retry_rehydrates_after_readiness_rejection() -> None:
    """Recovery backoff must release the room lock and revalidate before retrying."""
    room_id = "!room:localhost"
    bot_user_id = "@bot:localhost"
    joined_user_ids = frozenset({bot_user_id})
    client, room = _encrypted_client_with_outbound_session(room_id=room_id, user_ids=joined_user_ids)
    response = nio.JoinedMembersResponse([nio.RoomMember(bot_user_id, "Bot", "")], room_id)
    send_count = 0
    transaction_ids: list[str | None] = []

    async def room_send(**kwargs: object) -> nio.RoomSendResponse:
        nonlocal send_count
        send_count += 1
        tx_id = kwargs.get("tx_id")
        transaction_ids.append(tx_id if isinstance(tx_id, str) else None)
        if send_count == 1:
            room.members_synced = False
            message = "Encrypted room delivery readiness must be refreshed before sending."
            raise nio.SendRetryError(message)
        return nio.RoomSendResponse("$sent:localhost", room_id)

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    client.room_send = AsyncMock(side_effect=room_send)
    client.joined_members = AsyncMock(side_effect=joined_members)

    with patch("mindroom.matrix.client_delivery.asyncio.sleep", new=AsyncMock()):
        delivered = await send_message_result(
            client,
            room_id,
            {"body": "secret", "msgtype": "m.text"},
            retry_sync_recovery=True,
        )

    assert delivered == DeliveredMatrixEvent(
        event_id="$sent:localhost",
        content_sent={"body": "secret", "msgtype": "m.text"},
    )
    assert client.joined_members.await_count == 2
    assert len(transaction_ids) == 2
    assert transaction_ids[0] is not None
    assert transaction_ids[0] == transaction_ids[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_shared", [False, True])
async def test_encrypted_hydration_excludes_invitees_from_session_roster(session_shared: bool) -> None:
    """Invitees must not appear in the nio roster used for Megolm sharing."""
    room_id = "!room:localhost"
    bot_user_id = "@bot:localhost"
    invited_user_id = "@invited:localhost"
    joined_user_ids = frozenset({bot_user_id})
    client, room = _encrypted_client_with_outbound_session(
        room_id=room_id,
        user_ids=joined_user_ids,
        session_shared=session_shared,
    )
    room.add_member(invited_user_id, "Invited", None, invited=True)
    response = nio.JoinedMembersResponse(
        [nio.RoomMember(bot_user_id, "Bot", "")],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    client.joined_members = AsyncMock(side_effect=joined_members)

    proof = await hydrate_joined_room_for_delivery(client, room_id)

    assert proof == RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=joined_user_ids)
    assert set(room.users) == set(joined_user_ids)
    assert invited_user_id not in room.invited_users
    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.asyncio
async def test_encrypted_hydration_rejects_invitee_added_during_key_query() -> None:
    """A concurrent invite cannot enter the final Megolm recipient roster."""
    room_id = "!room:localhost"
    bot_user_id = "@bot:localhost"
    invitee_user_id = "@invitee:localhost"
    joined_user_ids = frozenset({bot_user_id})
    client, room = _encrypted_client_with_outbound_session(room_id=room_id, user_ids=joined_user_ids)
    response = nio.JoinedMembersResponse([nio.RoomMember(bot_user_id, "Bot", "")], room_id)
    assert client.olm is not None
    client.olm.users_for_key_query = {bot_user_id}
    client.olm.should_query_keys = True

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    async def keys_query() -> nio.KeysQueryResponse:
        client.olm.users_for_key_query.clear()
        room.add_member(invitee_user_id, "Invitee", None, invited=True)
        return nio.KeysQueryResponse(device_keys={}, failures={})

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.keys_query = AsyncMock(side_effect=keys_query)

    assert await hydrate_joined_room_for_delivery(client, room_id) is None
    assert room.members_synced is False
    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.asyncio
async def test_encrypted_hydration_promotes_authoritatively_joined_invitee() -> None:
    """A joined-members response must replace stale invite state and rotate its session."""
    room_id = "!room:localhost"
    bot_user_id = "@bot:localhost"
    joined_user_id = "@joined:localhost"
    client, room = _encrypted_client_with_outbound_session(
        room_id=room_id,
        user_ids=frozenset({bot_user_id}),
    )
    room.add_member(joined_user_id, "Previously invited", None, invited=True)
    joined_user_ids = frozenset({bot_user_id, joined_user_id})
    response = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, user_id, "") for user_id in sorted(joined_user_ids)],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    client.joined_members = AsyncMock(side_effect=joined_members)

    proof = await hydrate_joined_room_for_delivery(client, room_id)

    assert proof == RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=joined_user_ids)
    assert set(room.users) == set(joined_user_ids)
    assert not room.users[joined_user_id].invited
    assert not room.invited_users
    assert room_id not in client.olm.outbound_group_sessions


def test_production_room_sends_use_the_delivery_boundary() -> None:
    """Application modules must not bypass the centralized room-send gate."""
    source_root = Path(__file__).parents[1] / "src" / "mindroom"
    bypasses = [
        str(path.relative_to(source_root))
        for path in source_root.rglob("*.py")
        if path.name != "client_delivery.py" and ".room_send(" in path.read_text(encoding="utf-8")
    ]

    assert bypasses == []


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
