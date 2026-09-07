"""Secondary account HTTP operations never take ownership of a crypto store."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import nio
import pytest
from nio.durable import DurableSyncConfig

from mindroom.event_journal import EventJournalStore
from mindroom.matrix import _owned_session, users
from mindroom.matrix.client_session import MindRoomAsyncClient
from mindroom.matrix.state import MatrixState
from tests.test_matrix_agent_manager import _runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_active", [True, False])
async def test_account_http_operations_preserve_an_owned_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_active: bool,
) -> None:
    """State/profile HTTP works during ownership and after an adopted store closes."""
    runtime_paths = _runtime_paths(tmp_path)
    credentials = _owned_session.MatrixCredentials("@mindroom_router:localhost", "DEVICE", "saved-token")
    state = MatrixState()
    state.add_account(
        "agent_router",
        "mindroom_router",
        "unused-password",
        domain="localhost",
        device_id=credentials.device_id,
        access_token=credentials.access_token,
    )
    state.save(runtime_paths)
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    principal = journal.principal(credentials.user_id)
    opened = await _owned_session.open_owned_matrix_session(
        "https://localhost",
        credentials,
        runtime_paths,
        consumer_store=principal,
        new_consumer_generation=uuid4(),
        config=DurableSyncConfig(),
    )
    assert opened.client.olm is not None
    keys = opened.client.olm.account.identity_keys
    consumer = opened.consumer
    if not owner_active:
        await opened.session.close()
        await opened.client.close()
    login = AsyncMock()
    monkeypatch.setattr(MindRoomAsyncClient, "login", login)
    try:
        client = users.create_agent_http_client("router", runtime_paths)
        try:
            assert client.store is None
            assert client.olm is None
            assert client.store_path is None
            assert not client.config.encryption_enabled
            assert client.user_id == credentials.user_id
            assert client.access_token == credentials.access_token
            # Use actual nio endpoint methods, replacing only HTTP transport.
            send = AsyncMock(
                side_effect=[
                    nio.JoinedRoomsResponse(["!room:localhost"]),
                    nio.RoomGetStateResponse([], "!room:localhost"),
                    nio.RoomPutStateResponse("$avatar", "!room:localhost"),
                ],
            )
            monkeypatch.setattr(client, "_send", send)
            assert isinstance(await client.joined_rooms(), nio.JoinedRoomsResponse)
            assert isinstance(await client.room_get_state("!room:localhost"), nio.RoomGetStateResponse)
            assert isinstance(
                await client.room_put_state("!room:localhost", "m.room.avatar", {"url": "mxc://localhost/avatar"}),
                nio.RoomPutStateResponse,
            )
            login.assert_not_awaited()
            assert MatrixState.load(runtime_paths) == state
        finally:
            await client.close()
        if owner_active:
            assert opened.client.olm.account.identity_keys == keys
        assert await principal.load_or_create_ingestion_consumer(new_generation=uuid4()) == consumer
    finally:
        if owner_active:
            await opened.session.close()
            await opened.client.close()
        await journal.close()


def test_account_http_requires_saved_credentials(tmp_path: Path) -> None:
    """An auxiliary request cannot provision or rotate a managed account."""
    runtime_paths = _runtime_paths(tmp_path)
    with pytest.raises(ValueError, match="authenticated Matrix account"):
        users.create_agent_http_client("router", runtime_paths)


@pytest.mark.asyncio
async def test_account_http_uses_persisted_account_domain(tmp_path: Path) -> None:
    """Auxiliary clients preserve the actual account ID even when its domain differs from the URL."""
    paths = _runtime_paths(tmp_path)
    state = MatrixState()
    state.add_account(
        "agent_router",
        "actual_router",
        "unused-password",
        domain="matrix.example",
        access_token="saved-token",  # noqa: S106
    )
    state.save(paths)
    client = users.create_agent_http_client("router", paths)
    try:
        assert client.user_id == "@actual_router:matrix.example"
        assert client.store is None
    finally:
        await client.close()
