"""Tests for shared OAuth provider behavior."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

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
    OAuthRuntimeEndpoints,
    OAuthTokenEndpointChangedError,
    token_endpoint_origin,
)

if TYPE_CHECKING:
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
    monkeypatch.setattr("mindroom.oauth.providers.AsyncOAuth2Client", _UnexpectedTokenClient)

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
