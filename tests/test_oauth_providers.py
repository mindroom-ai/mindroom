"""Tests for shared OAuth provider behavior."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom import credentials as credentials_module
from mindroom.constants import resolve_runtime_paths
from mindroom.credential_policy import (
    OAUTH_DYNAMIC_CLIENT_REGISTERED_TOKEN_URL_KEY,
    OAUTH_DYNAMIC_CLIENT_REGISTRATION_SOURCE,
    RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY,
)
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth import credential_store, providers
from mindroom.oauth.providers import (
    OAuthProvider,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    OAuthRuntimeEndpoints,
    OAuthTokenEndpointChangedError,
    token_endpoint_origin,
)
from tests.oauth_test_utils import (
    DelayedTokenEndpointOutcome,
    TokenEndpointOutcome,
    rotated_token_response,
    serve_token_endpoint,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths


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


def _lost(request: httpx.Request) -> Exception:
    return httpx.ReadTimeout("timed out", request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lost_response",
    [
        pytest.param(_lost, id="read-timeout"),
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
    """A grant whose response never arrived is repeated with the same refresh token, and its answer is used."""
    provider, runtime_paths, token_data = _refreshable_provider(tmp_path)
    presented = serve_token_endpoint(monkeypatch, [lost_response, rotated_token_response()])

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
            [_lost, lambda request: httpx.ReadTimeout("timed out again", request=request)],
            2,
            id="repeat-also-lost",
        ),
        pytest.param(
            [lambda request: httpx.ConnectError("unreachable", request=request), rotated_token_response()],
            1,
            id="connect-error-never-reached-provider",
        ),
        pytest.param(
            [httpx.Response(429, text="Rate exceeded."), rotated_token_response()],
            1,
            id="non-json-error-response",
        ),
        pytest.param(
            [httpx.Response(429, content=b"\x80\x81 not text"), rotated_token_response()],
            1,
            id="non-utf8-error-response",
        ),
    ],
)
async def test_refresh_failures_stay_non_terminal_without_repeating_answered_grants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcomes: list[TokenEndpointOutcome],
    expected_requests: int,
) -> None:
    """Only a lost response is repeated, and each failure here is a non-terminal provider error."""
    provider, runtime_paths, token_data = _refreshable_provider(tmp_path)
    presented = serve_token_endpoint(monkeypatch, outcomes)

    with pytest.raises(OAuthProviderError) as exc_info:
        await provider.refresh_token_data(token_data, runtime_paths)

    assert type(exc_info.value) is OAuthProviderError
    assert presented == ["stored-refresh-token"] * expected_requests


@pytest.mark.asyncio
async def test_refresh_rejected_on_the_repeat_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A provider without a reuse grace period rejects the repeat, which the caller treats as a terminal rejection."""
    provider, runtime_paths, token_data = _refreshable_provider(tmp_path)
    presented = serve_token_endpoint(monkeypatch, [_lost, httpx.Response(400, json={"error": "invalid_grant"})])

    with pytest.raises(OAuthRefreshRejectedError) as exc_info:
        await provider.refresh_token_data(token_data, runtime_paths)

    assert exc_info.value.oauth_error == "invalid_grant"
    assert presented == ["stored-refresh-token", "stored-refresh-token"]


def test_refresh_deadline_fits_a_repeat_and_ends_before_lock_waiters_give_up() -> None:
    """A repeat after a full-length read timeout must fit the deadline, which must end before a lock waiter's."""
    assert (
        providers._DEFAULT_AUTHORIZE_TIMEOUT_SECONDS + providers._REFRESH_REPEAT_MIN_SECONDS
        <= providers._REFRESH_GRANT_DEADLINE_SECONDS
    )
    # Leave the rest of the lock-wait budget for local parsing, publication, and the commit.
    assert providers._REFRESH_GRANT_DEADLINE_SECONDS + 5.0 <= credential_store._LOCK_WAIT_TIMEOUT_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "expected_requests", "expected_cause"),
    [
        pytest.param(
            [DelayedTokenEndpointOutcome(0.35, _lost), rotated_token_response()],
            1,
            httpx.ReadTimeout,
            id="no-budget-left-for-repeat",
        ),
        pytest.param(
            [DelayedTokenEndpointOutcome(5.0, rotated_token_response())],
            1,
            TimeoutError,
            id="first-attempt-exceeds-deadline",
        ),
        pytest.param(
            [_lost, DelayedTokenEndpointOutcome(5.0, rotated_token_response())],
            2,
            TimeoutError,
            id="repeat-exceeds-remaining-budget",
        ),
    ],
)
async def test_refresh_grant_attempts_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcomes: list[TokenEndpointOutcome],
    expected_requests: int,
    expected_cause: type[Exception],
) -> None:
    """Both grant attempts end by one wall-clock deadline, and hitting it is a non-terminal failure."""
    monkeypatch.setattr(providers, "_REFRESH_GRANT_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(providers, "_REFRESH_REPEAT_MIN_SECONDS", 0.2)
    provider, runtime_paths, token_data = _refreshable_provider(tmp_path)
    presented = serve_token_endpoint(monkeypatch, outcomes)

    started = asyncio.get_running_loop().time()
    with pytest.raises(OAuthProviderError) as exc_info:
        await provider.refresh_token_data(token_data, runtime_paths)

    assert asyncio.get_running_loop().time() - started < 2.0
    assert type(exc_info.value) is OAuthProviderError
    assert isinstance(exc_info.value.__cause__, expected_cause)
    assert presented == ["stored-refresh-token"] * expected_requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "decode_error"),
    [
        pytest.param(b"Rate exceeded.", json.JSONDecodeError, id="non-json"),
        pytest.param(b"\x80\x81 not text", UnicodeDecodeError, id="non-utf8"),
    ],
)
async def test_code_exchange_reports_undecodable_error_response_as_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: bytes,
    decode_error: type[ValueError],
) -> None:
    """A token endpoint answering with an undecodable error body fails the exchange instead of escaping."""
    provider, runtime_paths, _token_data = _refreshable_provider(tmp_path)
    serve_token_endpoint(monkeypatch, [httpx.Response(429, content=body)])

    with pytest.raises(OAuthProviderError) as exc_info:
        await provider.exchange_code("authorization-code", runtime_paths, token_url=provider.token_url)

    assert type(exc_info.value.__cause__) is decode_error
