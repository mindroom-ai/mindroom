"""Native setup authentication and saved-configuration race tests."""

# ruff: noqa: D103, EM101, TRY003

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.desktop.cloudflare_access import CloudflareAccessHeaders
from mindroom.desktop.login_method import DesktopLoginMethod
from mindroom.desktop.native_config import (
    NativeBrowserConfig,
    NativeCaptureConfig,
    NativeDesktopConfig,
    native_config_path,
    save_native_config,
)
from mindroom.desktop.native_host import NativeDesktopHost
from mindroom.desktop.native_protocol import NativeProtocolError, NativeRequest
from mindroom.desktop.session import DesktopMatrixSession, load_desktop_session, save_desktop_session
from mindroom.matrix.olm_to_device import PinnedMatrixDevice

if TYPE_CHECKING:
    from pathlib import Path


def _host(tmp_path: Path) -> NativeDesktopHost:
    headers_path = tmp_path / "headers.json"
    headers_path.write_text(json.dumps({"x-static-header": "static-value"}))
    headers_path.chmod(0o600)
    paths = SimpleNamespace(
        storage_root=tmp_path,
        env_value=lambda name: str(headers_path) if name == "MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE" else None,
    )
    return NativeDesktopHost(paths, helper_version="1")


def _save_pairing_setup(tmp_path: Path, *, cloudflare_access: bool = False) -> NativeDesktopConfig:
    save_desktop_session(
        tmp_path / "desktop_bridge" / "matrix_session.json",
        DesktopMatrixSession("https://example.org", "@me:example.org", "LOCAL", "secret", cloudflare_access),
    )
    return save_native_config(
        native_config_path(tmp_path),
        NativeDesktopConfig(
            revision=0,
            enabled=True,
            controller=PinnedMatrixDevice("@controller:example.org", "CONTROLLER", "key"),
            allowed_requester_ids=("@me:example.org",),
            allowed_agent_names=("assistant",),
            allowed_app_ids=("com.example.Editor",),
            capture=NativeCaptureConfig(),
            browser=NativeBrowserConfig(),
        ),
        expected_revision=0,
    )


def _fake_access(monkeypatch: pytest.MonkeyPatch) -> None:
    event_loop_thread = threading.get_ident()

    class TokenProvider:
        value: str | None = None

        def current_token(self) -> str | None:
            return self.value

        def token(self) -> str:
            assert threading.get_ident() != event_loop_thread, "Interactive Access login must not block the helper"
            self.value = "access-token"
            return self.value

    monkeypatch.setattr(
        "mindroom.desktop.cloudflare_access.CloudflareAccessTokenProvider.create",
        lambda _url: TokenProvider(),
    )


@pytest.mark.parametrize("access", [False, True])
def test_login_authenticates_with_configured_headers_and_saves_access_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access: bool,
) -> None:
    host = _host(tmp_path)
    _fake_access(monkeypatch)
    owner = SimpleNamespace(client=object(), close=AsyncMock())

    async def resolve(requested: DesktopLoginMethod, **kwargs: object) -> DesktopLoginMethod:
        assert requested is DesktopLoginMethod.AUTO
        headers = kwargs["http_headers"]
        assert headers["x-static-header"] == "static-value"
        if access:
            assert isinstance(headers, CloudflareAccessHeaders)
            assert headers["cf-access-token"] == "access-token"
        return DesktopLoginMethod.PASSWORD

    async def login(**kwargs: object) -> tuple[object, DesktopMatrixSession]:
        assert kwargs["cloudflare_access"] is access
        assert kwargs["http_headers"]["x-static-header"] == "static-value"
        return owner, DesktopMatrixSession("https://example.org", "@me:example.org", "LOCAL", "secret", access)

    monkeypatch.setattr("mindroom.desktop.session.resolve_desktop_login_method", resolve)
    monkeypatch.setattr("mindroom.desktop.session.login_desktop_client", login)
    monkeypatch.setattr("mindroom.desktop.session.client_ed25519_fingerprint", lambda _client: "fingerprint")
    parameters = {"homeserver": "https://example.org", "user_id": "@me:example.org", "password": "password"}
    if access:
        parameters["cloudflare_access"] = True

    result = asyncio.run(host.handle(NativeRequest("login", "login", parameters)))

    assert load_desktop_session(tmp_path / "desktop_bridge" / "matrix_session.json").cloudflare_access is access
    assert result["device_id"] == "LOCAL"
    assert "secret" not in repr(result)
    owner.close.assert_awaited_once()


