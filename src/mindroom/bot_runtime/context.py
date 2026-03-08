"""Shared event gating, context derivation, and dispatch decision helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.authorization import (
    get_effective_sender_id_for_reply_permissions,
    is_sender_allowed_for_agent_reply,
)
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.reply_chain import derive_conversation_context
from mindroom.tool_system.runtime_context import ToolRuntimeContext

from .types import _DispatchEvent, _MessageContext, _PreparedDispatch, _ResponseAction

if TYPE_CHECKING:
    import nio

    from mindroom.bot import AgentBot
    from mindroom.commands.handler import CommandEvent
    from mindroom.matrix.identity import MatrixID
    from mindroom.teams import TeamFormationDecision


def _should_skip_mentions(event_source: dict[str, Any]) -> bool:
    """Check if mentions in this message should be ignored for agent responses."""
    content = event_source.get("content", {})
    return bool(content.get("com.mindroom.skip_mentions", False))


async def _derive_conversation_context(
    self: AgentBot,
    room_id: str,
    event_info: EventInfo,
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    """Derive conversation context from threads or reply chains."""
    assert self.client is not None
    return await derive_conversation_context(
        self.client,
        room_id,
        event_info,
        self._reply_chain,
        self.logger,
        self.fetch_thread_history,
    )


def _requester_user_id_for_event(
    self: AgentBot,
    event: CommandEvent,
) -> str:
    """Return the effective requester for per-user reply checks."""
    content = event.source.get("content") if isinstance(event.source, dict) else None
    if (
        event.sender == self.matrix_id.full_id
        and isinstance(content, dict)
        and isinstance(content.get(ORIGINAL_SENDER_KEY), str)
    ):
        return content[ORIGINAL_SENDER_KEY]
    return get_effective_sender_id_for_reply_permissions(event.sender, event.source, self.config)


def _precheck_event(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: _DispatchEvent,
    *,
    is_edit: bool = False,
) -> str | None:
    """Run common early-exit checks shared by text/media/voice handlers."""
    requester_user_id = self._requester_user_id_for_event(event)

    if requester_user_id == self.matrix_id.full_id:
        return None

    if not is_edit and self.response_tracker.has_responded(event.event_id):
        return None

    if not self.is_authorized_sender(
        event.sender,
        self.config,
        room.room_id,
        room_alias=room.canonical_alias,
    ):
        self.response_tracker.mark_responded(event.event_id)
        return None

    if not self._can_reply_to_sender(requester_user_id):
        self.response_tracker.mark_responded(event.event_id)
        return None

    return requester_user_id


async def _prepare_dispatch(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: _DispatchEvent,
    *,
    requester_user_id: str | None = None,
    event_label: str,
) -> _PreparedDispatch | None:
    """Run common precheck/context/sender-gating for dispatch handlers."""
    effective_requester_user_id = requester_user_id or self._precheck_event(room, event)
    if effective_requester_user_id is None:
        return None

    context = await self._extract_message_context(room, event)
    sender_agent_name = self.extract_agent_name(effective_requester_user_id, self.config)
    if sender_agent_name and not context.am_i_mentioned:
        self.logger.debug(f"Ignoring {event_label} from other agent (not mentioned)")
        return None

    return _PreparedDispatch(
        requester_user_id=effective_requester_user_id,
        context=context,
    )


def _can_reply_to_sender(self: AgentBot, sender_id: str) -> bool:
    """Return whether this entity may reply to *sender_id*."""
    return is_sender_allowed_for_agent_reply(sender_id, self.agent_name, self.config)


async def _resolve_response_action(
    self: AgentBot,
    context: _MessageContext,
    room: nio.MatrixRoom,
    requester_user_id: str,
    message: str,
    is_dm: bool,
) -> _ResponseAction:
    """Decide whether to respond as a team, individually, or skip."""
    agents_in_thread = self.get_agents_in_thread(context.thread_history, self.config)
    form_team = await self._decide_team_for_sender(
        agents_in_thread,
        context,
        room,
        requester_user_id,
        message,
        is_dm,
    )

    if form_team.should_form_team and self.matrix_id in form_team.agents:
        first_agent = min(form_team.agents, key=lambda x: x.full_id)
        if self.matrix_id != first_agent:
            return _ResponseAction(kind="skip")
        return _ResponseAction(kind="team", form_team=form_team)

    if not self.should_agent_respond(
        agent_name=self.agent_name,
        am_i_mentioned=context.am_i_mentioned,
        is_thread=context.is_thread,
        room=room,
        thread_history=context.thread_history,
        config=self.config,
        mentioned_agents=context.mentioned_agents,
        has_non_agent_mentions=context.has_non_agent_mentions,
        sender_id=requester_user_id,
    ):
        return _ResponseAction(kind="skip")

    return _ResponseAction(kind="individual")


async def _decide_team_for_sender(
    self: AgentBot,
    agents_in_thread: list[MatrixID],
    context: _MessageContext,
    room: nio.MatrixRoom,
    requester_user_id: str,
    message: str,
    is_dm: bool,
) -> TeamFormationDecision:
    """Decide team formation using only agents the sender is allowed to interact with."""
    all_mentioned_in_thread = self.get_all_mentioned_agents_in_thread(context.thread_history, self.config)
    available_agents_in_room: list[MatrixID] | None = None
    if is_dm:
        available_agents_in_room = self.get_available_agents_for_sender(room, requester_user_id, self.config)
    return await self.decide_team_formation(
        self.matrix_id,
        self.filter_agents_by_sender_permissions(context.mentioned_agents, requester_user_id, self.config),
        self.filter_agents_by_sender_permissions(agents_in_thread, requester_user_id, self.config),
        self.filter_agents_by_sender_permissions(all_mentioned_in_thread, requester_user_id, self.config),
        room=room,
        message=message,
        config=self.config,
        is_dm_room=is_dm,
        is_thread=context.is_thread,
        available_agents_in_room=available_agents_in_room,
    )


async def _extract_message_context(self: AgentBot, room: nio.MatrixRoom, event: _DispatchEvent) -> _MessageContext:
    """Extract mention and thread context for an event."""
    assert self.client is not None

    skip_mentions = _should_skip_mentions(event.source)

    if skip_mentions:
        mentioned_agents: list[MatrixID] = []
        am_i_mentioned = False
        has_non_agent_mentions = False
    else:
        mentioned_agents, am_i_mentioned, has_non_agent_mentions = self.check_agent_mentioned(
            event.source,
            self.matrix_id,
            self.config,
        )

    if am_i_mentioned:
        self.logger.info("Mentioned", event_id=event.event_id, room_id=room.room_id)

    event_info = EventInfo.from_event(event.source)
    if self.config.get_entity_thread_mode(self.agent_name, room_id=room.room_id) == "room":
        is_thread = False
        thread_id = None
        thread_history: list[dict[str, Any]] = []
    else:
        is_thread, thread_id, thread_history = await self._derive_conversation_context(
            room.room_id,
            event_info,
        )

    return _MessageContext(
        am_i_mentioned=am_i_mentioned,
        is_thread=is_thread,
        thread_id=thread_id,
        thread_history=thread_history,
        mentioned_agents=mentioned_agents,
        has_non_agent_mentions=has_non_agent_mentions,
    )


def _cached_room(self: AgentBot, room_id: str) -> nio.MatrixRoom | None:
    """Return room from client cache when available."""
    client = self.client
    if client is None:
        return None
    return client.rooms.get(room_id)


def _build_tool_runtime_context(
    self: AgentBot,
    room_id: str,
    thread_id: str | None,
    reply_to_event_id: str | None,
    user_id: str | None,
    *,
    agent_name: str | None = None,
    attachment_ids: list[str] | None = None,
) -> ToolRuntimeContext | None:
    """Build shared runtime context for all tool calls."""
    if self.client is None:
        return None
    return ToolRuntimeContext(
        agent_name=agent_name or self.agent_name,
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=self._resolve_reply_thread_id(thread_id, reply_to_event_id, room_id=room_id),
        requester_id=user_id or self.matrix_id.full_id,
        client=self.client,
        config=self.config,
        room=self._cached_room(room_id),
        reply_to_event_id=reply_to_event_id,
        storage_path=self.storage_path,
        attachment_ids=tuple(attachment_ids or []),
    )


def _agent_has_matrix_messaging_tool(self: AgentBot, agent_name: str) -> bool:
    """Return whether an agent can issue Matrix message actions."""
    try:
        tool_names = self.config.get_agent_tools(agent_name)
    except ValueError:
        return False
    if not isinstance(tool_names, list | tuple | set):
        return False
    return "matrix_message" in tool_names


def _append_matrix_prompt_context(
    self: AgentBot,
    prompt: str,
    *,
    room_id: str,
    thread_id: str | None,
    reply_to_event_id: str | None,
    include_context: bool,
) -> str:
    """Append room/thread/event ids to the LLM prompt when messaging tools are available."""
    if not include_context:
        return prompt
    if self._MATRIX_PROMPT_CONTEXT_MARKER in prompt:
        return prompt

    effective_thread_id = self._resolve_reply_thread_id(thread_id, reply_to_event_id, room_id=room_id)
    metadata_block = "\n".join(
        (
            self._MATRIX_PROMPT_CONTEXT_MARKER,
            f"room_id: {room_id}",
            f"thread_id: {effective_thread_id or 'none'}",
            f"reply_to_event_id: {reply_to_event_id or 'none'}",
            "Use these IDs when calling matrix_message.",
        ),
    )
    return f"{prompt.rstrip()}\n\n{metadata_block}"
