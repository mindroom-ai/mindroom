"""Tests for OAuth-backed MCP provider registration."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credential_policy import credential_service_policy
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.oauth import (
    _DISCOVERY_CACHE,
    _DYNAMIC_CLIENT_REGISTRATION_LOCKS,
    _resolve_mcp_oauth_metadata,
    mcp_oauth_provider,
    mcp_oauth_provider_id,
)
from mindroom.oauth.providers import OAuthProvider, OAuthProviderError
from mindroom.oauth.registry import clear_oauth_provider_cache, load_oauth_providers
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


@pytest.fixture(autouse=True)
def _clear_discovery_cache() -> None:
    _DISCOVERY_CACHE.clear()
    _DYNAMIC_CLIENT_REGISTRATION_LOCKS.clear()


def test_mcp_registry_import_does_not_cycle_in_fresh_interpreter() -> None:
    """Importing the MCP registry first should not trigger OAuth registry cycles."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from mindroom.mcp.registry import resolved_mcp_tool_state; print(resolved_mcp_tool_state is not None)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _oauth_mcp_server_config() -> MCPServerConfig:
    return MCPServerConfig(
        transport="streamable-http",
        url="https://mcp.example.test/mcp",
        tool_prefix="example",
        auth={
            "type": "oauth",
            "display_name": "Example MCP",
            "resource": "https://mcp.example.test/mcp",
            "discovery": "manual",
            "authorization_url": "https://auth.example.test/authorize",
            "token_url": "https://auth.example.test/token",
            "scopes": [],
            "extra_auth_params": {"audience": "example"},
            "extra_token_params": {"resource": "https://mcp.example.test/mcp"},
        },
    )


def _auto_oauth_mcp_server_config() -> MCPServerConfig:
    return MCPServerConfig(
        transport="streamable-http",
        url="https://mcp.example.test/mcp",
        tool_prefix="example",
        auth={
            "type": "oauth",
            "display_name": "Example MCP",
            "resource": "https://mcp.example.test/mcp",
            "discovery": "auto",
            "scopes": ["mcp.read"],
            "extra_auth_params": {"audience": "example"},
            "extra_token_params": {"resource": "https://mcp.example.test/mcp"},
        },
    )


class _FakeDiscoveryResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}"
            raise RuntimeError(msg)


