"""Tests for manual credential fallbacks on OAuth-backed tools."""

# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.api import tools as tools_api
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.oauth.github import github_oauth_provider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
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


def _context(
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget,
) -> tools_api._ResolvedToolAvailabilityContext:
    provider = github_oauth_provider()
    return tools_api._ResolvedToolAvailabilityContext(
        execution_scope="user_agent",
        dashboard_configuration_supported=True,
        status_authoritative=True,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
        allowed_shared_services=None,
        auth_provider_credential_services={"github": provider.credential_service},
        oauth_providers={"github": provider},
        runtime_paths=runtime_paths,
    )


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
