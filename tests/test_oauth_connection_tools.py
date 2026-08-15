"""Tests for narrow agent-facing OAuth connection management."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from agno.models.response import ToolExecution

from mindroom.approval_bindings import build_approval_tool_bindings
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials, save_scoped_credentials
from mindroom.custom_tools.oauth_connections import OAuthConnectionTools
from mindroom.message_target import MessageTarget
from mindroom.oauth import credential_lifecycle
from mindroom.oauth import reset as oauth_reset_module
from mindroom.oauth import reset_execution as oauth_reset_execution_module
from mindroom.oauth.google_calendar import google_calendar_oauth_provider
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.oauth.reset import (
    build_oauth_reset_approval_bindings,
    validate_oauth_reset_approval_bindings,
)
from mindroom.oauth.service import lookup_oauth_connect_token, oauth_credentials_worker_target
from mindroom.tool_system.runtime_context import (
    ApprovalToolOperation,
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, write_config_yaml

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.credentials import CredentialsManager
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
        approval_operation=ApprovalToolOperation(
            approval_id="approval-default",
            generation=1,
            tool_call_id="reset-call",
            credential_generation="initial",
            connection_generation="initial",
        ),
    )
    worker_target = build_agent_toolkit_worker_target(
        config.resolve_entity("research").execution_scope,
        "research",
        is_private=False,
        execution_identity=build_execution_identity_from_runtime_context(context),
        runtime_paths=runtime_paths,
    )
    tool = OAuthConnectionTools(runtime_paths, worker_target=worker_target)
    return tool, context, worker_target


def _connect_url_from_result(result: str) -> str:
    marker = "`connect_url`: "
    assert marker in result
    return result.split(marker, maxsplit=1)[1].split(";", maxsplit=1)[0]


def _save_test_credentials(
    context: ToolRuntimeContext,
    worker_target: ResolvedWorkerTarget,
    provider: OAuthProvider,
    refresh_token: str,
) -> tuple[CredentialsManager, ResolvedWorkerTarget, dict[str, str]]:
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
    tool, _context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")

    assert list(tool.async_functions) == ["reset_oauth_connection"]
    assert tool.functions == {}
    assert tool.async_functions["reset_oauth_connection"].requires_confirmation is True
    assert tool.async_functions["reset_oauth_connection"].stop_after_tool_call is True


@pytest.mark.asyncio
async def test_reset_oauth_connection_deletes_only_current_requester_scope(tmp_path: Path) -> None:
    """An approved reset should delete the caller's scoped grant and return a bound reconnect link."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
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
    assert "valid for 10 minutes" in result
    assert "run this reset again" in result


