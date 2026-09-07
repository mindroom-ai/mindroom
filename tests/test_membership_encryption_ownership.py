"""Application membership lookups must not choose nio encryption recipients."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import nio
import pytest
import vodozemac
from aiohttp import web
from nio.crypto import OlmAccount, OlmDevice
from nio.durable import open_durable_sync
from unpaddedbase64 import decode_base64

from mindroom.authorization import ensure_room_membership_synced
from mindroom.matrix.client_session import MindRoomAsyncClient

if TYPE_CHECKING:
    from pathlib import Path

    from nio.durable import DurableSync

USER = "@bot:example.org"
PEER = "@peer:example.org"
ROOM = "!room:example.org"


def _seed_peer_keys(session: DurableSync, client: MindRoomAsyncClient, peer: OlmAccount) -> None:
    """Install a verified peer and an Olm channel capable of carrying room keys."""
    assert client.olm is not None
    assert client.store is not None
    with session._outbound.transaction():
        client.olm.account.shared = True
        client.olm.save_account()
        device = OlmDevice(PEER, "PEER", peer.identity_keys)
        client.olm.device_store.add(device)
        client.store.save_device_keys({PEER: {"PEER": device}})
        client.olm.create_session(next(iter(peer.one_time_keys["curve25519"].values())), device.curve25519)
        client.verify_device(device)
        client.olm.tracked_users.update((USER, PEER))
        client.olm.users_for_key_query.clear()
        session._crypto.capture()


def _decrypt_for_peer(peer: OlmAccount, shares: list[dict], ciphertext: str) -> str | None:
    """Use only the key material actually sent to the peer to attempt decryption."""
    for share in shares:
        if PEER not in share:
            continue
        encrypted_key = share[PEER]["PEER"]
        encrypted = encrypted_key["ciphertext"][peer.identity_keys["curve25519"]]
        _, plaintext = peer.create_inbound_session(
            encrypted_key["sender_key"],
            vodozemac.AnyOlmMessage.from_parts(encrypted["type"], decode_base64(encrypted["body"])),
        )
        key = json.loads(plaintext)["content"]["session_key"]
        group = vodozemac.InboundGroupSession(vodozemac.SessionKey(key))
        clear = group.decrypt(vodozemac.MegolmMessage.from_base64(ciphertext)).plaintext
        return json.loads(clear)["content"]["body"]
    return None


def _membership_app(members: list[str], shares: list[dict], sent: list[dict]) -> web.Application:
    """Serve recipient lookups while capturing real encrypted Matrix sends."""

    async def handle(request: web.Request) -> web.Response:
        if request.path.endswith("/joined_members"):
            return web.json_response({"joined": {user: {"display_name": "Member"} for user in members}})
        if request.path.endswith("/keys/query"):
            return web.json_response({"device_keys": {USER: {}}, "failures": {}})
        if "/sendToDevice/m.room.encrypted/" in request.path:
            shares.append((await request.json())["messages"])
            return web.json_response({})
        if "/send/m.room.encrypted/" in request.path:
            sent.append(await request.json())
            return web.json_response({"event_id": "$sent"})
        raise AssertionError(request.path)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("application_lookup", [False, True], ids=["nio-control", "mindroom"])
async def test_departed_member_gets_no_new_encryption_key(
    tmp_path: Path,
    restart: bool,
    application_lookup: bool,
) -> None:
    """A fresh recipient lookup excludes a peer even after app lookup and restart."""
    members = [USER, PEER]
    shares: list[dict] = []
    sent: list[dict] = []

    http = web.AppRunner(_membership_app(members, shares, sent))
    await http.setup()
    site = web.TCPSite(http, "127.0.0.1", 0)
    await site.start()
    host, port = http.addresses[0]
    homeserver = f"http://{host}:{port}"
    consumer = uuid4()
    client = MindRoomAsyncClient(homeserver, USER, "BOT", store_path=None)
    client.restore_login(USER, "BOT", "test-token")
    session = open_durable_sync(client, consumer_id=consumer, store_path=tmp_path)
    peer = OlmAccount()
    peer.generate_one_time_keys(1)
    try:
        _seed_peer_keys(session, client, peer)
        room = nio.MatrixRoom(ROOM, USER, encrypted=True)
        room.add_member(USER, "Bot", None)
        room.add_member(PEER, "Peer", None)
        client.rooms[ROOM] = room
        if application_lookup:
            assert await ensure_room_membership_synced(client, room, sender_id=PEER)
        else:
            assert isinstance(await client.joined_members(ROOM), nio.JoinedMembersResponse)

        if restart:
            # Persist a subsequent producer projection, including its completeness
            # flag, as the next sync would do before a process restart.
            with session._store.transaction():
                session._save_rooms({ROOM: room}, {(ROOM, USER), (ROOM, PEER)})
            await session.close()
            await client.close()
            client = MindRoomAsyncClient(homeserver, USER, "BOT", store_path=None)
            client.restore_login(USER, "BOT", "test-token")
            session = open_durable_sync(client, consumer_id=consumer, store_path=tmp_path)

        members[:] = [USER]
        refreshed = await client.joined_members(ROOM)
        assert isinstance(refreshed, nio.JoinedMembersResponse)
        assert [member.user_id for member in refreshed.members] == [USER]
        result = await client.room_send(
            ROOM,
            "m.room.message",
            {"msgtype": "m.text", "body": "after removal"},
            tx_id="after-removal",
        )
        assert isinstance(result, nio.RoomSendResponse)
        assert len(sent) == 1
        decrypted = _decrypt_for_peer(peer, shares, sent[0]["ciphertext"])
        assert decrypted is None, "Departed peer received a new room key and decrypted the subsequent message"
        assert not any(PEER in share for share in shares)
        assert not client.rooms[ROOM].members_synced
    finally:
        await session.close()
        await client.close()
        await http.cleanup()
