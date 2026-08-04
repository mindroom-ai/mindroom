"""Tests for MindRoom-specific Matrix client behavior."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import nio
import pytest

from mindroom.constants import (
    CONFIG_CONFIRMATION_REACTION_KEY,
    STREAM_STATUS_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
    RuntimePaths,
)
from mindroom.matrix import client_session
from mindroom.matrix.client_delivery import send_room_event_result
from mindroom.matrix.client_session import (
    PermanentMatrixStartupError,
    _MindRoomAsyncClient,
    login_flows,
    login_with_token,
    matrix_client_config,
)
from mindroom.matrix.encryption_recipients import joined_members_query, room_membership_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("unready_state", ["members", "keys", "invitee"])
async def test_room_send_preparation_rejects_unready_encryption_roster(
    monkeypatch: pytest.MonkeyPatch,
    unready_state: str,
) -> None:
    """The runtime client must fail closed before nio can refresh or share keys."""
    room_id = "!room:example.org"
    client = _MindRoomAsyncClient("https://example.org", "@bot:example.org")
    room = nio.MatrixRoom(room_id, client.user_id, encrypted=True)
    room.members_synced = unready_state != "members"
    room.add_member(client.user_id, "Bot", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.users_for_key_query = {client.user_id} if unready_state == "keys" else set()
    if unready_state == "invitee":
        room.add_member("@invitee:example.org", "Invitee", None, invited=True)
    prepare = AsyncMock(return_value=("PUT", "/send", "{}"))
    monkeypatch.setattr(nio.AsyncClient, "_prepare_room_send", prepare)

    with pytest.raises(nio.SendRetryError):
        await client._prepare_room_send(
            room_id,
            "m.room.message",
            {"body": "secret", "msgtype": "m.text"},
            UUID(int=0),
            True,
        )

    prepare.assert_not_awaited()


def test_joined_members_response_removes_encrypted_room_invitees() -> None:
    """The runtime crypto roster must contain joined members only."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    invitee_user_id = "@invitee:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(invitee_user_id, "Invitee", None, invited=True)
    client.rooms[room_id] = room

    client._handle_joined_members(
        nio.JoinedMembersResponse(
            [nio.RoomMember(bot_user_id, "Bot", "")],
            room_id,
        ),
    )

    assert set(room.users) == {bot_user_id}
    assert not room.invited_users


def test_joined_members_response_promotes_invitee_in_authoritative_roster() -> None:
    """An authoritative joined response must replace nio's invited user object."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    joined_user_id = "@joined:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(joined_user_id, "Joined", None, invited=True)
    client.rooms[room_id] = room

    client._handle_joined_members(
        nio.JoinedMembersResponse(
            [
                nio.RoomMember(bot_user_id, "Bot", ""),
                nio.RoomMember(joined_user_id, "Joined", ""),
            ],
            room_id,
        ),
    )

    assert set(room.users) == {bot_user_id, joined_user_id}
    assert not room.users[joined_user_id].invited
    assert not room.invited_users


@pytest.mark.parametrize("session_shared", [False, True])
def test_joined_members_response_retires_session_when_recipients_change(session_shared: bool) -> None:
    """Every joined-members consumer must rotate sessions after a departure."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.outbound_group_sessions = {
        room_id: SimpleNamespace(shared=session_shared),
    }

    client._handle_joined_members(
        nio.JoinedMembersResponse(
            [nio.RoomMember(bot_user_id, "Bot", "")],
            room_id,
        ),
    )

    assert set(room.users) == {bot_user_id}
    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.asyncio
async def test_sync_response_removes_encrypted_room_invitees() -> None:
    """A joined-room sync must not leave invitees in the runtime crypto roster."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    invitee_user_id = "@invitee:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.rooms[room_id] = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    invite_event = nio.Event.parse_event(
        {
            "content": {"displayname": "Invitee", "membership": "invite"},
            "event_id": "$invite",
            "origin_server_ts": 1,
            "sender": bot_user_id,
            "state_key": invitee_user_id,
            "type": "m.room.member",
        },
    )
    response = nio.SyncResponse(
        "s_next",
        nio.Rooms(
            invite={},
            join={
                room_id: nio.RoomInfo(
                    nio.Timeline([], limited=False, prev_batch=None),
                    state=[invite_event],
                    ephemeral=[],
                    account_data=[],
                ),
            },
            leave={},
        ),
        nio.DeviceOneTimeKeyCount(None, None),
        nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
    )

    await client._handle_joined_rooms(response)

    assert invitee_user_id not in client.rooms[room_id].users
    assert not client.rooms[room_id].invited_users


@pytest.mark.asyncio
async def test_sync_invitee_is_removed_before_timeline_callback_yields() -> None:
    """A concurrent send cannot observe an invitee during an awaited sync callback."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    invitee_user_id = "@invitee:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.rooms[room_id] = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    invite_event = nio.Event.parse_event(
        {
            "content": {"displayname": "Invitee", "membership": "invite"},
            "event_id": "$invite",
            "origin_server_ts": 1,
            "sender": bot_user_id,
            "state_key": invitee_user_id,
            "type": "m.room.member",
        },
    )
    invitee_visible_during_callback: list[bool] = []

    async def on_event(_event: object, room: nio.MatrixRoom) -> None:
        invitee_visible_during_callback.append(invitee_user_id in room.users)
        await asyncio.sleep(0)

    client._on_event = AsyncMock(side_effect=on_event)
    response = nio.SyncResponse(
        "s_next",
        nio.Rooms(
            invite={},
            join={
                room_id: nio.RoomInfo(
                    nio.Timeline([invite_event], limited=False, prev_batch=None),
                    state=[],
                    ephemeral=[],
                    account_data=[],
                ),
            },
            leave={},
        ),
        nio.DeviceOneTimeKeyCount(None, None),
        nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
    )

    await client._handle_joined_rooms(response)

    assert invitee_visible_during_callback == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_kind", ["classic", "sliding"])
