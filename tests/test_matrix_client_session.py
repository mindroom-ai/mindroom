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
from nio.ingest.config import ClassicSourceConfig, IngestionConfig
from nio.store.database import DefaultStore, SqliteStore

from mindroom.bot import _SYNC_TIMELINE_LIMIT
from mindroom.constants import (
    CONFIG_CONFIRMATION_REACTION_KEY,
    STREAM_STATUS_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
    RuntimePaths,
)
from mindroom.event_journal.models import IngestionConsumer
from mindroom.matrix import _owned_session, client_session
from mindroom.matrix.client_session import (
    MatrixSyncStorage,
    MindRoomAsyncClient,
    PermanentMatrixStartupError,
    login_flows,
    login_with_token,
    matrix_client_config,
)


def test_encryption_exposes_only_mindroom_recovery_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    client = MindRoomAsyncClient("https://example.org", "@mindroom_agent:example.org")

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


def test_encryption_does_not_add_metadata_to_ordinary_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary encrypted messages retain nio's standard envelope."""

    def fake_encrypt(
        _client: nio.AsyncClient,
        _room_id: str,
        _message_type: str,
        _content: dict[Any, Any],
    ) -> tuple[str, dict[str, str]]:
        return "m.room.encrypted", {"ciphertext": "encrypted payload"}

    monkeypatch.setattr(nio.AsyncClient, "encrypt", fake_encrypt)
    client = MindRoomAsyncClient("https://example.org", "@mindroom_agent:example.org")

    _, encrypted_content = client.encrypt(
        "!room:example.org",
        "m.room.message",
        {"body": "private answer text", "msgtype": "m.text"},
    )

    assert encrypted_content == {"ciphertext": "encrypted payload"}


def test_explicit_zero_one_time_key_count_requests_replenishment(
    tmp_path: Path,
) -> None:
    """A drained server OTK pool must make nio upload replacement keys."""
    user_id = "@agent:example.org"
    client = MindRoomAsyncClient(
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


def test_matrix_client_config_backfills_far_past_the_sync_window() -> None:
    """A room busy enough to truncate its sync window must still be recoverable.

    nio's default event cap abandons recovery after four sync windows of
    catch-up, which a single burst of streaming agent edits already exceeds.
    """
    config = matrix_client_config()

    assert config.backfill_max_events >= 20 * _SYNC_TIMELINE_LIMIT
    assert config.backfill_max_events > nio.AsyncClientConfig().backfill_max_events


def test_matrix_client_config_supports_application_owned_classic_sync() -> None:
    """Classic ingress can disable nio's durable cursor and recovery journal."""
    config = matrix_client_config(
        sync_storage=MatrixSyncStorage(
            store_tokens=False,
            persist_recovery=False,
        ),
    )

    assert config.backfill_limited_timelines is True
    assert config.backfill_persist_recovery is False
    assert config.store_sync_tokens is False


@pytest.mark.asyncio
async def _retired_unrecovered_timeline_gap_survives_client_restart(tmp_path: Path) -> None:
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

    def load_client() -> MindRoomAsyncClient:
        client = MindRoomAsyncClient(
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


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permission bits are unavailable on Windows",
)
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
    response = nio.LoginResponse(
        "@desktop:example.org",
        "DESKTOP",
        "matrix-access-token",
    )
    login_client = SimpleNamespace(
        login=AsyncMock(return_value=response),
        close=AsyncMock(),
    )
    create_login_client = Mock(return_value=login_client)
    restored_client = object()
    create_authenticated = Mock(return_value=restored_client)
    monkeypatch.setattr(client_session, "_create_matrix_client", create_login_client)
    monkeypatch.setattr(
        client_session,
        "create_authenticated_client",
        create_authenticated,
    )
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
        sync_storage=MatrixSyncStorage(),
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
        sync_storage=MatrixSyncStorage(),
    )