@pytest.mark.asyncio
async def test_reset_oauth_connection_requires_approved_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live request context alone must not authorize destructive credential deletion."""
    tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    execute_reset = AsyncMock()
    monkeypatch.setattr(
        "mindroom.custom_tools.oauth_connections.execute_oauth_connection_reset",
        execute_reset,
    )

    with tool_runtime_context(replace(context, approval_operation=None)):
        result = await tool.reset_oauth_connection("google_drive")

    assert result == "Error: OAuth reset requires an approved operation."
    execute_reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_oauth_reset_approval_binding_rejects_worker_scope_drift(tmp_path: Path) -> None:
    """Approval must freeze the exact credential target rather than only provider_id."""
    _tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    tool_call = ToolExecution(
        tool_call_id="reset-call",
        tool_name="reset_oauth_connection",
        tool_args={"provider_id": "google_drive"},
        requires_confirmation=True,
    )
    execution_identity = build_execution_identity_from_runtime_context(context)
    bindings = await build_oauth_reset_approval_bindings(
        ((tool_call, "reset-call", "reset_oauth_connection", "research"),),
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=execution_identity,
    )
    context.config.agents["research"].worker_scope = "user"

    with pytest.raises(RuntimeError, match="credential target changed"):
        await validate_oauth_reset_approval_bindings(
            calls=(("reset-call", "reset_oauth_connection", "research", True),),
            bindings=bindings,
            config=context.config,
            runtime_paths=context.runtime_paths,
            execution_identity=execution_identity,
        )


@pytest.mark.asyncio
async def test_oauth_reset_approval_binding_rejects_connection_generation_drift(tmp_path: Path) -> None:
    """A waiting reset approval must not delete credentials connected after card creation."""
    _tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    provider = google_drive_oauth_provider()
    save_scoped_credentials(
        provider.credential_service,
        {"refresh_token": "first-refresh-token"},
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    tool_call = ToolExecution(
        tool_call_id="reset-call",
        tool_name="reset_oauth_connection",
        tool_args={"provider_id": provider.id},
        requires_confirmation=True,
    )
    execution_identity = build_execution_identity_from_runtime_context(context)
    bindings = await build_oauth_reset_approval_bindings(
        ((tool_call, "reset-call", "reset_oauth_connection", "research"),),
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=execution_identity,
    )
    reset_target = oauth_reset_module.resolve_oauth_reset_target(
        provider.id,
        agent_name="research",
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=execution_identity,
    )
    assert await credential_lifecycle.reset_oauth_credentials(reset_target.credential_context) is True
    state = credential_lifecycle._load_oauth_credential_state(reset_target.credential_context)
    credential_lifecycle._publish_oauth_credentials_locked(
        reset_target.credential_context,
        {"refresh_token": "replacement-refresh-token"},
        state=state,
        advance_connection_generation=True,
    )

    with pytest.raises(RuntimeError, match="credential target changed"):
        await validate_oauth_reset_approval_bindings(
            calls=(("reset-call", "reset_oauth_connection", "research", True),),
            bindings=bindings,
            config=context.config,
            runtime_paths=context.runtime_paths,
            execution_identity=execution_identity,
        )


@pytest.mark.asyncio
async def test_oauth_reset_approval_binding_allows_same_connection_refresh_drift(tmp_path: Path) -> None:
    """A token refresh may advance its lease revision without invalidating reset approval."""
    _tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    provider = google_drive_oauth_provider()
    save_scoped_credentials(
        provider.credential_service,
        {"refresh_token": "first-refresh-token"},
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    tool_call = ToolExecution(
        tool_call_id="reset-call",
        tool_name="reset_oauth_connection",
        tool_args={"provider_id": provider.id},
        requires_confirmation=True,
    )
    execution_identity = build_execution_identity_from_runtime_context(context)
    bindings = await build_oauth_reset_approval_bindings(
        ((tool_call, "reset-call", "reset_oauth_connection", "research"),),
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=execution_identity,
    )
    reset_target = oauth_reset_module.resolve_oauth_reset_target(
        provider.id,
        agent_name="research",
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=execution_identity,
    )
    state = credential_lifecycle._load_oauth_credential_state(reset_target.credential_context)
    _credentials, refreshed_state = credential_lifecycle._publish_oauth_credentials_locked(
        reset_target.credential_context,
        {"refresh_token": "rotated-refresh-token"},
        state=state,
        advance_connection_generation=False,
    )

    await validate_oauth_reset_approval_bindings(
        calls=(("reset-call", "reset_oauth_connection", "research", True),),
        bindings=bindings,
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=execution_identity,
    )

    reset_binding = bindings["reset-call"]
    assert reset_binding["credential_generation"] != refreshed_state.generation
    assert reset_binding["connection_generation"] == refreshed_state.connection_generation


@pytest.mark.asyncio
async def test_oauth_reset_must_be_only_call_in_approval_generation(tmp_path: Path) -> None:
    """A reset cannot share one approval generation with another side effect."""
    _tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    reset_call = ToolExecution(
        tool_call_id="reset-call",
        tool_name="reset_oauth_connection",
        tool_args={"provider_id": "google_drive"},
        requires_confirmation=True,
    )
    other_call = ToolExecution(
        tool_call_id="other-call",
        tool_name="other_tool",
        tool_args={},
        requires_confirmation=True,
    )

    with pytest.raises(RuntimeError, match="only tool call"):
        await build_approval_tool_bindings(
            (
                (reset_call, "reset-call", "reset_oauth_connection", "research"),
                (other_call, "other-call", "other_tool", "research"),
            ),
            config=context.config,
            runtime_paths=context.runtime_paths,
            execution_identity=build_execution_identity_from_runtime_context(context),
        )


@pytest.mark.asyncio
async def test_oauth_reset_rejects_executed_non_confirmation_sibling(tmp_path: Path) -> None:
    """A reset pause cannot hide an ordinary sibling that Agno already dispatched."""
    _tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    ordinary_call = ToolExecution(
        tool_call_id="ordinary-call",
        tool_name="ordinary_side_effect",
        tool_args={"value": 1},
    )
    reset_call = ToolExecution(
        tool_call_id="reset-call",
        tool_name="reset_oauth_connection",
        tool_args={"provider_id": "google_drive"},
        requires_confirmation=True,
    )

    with pytest.raises(RuntimeError, match="only tool call"):
        await build_approval_tool_bindings(
            ((reset_call, "reset-call", "reset_oauth_connection", "research"),),
            observed_tools=(ordinary_call, reset_call),
            config=context.config,
            runtime_paths=context.runtime_paths,
            execution_identity=build_execution_identity_from_runtime_context(context),
        )


@pytest.mark.asyncio
async def test_oauth_reset_rejects_conflicting_sibling_with_same_call_id(tmp_path: Path) -> None:
    """A model-supplied call-ID collision cannot hide an already-executed side effect."""
    _tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    ordinary_call = ToolExecution(
        tool_call_id="reset-call",
        tool_name="ordinary_side_effect",
        tool_args={"value": 1},
    )
    reset_call = ToolExecution(
        tool_call_id="reset-call",
        tool_name="reset_oauth_connection",
        tool_args={"provider_id": "google_drive"},
        requires_confirmation=True,
    )

    with pytest.raises(RuntimeError, match="only tool call"):
        await build_approval_tool_bindings(
            ((reset_call, "reset-call", "reset_oauth_connection", "research"),),
            observed_tools=(ordinary_call, reset_call),
            config=context.config,
            runtime_paths=context.runtime_paths,
            execution_identity=build_execution_identity_from_runtime_context(context),
        )


@pytest.mark.asyncio
async def test_oauth_reset_rejects_requirement_only_reset_with_observed_sibling(tmp_path: Path) -> None:
    """A requirement-only reset cannot hide a separately observed ordinary call."""
    _tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    ordinary_call = ToolExecution(
        tool_call_id="ordinary-call",
        tool_name="ordinary_side_effect",
        tool_args={"value": 1},
    )
    reset_call = ToolExecution(
        tool_call_id="reset-call",
        tool_name="reset_oauth_connection",
        tool_args={"provider_id": "google_drive"},
        requires_confirmation=True,
    )

    with pytest.raises(RuntimeError, match="only tool call"):
        await build_approval_tool_bindings(
            ((reset_call, "reset-call", "reset_oauth_connection", "research"),),
            observed_tools=(ordinary_call,),
            config=context.config,
            runtime_paths=context.runtime_paths,
            execution_identity=build_execution_identity_from_runtime_context(context),
        )


@pytest.mark.asyncio
async def test_reset_uses_stable_approval_operation_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live tool must pass its persisted approval identity to the reset owner."""
    tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    context = replace(
        context,
        approval_operation=ApprovalToolOperation(
            approval_id="approval-1",
            generation=4,
            tool_call_id="reset-call",
            credential_generation="credential-generation-1",
            connection_generation="connection-generation-1",
        ),
    )
    execute_reset = AsyncMock(return_value="receipt")
    monkeypatch.setattr(
        "mindroom.custom_tools.oauth_connections.execute_oauth_connection_reset",
        execute_reset,
    )

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection("google_drive")

    assert result == "receipt"
    assert execute_reset.await_args.kwargs["operation_id"] == "approval-1:4:reset-call"
    assert execute_reset.await_args.kwargs["expected_connection_generation"] == "connection-generation-1"