async def test_new_encrypted_recipient_is_keyed_before_callback_send(  # noqa: PLR0915
    sync_kind: str,
) -> None:
    """Every sync path must key a new recipient before a callback can send."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    joined_user_id = "@joined:example.org"
    client = _MindRoomAsyncClient(
        "https://example.org",
        bot_user_id,
        config=matrix_client_config(),
    )
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=sync_kind == "classic")
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    client.rooms[room_id] = room
    tracked_user_ids = {bot_user_id, joined_user_id}
    pending_key_user_ids: set[str] = set()
    new_session = SimpleNamespace(shared=True)
    client.olm = Mock()
    client.olm.clear_verifications.return_value = []
    client.olm.collect_key_requests.return_value = []
    client.olm.users_for_key_query = pending_key_user_ids
    client.olm.outbound_group_sessions = {
        room_id: SimpleNamespace(shared=True),
    }
    client.olm.should_query_keys = False
    client.olm.should_share_group_session.return_value = True
    order: list[str] = []

    def add_changed_users(user_ids: set[str]) -> None:
        order.append("device_change")
        pending_key_user_ids.update(user_ids)
        client.olm.should_query_keys = True

    def update_tracked_users(tracked_room: nio.MatrixRoom) -> None:
        missing = set(tracked_room.users).difference(tracked_user_ids)
        tracked_user_ids.update(missing)
        pending_key_user_ids.update(missing)

    client.olm.update_tracked_users.side_effect = update_tracked_users
    client.olm.add_changed_users.side_effect = add_changed_users
    joined_response = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(joined_user_id, "Joined", ""),
        ],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        order.append("joined_members")
        client._handle_joined_members(joined_response)
        return joined_response

    async def keys_query() -> nio.KeysQueryResponse:
        order.append("keys_query")
        pending_key_user_ids.clear()
        client.olm.should_query_keys = False
        return nio.KeysQueryResponse({}, {})

    async def share_group_session(*_args: object, **_kwargs: object) -> nio.ShareGroupSessionResponse:
        assert order == ["device_change", "joined_members", "keys_query", "joined_members"]
        assert not pending_key_user_ids
        assert set(room.users) == {bot_user_id, joined_user_id}
        order.append("share")
        client.olm.outbound_group_sessions[room_id] = new_session
        return nio.ShareGroupSessionResponse(room_id, {bot_user_id, joined_user_id})

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.keys_query = AsyncMock(side_effect=keys_query)
    client.share_group_session = AsyncMock(side_effect=share_group_session)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "ready"}))
    client._send = AsyncMock(return_value=nio.RoomSendResponse("$sent", room_id))
    callback_results: list[object | None] = []

    async def on_event(_event: object, _room: nio.MatrixRoom) -> None:
        callback_results.append(
            await send_room_event_result(
                client,
                room_id,
                "m.room.message",
                {"body": "secret", "msgtype": "m.text"},
            ),
        )

    client._on_event = AsyncMock(side_effect=on_event)
    join_event = nio.Event.parse_event(
        {
            "content": {"displayname": "Joined", "membership": "join"},
            "event_id": "$join",
            "origin_server_ts": 1,
            "sender": joined_user_id,
            "state_key": joined_user_id,
            "type": "m.room.member",
        },
    )
    if sync_kind == "classic":
        await client.receive_response(
            nio.SyncResponse(
                "s_next",
                nio.Rooms(
                    invite={},
                    join={
                        room_id: nio.RoomInfo(
                            nio.Timeline([join_event], limited=False, prev_batch=None),
                            state=[],
                            ephemeral=[],
                            account_data=[],
                        ),
                    },
                    leave={},
                ),
                nio.DeviceOneTimeKeyCount(None, None),
                nio.DeviceList(changed=[joined_user_id], left=[]),
                to_device_events=[],
                presence_events=[],
            ),
        )
    else:
        encryption_event = nio.Event.parse_event(
            {
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                "event_id": "$encryption",
                "origin_server_ts": 1,
                "sender": bot_user_id,
                "state_key": "",
                "type": "m.room.encryption",
            },
        )
        message_event = nio.Event.parse_event(
            {
                "content": {"body": "trigger", "msgtype": "m.text"},
                "event_id": "$message",
                "origin_server_ts": 2,
                "sender": joined_user_id,
                "type": "m.room.message",
            },
        )
        await client.receive_response(
            nio.SlidingSyncResponse(
                "p1",
                rooms={
                    room_id: nio.SlidingSyncRoom(
                        membership="join",
                        required_state=[join_event, encryption_event],
                        timeline=[message_event],
                        num_live=1,
                    ),
                },
                device_list=nio.DeviceList(changed=[joined_user_id], left=[]),
            ),
        )

    assert len(callback_results) == 1
    assert isinstance(callback_results[0], nio.RoomSendResponse)
    assert order[:5] == ["device_change", "joined_members", "keys_query", "joined_members", "share"]
    assert client.olm.outbound_group_sessions[room_id] is new_session


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_kind", ["classic", "sliding"])
async def test_changed_device_retires_session_before_timeline_callback_send(  # noqa: PLR0915
    sync_kind: str,
) -> None:
    """Device-list changes must rotate room sessions before same-response callbacks."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    human_user_id = "@human:example.org"
    client = _MindRoomAsyncClient(
        "https://example.org",
        bot_user_id,
        config=matrix_client_config(),
    )
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(human_user_id, "Human", None)
    client.rooms[room_id] = room
    old_session = SimpleNamespace(shared=True)
    new_session = SimpleNamespace(shared=True)
    pending_key_user_ids: set[str] = set()
    client.olm = Mock()
    client.olm.clear_verifications.return_value = []
    client.olm.collect_key_requests.return_value = []
    client.olm.users_for_key_query = pending_key_user_ids
    client.olm.outbound_group_sessions = {room_id: old_session}
    client.olm.should_query_keys = False
    client.olm.should_share_group_session.return_value = True
    order: list[str] = []

    def add_changed_users(user_ids: set[str]) -> None:
        order.append("device_change")
        pending_key_user_ids.update(user_ids)
        client.olm.should_query_keys = True

    client.olm.add_changed_users.side_effect = add_changed_users
    joined_response = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(human_user_id, "Human", ""),
        ],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        order.append("joined_members")
        client._handle_joined_members(joined_response)
        return joined_response

    async def keys_query() -> nio.KeysQueryResponse:
        order.append("keys_query")
        pending_key_user_ids.clear()
        client.olm.should_query_keys = False
        return nio.KeysQueryResponse({}, {})

    async def share_group_session(*_args: object, **_kwargs: object) -> nio.ShareGroupSessionResponse:
        assert room_id not in client.olm.outbound_group_sessions
        assert not pending_key_user_ids
        order.append("share")
        client.olm.outbound_group_sessions[room_id] = new_session
        return nio.ShareGroupSessionResponse(room_id, {bot_user_id, human_user_id})

    async def wire_send(*_args: object, **_kwargs: object) -> nio.RoomSendResponse:
        order.append("wire")
        return nio.RoomSendResponse("$sent", room_id)

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.keys_query = AsyncMock(side_effect=keys_query)
    client.share_group_session = AsyncMock(side_effect=share_group_session)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "new-session"}))
    client._send = AsyncMock(side_effect=wire_send)
    callback_results: list[object | None] = []

    async def on_event(_event: object, _room: nio.MatrixRoom) -> None:
        callback_results.append(
            await send_room_event_result(
                client,
                room_id,
                "m.room.message",
                {"body": "secret", "msgtype": "m.text"},
            ),
        )

    client._on_event = AsyncMock(side_effect=on_event)
    message_event = nio.Event.parse_event(
        {
            "content": {"body": "trigger", "msgtype": "m.text"},
            "event_id": "$message",
            "origin_server_ts": 1,
            "sender": human_user_id,
            "type": "m.room.message",
        },
    )
    device_list = nio.DeviceList(changed=[human_user_id], left=[])
    if sync_kind == "classic":
        response: nio.SyncResponse | nio.SlidingSyncResponse = nio.SyncResponse(
            "s_next",
            nio.Rooms(
                invite={},
                join={
                    room_id: nio.RoomInfo(
                        nio.Timeline([message_event], limited=False, prev_batch=None),
                        state=[],
                        ephemeral=[],
                        account_data=[],
                    ),
                },
                leave={},
            ),
            nio.DeviceOneTimeKeyCount(None, None),
            device_list,
            to_device_events=[],
            presence_events=[],
        )
    else:
        response = nio.SlidingSyncResponse(
            "p1",
            rooms={
                room_id: nio.SlidingSyncRoom(
                    membership="join",
                    timeline=[message_event],
                    num_live=1,
                ),
            },
            device_list=device_list,
        )

    await client.receive_response(response)

    assert len(callback_results) == 1
    assert isinstance(callback_results[0], nio.RoomSendResponse)
    assert order.index("device_change") < order.index("joined_members")
    assert order.index("keys_query") < order.index("share") < order.index("wire")
    assert client.olm.outbound_group_sessions[room_id] is new_session


