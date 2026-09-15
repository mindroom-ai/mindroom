"""Tests for private local desktop Matrix sessions."""

# ruff: noqa: S106 - These are test-only credential values.

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import nio
import pytest

from mindroom.desktop.session import (
    DesktopLoginMethod,
    DesktopMatrixSession,
    DesktopSessionError,
    _open_owned_session,
    _prepare_crypto,
    _transport_binding,
    desktop_transport_binding_path,
    load_desktop_http_headers,
    load_desktop_session,
    login_desktop_client,
    open_desktop_client,
    resolve_desktop_login_method,
    save_desktop_session,
)
from tests.conftest import test_runtime_paths as make_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _session() -> DesktopMatrixSession:
    return DesktopMatrixSession(
        homeserver="https://matrix.example.org",
        user_id="@desktop:example.org",
        device_id="DESKTOP",
        access_token="secret-access-token",
    )


def test_session_round_trip_uses_owner_only_permissions(tmp_path: Path) -> None:
    """The reusable Matrix token is never persisted with ambient read access."""
    path = tmp_path / "desktop" / "matrix_session.json"

    save_desktop_session(path, _session())

    assert load_desktop_session(path) == _session()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_session_round_trip_remembers_interactive_access_transport(
    tmp_path: Path,
) -> None:
    """Bridge startup can renew Access without requiring the flag again."""
    path = tmp_path / "desktop" / "matrix_session.json"
    session = DesktopMatrixSession(
        homeserver="https://matrix.example.org",
        user_id="@desktop:example.org",
        device_id="DESKTOP",
        access_token="secret-access-token",
        cloudflare_access=True,
    )

    save_desktop_session(path, session)

    assert load_desktop_session(path) == session


@pytest.mark.skipif(
    os.name == "nt",
    reason="Unix permission bits are not authoritative on Windows",
)
def test_session_refuses_group_readable_token(tmp_path: Path) -> None:
    """An accidentally exposed token stops the bridge instead of being used."""
    path = tmp_path / "matrix_session.json"
    path.write_text(json.dumps(_session().to_payload()), encoding="utf-8")
    path.chmod(0o640)

    with pytest.raises(DesktopSessionError, match="must not be readable"):
        load_desktop_session(path)


