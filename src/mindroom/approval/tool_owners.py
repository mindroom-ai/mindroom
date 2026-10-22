"""Restore recorded approval toolkit origins under current tool permissions, and bind approvals to them."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom.authorization import is_sender_allowed_for_entity_replies_in_room
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.mcp.registry import mcp_server_id_from_tool_name
from mindroom.mcp.toolkit import require_mcp_server_manager
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agno.agent import Agent

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.llm.runs import RunRequirement
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity


def toolkit_owners_for_agents(agents: Sequence[Agent]) -> dict[tuple[str, str], str | None]:
    """Observe final async toolkit functions with Agno's first-wins collision policy.

    Tests that still build Agno agents read owners here; an owned agent build carries the same map as
    ``AgentBuild.tool_owners``.
    """
    owners: dict[tuple[str, str], str | None] = {}
    for agent in agents:
        if agent.id is None:
            continue
        for tool in agent.tools if isinstance(agent.tools, list) else ():
            if isinstance(tool, Toolkit):
                for name, function in tool.get_async_functions().items():
                    owners.setdefault((agent.id, name), function.owning_toolkit)
            elif isinstance(tool, Function):
                owners.setdefault((agent.id, tool.name), tool.owning_toolkit)
            elif callable(tool) and isinstance(name := getattr(tool, "__name__", None), str):
                owners.setdefault((agent.id, name), None)
    return owners


def validate_approval_tool_owners(
    owners: Mapping[tuple[str, str], str | None],
    calls: Sequence[ApprovalCall],
    requirements: Sequence[RunRequirement],
) -> None:
    """Refuse an approved call unless the waiting call it names still has its recorded function, arguments, member, and toolkit.

    ``owners`` maps ``(agent id, function name)`` to the authored toolkit the rebuilt agent offers the function from,
    as ``AgentBuild.tool_owners`` records it. The resume itself refuses decisions that do not cover the waiting calls
    exactly; this check binds each approval to the toolkit it was granted for.
    """
    pending = {
        requirement.tool_execution.tool_call_id: requirement
        for requirement in requirements
        if requirement.needs_confirmation
    }
    for call in calls:
        requirement = pending.get(call.tool_call_id)
        if requirement is None:
            msg = "Saved approval calls no longer match the pending requirements; retry the request"
            raise RuntimeError(msg)
        tool = requirement.tool_execution
        if (
            tool.tool_name != call.tool_name
            or not call.binds_arguments(tool.tool_args)
            or (requirement.member_agent_id is not None and requirement.member_agent_id != call.invoking_agent)
        ):
            msg = "Saved approval function, arguments, or member no longer match the pending call; retry the request"
            raise RuntimeError(msg)
        if call.toolkit_name is None or owners.get((call.invoking_agent, call.tool_name)) != call.toolkit_name:
            msg = f"Saved approval tool {call.tool_name!r} no longer has its original toolkit; retry the request"
            raise RuntimeError(msg)


async def _warm_approval_mcp_catalog(
    server_id: str,
    tool_name: str,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> None:
    """Warm one required catalog through normal agent credential routing."""
    manager = require_mcp_server_manager()
    if manager is None:
        msg = f"MCP tool {tool_name!r} is unavailable for approval continuation"
        raise RuntimeError(msg)
    runtime = await asyncio.to_thread(
        resolve_agent_runtime,
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    await manager.get_request_catalog(
        server_id,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=build_agent_toolkit_worker_target(
            runtime.execution.execution_scope,
            agent_name,
            is_private=runtime.execution.is_private,
            execution_identity=execution_identity,
            runtime_paths=runtime_paths,
        ),
        expected_config=config,
    )


async def required_approval_tool_names(
    agent_name: str,
    calls: Sequence[ApprovalCall],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> tuple[str, ...]:
    """Return the toolkits a saved approval recorded, after checking that the agent may still use each."""
    if not calls:
        return ()
    if any(call.invoking_agent != agent_name or not call.toolkit_name for call in calls):
        msg = "Saved approval has unknown toolkit ownership; retry the request"
        raise RuntimeError(msg)
    await asyncio.to_thread(ensure_tool_registry_loaded, runtime_paths, config)
    surface = visible_tool_surface(
        agent_name=agent_name,
        config=config,
        enable_dynamic_tools_manager=True,
        include_matrix_room_runtime_tools=execution_identity.room_id is not None,
    )
    permitted = {entry.authored_name or entry.name: entry for entry in surface.runtime_tool_configs}
    required = sorted({call.toolkit_name for call in calls if call.toolkit_name is not None})
    for name in required:
        entry = permitted.get(name)
        if entry is None:
            msg = f"Saved approval toolkit {name!r} is no longer permitted; retry the request"
            raise RuntimeError(msg)
        metadata = TOOL_METADATA.get(entry.name)
        if metadata is not None and metadata.requires_room_context and execution_identity.room_id is None:
            msg = f"Saved approval toolkit {name!r} requires room context; retry the request"
            raise RuntimeError(msg)
        server_id = mcp_server_id_from_tool_name(entry.name)
        if server_id is not None:
            server_config = config.mcp_servers.get(server_id)
            if server_config is None or not server_config.enabled:
                msg = f"Saved approval toolkit {name!r} is unavailable; retry the request"
                raise RuntimeError(msg)
            await _warm_approval_mcp_catalog(
                server_id,
                entry.name,
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            )
    return tuple(required)


async def authorize_prepared_tool_call(
    binding: PreparedAgentToolBinding,
    *,
    expected_worker_target: ResolvedWorkerTarget,
) -> None:
    """Recheck live or recovered CLI dispatch against its original prepared authority.

    The catalog's toolkits were resolved from ``context.config``. Requiring the
    current config to equal it keeps every config-derived toolkit assignment
    valid, so only live membership, worker routing, and function filters change.
    Recovery restores configured toolkits through ``required_approval_tool_names``
    before rebuilding the agent.
    """
    catalog = binding.catalog
    context = catalog.runtime_context
    config = context.current_config
    # The reserved namespace is assigned only to functions generated for this agent's
    # build. Never accept a binding the catalog does not own or use the namespace to
    # bypass current config/membership/worker checks.
    if binding.key.toolkit == "agent" and (
        not catalog.owns(binding)
        or catalog.agent_name != context.agent_name
        or binding.function.name != binding.key.function
        or context.agent_name not in config.agents
    ):
        msg = "Generated CLI function no longer belongs to its prepared Agent"
        raise PermissionError(msg)
    if (
        config != context.config
        or context.resolve_worker_target() != expected_worker_target
        or not is_sender_allowed_for_entity_replies_in_room(
            context.requester_id,
            (context.agent_name,),
            config,
            context.room_id,
            context.runtime_paths,
            context.require_agent_reply_memberships(),
            require_resolved_membership=True,
        )
        or (context.tool_function_filter is not None and not context.tool_function_filter(binding.function))
    ):
        msg = "Current authorization no longer permits this prepared CLI call"
        raise PermissionError(msg)
