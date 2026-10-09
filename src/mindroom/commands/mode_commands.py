"""Authenticated conversation mode selection with deployment preflight."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_modes import clear_agent_mode, resolve_agent_mode, set_agent_mode
from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.message_target import MessageTarget
from mindroom.minimal_mode_preflight import minimal_mode_problems, render_minimal_mode_problems
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.worker_routing import build_tool_execution_identity

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def handle_mode_command(
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
    root = resolve_agent_storage(agent_name, config, runtime_paths, identity).state_root
    if action == "minimal":
        problems = minimal_mode_problems(config, runtime_paths, agent_name, identity)
        if problems:
            return render_minimal_mode_problems(agent_name, problems, runtime_paths)
        set_agent_mode(runtime_paths, root, agent_name, target.session_id, "minimal", requester_id)
    elif action in {"standard", "reset"}:
        # Standard is the default, so choosing it keeps no record in the bounded store.
        clear_agent_mode(runtime_paths, root, agent_name, target.session_id)
    return f"Agent `{agent_name}` uses `{resolve_agent_mode(runtime_paths, root, agent_name, target.session_id)}` mode in this conversation."
