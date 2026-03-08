"""Message and reaction dispatch workflows for agent bots."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom import interactive
from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.commands.handler import CommandHandlerContext, handle_command
from mindroom.commands.parsing import command_parser
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.streaming import is_in_progress_message

if TYPE_CHECKING:
    import nio

    from mindroom.bot import AgentBot
    from mindroom.commands.parsing import Command

    from .types import _DispatchEvent, _DispatchPayload, _PreparedDispatch, _ResponseAction, _TextDispatchEvent


async def _on_message(self: AgentBot, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
    """Handle incoming text messages for one agent bot."""
    self.logger.info("Received message", event_id=event.event_id, room_id=room.room_id, sender=event.sender)
    assert self.client is not None
    if not isinstance(event.body, str) or is_in_progress_message(event.body):
        return

    event_info = EventInfo.from_event(event.source)
    requester_user_id = self._precheck_event(room, event, is_edit=event_info.is_edit)
    if requester_user_id is None:
        return

    if event_info.is_edit:
        await self._handle_message_edit(room, event, event_info)
        return

    await interactive.handle_text_response(self.client, room, event, self.agent_name)
    await self._dispatch_text_message(room, event, requester_user_id)


async def _dispatch_text_message(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: _TextDispatchEvent,
    requester_user_id: str,
) -> None:
    """Run the normal text and command dispatch pipeline for a prepared text event."""
    assert isinstance(event.body, str)

    command = command_parser.parse(event.body)
    if command:
        if self.agent_name == ROUTER_AGENT_NAME:
            await self._handle_command(room, event, command)
        return

    dispatch = await self._prepare_dispatch(
        room,
        event,
        requester_user_id=requester_user_id,
        event_label="message",
    )
    if dispatch is None:
        return

    content = event.source.get("content") if isinstance(event.source, dict) else None
    message_attachment_ids = parse_attachment_ids_from_event_source(event.source)
    message_extra_content: dict[str, Any] = {}
    if message_attachment_ids:
        message_extra_content[ATTACHMENT_IDS_KEY] = message_attachment_ids
    if isinstance(content, dict):
        original_sender = content.get(ORIGINAL_SENDER_KEY)
        if isinstance(original_sender, str):
            message_extra_content[ORIGINAL_SENDER_KEY] = original_sender
        raw_audio_fallback = content.get(VOICE_RAW_AUDIO_FALLBACK_KEY)
        if isinstance(raw_audio_fallback, bool) and raw_audio_fallback:
            message_extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True

    action = await self._resolve_dispatch_action(
        room,
        event,
        dispatch,
        message_for_decision=event.body,
        extra_content=message_extra_content or None,
    )
    if action is None:
        return

    context = dispatch.context
    payload = await self._build_dispatch_payload_with_attachments(
        room_id=room.room_id,
        context=context,
        prompt=event.body,
        current_attachment_ids=message_attachment_ids,
        media_thread_id=context.thread_id,
    )
    await self._execute_dispatch_action(
        room,
        event,
        dispatch,
        action,
        payload,
        processing_log="Processing",
    )


async def _on_reaction(self: AgentBot, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
    """Handle reaction events for interactive questions, stop functionality, and config confirmations."""
    assert self.client is not None

    if not self.is_authorized_sender(
        event.sender,
        self.config,
        room.room_id,
        room_alias=room.canonical_alias,
    ):
        self.logger.debug(f"Ignoring reaction from unauthorized sender: {event.sender}")
        return

    if not self._can_reply_to_sender(event.sender):
        self.logger.debug("Ignoring reaction due to reply permissions", sender=event.sender)
        return

    if event.key == "🛑":
        sender_agent_name = self.extract_agent_name(event.sender, self.config)
        if not sender_agent_name and await self.stop_manager.handle_stop_reaction(event.reacts_to):
            self.logger.info(
                "Stopped generation for message",
                message_id=event.reacts_to,
                stopped_by=event.sender,
            )
            await self.stop_manager.remove_stop_button(self.client, event.reacts_to)
            await self._send_response(room.room_id, event.reacts_to, "✅ Generation stopped", None)
            return

    pending_change = self.config_confirmation.get_pending_change(event.reacts_to)

    if pending_change and self.agent_name == ROUTER_AGENT_NAME:
        await self.config_confirmation.handle_confirmation_reaction(self, room, event, pending_change)
        return

    result = await interactive.handle_reaction(self.client, event, self.agent_name, self.config)

    if result:
        selected_value, thread_id = result
        thread_history = []
        if thread_id:
            thread_history = await self.fetch_thread_history(self.client, room.room_id, thread_id)
            if self.has_user_responded_after_message(thread_history, event.reacts_to, self.matrix_id):
                self.logger.info(
                    "Ignoring reaction - agent already responded after this question",
                    reacted_to=event.reacts_to,
                )
                return

        ack_text = f"You selected: {event.key} {selected_value}\n\nProcessing your response..."
        ack_event_id = await self._send_response(
            room.room_id,
            None if thread_id else event.reacts_to,
            ack_text,
            thread_id,
        )

        if not ack_event_id:
            self.logger.error("Failed to send acknowledgment for reaction")
            return

        prompt = f"The user selected: {selected_value}"
        response_event_id = await self._generate_response(
            room_id=room.room_id,
            prompt=prompt,
            reply_to_event_id=event.reacts_to,
            thread_id=thread_id,
            thread_history=thread_history,
            existing_event_id=ack_event_id,
            user_id=event.sender,
        )
        self.response_tracker.mark_responded(event.reacts_to, response_event_id)


async def _resolve_dispatch_action(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: _DispatchEvent,
    dispatch: _PreparedDispatch,
    *,
    message_for_decision: str,
    router_message: str | None = None,
    extra_content: dict[str, Any] | None = None,
) -> _ResponseAction | None:
    """Resolve routing plus team or individual action for a prepared dispatch."""
    router_result = await self._handle_router_dispatch(
        room,
        event,
        dispatch.context,
        dispatch.requester_user_id,
        message=router_message,
        extra_content=extra_content,
    )
    if router_result.handled:
        visible_router_echo_event_id = self.response_tracker.get_visible_echo_event_id(event.event_id)
        if (
            router_result.mark_visible_echo_responded
            and visible_router_echo_event_id is not None
            and not self.response_tracker.has_responded(event.event_id)
        ):
            self.response_tracker.mark_responded(event.event_id, visible_router_echo_event_id)
        return None

    assert self.client is not None
    dm_room = await self.is_dm_room(self.client, room.room_id)
    action = await self._resolve_response_action(
        dispatch.context,
        room,
        dispatch.requester_user_id,
        message_for_decision,
        dm_room,
    )
    if action.kind == "skip":
        return None
    return action


async def _execute_dispatch_action(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: _DispatchEvent,
    dispatch: _PreparedDispatch,
    action: _ResponseAction,
    payload: _DispatchPayload,
    *,
    processing_log: str,
) -> None:
    """Execute resolved dispatch action and mark the source event responded."""
    if action.kind == "team":
        assert action.form_team is not None
        response_event_id = await self._generate_team_response_helper(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            thread_id=dispatch.context.thread_id,
            payload=payload,
            team_agents=action.form_team.agents,
            team_mode=action.form_team.mode,
            thread_history=dispatch.context.thread_history,
            requester_user_id=dispatch.requester_user_id,
            existing_event_id=None,
        )
        self.response_tracker.mark_responded(event.event_id, response_event_id)
        return

    if not dispatch.context.am_i_mentioned:
        self.logger.info("Will respond: only agent in thread")

    self.logger.info(processing_log, event_id=event.event_id)
    response_event_id = await self._generate_response(
        room_id=room.room_id,
        prompt=payload.prompt,
        reply_to_event_id=event.event_id,
        thread_id=dispatch.context.thread_id,
        thread_history=dispatch.context.thread_history,
        user_id=dispatch.requester_user_id,
        media=payload.media,
        attachment_ids=payload.attachment_ids,
    )
    self.response_tracker.mark_responded(event.event_id, response_event_id)


async def _handle_command(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: _TextDispatchEvent,
    command: Command,
) -> None:
    """Handle a parsed router command."""
    assert self.client is not None
    context = CommandHandlerContext(
        client=self.client,
        config=self.config,
        logger=self.logger,
        response_tracker=self.response_tracker,
        derive_conversation_context=self._derive_conversation_context,
        requester_user_id_for_event=self._requester_user_id_for_event,
        resolve_reply_thread_id=self._resolve_reply_thread_id,
        send_response=self._send_response,
        send_skill_command_response=self._send_skill_command_response,
    )
    await handle_command(
        context=context,
        room=room,
        event=event,
        command=command,
    )