@pytest.mark.asyncio
async def test_approved_reset_propagates_runtime_failure_for_durable_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approved reset failure must not become a normal completed Agno tool result."""
    tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    context = replace(
        context,
        approval_operation=ApprovalToolOperation(
            approval_id="approval-1",
            generation=4,
            tool_call_id="reset-call",
            credential_generation="credential-generation-1",
            connection_generation="connection-generation-1",
        ),
    )
    execute_reset = AsyncMock(side_effect=OSError("state publication failed"))
    monkeypatch.setattr(
        "mindroom.custom_tools.oauth_connections.execute_oauth_connection_reset",
        execute_reset,
    )

    with tool_runtime_context(context), pytest.raises(OSError, match="state publication failed"):
        await tool.reset_oauth_connection("google_drive")


@pytest.mark.asyncio
async def test_completed_reset_replay_skips_retirement_and_preserves_reconnected_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receipt replay must not retire or delete a session connected after reset completion."""
    _tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    _credentials_manager, worker_target, _credentials = _save_test_credentials(
        context,
        worker_target,
        provider,
        "first-refresh-token",
    )
    reset_target = oauth_reset_module.resolve_oauth_reset_target(
        provider.id,
        agent_name="research",
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=build_execution_identity_from_runtime_context(context),
        worker_target=worker_target,
    )
    approved_generation = credential_lifecycle.oauth_connection_generation(reset_target.credential_context)
    operation_id = "approval-1:4:reset-call"
    assert await credential_lifecycle.reset_oauth_credentials(
        reset_target.credential_context,
        operation_id=operation_id,
        expected_connection_generation=approved_generation,
    )
    replacement = {"refresh_token": "replacement-refresh-token"}
    state = credential_lifecycle._load_oauth_credential_state(reset_target.credential_context)
    credential_lifecycle._publish_oauth_credentials_locked(
        reset_target.credential_context,
        replacement,
        state=state,
        advance_connection_generation=True,
    )
    retire = MagicMock(side_effect=AssertionError("completed replay retired new session"))
    monkeypatch.setattr(oauth_reset_execution_module, "retire_mcp_oauth_request_session", retire)

    result = await oauth_reset_execution_module.execute_oauth_connection_reset(
        provider.id,
        agent_name="research",
        config=context.config,
        runtime_paths=context.runtime_paths,
        execution_identity=build_execution_identity_from_runtime_context(context),
        worker_target=worker_target,
        operation_id=operation_id,
        expected_connection_generation=approved_generation,
    )

    assert "Error:" not in result
    assert credential_lifecycle.load_oauth_credentials(reset_target.credential_context) == replacement
    retire.assert_not_called()


