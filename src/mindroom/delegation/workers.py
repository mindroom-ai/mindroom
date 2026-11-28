"""Whether a delegated subagent may run its worker-routed tool calls in a dedicated or sandboxed worker."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.approval.tool_approval import tool_may_require_approval
from mindroom.background_tasks import create_background_task
from mindroom.delegation.state import child_execution_identity
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.agent_tools import AgentTools, turn_keeps_room_tools
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.worker_routing import subagent_worker_key, worker_log_id
from mindroom.workers.backend import RetiringWorkerBackend, WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workers.backends.kubernetes_config import configured_named_workers_max, configured_runtime_class_name
from mindroom.workers.models import SANDBOX_ALLOWED_TOOLKITS, SCRIPT_RESOURCE_PROFILE_NAMES, WorkerSandboxConfig
from mindroom.workers.placements import DedicatedWorkerPlacement
from mindroom.workers.runtime import idle_worker_cleanup_runs, lease_configured_primary_worker_manager
from mindroom.workers.sandbox_mounts import (
    normalize_sandbox_mount_dir,
    parse_sandbox_config,
    resolve_worker_sandbox_mount,
    sandbox_mount_dir_runtime_class_refusal,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.agent_modes import AgentMode
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild, SubagentPersona
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workers.models import ScriptResourceProfileName

logger = get_logger(__name__)

__all__ = [
    "SANDBOX_ALLOWED_TOOLKITS",
    "SandboxedSubagentUnavailableError",
    "child_placement",
    "dedicated_worker_refusal",
    "parse_delegation_sandbox",
    "resource_profiles_available",
    "retire_subagent_worker",
    "sandbox_persona_refusal",
    "sandbox_workers_available",
    "schedule_subagent_worker_retirement",
    "subagent_sandbox_worker_key",
    "validate_sandbox_child",
    "validate_sandbox_execution",
]


class SandboxedSubagentUnavailableError(RuntimeError):
    """Raised when a sandboxed subagent can no longer run in a valid Kubernetes sandbox."""


def _dedicated_workers_available(runtime_paths: RuntimePaths) -> bool:
    """Return whether this runtime can give a subagent a worker of its own.

    Each such worker serves one subagent, so they are only offered where idle cleanup removes them again.
    """
    return worker_backend_kind(runtime_paths).dedicated and idle_worker_cleanup_runs(runtime_paths)


def resource_profiles_available(runtime_paths: RuntimePaths) -> bool:
    """Return whether a subagent's own worker can be given a resource profile, which only Kubernetes enforces.

    It asks only for the backend; callers first check that this runtime offers dedicated workers at all.
    """
    return worker_backend_kind(runtime_paths) is WorkerBackendKind.KUBERNETES


def sandbox_workers_available(caller_name: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Return whether ``caller_name`` may launch sandboxed Kubernetes subagents in this runtime."""
    return (
        dedicated_worker_refusal(caller_name, config, runtime_paths, resource_profile=None) is None
        and resource_profiles_available(runtime_paths)
        and configured_named_workers_max(runtime_paths) >= 1
    )


def parse_delegation_sandbox(
    args: Mapping[str, object],
    *,
    runtime_paths: RuntimePaths,
) -> tuple[bool, ScriptResourceProfileName | None, WorkerSandboxConfig | None] | str:
    """Parse ``dedicated_worker``/``resource_profile``/sandbox options without touching disk or target config."""
    dedicated_worker = args.get("dedicated_worker")
    resource_profile = args.get("resource_profile")
    if dedicated_worker is not None and not isinstance(dedicated_worker, bool):
        return "Cannot delegate: dedicated_worker must be a boolean or null."
    try:
        sandbox_config, _ = parse_sandbox_config(
            args.get("sandbox"),
            args.get("mount_dir"),
            args.get("mount_read_only"),
            runtime_class_name=configured_runtime_class_name(runtime_paths),
        )
    except ValueError as exc:
        return f"Cannot delegate: {exc}"
    if sandbox_config is not None:
        if dedicated_worker is False:
            return "Cannot delegate: sandbox requires dedicated_worker=true."
        effective_dedicated = True
    else:
        if resource_profile is not None and not dedicated_worker:
            return "Cannot delegate: resource_profile requires dedicated_worker=true."
        effective_dedicated = bool(dedicated_worker)
    if resource_profile is not None and (
        not isinstance(resource_profile, str) or resource_profile not in SCRIPT_RESOURCE_PROFILE_NAMES
    ):
        return "Cannot delegate: resource_profile must be small, standard, or large."
    return effective_dedicated, resource_profile, sandbox_config


def sandbox_persona_refusal(persona: SubagentPersona | None, agent_mode: AgentMode) -> str | None:
    """Refuse persona tools, skills, or knowledge outside the sandbox allowlist (pure, no disk I/O)."""
    if persona is None:
        return None
    if persona.skills:
        return "Cannot delegate: skills are not available in sandbox mode."
    if persona.knowledge:
        return "Cannot delegate: knowledge is not available in sandbox mode."
    allowed = frozenset({"shell"}) if agent_mode == "minimal" else SANDBOX_ALLOWED_TOOLKITS
    for entry in persona.tools or ():
        toolkit = entry.partition(".")[0]
        if toolkit not in allowed:
            permitted = ", ".join(sorted(allowed))
            return f"Cannot delegate: tool '{entry}' is not allowed in sandbox mode. Allowed toolkits: {permitted}."
    return None


