"""Restore recorded approval toolkit origins under current tool permissions."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING

from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom.agno_compat_approval import append_denied_tool_result, before_tool_lookup
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.custom_tools.job import JobTools
from mindroom.mcp.registry import mcp_server_id_from_tool_name
from mindroom.mcp.toolkit import require_mcp_server_manager
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from agno.agent import Agent
    from agno.run.agent import RunOutput
    from agno.run.requirement import RunRequirement
    from agno.run.team import TeamRunOutput
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def toolkit_owners_for_agents(agents: Sequence[Agent]) -> dict[tuple[str, str], str | None]:
    """Observe final async toolkit functions with Agno's first-wins collision policy."""
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


def record_approval_denials(
    actor: Agent | Team,
    run: RunOutput | TeamRunOutput,
    calls: Sequence[ApprovalCall],
) -> None:
    """Consume exact denied IDs using Agno's native rejection result formatting."""
    denied = {call.tool_call_id: call for call in calls}
    run.messages = run.messages or []
    tools = [*(run.tools or ()), *(r.tool_execution for r in run.requirements or () if r.tool_execution)]
    for tool in tools:
        call = denied.get(tool.tool_call_id or "")
        if call is None or tool.confirmed is not False:
            continue
        if tool.tool_name != call.tool_name:
            msg = "Denied approval no longer matches the pending function; retry the request"
            raise RuntimeError(msg)
        if not any(message.tool_call_id == tool.tool_call_id for message in run.messages):
            append_denied_tool_result(actor, run.messages, tool, tool_name=call.tool_name)
        tool.requires_confirmation = False
        tool.tool_call_error = True


@contextmanager
def approval_denial_context(actor: Agent, calls_by_run: Mapping[str, Sequence[ApprovalCall]]) -> Iterator[None]:
    """Apply exact run denials while Agno owns this approval continuation.

    Agno resolves a Function even to reject a removed tool. It calls aget_tools
    after binding the canonical run and before copying messages. A team may continue
    several paused runs on one member, and retries may bind a run again, so keep
    the exact run map until the entire continuation exits. No placeholder enters
    the model tool surface.
    """
    if not any(calls_by_run.values()):
        yield
        return

    def apply_denials(run: RunOutput) -> None:
        if run.run_id is not None and (calls := calls_by_run.get(run.run_id)):
            record_approval_denials(actor, run, calls)

    with before_tool_lookup(actor, apply_denials):
        yield


def validate_approval_tool_owners(
    agents: Sequence[Agent],
    calls: Sequence[ApprovalCall],
    requirements: Sequence[RunRequirement],
) -> None:
    """Reject an approved call unless its final executable has the recorded owner."""
    owners = toolkit_owners_for_agents(agents)
    pending = {
        requirement.tool_execution.tool_call_id: requirement
        for requirement in requirements
        if requirement.confirmation is True and requirement.tool_execution is not None
    }
    if set(pending) != {call.tool_call_id for call in calls}:
        msg = "Saved approval calls no longer match the pending requirements; retry the request"
        raise RuntimeError(msg)
    for call in calls:
        requirement = pending[call.tool_call_id]
        tool = requirement.tool_execution
        if (
            tool is None
            or tool.tool_name != call.tool_name
            or (requirement.member_agent_id is not None and requirement.member_agent_id != call.invoking_agent)
        ):
            msg = "Saved approval function or member no longer matches the pending call; retry the request"
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
    """Restore only recorded toolkit origins, without changing session selection."""
    if not calls:
        return ()
    if any(call.invoking_agent != agent_name or not call.toolkit_name for call in calls):
        msg = "Saved approval has unknown toolkit ownership; retry the request"
        raise RuntimeError(msg)
    if any(call.toolkit_name == call.tool_name == "job" for call in calls) and not JobTools.available(
        runtime_paths,
        execution_identity,
        depth=0,
        enabled=background_tool_jobs_enabled(config, runtime_paths),
    ):
        msg = "Saved job controls are no longer available; retry the request"
        raise RuntimeError(msg)
    await asyncio.to_thread(ensure_tool_registry_loaded, runtime_paths, config)
    surface = visible_tool_surface(
        agent_name=agent_name,
        config=config,
        loaded_tools=[entry.name for entry in config.resolve_entity(agent_name).authored_deferred_tool_configs],
        include_matrix_room_runtime_tools=execution_identity.room_id is not None,
    )
    permitted = {entry.authored_name or entry.name: entry for entry in surface.runtime_tool_configs}
    required = sorted(
        {
            call.toolkit_name
            for call in calls
            if call.toolkit_name is not None and not call.toolkit_name == call.tool_name == "job"
        },
    )
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