@pytest.mark.asyncio
async def test_reset_oauth_connection_uses_team_member_ownership(tmp_path: Path) -> None:
    """A team member toolkit must manage its owning agent scope from a team request context."""
    tool, context, worker_target = _tool_and_context(
        tmp_path,
        worker_scope="user_agent",
        context_agent_name="research_team",
    )
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    provider = google_drive_oauth_provider()
    save_scoped_credentials(
        provider.credential_service,
        {"refresh_token": "team-member-refresh-token"},
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert "Error:" not in result
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


@pytest.mark.asyncio
async def test_reset_oauth_connection_canonicalizes_bridge_alias_scope(tmp_path: Path) -> None:
    """An authorized bridge alias must reset and reconnect the canonical requester's credential scope."""
    alias = "@telegram_alice:example.org"
    tool, context, worker_target = _tool_and_context(
        tmp_path,
        worker_scope="user_agent",
        requester_id=alias,
        aliases={"@alice:example.org": [alias]},
    )
    assert worker_target.execution_identity is not None
    assert worker_target.execution_identity.requester_id == alias
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    provider = google_drive_oauth_provider()
    canonical_worker_target = oauth_credentials_worker_target(
        provider,
        worker_target,
        authorization=context.config.authorization,
    )
    assert canonical_worker_target is not None
    save_scoped_credentials(
        provider.credential_service,
        {"refresh_token": "canonical-refresh-token"},
        credentials_manager=credentials_manager,
        worker_target=canonical_worker_target,
    )

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert "Error:" not in result
    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=canonical_worker_target,
        )
        is None
    )
    query = parse_qs(urlparse(_connect_url_from_result(result)).query)
    connect_target = lookup_oauth_connect_token(
        provider,
        context.runtime_paths,
        query["connect_token"][0],
    )
    assert connect_target.requester_id == "@alice:example.org"


