"""Tests for narrow agent-facing OAuth connection management."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials, save_scoped_credentials
from mindroom.custom_tools import oauth_connections as oauth_connections_module
from mindroom.custom_tools.oauth_connections import OAuthConnectionTools
from mindroom.message_target import MessageTarget
from mindroom.oauth.google_calendar import google_calendar_oauth_provider
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.oauth.service import lookup_oauth_connect_token
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, write_config_yaml

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


def _tool_and_context(
    tmp_path: Path,
    *,
    worker_scope: str,
) -> tuple[OAuthConnectionTools, ToolRuntimeContext]:
    config = Config(
        agents={
            "research": AgentConfig(
                display_name="Research",
                role="Research",
                tools=["oauth_connections", "google_drive"],
                worker_scope=worker_scope,
            ),
        },
        models={"default": {"provider": "openai", "id": "gpt-4o"}},
    )
    config_path = tmp_path / "config.yaml"
    write_config_yaml(config, config_path)
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path, process_env={})
    tool = OAuthConnectionTools(runtime_paths)
    context = ToolRuntimeContext(
        agent_name="research",
        target=MessageTarget.resolve(
            room_id="!room:example.org",
            thread_id="$thread",
            reply_to_event_id="$request",
        ),
        requester_id="@alice:example.org",
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    return tool, context


def _connect_url_from_result(result: str) -> str:
    marker = "`connect_url`: "
    assert marker in result
    return result.split(marker, maxsplit=1)[1].split(";", maxsplit=1)[0]


def _save_test_credentials(
    context: ToolRuntimeContext,
    provider: OAuthProvider,
    refresh_token: str,
) -> tuple[CredentialsManager, ResolvedWorkerTarget, dict[str, str]]:
    worker_target = context.resolve_worker_target()
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    credentials = {"refresh_token": refresh_token}
    save_scoped_credentials(
        provider.credential_service,
        credentials,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    return credentials_manager, worker_target, credentials


def test_oauth_connections_exposes_only_approval_gated_reset(tmp_path: Path) -> None:
    """The narrow toolkit should grant no configuration-management functions."""
    tool, _context = _tool_and_context(tmp_path, worker_scope="user_agent")

    assert list(tool.async_functions) == ["reset_oauth_connection"]
    assert tool.functions == {}
    assert tool.async_functions["reset_oauth_connection"].requires_confirmation is True


@pytest.mark.asyncio
async def test_reset_oauth_connection_deletes_only_current_requester_scope(tmp_path: Path) -> None:
    """An approved reset should delete the caller's scoped grant and return a bound reconnect link."""
    tool, context = _tool_and_context(tmp_path, worker_scope="user_agent")
    worker_target = context.resolve_worker_target()
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    provider = google_drive_oauth_provider()
    save_scoped_credentials(
        provider.credential_service,
        {"refresh_token": "refresh-token"},
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        is None
    )
    query = parse_qs(urlparse(_connect_url_from_result(result)).query)
    connect_target = lookup_oauth_connect_token(
        provider,
        context.runtime_paths,
        query["connect_token"][0],
    )
    assert connect_target.agent_name == "research"
    assert connect_target.requester_id == "@alice:example.org"
    assert connect_target.worker_scope == "user_agent"


@pytest.mark.asyncio
async def test_reset_oauth_connection_reports_user_scope_blast_radius(tmp_path: Path) -> None:
    """The approval receipt should state when a reset affects this requester across agents."""
    tool, context = _tool_and_context(tmp_path, worker_scope="user")
    provider = google_drive_oauth_provider()

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert "for this requester across agents" in result


@pytest.mark.asyncio
async def test_reset_oauth_connection_refuses_shared_scope(tmp_path: Path) -> None:
    """The agent-facing tool must not disconnect a credential shared with other requesters."""
    tool, context = _tool_and_context(tmp_path, worker_scope="shared")
    worker_target = context.resolve_worker_target()
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    provider = google_drive_oauth_provider()
    save_scoped_credentials(
        provider.credential_service,
        {"refresh_token": "shared-refresh-token"},
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert "requester-isolated" in result
    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        is not None
    )


@pytest.mark.asyncio
async def test_reset_oauth_connection_denies_unauthorized_requester_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reply permissions must deny reset before credential or MCP state changes."""
    tool, context = _tool_and_context(tmp_path, worker_scope="user_agent")
    context.config.authorization.agent_reply_permissions = {"research": ["@bob:example.org"]}
    provider = google_drive_oauth_provider()
    credentials_manager, worker_target, credentials = _save_test_credentials(
        context,
        provider,
        "unauthorized-refresh-token",
    )
    disconnect = AsyncMock()
    monkeypatch.setattr(oauth_connections_module, "disconnect_mcp_oauth_request_session", disconnect)

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert "not authorized" in result
    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        == credentials
    )
    disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_oauth_connection_denies_provider_not_backing_agent_tool_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only providers backing the current agent's tools may be reset."""
    tool, context = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_calendar_oauth_provider()
    credentials_manager, worker_target, credentials = _save_test_credentials(
        context,
        provider,
        "unavailable-refresh-token",
    )
    disconnect = AsyncMock()
    monkeypatch.setattr(oauth_connections_module, "disconnect_mcp_oauth_request_session", disconnect)

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert "is not available to this agent" in result
    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        == credentials
    )
    disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_oauth_connection_denies_unconfigured_provider_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog-allowed provider must still exist in the active OAuth registry."""
    tool, context = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    credentials_manager, worker_target, credentials = _save_test_credentials(
        context,
        provider,
        "unconfigured-refresh-token",
    )
    disconnect = AsyncMock()
    monkeypatch.setattr(oauth_connections_module, "load_oauth_providers", lambda *_args: {})
    monkeypatch.setattr(oauth_connections_module, "disconnect_mcp_oauth_request_session", disconnect)

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert "is not configured" in result
    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        == credentials
    )
    disconnect.assert_not_awaited()
