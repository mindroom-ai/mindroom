"""Thread-aware Matrix notices that tools post into their current conversation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


class ConversationNoticeError(Exception):
    """A notice could not be posted: the thread fallback was unresolvable, or delivery failed."""

    def __init__(self, reason: Literal["thread_fallback", "delivery"]) -> None:
        super().__init__(reason)
        self.reason = reason


async def send_conversation_notice(
    context: ToolRuntimeContext,
    body: str,
    extra_content: dict[str, object],
    *,
    operation: str,
) -> str:
    """Post one ``m.notice`` into the tool's room or thread and return its event ID.

    In a thread without a reply target, the notice's thread fallback points at the
    thread's latest event so clients without thread support still show it in context.
    """
    thread_id = context.resolved_thread_id
    latest_thread_event_id = context.reply_to_event_id
    if thread_id is not None and latest_thread_event_id is None:
        latest_thread_event_id = await context.conversation_reader.latest_thread_event_id(
            room_id=context.room_id,
            thread_id=thread_id,
        )
        if latest_thread_event_id is None:
            raise ConversationNoticeError(reason="thread_fallback")
    content = build_message_content(
        body,
        thread_event_id=thread_id,
        reply_to_event_id=context.reply_to_event_id if thread_id is not None else None,
        latest_thread_event_id=latest_thread_event_id if thread_id is not None else None,
        extra_content={"msgtype": "m.notice", **extra_content},
    )
    delivered = await send_message_result(context.client, context.room_id, content, operation=operation)
    if delivered is None:
        raise ConversationNoticeError(reason="delivery")
    return delivered.event_id
