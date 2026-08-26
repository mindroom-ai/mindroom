"""Tests for reusable protected-resource OAuth discovery."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, ClassVar, cast
from urllib.parse import parse_qs, urlparse

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.credential_policy import (
    OAUTH_DYNAMIC_CLIENT_REGISTERED_REDIRECT_URI_KEY,
    RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY,
)
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth import OAuthDiscoveryConfig, OAuthProvider, oauth_runtime_bootstrapper
from mindroom.oauth.discovery import (
    _DISCOVERY_CACHE,
    _DYNAMIC_CLIENT_REGISTRATION_LOCKS,
    _cross_loop_lock,
    _discover_metadata,
    _stored_registration,
)
from mindroom.oauth.providers import OAuthProviderError
from mindroom.server_fetch_url import ServerFetchUrlError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _ResourceOriginDiscoveryClient:
    gets: ClassVar[list[str]] = []
    posts: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self) -> _ResourceOriginDiscoveryClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> _Response:
        del headers
        self.gets.append(url)
        if url == "https://resource.example.test/.well-known/oauth-protected-resource":
            return _Response({}, 404)
        if url == "https://resource.example.test/.well-known/oauth-authorization-server":
            return _Response(
                {
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "registration_endpoint": "https://auth.example.test/register",
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        return _Response({}, 404)

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> _Response:
        del headers
        self.posts.append((url, json))
        return _Response(
            {
                "client_id": "registered-public-client",
                "redirect_uris": ["https://mindroom.example.test/api/oauth/example/callback"],
            },
            201,
        )


class _BlockingRegistrationClient(_ResourceOriginDiscoveryClient):
    registration_started = threading.Event()
    registration_release = threading.Event()

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> _Response:
        self.registration_started.set()
        assert await asyncio.to_thread(self.registration_release.wait, 5)
        return await super().post(url, json=json, headers=headers)


def _install_dns_rebinding(monkeypatch: pytest.MonkeyPatch, *, safe_resolutions: int) -> None:
    # Discovery uses two safe preflight resolutions before the guarded dial;
    # manual DCR uses three endpoint preflights plus a registration preflight.
    remaining_safe_resolutions = safe_resolutions

    def resolve(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        nonlocal remaining_safe_resolutions
        address = "93.184.216.34" if remaining_safe_resolutions > 0 else "10.0.0.5"
        remaining_safe_resolutions -= 1
        return [(0, 0, 0, "", (address, 0))]

    async def unexpected_connect(*_args: object, **_kwargs: object) -> None:
        msg = "unsafe address reached the network backend"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.server_fetch_url.socket.getaddrinfo", resolve)
    monkeypatch.setattr("mindroom.server_fetch_url.AnyIOBackend.connect_tcp", unexpected_connect)


@pytest.fixture(autouse=True)
def _allow_example_test_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    _DISCOVERY_CACHE.clear()
    _DYNAMIC_CLIENT_REGISTRATION_LOCKS.clear()
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )


@pytest.mark.asyncio
async def test_cross_loop_lock_releases_after_repeated_waiter_cancellation() -> None:
    """Repeated cancellation must not strand a later-acquired thread lock."""
    lock = threading.Lock()
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold_lock() -> None:
        async with _cross_loop_lock(lock):
            holder_entered.set()
            await release_holder.wait()

    async def wait_for_lock() -> None:
        async with _cross_loop_lock(lock):
            msg = "cancelled waiter entered the critical section"
            raise AssertionError(msg)

    holder = asyncio.create_task(hold_lock())
    await holder_entered.wait()
    waiter = asyncio.create_task(wait_for_lock())
    await asyncio.sleep(0)

    waiter.cancel()
    waiter.cancel()
    release_holder.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, timeout=2)
    await asyncio.wait_for(holder, timeout=2)

    assert lock.acquire(blocking=False)
    lock.release()


@pytest.mark.asyncio
async def test_cross_loop_lock_waiter_does_not_saturate_default_executor() -> None:
    """A blocked waiter must leave executor capacity for work needed by the holder."""
    real_lock = threading.Lock()
    real_lock.acquire()
    loop = asyncio.get_running_loop()
    acquire_attempted = asyncio.Event()

    class ObservedLock:
        def acquire(self, blocking: bool = True) -> bool:
            loop.call_soon_threadsafe(acquire_attempted.set)
            return real_lock.acquire(blocking=blocking)

        def release(self) -> None:
            real_lock.release()

    lock = cast("threading.Lock", ObservedLock())
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    waiter_entered = asyncio.Event()

    async def wait_for_lock() -> None:
        async with _cross_loop_lock(lock):
            waiter_entered.set()

    waiter = asyncio.create_task(wait_for_lock())
    await acquire_attempted.wait()

    executor_probe = asyncio.create_task(asyncio.to_thread(lambda: True))
    probe_completed_while_waiting = False
    try:
        probe_completed_while_waiting = await asyncio.wait_for(asyncio.shield(executor_probe), timeout=0.2)
    except TimeoutError:
        pass
    finally:
        real_lock.release()
        await asyncio.wait_for(waiter, timeout=2)
        await asyncio.wait_for(executor_probe, timeout=2)

    assert probe_completed_while_waiting
    assert waiter_entered.is_set()


@pytest.mark.asyncio
async def test_resource_origin_metadata_registers_public_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App-domain metadata should bootstrap a PKCE public client without a secret."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    _ResourceOriginDiscoveryClient.gets = []
    _ResourceOriginDiscoveryClient.posts = []
    monkeypatch.setattr("mindroom.oauth.discovery.httpx.AsyncClient", _ResourceOriginDiscoveryClient)
    provider = OAuthProvider(
        id="example",
        display_name="Example",
        authorization_url="",
        token_url="",
        scopes=(),
        allow_empty_scopes=True,
        credential_service="example_oauth",
        client_config_services=("example_oauth_client",),
        token_endpoint_auth_method="none",  # noqa: S106
        pkce_code_challenge_method="S256",
        extra_auth_params={"resource": "https://resource.example.test"},
        extra_token_params={"resource": "https://resource.example.test"},
        runtime_bootstrapper=oauth_runtime_bootstrapper(
            OAuthDiscoveryConfig(
                resource="https://resource.example.test",
                token_endpoint_auth_method="none",  # noqa: S106
                pkce_code_challenge_method="S256",
            ),
        ),
    )
    verifier = provider.issue_pkce_code_verifier()
    assert verifier is not None

    authorization_url = await provider.authorization_uri_async(
        runtime_paths,
        state="state-token",
        code_verifier=verifier,
    )

    query = parse_qs(urlparse(authorization_url).query)
    assert query["client_id"] == ["registered-public-client"]
    assert query["resource"] == ["https://resource.example.test"]
    assert query["code_challenge_method"] == ["S256"]
    assert _ResourceOriginDiscoveryClient.gets == [
        "https://resource.example.test/.well-known/oauth-protected-resource",
        "https://resource.example.test/.well-known/oauth-authorization-server",
    ]
    assert _ResourceOriginDiscoveryClient.posts == [
        (
            "https://auth.example.test/register",
            {
                "client_name": "Example",
                "redirect_uris": ["https://mindroom.example.test/api/oauth/example/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        ),
    ]
    assert get_runtime_credentials_manager(runtime_paths).load_credentials("example_oauth_client") == {
        "client_id": "registered-public-client",
        "redirect_uri": "https://mindroom.example.test/api/oauth/example/callback",
        OAUTH_DYNAMIC_CLIENT_REGISTERED_REDIRECT_URI_KEY: "https://mindroom.example.test/api/oauth/example/callback",
        "_source": "oauth_dynamic_client_registration",
        "_oauth_provider": "example",
        RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: True,
    }


@pytest.mark.parametrize(
    "registered_redirect_uris",
    [None, [], ["https://other.example.test/api/oauth/example/callback"]],
)
def test_dynamic_registration_rejects_unconfirmed_redirect_uri(
    tmp_path: Path,
    registered_redirect_uris: list[str] | None,
) -> None:
    """Only callback metadata returned by the registration server is confirmed."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    provider = OAuthProvider(
        id="example",
        display_name="Example",
        authorization_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",  # noqa: S106
        scopes=(),
        allow_empty_scopes=True,
        credential_service="example_oauth",
        client_config_services=("example_oauth_client",),
        token_endpoint_auth_method="none",  # noqa: S106
    )
    registration: dict[str, Any] = {"client_id": "registered-public-client"}
    if registered_redirect_uris is not None:
        registration["redirect_uris"] = registered_redirect_uris

    with pytest.raises(OAuthProviderError, match="did not confirm redirect_uri"):
        _stored_registration(provider, runtime_paths, registration)


