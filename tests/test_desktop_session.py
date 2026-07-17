"""Tests for private local desktop Matrix sessions."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.desktop.session import (
    DesktopMatrixSession,
    DesktopSessionError,
    _prepare_crypto,
    load_desktop_session,
    login_desktop_client,
    open_desktop_client,
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
    monkeypatch.setattr(
        "mindroom.desktop.session.login",
        AsyncMock(side_effect=PermanentMatrixStartupError("invalid credentials")),
    )

    with pytest.raises(DesktopSessionError, match="invalid credentials"):
        await login_desktop_client(
            homeserver="https://matrix.example.org",
            user_id="@desktop:example.org",
            password="wrong-password",  # noqa: S106 - Test-only invalid credential.
            runtime_paths=SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_restore_translates_expected_revoked_session_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked saved access token becomes one actionable session-domain error."""
    monkeypatch.setattr("mindroom.desktop.session.olm_store_exists", lambda *_args: True)
    monkeypatch.setattr(
        "mindroom.desktop.session.restore_login",
        AsyncMock(side_effect=PermanentMatrixStartupError("access token revoked")),
    )

    with pytest.raises(DesktopSessionError, match="access token revoked"):
        await open_desktop_client(_session(), runtime_paths=SimpleNamespace())
