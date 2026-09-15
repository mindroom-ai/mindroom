"""Keyed retries preserve one accepted encrypted event across client restart."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import nio
import pytest
from aiohttp import web
from nio.crypto import OlmAccount
from nio.durable import open_durable_sync

from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.matrix.client_session import MindRoomAsyncClient
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.test_matrix_agent_discovery import context as context  # noqa: PLC0414
from tests.test_membership_encryption_ownership import (
    PEER,
    ROOM,
    USER,
    _decrypt_for_peer,
    _membership_app,
    _seed_peer_keys,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("enforce_turn_authorization")]


async def test_encrypted_retry_after_client_restart_keeps_one_event(  # noqa: PLR0915
    context: ToolRuntimeContext,
    tmp_path: Path,
) -> None:
    """Real nio encryption and durable crypto survive an ambiguous HTTP acknowledgement."""
    shares: list[dict] = []
    sent: list[dict] = []
    attempts: list[tuple[str, dict[str, Any]]] = []
    receipts: dict[str, str] = {}
    acknowledge = False

    @web.middleware
    async def transaction_receipts(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if "/send/m.room.encrypted/" not in request.path:
            return await handler(request)
        transaction = request.path.rsplit("/", 1)[-1]
        attempts.append((transaction, await request.json()))
        if transaction in receipts:
            response = web.json_response({"event_id": receipts[transaction]})
        else:
            response = await handler(request)
            receipts[transaction] = "$sent"
        if not acknowledge:
            assert request.transport is not None
            request.transport.close()
        return response

    app = _membership_app([USER, PEER], shares, sent)
    app.middlewares.append(transaction_receipts)
    http = web.AppRunner(app)
    await http.setup()
    site = web.TCPSite(http, "127.0.0.1", 0)
    await site.start()
    host, port = http.addresses[0]
    homeserver = f"http://{host}:{port}"
    consumer = uuid4()
    peer = OlmAccount()
    peer.generate_one_time_keys(1)
    config = nio.AsyncClientConfig(max_timeouts=0, max_limit_exceeded=0)
    client = MindRoomAsyncClient(homeserver, USER, "BOT", store_path=None, config=config)
    client.restore_login(USER, "BOT", "test-token")
    session = open_durable_sync(client, consumer_id=consumer, store_path=tmp_path / "crypto")
    MatrixMessageTools._recent_actions.clear()
    try:
        _seed_peer_keys(session, client, peer)
        room = nio.MatrixRoom(ROOM, USER, encrypted=True)
        room.add_member(USER, "Bot", None)
        room.add_member(PEER, "Peer", None)
        client.rooms[ROOM] = room
        runtime = replace(
            context,
            client=client,
            room=room,
            target=MessageTarget.resolve(room_id=ROOM, thread_id=None, reply_to_event_id=None),
        )
        with tool_runtime_context(runtime):
            first = json.loads(await MatrixMessageTools().matrix_message(message="first", idempotency_key="encrypted"))
        assert first["status"] == "error"
        acknowledge = True
        assert len(sent) == 1
        assert client.olm is not None
        identity_keys = client.olm.account.identity_keys
        with session._store.transaction():
            session._save_rooms({ROOM: room}, {(ROOM, USER), (ROOM, PEER)})
        await session.close()
        await client.close()

        client = MindRoomAsyncClient(homeserver, USER, "BOT", store_path=None, config=config)
        client.restore_login(USER, "BOT", "test-token")
        session = open_durable_sync(client, consumer_id=consumer, store_path=tmp_path / "crypto")
        assert client.olm is not None
        assert client.olm.account.identity_keys == identity_keys
        with tool_runtime_context(replace(runtime, client=client, room=client.rooms[ROOM])):
            retry = json.loads(
                await MatrixMessageTools().matrix_message(message="changed", idempotency_key="encrypted"),
            )
        assert retry["status"] == "ok"
        assert retry["event_id"] == "$sent"
        assert len(sent) == len(receipts) == 1
        assert len(attempts) >= 2
        assert len({transaction for transaction, _ in attempts}) == 1
        assert all("ciphertext" in content and "body" not in content for _, content in attempts)
        assert _decrypt_for_peer(peer, shares, sent[0]["ciphertext"]) == "first"
    finally:
        await session.close()
        await client.close()
        await http.cleanup()
