"""Post a finished call's transcript into the conversation it was started from."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix_rtc.call_origin import CallOrigin
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_MIN_WRITEBACK_SECONDS = 10


@dataclass(frozen=True)
class _CallWriteback:
    """Plain and HTML bodies of one transcript message."""

    body: str
    formatted_body: str


def format_call_writeback(
    *,
    turns: Sequence[tuple[str, str]],
    duration_seconds: float,
    caller_label: str,
    agent_label: str,
) -> _CallWriteback | None:
    """Render the transcript message, or ``None`` for calls not worth posting.

    Labels and spoken text are folded onto one line and HTML-escaped, so neither can start a speaker line or add markup.
    """
    if duration_seconds < _MIN_WRITEBACK_SECONDS or not any(role == "user" for role, _ in turns):
        return None
    minutes = max(1, round(duration_seconds / 60))
    heading = f"📞 Voice call · {minutes} min"
    labels = {"user": caller_label, "assistant": agent_label}
    lines = [(" ".join(labels.get(role, role).split()), " ".join(text.split())) for role, text in turns]
    body = "\n\n".join([heading, "Transcript", *(f"{label}: {text}" for label, text in lines)])
    transcript = "".join(f"<p><strong>{escape(label)}</strong>: {escape(text)}</p>" for label, text in lines)
    formatted_body = f"<p>{heading}</p><details><summary>Transcript</summary>{transcript}</details>"
    return _CallWriteback(body=body, formatted_body=formatted_body)


async def post_call_writeback(*, context: ToolRuntimeContext, origin: CallOrigin, writeback: _CallWriteback) -> None:
    """Send one transcript message into the origin thread or room."""
    latest_thread_event_id = await context.conversation_reader.latest_thread_event_id(
        room_id=origin.room_id,
        thread_id=origin.thread_id,
    )
    content = build_message_content(
        writeback.body,
        formatted_body=writeback.formatted_body,
        thread_event_id=origin.thread_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content={SKIP_MENTIONS_KEY: True},
    )
    delivered = await send_message_result(
        context.client,
        origin.room_id,
        content,
        operation="call_transcript_writeback",
    )
    if delivered is None:
        logger.warning("call_writeback_send_failed", room_id=origin.room_id, thread_id=origin.thread_id)
        return
    logger.info("call_writeback_sent", room_id=origin.room_id, thread_id=origin.thread_id)
