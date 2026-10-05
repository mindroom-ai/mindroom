"""Post a finished call's transcript into the conversation it was started from."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix_rtc.call_origin import CallOrigin
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_MIN_WRITEBACK_SECONDS = 10


def format_call_writeback(
    *,
    turns: Sequence[tuple[str, str]],
    duration_seconds: float,
    caller_label: str,
    agent_label: str,
) -> str | None:
    """Render the transcript message, or ``None`` for calls not worth posting."""
    if duration_seconds < _MIN_WRITEBACK_SECONDS or not any(role == "user" for role, _ in turns):
        return None
    minutes = max(1, round(duration_seconds / 60))
    labels = {"user": caller_label, "assistant": agent_label}
    lines = [f"**{labels.get(role, role)}**: {text}" for role, text in turns]
    transcript = "\n\n".join(lines)
    return f"📞 Voice call · {minutes} min\n\n<details>\n<summary>Transcript</summary>\n\n{transcript}\n\n</details>"


async def post_call_writeback(*, context: ToolRuntimeContext, origin: CallOrigin, body: str) -> bool:
    """Send one transcript message into the origin thread or room."""
    latest_thread_event_id = None
    if origin.thread_id is not None:
        latest_thread_event_id = await context.conversation_reader.latest_thread_event_id(
            room_id=origin.room_id,
            thread_id=origin.thread_id,
        )
    content = build_message_content(
        body,
        thread_event_id=origin.thread_id,
        latest_thread_event_id=latest_thread_event_id or origin.thread_id,
    )
    delivered = await send_message_result(
        context.client,
        origin.room_id,
        content,
        operation="call_transcript_writeback",
    )
    if delivered is None:
        logger.warning("call_writeback_send_failed", room_id=origin.room_id, thread_id=origin.thread_id)
        return False
    logger.info("call_writeback_sent", room_id=origin.room_id, thread_id=origin.thread_id)
    return True
