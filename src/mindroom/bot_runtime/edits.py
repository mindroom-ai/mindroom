"""Message edit handling for agent bots."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.thread_utils import create_session_id

if TYPE_CHECKING:
    import nio

    from mindroom.bot import AgentBot
    from mindroom.matrix.event_info import EventInfo


async def _handle_message_edit(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: nio.RoomMessageText,
    event_info: EventInfo,
) -> None:
    """Handle an edited message by regenerating the agent's response."""
    if not event_info.original_event_id:
        self.logger.debug("Edit event has no original event ID")
        return

    sender_agent_name = self.extract_agent_name(event.sender, self.config)
    if sender_agent_name:
        self.logger.debug(f"Ignoring edit from other agent: {sender_agent_name}")
        return

    response_event_id = self.response_tracker.get_response_event_id(event_info.original_event_id)
    if not response_event_id:
        self.logger.debug(f"No previous response found for edited message {event_info.original_event_id}")
        return

    self.logger.info(
        "Regenerating response for edited message",
        original_event_id=event_info.original_event_id,
        response_event_id=response_event_id,
    )

    context = await self._extract_message_context(room, event)
    requester_user_id = self._requester_user_id_for_event(event)

    should_respond = self.should_agent_respond(
        agent_name=self.agent_name,
        am_i_mentioned=context.am_i_mentioned,
        is_thread=context.is_thread,
        room=room,
        thread_history=context.thread_history,
        config=self.config,
        mentioned_agents=context.mentioned_agents,
        has_non_agent_mentions=context.has_non_agent_mentions,
        sender_id=requester_user_id,
    )

    if not should_respond:
        self.logger.debug("Agent should not respond to edited message")
        return

    edited_content = event.source["content"]["m.new_content"]["body"]

    storage = self.create_session_storage(self.agent_name, self.storage_path)
    session_ids_to_check = [
        create_session_id(room.room_id, context.thread_id),
        create_session_id(room.room_id, None),
    ]
    checked_session_ids: set[str] = set()
    for session_id in session_ids_to_check:
        if session_id in checked_session_ids:
            continue
        checked_session_ids.add(session_id)
        removed = self.remove_run_by_event_id(storage, session_id, event_info.original_event_id)
        if removed:
            self.logger.info(
                "Removed stale run for edited message",
                event_id=event_info.original_event_id,
                session_id=session_id,
            )

    await self._generate_response(
        room_id=room.room_id,
        prompt=edited_content,
        reply_to_event_id=event_info.original_event_id,
        thread_id=context.thread_id,
        thread_history=context.thread_history,
        existing_event_id=response_event_id,
        user_id=requester_user_id,
    )

    self.response_tracker.mark_responded(event_info.original_event_id, response_event_id)
    self.logger.info("Successfully regenerated response for edited message")
