"""Tests for interactive Cloudflare Access on Desktop Matrix requests."""

from __future__ import annotations

import base64
import json
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import nio
import pytest

from mindroom.desktop.cloudflare_access import (
    CloudflareAccessError,
    CloudflareAccessHeaders,
    CloudflareAccessTokenProvider,
    cloudflare_access_headers,
)
from mindroom.desktop.session import DesktopMatrixSession, _open_owned_session
from mindroom.matrix.client_session import MindRoomAsyncClient, matrix_client_config
from tests.conftest import test_runtime_paths as make_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _jwt(*, expires_at: int, marker: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires_at}).encode()).decode().rstrip("=")
    return f"header.{payload}.{marker}"


class _TestTokenProvider:
    def __init__(self, *tokens: str) -> None:
        self._tokens = iter(tokens)
        self._current: str | None = None
        self.token_threads: list[int] = []

    def current_token(self) -> str | None:
        return self._current

    def token(self) -> str:
        self.token_threads.append(threading.get_ident())
        self._current = next(self._tokens)
        return self._current

    def expire(self) -> None:
        self._current = None


@pytest.mark.asyncio
async def test_request_headers_cache_current_token_and_reauthenticate_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every request sees a current token without a guessed periodic refresh timer."""
    now = [100.0]
    first_token = _jwt(expires_at=200, marker="first")
    second_token = _jwt(expires_at=300, marker="second")
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout=first_token, stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="expired"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout=second_token, stderr=""),
        ],
    )
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> object:
        calls.append(args)
        return next(results)

    monkeypatch.setattr("mindroom.desktop.cloudflare_access.subprocess.run", run)
    provider = CloudflareAccessTokenProvider(
        app_url="https://matrix.example.org",
        executable="/usr/bin/cloudflared",
        clock=lambda: now[0],
    )
    headers = CloudflareAccessHeaders(provider, {"X-Static": "value"})

    await headers.prepare()
    assert dict(headers) == {"X-Static": "value", "cf-access-token": first_token}
    assert len(calls) == 1

    now[0] = 201.0
    await headers.prepare()
    assert dict(headers)["cf-access-token"] == second_token
    assert calls == [
        ["/usr/bin/cloudflared", "access", "token", "-app=https://matrix.example.org"],
        ["/usr/bin/cloudflared", "access", "token", "-app=https://matrix.example.org"],
        ["/usr/bin/cloudflared", "access", "login", "--quiet", "https://matrix.example.org"],
        ["/usr/bin/cloudflared", "access", "token", "-app=https://matrix.example.org"],
    ]


def test_cloudflare_access_requires_cloudflared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing binary fails before Matrix or browser login starts."""
    monkeypatch.setattr("mindroom.desktop.cloudflare_access.shutil.which", lambda _name: None)

    with pytest.raises(CloudflareAccessError, match="cloudflared CLI"):
        cloudflare_access_headers("https://matrix.example.org")


def test_cloudflare_access_rejects_duplicate_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """A static token cannot silently override interactive renewal."""
    monkeypatch.setattr("mindroom.desktop.cloudflare_access.shutil.which", lambda _name: "/usr/bin/cloudflared")

    with pytest.raises(CloudflareAccessError, match="cannot be combined"):
        cloudflare_access_headers(
            "https://matrix.example.org",
            {"CF-Access-Token": "static-token"},
        )


def test_nio_config_preserves_request_time_access_headers() -> None:
    """Long-running Matrix client resolves the mapping again for every HTTP request."""
    headers = CloudflareAccessHeaders(_TestTokenProvider("current-token"))

    config = matrix_client_config(http_headers=headers)

    assert config.custom_headers is headers


