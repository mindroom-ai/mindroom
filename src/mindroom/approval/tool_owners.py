"""Restore recorded approval toolkit origins under current tool permissions, and bind approvals to them."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mindroom.authorization import is_sender_allowed_for_entity_replies_in_room
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.mcp.toolkit import require_mcp_server_manager
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.agent_tools import resolve_agent_tools, turn_room_flags

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.llm.runs import RunRequirement
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

# Fields of an agent's own entry that shape only its prompt and model, never the toolkits a prepared catalog built or
# their permissions. Every other field of the config, including any added later, counts as the catalog's authority.
_PROMPT_ONLY_AGENT_FIELDS = frozenset(
    {"display_name", "role", "instructions", "minimal_instructions", "model", "cheap_model", "markdown"},
)
# Private config state that records where the config came from or how agents were generated, never what they hold;
# every other private field, such as whether approved egress is enabled, counts as authority.
_PROVENANCE_PRIVATE_FIELDS = frozenset(
    {"_source_files", "_source_fingerprint", "_uses_includes", "_generated_agent_dumps"},
)
# Private config state kept per agent, of which only the catalog's own agent's entry counts.
_PER_AGENT_PRIVATE_FIELDS = frozenset({"_personal_agent_instances", "_generated_agents", "_personal_agent_call_agents"})


def validate_approval_tool_owners(
    owners: Mapping[tuple[str, str], str],
    calls: Sequence[ApprovalCall],
    requirements: Sequence[RunRequirement],
) -> None:
    """Refuse an approved call unless the waiting call it names still has its recorded function, arguments, member, and toolkit.

    ``owners`` are the rebuilt agent's, as ``require_original_toolkit`` reads them. The resume itself refuses decisions
    that do not cover the waiting calls exactly; this check binds each approval to the toolkit it was granted for.
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
        require_original_toolkit(owners, call)


def require_original_toolkit(owners: Mapping[tuple[str, str], str], call: ApprovalCall) -> None:
    """Refuse an approved ``call`` unless its agent still offers the function from the toolkit the approval recorded.

    ``owners`` maps ``(agent id, function name)`` to the authored toolkit the rebuilt agent offers the function from,
    as ``tool_owners_by_agent`` returns it, so a removed or replaced tool cannot run under an old approval.
    """
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
        worker_target=runtime.execution.worker_target,
        expected_config=config,
    )


async def required_approval_tool_names(
    agent_name: str,
    calls: Sequence[ApprovalCall],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    tools: ToolRegistry,
    execution_identity: ToolExecutionIdentity,
    delegation_depth: int,
) -> tuple[str, ...]:
    """Return the toolkits a saved approval recorded, after checking that the agent may still use each.

    ``tools`` is the registry the resumed run builds from, and ``delegation_depth`` the depth of the paused run, whose
    build offers the toolkits at that depth.
    """
    if not calls:
        return ()
    if any(call.invoking_agent != agent_name or not call.toolkit_name for call in calls):
        msg = "Saved approval has unknown toolkit ownership; retry the request"
        raise RuntimeError(msg)
    agent_tools = resolve_agent_tools(config, agent_name, tools)
    room_tools, matrix_room_tools = turn_room_flags(execution_identity)
    offered = agent_tools.candidates(
        depth=delegation_depth,
        room_tools=room_tools,
        matrix_room_tools=matrix_room_tools,
        portable_search=True,
    )
    required = sorted({call.toolkit_name for call in calls if call.toolkit_name is not None})
    for name in required:
        # A call names its authored entry; the toolkits a preset or an implied tool brings in share that name.
        owned = [entry for entry in offered if (entry.authored_name or entry.name) == name]
        if not owned:
            in_room = agent_tools.candidates(depth=delegation_depth, matrix_room_tools=True, portable_search=True)
            needs_room = any((entry.authored_name or entry.name) == name for entry in in_room)
            reason = "requires room context" if needs_room else "is no longer permitted"
            msg = f"Saved approval toolkit {name!r} {reason}; retry the request"
            raise RuntimeError(msg)
        for entry in owned:
            server_id = agent_tools.tools.mcp_server_id(entry.name)
            if server_id is None:
                continue
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


def _catalog_authority(config: Config, agent_name: str) -> dict[str, Any] | None:
    """Return what a prepared catalog's toolkits and permissions were built from, or None once the agent is gone.

    Other agents' and teams' entries are left out: a call re-authorizes its requester against the current config, and
    nothing an edit there changes reaches the toolkits this agent's catalog holds.
    """
    agent = config.agents.get(agent_name)
    if agent is None:
        return None
    private = config.__pydantic_private__ or {}
    dump = config.model_dump(exclude={"agents", "teams"})
    # Each onboarding adds the new user's agent to calls.agents; another generated agent's entry never reaches this one.
    dump["calls"]["agents"] = {
        name: profile
        for name, profile in dump["calls"]["agents"].items()
        if name == agent_name or config.personal_agent_instance(name) is None
    }
    return {
        **dump,
        "agent": agent.model_dump(exclude=set(_PROMPT_ONLY_AGENT_FIELDS)),
        "private": {
            name: value.get(agent_name) if name in _PER_AGENT_PRIVATE_FIELDS else value
            for name, value in private.items()
            if name not in _PROVENANCE_PRIVATE_FIELDS
        },
    }


def check_prepared_catalog_authority(context: ToolRuntimeContext) -> None:
    """Refuse a prepared CLI catalog once a reload changed what its agent's toolkits were built from.

    The catalog's toolkits were resolved from ``context.config``, while live reloads hand running turns the current
    config. Edits that leave the agent's own toolkits and permissions alone, such as another agent's instructions or
    this agent's own prompt, keep the catalog valid; any other edit refuses it.
    """
    current = context.current_config
    if current is context.config:
        return
    authority = _catalog_authority(current, context.agent_name)
    if authority is None or authority != _catalog_authority(context.config, context.agent_name):
        msg = "Current configuration no longer permits this prepared CLI call"
        raise PermissionError(msg)


async def authorize_prepared_tool_call(
    binding: PreparedAgentToolBinding,
    *,
    expected_worker_target: ResolvedWorkerTarget,
) -> None:
    """Recheck live or recovered CLI dispatch against its original prepared authority.

    Its toolkits must still be what the current config would give the agent (`check_prepared_catalog_authority`), so
    only live membership, worker routing, and function filters change. Recovery restores configured toolkits through
    ``required_approval_tool_names`` before rebuilding the agent.
    """
    catalog = binding.catalog
    context = catalog.runtime_context
    check_prepared_catalog_authority(context)
    config = context.current_config
    # The reserved namespace is assigned only to functions generated for this agent's
    # build. Never accept a binding the catalog does not own or use the namespace to
    # bypass current config/membership/worker checks.
    if binding.key.toolkit == "agent" and (
        not catalog.owns(binding)
        or binding.function.name != binding.key.function
        or context.agent_name not in config.agents
    ):
        msg = "Generated CLI function no longer belongs to its prepared Agent"
        raise PermissionError(msg)
    if (
        context.resolve_worker_target() != expected_worker_target
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
