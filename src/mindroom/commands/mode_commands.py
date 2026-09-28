"""Authenticated conversation mode selection with deployment preflight."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_cli.worker_network import validate_cli_primary_auth
from mindroom.agent_cli.worker_protocol import SHELL_OPERATION_NAMES, safe_origin
from mindroom.agent_modes import clear_agent_mode, resolve_agent_mode, set_agent_mode
from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.message_target import MessageTarget
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.catalog import ensure_tool_registry_loaded, get_tool_by_name
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target, build_tool_execution_identity
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.docker_config import DockerWorkerBackendConfig
from mindroom.workers.runtime import primary_worker_backend_name
from mindroom.workspaces import resolve_agent_workspace_from_state_path

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


def _minimal_mode_unavailable_reason(  # noqa: PLR0911 - independent deployment eligibility failures
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    worker_target: ResolvedWorkerTarget,
) -> str | None:
    """Check deployment prerequisites without opening integrations or a worker."""
    shell = next(
        (entry for entry in config.resolve_entity(agent_name).authored_tool_configs if entry.name == "shell"),
        None,
    )
    if shell is None:
        return "Minimal mode requires the agent's existing shell permission."
    try:
        ensure_tool_registry_loaded(runtime_paths, config, load_plugin_tools=False)
        toolkit = get_tool_by_name(
            "shell",
            runtime_paths,
            runtime_config=config,
            tool_config_overrides=shell.tool_config_overrides,
            worker_target=worker_target,
            allowed_shared_services=(
                config.get_worker_grantable_credentials() if worker_target.worker_scope is not None else None
            ),
            disable_sandbox_proxy=True,
        )
    except (ImportError, ValueError):
        return "Minimal mode requires an available, valid shell configuration."
    if not set(SHELL_OPERATION_NAMES).issubset(toolkit.get_async_functions()):
        return "Minimal mode requires the agent's run, check, and kill shell permissions."
    if primary_worker_backend_name(runtime_paths) != "docker":
        return "Minimal mode requires dedicated Docker workers."
    try:
        DockerWorkerBackendConfig.from_runtime(runtime_paths).validate_cli_profile()
    except WorkerBackendError as exc:
        return f"Minimal mode is unavailable: {exc}"
    try:
        validate_cli_primary_auth(runtime_paths)
        gateway = safe_origin(runtime_paths.env_value("MINDROOM_AGENT_CLI_GATEWAY_URL") or "")
        primary = safe_origin(runtime_paths.env_value("MINDROOM_AGENT_CLI_PRIMARY_URL") or "")
    except ValueError as exc:
        return str(exc)
    if gateway == primary:
        return "Minimal mode requires a separate gateway-only proxy origin."
    return None


def handle_mode_command(  # noqa: PLR0911 - independent authorization and deployment refusals
    args_text: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    target: MessageTarget,
    requester_id: str,
    membership_index: AgentReplyMembershipIndex,
) -> str:
    """Authorize the target agent before inspecting or changing its scoped choice."""
    words = args_text.split()
    if len(words) != 2 or words[1] not in {"standard", "minimal", "show", "reset"}:
        return "Use `!mode <agent> minimal|standard|show|reset`."
    agent_name, action = words
    if agent_name not in config.agents:
        return f"Unknown agent `{agent_name}`."
    if not is_sender_allowed_for_responder(
        requester_id,
        agent_name,
        target.room_id,
        config,
        runtime_paths,
        membership_index,
        require_resolved_membership=True,
    ):
        return f"Access denied for agent `{agent_name}`."
    room_mode = config.get_entity_thread_mode(agent_name, runtime_paths, room_id=target.room_id) == "room"
    if not room_mode and target.source_thread_id is None:
        return f"Run `!mode {agent_name} {action}` inside an existing thread for this agent."
    target = MessageTarget.resolve(
        target.room_id,
        target.source_thread_id,
        target.reply_to_event_id,
        room_mode=room_mode,
    )
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=agent_name,
        runtime_paths=runtime_paths,
        requester_id=requester_id,
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        resolved_thread_id=target.resolved_thread_id,
        session_id=target.session_id,
    )
    storage = resolve_agent_storage(agent_name, config, runtime_paths, identity)
    root = storage.state_root
    if action == "minimal":
        reason = _minimal_mode_unavailable_reason(
            config,
            runtime_paths,
            agent_name,
            build_agent_toolkit_worker_target(
                storage.execution.execution_scope,
                agent_name,
                is_private=storage.execution.is_private,
                execution_identity=identity,
                runtime_paths=runtime_paths,
            ),
        )
        if reason is not None:
            return reason
        workspace = resolve_agent_workspace_from_state_path(
            agent_name,
            config,
            runtime_paths=runtime_paths,
            state_storage_path=root,
            use_state_storage_path=storage.execution.policy.private_workspace_enabled,
        )
        if workspace is None:
            return "Minimal mode requires an existing canonical agent workspace."
        set_agent_mode(root, agent_name, target.session_id, "minimal", requester_id)
    elif action in {"standard", "reset"}:
        # Standard is the default, so choosing it keeps no record in the bounded store.
        clear_agent_mode(root, agent_name, target.session_id)
    return f"Agent `{agent_name}` uses `{resolve_agent_mode(root, agent_name, target.session_id)}` mode in this conversation."
