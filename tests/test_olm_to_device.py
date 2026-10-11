"""Public NIO transport integration and current pinned-sender authorization."""
# ruff: noqa: D103

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING

import nio
import pytest
from aiohttp import web
from nio.crypto import OlmDevice
from nio.store import SqliteMemoryStore

from mindroom.matrix.client_session import MindRoomAsyncClient
from mindroom.matrix.olm_to_device import (
    OlmToDeviceError,
    PinnedMatrixDevice,
    authenticated_sender_matches,
    resolve_pinned_device,
    send_encrypted_to_device,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

SENDER = "@cloud:example.org"
RECIPIENT = "@desktop:example.org"


@pytest.mark.asyncio
async def test_catalog_transport_preserves_unverified_device_trust() -> None:
    """An authenticated request permits a private reply without granting device trust."""
    async with olm_transport() as (client, peer, requests, _):
        assert client.olm is not None
        assert peer.olm is not None
        target = PinnedMatrixDevice(RECIPIENT, "DESKTOP", peer.olm.account.identity_keys["ed25519"])
        await send_encrypted_to_device(client, target, event_type="io.mindroom.test", content={})
        assert not client.olm.device_store[RECIPIENT]["DESKTOP"].verified
        messages = requests[-1]["body"]["messages"]
        assert set(messages) == {RECIPIENT}
        assert set(messages[RECIPIENT]) == {"DESKTOP"}


@pytest.mark.parametrize("user_id", ["@:", "@:example.org", "@desktop:"])
def test_pinned_matrix_device_rejects_empty_user_id_components(user_id: str) -> None:
    with pytest.raises(ValueError, match="@user:server"):
        PinnedMatrixDevice(user_id, "DESKTOP", "fingerprint")


@asynccontextmanager
async def olm_transport(
    *,
    sender: str = SENDER,
    recipient: str = RECIPIENT,
) -> AsyncIterator[tuple[MindRoomAsyncClient, nio.AsyncClient, list[dict], dict]]:
    """Exercise public nio calls against actual signed keys and local HTTP."""
    requests: list[dict] = []
    query_override: dict = {}
    config = nio.AsyncClientConfig(store=SqliteMemoryStore)
    peer = MindRoomAsyncClient("https://unused.invalid", recipient, "DESKTOP", config=config)
    peer.restore_login(recipient, "DESKTOP", "test-token")
    assert peer.olm is not None
    peer_keys = peer.olm.share_keys()

    async def handle(request: web.Request) -> web.Response:
        body = await request.json()
        requests.append({"path": request.path, "body": body})
        if request.path.endswith("/keys/query"):
            return web.json_response(
                query_override or {"device_keys": {recipient: {"DESKTOP": peer_keys["device_keys"]}}},
            )
        if request.path.endswith("/keys/claim"):
            first = next(iter(peer_keys["one_time_keys"].items()))
            return web.json_response({"one_time_keys": {recipient: {"DESKTOP": dict([first])}}})
        assert "/sendToDevice/m.room.encrypted/" in request.path
        return web.json_response({})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    client = MindRoomAsyncClient(f"http://127.0.0.1:{port}", sender, "CLOUD", config=config)
    client.restore_login(sender, "CLOUD", "test-token")
    try:
        yield client, peer, requests, query_override
    finally:
        for current in (client, peer):
            await current.close()
            assert current.store is not None
            current.store.database.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_first_contact_public_transport_delivers_authenticated_custom_event() -> None:
    async with olm_transport() as (client, peer, requests, _):
        assert client.olm is not None
        assert peer.olm is not None
        target = PinnedMatrixDevice(RECIPIENT, "DESKTOP", peer.olm.account.identity_keys["ed25519"])
        await peer.receive_response(
            nio.KeysQueryResponse({SENDER: {"CLOUD": client.olm.share_keys()["device_keys"]}}, {}),
        )
        await send_encrypted_to_device(client, target, event_type="io.mindroom.test", content={"secret": "value"})
        assert [item["path"].rsplit("/", 2)[-2:] for item in requests[:2]] == [["keys", "query"], ["keys", "claim"]]
        encrypted = requests[-1]["body"]["messages"][RECIPIENT]["DESKTOP"]
        assert "secret" not in str(encrypted)
        event = nio.ToDeviceEvent.parse_event({"type": "m.room.encrypted", "sender": SENDER, "content": encrypted})
        assert isinstance(event, nio.OlmEvent)
        received: list[nio.AuthenticatedToDeviceEvent] = []
        peer.add_to_device_callback(received.append, nio.AuthenticatedToDeviceEvent)
        await peer.receive_response(
            nio.SyncResponse.from_dict({"next_batch": "s1", "to_device": {"events": [event.source]}}),
        )
        assert len(received) == 1
        decrypted = received[0]
        assert isinstance(decrypted, nio.AuthenticatedToDeviceEvent)
        assert decrypted.source["content"] == {"secret": "value"}
        assert decrypted.authenticated_sender.device_id == "CLOUD"
        assert not client.olm.device_store[RECIPIENT]["DESKTOP"].verified


@pytest.mark.asyncio
async def test_public_transport_pin_mismatch_has_application_error() -> None:
    async with olm_transport() as (client, _, requests, _):
        with pytest.raises(OlmToDeviceError, match="pin"):
            await send_encrypted_to_device(
                client,
                PinnedMatrixDevice(RECIPIENT, "DESKTOP", "wrong"),
                event_type="io.mindroom.test",
                content={},
            )
        assert len(requests) == 1
        assert requests[0]["path"].endswith("/keys/query")


@pytest.mark.asyncio
async def test_resolve_pinned_device_rejects_missing_fresh_identity() -> None:
    async with olm_transport() as (client, peer, _, query_override):
        assert peer.olm is not None
        target = PinnedMatrixDevice(RECIPIENT, "DESKTOP", peer.olm.account.identity_keys["ed25519"])
        await resolve_pinned_device(client, target)
        query_override.update({"device_keys": {}, "failures": {"example.org": {}}})
        with pytest.raises(OlmToDeviceError):
            await resolve_pinned_device(client, target)


@pytest.mark.parametrize("change", [None, "sender", "device", "fingerprint", "curve", "deleted", "blacklisted"])
def test_authenticated_sender_requires_captured_identity_and_current_authority(change: str | None) -> None:
    config = nio.AsyncClientConfig(store=SqliteMemoryStore)
    client = nio.AsyncClient("https://unused.invalid", SENDER, "CLOUD", config=config)
    client.restore_login(SENDER, "CLOUD", "test-token")
    assert client.olm is not None
    device = OlmDevice(RECIPIENT, "DESKTOP", {"curve25519": "curve", "ed25519": "signing"})
    client.olm.device_store.add(device)
    client.olm.store.save_device_keys({RECIPIENT: {"DESKTOP": device}})
    evidence = nio.AuthenticatedDevice(RECIPIENT, "DESKTOP", "curve", "signing")
    event = nio.AuthenticatedToDeviceEvent({}, RECIPIENT, "io.mindroom.test", evidence)
    target = PinnedMatrixDevice(RECIPIENT, "DESKTOP", "signing")
    if change == "sender":
        event.sender = "@other:example.org"
    elif change == "device":
        event.authenticated_sender = replace(evidence, device_id="OTHER")
    elif change == "fingerprint":
        event.authenticated_sender = replace(evidence, ed25519="different")
    elif change == "curve":
        device.curve25519 = "rotated"
    elif change == "deleted":
        device.deleted = True
    elif change == "blacklisted":
        client.blacklist_device(device)
    try:
        assert authenticated_sender_matches(client, event, target) is (change is None)
    finally:
        client.store.database.close()
