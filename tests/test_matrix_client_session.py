"""Tests for MindRoom-specific Matrix client behavior."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import nio
import pytest

from mindroom.constants import STREAM_STATUS_KEY, RuntimePaths
from mindroom.matrix import client_session
from mindroom.matrix.client_session import (
    PermanentMatrixStartupError,
    _MindRoomAsyncClient,
    login_flows,
    login_with_token,
    matrix_client_config,
)


def test_encryption_exposes_only_mindroom_stream_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encrypted stream events expose status but no private message fields."""
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
        },
    )

    assert message_type == "m.room.encrypted"
    assert encrypted_content == {
        "algorithm": "m.megolm.v1.aes-sha2",
        "ciphertext": "encrypted payload",
        "m.relates_to": relation,
        STREAM_STATUS_KEY: "streaming",
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
    assert matrix_client_config().backfill_limited_timelines is True


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
