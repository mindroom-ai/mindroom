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
from mindroom.delegation.recovery import load_settled_subagent
from mindroom.delegation.sessions import SUBAGENT_PREFIX_PATTERN, SubagentSessionError, resolve_subagent_id
from mindroom.delegation.state import CONTINUE_SUBAGENT, display_title
from mindroom.delegation.workers import (
    dedicated_worker_refusal,
    parse_delegation_sandbox,
    sandbox_persona_refusal,
    validate_sandbox_child,
)
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
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workers.models import ScriptResourceProfileName, WorkerSandboxConfig


@dataclass(frozen=True)
class DelegationTarget:
    """Validated destination and task for a fresh or continued subagent."""

    agent_name: str
    task: str
    previous_child: DelegationChild | None = None
    # What people watching the run see it as (``display_title``); empty when the call named none.
    title: str = ""
    agent_mode: AgentMode = "standard"
    persona: SubagentPersona | None = None
    model: str | None = None
    dedicated_worker: bool = False
    resource_profile: ScriptResourceProfileName | None = None
    sandbox: WorkerSandboxConfig | None = None


@dataclass(frozen=True)
class _FollowUpLookup:
    """Outcome of looking up a follow-up subagent: resolved target, missing session, or policy/sandbox refusal."""

    target: DelegationTarget | None = None
    refusal: str | None = None
    unavailable: bool = False


def minimal_mode_refusal(
    agent_mode: AgentMode,
    *,
    execution_identity: ToolExecutionIdentity | None,
    runtime_context: ToolRuntimeContext | None,
) -> str | None:
    """Refuse minimal-mode subagents outside an active Matrix tool runtime context."""
    if agent_mode != "minimal":
        return None
    if runtime_context is None or (execution_identity is not None and execution_identity.channel != "matrix"):
        return "Cannot delegate: minimal mode requires an active Matrix conversation context; omit minimal=true."
    return None


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
    context = get_tool_runtime_context()
    if tool.tool_name == CONTINUE_SUBAGENT:
        resolved = await resolve_follow_up_target(
            args,
            retained=retained,
            owner=caller_identity,
            config=config,
            runtime_paths=runtime_paths,
            depth=depth,
        )
    else:

        def caller_workspace_root() -> Path:
            return resolve_agent_runtime(
                caller_identity.agent_name,
                config,
                runtime_paths,
                execution_identity=caller_identity,
            ).workspace.root

        resolved = resolve_fresh_target(
            args,
            retained=retained,
            caller_name=caller_identity.agent_name,
            config=config,
            runtime_paths=runtime_paths,
            depth=depth,
            workspace_root=caller_workspace_root,
            cap=context.persona if context is not None else None,
        )
    if isinstance(resolved, str) or retained is not None:
        return resolved
    effective_mode = resolved.previous_child.agent_mode if resolved.previous_child is not None else resolved.agent_mode
    return minimal_mode_refusal(effective_mode, execution_identity=caller_identity, runtime_context=context) or resolved