@pytest.mark.asyncio
async def test_dynamic_client_registration_singleflights_across_fresh_event_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider refresh worker loops must share one loop-neutral DCR lock."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    _BlockingRegistrationClient.gets = []
    _BlockingRegistrationClient.posts = []
    _BlockingRegistrationClient.registration_started.clear()
    _BlockingRegistrationClient.registration_release.clear()
    monkeypatch.setattr("mindroom.oauth.discovery.httpx.AsyncClient", _BlockingRegistrationClient)
    provider = OAuthProvider(
        id="example",
        display_name="Example",
        authorization_url="",
        token_url="",
        scopes=(),
        allow_empty_scopes=True,
        credential_service="example_oauth",
        client_config_services=("example_oauth_client",),
        token_endpoint_auth_method="none",  # noqa: S106
        pkce_code_challenge_method="S256",
        runtime_bootstrapper=oauth_runtime_bootstrapper(
            OAuthDiscoveryConfig(
                resource="https://resource.example.test",
                token_endpoint_auth_method="none",  # noqa: S106
                pkce_code_challenge_method="S256",
            ),
        ),
    )

    def authorize(state: str) -> str:
        verifier = provider.issue_pkce_code_verifier()
        assert verifier is not None
        return asyncio.run(
            provider.authorization_uri_async(
                runtime_paths,
                state=state,
                code_verifier=verifier,
            ),
        )

    first = asyncio.create_task(asyncio.to_thread(authorize, "first-state"))
    assert await asyncio.to_thread(_BlockingRegistrationClient.registration_started.wait, 5)
    second = asyncio.create_task(asyncio.to_thread(authorize, "second-state"))
    await asyncio.sleep(0.05)
    _BlockingRegistrationClient.registration_release.set()

    first_url, second_url = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert parse_qs(urlparse(first_url).query)["client_id"] == ["registered-public-client"]
    assert parse_qs(urlparse(second_url).query)["client_id"] == ["registered-public-client"]
    assert len(_BlockingRegistrationClient.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["  ", "resource.example.test"])