@pytest.mark.asyncio
async def test_device_change_during_key_query_cannot_be_cleared_by_stale_response() -> None:
    """A newer device invalidation must survive an older in-flight key query."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    human_user_id = "@human:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(human_user_id, "Human", None)
    client.rooms[room_id] = room
    pending_key_user_ids = {human_user_id}
    client.olm = Mock()
    client.olm.clear_verifications.return_value = []
    client.olm.collect_key_requests.return_value = []
    client.olm.users_for_key_query = pending_key_user_ids
    client.olm.outbound_group_sessions = {room_id: SimpleNamespace(shared=True)}
    client.olm.should_query_keys = True
    client.olm.should_share_group_session.return_value = True
    client.olm.add_changed_users.side_effect = pending_key_user_ids.update
    joined_response = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(human_user_id, "Human", ""),
        ],
        room_id,
    )
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(joined_response)
        return joined_response

    async def stale_keys_query() -> nio.KeysQueryResponse:
        query_started.set()
        await release_query.wait()
        pending_key_user_ids.clear()
        client.olm.should_query_keys = False
        return nio.KeysQueryResponse({}, {})

    async def share_group_session(*_args: object, **_kwargs: object) -> nio.ShareGroupSessionResponse:
        client.olm.outbound_group_sessions[room_id] = SimpleNamespace(shared=True)
        return nio.ShareGroupSessionResponse(room_id, {bot_user_id, human_user_id})

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.keys_query = AsyncMock(side_effect=stale_keys_query)
    client.share_group_session = AsyncMock(side_effect=share_group_session)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "stale-devices"}))
    client._send = AsyncMock(return_value=nio.RoomSendResponse("$unsafe", room_id))

    delivery = asyncio.create_task(
        send_room_event_result(
            client,
            room_id,
            "m.room.message",
            {"body": "secret", "msgtype": "m.text"},
        ),
    )
    await query_started.wait()
    await client.receive_response(
        nio.SyncResponse(
            "s_next",
            nio.Rooms(invite={}, join={}, leave={}),
            nio.DeviceOneTimeKeyCount(None, None),
            nio.DeviceList(changed=[human_user_id], left=[]),
            to_device_events=[],
            presence_events=[],
        ),
    )
    release_query.set()

    assert await delivery is None
    assert human_user_id in pending_key_user_ids
    assert not room.members_synced
    client.share_group_session.assert_not_awaited()
    client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_newer_key_query_response_supersedes_older_background_response() -> None:
    """An older background key response must not overwrite a newer device snapshot."""
    user_id = "@human:example.org"
    client = _MindRoomAsyncClient("https://example.org", "@bot:example.org")
    client.access_token = "token"  # noqa: S105
    client.device_id = "DEVICE"
    client.store = Mock()
    client.olm = Mock()
    client.olm.users_for_key_query = {user_id}
    handled_responses: list[nio.KeysQueryResponse] = []
    client.olm.handle_response.side_effect = handled_responses.append
    old_response = nio.KeysQueryResponse({user_id: {}}, {})
    new_response = nio.KeysQueryResponse({user_id: {}}, {})
    query_started = [asyncio.Event(), asyncio.Event()]
    release_query = [asyncio.Event(), asyncio.Event()]
    query_count = 0

    async def send_query(*_args: object, **_kwargs: object) -> nio.KeysQueryResponse:
        nonlocal query_count
        index = query_count
        query_count += 1
        query_started[index].set()
        await release_query[index].wait()
        response = (old_response, new_response)[index]
        await client.receive_response(response)
        return response

    client._send = AsyncMock(side_effect=send_query)
    old_query = asyncio.create_task(client.keys_query())
    await query_started[0].wait()
    new_query = asyncio.create_task(client.keys_query())
    await query_started[1].wait()
    release_query[1].set()
    assert await new_query is new_response
    release_query[0].set()
    assert await old_query is old_response

    assert handled_responses == [new_response]


@pytest.mark.asyncio
async def test_key_query_rejects_success_typed_transport_failure() -> None:
    """A non-2xx device-key response must not mutate nio's crypto store."""
    user_id = "@human:example.org"
    client = _MindRoomAsyncClient("https://example.org", "@bot:example.org")
    client.access_token = "token"  # noqa: S105
    client.device_id = "DEVICE"
    client.store = Mock()
    client.olm = Mock()
    client.olm.users_for_key_query = {user_id}
    response = nio.KeysQueryResponse({user_id: {}}, {})
    transport = Mock()
    transport.status = 502
    response.transport_response = transport

    async def send_query(*_args: object, **_kwargs: object) -> nio.KeysQueryResponse:
        await client.receive_response(response)
        return response

    client._send = AsyncMock(side_effect=send_query)

    result = await client.keys_query()

    assert isinstance(result, nio.KeysQueryError)
    client.olm.handle_response.assert_not_called()


@pytest.mark.asyncio
async def test_device_invalidation_supersedes_background_key_query_response() -> None:
    """A sync device delta must reject an older background key response globally."""
    user_id = "@human:example.org"
    client = _MindRoomAsyncClient("https://example.org", "@bot:example.org")
    client.access_token = "token"  # noqa: S105
    client.device_id = "DEVICE"
    client.store = Mock()
    client.olm = Mock()
    pending_user_ids = {user_id}
    client.olm.users_for_key_query = pending_user_ids
    client.olm.add_changed_users.side_effect = pending_user_ids.update
    query_started = asyncio.Event()
    release_query = asyncio.Event()
    response = nio.KeysQueryResponse({user_id: {}}, {})

    async def send_query(*_args: object, **_kwargs: object) -> nio.KeysQueryResponse:
        query_started.set()
        await release_query.wait()
        await client.receive_response(response)
        return response

    client._send = AsyncMock(side_effect=send_query)
    query = asyncio.create_task(client.keys_query())
    await query_started.wait()
    client._preapply_delivery_invalidations(
        nio.SyncResponse(
            "s_next",
            nio.Rooms(invite={}, join={}, leave={}),
            nio.DeviceOneTimeKeyCount(None, None),
            nio.DeviceList(changed=[user_id], left=[]),
            to_device_events=[],
            presence_events=[],
        ),
    )
    release_query.set()

    assert await query is response
    assert user_id in pending_user_ids
    client.olm.handle_response.assert_not_called()


