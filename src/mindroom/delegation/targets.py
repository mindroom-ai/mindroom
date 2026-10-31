"""Turn a ``run_subagent`` or ``continue_subagent`` call into the child it starts or continues, for both adapters.

The direct adapter runs the child inside the caller's tool call and the native driver runs it durably, but both read
a call's arguments the same way; authorizing the resolved target stays with each adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.delegation.lifecycle import live_delegation_config
from mindroom.delegation.personas import (
    caller_knowledge_names,
    caller_skill_names,
    caller_toolkit_names,
    follow_up_refusal,
    resolve_persona_request,
)
from mindroom.delegation.recovery import resolve_subagent
from mindroom.delegation.sessions import SubagentSessionError
from mindroom.delegation.state import CONTINUE_SUBAGENT
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.agent_modes import AgentMode
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild, SubagentPersona
    from mindroom.llm.runs import ToolExecution
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class DelegationTarget:
    """Validated destination and task for a fresh or continued subagent."""

    agent_name: str
    task: str
    previous_child: DelegationChild | None = None
    agent_mode: AgentMode = "standard"
    persona: SubagentPersona | None = None
    model: str | None = None


async def resolve_delegation_target(
    tool: ToolExecution,
    retained: DelegationChild | None,
    *,
    caller_identity: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> DelegationTarget | str:
    """Resolve a delegation call the native driver runs, as ``caller_identity`` makes it, or return a refusal.

    ``retained`` is the child a resumed call already started, which keeps what it was started with. A fresh child's
    profile and skills come from the caller's workspace, and the persona of the authored child making the call, which
    its tool context carries, caps the copies it authors.
    """
    args = tool.tool_args or {}
    if tool.tool_name == CONTINUE_SUBAGENT:
        return await resolve_follow_up_target(
            args,
            retained,
            owner=caller_identity,
            config=config,
            runtime_paths=runtime_paths,
            depth=depth,
        )

    def caller_workspace_root() -> Path:
        return resolve_agent_runtime(
            caller_identity.agent_name,
            config,
            runtime_paths,
            execution_identity=caller_identity,
        ).workspace.root

    context = get_tool_runtime_context()
    return resolve_fresh_target(
        args,
        retained,
        caller_name=caller_identity.agent_name,
        config=config,
        runtime_paths=runtime_paths,
        depth=depth,
        workspace_root=caller_workspace_root,
        cap=context.persona if context is not None else None,
    )


async def resolve_follow_up_target(
    args: Mapping[str, object],
    retained: DelegationChild | None,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> DelegationTarget | str:
    """Resolve a follow-up to the child ``owner`` continues, which keeps its model, mode, and persona."""
    subagent_id, task = args.get("subagent_id"), args.get("message")
    if not isinstance(subagent_id, str) or not isinstance(task, str):
        return "Cannot continue: subagent_id and message must be strings."
    if retained is not None:
        # A resumed call continues the child it started, whatever the caller's tools are now.
        if retained.subagent_id != subagent_id:
            msg = "Subagent ID no longer matches its retained requirement"
            raise RuntimeError(msg)
        return DelegationTarget(retained.child_agent_name, task, previous_child=retained)
    try:
        previous_child = await resolve_subagent(
            subagent_id,
            owner=owner,
            config=config,
            runtime_paths=runtime_paths,
            depth=depth,
        )
    except SubagentSessionError as error:
        return str(error)
    live_config = live_delegation_config(config)
    refusal = follow_up_refusal(
        previous_child.persona,
        owner.agent_name,
        live_config,
        config_plugins(live_config, runtime_paths).tools,
        delegation_depth=depth,
    )
    if refusal is not None:
        return refusal
    return DelegationTarget(previous_child.child_agent_name, task, previous_child=previous_child)


def resolve_fresh_target(
    args: Mapping[str, object],
    retained: DelegationChild | None,
    *,
    caller_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
    workspace_root: Callable[[], Path],
    cap: SubagentPersona | None,
) -> DelegationTarget | str:
    """Resolve a fresh child and any persona it authors; a started child keeps its frozen persona.

    ``workspace_root`` returns the caller's workspace, resolved only to read a profile or the caller's skills, and
    ``cap`` is the persona of the authored subagent making the call, if any.
    """
    child_name, task, minimal = args.get("agent_name"), args.get("task"), args.get("minimal")
    model = cast("str | None", args.get("model"))
    if child_name is None:
        child_name = caller_name
    if minimal is not None and not isinstance(minimal, bool):
        return "Cannot delegate: minimal must be a boolean or null."
    if not isinstance(child_name, str) or not isinstance(task, str):
        return "Cannot delegate: task must be a string and agent_name must be a string or null."
    if retained is not None:
        # A resumed call never rereads a profile the worker could have changed.
        return DelegationTarget(child_name, task, model=model)
    request = resolve_persona_request(
        caller_name=caller_name,
        agent_name=child_name,
        system_prompt=args.get("system_prompt"),
        tools=args.get("tools"),
        skills=args.get("skills"),
        knowledge=args.get("knowledge"),
        profile=args.get("profile"),
        model=model,
        minimal=bool(minimal),
        workspace_root=workspace_root,
        # The copy runs one level deeper, where delegate may no longer be offered.
        available_toolkits=lambda: caller_toolkit_names(
            caller_name,
            (live_config := live_delegation_config(config)),
            config_plugins(live_config, runtime_paths).tools,
            delegation_depth=depth + 1,
        ),
        available_skills=lambda: caller_skill_names(
            caller_name,
            live_delegation_config(config),
            runtime_paths,
            workspace_root(),
        ),
        available_knowledge=lambda: caller_knowledge_names(caller_name, live_delegation_config(config)),
        cap=cap,
    )
    if isinstance(request, str):
        return request
    return DelegationTarget(
        child_name,
        task,
        agent_mode=request.agent_mode,
        persona=request.persona,
        model=request.model,
    )