@pytest.mark.asyncio
async def test_login_with_token_uses_supplied_sync_storage_for_both_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Token exchange and restored client retain one caller's sync policy.

    This fails if ``login_with_token`` drops the policy at either client
    construction boundary.
    """
    response = nio.LoginResponse(
        "@desktop:example.org",
        "DESKTOP",
        "matrix-access-token",
    )
    login_client = SimpleNamespace(
        login=AsyncMock(return_value=response),
        close=AsyncMock(),
    )
    create_login_client = Mock(return_value=login_client)
    restored_client = object()
    create_authenticated = Mock(return_value=restored_client)
    monkeypatch.setattr(client_session, "_create_matrix_client", create_login_client)
    monkeypatch.setattr(
        client_session,
        "create_authenticated_client",
        create_authenticated,
    )
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )
    sync_storage = MatrixSyncStorage(
        recover_limited_timelines=False,
        persist_recovery=False,
        store_tokens=True,
    )

    result = await login_with_token(
        "https://matrix.example.org",
        "short-lived-token",
        runtime_paths,
        sync_storage=sync_storage,
    )

    assert result is restored_client
    assert create_login_client.call_args.kwargs["sync_storage"] is sync_storage
    assert create_authenticated.call_args.kwargs["sync_storage"] is sync_storage


@pytest.mark.asyncio
async def test_login_with_token_revokes_unexpected_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """SSO cannot silently enroll a different Matrix account than requested."""
    login_client = SimpleNamespace(
        login=AsyncMock(
            return_value=nio.LoginResponse(
                "@wrong:example.org",
                "WRONG",
                "access-token",
            ),
        ),
        logout=AsyncMock(return_value=nio.LogoutResponse()),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        client_session,
        "_create_matrix_client",
        Mock(return_value=login_client),
    )
    create_authenticated = Mock()
    monkeypatch.setattr(
        client_session,
        "create_authenticated_client",
        create_authenticated,
    )

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
        login_info=AsyncMock(
            return_value=nio.LoginInfoResponse(["m.login.token", "m.login.sso"]),
        ),
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


@pytest.mark.asyncio
async def test_password_credentials_use_storeless_client_and_close_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Credential HTTP must finish and close before exclusive store construction."""
    response = nio.LoginResponse(
        "@agent:example.org",
        "AGENTDEVICE",
        "access-token",
    )
    temporary = SimpleNamespace(
        login=AsyncMock(return_value=response),
        close=AsyncMock(),
        store=None,
        olm=None,
        store_path=None,
    )
    create_temporary = Mock(return_value=temporary)
    monkeypatch.setattr(
        _owned_session,
        "_create_credential_client",
        create_temporary,
        raising=False,
    )
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )

    credentials = await _owned_session.login_password_credentials(
        "https://matrix.example.org",
        "@agent:example.org",
        "password",
        runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )

    assert type(credentials) is _owned_session.MatrixCredentials
    assert (
        credentials.user_id,
        credentials.device_id,
        credentials.access_token,
    ) == (
        "@agent:example.org",
        "AGENTDEVICE",
        "access-token",
    )
    create_temporary.assert_called_once_with(
        "https://matrix.example.org",
        runtime_paths,
        "@agent:example.org",
        http_headers={"X-Access-Client": "test-secret"},
    )
    temporary.login.assert_awaited_once_with("password")
    temporary.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("login_result", "expected_exception"),
    [
        (nio.LoginError("rejected", "M_FORBIDDEN"), PermanentMatrixStartupError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
)
async def test_password_credentials_close_temporary_client_on_failure_or_cancel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    login_result: object,
    expected_exception: type[BaseException],
) -> None:
    """No credential failure may leave HTTP or a pre-lease client alive."""
    login = (
        AsyncMock(side_effect=login_result)
        if isinstance(login_result, BaseException)
        else AsyncMock(return_value=login_result)
    )
    temporary = SimpleNamespace(
        login=login,
        close=AsyncMock(),
        store=None,
        olm=None,
        store_path=None,
    )
    monkeypatch.setattr(
        _owned_session,
        "_create_credential_client",
        Mock(return_value=temporary),
        raising=False,
    )
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )

    with pytest.raises(expected_exception):
        await _owned_session.login_password_credentials(
            "https://matrix.example.org",
            "@agent:example.org",
            "password",
            runtime_paths,
        )

    temporary.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_restored_credentials_verify_identity_storeless_and_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Restored tokens are verified before any configured store is opened."""
    temporary = SimpleNamespace(
        user_id="",
        device_id="",
        access_token="",
        whoami=AsyncMock(
            return_value=nio.WhoamiResponse(
                "@agent:example.org",
                "AGENTDEVICE",
                False,
            ),
        ),
        close=AsyncMock(),
        store=None,
        olm=None,
        store_path=None,
    )
    create_temporary = Mock(return_value=temporary)
    monkeypatch.setattr(
        _owned_session,
        "_create_credential_client",
        create_temporary,
    )
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )

    credentials = await _owned_session.restore_credentials(
        "https://matrix.example.org",
        "@agent:example.org",
        "AGENTDEVICE",
        "access-token",
        runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )

    assert credentials == _owned_session.MatrixCredentials(
        "@agent:example.org",
        "AGENTDEVICE",
        "access-token",
    )
    assert (
        temporary.user_id,
        temporary.device_id,
        temporary.access_token,
    ) == (
        credentials.user_id,
        credentials.device_id,
        credentials.access_token,
    )
    create_temporary.assert_called_once_with(
        "https://matrix.example.org",
        runtime_paths,
        "@agent:example.org",
        http_headers={"X-Access-Client": "test-secret"},
    )
    temporary.whoami.assert_awaited_once()
    temporary.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_owned_matrix_session_factory_creates_one_fresh_store_and_binding(
    tmp_path: Path,
) -> None:
    """Fresh credentials transfer one store and stream into one owned session."""
    generation = UUID("22222222-2222-4222-8222-222222222222")
    consumer_store = SimpleNamespace(
        load_or_create_ingestion_consumer=AsyncMock(
            return_value=IngestionConsumer(generation, None),
        ),
        bind_ingestion_stream=AsyncMock(),
    )

    async def bind_stream(*, generation: UUID, stream_id: UUID) -> IngestionConsumer:
        return IngestionConsumer(generation, stream_id)

    consumer_store.bind_ingestion_stream.side_effect = bind_stream
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )
    credentials = _owned_session.MatrixCredentials(
        "@agent:example.org",
        "AGENTDEVICE",
        "access-token",
    )
    config = IngestionConfig(
        ClassicSourceConfig(
            timeout_ms=30_000,
            filter_json=b'{"room":{"timeline":{"limit":50}}}',
        ),
    )

    opened = await _owned_session.open_owned_matrix_session(
        "https://matrix.example.org",
        credentials,
        runtime_paths,
        consumer_store=consumer_store,
        new_consumer_generation=generation,
        config=config,
        http_headers={"X-Access-Client": "test-secret"},
    )
    try:
        assert type(opened) is _owned_session.OwnedMatrixSession
        assert opened.consumer.generation == generation
        assert type(opened.consumer.stream_id) is UUID
        assert opened.client.user_id == credentials.user_id
        assert opened.client.device_id == credentials.device_id
        assert opened.client.access_token == credentials.access_token
        assert type(opened.client.store) is SqliteStore
        assert opened.client.olm is not None
        assert opened.session.next_batch(max_records=1) is None
        consumer_store.load_or_create_ingestion_consumer.assert_awaited_once_with(
            new_generation=generation,
        )
        consumer_store.bind_ingestion_stream.assert_awaited_once_with(
            generation=generation,
            stream_id=opened.consumer.stream_id,
        )
        database_path = (
            client_session.olm_store_dir(credentials.user_id, runtime_paths)
            / f"{credentials.user_id}_{credentials.device_id}.db"
        )
        assert database_path.is_file()
    finally:
        await opened.session.close()
        assert opened.client.store is None
        assert opened.client.olm is None
        await opened.client.close()


@pytest.mark.asyncio
async def test_owned_matrix_session_factory_failure_closes_http_and_reopens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed transfer closes its client and releases the marked store lease."""
    generation = UUID("22222222-2222-4222-8222-222222222222")
    consumer_store = SimpleNamespace(
        load_or_create_ingestion_consumer=AsyncMock(
            return_value=IngestionConsumer(generation, None),
        ),
        bind_ingestion_stream=AsyncMock(
            side_effect=lambda *, generation, stream_id: IngestionConsumer(
                generation,
                stream_id,
            ),
        ),
    )
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )
    credentials = _owned_session.MatrixCredentials(
        "@agent:example.org",
        "AGENTDEVICE",
        "access-token",
    )
    config = IngestionConfig(
        ClassicSourceConfig(
            timeout_ms=30_000,
            filter_json=b'{"room":{"timeline":{"limit":50}}}',
        ),
    )
    real_open = _owned_session._open_owned_ingestion
    failed_clients: list[nio.AsyncClient] = []
    transfer_error = RuntimeError("owned transfer failed")

    def fail_transfer(client: nio.AsyncClient, *_args: object, **_kwargs: object) -> None:
        client.close = AsyncMock(  # type: ignore[method-assign]
            side_effect=OSError("HTTP close failed"),
        )
        failed_clients.append(client)
        raise transfer_error

    monkeypatch.setattr(_owned_session, "_open_owned_ingestion", fail_transfer)
    with pytest.raises(RuntimeError, match="owned transfer failed"):
        await _owned_session.open_owned_matrix_session(
            "https://matrix.example.org",
            credentials,
            runtime_paths,
            consumer_store=consumer_store,
            new_consumer_generation=generation,
            config=config,
        )

    assert len(failed_clients) == 1
    failed_clients[0].close.assert_awaited_once()  # type: ignore[attr-defined]
    monkeypatch.setattr(_owned_session, "_open_owned_ingestion", real_open)
    reopened = await _owned_session.open_owned_matrix_session(
        "https://matrix.example.org",
        credentials,
        runtime_paths,
        consumer_store=consumer_store,
        new_consumer_generation=generation,
        config=config,
    )
    await reopened.session.close()
    await reopened.client.close()