@pytest.mark.asyncio
async def test_membership_change_during_joined_query_rejects_stale_roster() -> None:
    """A newer leave event must fence an older in-flight joined-members response."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.clear_verifications.return_value = []
    client.olm.collect_key_requests.return_value = []
    client.olm.users_for_key_query = set()
    client.olm.outbound_group_sessions = {room_id: SimpleNamespace(shared=True)}
    client.olm.should_query_keys = False
    client.olm.should_share_group_session.return_value = True
    stale_joined_response = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(departed_user_id, "Departed", ""),
        ],
        room_id,
    )
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def stale_joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        query_started.set()
        await release_query.wait()
        client._handle_joined_members(stale_joined_response)
        return stale_joined_response

    async def share_group_session(*_args: object, **_kwargs: object) -> nio.ShareGroupSessionResponse:
        client.olm.outbound_group_sessions[room_id] = SimpleNamespace(shared=True)
        return nio.ShareGroupSessionResponse(room_id, {bot_user_id, departed_user_id})

    client.joined_members = AsyncMock(side_effect=stale_joined_members)
    client.share_group_session = AsyncMock(side_effect=share_group_session)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "stale-members"}))
    client._send = AsyncMock(return_value=nio.RoomSendResponse("$unsafe", room_id))
    leave_event = nio.Event.parse_event(
        {
            "content": {"membership": "leave"},
            "event_id": "$leave",
            "origin_server_ts": 1,
            "sender": departed_user_id,
            "state_key": departed_user_id,
            "type": "m.room.member",
        },
    )

    delivery = asyncio.create_task(
        send_room_event_result(
            client,
            room_id,
            "m.room.message",
            {"body": "secret", "msgtype": "m.text"},
        ),
    )
    await query_started.wait()
    await client.receive_response(
        nio.SyncResponse(
            "s_next",
            nio.Rooms(
                invite={},
                join={
                    room_id: nio.RoomInfo(
                        nio.Timeline([leave_event], limited=False, prev_batch=None),
                        state=[],
                        ephemeral=[],
                        account_data=[],
                    ),
                },
                leave={},
            ),
            nio.DeviceOneTimeKeyCount(None, None),
            nio.DeviceList(changed=[], left=[]),
            to_device_events=[],
            presence_events=[],
        ),
    )
    assert departed_user_id not in room.users
    release_query.set()

    assert await delivery is None
    assert not room.members_synced
    client.share_group_session.assert_not_awaited()
    client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_newer_joined_members_response_supersedes_in_flight_roster() -> None:
    """A later joined-members response must order before an older blocked request."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.users_for_key_query = set()
    client.olm.outbound_group_sessions = {room_id: SimpleNamespace(shared=True)}
    client.olm.should_query_keys = False
    client.room_send = AsyncMock()
    stale_response = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(departed_user_id, "Departed", ""),
        ],
        room_id,
    )
    newer_response = nio.JoinedMembersResponse(
        [nio.RoomMember(bot_user_id, "Bot", "")],
        room_id,
    )
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def stale_joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        query_started.set()
        await release_query.wait()
        client._handle_joined_members(stale_response)
        return stale_response

    client.joined_members = AsyncMock(side_effect=stale_joined_members)
    hydration = asyncio.create_task(send_room_event_result(client, room_id, "m.reaction", {"m.relates_to": {}}))
    await query_started.wait()
    client._handle_joined_members(newer_response)
    assert set(room.users) == {bot_user_id}
    release_query.set()

    assert await hydration is None
    assert not room.members_synced
    assert set(room.users) == {bot_user_id}
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_completes_first", [True, False], ids=["newer-first", "older-first"])
async def test_newer_ordinary_joined_members_request_supersedes_older_request(
    newer_completes_first: bool,
) -> None:
    """Request generation must own the runtime roster regardless of completion order."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    stale_response = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(departed_user_id, "Departed", ""),
        ],
        room_id,
    )
    current_response = nio.JoinedMembersResponse(
        [nio.RoomMember(bot_user_id, "Bot", "")],
        room_id,
    )
    query_started = [asyncio.Event(), asyncio.Event()]
    release_query = [asyncio.Event(), asyncio.Event()]
    query_count = 0

    async def send_query(*_args: object, **_kwargs: object) -> nio.JoinedMembersResponse:
        nonlocal query_count
        index = query_count
        query_count += 1
        query_started[index].set()
        await release_query[index].wait()
        response = (stale_response, current_response)[index]
        await client.receive_response(response)
        return response

    client._send = AsyncMock(side_effect=send_query)
    stale_query = asyncio.create_task(client.joined_members(room_id))
    await query_started[0].wait()
    current_query = asyncio.create_task(client.joined_members(room_id))
    await query_started[1].wait()
    if newer_completes_first:
        release_query[1].set()
        assert await current_query is current_response
        assert set(room.users) == {bot_user_id}
        release_query[0].set()
        assert isinstance(await stale_query, nio.JoinedMembersError)
    else:
        release_query[0].set()
        assert isinstance(await stale_query, nio.JoinedMembersError)
        assert set(room.users) == {bot_user_id, departed_user_id}
        release_query[1].set()
        assert await current_query is current_response
    assert set(room.users) == {bot_user_id}


@pytest.mark.asyncio
async def test_joined_members_query_reuses_runtime_client_generation() -> None:
    """An explicit delivery guard must nest around the runtime client without self-invalidating."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.access_token = "token"  # noqa: S105
    client.rooms[room_id] = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    response = nio.JoinedMembersResponse(
        [nio.RoomMember(bot_user_id, "Bot", "")],
        room_id,
    )

    async def send_query(*_args: object, **_kwargs: object) -> nio.JoinedMembersResponse:
        await client.receive_response(response)
        return response

    client._send = AsyncMock(side_effect=send_query)

    with joined_members_query(client, room_id) as response_is_current:
        result = await client.joined_members(room_id)

    assert result is response
    assert response_is_current(response)


@pytest.mark.asyncio
async def test_joined_members_rejects_success_typed_transport_failure() -> None:
    """A non-2xx joined-members response must not mutate the runtime room cache."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    client.rooms[room_id] = room
    response = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(departed_user_id, "Departed", ""),
        ],
        room_id,
    )
    transport = Mock()
    transport.status = 502
    response.transport_response = transport

    async def send_query(*_args: object, **_kwargs: object) -> nio.JoinedMembersResponse:
        await client.receive_response(response)
        return response

    client._send = AsyncMock(side_effect=send_query)
    prior_membership_epoch = room_membership_epoch(client, room_id)

    result = await client.joined_members(room_id)

    assert isinstance(result, nio.JoinedMembersError)
    assert set(room.users) == {bot_user_id}
    assert room_membership_epoch(client, room_id) == prior_membership_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_kind", ["classic", "sliding"])
async def test_membership_reset_fences_encrypted_room_before_callbacks(sync_kind: str) -> None:
    """Leave and ban responses must retire stale cached room delivery state."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.clear_verifications.return_value = []
    client.olm.collect_key_requests.return_value = []
    client.olm.users_for_key_query = set()
    client.olm.outbound_group_sessions = {room_id: SimpleNamespace(shared=True)}
    if sync_kind == "classic":
        response: nio.SyncResponse | nio.SlidingSyncResponse = nio.SyncResponse(
            "s_next",
            nio.Rooms(
                invite={},
                join={},
                leave={
                    room_id: nio.RoomInfo(
                        nio.Timeline([], limited=False, prev_batch=None),
                        state=[],
                        ephemeral=[],
                        account_data=[],
                    ),
                },
            ),
            nio.DeviceOneTimeKeyCount(None, None),
            nio.DeviceList([], []),
            to_device_events=[],
            presence_events=[],
        )
    else:
        response = nio.SlidingSyncResponse(
            "p1",
            rooms={room_id: nio.SlidingSyncRoom(membership="ban")},
        )

    await client.receive_response(response)

    assert not room.members_synced
    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.parametrize("sync_kind", ["classic", "sliding"])
