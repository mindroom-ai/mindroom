"""Tests for the generic OAuth API."""

# ruff: noqa: D103, FLY002, S105, S106, SIM117, TC003

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import auth, main
from mindroom.api.oauth import router as oauth_router
from mindroom.config.main import Config
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth import (
    OAuthClaimValidationContext,
    OAuthClaimValidationError,
    OAuthClientConfig,
    OAuthProvider,
    OAuthTokenResult,
    load_oauth_providers,
)
from mindroom.oauth import registry as oauth_registry
from mindroom.oauth import service as oauth_service
from mindroom.oauth.google_calendar import google_calendar_oauth_provider
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.oauth.service import oauth_credentials_satisfy_identity_policy
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    resolve_worker_key,
    resolve_worker_target,
)


def _runtime_paths(tmp_path: Path, process_env: dict[str, str] | None = None) -> constants.RuntimePaths:
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env or {},
    )
    process_env = process_env or {}
    client_id = process_env.get("TEST_OAUTH_CLIENT_ID")
    client_secret = process_env.get("TEST_OAUTH_CLIENT_SECRET")
    if client_id and client_secret:
        get_runtime_credentials_manager(runtime_paths).save_credentials(
            "test_drive_oauth_client",
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "_source": "ui",
            },
        )
    return runtime_paths


def _config_payload(worker_scope: str = "user_agent") -> dict[str, Any]:
    return {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "role": "test",
                "tools": ["google_drive"],
                "worker_scope": worker_scope,
                "rooms": [],
            },
        },
    }


def _make_test_app(runtime_paths: constants.RuntimePaths, payload: dict[str, Any]) -> FastAPI:
    api_app = FastAPI()
    main.initialize_api_app(api_app, runtime_paths)
    api_app.include_router(auth.router)
    api_app.include_router(oauth_router)
    _publish_config(api_app, runtime_paths, payload)
    return api_app


def _publish_config(
    api_app: FastAPI,
    runtime_paths: constants.RuntimePaths,
    payload: dict[str, Any],
) -> None:
    context = main._app_context(api_app)
    runtime_config = Config.validate_with_runtime(payload, runtime_paths)
    context.config_data = runtime_config.authored_model_dump()
    context.runtime_config = runtime_config
    context.config_load_result = main.ConfigLoadResult(success=True)
    context.auth_state = auth.ApiAuthState(
        runtime_paths=runtime_paths,
        settings=auth.ApiAuthSettings(
            platform_login_url=None,
            supabase_url=None,
            supabase_anon_key=None,
            account_id=None,
            mindroom_api_key="test-key",
        ),
        supabase_auth=None,
    )


def _use_runtime_auth_settings(api_app: FastAPI) -> None:
    main._app_context(api_app).auth_state = None


def _fake_provider(
    provider_id: str = "test_drive",
    *,
    credential_service: str = "test_drive",
    tool_config_service: str | None = None,
    email: str = "alice@example.com",
    hosted_domain: str = "example.com",
    email_verified: bool = True,
    include_refresh_token: bool = True,
    allowed_email_domains: tuple[str, ...] = (),
    allowed_hosted_domains: tuple[str, ...] = (),
    scopes: tuple[str, ...] = ("scope.read",),
    client_config_services: tuple[str, ...] = ("test_drive_oauth_client",),
    shared_client_config_services: tuple[str, ...] = (),
) -> OAuthProvider:
    async def _exchange(
        provider: OAuthProvider,
        code: str,
        client_config: object,
        _runtime_paths: object,
    ) -> OAuthTokenResult:
        assert code == "test-code"
        assert isinstance(client_config, OAuthClientConfig)
        token_data = {
            "token": f"{provider.id}-access-token",
            "token_uri": provider.token_url,
            "client_id": client_config.client_id,
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        }
        if include_refresh_token:
            token_data["refresh_token"] = f"{provider.id}-refresh-token"
        return OAuthTokenResult(
            token_data=token_data,
            claims={
                "sub": "subject-1",
                "email": email,
                "hd": hosted_domain,
                "email_verified": email_verified,
            },
            claims_verified=True,
        )

    return OAuthProvider(
        id=provider_id,
        display_name="Test Drive",
        authorization_url=f"https://auth.example.test/{provider_id}/authorize",
        token_url=f"https://auth.example.test/{provider_id}/token",
        scopes=scopes,
        credential_service=credential_service,
        tool_config_service=tool_config_service,
        client_config_services=client_config_services,
        shared_client_config_services=shared_client_config_services,
        allowed_email_domains=allowed_email_domains,
        allowed_hosted_domains=allowed_hosted_domains,
        status_capabilities=("Test files",),
        token_exchanger=_exchange,
    )


def _login(client: TestClient) -> None:
    response = client.post("/api/auth/session", json={"api_key": "test-key"})
    assert response.status_code == 200


def _state_from_auth_url(auth_url: str) -> str:
    parsed = urlparse(auth_url)
    state = parse_qs(parsed.query)["state"][0]
    assert state
    return state


def _worker_key_for_standalone_user() -> str:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="standalone",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_key = resolve_worker_key("user_agent", identity, agent_name="general")
    assert worker_key is not None
    return worker_key


def _worker_key_for_matrix_user(requester_id: str) -> str:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_key = resolve_worker_key("user_agent", identity, agent_name="general")
    assert worker_key is not None
    return worker_key


def _worker_key_for_matrix_user_scope(requester_id: str, worker_scope: WorkerScope = "user_agent") -> str:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_key = resolve_worker_key(worker_scope, identity, agent_name="general")
    assert worker_key is not None
    return worker_key