async def test_auto_discovery_requires_an_absolute_resource(tmp_path: Path, resource: str) -> None:
    """Auto discovery should fail clearly when no protected resource is configured."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})

    with pytest.raises(OAuthProviderError, match="auto discovery requires a protected-resource URL"):
        await _discover_metadata(OAuthDiscoveryConfig(resource=resource), runtime_paths)


@pytest.mark.asyncio
async def test_cached_endpoints_are_revalidated_before_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached endpoints must not bypass current network safety checks."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    blocked_auth_host = False

    def resolve(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        address = "10.0.0.5" if blocked_auth_host and host == "auth.example.test" else "93.184.216.34"
        return [(0, 0, 0, "", (address, 0))]

    monkeypatch.setattr("mindroom.server_fetch_url.socket.getaddrinfo", resolve)
    monkeypatch.setattr("mindroom.oauth.discovery.httpx.AsyncClient", _ResourceOriginDiscoveryClient)
    config = OAuthDiscoveryConfig(
        resource="https://resource.example.test",
        token_endpoint_auth_method="none",  # noqa: S106
        pkce_code_challenge_method="S256",
    )
    await _discover_metadata(config, runtime_paths)
    blocked_auth_host = True

    with pytest.raises(OAuthProviderError, match="refused unsafe URL"):
        await _discover_metadata(config, runtime_paths)


@pytest.mark.asyncio
async def test_discovery_revalidates_dns_when_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery must reject a hostname that rebinds before the connection."""
    _install_dns_rebinding(monkeypatch, safe_resolutions=2)
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})

    with pytest.raises(OAuthProviderError, match="metadata request failed") as error:
        await _discover_metadata(OAuthDiscoveryConfig(resource="https://resource.example.test"), runtime_paths)

    assert isinstance(error.value.__cause__, ServerFetchUrlError)


@pytest.mark.asyncio
async def test_dynamic_registration_requires_provider_specific_client_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DCR clients must not be stored in services shared by multiple providers."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    _ResourceOriginDiscoveryClient.posts = []
    monkeypatch.setattr("mindroom.oauth.discovery.httpx.AsyncClient", _ResourceOriginDiscoveryClient)
    provider = OAuthProvider(
        id="shared_example",
        display_name="Shared Example",
        authorization_url="",
        token_url="",
        scopes=(),
        allow_empty_scopes=True,
        credential_service="shared_example_oauth",
        shared_client_config_services=("shared_example_oauth_client",),
        token_endpoint_auth_method="none",  # noqa: S106
        pkce_code_challenge_method="S256",
        runtime_bootstrapper=oauth_runtime_bootstrapper(
            OAuthDiscoveryConfig(
                resource="https://resource.example.test",
                token_endpoint_auth_method="none",  # noqa: S106
                pkce_code_challenge_method="S256",
            ),
        ),
    )

    with pytest.raises(OAuthProviderError, match="provider-specific client config service"):
        await provider.runtime_endpoints(runtime_paths)

    assert _ResourceOriginDiscoveryClient.posts == []
    assert get_runtime_credentials_manager(runtime_paths).load_credentials("shared_example_oauth_client") is None