def test_summary_joined_count_mismatch_pre_fences_encrypted_room(sync_kind: str) -> None:
    """A summary-only departure must invalidate the stale recipient roster immediately."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.outbound_group_sessions = {room_id: SimpleNamespace(shared=True)}
    if sync_kind == "classic":
        response: nio.SyncResponse | nio.SlidingSyncResponse = nio.SyncResponse(
            "s_next",
            nio.Rooms(
                invite={},
                join={
                    room_id: nio.RoomInfo(
                        nio.Timeline([], limited=False, prev_batch=None),
                        state=[],
                        ephemeral=[],
                        account_data=[],
                        summary=nio.RoomSummary(joined_member_count=1),
                    ),
                },
                leave={},
            ),
            nio.DeviceOneTimeKeyCount(None, None),
            nio.DeviceList([], []),
            to_device_events=[],
            presence_events=[],
        )
    else:
        response = nio.SlidingSyncResponse(
            "p1",
            rooms={
                room_id: nio.SlidingSyncRoom(
                    membership="join",
                    joined_count=1,
                    timeline=[],
                ),
            },
        )

    client._preapply_delivery_invalidations(response)

    assert not room.members_synced
    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.parametrize("sync_kind", ["classic", "sliding"])
def test_repeated_encryption_event_preserves_ready_session(sync_kind: str) -> None:
    """Repeated encryption state must not rotate an already encrypted room session."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    client.rooms[room_id] = room
    client.encrypted_rooms.add(room_id)
    session = SimpleNamespace(shared=True)
    client.olm = Mock()
    client.olm.outbound_group_sessions = {room_id: session}
    encryption_event = nio.Event.parse_event(
        {
            "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            "event_id": "$encryption",
            "origin_server_ts": 1,
            "sender": bot_user_id,
            "state_key": "",
            "type": "m.room.encryption",
        },
    )
    if sync_kind == "classic":
        response: nio.SyncResponse | nio.SlidingSyncResponse = nio.SyncResponse(
            "s_next",
            nio.Rooms(
                invite={},
                join={
                    room_id: nio.RoomInfo(
                        nio.Timeline([], limited=False, prev_batch=None),
                        state=[encryption_event],
                        ephemeral=[],
                        account_data=[],
                    ),
                },
                leave={},
            ),
            nio.DeviceOneTimeKeyCount(None, None),
            nio.DeviceList([], []),
            to_device_events=[],
            presence_events=[],
        )
    else:
        response = nio.SlidingSyncResponse(
            "p1",
            rooms={
                room_id: nio.SlidingSyncRoom(
                    membership="join",
                    required_state=[encryption_event],
                    timeline=[],
                ),
            },
        )

    client._preapply_delivery_invalidations(response)

    assert room.members_synced
    assert client.olm.outbound_group_sessions[room_id] is session


@pytest.mark.asyncio
async def test_sliding_required_state_invitee_is_pruned_before_key_tracking() -> None:
    """Sliding Sync required state must not expose invitees to crypto tracking."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    invitee_user_id = "@invitee:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.add_member(bot_user_id, "Bot", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.clear_verifications.return_value = []
    client.olm.collect_key_requests.return_value = []
    client.olm.outbound_group_sessions = {}
    tracked_rosters: list[frozenset[str]] = []
    client.olm.update_tracked_users.side_effect = lambda tracked_room: tracked_rosters.append(
        frozenset(tracked_room.users),
    )
    invite_event = nio.Event.parse_event(
        {
            "content": {"displayname": "Invitee", "membership": "invite"},
            "event_id": "$invite",
            "origin_server_ts": 1,
            "sender": bot_user_id,
            "state_key": invitee_user_id,
            "type": "m.room.member",
        },
    )

    await client.receive_response(
        nio.SlidingSyncResponse(
            "p1",
            rooms={
                room_id: nio.SlidingSyncRoom(
                    membership="join",
                    required_state=[invite_event],
                    timeline=[],
                ),
            },
        ),
    )

    assert set(room.users) == {bot_user_id}
    assert not room.invited_users
    assert tracked_rosters
    assert all(roster == frozenset({bot_user_id}) for roster in tracked_rosters)


@pytest.mark.asyncio
async def test_sliding_summary_hero_is_excluded_before_gated_send() -> None:
    """A mixed-membership hero cannot become an authoritative encryption recipient."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    invitee_user_id = "@invitee:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    client.rooms[room_id] = room
    old_session = SimpleNamespace(shared=True)
    new_session = SimpleNamespace(shared=True)
    client.olm = Mock()
    client.olm.clear_verifications.return_value = []
    client.olm.collect_key_requests.return_value = []
    client.olm.users_for_key_query = set()
    client.olm.outbound_group_sessions = {room_id: old_session}

    await client.receive_response(
        nio.SlidingSyncResponse(
            "p1",
            rooms={
                room_id: nio.SlidingSyncRoom(
                    membership="join",
                    heroes=[nio.SlidingSyncHero(invitee_user_id, "Invitee")],
                    joined_count=2,
                    invited_count=1,
                    timeline=[],
                ),
            },
        ),
    )

    assert set(room.users) == {bot_user_id}
    assert not room.members_synced
    assert room_id not in client.olm.outbound_group_sessions

    joined_response = nio.JoinedMembersResponse(
        [nio.RoomMember(bot_user_id, "Bot", "")],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(joined_response)
        return joined_response

    async def share_group_session(*_args: object, **_kwargs: object) -> nio.ShareGroupSessionResponse:
        assert set(room.users) == {bot_user_id}
        client.olm.outbound_group_sessions[room_id] = new_session
        return nio.ShareGroupSessionResponse(room_id, {bot_user_id})

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.olm.should_query_keys = False
    client.olm.should_share_group_session.return_value = True
    client.share_group_session = AsyncMock(side_effect=share_group_session)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "new-session"}))
    client._send = AsyncMock(return_value=nio.RoomSendResponse("$sent", room_id))

    response = await send_room_event_result(
        client,
        room_id,
        "m.room.message",
        {"body": "secret", "msgtype": "m.text"},
    )

    assert isinstance(response, nio.RoomSendResponse)
    assert client.olm.outbound_group_sessions[room_id] is new_session
    assert client.olm.outbound_group_sessions[room_id] is not old_session