def test_session_rejects_malformed_payload(tmp_path: Path) -> None:
    """Incomplete credentials never reach the Matrix client."""
    path = tmp_path / "matrix_session.json"
    path.write_text('{"v": 1, "user_id": "@desktop:example.org"}', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(DesktopSessionError, match="field homeserver"):
        load_desktop_session(path)


def test_session_missing_path_has_setup_instruction(tmp_path: Path) -> None:
    """A genuinely absent session gets the actionable login instruction."""
    with pytest.raises(DesktopSessionError, match="desktop login"):
        load_desktop_session(tmp_path / "missing.json")


def test_session_preserves_unexpected_filesystem_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permission and device errors retain their native exception and traceback."""
    path = tmp_path / "matrix_session.json"
    path.write_text(json.dumps(_session().to_payload()), encoding="utf-8")
    path.chmod(0o600)

    def denied(*_args: object, **_kwargs: object) -> str:
        message = "test permission failure"
        raise PermissionError(message)

    monkeypatch.setattr(path.__class__, "read_text", denied)

    with pytest.raises(PermissionError, match="test permission failure"):
        load_desktop_session(path)


def test_http_headers_file_loads_string_mapping(tmp_path: Path) -> None:
    """Proxy credentials remain in a separate private file instead of session state."""
    path = tmp_path / "matrix-http-headers.json"
    path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    path.chmod(0o600)

    assert load_desktop_http_headers(path) == {"X-Access-Client": "test-secret"}


@pytest.mark.skipif(
    os.name == "nt",
    reason="Unix permission bits are not authoritative on Windows",
)
def test_http_headers_file_refuses_group_readable_secrets(tmp_path: Path) -> None:
    """An exposed proxy credential file stops before any Matrix request."""
    path = tmp_path / "matrix-http-headers.json"
    path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    path.chmod(0o640)

    with pytest.raises(DesktopSessionError, match="must not be readable"):
        load_desktop_http_headers(path)


@pytest.mark.parametrize("payload", ['["not-an-object"]', '{"X-Access-Client": 1}'])
def test_http_headers_file_requires_string_mapping(
    tmp_path: Path,
    payload: str,
) -> None:
    """Malformed header configuration fails before nio receives it."""
    path = tmp_path / "matrix-http-headers.json"
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(DesktopSessionError, match="JSON object of string values"):
        load_desktop_http_headers(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flows", "expected"),
    [
        (("m.login.password", "m.login.sso"), DesktopLoginMethod.SSO),
        (("m.login.password",), DesktopLoginMethod.PASSWORD),
        (("m.login.token", "m.login.sso"), DesktopLoginMethod.SSO),
    ],
)
async def test_auto_login_method_uses_advertised_matrix_flows(
    monkeypatch: pytest.MonkeyPatch,
    flows: tuple[str, ...],
    expected: DesktopLoginMethod,
) -> None:
    """Auto prefers browser SSO and falls back to password-only homeservers."""
    query = AsyncMock(return_value=flows)
    monkeypatch.setattr("mindroom.desktop.session.login_flows", query)
    runtime_paths = SimpleNamespace()

    resolved = await resolve_desktop_login_method(
        DesktopLoginMethod.AUTO,
        homeserver="https://matrix.example.org",
        runtime_paths=runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )

    assert resolved is expected
    query.assert_awaited_once_with(
        "https://matrix.example.org",
        runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )


@pytest.mark.asyncio
async def test_explicit_login_method_skips_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators can force SSO when a homeserver also advertises password login."""
    query = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.session.login_flows", query)

    resolved = await resolve_desktop_login_method(
        DesktopLoginMethod.SSO,
        homeserver="https://matrix.example.org",
        runtime_paths=SimpleNamespace(),
    )

    assert resolved is DesktopLoginMethod.SSO
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_login_method_rejects_unsupported_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application-service-only servers produce a clear local setup error."""
    monkeypatch.setattr(
        "mindroom.desktop.session.login_flows",
        AsyncMock(return_value=("m.login.application_service",)),
    )

    with pytest.raises(DesktopSessionError, match=r"m\.login\.application_service"):
        await resolve_desktop_login_method(
            DesktopLoginMethod.AUTO,
            homeserver="https://matrix.example.org",
            runtime_paths=SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_auto_login_method_translates_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login discovery reports transport failure as one actionable desktop error."""
    monkeypatch.setattr(
        "mindroom.desktop.session.login_flows",
        AsyncMock(side_effect=aiohttp.ClientConnectionError("homeserver unavailable")),
    )

    with pytest.raises(
        DesktopSessionError,
        match=r"Could not discover.*homeserver unavailable",
    ):
        await resolve_desktop_login_method(
            DesktopLoginMethod.AUTO,
            homeserver="https://matrix.example.org",
            runtime_paths=SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_crypto_preparation_uploads_keys_without_sync() -> None:
    """Preparing encryption must not consume commands outside durable admission."""
    client = SimpleNamespace(sync=AsyncMock(), should_upload_keys=True, keys_upload=AsyncMock(), olm=object())

    await _prepare_crypto(client)

    client.sync.assert_not_awaited()
    client.keys_upload.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("token_login", [False, True])
async def test_login_acquires_credentials_without_crypto_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token_login: bool,
) -> None:
    """Credential exchange cannot adopt a store before the exact device is known."""
    calls = []

    async def credential_login(
        client: nio.AsyncClient,
        password: str | None = None,
        **kwargs: object,
    ) -> nio.LoginResponse:
        assert not client.config.encryption_enabled
        assert client.store is None
        assert client.store_path is None
        calls.append({**kwargs, "password": password})
        return nio.LoginResponse(user_id="@desktop:example.org", device_id="DESKTOP", access_token="issued-token")

    async def reject_sync(*_args: object, **_kwargs: object) -> None:
        msg = "ordinary sync consumed desktop input"
        raise AssertionError(msg)

    monkeypatch.setattr(nio.AsyncClient, "login", credential_login)
    monkeypatch.setattr(nio.AsyncClient, "sync", reject_sync)
    monkeypatch.setattr(nio.AsyncClient, "keys_upload", AsyncMock())
    monkeypatch.setattr("mindroom.desktop.session.ensure_agent_cross_signing", AsyncMock())
    owner, session = await login_desktop_client(
        homeserver="https://matrix.example.org",
        user_id=None if token_login else "@desktop:example.org",
        password=None if token_login else "password",
        login_token="sso-token" if token_login else None,
        runtime_paths=make_runtime_paths(tmp_path),
    )
    try:
        assert session.user_id == "@desktop:example.org"
        assert session.device_id == "DESKTOP"
        assert owner.client.olm is not None
        assert owner.client.store is not None
        assert owner.source.cursor is None
        assert calls[0]["token" if token_login else "password"] == ("sso-token" if token_login else "password")
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_login_translates_expected_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Credential rejection becomes an actionable session error without opening crypto."""
    monkeypatch.setattr(
        nio.AsyncClient,
        "login",
        AsyncMock(return_value=nio.LoginError("invalid credentials", "M_FORBIDDEN")),
    )
    with pytest.raises(DesktopSessionError, match="invalid credentials"):
        await login_desktop_client(
            homeserver="https://matrix.example.org",
            user_id="@desktop:example.org",
            password="wrong",
            runtime_paths=make_runtime_paths(tmp_path),
        )


@pytest.mark.asyncio
async def test_login_translates_network_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Credential network failures are reported without opening a persistent store."""
    monkeypatch.setattr(
        nio.AsyncClient,
        "login",
        AsyncMock(side_effect=aiohttp.ClientConnectionError("connection refused")),
    )
    with pytest.raises(DesktopSessionError, match="connection refused"):
        await login_desktop_client(
            homeserver="https://matrix.example.org",
            user_id=None,
            login_token="sso-token",
            runtime_paths=make_runtime_paths(tmp_path),
        )


@pytest.mark.asyncio
async def test_saved_session_requires_existing_crypto_store(tmp_path: Path) -> None:
    """Restore cannot silently replace the established encryption identity."""
    with pytest.raises(DesktopSessionError, match="encryption store is missing"):
        await open_desktop_client(_session(), runtime_paths=make_runtime_paths(tmp_path))


@pytest.mark.asyncio
async def test_owned_session_preserves_binding_and_identity_across_reopen(tmp_path: Path) -> None:
    """The same consumer, stream, and Olm account survive the owner lifecycle."""
    runtime_paths = make_runtime_paths(tmp_path)
    owner = await _open_owned_session(_session(), runtime_paths=runtime_paths, allow_create=True)
    fingerprint = owner.client.olm.account.identity_keys["ed25519"]
    stream = owner.source.stream_id
    assert owner.source.config.to_device_only is True
    assert owner.source.config.sync_filter is None
    await owner.close()
    restored = await open_desktop_client(_session(), runtime_paths=runtime_paths)
    try:
        assert restored.source.stream_id == stream
        assert restored.client.olm.account.identity_keys["ed25519"] == fingerprint
    finally:
        await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["user_id", "device_id", "stream_id"])
