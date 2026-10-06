"""Tests for shared OAuth provider behavior."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client

from mindroom import credentials as credentials_module
from mindroom.constants import resolve_runtime_paths
from mindroom.credential_policy import (
    OAUTH_DYNAMIC_CLIENT_REGISTERED_TOKEN_URL_KEY,
    OAUTH_DYNAMIC_CLIENT_REGISTRATION_SOURCE,
    RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY,
)
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth.providers import (
    OAuthProvider,
    OAuthProviderError,
    OAuthRuntimeEndpoints,
    OAuthTokenEndpointChangedError,
    token_endpoint_origin,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

type _TokenEndpointOutcome = httpx.Response | Callable[[httpx.Request], Exception]


@pytest.mark.asyncio
@pytest.mark.parametrize("bootstrap_required", [False, True], ids=["initial-read", "after-bootstrap"])
async def test_async_client_config_file_reads_stay_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bootstrap_required: bool,
) -> None:
    """Stored client settings are read off-loop, including the read after bootstrap."""
    owner_thread = threading.get_ident()
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    client_settings = {"client_id": "example-client", "client_secret": "example-secret"}
    service = "example_oauth_client"
    client_path = manager.get_credentials_path(service)
    bootstrap_calls = 0

    async def bootstrap(received_provider: OAuthProvider, received_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
        nonlocal bootstrap_calls
        assert received_provider is provider
        assert received_paths is runtime_paths
        assert threading.get_ident() == owner_thread
        bootstrap_calls += 1
        await asyncio.to_thread(manager.save_credentials, service, client_settings)
        return OAuthRuntimeEndpoints(
            authorization_url="https://auth.example.test/authorize",
            token_url="https://auth.example.test/token",  # noqa: S106
        )

    provider = OAuthProvider(
        id="example",
        display_name="Example",
        authorization_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",  # noqa: S106
        scopes=("read",),
        credential_service="example_oauth",
        client_config_services=(service,),
        runtime_bootstrapper=bootstrap,
    )
    if not bootstrap_required:
        manager.save_credentials(service, client_settings)

    original_read = credentials_module._read_credentials_payload
    read_threads: list[int] = []

    def observed_read(path: Path) -> bytes | None:
        if path == client_path:
            read_threads.append(threading.get_ident())
            assert threading.get_ident() != owner_thread, "Client config file read blocked the event loop"
        return original_read(path)

    monkeypatch.setattr(credentials_module, "_read_credentials_payload", observed_read)

    resolution = await provider.client_config_resolution_async(runtime_paths)

    assert resolution is not None
    assert resolution.config.client_id == "example-client"
    assert resolution.config.client_secret == "example-secret"  # noqa: S105
    assert resolution.service == service
    assert resolution.custom is True
    assert bootstrap_calls == int(bootstrap_required)
    assert read_threads


def test_token_endpoint_change_error_keeps_only_loggable_origins() -> None:
    """Endpoint-change diagnostics must not carry userinfo, paths, or queries into logs."""
    error = OAuthTokenEndpointChangedError(
        "https://client:secret@auth.example.test:8443/tenant/token?key=value",
        "https://attacker.example.test/token",
    )

    assert (error.stored_token_endpoint_origin, error.current_token_endpoint_origin) == (
        "https://auth.example.test:8443",
        "https://attacker.example.test",
    )
    unparseable = OAuthTokenEndpointChangedError(None, "not a url")
    assert (unparseable.stored_token_endpoint_origin, unparseable.current_token_endpoint_origin) == (None, None)
    malformed = OAuthTokenEndpointChangedError("https://[::1/token", "https://auth.example.test/token")
    assert malformed.stored_token_endpoint_origin is None
    assert token_endpoint_origin("https://[::1/token") is None


class _UnexpectedTokenClient:
    def __init__(self, **_kwargs: object) -> None:
        pytest.fail("a dynamic client bound to another token endpoint must not be used")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["exchange", "refresh"])
async def test_token_requests_refuse_dynamic_client_registered_for_another_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    """A registration replaced after the endpoint check is still refused before its client is used."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    get_runtime_credentials_manager(runtime_paths).save_credentials(
        "example_oauth_client",
        {
            "client_id": "client-of-other-server",
            "_source": OAUTH_DYNAMIC_CLIENT_REGISTRATION_SOURCE,
            RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: True,
            OAUTH_DYNAMIC_CLIENT_REGISTERED_TOKEN_URL_KEY: "https://other.example.test/token",
        },
    )

    async def bootstrap(provider: OAuthProvider, _runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
        return OAuthRuntimeEndpoints(authorization_url=provider.authorization_url, token_url=provider.token_url)

    provider = OAuthProvider(
        id="example",
        display_name="Example",
        authorization_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",  # noqa: S106
        scopes=("read",),
        credential_service="example_oauth",
        client_config_services=("example_oauth_client",),
        token_endpoint_auth_method="none",  # noqa: S106
        runtime_bootstrapper=bootstrap,
    )
    monkeypatch.setattr("authlib.integrations.httpx_client.AsyncOAuth2Client", _UnexpectedTokenClient)

    request = (
        provider.exchange_code("authorization-code", runtime_paths, token_url=provider.token_url)
        if operation == "exchange"
        else provider.refresh_token_data(
            {
                "token": "expired-access-token",
                "refresh_token": "stored-refresh-token",
                "token_uri": provider.token_url,
                "client_id": "client-of-other-server",
                "expires_at": 1.0,
            },
            runtime_paths,
        )
    )
    with pytest.raises(OAuthTokenEndpointChangedError) as exc_info:
        await request

    origins = (exc_info.value.stored_token_endpoint_origin, exc_info.value.current_token_endpoint_origin)
    assert origins == ("https://other.example.test", "https://auth.example.test")


def _refreshable_provider(tmp_path: Path) -> tuple[OAuthProvider, RuntimePaths, dict[str, object]]:
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    get_runtime_credentials_manager(runtime_paths).save_credentials(
        "example_oauth_client",
        {"client_id": "example-client", "client_secret": "example-secret"},
    )
    provider = OAuthProvider(
        id="example",
        display_name="Example",
        authorization_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",  # noqa: S106
        scopes=("read",),
        credential_service="example_oauth",
        client_config_services=("example_oauth_client",),
    )
    token_data: dict[str, object] = {
        "token": "expired-access-token",
        "refresh_token": "stored-refresh-token",
        "token_uri": provider.token_url,
        "client_id": "example-client",
        "scopes": ["read"],
        "expires_at": 1.0,
    }
    return provider, runtime_paths, token_data


def _serve_token_endpoint(monkeypatch: pytest.MonkeyPatch, outcomes: list[_TokenEndpointOutcome]) -> list[str | None]:
    """Answer token requests with the given outcomes in order and record each presented refresh token."""
    presented_refresh_tokens: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        presented_refresh_tokens.append(parse_qs(request.content.decode()).get("refresh_token", [None])[0])
        outcome = outcomes.pop(0)
        if isinstance(outcome, httpx.Response):
            return outcome
        raise outcome(request)

    class _MockTransportClient(AsyncOAuth2Client):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr("authlib.integrations.httpx_client.AsyncOAuth2Client", _MockTransportClient)
    return presented_refresh_tokens


def _rotated_token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "rotated-access-token",
            "refresh_token": "rotated-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "read",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lost_response",
    [
        pytest.param(lambda request: httpx.ReadTimeout("timed out", request=request), id="read-timeout"),
        pytest.param(lambda request: httpx.ReadError("connection reset", request=request), id="read-error"),
        pytest.param(
            lambda request: httpx.RemoteProtocolError("Server disconnected", request=request),
            id="disconnected",
        ),
    ],
)
async def test_refresh_repeats_grant_once_when_its_response_is_lost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lost_response: Callable[[httpx.Request], Exception],
) -> None:
    """A rotating provider may already have redeemed the token, so the repeat must return its rotated token."""
    provider, runtime_paths, token_data = _refreshable_provider(tmp_path)
    presented = _serve_token_endpoint(monkeypatch, [lost_response, _rotated_token_response()])

    refreshed = await provider.refresh_token_data(token_data, runtime_paths)

    assert presented == ["stored-refresh-token", "stored-refresh-token"]
    assert refreshed is not None
    assert refreshed["token"] == "rotated-access-token"  # noqa: S105
    assert refreshed["refresh_token"] == "rotated-refresh-token"  # noqa: S105


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "expected_requests"),
    [
        pytest.param(
            [
                lambda request: httpx.ReadTimeout("timed out", request=request),
                lambda request: httpx.ReadTimeout("timed out again", request=request),
            ],
            2,
            id="repeat-also-lost",
        ),
        pytest.param(
            [lambda request: httpx.ConnectError("unreachable", request=request), _rotated_token_response()],
            1,
            id="connect-error-never-reached-provider",
        ),
        pytest.param(
            [httpx.Response(429, text="Rate exceeded."), _rotated_token_response()],
            1,
            id="non-json-error-response",
        ),
    ],
)
async def test_refresh_failures_stay_retryable_without_repeating_answered_grants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcomes: list[_TokenEndpointOutcome],
    expected_requests: int,
) -> None:
    """Only a lost response is repeated, and every failure is a non-terminal provider error that keeps the token."""
    provider, runtime_paths, token_data = _refreshable_provider(tmp_path)
    presented = _serve_token_endpoint(monkeypatch, outcomes)

    with pytest.raises(OAuthProviderError) as exc_info:
        await provider.refresh_token_data(token_data, runtime_paths)

    assert type(exc_info.value) is OAuthProviderError
    assert presented == ["stored-refresh-token"] * expected_requests


@pytest.mark.asyncio
async def test_code_exchange_reports_non_json_error_response_as_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A token endpoint answering with a non-JSON error body fails the exchange instead of escaping as a decode error."""
    provider, runtime_paths, _token_data = _refreshable_provider(tmp_path)
    _serve_token_endpoint(monkeypatch, [httpx.Response(429, text="Rate exceeded.")])

    with pytest.raises(OAuthProviderError) as exc_info:
        await provider.exchange_code("authorization-code", runtime_paths, token_url=provider.token_url)

    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)
