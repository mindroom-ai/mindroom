"""Router-specific dispatch logic for agent bots."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import ATTACHMENT_IDS_KEY, ROUTER_AGENT_NAME
from mindroom.thread_utils import get_configured_agents_for_room

from .types import _DispatchEvent, _MessageContext, _RouterDispatchResult

if TYPE_CHECKING:
    from mindroom.bot import AgentBot


async def _handle_router_dispatch(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: _DispatchEvent,
    context: _MessageContext,
    requester_user_id: str,
    *,
    message: str | None = None,
    extra_content: dict[str, Any] | None = None,
) -> _RouterDispatchResult:
    """Run the router dispatch logic shared by text and media handlers."""
    if self.agent_name != ROUTER_AGENT_NAME:
        return _RouterDispatchResult(handled=False)

    agents_in_thread = self.get_agents_in_thread(context.thread_history, self.config)
    sender_visible = self.filter_agents_by_sender_permissions(agents_in_thread, requester_user_id, self.config)

    if not context.mentioned_agents and not context.has_non_agent_mentions and not sender_visible:
        if context.is_thread and self.has_multiple_non_agent_users_in_thread(context.thread_history, self.config):
            self.logger.info("Skipping routing: multiple non-agent users in thread (mention required)")
            return _RouterDispatchResult(handled=True, mark_visible_echo_responded=True)
        available_agents = self.get_available_agents_for_sender(room, requester_user_id, self.config)
        if len(available_agents) == 1:
            self.logger.info("Skipping routing: only one agent present")
            return _RouterDispatchResult(handled=True, mark_visible_echo_responded=True)
        await self._handle_ai_routing(
            room,
            event,
            context.thread_history,
            context.thread_id,
            message=message,
            requester_user_id=requester_user_id,
            extra_content=extra_content,
        )
        return _RouterDispatchResult(handled=True)
    return _RouterDispatchResult(handled=True, mark_visible_echo_responded=True)


async def _handle_ai_routing(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: _DispatchEvent,
    thread_history: list[dict[str, Any]],
    thread_id: str | None = None,
    message: str | None = None,
    requester_user_id: str | None = None,
    extra_content: dict[str, Any] | None = None,
) -> None:
    """Suggest an agent and relay that routing decision into the room."""
    assert self.agent_name == ROUTER_AGENT_NAME

    permission_sender_id = requester_user_id or event.sender
    available_agents = get_configured_agents_for_room(room.room_id, self.config)
    available_agents = self.filter_agents_by_sender_permissions(available_agents, permission_sender_id, self.config)
    if not available_agents:
        self.logger.debug("No configured agents to route to in this room for sender", sender=permission_sender_id)
        return

    self.logger.info("Handling AI routing", event_id=event.event_id)

    routing_text = message or event.body
    suggested_agent = await self.suggest_agent_for_message(
        routing_text,
        available_agents,
        self.config,
        thread_history,
    )

    if not suggested_agent:
        response_text = (
            "⚠️ I couldn't determine which agent should help with this. "
            "Please try mentioning an agent directly with @ or rephrase your request."
        )
        self.logger.warning("Router failed to determine agent")
    else:
        response_text = f"@{suggested_agent} could you help with this?"

    target_thread_mode = (
        self.config.get_entity_thread_mode(suggested_agent, room_id=room.room_id) if suggested_agent else None
    )
    thread_event_id = self._resolve_reply_thread_id(
        thread_id,
        event.event_id,
        room_id=room.room_id,
        event_source=event.source,
        thread_mode_override=target_thread_mode,
    )
    routed_extra_content = dict(extra_content) if extra_content is not None else {}
    if isinstance(
        event,
        nio.RoomMessageFile
        | nio.RoomEncryptedFile
        | nio.RoomMessageVideo
        | nio.RoomEncryptedVideo
        | nio.RoomMessageImage
        | nio.RoomEncryptedImage,
    ):
        attachment_id = await self._register_routed_attachment(
            room_id=room.room_id,
            thread_id=thread_event_id,
            event=event,
        )
        if attachment_id is None:
            routed_extra_content.pop(ATTACHMENT_IDS_KEY, None)
        else:
            routed_extra_content[ATTACHMENT_IDS_KEY] = [attachment_id]

    event_id = await self._send_response(
        room_id=room.room_id,
        reply_to_event_id=event.event_id,
        response_text=response_text,
        thread_id=thread_event_id,
        extra_content=routed_extra_content or None,
        thread_mode_override=target_thread_mode,
    )
    if event_id:
        self.logger.info("Routed to agent", suggested_agent=suggested_agent)
        self.response_tracker.mark_responded(event.event_id)
    else:
        self.logger.error("Failed to route to agent", agent=suggested_agent)