def test_plugin_config_registers_oauth_provider(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "mindroom.plugin.json").write_text(
        '{"name": "oauth_plugin", "oauth_module": "oauth_provider.py"}',
        encoding="utf-8",
    )
    (plugin_dir / "oauth_provider.py").write_text(
        "\n".join(
            [
                "from mindroom.oauth import OAuthProvider",
                "",
                "def register_oauth_providers(settings, runtime_paths):",
                "    del runtime_paths",
                "    return [OAuthProvider(",
                "        id=settings['provider_id'],",
                "        display_name='Plugin OAuth',",
                "        authorization_url='https://auth.example.test/authorize',",
                "        token_url='https://auth.example.test/token',",
                "        scopes=('plugin.read',),",
                "        credential_service=settings['credential_service'],",
                "        client_config_services=(f\"{settings['provider_id']}_oauth_client\",),",
                "    )]",
            ],
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.model_validate(
        {
            **_config_payload(),
            "plugins": [
                {
                    "path": str(plugin_dir),
                    "settings": {
                        "provider_id": "plugin_drive",
                        "credential_service": "plugin_drive",
                    },
                },
            ],
        },
    )

    providers = load_oauth_providers(config, runtime_paths)

    assert providers["plugin_drive"].display_name == "Plugin OAuth"
    assert providers["plugin_drive"].credential_service == "plugin_drive"


def test_plugin_oauth_provider_rejects_duplicate_service_names(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "mindroom.plugin.json").write_text(
        '{"name": "oauth_plugin", "oauth_module": "oauth_provider.py"}',
        encoding="utf-8",
    )
    (plugin_dir / "oauth_provider.py").write_text(
        "\n".join(
            [
                "from mindroom.oauth import OAuthProvider",
                "",
                "def register_oauth_providers(settings, runtime_paths):",
                "    del settings, runtime_paths",
                "    return [",
                "        OAuthProvider(",
                "            id='plugin_one',",
                "            display_name='Plugin One',",
                "            authorization_url='https://auth.example.test/one/authorize',",
                "            token_url='https://auth.example.test/one/token',",
                "            scopes=('plugin.read',),",
                "            credential_service='plugin_oauth',",
                "            client_config_services=('plugin_one_oauth_client',),",
                "        ),",
                "        OAuthProvider(",
                "            id='plugin_two',",
                "            display_name='Plugin Two',",
                "            authorization_url='https://auth.example.test/two/authorize',",
                "            token_url='https://auth.example.test/two/token',",
                "            scopes=('plugin.read',),",
                "            credential_service='plugin_oauth',",
                "            client_config_services=('plugin_two_oauth_client',),",
                "        ),",
                "    ]",
            ],
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.model_validate(
        {
            **_config_payload(),
            "plugins": [{"path": str(plugin_dir)}],
        },
    )

    with pytest.raises(plugin_imports.PluginValidationError, match="Duplicate OAuth provider service name"):
        load_oauth_providers(config, runtime_paths, skip_broken_plugins=False)


def test_oauth_provider_requires_client_config_service() -> None:
    with pytest.raises(ValueError, match="must declare at least one client config service"):
        OAuthProvider(
            id="plugin_drive",
            display_name="Plugin Drive",
            authorization_url="https://auth.example.test/authorize",
            token_url="https://auth.example.test/token",
            scopes=("plugin.read",),
            credential_service="plugin_drive_oauth",
        )


def test_plugin_oauth_provider_rejects_tool_config_overlap(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "mindroom.plugin.json").write_text(
        '{"name": "oauth_plugin", "oauth_module": "oauth_provider.py"}',
        encoding="utf-8",
    )
    (plugin_dir / "oauth_provider.py").write_text(
        "\n".join(
            [
                "from mindroom.oauth import OAuthProvider",
                "",
                "def register_oauth_providers(settings, runtime_paths):",
                "    del settings, runtime_paths",
                "    return [OAuthProvider(",
                "        id='plugin_drive',",
                "        display_name='Plugin Drive',",
                "        authorization_url='https://auth.example.test/authorize',",
                "        token_url='https://auth.example.test/token',",
                "        scopes=('plugin.read',),",
                "        credential_service='google_drive',",
                "        client_config_services=('plugin_drive_oauth_client',),",
                "    )]",
            ],
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.model_validate(
        {
            **_config_payload(),
            "plugins": [{"path": str(plugin_dir)}],
        },
    )

    with pytest.raises(plugin_imports.PluginValidationError, match="Duplicate OAuth provider service name"):
        load_oauth_providers(config, runtime_paths, skip_broken_plugins=False)


def test_plugin_oauth_provider_rejects_ordinary_tool_credential_service_overlap(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "mindroom.plugin.json").write_text(
        '{"name": "oauth_plugin", "oauth_module": "oauth_provider.py"}',
        encoding="utf-8",
    )
    (plugin_dir / "oauth_provider.py").write_text(
        "\n".join(
            [
                "from mindroom.oauth import OAuthProvider",
                "",
                "def register_oauth_providers(settings, runtime_paths):",
                "    del settings, runtime_paths",
                "    return [OAuthProvider(",
                "        id='plugin_weather',",
                "        display_name='Plugin Weather',",
                "        authorization_url='https://auth.example.test/authorize',",
                "        token_url='https://auth.example.test/token',",
                "        scopes=('plugin.read',),",
                "        credential_service='openweather',",
                "        client_config_services=('plugin_weather_oauth_client',),",
                "    )]",
            ],
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.model_validate(
        {
            **_config_payload(),
            "plugins": [{"path": str(plugin_dir)}],
        },
    )

    with pytest.raises(plugin_imports.PluginValidationError, match="overlap existing tool service"):
        load_oauth_providers(config, runtime_paths, skip_broken_plugins=False)


def test_plugin_oauth_provider_rejects_unrelated_tool_config_service_overlap(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "mindroom.plugin.json").write_text(
        '{"name": "oauth_plugin", "oauth_module": "oauth_provider.py"}',
        encoding="utf-8",
    )
    (plugin_dir / "oauth_provider.py").write_text(
        "\n".join(
            [
                "from mindroom.oauth import OAuthProvider",
                "",
                "def register_oauth_providers(settings, runtime_paths):",
                "    del settings, runtime_paths",
                "    return [OAuthProvider(",
                "        id='plugin_weather',",
                "        display_name='Plugin Weather',",
                "        authorization_url='https://auth.example.test/authorize',",
                "        token_url='https://auth.example.test/token',",
                "        scopes=('plugin.read',),",
                "        credential_service='plugin_weather_oauth',",
                "        tool_config_service='openweather',",
                "        client_config_services=('plugin_weather_oauth_client',),",
                "    )]",
            ],
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.model_validate(
        {
            **_config_payload(),
            "plugins": [{"path": str(plugin_dir)}],
        },
    )

    with pytest.raises(plugin_imports.PluginValidationError, match="overlap existing tool service"):
        load_oauth_providers(config, runtime_paths, skip_broken_plugins=False)


def test_plugin_oauth_provider_rejects_client_config_token_service_overlap(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "mindroom.plugin.json").write_text(
        '{"name": "oauth_plugin", "oauth_module": "oauth_provider.py"}',
        encoding="utf-8",
    )
    (plugin_dir / "oauth_provider.py").write_text(
        "\n".join(
            [
                "from mindroom.oauth import OAuthProvider",
                "",
                "def register_oauth_providers(settings, runtime_paths):",
                "    del settings, runtime_paths",
                "    return [OAuthProvider(",
                "        id='plugin_weather',",
                "        display_name='Plugin Weather',",
                "        authorization_url='https://auth.example.test/authorize',",
                "        token_url='https://auth.example.test/token',",
                "        scopes=('plugin.read',),",
                "        credential_service='plugin_weather_oauth_client',",
                "        client_config_services=('plugin_weather_oauth_client',),",
                "    )]",
            ],
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.model_validate(
        {
            **_config_payload(),
            "plugins": [{"path": str(plugin_dir)}],
        },
    )

    with pytest.raises(ValueError, match=r"credential_service.*must not end with '_oauth_client'"):
        load_oauth_providers(config, runtime_paths, skip_broken_plugins=False)


def test_plugin_oauth_provider_rejects_provider_specific_client_config_reuse(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "mindroom.plugin.json").write_text(
        '{"name": "oauth_plugin", "oauth_module": "oauth_provider.py"}',
        encoding="utf-8",
    )
    (plugin_dir / "oauth_provider.py").write_text(
        "\n".join(
            [
                "from mindroom.oauth import OAuthProvider",
                "",
                "def register_oauth_providers(settings, runtime_paths):",
                "    del settings, runtime_paths",
                "    return [OAuthProvider(",
                "        id='plugin_weather',",
                "        display_name='Plugin Weather',",
                "        authorization_url='https://auth.example.test/authorize',",
                "        token_url='https://auth.example.test/token',",
                "        scopes=('plugin.read',),",
                "        credential_service='plugin_weather_oauth',",
                "        client_config_services=('google_drive_oauth_client',),",
                "    )]",
            ],
        ),
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.model_validate(
        {
            **_config_payload(),
            "plugins": [{"path": str(plugin_dir)}],
        },
    )

    with pytest.raises(plugin_imports.PluginValidationError, match="Duplicate OAuth provider service name"):
        load_oauth_providers(config, runtime_paths, skip_broken_plugins=False)


def test_plugin_oauth_provider_allows_explicit_shared_client_config_reuse() -> None:
    first_provider = _fake_provider(
        "first_provider",
        credential_service="first_provider_oauth",
        client_config_services=(),
        shared_client_config_services=("shared_oauth_client",),
    )
    second_provider = _fake_provider(
        "second_provider",
        credential_service="second_provider_oauth",
        client_config_services=(),
        shared_client_config_services=("shared_oauth_client",),
    )

    providers = oauth_registry._provider_registry([first_provider, second_provider])

    assert set(providers) == {"first_provider", "second_provider"}


def test_plugin_oauth_provider_rejects_client_config_tool_service_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(oauth_registry.TOOL_METADATA, "acme_oauth_client", SimpleNamespace(auth_provider=None))
    provider = _fake_provider(
        "plugin_weather",
        credential_service="plugin_weather_oauth",
        client_config_services=("acme_oauth_client",),
    )

    with pytest.raises(plugin_imports.PluginValidationError, match="overlap existing tool service"):
        oauth_registry._provider_registry([provider])


def test_oauth_provider_rejects_client_config_suffix_for_token_service() -> None:
    with pytest.raises(ValueError, match=r"credential_service.*must not end with '_oauth_client'"):
        _fake_provider(credential_service="bad_oauth_client")


def test_oauth_provider_rejects_client_config_suffix_for_tool_config_service() -> None:
    with pytest.raises(ValueError, match=r"tool_config_service.*must not end with '_oauth_client'"):
        _fake_provider(
            credential_service="bad_oauth",
            tool_config_service="bad_oauth_client",
        )


def test_connect_generates_authorization_url_with_opaque_state(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload())
    provider = _fake_provider()

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")

    assert response.status_code == 200
    response_data = response.json()
    auth_url = response_data["auth_url"]
    parsed = urlparse(auth_url)
    params = parse_qs(parsed.query)
    assert response_data["completion_origin"] == "http://localhost:8765"
    assert parsed.scheme == "https"
    assert params["client_id"] == ["client-id"]
    assert params["scope"] == ["scope.read"]
    assert params["state"][0] != "general"
    assert "." not in params["state"][0]
    state_store = runtime_paths.storage_root / "oauth_state" / "oauth_state.json"
    assert state_store.exists()
    assert params["state"][0] in state_store.read_text(encoding="utf-8")


def test_connect_uses_stored_oauth_client_config(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload())
    provider = _fake_provider(client_config_services=("test_drive_oauth_client",))
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "test_drive_oauth_client",
        {
            "client_id": "stored-client-id",
            "client_secret": "stored-client-secret",
            "_source": "ui",
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")

    assert response.status_code == 200
    params = parse_qs(urlparse(response.json()["auth_url"]).query)
    assert params["client_id"] == ["stored-client-id"]


def test_provider_exchange_and_refresh_use_oauth_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    provider = _fake_provider()
    provider = OAuthProvider(
        id=provider.id,
        display_name=provider.display_name,
        authorization_url=provider.authorization_url,
        token_url=provider.token_url,
        scopes=provider.scopes,
        credential_service=provider.credential_service,
        client_config_services=provider.client_config_services,
    )
    seen: dict[str, Any] = {}

    class FakeOAuth2Client:
        def __init__(self, **kwargs: object) -> None:
            seen.setdefault("init_kwargs", []).append(kwargs)

        async def __aenter__(self) -> FakeOAuth2Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def fetch_token(self, url: str, **kwargs: object) -> dict[str, Any]:
            seen["fetch"] = {"url": url, **kwargs}
            return {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "scope": "scope.read",
                "expires_at": 1234.0,
            }

        async def refresh_token(self, url: str, **kwargs: object) -> dict[str, Any]:
            seen["refresh"] = {"url": url, **kwargs}
            return {
                "access_token": "refreshed-token",
                "token_type": "Bearer",
                "expires_at": 2234.0,
            }

    monkeypatch.setattr("mindroom.oauth.providers.AsyncOAuth2Client", FakeOAuth2Client)

    result = asyncio.run(provider.exchange_code("auth-code", runtime_paths))
    refreshed = asyncio.run(
        provider.refresh_token_data(
            {
                **result.token_data,
                "_id_token": "old-raw-id-token",
                "id_token": "old-standard-id-token",
                "client_secret": "old-client-secret",
            },
            runtime_paths,
        ),
    )

    assert seen["init_kwargs"][0]["token_endpoint_auth_method"] == "client_secret_post"
    assert seen["fetch"] == {
        "url": provider.token_url,
        "code": "auth-code",
        "grant_type": "authorization_code",
    }
    assert result.token_data["token"] == "access-token"
    assert result.token_data["_source"] == "oauth"
    assert result.token_data["_oauth_provider"] == provider.id
    assert result.token_data["refresh_token"] == "refresh-token"
    assert result.token_data["expires_at"] == 1234.0
    assert seen["refresh"]["url"] == provider.token_url
    assert seen["refresh"]["refresh_token"] == "refresh-token"
    assert refreshed is not None
    assert refreshed["token"] == "refreshed-token"
    assert refreshed["_source"] == "oauth"
    assert refreshed["_oauth_provider"] == provider.id
    assert refreshed["refresh_token"] == "refresh-token"
    assert refreshed["expires_at"] == 2234.0
    assert "_id_token" not in refreshed
    assert "id_token" not in refreshed
    assert "client_secret" not in refreshed


def test_custom_token_exchanger_metadata_is_stamped_by_core(tmp_path: Path) -> None:
    async def _exchange(
        provider: OAuthProvider,
        code: str,
        _client_config: object,
        _runtime_paths: object,
    ) -> OAuthTokenResult:
        assert code == "test-code"
        return OAuthTokenResult(token_data={"token": f"{provider.id}-access-token"})

    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    provider = OAuthProvider(
        id="custom_drive",
        display_name="Custom Drive",
        authorization_url="https://auth.example.test/custom/authorize",
        token_url="https://auth.example.test/custom/token",
        scopes=("scope.read",),
        credential_service="custom_drive_oauth",
        client_config_services=("test_drive_oauth_client",),
        token_exchanger=_exchange,
    )

    result = asyncio.run(provider.exchange_code("test-code", runtime_paths))
    safe_result = provider.token_result_with_safe_claims(result)

    assert safe_result.token_data["_source"] == "oauth"
    assert safe_result.token_data["_oauth_provider"] == provider.id
    assert safe_result.token_data["client_id"] == "client-id"
    assert safe_result.token_data["scopes"] == ["scope.read"]


def test_safe_token_result_drops_raw_id_token() -> None:
    provider = OAuthProvider(
        id="custom_mail",
        display_name="Custom Mail",
        authorization_url="https://auth.example.test/custom/authorize",
        token_url="https://auth.example.test/custom/token",
        scopes=("mail.read",),
        credential_service="custom_mail_oauth",
        client_config_services=("custom_mail_oauth_client",),
    )

    safe_result = provider.token_result_with_safe_claims(
        OAuthTokenResult(
            token_data={
                "token": "access-token",
                "_id_token": "header.payload.signature",
                "id_token": "standard.header.payload",
                "client_secret": "stored-client-secret",
                "_oauth_claims": {"email": "unverified@example.test"},
            },
            claims={"email": "alice@example.com", "sub": "google-subject"},
            claims_verified=True,
        ),
    )

    assert "_id_token" not in safe_result.token_data
    assert "id_token" not in safe_result.token_data
    assert "client_secret" not in safe_result.token_data
    assert safe_result.token_data["_oauth_claims"] == {
        "email": "alice@example.com",
        "sub": "google-subject",
    }
    assert safe_result.token_data["_oauth_claims_verified"] is True


def test_safe_token_result_does_not_persist_unverified_claims() -> None:
    provider = OAuthProvider(
        id="custom_mail",
        display_name="Custom Mail",
        authorization_url="https://auth.example.test/custom/authorize",
        token_url="https://auth.example.test/custom/token",
        scopes=("mail.read",),
        credential_service="custom_mail_oauth",
        client_config_services=("custom_mail_oauth_client",),
    )

    safe_result = provider.token_result_with_safe_claims(
        OAuthTokenResult(
            token_data={"token": "access-token"},
            claims={"email": "alice@example.com", "email_verified": True},
            claims_verified=False,
        ),
    )

    assert "_oauth_claims" not in safe_result.token_data
    assert "_oauth_claims_verified" not in safe_result.token_data


def test_safe_token_result_preserves_verified_claims_for_custom_validator(tmp_path: Path) -> None:
    def _validate_org(context: OAuthClaimValidationContext) -> None:
        if context.claims.get("org_id") != "acme":
            msg = "OAuth account organization is not allowed"
            raise OAuthClaimValidationError(msg)

    provider = OAuthProvider(
        id="custom_mail",
        display_name="Custom Mail",
        authorization_url="https://auth.example.test/custom/authorize",
        token_url="https://auth.example.test/custom/token",
        scopes=("mail.read",),
        credential_service="custom_mail_oauth",
        client_config_services=("custom_mail_oauth_client",),
        claim_validator=_validate_org,
    )
    runtime_paths = _runtime_paths(tmp_path, {})
    result = OAuthTokenResult(
        token_data={"token": "access-token", "scopes": ["mail.read"]},
        claims={
            "sub": "custom-subject",
            "email": "alice@example.com",
            "email_verified": True,
            "org_id": "acme",
        },
        claims_verified=True,
    )

    provider.validate_claims(result, runtime_paths)
    safe_result = provider.token_result_with_safe_claims(result)

    assert safe_result.token_data["_oauth_claims"]["org_id"] == "acme"
    assert oauth_credentials_satisfy_identity_policy(provider, runtime_paths, safe_result.token_data)


def test_google_drive_refresh_parser_accepts_existing_verified_claim_summary(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    provider = google_drive_oauth_provider()
    assert provider.token_parser is not None
    assert provider.credential_service == "google_drive_oauth"
    assert provider.tool_config_service == "google_drive"

    result = provider.token_parser(
        provider,
        {
            "access_token": "refreshed-access",
            "expires_at": 2234.0,
            "_oauth_claims": {"email": "alice@example.com", "hd": "example.com"},
            "_oauth_claims_verified": True,
        },
        OAuthClientConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="http://localhost/callback",
        ),
        runtime_paths,
    )

    assert result.token_data["token"] == "refreshed-access"
    assert result.token_data["expires_at"] == 2234.0
    assert "_id_token" not in result.token_data
    assert result.claims["email"] == "alice@example.com"
    assert result.claims_verified is True


def test_google_oauth_client_config_prefers_stored_provider_config(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "google_drive_oauth_client",
        {
            "client_id": "stored-client-id",
            "client_secret": "stored-client-secret",
            "redirect_uri": "https://stored.example.test/callback",
            "_source": "ui",
        },
    )

    client_config = google_drive_oauth_provider().client_config(runtime_paths)

    assert client_config == OAuthClientConfig(
        client_id="stored-client-id",
        client_secret="stored-client-secret",
        redirect_uri="https://stored.example.test/callback",
    )


def test_google_oauth_client_config_ignores_env(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "GOOGLE_CLIENT_ID": "env-client-id",
            "GOOGLE_CLIENT_SECRET": "env-client-secret",
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
        },
    )

    client_config = google_drive_oauth_provider().client_config(runtime_paths)

    assert client_config is None


def test_google_provider_oauth_client_config_wins_over_shared_config(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "google_oauth_client",
        {
            "client_id": "shared-client-id",
            "client_secret": "shared-client-secret",
            "redirect_uri": "https://shared.example.test/callback",
            "_source": "ui",
        },
    )
    manager.save_credentials(
        "google_drive_oauth_client",
        {
            "client_id": "drive-client-id",
            "client_secret": "drive-client-secret",
            "redirect_uri": "https://drive.example.test/callback",
            "_source": "ui",
        },
    )

    client_config = google_drive_oauth_provider().client_config(runtime_paths)

    assert client_config == OAuthClientConfig(
        client_id="drive-client-id",
        client_secret="drive-client-secret",
        redirect_uri="https://drive.example.test/callback",
    )


def test_google_shared_oauth_client_config_uses_provider_redirect_uri(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "google_oauth_client",
        {
            "client_id": "shared-client-id",
            "client_secret": "shared-client-secret",
            "redirect_uri": "https://wrong.example.test/api/oauth/google_drive/callback",
            "_source": "ui",
        },
    )

    client_config = google_calendar_oauth_provider().client_config(runtime_paths)

    assert client_config == OAuthClientConfig(
        client_id="shared-client-id",
        client_secret="shared-client-secret",
        redirect_uri="https://mindroom.example.test/api/oauth/google_calendar/callback",
    )


def test_google_drive_refresh_parser_rejects_unverified_existing_claim_summary(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    provider = google_drive_oauth_provider()
    assert provider.token_parser is not None

    with pytest.raises(OAuthClaimValidationError, match="verifiable identity token"):
        provider.token_parser(
            provider,
            {
                "access_token": "refreshed-access",
                "_oauth_claims": {"email": "alice@example.com", "email_verified": True},
            },
            OAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="http://localhost/callback",
            ),
            runtime_paths,
        )


def test_google_token_parser_rejects_invalid_id_token_with_claim_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    provider = google_drive_oauth_provider()
    assert provider.token_parser is not None

    def _raise_invalid_token(*_args: object, **_kwargs: object) -> None:
        msg = "invalid token"
        raise ValueError(msg)

    monkeypatch.setattr("mindroom.oauth.google.google_id_token.verify_oauth2_token", _raise_invalid_token)

    with pytest.raises(OAuthClaimValidationError, match="Google identity token verification failed"):
        provider.token_parser(
            provider,
            {
                "access_token": "access-token",
                "id_token": "bad-id-token",
            },
            OAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="http://localhost/callback",
            ),
            runtime_paths,
        )


def test_default_redirect_uri_uses_public_mindroom_origin(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "MINDROOM_PUBLIC_URL": "https://prod.example",
        },
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "google_drive_oauth_client",
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "_source": "ui",
        },
    )
    provider = google_drive_oauth_provider()

    client_config = provider.client_config(runtime_paths)

    assert client_config is not None
    assert client_config.redirect_uri == "https://prod.example/api/oauth/google_drive/callback"


def test_authorize_redirects_unauthenticated_browser_to_login(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    api_app = _make_test_app(runtime_paths, _config_payload())

    with TestClient(api_app) as client:
        response = client.get("/api/oauth/test_drive/authorize?agent_name=general", follow_redirects=False)

    assert response.status_code == 307
    location = urlparse(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query) == {
        "next": ["/api/oauth/test_drive/authorize?agent_name=general"],
    }


def test_authorize_login_redirect_preserves_scoped_oauth_query(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user"))

    with TestClient(api_app) as client:
        response = client.get(
            "/api/oauth/test_drive/authorize?agent_name=general&execution_scope=user",
            follow_redirects=False,
        )

    assert response.status_code == 307
    location = urlparse(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query) == {
        "next": ["/api/oauth/test_drive/authorize?agent_name=general&execution_scope=user"],
    }


def test_success_page_signals_oauth_completion_to_popup_opener(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    api_app = _make_test_app(runtime_paths, _config_payload())
    provider = _fake_provider()

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            response = client.get(f"/api/oauth/{provider.id}/success")

    assert response.status_code == 200
    assert "mindroom:oauth-complete" in response.text
    assert f'"provider": "{provider.id}"' in response.text
    assert '"status": "connected"' in response.text
    assert "window.opener.postMessage" in response.text
    assert 'postMessage(message, "*")' in response.text
    assert "window.close()" in response.text


def test_callback_stores_credentials_in_scoped_target(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        tool_config_service="google_drive",
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    owner_worker_key = _worker_key_for_matrix_user("@alice:example.org")
    scoped_manager = manager.for_primary_runtime_scope("@alice:example.org", "general")
    scoped_manager.save_credentials(
        "google_drive",
        {
            "list_files": False,
            "max_read_size": 42,
            "_source": "ui",
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert callback_response.status_code == 307
    assert urlparse(callback_response.headers["location"]).path == f"/api/oauth/{provider.id}/success"
    scoped_credentials = scoped_manager.load_credentials(
        provider.credential_service,
    )
    assert scoped_credentials is not None
    assert scoped_credentials["token"] == "google_drive-access-token"
    assert scoped_credentials["_oauth_claims"]["email"] == "alice@example.com"
    assert scoped_credentials["_oauth_claims_verified"] is True
    assert manager.for_worker(owner_worker_key).load_credentials(provider.credential_service) is None
    settings = scoped_manager.load_credentials("google_drive")
    assert settings == {
        "list_files": False,
        "max_read_size": 42,
        "_source": "ui",
    }
    assert manager.for_worker(owner_worker_key).load_credentials("google_drive") is None
    assert manager.for_worker(_worker_key_for_standalone_user()).load_credentials(provider.credential_service) is None


def test_callback_uses_stored_oauth_client_config(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        tool_config_service="google_drive",
        client_config_services=("google_drive_oauth_client",),
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "google_drive_oauth_client",
        {
            "client_id": "stored-client-id",
            "client_secret": "stored-client-secret",
            "_source": "ui",
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert callback_response.status_code == 307
    scoped_manager = manager.for_primary_runtime_scope("@alice:example.org", "general")
    scoped_credentials = scoped_manager.load_credentials(provider.credential_service)
    assert scoped_credentials is not None
    assert scoped_credentials["client_id"] == "stored-client-id"
    assert scoped_credentials["token"] == "google_drive-access-token"


@pytest.mark.parametrize("existing_token_client_id", ["old-client-id", None], ids=["previous-client", "unknown-client"])
def test_callback_does_not_preserve_refresh_token_from_previous_client(
    tmp_path: Path,
    existing_token_client_id: str | None,
) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        tool_config_service="google_drive",
        client_config_services=("google_drive_oauth_client",),
        include_refresh_token=False,
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "google_drive_oauth_client",
        {
            "client_id": "new-client-id",
            "client_secret": "stored-client-secret",
            "_source": "ui",
        },
    )
    existing_token_credentials = {
        "token": "old-access-token",
        "refresh_token": "old-refresh-token",
        "scopes": list(provider.scopes),
        "_source": "oauth",
        "_oauth_provider": provider.id,
        "_oauth_claims": {
            "sub": "subject-1",
            "email": "alice@example.com",
        },
        "_oauth_claims_verified": True,
    }
    if existing_token_client_id is not None:
        existing_token_credentials["client_id"] = existing_token_client_id
    manager.for_primary_runtime_scope("@alice:example.org", "general").save_credentials(
        provider.credential_service,
        existing_token_credentials,
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert callback_response.status_code == 307
    scoped_credentials = manager.for_primary_runtime_scope("@alice:example.org", "general").load_credentials(
        provider.credential_service,
    )
    assert scoped_credentials is not None
    assert scoped_credentials["client_id"] == "new-client-id"
    assert "refresh_token" not in scoped_credentials


def test_user_scope_oauth_token_not_in_worker_path(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user"))
    provider = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    manager = get_runtime_credentials_manager(runtime_paths)
    user_worker_key = _worker_key_for_matrix_user_scope("@alice:example.org", "user")

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert callback_response.status_code == 307
    stored_credentials = manager.for_primary_runtime_scope("@alice:example.org", None).load_credentials(
        provider.credential_service,
    )
    assert stored_credentials is not None
    assert stored_credentials["token"] == "google_drive-access-token"
    assert manager.for_worker(user_worker_key).load_credentials(provider.credential_service) is None
    assert not manager.for_worker(user_worker_key).get_credentials_path(provider.credential_service).exists()


def test_shared_scope_oauth_token_uses_shared_store_not_worker_path(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="shared"))
    provider = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    manager = get_runtime_credentials_manager(runtime_paths)

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert callback_response.status_code == 307
    shared_credentials = manager.shared_manager().load_credentials(provider.credential_service)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_key = resolve_worker_key("shared", identity, agent_name="general")
    assert worker_key is not None
    assert shared_credentials is not None
    assert shared_credentials["token"] == "google_drive-access-token"
    assert manager.for_worker(worker_key).load_credentials(provider.credential_service) is None


def test_dashboard_private_oauth_rejects_unbound_standalone_requester(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider()

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")

    assert response.status_code == 400
    assert "Matrix requester identity" in response.json()["detail"]


def test_callback_preserves_old_refresh_token_when_provider_omits_new_one(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        include_refresh_token=False,
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    owner_worker_key = _worker_key_for_matrix_user("@alice:example.org")
    scoped_manager = manager.for_primary_runtime_scope("@alice:example.org", "general")
    scoped_manager.save_credentials(
        provider.credential_service,
        {
            "token": "old-access-token",
            "refresh_token": "old-refresh-token",
            "client_id": "client-id",
            "_id_token": "old-raw-id-token",
            "id_token": "old-standard-id-token",
            "client_secret": "old-client-secret",
            "_source": "oauth",
            "_oauth_provider": provider.id,
            "_oauth_claims": {"sub": "subject-1", "email": "alice@example.com"},
            "_oauth_claims_verified": True,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert callback_response.status_code == 307
    stored_credentials = scoped_manager.load_credentials(provider.credential_service)
    assert stored_credentials is not None
    assert stored_credentials["token"] == "google_drive-access-token"
    assert stored_credentials["refresh_token"] == "old-refresh-token"
    assert "_id_token" not in stored_credentials
    assert "id_token" not in stored_credentials
    assert "client_secret" not in stored_credentials
    assert manager.for_worker(owner_worker_key).load_credentials(provider.credential_service) is None


def test_callback_drops_old_refresh_token_when_identity_changes(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        include_refresh_token=False,
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    owner_worker_key = _worker_key_for_matrix_user("@alice:example.org")
    scoped_manager = manager.for_primary_runtime_scope("@alice:example.org", "general")
    scoped_manager.save_credentials(
        provider.credential_service,
        {
            "token": "old-access-token",
            "refresh_token": "old-refresh-token",
            "_source": "oauth",
            "_oauth_provider": provider.id,
            "_oauth_claims": {"sub": "subject-2", "email": "bob@example.com"},
            "_oauth_claims_verified": True,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert callback_response.status_code == 307
    stored_credentials = scoped_manager.load_credentials(provider.credential_service)
    assert stored_credentials is not None
    assert stored_credentials["token"] == "google_drive-access-token"
    assert "refresh_token" not in stored_credentials
    assert manager.for_worker(owner_worker_key).load_credentials(provider.credential_service) is None


def test_callback_replaces_old_refresh_token_when_provider_returns_new_one(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        include_refresh_token=True,
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    owner_worker_key = _worker_key_for_matrix_user("@alice:example.org")
    scoped_manager = manager.for_primary_runtime_scope("@alice:example.org", "general")
    scoped_manager.save_credentials(
        provider.credential_service,
        {
            "token": "old-access-token",
            "refresh_token": "old-refresh-token",
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert callback_response.status_code == 307
    stored_credentials = scoped_manager.load_credentials(provider.credential_service)
    assert stored_credentials is not None
    assert stored_credentials["token"] == "google_drive-access-token"
    assert stored_credentials["refresh_token"] == "google_drive-refresh-token"
    assert manager.for_worker(owner_worker_key).load_credentials(provider.credential_service) is None


def test_agent_connect_token_stores_credentials_in_matrix_requester_scope(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    assert worker_target.execution_identity is not None
    connect_token = oauth_service.issue_oauth_connect_token(
        provider,
        runtime_paths,
        worker_target,
    )
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                follow_redirects=False,
            )
            state = _state_from_auth_url(authorize_response.headers["location"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert authorize_response.status_code == 307
    assert callback_response.status_code == 307
    manager = get_runtime_credentials_manager(runtime_paths)
    matrix_credentials = manager.for_primary_runtime_scope("@alice:example.org", "general").load_credentials(
        provider.credential_service,
    )
    worker_credentials = manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).load_credentials(
        provider.credential_service,
    )
    standalone_credentials = manager.for_worker(_worker_key_for_standalone_user()).load_credentials(
        provider.credential_service,
    )
    assert matrix_credentials is not None
    assert matrix_credentials["token"] == "google_drive-access-token"
    assert worker_credentials is None
    assert standalone_credentials is None


def _trusted_upstream_oauth_env() -> dict[str, str]:
    return {
        "TEST_OAUTH_CLIENT_ID": "client-id",
        "TEST_OAUTH_CLIENT_SECRET": "client-secret",
        "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
        "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
        "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
        "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
    }


def _trusted_upstream_oauth_email_template_env() -> dict[str, str]:
    env = _trusted_upstream_oauth_env()
    env.pop("MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER")
    env["MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE"] = "@{localpart}:example.org"
    return env


def _trusted_upstream_headers(
    *,
    user_id: str = "alice",
    email: str = "alice@example.com",
    matrix_user_id: str = "@alice:example.org",
) -> dict[str, str]:
    return {
        "X-Trusted-User": user_id,
        "X-Trusted-Email": email,
        "X-Trusted-Matrix-User": matrix_user_id,
    }


def test_agent_connect_token_uses_trusted_upstream_matrix_requester(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path, _trusted_upstream_oauth_env())
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    _use_runtime_auth_settings(api_app)
    provider = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                headers=_trusted_upstream_headers(),
                follow_redirects=False,
            )
            state = _state_from_auth_url(authorize_response.headers["location"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                headers=_trusted_upstream_headers(),
                follow_redirects=False,
            )

    assert authorize_response.status_code == 307
    assert callback_response.status_code == 307
    manager = get_runtime_credentials_manager(runtime_paths)
    matrix_credentials = manager.for_primary_runtime_scope("@alice:example.org", "general").load_credentials(
        provider.credential_service,
    )
    standalone_credentials = manager.for_worker(_worker_key_for_standalone_user()).load_credentials(
        provider.credential_service,
    )
    assert matrix_credentials is not None
    assert matrix_credentials["token"] == "google_drive-access-token"
    assert standalone_credentials is None


def test_agent_connect_token_accepts_trusted_upstream_derived_matrix_requester(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path, _trusted_upstream_oauth_email_template_env())
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    _use_runtime_auth_settings(api_app)
    provider = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None

    headers = {
        "X-Trusted-User": "alice",
        "X-Trusted-Email": "alice@example.com",
    }
    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                headers=headers,
                follow_redirects=False,
            )
            state = _state_from_auth_url(authorize_response.headers["location"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                headers=headers,
                follow_redirects=False,
            )

    assert authorize_response.status_code == 307
    assert callback_response.status_code == 307
    manager = get_runtime_credentials_manager(runtime_paths)
    matrix_credentials = manager.for_primary_runtime_scope("@alice:example.org", "general").load_credentials(
        provider.credential_service,
    )
    assert matrix_credentials is not None
    assert matrix_credentials["token"] == "google_drive-access-token"


@pytest.mark.parametrize("matrix_user_id", ["@Alice:example.org", "@:example.org"])
def test_agent_connect_token_accepts_historical_trusted_upstream_matrix_requester(
    tmp_path: Path,
    matrix_user_id: str,
) -> None:
    runtime_paths = _runtime_paths(tmp_path, _trusted_upstream_oauth_env())
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    _use_runtime_auth_settings(api_app)
    provider = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=matrix_user_id,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                headers=_trusted_upstream_headers(matrix_user_id=matrix_user_id),
                follow_redirects=False,
            )
            state = _state_from_auth_url(authorize_response.headers["location"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                headers=_trusted_upstream_headers(matrix_user_id=matrix_user_id),
                follow_redirects=False,
            )

    assert authorize_response.status_code == 307
    assert callback_response.status_code == 307
    manager = get_runtime_credentials_manager(runtime_paths)
    matrix_credentials = manager.for_primary_runtime_scope(matrix_user_id, "general").load_credentials(
        provider.credential_service,
    )
    assert matrix_credentials is not None
    assert matrix_credentials["token"] == "google_drive-access-token"


def test_agent_connect_token_rejects_trusted_upstream_requester_mismatch(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path, _trusted_upstream_oauth_env())
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    _use_runtime_auth_settings(api_app)
    provider = _fake_provider()
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                headers=_trusted_upstream_headers(
                    user_id="bob",
                    email="bob@example.com",
                    matrix_user_id="@bob:example.org",
                ),
                follow_redirects=False,
            )

    assert authorize_response.status_code == 403
    assert "current user" in authorize_response.json()["detail"]


def test_agent_connect_token_rejects_missing_trusted_upstream_identity(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path, _trusted_upstream_oauth_env())
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    _use_runtime_auth_settings(api_app)
    provider = _fake_provider()
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                follow_redirects=False,
            )

    assert authorize_response.status_code == 401
    assert "trusted upstream identity header" in authorize_response.json()["detail"]


def test_agent_connect_token_missing_trusted_identity_does_not_redirect_to_standalone_login(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        _trusted_upstream_oauth_env() | {"MINDROOM_API_KEY": "dashboard-secret"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    _use_runtime_auth_settings(api_app)
    provider = _fake_provider()
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                follow_redirects=False,
            )

    assert authorize_response.status_code == 401
    assert "location" not in authorize_response.headers
    assert "trusted upstream identity header" in authorize_response.json()["detail"]


def test_agent_connect_token_rejects_trusted_upstream_identity_without_matrix_mapping(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        _trusted_upstream_oauth_env() | {"MINDROOM_OWNER_USER_ID": "@alice:example.org"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    _use_runtime_auth_settings(api_app)
    provider = _fake_provider()
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                headers=_trusted_upstream_headers(matrix_user_id=""),
                follow_redirects=False,
            )

    assert authorize_response.status_code == 403
    assert "current user" in authorize_response.json()["detail"]


def test_agent_connect_token_callback_rejects_missing_trusted_upstream_identity(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path, _trusted_upstream_oauth_env())
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    _use_runtime_auth_settings(api_app)
    provider = _fake_provider()
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                headers=_trusted_upstream_headers(),
                follow_redirects=False,
            )
            state = _state_from_auth_url(authorize_response.headers["location"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                follow_redirects=False,
            )

    assert authorize_response.status_code == 307
    assert callback_response.status_code == 401
    assert "trusted upstream identity header" in callback_response.json()["detail"]


def test_agent_connect_token_callback_rejects_changed_trusted_matrix_requester(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path, _trusted_upstream_oauth_env())
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    _use_runtime_auth_settings(api_app)
    provider = _fake_provider()
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                headers=_trusted_upstream_headers(),
                follow_redirects=False,
            )
            state = _state_from_auth_url(authorize_response.headers["location"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
                headers=_trusted_upstream_headers(matrix_user_id="@bob:example.org"),
                follow_redirects=False,
            )

    assert authorize_response.status_code == 307
    assert callback_response.status_code == 409
    assert "credential target" in callback_response.json()["detail"]


def _config_payload_with_extra_google_agents(worker_scope: str = "user_agent") -> dict[str, Any]:
    payload = _config_payload(worker_scope=worker_scope)
    payload["agents"]["devagent"] = {
        "display_name": "Dev Agent",
        "role": "test",
        "tools": ["google_drive"],
        "worker_scope": worker_scope,
        "rooms": [],
    }
    payload["agents"]["router_agent"] = {
        "display_name": "Router Agent",
        "role": "test",
        "tools": ["google_drive"],
        "worker_scope": worker_scope,
        "rooms": [],
    }
    return payload


def _connect_token_for_devagent(provider: OAuthProvider, runtime_paths: constants.RuntimePaths) -> str:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="devagent",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "devagent", execution_identity=identity)
    connect_token = oauth_service.issue_oauth_connect_token(provider, runtime_paths, worker_target)
    assert connect_token is not None
    return connect_token


def test_connect_token_rejects_tampered_agent_name(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload_with_extra_google_agents())
    provider = _fake_provider()
    connect_token = _connect_token_for_devagent(provider, runtime_paths)

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=router_agent&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                follow_redirects=False,
            )

    assert response.status_code == 400
    assert "target" in response.json()["detail"]


def test_connect_token_rejects_tampered_execution_scope(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload_with_extra_google_agents())
    provider = _fake_provider()
    connect_token = _connect_token_for_devagent(provider, runtime_paths)

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=devagent&execution_scope=shared"
                f"&connect_token={connect_token}",
                follow_redirects=False,
            )

    assert response.status_code == 400
    assert "target" in response.json()["detail"]


def test_connect_token_rejects_omitted_target_params(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload_with_extra_google_agents())
    provider = _fake_provider()
    connect_token = _connect_token_for_devagent(provider, runtime_paths)

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            response = client.get(
                f"/api/oauth/{provider.id}/authorize?connect_token={connect_token}",
                follow_redirects=False,
            )

    assert response.status_code == 400
    assert "target" in response.json()["detail"]


def test_agent_connect_token_rejects_wrong_authenticated_requester(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path / "wrong-user",
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider()
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    assert worker_target.execution_identity is not None
    connect_token = oauth_service.issue_oauth_connect_token(
        provider,
        runtime_paths,
        worker_target,
    )
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=user_agent"
                f"&connect_token={connect_token}",
                follow_redirects=False,
            )

    assert authorize_response.status_code == 403
    wrong_manager = get_runtime_credentials_manager(runtime_paths)
    wrong_matrix_credentials = wrong_manager.for_worker(
        _worker_key_for_matrix_user("@alice:example.org"),
    ).load_credentials(
        provider.credential_service,
    )
    assert wrong_matrix_credentials is None


def test_shared_agent_connect_token_rejects_wrong_authenticated_requester(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="shared"))
    provider = _fake_provider()
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("shared", "general", execution_identity=identity)
    assert worker_target.execution_identity is not None
    connect_token = oauth_service.issue_oauth_connect_token(
        provider,
        runtime_paths,
        worker_target,
    )
    assert connect_token is not None

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            authorize_response = client.get(
                f"/api/oauth/{provider.id}/authorize?agent_name=general&execution_scope=shared"
                f"&connect_token={connect_token}",
                follow_redirects=False,
            )

    assert authorize_response.status_code == 403
    assert "current user" in authorize_response.json()["detail"]


def test_callback_rejects_wrong_provider_state(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="shared"))
    first_provider = _fake_provider("first_drive", credential_service="first_drive")
    second_provider = _fake_provider("second_drive", credential_service="second_drive")
    providers = {
        first_provider.id: first_provider,
        second_provider.id: second_provider,
    }

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value=providers):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{first_provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{second_provider.id}/callback?code=test-code&state={state}",
            )

    assert callback_response.status_code == 400
    assert "does not match" in callback_response.json()["detail"]


def test_callback_rejects_changed_credential_target(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider()

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            _publish_config(api_app, runtime_paths, _config_payload(worker_scope="shared"))
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
            )

    assert callback_response.status_code == 409
    manager = get_runtime_credentials_manager(runtime_paths)
    assert (
        manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).load_credentials(
            provider.credential_service,
        )
        is None
    )
    assert manager.shared_manager().load_credentials(provider.credential_service) is None


def test_callback_rejects_failed_claim_validation(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        email="alice@blocked.example",
        allowed_email_domains=("example.com",),
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
            )

    assert callback_response.status_code == 400
    manager = get_runtime_credentials_manager(runtime_paths)
    worker_credentials = manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).load_credentials(
        provider.credential_service,
    )
    assert worker_credentials is None


def test_callback_rejects_unverified_email_domain_claim(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        email_verified=False,
        allowed_email_domains=("example.com",),
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            connect_response = client.post(f"/api/oauth/{provider.id}/connect?agent_name=general")
            state = _state_from_auth_url(connect_response.json()["auth_url"])
            callback_response = client.get(
                f"/api/oauth/{provider.id}/callback?code=test-code&state={state}",
            )

    assert callback_response.status_code == 400
    assert "email ownership" in callback_response.json()["detail"]


def test_status_and_disconnect_use_same_scoped_target(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        tool_config_service="google_drive",
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    owner_worker_key = _worker_key_for_matrix_user("@alice:example.org")
    scoped_manager = manager.for_primary_runtime_scope("@alice:example.org", "general")
    scoped_manager.save_credentials(
        provider.credential_service,
        {
            "token": "stored-token",
            "refresh_token": "stored-refresh-token",
            "client_id": "client-id",
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_claims": {"email": "alice@example.com", "hd": "example.com"},
            "_oauth_claims_verified": True,
        },
    )
    scoped_manager.save_credentials(
        "google_drive",
        {
            "list_files": False,
            "max_read_size": 42,
            "_source": "ui",
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")
            disconnect_response = client.post(f"/api/oauth/{provider.id}/disconnect?agent_name=general")
            disconnected_status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["connected"] is True
    assert status_response.json()["email"] == "alice@example.com"
    assert disconnect_response.status_code == 200
    assert disconnected_status_response.status_code == 200
    assert disconnected_status_response.json()["connected"] is False
    remaining_token_credentials = scoped_manager.load_credentials(
        provider.credential_service,
    )
    remaining_settings = scoped_manager.load_credentials("google_drive")
    assert remaining_token_credentials is None
    assert remaining_settings is not None
    assert remaining_settings["list_files"] is False
    assert remaining_settings["max_read_size"] == 42
    assert manager.for_worker(owner_worker_key).load_credentials(provider.credential_service) is None


def test_disconnect_preserves_tool_config_settings(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(
        provider_id="google_calendar",
        credential_service="google_calendar_oauth",
        tool_config_service="google_calendar",
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    owner_worker_key = _worker_key_for_matrix_user("@alice:example.org")
    scoped_manager = manager.for_primary_runtime_scope("@alice:example.org", "general")
    scoped_manager.save_credentials(
        provider.credential_service,
        {
            "token": "stored-token",
            "refresh_token": "stored-refresh-token",
            "scopes": list(provider.scopes),
            "_source": "oauth",
        },
    )
    scoped_manager.save_credentials(
        "google_calendar",
        {
            "allow_update": True,
            "_source": "ui",
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            response = client.post(f"/api/oauth/{provider.id}/disconnect?agent_name=general")

    assert response.status_code == 200
    assert scoped_manager.load_credentials(provider.credential_service) is None
    assert manager.for_worker(owner_worker_key).load_credentials(provider.credential_service) is None
    settings = scoped_manager.load_credentials("google_calendar")
    assert settings is not None
    assert settings["allow_update"] is True


def test_status_requires_client_config_for_connected_true(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path, {constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org"})
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider()
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).save_credentials(
        provider.credential_service,
        {
            "token": "stored-token",
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["has_client_config"] is False
    assert status_response.json()["connected"] is False


def test_status_rejects_stored_token_without_client_id(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(client_config_services=("test_drive_oauth_client",))
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "test_drive_oauth_client",
        {
            "client_id": "stored-client-id",
            "client_secret": "stored-client-secret",
            "_source": "ui",
        },
    )
    manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).save_credentials(
        provider.credential_service,
        {
            "token": "stored-token",
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["has_client_config"] is True
    assert status_response.json()["client_config_redirect_uri_supported"] is True
    assert status_response.json()["connected"] is False


def test_status_rejects_token_from_previous_oauth_client(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(client_config_services=("test_drive_oauth_client",))
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "test_drive_oauth_client",
        {
            "client_id": "new-client-id",
            "client_secret": "stored-client-secret",
            "_source": "ui",
        },
    )
    manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).save_credentials(
        provider.credential_service,
        {
            "token": "stored-token",
            "client_id": "old-client-id",
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["has_client_config"] is True
    assert status_response.json()["connected"] is False


def test_status_reports_shared_oauth_client_config_service(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload())
    provider = _fake_provider(shared_client_config_services=("shared_oauth_client",))
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "shared_oauth_client",
        {
            "client_id": "stored-client-id",
            "client_secret": "stored-client-secret",
            "_source": "ui",
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status")

    assert status_response.status_code == 200
    assert status_response.json()["has_client_config"] is True
    assert status_response.json()["client_config_service"] == "shared_oauth_client"
    assert status_response.json()["client_config_redirect_uri_supported"] is False


def test_status_reports_active_shared_oauth_client_config_service(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org"},
    )
    api_app = _make_test_app(runtime_paths, _config_payload())
    provider = _fake_provider(
        client_config_services=("test_drive_oauth_client",),
        shared_client_config_services=("shared_oauth_client",),
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "shared_oauth_client",
        {
            "client_id": "stored-client-id",
            "client_secret": "stored-client-secret",
            "_source": "ui",
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status")

    assert status_response.status_code == 200
    assert status_response.json()["has_client_config"] is True
    assert status_response.json()["client_config_service"] == "shared_oauth_client"
    assert status_response.json()["client_config_redirect_uri_supported"] is False


def test_google_status_reports_connected_with_service_account(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "google-service-account.json"),
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = google_drive_oauth_provider()

    with TestClient(api_app) as client:
        _login(client)
        status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["has_client_config"] is False
    assert status_response.json()["has_service_account_config"] is True
    assert status_response.json()["connected"] is True


def test_status_rejects_expired_access_token_without_refresh(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider()
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).save_credentials(
        provider.credential_service,
        {
            "token": "expired-access-token",
            "expires_at": 1.0,
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["has_client_config"] is True
    assert status_response.json()["connected"] is False


def test_oauth_credentials_usable_rejects_refresh_only_without_expiry(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    provider = _fake_provider()

    assert (
        oauth_service.oauth_credentials_usable(
            provider,
            runtime_paths,
            {
                "refresh_token": "stored-refresh-token",
                "scopes": list(provider.scopes),
                "_source": "oauth",
                "_oauth_provider": provider.id,
            },
        )
        is False
    )


def test_status_rejects_refresh_only_credentials_without_expiry(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider()
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).save_credentials(
        provider.credential_service,
        {
            "refresh_token": "stored-refresh-token",
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["connected"] is False


def test_oauth_credentials_usable_accepts_access_token_without_expiry(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    provider = _fake_provider()

    assert oauth_service.oauth_credentials_usable(
        provider,
        runtime_paths,
        {
            "token": "stored-token",
            "client_id": "client-id",
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )


def test_oauth_credentials_usable_rejects_missing_client_id(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    provider = _fake_provider()

    assert (
        oauth_service.oauth_credentials_usable(
            provider,
            runtime_paths,
            {
                "token": "stored-token",
                "scopes": list(provider.scopes),
                "_source": "oauth",
                "_oauth_provider": provider.id,
            },
        )
        is False
    )


def test_oauth_credentials_usable_rejects_mismatched_client_id(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "new-client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    provider = _fake_provider()

    assert (
        oauth_service.oauth_credentials_usable(
            provider,
            runtime_paths,
            {
                "token": "stored-token",
                "client_id": "old-client-id",
                "scopes": list(provider.scopes),
                "_source": "oauth",
                "_oauth_provider": provider.id,
            },
        )
        is False
    )


def test_oauth_credentials_usable_accepts_expired_access_token_with_refresh(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {"TEST_OAUTH_CLIENT_ID": "client-id", "TEST_OAUTH_CLIENT_SECRET": "client-secret"},
    )
    provider = _fake_provider()

    assert oauth_service.oauth_credentials_usable(
        provider,
        runtime_paths,
        {
            "token": "expired-access-token",
            "refresh_token": "stored-refresh-token",
            "client_id": "client-id",
            "expires_at": 1.0,
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )


def test_status_rejects_refresh_token_without_required_scopes(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider()
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).save_credentials(
        provider.credential_service,
        {
            "token": "stored-token",
            "refresh_token": "stored-refresh-token",
            "scopes": ["different.scope"],
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["has_client_config"] is True
    assert status_response.json()["connected"] is False


def test_status_rejects_stored_oauth_token_disallowed_by_new_identity_policy(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(allowed_email_domains=("example.com",))
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).save_credentials(
        provider.credential_service,
        {
            "token": "stored-token",
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
            "_oauth_claims": {"email": "alice@blocked.example", "email_verified": True},
            "_oauth_claims_verified": True,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["connected"] is False


def test_status_rejects_stored_oauth_token_unverified_claim_summary(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(allowed_email_domains=("example.com",))
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).save_credentials(
        provider.credential_service,
        {
            "token": "stored-token",
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
            "_oauth_claims": {"email": "alice@example.com", "email_verified": True},
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["connected"] is False


def test_status_rejects_stored_oauth_token_missing_claims_when_identity_policy_configured(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "TEST_OAUTH_CLIENT_ID": "client-id",
            "TEST_OAUTH_CLIENT_SECRET": "client-secret",
            constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org",
        },
    )
    api_app = _make_test_app(runtime_paths, _config_payload(worker_scope="user_agent"))
    provider = _fake_provider(allowed_email_domains=("example.com",))
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.for_worker(_worker_key_for_matrix_user("@alice:example.org")).save_credentials(
        provider.credential_service,
        {
            "token": "stored-token",
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
    )

    with patch("mindroom.api.oauth.load_oauth_providers_for_snapshot", return_value={provider.id: provider}):
        with TestClient(api_app) as client:
            _login(client)
            status_response = client.get(f"/api/oauth/{provider.id}/status?agent_name=general")

    assert status_response.status_code == 200
    assert status_response.json()["connected"] is False


def test_required_scope_check_accepts_google_scope_supersets() -> None:
    calendar_provider = _fake_provider(scopes=("https://www.googleapis.com/auth/calendar.readonly",))
    gmail_provider = _fake_provider(scopes=("https://www.googleapis.com/auth/gmail.readonly",))
    drive_provider = _fake_provider(scopes=("https://www.googleapis.com/auth/drive.file",))
    sheets_provider = _fake_provider(scopes=("https://www.googleapis.com/auth/spreadsheets.readonly",))

    assert oauth_service.oauth_credentials_have_required_scopes(
        calendar_provider,
        {"scopes": ["https://www.googleapis.com/auth/calendar"]},
    )
    assert oauth_service.oauth_credentials_have_required_scopes(
        gmail_provider,
        {"scope": "https://www.googleapis.com/auth/gmail.modify"},
    )
    assert oauth_service.oauth_credentials_have_required_scopes(
        drive_provider,
        {"scopes": ["https://www.googleapis.com/auth/drive"]},
    )
    assert oauth_service.oauth_credentials_have_required_scopes(
        sheets_provider,
        {"scope": "https://www.googleapis.com/auth/spreadsheets"},
    )