async def test_owned_session_rejects_mismatched_binding(tmp_path: Path, field: str) -> None:
    """Account, device, and stream substitutions fail before desktop consumption."""
    runtime_paths = make_runtime_paths(tmp_path)
    owner = await _open_owned_session(_session(), runtime_paths=runtime_paths, allow_create=True)
    await owner.close()
    path = desktop_transport_binding_path(runtime_paths, _session())
    payload = json.loads(path.read_text())
    payload[field] = "00000000-0000-0000-0000-000000000001" if field == "stream_id" else "different"
    path.write_text(json.dumps(payload))
    with pytest.raises(DesktopSessionError, match="binding"):
        await open_desktop_client(_session(), runtime_paths=runtime_paths)


def test_concurrent_first_open_preserves_one_consumer_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent processes must not persist competing consumer identities before adoption."""
    from mindroom.desktop import session as session_module  # noqa: PLC0415

    write = session_module.write_json_file_durable
    start = Barrier(8)

    def delayed_write(*args: object, **kwargs: object) -> None:
        # Enlarge the competing read/write window without replacing filesystem persistence.
        time.sleep(0.01)
        write(*args, **kwargs)

    def create() -> dict[str, object]:
        start.wait()
        return _transport_binding(tmp_path / "binding.json", _session())

    monkeypatch.setattr(session_module, "write_json_file_durable", delayed_write)
    with ThreadPoolExecutor(max_workers=8) as executor:
        bindings = list(executor.map(lambda _index: create(), range(8)))
    assert len({binding["consumer_id"] for binding in bindings}) == 1
    assert json.loads((tmp_path / "binding.json").read_text()) == bindings[0]