@pytest.mark.asyncio
async def test_dynamic_registration_revalidates_dns_when_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client registration must reject a hostname that rebinds before the connection."""
    _install_dns_rebinding(monkeypatch, safe_resolutions=4)
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    provider = OAuthProvider(
        id="rebound_registration",
        display_name="Rebound Registration",
        authorization_url="",
        token_url="",
        scopes=(),
        allow_empty_scopes=True,
        credential_service="rebound_registration_oauth",
        client_config_services=("rebound_registration_oauth_client",),
        token_endpoint_auth_method="none",  # noqa: S106
        runtime_bootstrapper=oauth_runtime_bootstrapper(
            OAuthDiscoveryConfig(
                resource="",
                discovery="manual",
                authorization_url="https://auth.example.test/authorize",
                token_url="https://auth.example.test/token",  # noqa: S106
                registration_url="https://auth.example.test/register",
                token_endpoint_auth_method="none",  # noqa: S106
            ),
        ),
    )

    with pytest.raises(OAuthProviderError, match="dynamic client registration failed") as error:
        await provider.runtime_endpoints(runtime_paths)

    assert isinstance(error.value.__cause__, ServerFetchUrlError)


@pytest.mark.asyncio
async def test_discovery_reports_the_last_candidate_fetch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken final candidate must not be reported as absent metadata."""

    class _InvalidJsonResponse(_Response):
        def json(self) -> dict[str, Any]:
            msg = "invalid metadata JSON"
            raise ValueError(msg)

    class _InvalidMetadataClient(_ResourceOriginDiscoveryClient):
        async def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> _Response:
            del headers
            if url.endswith("/.well-known/oauth-protected-resource"):
                return _Response({}, 404)
            return _InvalidJsonResponse({})

    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    monkeypatch.setattr("mindroom.oauth.discovery.httpx.AsyncClient", _InvalidMetadataClient)

    with pytest.raises(OAuthProviderError, match="metadata request failed") as error:
        await _discover_metadata(OAuthDiscoveryConfig(resource="https://resource.example.test"), runtime_paths)

    assert isinstance(error.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_dynamic_registration_refuses_dedicated_worker_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OAuth app clients must be provisioned in the primary runtime."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "worker-a",
        },
    )
    _ResourceOriginDiscoveryClient.posts = []
    monkeypatch.setattr("mindroom.oauth.discovery.httpx.AsyncClient", _ResourceOriginDiscoveryClient)
    provider = OAuthProvider(
        id="worker_example",
        display_name="Worker Example",
        authorization_url="",
        token_url="",
        scopes=(),
        allow_empty_scopes=True,
        credential_service="worker_example_oauth",
        client_config_services=("worker_example_oauth_client",),
        token_endpoint_auth_method="none",  # noqa: S106
        pkce_code_challenge_method="S256",
        runtime_bootstrapper=oauth_runtime_bootstrapper(
            OAuthDiscoveryConfig(
                resource="https://resource.example.test",
                token_endpoint_auth_method="none",  # noqa: S106
                pkce_code_challenge_method="S256",
            ),
        ),
    )

    with pytest.raises(OAuthProviderError, match="primary runtime"):
        await provider.runtime_endpoints(runtime_paths)

    assert _ResourceOriginDiscoveryClient.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("discovery_config", "error"),
    [
        (
            OAuthDiscoveryConfig(
                resource="",
                discovery="manual",
                authorization_url="https://auth.example.test/authorize",
                token_url="https://auth.example.test/token",  # noqa: S106
                token_endpoint_auth_method="none",  # noqa: S106
                pkce_code_challenge_method=None,
            ),
            "token endpoint auth method",
        ),
        (
            OAuthDiscoveryConfig(
                resource="",
                discovery="manual",
                authorization_url="https://auth.example.test/authorize",
                token_url="https://auth.example.test/token",  # noqa: S106
                token_endpoint_auth_method="client_secret_post",  # noqa: S106
                pkce_code_challenge_method="S256",
            ),
            "PKCE method",
        ),
    ],
)
async def test_bootstrap_rejects_provider_method_mismatch(
    tmp_path: Path,
    discovery_config: OAuthDiscoveryConfig,
    error: str,
) -> None:
    """Discovery and provider runtime methods must agree."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    provider = OAuthProvider(
        id="mismatched",
        display_name="Mismatched",
        authorization_url="",
        token_url="",
        scopes=("read",),
        credential_service="mismatched_oauth",
        client_config_services=("mismatched_oauth_client",),
        token_endpoint_auth_method="client_secret_post",  # noqa: S106
        pkce_code_challenge_method=None,
        runtime_bootstrapper=oauth_runtime_bootstrapper(discovery_config),
    )

    with pytest.raises(OAuthProviderError, match=error):
        await provider.runtime_endpoints(runtime_paths)
