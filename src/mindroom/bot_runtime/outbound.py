"""Outbound Matrix messaging helpers for agent bots."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import nio

from mindroom.matrix.client import edit_message
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from mindroom.bot import AgentBot
    from mindroom.tool_system.events import ToolTraceEntry


def _resolve_reply_thread_id(
    self: AgentBot,
    thread_id: str | None,
    reply_to_event_id: str | None = None,
    *,
    room_id: str | None = None,
    event_source: dict[str, Any] | None = None,
    thread_mode_override: Literal["thread", "room"] | None = None,
) -> str | None:
    """Resolve the effective thread root for outgoing replies."""
    effective_thread_mode = thread_mode_override or self.config.get_entity_thread_mode(
        self.agent_name,
        room_id=room_id,
    )
    if effective_thread_mode == "room":
        return None
    event_info = EventInfo.from_event(event_source)
    return thread_id or event_info.safe_thread_root or reply_to_event_id


async def _send_response(
    self: AgentBot,
    room_id: str,
    reply_to_event_id: str | None,
    response_text: str,
    thread_id: str | None,
    reply_to_event: nio.RoomMessageText | None = None,
    skip_mentions: bool = False,
    tool_trace: list[ToolTraceEntry] | None = None,
    extra_content: dict[str, Any] | None = None,
    thread_mode_override: Literal["thread", "room"] | None = None,
) -> str | None:
    """Send a response message to a room."""
    sender_id = self.matrix_id
    sender_domain = sender_id.domain

    effective_thread_id = self._resolve_reply_thread_id(
        thread_id,
        reply_to_event_id,
        room_id=room_id,
        event_source=reply_to_event.source if reply_to_event else None,
        thread_mode_override=thread_mode_override,
    )

    if effective_thread_id is None:
        content = self.format_message_with_mentions(
            self.config,
            response_text,
            sender_domain=sender_domain,
            thread_event_id=None,
            reply_to_event_id=None,
            latest_thread_event_id=None,
            tool_trace=tool_trace,
            extra_content=extra_content,
        )
    else:
        latest_thread_event_id = await self.get_latest_thread_event_id_if_needed(
            self.client,
            room_id,
            effective_thread_id,
            reply_to_event_id,
        )
        content = self.format_message_with_mentions(
            self.config,
            response_text,
            sender_domain=sender_domain,
            thread_event_id=effective_thread_id,
            reply_to_event_id=reply_to_event_id,
            latest_thread_event_id=latest_thread_event_id,
            tool_trace=tool_trace,
            extra_content=extra_content,
        )

    if skip_mentions:
        content["com.mindroom.skip_mentions"] = True

    assert self.client is not None
    event_id = await self.send_message(self.client, room_id, content)
    if event_id:
        self.logger.info("Sent response", event_id=event_id, room_id=room_id)
        return event_id
    self.logger.error("Failed to send response to room", room_id=room_id)
    return None


async def _edit_message(
    self: AgentBot,
    room_id: str,
    event_id: str,
    new_text: str,
    thread_id: str | None,
    tool_trace: list[ToolTraceEntry] | None = None,
    extra_content: dict[str, Any] | None = None,
) -> bool:
    """Edit an existing message."""
    sender_id = self.matrix_id
    sender_domain = sender_id.domain

    if self.config.get_entity_thread_mode(self.agent_name, room_id=room_id) == "room":
        content = self.format_message_with_mentions(
            self.config,
            new_text,
            sender_domain=sender_domain,
            tool_trace=tool_trace,
            extra_content=extra_content,
        )
    else:
        latest_thread_event_id = None
        if thread_id:
            assert self.client is not None
            latest_thread_event_id = await self.latest_thread_event_id(self.client, room_id, thread_id)
            if latest_thread_event_id is None:
                latest_thread_event_id = event_id

        content = self.format_message_with_mentions(
            self.config,
            new_text,
            sender_domain=sender_domain,
            thread_event_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
            tool_trace=tool_trace,
            extra_content=extra_content,
        )

    assert self.client is not None
    response = await edit_message(self.client, room_id, event_id, content, new_text)

    if isinstance(response, nio.RoomSendResponse):
        self.logger.info("Edited message", event_id=event_id)
        return True
    self.logger.error("Failed to edit message", event_id=event_id, error=str(response))
    return False
