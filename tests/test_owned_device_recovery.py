"""Device renewal preserves the owned transport and its application consumer."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import nio
import pytest
from nio.durable import DurableSyncConfig

from mindroom.event_journal import EventJournalStore, PrincipalStore
from mindroom.matrix import _owned_session, appservice, users
from mindroom.matrix.client_session import MindRoomAsyncClient, PermanentMatrixStartupError, olm_store_dir
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from tests.test_matrix_agent_manager import _runtime_paths

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import IngestionConsumer

USER = "@mindroom_agent:localhost"
DEVICE = "DEVICE"


def _credential_http(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace only authentication HTTP; retain real owned stores and persistence."""
    requested_devices: list[str] = []
    create = _owned_session.create_matrix_http_client

    def client(homeserver: str, runtime_paths: RuntimePaths, user_id: str, **kwargs: object) -> nio.AsyncClient:
        temporary = create(homeserver, runtime_paths, user_id, **kwargs)

        async def login(_password: str) -> nio.LoginResponse:
            requested_devices.append(temporary.device_id)
            return nio.LoginResponse(USER, temporary.device_id or DEVICE, "renewed-token")

        monkeypatch.setattr(temporary, "login", login)
        monkeypatch.setattr(
            temporary,
            "whoami",
            AsyncMock(
                return_value=nio.WhoamiError(
                    "expired",
                    "M_UNKNOWN_TOKEN",
                    soft_logout=True,
                ),
            ),
        )
        return temporary

    monkeypatch.setattr(_owned_session, "create_matrix_http_client", client)
    monkeypatch.setattr(users, "ensure_agent_cross_signing", AsyncMock())
    monkeypatch.setattr(MindRoomAsyncClient, "set_displayname", AsyncMock())

    async def appservice_login(*_args: object, payload: dict[str, object], **_kwargs: object) -> httpx.Response:
        requested_devices.append(str(payload.get("device_id", "")))
        return httpx.Response(
            200,
            json={"user_id": USER, "device_id": payload.get("device_id", DEVICE), "access_token": "renewed-token"},
        )

    monkeypatch.setattr(appservice, "_post", appservice_login)
    return requested_devices


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["password", "appservice"])
async def test_soft_logout_keeps_stream_crypto_and_retained_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: str,
) -> None:
    """Renewal must not abandon a producer batch, rotate keys, or replace its consumer."""
    requested_devices = _credential_http(monkeypatch)
    environment = {"MATRIX_MANAGED_ACCOUNT_AUTH": auth_mode}
    if auth_mode == "appservice":
        environment["MATRIX_APPSERVICE_TOKEN"] = "test-appservice-token"  # noqa: S105
    runtime_paths = _runtime_paths(tmp_path, **environment)
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    agent = AgentMatrixUser("agent", USER, "Agent", "password")
    principal = journal.principal(USER)

    async def open_session() -> _owned_session.OwnedMatrixSession:
        return await users.login_agent_owned_session(
            "https://example.org",
            agent,
            runtime_paths,
            consumer_store=principal,
            new_consumer_generation=uuid4(),
            config=DurableSyncConfig(),
        )

    try:
        first = await open_session()
        try:
            assert first.client.olm is not None
            keys = first.client.olm.account.identity_keys
            consumer = first.consumer
            first.session._transport.request = AsyncMock(
                return_value=json.dumps({"next_batch": "retained", "rooms": {}}).encode(),
            )
            first.session._maintain_crypto = AsyncMock()
            producer = asyncio.create_task(first.session.run())
            try:
                await asyncio.wait_for(first.session.wait_for_work(), timeout=2)
            finally:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            retained = await first.session.next_batch()
            assert retained is not None
        finally:
            await first.session.close()
            await first.client.close()
        reopened = await open_session()
        try:
            assert reopened.consumer == consumer
            assert reopened.client.olm is not None
            assert reopened.client.olm.account.identity_keys == keys
            assert await reopened.session.next_batch() == retained
            assert requested_devices == ["", DEVICE]
            saved = MatrixState.load(runtime_paths=runtime_paths).accounts["agent_agent"]
            assert saved.device_id == DEVICE
        finally:
            await reopened.session.close()
            await reopened.client.close()
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_initial_login_crash_before_binding_reuses_persisted_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash at journal binding must leave the exact local device recoverable."""
    requested_devices = _credential_http(monkeypatch)
    runtime_paths = _runtime_paths(tmp_path)
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    principal = journal.principal(USER)
    agent = AgentMatrixUser("agent", USER, "Agent", "password")
    bind = PrincipalStore.bind_ingestion_stream
    interrupted = False

    async def interrupt_binding(store: PrincipalStore, *, generation: UUID, stream_id: UUID) -> IngestionConsumer:
        nonlocal interrupted
        saved = MatrixState.load(runtime_paths=runtime_paths).accounts["agent_agent"]
        assert saved.device_id == DEVICE
        assert (olm_store_dir(USER, runtime_paths) / f"{USER}_{DEVICE}.db").is_file()
        if not interrupted:
            interrupted = True
            message = "crash before binding"
            raise RuntimeError(message)
        return await bind(store, generation=generation, stream_id=stream_id)

    monkeypatch.setattr(PrincipalStore, "bind_ingestion_stream", interrupt_binding)
    try:
        with pytest.raises(RuntimeError, match="crash before binding"):
            await users.login_agent_owned_session(
                "https://example.org",
                agent,
                runtime_paths,
                consumer_store=principal,
                new_consumer_generation=uuid4(),
                config=DurableSyncConfig(),
            )
        saved = MatrixState.load(runtime_paths=runtime_paths).accounts["agent_agent"]
        restarted = replace(agent, device_id=saved.device_id, access_token=saved.access_token)
        opened = await users.login_agent_owned_session(
            "https://example.org",
            restarted,
            runtime_paths,
            consumer_store=principal,
            new_consumer_generation=uuid4(),
            config=DurableSyncConfig(),
        )
        await opened.session.close()
        await opened.client.close()
        assert requested_devices == ["", DEVICE]
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_missing_device_store_refuses_login_before_replacing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing crypto must never silently rotate a bound account to another stream."""
    requested_devices = _credential_http(monkeypatch)
    runtime_paths = _runtime_paths(tmp_path)
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    agent = AgentMatrixUser("agent", USER, "Agent", "password", device_id=DEVICE, access_token="old-token")  # noqa: S106
    try:
        with pytest.raises(PermanentMatrixStartupError, match="store is missing"):
            await users.login_agent_owned_session(
                "https://example.org",
                agent,
                runtime_paths,
                consumer_store=journal.principal(USER),
                new_consumer_generation=uuid4(),
                config=DurableSyncConfig(),
            )
        assert requested_devices == []
        assert agent.access_token == "old-token"  # noqa: S105
        assert not MatrixState.load(runtime_paths=runtime_paths).accounts
    finally:
        await journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hard_logout", "transient", "wrong_device", "wrong_user"])
