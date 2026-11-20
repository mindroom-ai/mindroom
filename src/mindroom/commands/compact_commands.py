"""Ask agents and teams to compact their context before their next reply in one conversation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.authorization import addressable_responder_names, is_sender_allowed_for_responder
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.history.manual import schedule_compaction_before_next_reply, scope_has_session
from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget
from mindroom.tool_system.worker_routing import build_tool_execution_identity

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


async def handle_compact_command(
    args_text: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    target: MessageTarget,
    requester_id: str,
    membership_index: AgentReplyMembershipIndex,
) -> str:
    """Compact the named agent or team, else every one the requester may address that already has a session here."""
    words = args_text.split()
    if len(words) > 1:
        return "Use `/compact [agent_or_team]`."
    if words:
        name = words[0]
        if name not in config.agents and name not in config.teams:
            return f"Unknown agent or team `{name}`."
        if not is_sender_allowed_for_responder(
            requester_id,
            name,
            target.room_id,
            config,
            runtime_paths,
            membership_index,
            require_resolved_membership=True,
        ):
            return f"Access denied for `{name}`."
        reply = await _compact(name, config, runtime_paths, target, requester_id, only_stored=False)
        return reply or f"Run `/compact {name}` inside an existing thread for `{name}`."
    names = addressable_responder_names(requester_id, target.room_id, config, runtime_paths, membership_index)
    replies = [
        reply
        for name in names
        if name != ROUTER_AGENT_NAME
        and (reply := await _compact(name, config, runtime_paths, target, requester_id, only_stored=True))
    ]
    return "\n".join(replies) or "Nothing to compact here."


async def _compact(
    name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    target: MessageTarget,
    requester_id: str,
    *,
    only_stored: bool,
) -> str | None:
    """Request compaction for ``name`` in this conversation; None when it has no session here to compact."""
    room_mode = config.get_entity_thread_mode(name, runtime_paths, room_id=target.room_id) == "room"
    if not room_mode and target.source_thread_id is None:
        return None
    target = MessageTarget.resolve(
        target.room_id,
        target.source_thread_id,
        target.reply_to_event_id,
        room_mode=room_mode,
    )
    # A configured team's members store to the team's scope, as its replies do.
    scope = HistoryScope(kind="team" if name in config.teams else "agent", scope_id=name)
    # The requester's own store for a private agent.
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=name,
        runtime_paths=runtime_paths,
        requester_id=requester_id,
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        resolved_thread_id=target.resolved_thread_id,
        session_id=target.session_id,
    )
    if only_stored and not await scope_has_session(
        scope=scope,
        session_id=target.session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity,
    ):
        return None
    unavailable = await schedule_compaction_before_next_reply(
        scope=scope,
        agent_name=name,
        session_id=target.session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity,
        active_model_name=None,
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
    )
    if unavailable is not None:
        return f"❌ `{name}` cannot compact its context because {unavailable}."
    return f"✅ `{name}` compacts its context before its next reply in this {'room' if room_mode else 'thread'}."