@pytest.mark.asyncio
async def test_owned_matrix_session_factory_cancellation_releases_bootstrap(
    tmp_path: Path,
) -> None:
    """Cancellation while binding the consumer leaves the fresh graph reopenable."""
    generation = UUID("22222222-2222-4222-8222-222222222222")
    bind_calls = 0

    async def bind_stream(*, generation: UUID, stream_id: UUID) -> IngestionConsumer:
        nonlocal bind_calls
        bind_calls += 1
        if bind_calls == 1:
            raise asyncio.CancelledError
        return IngestionConsumer(generation, stream_id)

    consumer_store = SimpleNamespace(
        load_or_create_ingestion_consumer=AsyncMock(
            return_value=IngestionConsumer(generation, None),
        ),
        bind_ingestion_stream=AsyncMock(side_effect=bind_stream),
    )
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )
    credentials = _owned_session.MatrixCredentials(
        "@agent:example.org",
        "AGENTDEVICE",
        "access-token",
    )
    config = IngestionConfig(
        ClassicSourceConfig(
            timeout_ms=30_000,
            filter_json=b'{"room":{"timeline":{"limit":50}}}',
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        await _owned_session.open_owned_matrix_session(
            "https://matrix.example.org",
            credentials,
            runtime_paths,
            consumer_store=consumer_store,
            new_consumer_generation=generation,
            config=config,
        )

    reopened = await _owned_session.open_owned_matrix_session(
        "https://matrix.example.org",
        credentials,
        runtime_paths,
        consumer_store=consumer_store,
        new_consumer_generation=generation,
        config=config,
    )
    await reopened.session.close()
    await reopened.client.close()


@pytest.mark.asyncio
async def test_owned_matrix_session_factory_adopts_default_then_reopens_marked(
    tmp_path: Path,
) -> None:
    """Historical DefaultStore identity survives adoption and typed marked retry."""
    generation = UUID("22222222-2222-4222-8222-222222222222")
    bound = IngestionConsumer(generation, None)

    async def load_consumer(*, new_generation: UUID) -> IngestionConsumer:
        assert new_generation == generation
        return bound

    async def bind_stream(*, generation: UUID, stream_id: UUID) -> IngestionConsumer:
        nonlocal bound
        assert generation == bound.generation
        if bound.stream_id is not None:
            assert stream_id == bound.stream_id
        bound = IngestionConsumer(generation, stream_id)
        return bound

    consumer_store = SimpleNamespace(
        load_or_create_ingestion_consumer=AsyncMock(side_effect=load_consumer),
        bind_ingestion_stream=AsyncMock(side_effect=bind_stream),
    )
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "data",
    )
    credentials = _owned_session.MatrixCredentials(
        "@agent:example.org",
        "AGENTDEVICE",
        "access-token",
    )
    legacy = client_session.create_authenticated_client(
        "https://matrix.example.org",
        credentials.user_id,
        credentials.device_id,
        credentials.access_token,
        runtime_paths,
    )
    assert type(legacy.store) is DefaultStore
    assert legacy.olm is not None
    identity_keys = dict(legacy.olm.account.identity_keys)
    await legacy.close()
    legacy.store.database.close()
    config = IngestionConfig(
        ClassicSourceConfig(
            timeout_ms=30_000,
            filter_json=b'{"room":{"timeline":{"limit":50}}}',
        ),
    )

    adopted = await _owned_session.open_owned_matrix_session(
        "https://matrix.example.org",
        credentials,
        runtime_paths,
        consumer_store=consumer_store,
        new_consumer_generation=generation,
        config=config,
    )
    assert type(adopted.client.store) is SqliteStore
    assert adopted.client.olm is not None
    assert adopted.client.olm.account.identity_keys == identity_keys
    first_stream = adopted.consumer.stream_id
    await adopted.session.close()
    await adopted.client.close()

    reopened = await _owned_session.open_owned_matrix_session(
        "https://matrix.example.org",
        credentials,
        runtime_paths,
        consumer_store=consumer_store,
        new_consumer_generation=generation,
        config=config,
    )
    try:
        assert reopened.consumer.stream_id == first_stream
        assert type(reopened.client.store) is SqliteStore
        assert reopened.client.olm is not None
        assert reopened.client.olm.account.identity_keys == identity_keys
    finally:
        await reopened.session.close()
        await reopened.client.close()