async def test_failed_restore_preserves_binding_and_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Only a confirmed soft logout may reach another authentication request."""
    requested_devices = _credential_http(monkeypatch)
    runtime_paths = _runtime_paths(tmp_path)
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    principal = journal.principal(USER)
    agent = AgentMatrixUser("agent", USER, "Agent", "password")
    try:
        first = await users.login_agent_owned_session(
            "https://example.org",
            agent,
            runtime_paths,
            consumer_store=principal,
            new_consumer_generation=uuid4(),
            config=DurableSyncConfig(),
        )
        consumer = first.consumer
        await first.session.close()
        await first.client.close()
        state_before = MatrixState.load(runtime_paths=runtime_paths)
        responses = {
            "hard_logout": nio.WhoamiError("deleted device", "M_UNKNOWN_TOKEN", soft_logout=False),
            "transient": nio.WhoamiError("unavailable", "M_UNKNOWN"),
            "wrong_device": nio.WhoamiResponse(USER, "OTHER", False),
            "wrong_user": nio.WhoamiResponse("@other:localhost", DEVICE, False),
        }
        create = _owned_session.create_matrix_http_client

        def credential_client(*args: object, **kwargs: object) -> nio.AsyncClient:
            client = create(*args, **kwargs)
            monkeypatch.setattr(client, "whoami", AsyncMock(return_value=responses[failure]))
            return client

        monkeypatch.setattr(_owned_session, "create_matrix_http_client", credential_client)
        with pytest.raises(ValueError, match="Matrix") as error:
            await users.login_agent_owned_session(
                "https://example.org",
                agent,
                runtime_paths,
                consumer_store=principal,
                new_consumer_generation=uuid4(),
                config=DurableSyncConfig(),
            )
        assert isinstance(error.value, PermanentMatrixStartupError) is (failure != "transient")
        assert requested_devices == [""]
        assert await principal.load_or_create_ingestion_consumer(new_generation=uuid4()) == consumer
        assert MatrixState.load(runtime_paths=runtime_paths) == state_before
    finally:
        await journal.close()
