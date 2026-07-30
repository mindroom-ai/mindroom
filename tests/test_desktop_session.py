"""Tests for private local desktop Matrix sessions."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import pytest

from mindroom.desktop.session import (
    DesktopLoginMethod,
    DesktopMatrixSession,
    DesktopSessionError,
    _prepare_crypto,
    load_desktop_http_headers,
    load_desktop_session,
    login_desktop_client,
    open_desktop_client,
    resolve_desktop_login_method,
    save_desktop_session,
)
from mindroom.matrix.client_session import PermanentMatrixStartupError

if TYPE_CHECKING:
    from pathlib import Path


def _session() -> DesktopMatrixSession:
    return DesktopMatrixSession(
        homeserver="https://matrix.example.org",
        user_id="@desktop:example.org",
        device_id="DESKTOP",
        access_token="secret-access-token",  # noqa: S106 - Test-only persisted token fixture.
    )


def test_session_round_trip_uses_owner_only_permissions(tmp_path: Path) -> None:
    """The reusable Matrix token is never persisted with ambient read access."""
    path = tmp_path / "desktop" / "matrix_session.json"

    save_desktop_session(path, _session())

    assert load_desktop_session(path) == _session()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_session_round_trip_remembers_interactive_access_transport(tmp_path: Path) -> None:
    """Bridge startup can renew Access without requiring the flag again."""
    path = tmp_path / "desktop" / "matrix_session.json"
    session = DesktopMatrixSession(
        homeserver="https://matrix.example.org",
        user_id="@desktop:example.org",
        device_id="DESKTOP",
        access_token="secret-access-token",  # noqa: S106 - Test-only persisted token fixture.
        cloudflare_access=True,
    )

    save_desktop_session(path, session)

    assert load_desktop_session(path) == session


@pytest.mark.skipif(os.name == "nt", reason="Unix permission bits are not authoritative on Windows")
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


def test_session_preserves_unexpected_filesystem_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.skipif(os.name == "nt", reason="Unix permission bits are not authoritative on Windows")
def test_http_headers_file_refuses_group_readable_secrets(tmp_path: Path) -> None:
    """An exposed proxy credential file stops before any Matrix request."""
    path = tmp_path / "matrix-http-headers.json"
    path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    path.chmod(0o640)

    with pytest.raises(DesktopSessionError, match="must not be readable"):
        load_desktop_http_headers(path)


@pytest.mark.parametrize("payload", ['["not-an-object"]', '{"X-Access-Client": 1}'])
def test_http_headers_file_requires_string_mapping(tmp_path: Path, payload: str) -> None:
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
async def test_explicit_login_method_skips_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_auto_login_method_rejects_unsupported_flows(monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_auto_login_method_translates_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Login discovery reports transport failure as one actionable desktop error."""
    monkeypatch.setattr(
        "mindroom.desktop.session.login_flows",
        AsyncMock(side_effect=aiohttp.ClientConnectionError("homeserver unavailable")),
    )

    with pytest.raises(DesktopSessionError, match=r"Could not discover.*homeserver unavailable"):
        await resolve_desktop_login_method(
            DesktopLoginMethod.AUTO,
            homeserver="https://matrix.example.org",
            runtime_paths=SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_initial_crypto_sync_does_not_announce_bridge_online() -> None:
    """Presence stays offline until the command callback is registered and sync-forever starts."""
    client = SimpleNamespace(
        sync=AsyncMock(return_value=object()),
        should_upload_keys=False,
        olm=object(),
    )

    await _prepare_crypto(client)

    client.sync.assert_awaited_once_with(timeout=0, full_state=False, set_presence="offline")


@pytest.mark.asyncio
async def test_login_translates_expected_matrix_authentication_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad desktop credentials become one actionable session-domain error."""
    runtime_paths = SimpleNamespace()
    matrix_login = AsyncMock(side_effect=PermanentMatrixStartupError("invalid credentials"))
    monkeypatch.setattr(
        "mindroom.desktop.session.login",
        matrix_login,
    )

    with pytest.raises(DesktopSessionError, match="invalid credentials"):
        await login_desktop_client(
            homeserver="https://matrix.example.org",
            user_id="@desktop:example.org",
            password="wrong-password",  # noqa: S106 - Test-only invalid credential.
            runtime_paths=runtime_paths,
            http_headers={"X-Access-Client": "test-secret"},
        )

    matrix_login.assert_awaited_once_with(
        "https://matrix.example.org",
        "@desktop:example.org",
        "wrong-password",
        runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )


@pytest.mark.asyncio
async def test_login_translates_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Desktop login reports exhausted transport retries without a traceback."""
    monkeypatch.setattr(
        "mindroom.desktop.session.login_with_token",
        AsyncMock(side_effect=aiohttp.ClientConnectionError("connection refused")),
    )

    with pytest.raises(DesktopSessionError, match=r"Desktop Matrix login failed.*connection refused"):
        await login_desktop_client(
            homeserver="https://matrix.example.org",
            user_id=None,
            login_token="short-lived-token",  # noqa: S106 - Test-only login token.
            runtime_paths=SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_sso_login_uses_returned_identity_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSO token exchange owns user identity and still prepares one encrypted device."""
    client = SimpleNamespace(
        user_id="@desktop:example.org",
        device_id="DESKTOP",
        access_token="matrix-access-token",  # noqa: S106 - Test-only access token.
        close=AsyncMock(),
    )
    token_login = AsyncMock(return_value=client)
    prepare = AsyncMock()
    cross_sign = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.session.login_with_token", token_login)
    monkeypatch.setattr("mindroom.desktop.session._prepare_crypto", prepare)
    monkeypatch.setattr("mindroom.desktop.session.ensure_agent_cross_signing", cross_sign)
    runtime_paths = SimpleNamespace()

    returned_client, session = await login_desktop_client(
        homeserver="https://matrix.example.org",
        user_id=None,
        login_token="short-lived-token",  # noqa: S106 - Test-only login token.
        runtime_paths=runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
        cloudflare_access=True,
    )

    assert returned_client is client
    assert session == DesktopMatrixSession(
        homeserver="https://matrix.example.org",
        user_id="@desktop:example.org",
        device_id="DESKTOP",
        access_token="matrix-access-token",  # noqa: S106 - Test-only access token.
        cloudflare_access=True,
    )
    token_login.assert_awaited_once_with(
        "https://matrix.example.org",
        "short-lived-token",
        runtime_paths,
        expected_user_id=None,
        http_headers={"X-Access-Client": "test-secret"},
    )
    prepare.assert_awaited_once_with(client)
    cross_sign.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_translates_expected_revoked_session_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked saved access token becomes one actionable session-domain error."""
    monkeypatch.setattr("mindroom.desktop.session.olm_store_exists", lambda *_args: True)
    matrix_restore = AsyncMock(side_effect=PermanentMatrixStartupError("access token revoked"))
    monkeypatch.setattr(
        "mindroom.desktop.session.restore_login",
        matrix_restore,
    )

    with pytest.raises(DesktopSessionError, match="access token revoked"):
        await open_desktop_client(
            _session(),
            runtime_paths=SimpleNamespace(),
            http_headers={"X-Access-Client": "test-secret"},
        )

    assert matrix_restore.await_args.kwargs["http_headers"] == {"X-Access-Client": "test-secret"}