def _sandbox_config_refusal(
    caller_name: str,
    runtime_paths: RuntimePaths,
    sandbox: WorkerSandboxConfig,
    *,
    child_name: str | None,
) -> str | None:
    if worker_backend_kind(runtime_paths) is not WorkerBackendKind.KUBERNETES or not idle_worker_cleanup_runs(
        runtime_paths,
    ):
        return (
            "Cannot delegate: sandboxed subagents require the kubernetes worker backend and idle-worker cleanup, "
            "which the API server runs when MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS is above 0."
        )
    if configured_named_workers_max(runtime_paths) < 1:
        return "Cannot delegate: sandboxed subagents are disabled (named_workers_max is 0)."
    if sandbox.mount_dir is None:
        return "Cannot delegate: mount_read_only requires mount_dir." if sandbox.mount_read_only else None
    if child_name is not None and child_name != caller_name:
        return (
            "Cannot delegate: mount_dir is only supported on self-copy subagents (omit agent_name or select yourself)."
        )
    try:
        normalize_sandbox_mount_dir(sandbox.mount_dir)
    except ValueError as exc:
        return f"Cannot delegate: Invalid mount_dir: {exc}"
    runtime_class_name = configured_runtime_class_name(runtime_paths)
    return (
        None
        if runtime_class_name is None
        else f"Cannot delegate: {sandbox_mount_dir_runtime_class_refusal(runtime_class_name)}"
    )


def dedicated_worker_refusal(
    caller_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    resource_profile: object,
    sandbox: WorkerSandboxConfig | None = None,
    child_name: str | None = None,
) -> str | None:
    """Return why ``caller_name`` cannot run a subagent in a dedicated/sandboxed worker, or None."""
    caller = config.agents.get(caller_name)
    if caller is None or not caller.dedicated_subagent_workers:
        return f"Cannot delegate: {caller_name} may not run subagents in dedicated workers."
    if not _dedicated_workers_available(runtime_paths):
        return (
            "Cannot delegate: dedicated workers need the docker or kubernetes worker backend and idle-worker "
            "cleanup, which the API server runs when MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS is above 0."
        )
    if resource_profile is not None:
        if not isinstance(resource_profile, str) or resource_profile not in SCRIPT_RESOURCE_PROFILE_NAMES:
            return "Cannot delegate: resource_profile must be small, standard, or large."
        if not resource_profiles_available(runtime_paths):
            return "Cannot delegate: resource_profile requires the kubernetes worker backend."
    if sandbox is not None:
        return _sandbox_config_refusal(caller_name, runtime_paths, sandbox, child_name=child_name)
    return None


def _sandbox_target_tools_refusal(
    child_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    *,
    agent_mode: AgentMode,
    persona: SubagentPersona | None,
    delegation_depth: int,
) -> str | None:
    tools = config_plugins(config, runtime_paths).tools
    agent_tools = AgentTools(config=config, facts=config.resolve_agent(child_name).tools, tools=tools)
    placement = tool_placement(runtime_paths, tools)
    allowed = frozenset({"shell"}) if agent_mode == "minimal" else SANDBOX_ALLOWED_TOOLKITS
    if persona is not None and persona.tools is not None:
        allowed &= {entry.partition(".")[0] for entry in persona.tools}
    candidates = agent_tools.candidates(depth=delegation_depth, room_tools=turn_keeps_room_tools(execution_identity))
    routed_allowed = {
        entry.name
        for entry in candidates
        if entry.name in allowed and agent_tools.runs_in_worker(entry.name, placement)
    }
    if agent_mode == "minimal":
        if "shell" not in routed_allowed:
            return f"Cannot delegate: minimal sandboxed subagent '{child_name}' requires a worker-routed shell tool."
        if tool_may_require_approval(config, SHELL_FUNCTION, agent_name=child_name):
            return f"Cannot delegate: minimal sandboxed subagent '{child_name}' cannot use approval-gated shell."
        return None
    if not routed_allowed:
        return f"Cannot delegate: target agent '{child_name}' has no worker-routed sandbox tools available."
    return None