@pytest.mark.parametrize(("saved_access", "requested_access"), [(False, True), (True, False), (False, None)])
def test_pair_preserves_or_upgrades_saved_access_only_after_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saved_access: bool,
    requested_access: bool | None,
) -> None:
    config = _save_pairing_setup(tmp_path, cloudflare_access=saved_access)
    host = _host(tmp_path)
    _fake_access(monkeypatch)
    session_path = tmp_path / "desktop_bridge" / "matrix_session.json"
    original = session_path.read_bytes()
    owner = SimpleNamespace(close=AsyncMock())

    async def open_client(session: DesktopMatrixSession, **kwargs: object) -> object:
        assert session.cloudflare_access is saved_access
        headers = kwargs["http_headers"]
        assert headers["x-static-header"] == "static-value"
        if saved_access or requested_access:
            assert headers["cf-access-token"] == "access-token"
        return owner

    async def claim(_owner: object, controller: PinnedMatrixDevice, *, code: str) -> str:
        assert controller == config.controller
        assert code == "pairing-code"
        assert session_path.read_bytes() == original
        return "ABCD-EFGH"

    monkeypatch.setattr("mindroom.desktop.session.open_desktop_client", open_client)
    monkeypatch.setattr("mindroom.desktop.pairing_client.send_desktop_pairing_claim", claim)
    parameters: dict[str, object] = {"code": "pairing-code"}
    if requested_access is not None:
        parameters.update(cloudflare_access=requested_access, expected_revision=config.revision)

    result = asyncio.run(host.handle(NativeRequest("pair", "pair", parameters)))

    assert result["confirmation_command"] == "!desktop confirm pairing-code ABCD-EFGH"
    assert load_desktop_session(session_path).cloudflare_access is bool(saved_access or requested_access)
    if not requested_access:
        assert session_path.read_bytes() == original
    owner.close.assert_awaited_once()


@pytest.mark.parametrize("change", ["claim_failure", "session_replaced", "config_replaced"])
def test_pair_rejects_failed_claim_or_changed_setup_without_overwriting_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    config = _save_pairing_setup(tmp_path)
    host = _host(tmp_path)
    _fake_access(monkeypatch)
    session_path = tmp_path / "desktop_bridge" / "matrix_session.json"
    original_session = load_desktop_session(session_path)
    owner = SimpleNamespace(close=AsyncMock())

    async def open_client(*_args: object, **_kwargs: object) -> object:
        if change == "config_replaced":
            save_native_config(native_config_path(tmp_path), config, expected_revision=config.revision)
        return owner

    async def claim(*_args: object, **_kwargs: object) -> str:
        assert change != "config_replaced", "Never claim with a replaced saved configuration"
        if change == "claim_failure":
            raise RuntimeError("claim rejected")
        save_desktop_session(session_path, replace(original_session, device_id="REPLACEMENT"))
        return "ABCD-EFGH"

    monkeypatch.setattr("mindroom.desktop.session.open_desktop_client", open_client)
    monkeypatch.setattr("mindroom.desktop.pairing_client.send_desktop_pairing_claim", claim)

    with pytest.raises(NativeProtocolError) as exc:
        asyncio.run(
            host.handle(
                NativeRequest(
                    "pair",
                    "pair",
                    {"code": "pairing-code", "cloudflare_access": True, "expected_revision": config.revision},
                ),
            ),
        )

    assert exc.value.code == ("revision_conflict" if change == "config_replaced" else "pairing_failed")
    current = load_desktop_session(session_path)
    assert current.cloudflare_access is False
    assert current.device_id == ("REPLACEMENT" if change == "session_replaced" else "LOCAL")
    owner.close.assert_awaited_once()


def test_pair_rejects_stale_revision_before_authentication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _save_pairing_setup(tmp_path)
    host = _host(tmp_path)
    open_client = AsyncMock(side_effect=AssertionError("Stale setup must not authenticate"))
    monkeypatch.setattr("mindroom.desktop.session.open_desktop_client", open_client)

    with pytest.raises(NativeProtocolError) as exc:
        asyncio.run(
            host.handle(NativeRequest("pair", "pair", {"code": "code", "expected_revision": config.revision - 1})),
        )

    assert exc.value.code == "revision_conflict"
    open_client.assert_not_awaited()


@pytest.mark.parametrize("action", ["login", "pair"])
@pytest.mark.parametrize("invalid", [None, "true", 1])
def test_setup_access_parameter_requires_a_boolean(tmp_path: Path, action: str, invalid: object) -> None:
    _save_pairing_setup(tmp_path)
    host = _host(tmp_path)
    parameters = {"homeserver": "https://example.org"} if action == "login" else {"code": "code"}
    parameters["cloudflare_access"] = invalid

    with pytest.raises(NativeProtocolError, match="boolean") as exc:
        asyncio.run(host.handle(NativeRequest("invalid", action, parameters)))

    assert exc.value.code == "invalid_request"


@pytest.mark.parametrize("invalid", [None, True, -1, "1"])
def test_pair_revision_parameter_requires_a_nonnegative_integer(tmp_path: Path, invalid: object) -> None:
    _save_pairing_setup(tmp_path)
    host = _host(tmp_path)

    with pytest.raises(NativeProtocolError, match="expected_revision") as exc:
        asyncio.run(host.handle(NativeRequest("invalid", "pair", {"code": "code", "expected_revision": invalid})))

    assert exc.value.code == "invalid_request"