@pytest.mark.asyncio
async def test_reset_oauth_connection_reports_user_scope_blast_radius(tmp_path: Path) -> None:
    """The approval receipt should state when a reset affects this requester across agents."""
    tool, context, _worker_target = _tool_and_context(tmp_path, worker_scope="user")
    provider = google_drive_oauth_provider()

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert "for this requester across agents" in result


@pytest.mark.asyncio
async def test_reset_oauth_connection_refuses_shared_scope(tmp_path: Path) -> None:
    """The agent-facing tool must not disconnect a credential shared with other requesters."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="shared")
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
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    context.config.authorization.agent_reply_permissions = {"research": ["@bob:example.org"]}
    provider = google_drive_oauth_provider()
    credentials_manager, worker_target, credentials = _save_test_credentials(
        context,
        worker_target,
        provider,
        "unauthorized-refresh-token",
    )
    retire = MagicMock()
    monkeypatch.setattr(oauth_reset_execution_module, "retire_mcp_oauth_request_session", retire)

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
    retire.assert_not_called()


@pytest.mark.asyncio
async def test_reset_oauth_connection_denies_provider_not_backing_agent_tool_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only providers backing the current agent's tools may be reset."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_calendar_oauth_provider()
    credentials_manager, worker_target, credentials = _save_test_credentials(
        context,
        worker_target,
        provider,
        "unavailable-refresh-token",
    )
    retire = MagicMock()
    monkeypatch.setattr(oauth_reset_execution_module, "retire_mcp_oauth_request_session", retire)

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
    retire.assert_not_called()


@pytest.mark.asyncio
async def test_reset_oauth_connection_denies_unconfigured_provider_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog-allowed provider must still exist in the active OAuth registry."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    credentials_manager, worker_target, credentials = _save_test_credentials(
        context,
        worker_target,
        provider,
        "unconfigured-refresh-token",
    )
    retire = MagicMock()
    monkeypatch.setattr(oauth_reset_module, "load_oauth_providers", lambda *_args: {})
    monkeypatch.setattr(oauth_reset_execution_module, "retire_mcp_oauth_request_session", retire)

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
    retire.assert_not_called()


@pytest.mark.asyncio
async def test_reset_oauth_connection_builds_link_before_deleting_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Link-generation failure must leave the approved credential unchanged."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    credentials_manager, worker_target, credentials = _save_test_credentials(
        context,
        worker_target,
        provider,
        "link-failure-refresh-token",
    )
    monkeypatch.setattr(
        oauth_reset_execution_module,
        "oauth_connect_url",
        MagicMock(side_effect=RuntimeError("state persistence unavailable")),
    )

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert result == "Error: OAuth connection reset did not complete; verify connection status, then retry."

    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        == credentials
    )


@pytest.mark.asyncio
async def test_reset_oauth_connection_mints_link_after_mcp_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect-token TTL must begin only after old MCP calls finish draining."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    _save_test_credentials(context, worker_target, provider, "ordered-refresh-token")
    order: list[str] = []

    @asynccontextmanager
    async def retirement(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        order.append("retirement_started")
        yield
        order.append("retirement_finished")

    def connect_url(*_args: object, **_kwargs: object) -> str:
        order.append("link_minted")
        return "https://example.test/connect"

    async def reset(*_args: object, **_kwargs: object) -> bool:
        order.append("credential_reset")
        return True

    monkeypatch.setattr(oauth_reset_execution_module, "retire_mcp_oauth_request_session", retirement)
    monkeypatch.setattr(oauth_reset_execution_module, "oauth_connect_url", connect_url)
    monkeypatch.setattr(oauth_reset_execution_module, "reset_oauth_credentials", reset)

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert "Error:" not in result
    assert order == ["retirement_started", "link_minted", "credential_reset", "retirement_finished"]


@pytest.mark.asyncio
async def test_reset_oauth_connection_preserves_credentials_when_mcp_teardown_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallible MCP cleanup must complete before destructive credential deletion."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    credentials_manager, worker_target, _credentials = _save_test_credentials(
        context,
        worker_target,
        provider,
        "mcp-close-failure-refresh-token",
    )

    @asynccontextmanager
    async def failed_retirement(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        message = "close failed"
        raise RuntimeError(message)
        yield

    monkeypatch.setattr(oauth_reset_execution_module, "retire_mcp_oauth_request_session", failed_retirement)

    with tool_runtime_context(context):
        result = await tool.reset_oauth_connection(provider.id)

    assert result == "Error: OAuth connection reset did not complete; verify connection status, then retry."
    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        is not None
    )


@pytest.mark.asyncio
async def test_reset_oauth_connection_cancellation_during_teardown_preserves_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation remains safe because teardown precedes the reset commit point."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    credentials_manager, worker_target, credentials = _save_test_credentials(
        context,
        worker_target,
        provider,
        "mcp-cancel-refresh-token",
    )
    teardown_started = asyncio.Event()
    hold_teardown = asyncio.Event()

    @asynccontextmanager
    async def blocked_teardown(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        teardown_started.set()
        await hold_teardown.wait()
        yield

    monkeypatch.setattr(
        oauth_reset_execution_module,
        "retire_mcp_oauth_request_session",
        blocked_teardown,
    )

    with tool_runtime_context(context):
        reset_task = asyncio.create_task(tool.reset_oauth_connection(provider.id))
        await teardown_started.wait()
        reset_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reset_task

    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        == credentials
    )


@pytest.mark.asyncio
async def test_reset_oauth_connection_cancellation_after_delete_returns_reconnect_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after the destructive commit must not hide the reconnect receipt."""
    tool, context, worker_target = _tool_and_context(tmp_path, worker_scope="user_agent")
    provider = google_drive_oauth_provider()
    credentials_manager, worker_target, _credentials = _save_test_credentials(
        context,
        worker_target,
        provider,
        "post-delete-cancel-refresh-token",
    )
    credential_deleted = threading.Event()
    release_delete = threading.Event()
    real_delete = credential_lifecycle.delete_scoped_credentials

    def blocked_delete(*args: object, **kwargs: object) -> None:
        real_delete(*args, **kwargs)
        credential_deleted.set()
        release_delete.wait()

    monkeypatch.setattr(credential_lifecycle, "delete_scoped_credentials", blocked_delete)

    with tool_runtime_context(context):
        reset_task = asyncio.create_task(tool.reset_oauth_connection(provider.id))
        await asyncio.to_thread(credential_deleted.wait)
        reset_task.cancel()
        release_delete.set()
        result = await reset_task

    assert "OAuth connection reset" in result
    assert "`connect_url`:" in result
    assert (
        load_scoped_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        is None
    )