class _FakeDiscoveryClient:
    gets: ClassVar[list[str]] = []
    posts: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self) -> _FakeDiscoveryClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> _FakeDiscoveryResponse:
        del headers
        self.gets.append(url)
        if url == "https://mcp.example.test/.well-known/oauth-protected-resource":
            return _FakeDiscoveryResponse({}, status_code=404)
        if url == "https://mcp.example.test/.well-known/oauth-protected-resource/mcp":
            return _FakeDiscoveryResponse({"authorization_servers": ["https://auth.example.test/issuer"]})
        if url == "https://auth.example.test/.well-known/oauth-authorization-server/issuer":
            return _FakeDiscoveryResponse(
                {
                    "issuer": "https://auth.example.test/issuer",
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "registration_endpoint": "https://auth.example.test/register",
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        return _FakeDiscoveryResponse({}, status_code=404)

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> _FakeDiscoveryResponse:
        del headers
        self.posts.append((url, json))
        assert url == "https://auth.example.test/register"
        return _FakeDiscoveryResponse(
            {
                "client_id": "registered-client-id",
                "client_id_issued_at": 123,
                "registration_client_uri": "https://auth.example.test/register/registered-client-id",
                "registration_access_token": "registration-token",
                "token_endpoint_auth_method": "none",
            },
            status_code=201,
        )


class _InvalidJsonDiscoveryResponse(_FakeDiscoveryResponse):
    def __init__(self) -> None:
        super().__init__({})

    def json(self) -> dict[str, Any]:
        msg = "invalid json"
        raise ValueError(msg)


def test_mcp_oauth_provider_defaults_to_mcp_server_provider_id() -> None:
    """Generated MCP OAuth providers use deterministic services and public-client defaults."""
    provider = mcp_oauth_provider("demo", _oauth_mcp_server_config())

    assert provider.id == "mcp_demo"
    assert provider.display_name == "Example MCP"
    assert provider.authorization_url == "https://auth.example.test/authorize"
    assert provider.token_url == "https://auth.example.test/token"  # noqa: S105
    assert provider.scopes == ()
    assert provider.credential_service == "mcp_demo_oauth"
    assert provider.tool_config_service is None
    assert provider.client_config_services == ("mcp_demo_oauth_client",)
    assert provider.token_endpoint_auth_method == "none"  # noqa: S105
    assert provider.pkce_code_challenge_method == "S256"
    assert provider.extra_auth_params == {"audience": "example"}
    assert provider.extra_token_params == {"resource": "https://mcp.example.test/mcp"}


def test_custom_mcp_oauth_provider_id_keeps_generated_credential_services_mcp_scoped() -> None:
    """Custom generated provider ids still use MCP-prefixed credential service names."""
    config = _oauth_mcp_server_config().model_copy(
        update={"auth": _oauth_mcp_server_config().auth.model_copy(update={"provider_id": "custom"})},
    )
    provider = mcp_oauth_provider("demo", config)

    assert provider.credential_service == "mcp_custom_oauth"
    assert provider.client_config_services == ("mcp_custom_oauth_client",)
    assert credential_service_policy(provider.credential_service, "user_agent").uses_primary_runtime_scoped_credentials


def test_load_oauth_providers_includes_mcp_oauth_provider(tmp_path: Path) -> None:
    """The generic OAuth routes should see providers generated from MCP config."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": _oauth_mcp_server_config().model_dump(exclude_none=True),
            },
        },
        runtime_paths,
    )

    clear_oauth_provider_cache()
    try:
        providers = load_oauth_providers(config, runtime_paths)
    finally:
        clear_oauth_provider_cache()

    assert "mcp_demo" in providers
    assert providers["mcp_demo"].credential_service == "mcp_demo_oauth"


@pytest.mark.asyncio
async def test_mcp_oauth_provider_discovers_metadata_and_registers_public_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-discovered MCP OAuth should lazily register and persist a public client."""
    runtime_paths = _runtime_paths(tmp_path)
    _FakeDiscoveryClient.gets = []
    _FakeDiscoveryClient.posts = []
    monkeypatch.setattr("mindroom.mcp.oauth.httpx.AsyncClient", _FakeDiscoveryClient)
    provider = mcp_oauth_provider("demo", _auto_oauth_mcp_server_config())
    code_verifier = provider.issue_pkce_code_verifier()
    assert code_verifier is not None

    auth_url = await provider.authorization_uri_async(
        runtime_paths,
        state="state-token",
        code_verifier=code_verifier,
    )

    params = parse_qs(urlparse(auth_url).query)
    assert urlparse(auth_url).scheme == "https"
    assert urlparse(auth_url).netloc == "auth.example.test"
    assert urlparse(auth_url).path == "/authorize"
    assert params["client_id"] == ["registered-client-id"]
    assert params["scope"] == ["mcp.read"]
    assert params["audience"] == ["example"]
    assert params["code_challenge_method"] == ["S256"]
    assert _FakeDiscoveryClient.gets == [
        "https://mcp.example.test/.well-known/oauth-protected-resource",
        "https://mcp.example.test/.well-known/oauth-protected-resource/mcp",
        "https://auth.example.test/.well-known/oauth-authorization-server/issuer",
    ]
    assert _FakeDiscoveryClient.posts == [
        (
            "https://auth.example.test/register",
            {
                "client_name": "Example MCP",
                "redirect_uris": ["http://localhost:8765/api/oauth/mcp_demo/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "mcp.read",
            },
        ),
    ]
    stored_client = get_runtime_credentials_manager(runtime_paths).load_credentials("mcp_demo_oauth_client")
    assert stored_client == {
        "client_id": "registered-client-id",
        "redirect_uri": "http://localhost:8765/api/oauth/mcp_demo/callback",
        "client_id_issued_at": 123,
        "registration_client_uri": "https://auth.example.test/register/registered-client-id",
        "registration_access_token": "registration-token",
        "token_endpoint_auth_method": "none",
        "_source": "oauth_dynamic_client_registration",
        "_oauth_provider": "mcp_demo",
    }


@pytest.mark.asyncio
async def test_mcp_oauth_discovery_skips_optional_invalid_json_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad optional metadata candidates should not abort discovery before later valid candidates."""
    runtime_paths = _runtime_paths(tmp_path)

    class _InvalidFirstDiscoveryClient(_FakeDiscoveryClient):
        async def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> _FakeDiscoveryResponse:
            if url == "https://mcp.example.test/.well-known/oauth-protected-resource":
                _FakeDiscoveryClient.gets.append(url)
                return _InvalidJsonDiscoveryResponse()
            return await super().get(url, headers=headers)

    _FakeDiscoveryClient.gets = []
    _FakeDiscoveryClient.posts = []
    monkeypatch.setattr("mindroom.mcp.oauth.httpx.AsyncClient", _InvalidFirstDiscoveryClient)
    provider = mcp_oauth_provider("demo", _auto_oauth_mcp_server_config())
    code_verifier = provider.issue_pkce_code_verifier()
    assert code_verifier is not None

    auth_url = await provider.authorization_uri_async(
        runtime_paths,
        state="state-token",
        code_verifier=code_verifier,
    )

    assert urlparse(auth_url).netloc == "auth.example.test"
    assert _FakeDiscoveryClient.gets[:2] == [
        "https://mcp.example.test/.well-known/oauth-protected-resource",
        "https://mcp.example.test/.well-known/oauth-protected-resource/mcp",
    ]


@pytest.mark.asyncio
async def test_mcp_oauth_metadata_cache_includes_runtime_discovery_policy(tmp_path: Path) -> None:
    """Metadata cached under permissive discovery settings must not bypass stricter runtime checks."""
    permissive_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_MCP_OAUTH_ALLOW_INSECURE_DISCOVERY": "true"},
    )
    strict_paths = _runtime_paths(tmp_path)
    server_config = MCPServerConfig(
        transport="streamable-http",
        url="https://mcp.example.test/mcp",
        auth={
            "type": "oauth",
            "discovery": "manual",
            "authorization_url": "http://auth.example.test/authorize",
            "token_url": "http://auth.example.test/token",
        },
    )

    metadata = await _resolve_mcp_oauth_metadata("demo", server_config, permissive_paths)
    assert metadata.authorization_url == "http://auth.example.test/authorize"

    with pytest.raises(OAuthProviderError, match="requires HTTPS URL"):
        await _resolve_mcp_oauth_metadata("demo", server_config, strict_paths)


@pytest.mark.asyncio
async def test_mcp_oauth_dynamic_client_registration_is_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent authorization starts should not double-register the same OAuth client."""
    runtime_paths = _runtime_paths(tmp_path)
    first_post_started = asyncio.Event()
    release_first_post = asyncio.Event()

    class _SlowRegistrationDiscoveryClient(_FakeDiscoveryClient):
        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: Mapping[str, str] | None = None,
        ) -> _FakeDiscoveryResponse:
            del headers
            _FakeDiscoveryClient.posts.append((url, json))
            if len(_FakeDiscoveryClient.posts) == 1:
                first_post_started.set()
                await release_first_post.wait()
            assert url == "https://auth.example.test/register"
            return _FakeDiscoveryResponse(
                {
                    "client_id": "registered-client-id",
                    "client_id_issued_at": 123,
                    "token_endpoint_auth_method": "none",
                },
                status_code=201,
            )

    _FakeDiscoveryClient.gets = []
    _FakeDiscoveryClient.posts = []
    monkeypatch.setattr("mindroom.mcp.oauth.httpx.AsyncClient", _SlowRegistrationDiscoveryClient)
    provider = mcp_oauth_provider("demo", _auto_oauth_mcp_server_config())
    first_verifier = provider.issue_pkce_code_verifier()
    second_verifier = provider.issue_pkce_code_verifier()
    assert first_verifier is not None
    assert second_verifier is not None

    first_call = asyncio.create_task(
        provider.authorization_uri_async(
            runtime_paths,
            state="first-state",
            code_verifier=first_verifier,
        ),
    )
    await first_post_started.wait()
    second_call = asyncio.create_task(
        provider.authorization_uri_async(
            runtime_paths,
            state="second-state",
            code_verifier=second_verifier,
        ),
    )
    await asyncio.sleep(0)
    assert len(_FakeDiscoveryClient.posts) == 1

    release_first_post.set()
    first_url, second_url = await asyncio.gather(first_call, second_call)

    assert parse_qs(urlparse(first_url).query)["client_id"] == ["registered-client-id"]
    assert parse_qs(urlparse(second_url).query)["client_id"] == ["registered-client-id"]
    assert len(_FakeDiscoveryClient.posts) == 1


@pytest.mark.asyncio
async def test_mcp_oauth_discovery_rejects_hostname_resolving_to_private_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery must not allow DNS names that resolve to private-network targets."""
    runtime_paths = _runtime_paths(tmp_path)
    monkeypatch.setattr(
        "mindroom.mcp.oauth.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("10.0.0.5", 0))],
    )
    to_thread_calls = 0

    async def fake_to_thread(
        func: Callable[..., tuple[object, ...]],
        *args: object,
        **kwargs: object,
    ) -> tuple[object, ...]:
        nonlocal to_thread_calls
        to_thread_calls += 1
        return func(*args, **kwargs)

    monkeypatch.setattr("mindroom.mcp.oauth.asyncio.to_thread", fake_to_thread)
    provider = mcp_oauth_provider("demo", _auto_oauth_mcp_server_config())
    code_verifier = provider.issue_pkce_code_verifier()
    assert code_verifier is not None

    with pytest.raises(OAuthProviderError, match="refused unsafe URL host"):
        await provider.authorization_uri_async(
            runtime_paths,
            state="state-token",
            code_verifier=code_verifier,
        )
    assert to_thread_calls == 1


def test_oauth_provider_allows_public_clients_without_secret_and_empty_scopes(tmp_path: Path) -> None:
    """Public OAuth clients can be configured with a client ID and no client secret."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "mcp_demo_oauth_client",
        {
            "client_id": "public-client-id",
            "redirect_uri": "http://localhost:8765/api/oauth/mcp_demo/callback",
        },
    )
    provider = mcp_oauth_provider("demo", _oauth_mcp_server_config())

    client_config = provider.client_config(runtime_paths)

    assert client_config is not None
    assert client_config.client_id == "public-client-id"
    assert client_config.client_secret is None
    assert client_config.redirect_uri == "http://localhost:8765/api/oauth/mcp_demo/callback"


@pytest.mark.asyncio
async def test_oauth_provider_still_requires_confidential_client_secret(tmp_path: Path) -> None:
    """Existing confidential-client providers must keep requiring a client secret."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("example_oauth_client", {"client_id": "client-without-secret"})
    provider = OAuthProvider(
        id="example",
        display_name="Example",
        authorization_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",  # noqa: S106
        scopes=("example.read",),
        credential_service="example_oauth",
        client_config_services=("example_oauth_client",),
    )

    with pytest.raises(OAuthProviderError, match="client_id and client_secret"):
        await provider.require_client_config_async(runtime_paths)


def test_mcp_oauth_credentials_are_primary_runtime_scoped_for_user_agents(tmp_path: Path) -> None:
    """Requester-scoped MCP OAuth tokens should not be saved as worker-global credentials."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.test",
        room_id="!room:example.test",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
        tenant_id="tenant",
        account_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "code", identity)

    save_scoped_credentials(
        "mcp_demo_oauth",
        {"token": "alice-token", "_source": "oauth", "_oauth_provider": mcp_oauth_provider_id("demo", None)},
        credentials_manager=manager,
        worker_target=worker_target,
    )

    assert manager.load_credentials("mcp_demo_oauth") is None
    assert manager.for_worker(worker_target.worker_key).load_credentials("mcp_demo_oauth") is None
