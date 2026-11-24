"""Post a finished call's transcript into the conversation it was started from."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

from mindroom.constants import CALL_TRANSCRIPT_CONTENT_KEY, SKIP_MENTIONS_KEY
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix_rtc.call_origin import CallOrigin
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_MIN_WRITEBACK_SECONDS = 10


def _call_time(seconds: int) -> str:
    """Format whole seconds from the call start as ``[m:ss]``."""
    return f"[{seconds // 60}:{seconds % 60:02}]"


@dataclass(frozen=True)
class _CallWriteback:
    """Plain and HTML bodies of one transcript message, and its turns by sender ID for clients."""

    body: str
    formatted_body: str
    transcript: dict[str, object]


def format_call_writeback(
    *,
    turns: Sequence[tuple[str, str, int]],
    duration_seconds: float,
    caller_id: str,
    caller_label: str,
    agent_id: str,
    agent_label: str,
) -> _CallWriteback | None:
    """Render the transcript message, or ``None`` for calls not worth posting.

    Labels and spoken text are folded onto one line and HTML-escaped, so neither can start a speaker line or add markup.
    """
    if duration_seconds < _MIN_WRITEBACK_SECONDS or not any(role == "user" for role, _, _ in turns):
        return None
    minutes = max(1, round(duration_seconds / 60))
    heading = f"📞 Voice call · {minutes} min"
    speakers = {
        "user": (caller_id, " ".join(caller_label.split())),
        "assistant": (agent_id, " ".join(agent_label.split())),
    }
    lines = [(*speakers[role], " ".join(text.split()), at) for role, text, at in turns if role in speakers]
    body = "\n\n".join([heading, "Transcript", *(f"{_call_time(at)} {label}: {text}" for _, label, text, at in lines)])
    transcript = "".join(
        f"<p>{_call_time(at)} <strong>{escape(label)}</strong>: {escape(text)}</p>" for _, label, text, at in lines
    )
    formatted_body = f"<p>{heading}</p><details><summary>Transcript</summary>{transcript}</details>"
    turns_by_sender = [{"sender": sender, "text": text, "offset_seconds": at} for sender, _, text, at in lines]
    return _CallWriteback(
        body=body,
        formatted_body=formatted_body,
        transcript={"duration_seconds": round(duration_seconds), "turns": turns_by_sender},
    )


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
        extra_content={SKIP_MENTIONS_KEY: True, CALL_TRANSCRIPT_CONTENT_KEY: writeback.transcript},
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