async def lookup_follow_up_target(
    args: Mapping[str, object],
    *,
    retained: DelegationChild | None = None,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> _FollowUpLookup:
    """Resolve a follow-up child while distinguishing unavailable session files from permission/sandbox refusals."""
    subagent_id, task = args.get("subagent_id"), args.get("message")
    if not isinstance(subagent_id, str) or not isinstance(task, str):
        return _FollowUpLookup(refusal="Cannot continue: subagent_id and message must be strings.")
    if (previous_child := retained) is not None:
        # A resumed call continues the child it started, whatever the caller's tools are now.
        if previous_child.subagent_id != subagent_id and not (
            SUBAGENT_PREFIX_PATTERN.fullmatch(subagent_id) and previous_child.subagent_id.startswith(subagent_id)
        ):
            msg = "Subagent ID no longer matches its retained requirement"
            raise RuntimeError(msg)
    else:
        try:
            resolved_id = await resolve_subagent_id(subagent_id, owner=owner, runtime_paths=runtime_paths, depth=depth)
            previous_child = await load_settled_subagent(
                resolved_id,
                owner=owner,
                config=config,
                runtime_paths=runtime_paths,
                depth=depth,
            )
        except SubagentSessionError as error:
            return _FollowUpLookup(refusal=str(error), unavailable=error.unavailable)
        live_config = live_delegation_config(config)
        refusal = follow_up_refusal(
            previous_child.persona,
            owner.agent_name,
            live_config,
            config_plugins(live_config, runtime_paths).tools,
            delegation_depth=depth,
        ) or validate_sandbox_child(
            previous_child,
            live_config,
            runtime_paths,
            caller_name=owner.agent_name,
            # Full validation runs on the authorized turn, not while resolving its handle.
            check_mount=False,
        )
        if refusal is not None:
            return _FollowUpLookup(refusal=refusal)
    return _FollowUpLookup(
        target=DelegationTarget(
            previous_child.child_agent_name,
            task,
            previous_child=previous_child,
            dedicated_worker=previous_child.dedicated_worker,
            resource_profile=previous_child.worker_resource_profile,
            sandbox=previous_child.worker_sandbox,
        ),
    )


async def resolve_follow_up_target(
    args: Mapping[str, object],
    *,
    retained: DelegationChild | None = None,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> DelegationTarget | str:
    """Resolve a follow-up to the child ``owner`` continues, which keeps its model, mode, persona, and sandbox."""
    lookup = await lookup_follow_up_target(
        args,
        retained=retained,
        owner=owner,
        config=config,
        runtime_paths=runtime_paths,
        depth=depth,
    )
    if lookup.target is not None:
        return lookup.target
    assert lookup.refusal is not None
    return lookup.refusal


def resolve_fresh_target(
    args: Mapping[str, object],
    *,
    retained: DelegationChild | None = None,
    caller_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
    workspace_root: Callable[[], Path],
    cap: SubagentPersona | None,
) -> DelegationTarget | str:
    """Resolve a fresh child and any persona it authors; a started child keeps its frozen persona.

    ``workspace_root`` returns the caller's workspace, resolved only to read a profile or the caller's skills, and
    ``cap`` is the persona of the authored subagent making the call, if any. On-disk ``mount_dir`` existence is
    intentionally not checked here; callers validate it via ``validate_sandbox_target`` after authorization.
    """
    child_name, task, minimal = args.get("agent_name"), args.get("task"), args.get("minimal")
    model, title = cast("str | None", args.get("model")), args.get("title")
    if child_name is None:
        child_name = caller_name
    if (refusal := _option_refusal(minimal, title)) or not isinstance(child_name, str) or not isinstance(task, str):
        return refusal or "Cannot delegate: task must be a string and agent_name must be a string or null."
    parsed = parse_delegation_sandbox(args, runtime_paths=runtime_paths)
    if isinstance(parsed, str):
        return parsed
    dedicated_worker, resource_profile, sandbox = parsed
    if retained is not None:
        # A resumed call never rereads a profile the worker could have changed, while preserving its request placement.
        return DelegationTarget(
            child_name,
            task,
            model=model,
            dedicated_worker=dedicated_worker,
            resource_profile=resource_profile,
            sandbox=sandbox,
        )
    if dedicated_worker and (
        refusal := dedicated_worker_refusal(
            caller_name,
            live_delegation_config(config),
            runtime_paths,
            resource_profile=resource_profile,
            sandbox=sandbox,
            child_name=child_name,
        )
    ):
        return refusal
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
    refusal = (
        request
        if isinstance(request, str)
        else (sandbox_persona_refusal(request.persona, request.agent_mode) if sandbox is not None else None)
    )
    if refusal is not None:
        return refusal
    assert not isinstance(request, str)
    return DelegationTarget(
        child_name,
        task,
        # Stored, copied to follow-ups, and shown as the same short value.
        title=display_title(title),
        agent_mode=request.agent_mode,
        persona=request.persona,
        model=request.model,
        dedicated_worker=dedicated_worker,
        resource_profile=resource_profile,
        sandbox=sandbox,
    )


def _option_refusal(minimal: object, title: object) -> str | None:
    """Say why a call's ``minimal`` or ``title`` is unusable; a call stored before the title, or a direct one, has none."""
    if minimal is not None and not isinstance(minimal, bool):
        return "Cannot delegate: minimal must be a boolean or null."
    if title is not None and (not isinstance(title, str) or not title.strip()):
        return "Cannot delegate: title must name the task in a few words."
    return None
