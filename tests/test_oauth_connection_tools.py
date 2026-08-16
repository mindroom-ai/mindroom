"""Tests for narrow agent-facing OAuth connection management."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AgentReplyPermission, AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import (
    get_runtime_credentials_manager,
    save_scoped_credentials,
    scoped_credentials_path,
)
from mindroom.custom_tools.oauth_connections import OAuthConnectionTools
from mindroom.message_target import MessageTarget
from mindroom.oauth import reset as oauth_reset
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    load_oauth_credentials_snapshot,
)
from mindroom.oauth.credential_store import _oauth_credential_database_path
from mindroom.oauth.google_calendar import google_calendar_oauth_provider
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.service import oauth_credentials_worker_target
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, write_config_yaml

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, WorkerScope


def _tool_and_context(
    tmp_path: Path,
    *,
    worker_scope: WorkerScope,
    context_agent_name: str = "research",
    requester_id: str = "@alice:example.org",
    aliases: dict[str, list[str]] | None = None,
) -> tuple[OAuthConnectionTools, ToolRuntimeContext, ResolvedWorkerTarget]:
    config = Config(
        agents={
            "research": AgentConfig(
                display_name="Research",
                role="Research",
                tools=["oauth_connections", "google_drive"],
                worker_scope=worker_scope,
            ),
        },
        teams={
            "research_team": TeamConfig(
                display_name="Research Team",
                role="Research together",
                agents=["research"],
            ),
        },
        authorization=AuthorizationConfig(
            aliases=aliases or {},
            agent_reply_permissions={"research": ["@alice:example.org"]},
        ),
        models={"default": {"provider": "openai", "id": "gpt-5.6"}},
    )
    config_path = tmp_path / "config.yaml"
    write_config_yaml(config, config_path)
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path, process_env={})
    context = ToolRuntimeContext(
        agent_name=context_agent_name,
        target=MessageTarget.resolve(
            room_id="!room:example.org",
            thread_id="$thread",
            reply_to_event_id="$request",
        ),
        requester_id=requester_id,
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    worker_target = build_agent_toolkit_worker_target(
        config.resolve_entity("research").execution_scope,
        "research",
        is_private=False,
        execution_identity=build_execution_identity_from_runtime_context(context),
        runtime_paths=runtime_paths,
    )
    return OAuthConnectionTools(runtime_paths, worker_target=worker_target), context, worker_target


def _reset_intent(
    result: str,
    *,
    provider: OAuthProvider,
    context: ToolRuntimeContext,
) -> oauth_reset.BrowserOAuthResetIntent:
    marker = "`reset_url`: "
    assert marker in result
    reset_url = result.split(marker, maxsplit=1)[1].split(";", maxsplit=1)[0]
    parsed = urlparse(reset_url)
    assert parsed.path == f"/api/oauth/{provider.id}/reset"
    token = parse_qs(parsed.query)["reset_token"][0]
    return oauth_reset.lookup_browser_oauth_reset_intent(provider, context.runtime_paths, token)


def _save_credentials(
    context: ToolRuntimeContext,
    worker_target: ResolvedWorkerTarget,
    provider: OAuthProvider,
    refresh_token: str,
) -> dict[str, str]:
    credentials = {"refresh_token": refresh_token}
    save_scoped_credentials(
        provider.credential_service,
        credentials,
        credentials_manager=get_runtime_credentials_manager(context.runtime_paths),
        worker_target=worker_target,
    )
    return credentials


def test_oauth_connections_exposes_only_browser_reset() -> None:
    """The toolkit should expose one non-destructive browser action."""
    tool = OAuthConnectionTools(MagicMock(), worker_target=None)

    assert list(tool.async_functions) == ["reset_oauth_connection"]
    function = tool.async_functions["reset_oauth_connection"]
    assert function.requires_confirmation is False
    assert function.stop_after_tool_call is False


@pytest.mark.asyncio
async def test_reset_oauth_connection_issues_browser_confirmation_without_mutating_credentials(
    tmp_path: Path,
) -> None:
    """The agent tool should issue a browser action and leave deletion to its confirmation POST."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    credentials = _save_credentials(context, worker_target, provider, "refresh-token")

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    lifecycle_context = OAuthCredentialContext(
        provider=provider,
        runtime_paths=context.runtime_paths,
        credentials_manager=get_runtime_credentials_manager(context.runtime_paths),
        worker_target=worker_target,
    )
    stored = (await load_oauth_credentials_snapshot(lifecycle_context)).credentials
    assert stored is not None
    assert stored["refresh_token"] == credentials["refresh_token"]
    intent = _reset_intent(result, provider=provider, context=context)
    assert intent.agent_name == "research"
    assert intent.requester_id == "@alice:example.org"
    assert intent.worker_scope == "user_agent"


