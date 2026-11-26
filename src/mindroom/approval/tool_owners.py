"""Restore recorded approval toolkit origins under current tool permissions, and bind approvals to them."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.approval.tool_approval import approval_policy_names, effective_tool_approval_policy
from mindroom.authorization import is_sender_allowed_for_entity_replies_in_room
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.desktop.credentials import has_ready_self_service_desktop
from mindroom.mcp.toolkit import require_mcp_server_manager
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.agent_tools import resolve_agent_tools, turn_keeps_room_tools
from mindroom.tool_system.plugins import config_plugins

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.llm.runs import RunRequirement
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_access import ToolKey
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

# Fields of an agent's own entry that shape only its prompt and model, never the toolkits a prepared catalog built or
# their permissions. Every other field of the entry, and any config section `_catalog_authority` does not name,
# including one added later, counts as the catalog's authority.
_PROMPT_ONLY_AGENT_FIELDS = frozenset(
    {"display_name", "role", "instructions", "minimal_instructions", "model", "cheap_model", "markdown"},
)
# Private config state that records where the config came from or how agents were generated, never what they hold;
# every other private field, such as whether approved egress is enabled, counts as authority.
_PROVENANCE_PRIVATE_FIELDS = frozenset(
    {
        "_source_files",
        "_source_fingerprint",
        "_uses_includes",
        "_generated_agent_dumps",
        "_personal_agent_call_templates",
        "_personal_agent_call_agents",
        "_personal_agents_added_agents_section",
    },
)
# Private config state kept per agent, of which only the catalog's own agent's entry counts.
_PER_AGENT_PRIVATE_FIELDS = frozenset({"_personal_agent_instances", "_generated_agents"})


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
    runtime_toolkits: Collection[str] = (),
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
    offered = agent_tools.candidates(
        depth=delegation_depth,
        room_tools=turn_keeps_room_tools(execution_identity),
        portable_search=True,
    )
    required = sorted({call.toolkit_name for call in calls if call.toolkit_name is not None})
    for name in required:
        if name in runtime_toolkits:
            if agent_name not in config.agents:
                msg = f"Saved approval toolkit {name!r} is no longer permitted; retry the request"
                raise RuntimeError(msg)
            continue
        # A call names its authored entry; the toolkits a preset or an implied tool brings in share that name.
        owned = [entry for entry in offered if (entry.authored_name or entry.name) == name]
        if (
            not owned
            and name == "desktop"
            and "desktop" not in agent_tools.facts.disabled
            and turn_keeps_room_tools(execution_identity)
            and agent_tools.tools.available("desktop")
            and has_ready_self_service_desktop(config, runtime_paths, agent_name, execution_identity.requester_id)
        ):
            continue
        if not owned:
            in_room = agent_tools.candidates(depth=delegation_depth, portable_search=True)
            needs_room = any((entry.authored_name or entry.name) == name for entry in in_room)
            reason = "requires room context" if needs_room else "is no longer permitted"
            msg = f"Saved approval toolkit {name!r} {reason}; retry the request"
            raise RuntimeError(msg)
        await _warm_owned_mcp_servers(
            tuple(entry.name for entry in owned),
            toolkit_name=name,
            agent_name=agent_name,
            tools=agent_tools.tools,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
    return tuple(required)


async def _warm_owned_mcp_servers(
    entry_names: Sequence[str],
    *,
    toolkit_name: str,
    agent_name: str,
    tools: ToolRegistry,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> None:
    for entry_name in entry_names:
        server_id = tools.mcp_server_id(entry_name)
        if server_id is None:
            continue
        server_config = config.mcp_servers.get(server_id)
        if server_config is None or not server_config.enabled:
            msg = f"Saved approval toolkit {toolkit_name!r} is unavailable; retry the request"
            raise RuntimeError(msg)
        await _warm_approval_mcp_catalog(
            server_id,
            entry_name,
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )


def _catalog_authority(config: Config, agent_name: str) -> dict[str, Any] | None:
    """Return the config inputs this agent's catalog depends on, or None once the agent is gone.

    Other agents' and teams' entries are excluded; calls re-authorize the requester live.
    ``models``, ``room_models``, ``room_thread_summary_models``, ``defaults.thread_summary_model``, and
    ``router.model`` pick models only. ``prompts`` shapes prompt text only, and ``debug.log_llm_requests`` is read
    live when recording tool calls. The agent's own ``rooms`` are checked as a subset on reload. ``calls``,
    ``voice``, ``matrix_space``, ``server_emoji``, ``event_journal``, ``slack``, ``tailnet_ssh``, and
    ``scheduler_catch_up_grace_seconds`` configure no prepared CLI tool or its permissions (runtime services,
    ingress, or gateway code read them; per-agent ``slack`` tool overrides and ``tailnet_ssh`` opt-ins stay on the
    agent entry). ``personal_agents`` keeps only this agent's own template; ``tool_approval`` is checked per called
    key.
    """
    agent = config.agents.get(agent_name)
    if agent is None:
        return None
    private = config.__pydantic_private__ or {}
    dump = config.model_dump(
        exclude={
            "agents": True,
            "teams": True,
            "models": True,
            "room_models": True,
            "room_thread_summary_models": True,
            "debug": True,
            "prompts": True,
            "voice": True,
            "calls": True,
            "matrix_space": True,
            "server_emoji": True,
            "event_journal": True,
            "slack": True,
            "tailnet_ssh": True,
            "scheduler_catch_up_grace_seconds": True,
            "personal_agents": True,
            "tool_approval": True,
            "defaults": {"thread_summary_model"},
            "router": {"model"},
        },
    )
    personal_instance = config.personal_agent_instance(agent_name)
    own_template = config.personal_agents.get(personal_instance.template) if personal_instance is not None else None
    return {
        **dump,
        "agent": agent.model_dump(exclude=set(_PROMPT_ONLY_AGENT_FIELDS) | {"rooms"}),
        "personal_agent_template": own_template.model_dump() if own_template is not None else None,
        "private": {
            name: value.get(agent_name) if name in _PER_AGENT_PRIVATE_FIELDS else value
            for name, value in private.items()
            if name not in _PROVENANCE_PRIVATE_FIELDS
        },
    }


def _call_approval_authority(config: Config, tools: ToolRegistry, key: ToolKey) -> tuple[Any, ...]:
    registered_toolkit = None if key.toolkit == "agent" else key.toolkit
    names = (SHELL_FUNCTION, *approval_policy_names(config, tools, registered_toolkit, key.function))
    return (
        config.tool_approval.model_dump(exclude={"rules"}),
        *((name, effective_tool_approval_policy(config, name)) for name in names),
    )


def check_prepared_catalog_authority(context: ToolRuntimeContext, *, key: ToolKey) -> None:
    """Refuse a prepared CLI catalog if a reload changed its authority, removed a room, or changed the key's approval."""
    current = context.current_config
    if current is context.config:
        return
    old_agent = context.config.agents.get(context.agent_name)
    current_agent = current.agents.get(context.agent_name)
    if (
        old_agent is None
        or current_agent is None
        or not set(old_agent.rooms) <= set(current_agent.rooms)
        or _call_approval_authority(current, config_plugins(current, context.runtime_paths).tools, key)
        != _call_approval_authority(context.config, context.plugins.tools, key)
        or _catalog_authority(current, context.agent_name) != _catalog_authority(context.config, context.agent_name)
    ):
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
    check_prepared_catalog_authority(context, key=binding.key)
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