@pytest.mark.asyncio
async def test_nio_refreshes_access_header_for_each_transport_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired JWT is refreshed during nio's internal transport retry."""
    provider = _TestTokenProvider("first-token", "second-token")
    headers = CloudflareAccessHeaders(provider)
    client = MindRoomAsyncClient(
        "https://matrix.example.org",
        config=matrix_client_config(http_headers=headers),
    )
    sent_tokens: list[str] = []

    async def request(*_args: object, **kwargs: object) -> object:
        request_headers = kwargs["headers"]
        assert isinstance(request_headers, dict)
        sent_tokens.append(request_headers["cf-access-token"])
        if len(sent_tokens) == 1:
            provider.expire()
            message = "connection lost"
            raise aiohttp.ClientConnectionError(message)
        return SimpleNamespace(status=200)

    client.client_session = SimpleNamespace(request=request)  # type: ignore[assignment]
    monkeypatch.setattr(client, "create_matrix_response", AsyncMock(return_value=object()))
    monkeypatch.setattr(client, "receive_response", AsyncMock())

    event_loop_thread = threading.get_ident()
    await client._send(nio.WhoamiResponse, "GET", "/whoami")

    assert sent_tokens == ["first-token", "second-token"]
    assert provider.token_threads
    assert all(thread_id != event_loop_thread for thread_id in provider.token_threads)


@pytest.mark.asyncio
async def test_desktop_device_client_prepares_access_header_before_first_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pairing and bridge requests fetch the Access JWT instead of failing on an empty cache."""
    provider = _TestTokenProvider("device-token")
    session = DesktopMatrixSession(
        homeserver="https://matrix.example.org",
        user_id="@desktop:example.org",
        device_id="DESKTOP",
        access_token="matrix-access-token",  # noqa: S106
    )
    owner = await _open_owned_session(
        session,
        runtime_paths=make_runtime_paths(tmp_path),
        allow_create=True,
        http_headers=CloudflareAccessHeaders(provider),
    )
    sent_tokens: list[str] = []

    async def request(*_args: object, **kwargs: object) -> object:
        request_headers = kwargs["headers"]
        assert isinstance(request_headers, dict)
        sent_tokens.append(request_headers["cf-access-token"])
        return SimpleNamespace(status=200)

    try:
        owner.client.client_session = SimpleNamespace(request=request)  # type: ignore[assignment]
        monkeypatch.setattr(owner.client, "create_matrix_response", AsyncMock(return_value=object()))
        monkeypatch.setattr(owner.client, "receive_response", AsyncMock())

        await owner.client._send(nio.WhoamiResponse, "GET", "/whoami")
    finally:
        owner.client.client_session = None
        await owner.close()

    assert sent_tokens == ["device-token"]
    assert provider.token_threads


@pytest.mark.asyncio
async def test_access_header_lookup_is_case_insensitive() -> None:
    """HTTP casing does not affect lookup of the dynamic Access header."""
    headers = CloudflareAccessHeaders(_TestTokenProvider("current-token"))

    await headers.prepare()

    assert headers["CF-Access-Token"] == "current-token"


def test_failed_token_read_never_exposes_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed cloudflared call cannot copy token-shaped stdout into CLI errors."""
    stdout_value = "header.payload.signature"
    results = iter(
        [
            SimpleNamespace(returncode=1, stdout="", stderr="missing"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=1, stdout=stdout_value, stderr=""),
        ],
    )
    monkeypatch.setattr(
        "mindroom.desktop.cloudflare_access.subprocess.run",
        lambda *_args, **_kwargs: next(results),
    )
    provider = CloudflareAccessTokenProvider(
        app_url="https://matrix.example.org",
        executable="/usr/bin/cloudflared",
    )

    with pytest.raises(CloudflareAccessError, match="exit 1") as error:
        provider.token()

    assert stdout_value not in str(error.value)


@pytest.mark.parametrize(
    "token",
    ["not-a-jwt", _jwt(expires_at=100, marker="expired")],
)
def test_cloudflare_access_rejects_invalid_token_after_login(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    """Successful CLI exit never makes malformed or expired output trusted."""
    results = iter(
        [
            SimpleNamespace(returncode=1, stdout="", stderr="missing"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout=token, stderr=""),
        ],
    )
    monkeypatch.setattr(
        "mindroom.desktop.cloudflare_access.subprocess.run",
        lambda *_args, **_kwargs: next(results),
    )
    provider = CloudflareAccessTokenProvider(
        app_url="https://matrix.example.org",
        executable="/usr/bin/cloudflared",
        clock=lambda: 100.0,
    )

    with pytest.raises(CloudflareAccessError):
        provider.token()
