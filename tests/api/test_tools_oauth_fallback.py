"""Tests for manual credential fallbacks on OAuth-backed tools."""

# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom import tools as _mindroom_tools  # noqa: F401
from mindroom.api import tools as tools_api
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.google_cloud import google_cloud_oauth_provider
from mindroom.oauth.google_tasks import google_tasks_oauth_provider
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.metadata import export_tools_metadata
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def _worker_target(requester_id: str) -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!room:example.test",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
    )
    return resolve_worker_target("user_agent", "code", execution_identity=identity)


def _provider_context(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget,
) -> tools_api._ResolvedToolAvailabilityContext:
    return tools_api._ResolvedToolAvailabilityContext(
        execution_scope="user_agent",
        dashboard_configuration_supported=True,
        status_authoritative=True,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
        allowed_shared_services=None,
        auth_provider_credential_services={provider.id: provider.credential_service},
        oauth_providers={provider.id: provider},
        runtime_paths=runtime_paths,
    )


def _context(
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget,
) -> tools_api._ResolvedToolAvailabilityContext:
    return _provider_context(github_oauth_provider(), runtime_paths, credentials_manager, worker_target)


def _github_tool() -> dict[str, object]:
    return {
        "name": "github",
        "status": "requires_config",
        "setup_type": "oauth",
        "auth_provider": "github",
        "config_fields": [
            {"name": "access_token", "required": False},
            {"name": "base_url", "required": False},
        ],
        "oauth_fallback_fields": ["access_token"],
    }


@pytest.mark.asyncio
async def test_manual_oauth_fallback_status_is_requester_scoped_and_secret_free(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    alice_target = _worker_target("@alice:example.test")
    bob_target = _worker_target("@bob:example.test")
    manual_secret = "github-manual-secret"  # noqa: S105
    save_scoped_credentials(
        "github",
        {"access_token": manual_secret, "base_url": "https://api.github.com"},
        credentials_manager=manager,
        worker_target=alice_target,
        primary_built_tool=True,
    )
    alice_tool = _github_tool()
    bob_tool = _github_tool()

    await tools_api._update_tools_statuses([alice_tool], _context(runtime_paths, manager, alice_target))
    await tools_api._update_tools_statuses([bob_tool], _context(runtime_paths, manager, bob_target))

    assert alice_tool["status"] == "available"
    assert alice_tool["manual_auth_configured"] is True
    assert bob_tool["status"] == "requires_config"
    assert bob_tool["manual_auth_configured"] is False
    assert manual_secret not in repr(alice_tool)


@pytest.mark.asyncio
async def test_blank_manual_oauth_fallback_does_not_mark_tool_available(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    target = _worker_target("@alice:example.test")
    save_scoped_credentials(
        "github",
        {"access_token": "   ", "base_url": "https://api.github.com"},
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
    )
    tool = _github_tool()

    await tools_api._update_tools_statuses([tool], _context(runtime_paths, manager, target))

    assert tool["status"] == "requires_config"
    assert tool["manual_auth_configured"] is False


@pytest.mark.asyncio
async def test_environment_oauth_fallback_status_is_available_and_secret_free(tmp_path: Path) -> None:
    environment_secret = "github-environment-secret"  # noqa: S105
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"GITHUB_ACCESS_TOKEN": f"  {environment_secret}  "},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    target = _worker_target("@alice:example.test")
    tool = _github_tool()

    await tools_api._update_tools_statuses([tool], _context(runtime_paths, manager, target))

    assert tool["status"] == "available"
    assert tool["manual_auth_configured"] is False
    assert tool["environment_auth_configured"] is True
    assert environment_secret not in repr(tool)


@pytest.mark.asyncio
async def test_tool_status_reads_settings_from_primary_stores(tmp_path: Path) -> None:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_SANDBOX_PROXY_URL": "http://sandbox:8765", "MINDROOM_SANDBOX_PROXY_TOKEN": "token"},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    target = resolve_worker_target("shared", "code", execution_identity=None, tenant_id="test-tenant")
    assert target.worker_key is not None
    manager.for_worker(target.worker_key).save_credentials("postgres", {"host": "worker-planted.example.test"})
    tool = {
        "name": "postgres",
        "status": "requires_config",
        "config_fields": [{"name": "host", "required": True}],
    }
    context = tools_api._ResolvedToolAvailabilityContext(
        execution_scope="shared",
        dashboard_configuration_supported=True,
        status_authoritative=True,
        credentials_manager=manager,
        worker_target=target,
        allowed_shared_services=None,
        auth_provider_credential_services={},
        oauth_providers={},
        runtime_paths=runtime_paths,
    )

    await tools_api._update_tools_statuses([tool], context)
    assert tool["status"] == "requires_config"

    manager.for_primary_runtime_agent_scope("code").save_credentials("postgres", {"host": "primary.example.test"})
    await tools_api._update_tools_statuses([tool], context)
    assert tool["status"] == "available"


def _google_tool(tool_name: str) -> dict[str, Any]:
    tool = export_tools_metadata({tool_name: TOOL_METADATA[tool_name]})[0]
    assert tool["status"] == "requires_config"
    return tool


def _connect_google(
    provider: OAuthProvider,
    connection: str,
    tmp_path: Path,
    target: ResolvedWorkerTarget,
) -> tuple[RuntimePaths, CredentialsManager]:
    """Return a runtime whose Google provider is connected through OAuth or the service account file."""
    process_env = (
        {"GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "service-account.json")}
        if connection == "service_account"
        else {}
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env,
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    if connection == "oauth":
        manager.save_credentials(
            "google_oauth_client",
            {"client_id": "client-id", "client_secret": "client-secret", "_source": "ui"},
        )
        publish_oauth_credentials(
            provider,
            {
                "token": "access-token",
                "refresh_token": "refresh-token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client-id",
                "expires_at": 4_102_444_800.0,
                "scopes": list(provider.scopes),
                "_source": "oauth",
                "_oauth_provider": provider.id,
            },
            credentials_manager=manager,
            worker_target=target,
        )
    return runtime_paths, manager


def _save_bigquery_settings(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    settings: dict[str, str],
) -> None:
    save_scoped_credentials(
        "google_bigquery",
        settings,
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
    )


_BIGQUERY_SETTINGS = {"project": "example-project", "dataset": "analytics", "location": "US"}


@pytest.mark.asyncio
@pytest.mark.parametrize("connection", ["oauth", "service_account"])
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"project": "example-project", "dataset": "analytics"},
        {"dataset": "analytics", "location": "US"},
    ],
)
async def test_connected_google_cloud_tool_without_required_settings_requires_config(
    tmp_path: Path,
    connection: str,
    settings: dict[str, str],
) -> None:
    provider = google_cloud_oauth_provider()
    target = _worker_target("@alice:example.test")
    runtime_paths, manager = _connect_google(provider, connection, tmp_path, target)
    if settings:
        _save_bigquery_settings(manager, target, settings)
    tool = _google_tool("google_bigquery")

    await tools_api._update_tools_statuses([tool], _provider_context(provider, runtime_paths, manager, target))

    assert tool["status"] == "requires_config"