@pytest.mark.asyncio
async def test_sliding_departure_retires_unshared_session_before_gated_send() -> None:
    """A Sliding Sync departure must never let nio reuse a partially shared session."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    old_session = SimpleNamespace(shared=False)
    new_session = SimpleNamespace(shared=True)
    client.olm = Mock()
    client.olm.clear_verifications.return_value = []
    client.olm.collect_key_requests.return_value = []
    client.olm.users_for_key_query = set()
    client.olm.outbound_group_sessions = {room_id: old_session}
    client.olm.should_query_keys = False
    client.olm.should_share_group_session.return_value = True
    departure_event = nio.Event.parse_event(
        {
            "content": {"membership": "leave"},
            "event_id": "$departure",
            "origin_server_ts": 1,
            "sender": departed_user_id,
            "state_key": departed_user_id,
            "type": "m.room.member",
        },
    )

    await client.receive_response(
        nio.SlidingSyncResponse(
            "p1",
            rooms={
                room_id: nio.SlidingSyncRoom(
                    membership="join",
                    required_state=[departure_event],
                    timeline=[],
                ),
            },
        ),
    )

    assert room_id not in client.olm.outbound_group_sessions

    async def share_group_session(*_args: object, **_kwargs: object) -> nio.ShareGroupSessionResponse:
        assert set(room.users) == {bot_user_id}
        client.olm.outbound_group_sessions[room_id] = new_session
        return nio.ShareGroupSessionResponse(room_id, {bot_user_id})

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        joined_response = nio.JoinedMembersResponse(
            [nio.RoomMember(bot_user_id, "Bot", "")],
            room_id,
        )
        client._handle_joined_members(joined_response)
        return joined_response

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.share_group_session = AsyncMock(side_effect=share_group_session)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "new-session"}))
    client._send = AsyncMock(return_value=nio.RoomSendResponse("$sent", room_id))

    response = await send_room_event_result(
        client,
        room_id,
        "m.room.message",
        {"body": "secret", "msgtype": "m.text"},
    )

    assert isinstance(response, nio.RoomSendResponse)
    assert client.olm.outbound_group_sessions[room_id] is new_session
    assert client.olm.outbound_group_sessions[room_id] is not old_session


@pytest.mark.asyncio
async def test_room_send_preparation_rejects_recipient_change_during_session_share() -> None:
    """Nio preparation must discard ciphertext and sessions built across a roster change."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.users_for_key_query = set()
    client.olm.should_share_group_session.return_value = True
    client.olm.outbound_group_sessions = {
        room_id: SimpleNamespace(shared=False),
    }
    share_started = asyncio.Event()
    release_share = asyncio.Event()

    async def share_group_session(*_args: object, **_kwargs: object) -> nio.ShareGroupSessionResponse:
        share_started.set()
        await release_share.wait()
        room.remove_member(departed_user_id)
        return nio.ShareGroupSessionResponse(room_id, set())

    client.share_group_session = AsyncMock(side_effect=share_group_session)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "discard-me"}))

    preparation = asyncio.create_task(
        client._prepare_room_send(
            room_id,
            "m.room.message",
            {"body": "secret", "msgtype": "m.text"},
            UUID(int=0),
            True,
        ),
    )
    await share_started.wait()
    release_share.set()

    with pytest.raises(nio.SendRetryError):
        await preparation
    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.asyncio
async def test_membership_handler_defers_session_retirement_during_share() -> None:
    """Membership updates must not remove a session from under nio's active share."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.users_for_key_query = set()
    client.olm.should_share_group_session.return_value = True
    session = SimpleNamespace(shared=False)
    client.olm.outbound_group_sessions = {room_id: session}

    async def share_group_session(*_args: object, **_kwargs: object) -> nio.ShareGroupSessionResponse:
        client.sharing_session[room_id] = asyncio.Event()
        try:
            client._handle_joined_members(
                nio.JoinedMembersResponse(
                    [nio.RoomMember(bot_user_id, "Bot", "")],
                    room_id,
                ),
            )
            assert client.olm.outbound_group_sessions[room_id] is session
            await asyncio.sleep(0)
            session.shared = True
        finally:
            client.sharing_session.pop(room_id)
        return nio.ShareGroupSessionResponse(room_id, set())

    client.share_group_session = AsyncMock(side_effect=share_group_session)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "discard-me"}))

    with pytest.raises(nio.SendRetryError):
        await client._prepare_room_send(
            room_id,
            "m.room.message",
            {"body": "secret", "msgtype": "m.text"},
            UUID(int=0),
            True,
        )

    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_kind", ["classic", "sliding"])
async def test_transport_guard_rejects_membership_change_during_header_refresh(
    monkeypatch: pytest.MonkeyPatch,
    sync_kind: str,
) -> None:
    """A header await must not outlive the recipients used for frozen ciphertext."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    leave_event = nio.Event.parse_event(
        {
            "content": {"membership": "leave"},
            "event_id": "$leave",
            "origin_server_ts": 1,
            "sender": departed_user_id,
            "state_key": departed_user_id,
            "type": "m.room.member",
        },
    )
    if sync_kind == "classic":
        invalidation: nio.SyncResponse | nio.SlidingSyncResponse = nio.SyncResponse(
            "s_next",
            nio.Rooms(
                invite={},
                join={
                    room_id: nio.RoomInfo(
                        nio.Timeline([leave_event], limited=False, prev_batch=None),
                        state=[],
                        ephemeral=[],
                        account_data=[],
                    ),
                },
                leave={},
            ),
            nio.DeviceOneTimeKeyCount(None, None),
            nio.DeviceList(changed=[], left=[]),
            to_device_events=[],
            presence_events=[],
        )
    else:
        invalidation = nio.SlidingSyncResponse(
            "p1",
            rooms={
                room_id: nio.SlidingSyncRoom(
                    membership="join",
                    timeline=[leave_event],
                    num_live=1,
                ),
            },
        )

    class MembershipChangingHeaders:
        async def prepare(self) -> None:
            client._preapply_delivery_invalidations(invalidation)

    headers = MembershipChangingHeaders()
    config = nio.AsyncClientConfig(custom_headers=cast("Any", headers))
    client = _MindRoomAsyncClient("https://example.org", bot_user_id, config=config)
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.users_for_key_query = set()
    client.olm.outbound_group_sessions = {room_id: SimpleNamespace(shared=True)}
    client.olm.should_query_keys = False
    client.olm.should_share_group_session.return_value = False
    joined_response = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(departed_user_id, "Departed", ""),
        ],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(joined_response)
        return joined_response

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "stale"}))
    transport_send = AsyncMock()
    monkeypatch.setattr(nio.AsyncClient, "send", transport_send)

    response = await send_room_event_result(
        client,
        room_id,
        "m.room.message",
        {"body": "secret", "msgtype": "m.text"},
    )

    assert response is None
    transport_send.assert_not_awaited()
    assert not room.members_synced
    assert room_id not in client.olm.outbound_group_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_kind", ["classic", "sliding"])