@pytest.mark.asyncio
async def test_reset_oauth_connection_issues_browser_confirmation_for_unreadable_credentials(
    tmp_path: Path,
) -> None:
    """A corrupt credential must still be recoverable through the public browser-reset action."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    credentials_path = scoped_credentials_path(
        provider.credential_service,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    corrupt_payload = b"not-a-readable-credential"
    credentials_path.write_bytes(corrupt_payload)

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    intent = _reset_intent(result, provider=provider, context=context)
    assert intent.connection_generation == "initial"
    lifecycle_context = OAuthCredentialContext(
        provider=provider,
        runtime_paths=context.runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    with pytest.raises(OAuthProviderError, match="could not be loaded"):
        await load_oauth_credentials_snapshot(lifecycle_context)
    assert _oauth_credential_database_path(lifecycle_context).exists()


@pytest.mark.asyncio
async def test_reset_oauth_connection_uses_team_member_ownership(tmp_path: Path) -> None:
    """A team member toolkit should issue a link for its owning agent scope."""
    tool, context, _worker_target = _tool_and_context(
        tmp_path,
        worker_scope="user_agent",
        context_agent_name="research_team",
    )
    provider = google_drive_oauth_provider()

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    intent = _reset_intent(result, provider=provider, context=context)
    assert intent.agent_name == "research"
    assert intent.requester_id == "@alice:example.org"


@pytest.mark.asyncio
async def test_reset_oauth_connection_canonicalizes_bridge_alias_scope(tmp_path: Path) -> None:
    """A bridge alias should issue a reset for the canonical requester's credential scope."""
    alias = "@telegram_alice:example.org"
    tool, context, worker_target = _tool_and_context(
        tmp_path,
        worker_scope="user_agent",
        requester_id=alias,
        aliases={"@alice:example.org": [alias]},
    )
    provider = google_drive_oauth_provider()
    canonical_target = oauth_credentials_worker_target(
        provider,
        worker_target,
        authorization=context.config.authorization,
    )
    assert canonical_target is not None

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    intent = _reset_intent(result, provider=provider, context=context)
    assert intent.requester_id == "@alice:example.org"
    assert intent.worker_key == canonical_target.worker_key


@pytest.mark.asyncio
async def test_reset_oauth_connection_refuses_shared_scope(tmp_path: Path) -> None:
    """The agent-facing action must not target credentials shared by requesters."""
    tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="shared")

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection("google_drive")

    assert "requester-isolated" in result


@pytest.mark.asyncio
async def test_reset_oauth_connection_denies_unauthorized_requester(tmp_path: Path) -> None:
    """Current authorization should be checked before issuing the browser action."""
    tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    context.config.authorization.agent_reply_permissions = {
        "research": AgentReplyPermission(users=["@bob:example.org"]),
    }

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection("google_drive")

    assert "not authorized" in result


@pytest.mark.asyncio
async def test_reset_oauth_connection_denies_provider_not_backing_agent_tool(tmp_path: Path) -> None:
    """Only providers backing the current agent's tools may receive reset links."""
    tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(google_calendar_oauth_provider().id)

    assert "is not available to this agent" in result


@pytest.mark.asyncio
async def test_reset_oauth_connection_requires_live_runtime_context(tmp_path: Path) -> None:
    """A detached tool cannot issue a requester-bound browser action."""
    tool, _context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")

    result = await tool.reset_oauth_connection("google_drive")

    assert result == "Error: OAuth reset requires a live agent request context."