@pytest.mark.asyncio
@pytest.mark.parametrize("connection", ["oauth", "service_account"])
async def test_connected_google_cloud_tool_with_required_settings_is_available(
    tmp_path: Path,
    connection: str,
) -> None:
    provider = google_cloud_oauth_provider()
    target = _worker_target("@alice:example.test")
    runtime_paths, manager = _connect_google(provider, connection, tmp_path, target)
    _save_bigquery_settings(manager, target, _BIGQUERY_SETTINGS)
    tool = _google_tool("google_bigquery")

    await tools_api._update_tools_statuses([tool], _provider_context(provider, runtime_paths, manager, target))

    assert tool["status"] == "available"


@pytest.mark.asyncio
async def test_google_cloud_settings_alone_do_not_make_tool_available(tmp_path: Path) -> None:
    provider = google_cloud_oauth_provider()
    target = _worker_target("@alice:example.test")
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _save_bigquery_settings(manager, target, _BIGQUERY_SETTINGS)
    tool = _google_tool("google_bigquery")

    await tools_api._update_tools_statuses([tool], _provider_context(provider, runtime_paths, manager, target))

    assert tool["status"] == "requires_config"


@pytest.mark.asyncio
async def test_connected_oauth_tool_without_required_settings_is_available(tmp_path: Path) -> None:
    provider = google_tasks_oauth_provider()
    target = _worker_target("@alice:example.test")
    runtime_paths, manager = _connect_google(provider, "oauth", tmp_path, target)
    tool = _google_tool("google_tasks")
    assert not any(field["required"] for field in tool["config_fields"])

    await tools_api._update_tools_statuses([tool], _provider_context(provider, runtime_paths, manager, target))

    assert tool["status"] == "available"