def validate_sandbox_execution(
    *,
    caller_name: str,
    child_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    sandbox: WorkerSandboxConfig,
    resource_profile: ScriptResourceProfileName | None = None,
    agent_mode: AgentMode = "standard",
    persona: SubagentPersona | None = None,
    delegation_depth: int = 1,
) -> str | None:
    """Validate target scope, worker-routed sandbox tools, and on-disk ``mount_dir`` AFTER authorization."""
    pre_refusal = (
        dedicated_worker_refusal(
            caller_name,
            config,
            runtime_paths,
            resource_profile=resource_profile,
            sandbox=sandbox,
            child_name=child_name,
        )
        or sandbox_persona_refusal(persona, agent_mode)
        or (f"Cannot delegate: unknown target agent '{child_name}'." if child_name not in config.agents else None)
    )
    if pre_refusal is not None:
        return pre_refusal
    try:
        execution = resolve_agent_execution(child_name, config, runtime_paths, execution_identity=execution_identity)
    except ValueError as exc:
        return f"Cannot delegate: {exc}"
    worker_target = execution.worker_target
    if execution.execution_scope is None or worker_target.worker_key is None:
        return "Cannot delegate: sandboxed subagents require an agent with a worker execution scope."
    if tools_refusal := _sandbox_target_tools_refusal(
        child_name,
        config,
        runtime_paths,
        execution_identity,
        agent_mode=agent_mode,
        persona=persona,
        delegation_depth=delegation_depth,
    ):
        return tools_refusal
    return _sandbox_mount_refusal(
        runtime_paths=runtime_paths,
        worker_key=worker_target.worker_key,
        private_agent_names=worker_target.private_agent_names,
        config=config,
        sandbox=sandbox,
    )


def _sandbox_mount_refusal(
    *,
    runtime_paths: RuntimePaths,
    worker_key: str,
    private_agent_names: frozenset[str] | None,
    config: Config,
    sandbox: WorkerSandboxConfig,
) -> str | None:
    if sandbox.mount_dir is None:
        return None
    try:
        resolve_worker_sandbox_mount(
            storage_root=runtime_paths.storage_root,
            worker_key=worker_key,
            private_agent_names=private_agent_names,
            config=config,
            sandbox=sandbox,
            validate=True,
        )
    except ValueError as exc:
        return f"Cannot delegate: {exc}"
    return None


def validate_sandbox_child(
    child: DelegationChild,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    caller_name: str | None = None,
) -> str | None:
    """Re-validate an existing sandboxed ``DelegationChild`` fail-closed; returns None for unsandboxed children."""
    if child.worker_sandbox is None:
        return None
    if not child.dedicated_worker:
        return "Cannot run sandboxed subagent without dedicated_worker placement."
    return validate_sandbox_execution(
        caller_name=caller_name or child.caller_agent_name,
        child_name=child.child_agent_name,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=child_execution_identity(child),
        sandbox=child.worker_sandbox,
        resource_profile=child.worker_resource_profile,
        agent_mode=child.agent_mode,
        persona=child.persona,
        delegation_depth=child.depth,
    )


def child_placement(
    child: DelegationChild,
    config: Config,
    runtime_paths: RuntimePaths,
) -> DedicatedWorkerPlacement | None:
    """Return the worker placement for one attempt of ``child``, failing closed for sandboxed children.

    Non-sandboxed dedicated children recheck caller permission and backend and fall back to their normal worker when
    no longer eligible. Sandboxed children NEVER fall back: any missing requirement raises
    ``SandboxedSubagentUnavailableError``.
    """
    if child.worker_sandbox is not None:
        if (refusal := validate_sandbox_child(child, config, runtime_paths)) is not None:
            raise SandboxedSubagentUnavailableError(refusal)
        return DedicatedWorkerPlacement(child.subagent_id, child.worker_resource_profile, child.worker_sandbox)
    if (
        not child.dedicated_worker
        or dedicated_worker_refusal(
            child.caller_agent_name,
            config,
            runtime_paths,
            resource_profile=child.worker_resource_profile,
        )
        is not None
    ):
        return None
    return DedicatedWorkerPlacement(child.subagent_id, child.worker_resource_profile)


def subagent_sandbox_worker_key(
    child: DelegationChild,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return the exact Kubernetes worker key of a sandboxed child, or None if not sandboxed."""
    if child.worker_sandbox is None or child.child_agent_name not in config.agents:
        return None
    try:
        execution = resolve_agent_execution(
            child.child_agent_name,
            config,
            runtime_paths,
            execution_identity=child_execution_identity(child),
        )
    except ValueError:
        return None
    normal_key = execution.worker_target.worker_key
    return None if normal_key is None else subagent_worker_key(normal_key, child.subagent_id)


def retire_subagent_worker(runtime_paths: RuntimePaths, worker_key: str) -> None:
    """Retire one exact subagent worker on the configured primary backend; failures are logged."""
    lease = lease_configured_primary_worker_manager(runtime_paths)
    if lease is None:
        return
    with lease as backend:
        if not isinstance(backend, RetiringWorkerBackend):
            return
        try:
            backend.retire_worker(worker_key)
        except WorkerBackendError:
            logger.warning("subagent_worker_retire_failed", worker=worker_log_id(worker_key), exc_info=True)


def schedule_subagent_worker_retirement(runtime_paths: RuntimePaths, worker_key: str) -> asyncio.Task[None]:
    """Retire ``worker_key`` in a background thread without blocking workflow settlement."""
    return create_background_task(
        asyncio.to_thread(retire_subagent_worker, runtime_paths, worker_key),
        name="subagent_worker_retire",
    )