async def test_transport_guard_rejects_encryption_event_during_header_refresh(
    monkeypatch: pytest.MonkeyPatch,
    sync_kind: str,
) -> None:
    """A parsed encryption event must fence plaintext before later sync awaits."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    encryption_event = nio.Event.parse_event(
        {
            "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            "event_id": "$encryption",
            "origin_server_ts": 1,
            "sender": bot_user_id,
            "state_key": "",
            "type": "m.room.encryption",
        },
    )
    if sync_kind == "classic":
        invalidation: nio.SyncResponse | nio.SlidingSyncResponse = nio.SyncResponse(
            "s_next",
            nio.Rooms(
                invite={},
                join={
                    room_id: nio.RoomInfo(
                        nio.Timeline([encryption_event], limited=False, prev_batch=None),
                        state=[],
                        ephemeral=[],
                        account_data=[],
                    ),
                },
                leave={},
            ),
            nio.DeviceOneTimeKeyCount(None, None),
            nio.DeviceList(changed=[], left=[]),
            to_device_events=[],
            presence_events=[],
        )
    else:
        invalidation = nio.SlidingSyncResponse(
            "p1",
            rooms={
                room_id: nio.SlidingSyncRoom(
                    membership="join",
                    timeline=[encryption_event],
                    num_live=1,
                ),
            },
        )

    class EncryptionChangingHeaders:
        async def prepare(self) -> None:
            client._preapply_delivery_invalidations(invalidation)

    config = nio.AsyncClientConfig(custom_headers=cast("Any", EncryptionChangingHeaders()))
    client = _MindRoomAsyncClient("https://example.org", bot_user_id, config=config)
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=False)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.users_for_key_query = set()
    client.olm.outbound_group_sessions = {}
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventError("not found", status_code="M_NOT_FOUND"),
    )
    transport_send = AsyncMock()
    monkeypatch.setattr(nio.AsyncClient, "send", transport_send)

    response = await send_room_event_result(
        client,
        room_id,
        "m.room.message",
        {"body": "PLAINTEXT", "msgtype": "m.text"},
    )

    assert response is None
    assert room.encrypted
    assert not room.members_synced
    transport_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_cache", ["plaintext", "missing"])
async def test_transport_guard_rejects_plaintext_mode_change_during_header_refresh(
    monkeypatch: pytest.MonkeyPatch,
    initial_cache: str,
) -> None:
    """A plaintext payload must not reach transport after its room becomes encrypted."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"

    class EncryptionChangingHeaders:
        async def prepare(self) -> None:
            room = client.rooms.get(room_id)
            if room is None:
                room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
                client.rooms[room_id] = room
            else:
                room.encrypted = True

    config = nio.AsyncClientConfig(custom_headers=cast("Any", EncryptionChangingHeaders()))
    client = _MindRoomAsyncClient("https://example.org", bot_user_id, config=config)
    client.access_token = "token"  # noqa: S105
    client.olm = None
    client.rooms = (
        {room_id: nio.MatrixRoom(room_id, bot_user_id, encrypted=False)} if initial_cache == "plaintext" else {}
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventError("not found", status_code="M_NOT_FOUND"),
    )
    transport_send = AsyncMock()
    monkeypatch.setattr(nio.AsyncClient, "send", transport_send)

    response = await send_room_event_result(
        client,
        room_id,
        "m.room.message",
        {"body": "secret", "msgtype": "m.text"},
    )

    assert response is None
    transport_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_retry_revalidates_recipient_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate-limit retry must reject ciphertext invalidated during backoff."""
    room_id = "!room:example.org"
    bot_user_id = "@bot:example.org"
    departed_user_id = "@departed:example.org"
    client = _MindRoomAsyncClient("https://example.org", bot_user_id)
    client.access_token = "token"  # noqa: S105
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    room.add_member(bot_user_id, "Bot", None)
    room.add_member(departed_user_id, "Departed", None)
    client.rooms[room_id] = room
    client.olm = Mock()
    client.olm.users_for_key_query = set()
    client.olm.outbound_group_sessions = {room_id: SimpleNamespace(shared=True)}
    client.olm.should_query_keys = False
    client.olm.should_share_group_session.return_value = False
    joined_response = nio.JoinedMembersResponse(
        [
            nio.RoomMember(bot_user_id, "Bot", ""),
            nio.RoomMember(departed_user_id, "Departed", ""),
        ],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(joined_response)
        return joined_response

    client.joined_members = AsyncMock(side_effect=joined_members)
    client.encrypt = Mock(return_value=("m.room.encrypted", {"ciphertext": "stale"}))
    transport_send = AsyncMock(return_value=SimpleNamespace(status=429))
    monkeypatch.setattr(nio.AsyncClient, "send", transport_send)
    client.create_matrix_response = AsyncMock(
        return_value=nio.RoomSendError(
            "rate limited",
            status_code="M_LIMIT_EXCEEDED",
            retry_after_ms=1,
            room_id=room_id,
        ),
    )
    client.run_response_callbacks = AsyncMock()

    async def membership_change_during_backoff(_delay: float) -> None:
        client._handle_joined_members(
            nio.JoinedMembersResponse(
                [nio.RoomMember(bot_user_id, "Bot", "")],
                room_id,
            ),
        )

    monkeypatch.setattr("nio.client.async_client.asyncio.sleep", membership_change_during_backoff)

    response = await send_room_event_result(
        client,
        room_id,
        "m.room.message",
        {"body": "secret", "msgtype": "m.text"},
    )

    assert response is None
    assert transport_send.await_count == 1
    assert room_id not in client.olm.outbound_group_sessions


def test_encryption_exposes_only_mindroom_recovery_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encrypted events expose recovery markers but no private message fields."""
    relation = {"event_id": "$original:example.org", "rel_type": "m.replace"}

    def fake_encrypt(
        _client: nio.AsyncClient,
        _room_id: str,
        _message_type: str,
        _content: dict[Any, Any],
    ) -> tuple[str, dict[str, Any]]:
        return "m.room.encrypted", {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "encrypted payload",
            "m.relates_to": relation,
        }

    monkeypatch.setattr(nio.AsyncClient, "encrypt", fake_encrypt)
    client = _MindRoomAsyncClient("https://example.org", "@mindroom_agent:example.org")

    message_type, encrypted_content = client.encrypt(
        "!room:example.org",
        "m.room.message",
        {
            "body": "private answer text",
            "m.mentions": {"user_ids": ["@private:example.org"]},
            "msgtype": "m.notice",
            STREAM_STATUS_KEY: "streaming",
            VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
            CONFIG_CONFIRMATION_REACTION_KEY: "$reaction",
        },
    )

    assert message_type == "m.room.encrypted"
    assert encrypted_content == {
        "algorithm": "m.megolm.v1.aes-sha2",
        "ciphertext": "encrypted payload",
        "m.relates_to": relation,
        STREAM_STATUS_KEY: "streaming",
        VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
        CONFIG_CONFIRMATION_REACTION_KEY: "$reaction",
    }


def test_encryption_does_not_add_metadata_to_ordinary_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary encrypted messages retain nio's standard envelope."""

    def fake_encrypt(
        _client: nio.AsyncClient,
        _room_id: str,
        _message_type: str,
        _content: dict[Any, Any],
    ) -> tuple[str, dict[str, str]]:
        return "m.room.encrypted", {"ciphertext": "encrypted payload"}

    monkeypatch.setattr(nio.AsyncClient, "encrypt", fake_encrypt)
    client = _MindRoomAsyncClient("https://example.org", "@mindroom_agent:example.org")

    _, encrypted_content = client.encrypt(
        "!room:example.org",
        "m.room.message",
        {"body": "private answer text", "msgtype": "m.text"},
    )

    assert encrypted_content == {"ciphertext": "encrypted payload"}


def test_explicit_zero_one_time_key_count_requests_replenishment(tmp_path: Path) -> None:
    """A drained server OTK pool must make nio upload replacement keys."""
    user_id = "@agent:example.org"
    client = _MindRoomAsyncClient(
        "https://example.org",
        user_id,
        device_id="AGENTDEVICE",
        store_path=str(tmp_path),
    )
    client.restore_login(user_id, "AGENTDEVICE", "access-token")
    client.load_store()
    assert client.olm is not None
    client.olm.account.shared = True
    client.olm.uploaded_key_count = 50

    response = nio.SyncResponse(
        next_batch="next",
        rooms=nio.Rooms(invite={}, join={}, leave={}),
        device_key_count=nio.DeviceOneTimeKeyCount(curve25519=7, signed_curve25519=0),
        device_list=nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
        account_data_events=[],
    )
    client._handle_olm_events(response)

    assert client.olm.uploaded_key_count == 0
    assert client.should_upload_keys


def test_matrix_client_config_copies_custom_http_headers() -> None:
    """Caller-owned secrets cannot mutate a running client's request headers."""
    headers = {"X-Access-Client": "test-secret"}

    config = matrix_client_config(http_headers=headers)
    headers.clear()

    assert config.custom_headers == {"X-Access-Client": "test-secret"}


def test_matrix_client_config_enables_limited_timeline_backfill() -> None:
    """MindRoom clients must recover events omitted by limited sync windows."""
    config = matrix_client_config()

    assert config.backfill_limited_timelines is True
    assert config.backfill_persist_recovery is True
    assert config.store_sync_tokens is True


@pytest.mark.asyncio
async def test_unrecovered_timeline_gap_survives_client_restart(tmp_path: Path) -> None:
    """Nio must durably retain a gap when MindRoom advances its own sync token."""
    room_id = "!room:example.org"
    user_id = "@mindroom_agent:example.org"
    device_id = "AGENTDEVICE"
    config = matrix_client_config()

    def sync_response(next_batch: str, *, limited: bool) -> nio.SyncResponse:
        joined_rooms = (
            {
                room_id: nio.RoomInfo(
                    nio.Timeline([], limited=True, prev_batch="p_before_gap"),
                    state=[],
                    ephemeral=[],
                    account_data=[],
                ),
            }
            if limited
            else {}
        )
        return nio.SyncResponse(
            next_batch,
            nio.Rooms(invite={}, join=joined_rooms, leave={}),
            nio.DeviceOneTimeKeyCount(None, None),
            nio.DeviceList(changed=[], left=[]),
            to_device_events=[],
            presence_events=[],
        )

    def load_client() -> _MindRoomAsyncClient:
        client = _MindRoomAsyncClient(
            "https://example.org",
            user_id,
            device_id=device_id,
            store_path=str(tmp_path),
            config=config,
        )
        client.restore_login(user_id, device_id, "access-token")
        client.load_store()
        return client

    client = load_client()
    client.next_batch = "s_before_gap"
    client._recovery_room_messages = AsyncMock(side_effect=OSError("temporary failure"))

    limited_response = sync_response("s_limited", limited=True)
    await client.receive_response(limited_response)
    later_response = sync_response("s_later", limited=False)
    await client.receive_response(later_response)
    await client.close()

    assert limited_response.unrecovered_room_ids == {room_id}
    assert later_response.unrecovered_room_ids == {room_id}

    restarted = load_client()
    try:
        recovery = cast("Any", restarted)._recovery
        assert restarted.loaded_sync_token == "s_later"  # noqa: S105
        assert tuple(recovery.gaps) == (room_id,)
        assert recovery.gaps[room_id][0].cursor_token == "s_before_gap"  # noqa: S105
    finally:
        await restarted.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are unavailable on Windows")
def test_matrix_store_directory_is_owner_only(tmp_path: Path) -> None:
    """Private Olm identity material is inaccessible to other local users."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )

    client = client_session._create_matrix_client(
        "https://matrix.example.org",
        runtime_paths,
        "@desktop:example.org",
        "matrix-access-token",
    )

    assert client.store_path is not None
    assert stat.S_IMODE(Path(client.store_path).stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_login_with_token_restores_returned_device(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Token exchange uses no guessed identity and restores exactly returned credentials."""
    response = nio.LoginResponse("@desktop:example.org", "DESKTOP", "matrix-access-token")
    login_client = SimpleNamespace(
        login=AsyncMock(return_value=response),
        close=AsyncMock(),
    )
    create_login_client = Mock(return_value=login_client)
    restored_client = object()
    create_authenticated = Mock(return_value=restored_client)
    monkeypatch.setattr(client_session, "_create_matrix_client", create_login_client)
    monkeypatch.setattr(client_session, "create_authenticated_client", create_authenticated)
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )

    result = await login_with_token(
        "https://matrix.example.org",
        "short-lived-token",
        runtime_paths,
        expected_user_id="@desktop:example.org",
        http_headers={"X-Access-Client": "test-secret"},
    )

    assert result is restored_client
    create_login_client.assert_called_once_with(
        "https://matrix.example.org",
        runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )
    login_client.login.assert_awaited_once_with(
        token="short-lived-token",  # noqa: S106 - Test-only login token.
        device_name="MindRoom Desktop Bridge",
    )
    login_client.close.assert_awaited_once()
    create_authenticated.assert_called_once_with(
        "https://matrix.example.org",
        "@desktop:example.org",
        "DESKTOP",
        "matrix-access-token",
        runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )


@pytest.mark.asyncio
async def test_login_with_token_revokes_unexpected_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """SSO cannot silently enroll a different Matrix account than requested."""
    login_client = SimpleNamespace(
        login=AsyncMock(return_value=nio.LoginResponse("@wrong:example.org", "WRONG", "access-token")),
        logout=AsyncMock(return_value=nio.LogoutResponse()),
        close=AsyncMock(),
    )
    monkeypatch.setattr(client_session, "_create_matrix_client", Mock(return_value=login_client))
    create_authenticated = Mock()
    monkeypatch.setattr(client_session, "create_authenticated_client", create_authenticated)

    with pytest.raises(PermanentMatrixStartupError, match=r"@wrong:example\.org"):
        await login_with_token(
            "https://matrix.example.org",
            "short-lived-token",
            RuntimePaths(
                config_path=tmp_path / "config.yaml",
                config_dir=tmp_path,
                env_path=tmp_path / ".env",
                storage_root=tmp_path / "data",
            ),
            expected_user_id="@desktop:example.org",
        )

    login_client.logout.assert_awaited_once()
    login_client.close.assert_awaited_once()
    create_authenticated.assert_not_called()


@pytest.mark.asyncio
async def test_login_flows_uses_proxy_headers_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Automatic method discovery crosses the same authenticated proxy as login."""
    client = SimpleNamespace(
        login_info=AsyncMock(return_value=nio.LoginInfoResponse(["m.login.token", "m.login.sso"])),
        close=AsyncMock(),
    )
    create_client = Mock(return_value=client)
    monkeypatch.setattr(client_session, "_create_matrix_client", create_client)
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )

    flows = await login_flows(
        "https://matrix.example.org",
        runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )

    assert flows == ("m.login.token", "m.login.sso")
    create_client.assert_called_once_with(
        "https://matrix.example.org",
        runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )
    client.close.assert_awaited_once()
