"""AI-generated one-line summaries for Matrix threads."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import nio
from agno.agent import Agent
from pydantic import BaseModel, Field

from mindroom.ai import cached_agent_run, get_model_instance
from mindroom.logging_config import get_logger
from mindroom.matrix.client import (
    VisibleMessageLike,
    fetch_thread_history,
    visible_message_body,
    visible_message_sender,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# In-memory tracking of last summarized message count per thread.
# Key: "{room_id}:{thread_id}", value: message count at last summary.
_last_summary_counts: dict[str, int] = {}

_FIRST_THRESHOLD = 5
_SUBSEQUENT_INTERVAL = 10


class _ThreadSummary(BaseModel):
    """Structured thread summary response."""

    summary: str = Field(description="One-line summary of the thread conversation")


def _next_threshold(last_summarized_count: int) -> int:
    """Return the next message count at which a summary should be generated."""
    if last_summarized_count < _FIRST_THRESHOLD:
        return _FIRST_THRESHOLD
    return last_summarized_count + _SUBSEQUENT_INTERVAL


async def _recover_last_summary_count(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
) -> int:
    """Recover the last summarized message count from existing summary events in the thread.

    Scans recent room messages for events with ``io.mindroom.thread_summary``
    metadata that belong to *thread_id* and returns the highest
    ``message_count`` found, or 0.
    """
    response = await client.room_messages(
        room_id,
        start=None,
        limit=100,
        message_filter={"types": ["m.room.message"]},
        direction=nio.MessageDirection.back,
    )
    if not isinstance(response, nio.RoomMessagesResponse):
        return 0

    best_count = 0
    for event in response.chunk:
        content = event.source.get("content", {})
        meta = content.get("io.mindroom.thread_summary")
        if not isinstance(meta, dict):
            continue
        relates_to = content.get("m.relates_to")
        if not isinstance(relates_to, dict):
            continue
        if relates_to.get("event_id") != thread_id:
            continue
        count = meta.get("message_count")
        if not isinstance(count, int):
            continue
        best_count = max(best_count, count)
    return best_count


async def _generate_summary(
    thread_history: Sequence[VisibleMessageLike],
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Generate a one-line summary of a thread conversation via LLM."""
    model_name = config.defaults.thread_summary_model or "default"
    model = get_model_instance(config, runtime_paths, model_name)
    agent = Agent(
        name="ThreadSummarizer",
        role="Generate one-line thread summaries",
        model=model,
        output_schema=_ThreadSummary,
    )

    lines = []
    for msg in thread_history:
        sender = visible_message_sender(msg) or "unknown"
        body = visible_message_body(msg) or ""
        if body:
            lines.append(f"{sender}: {body}")
    conversation = "\n".join(lines)

    prompt = (
        "You are a thread summary writer. Your job is to produce a single concise summary "
        "line for a chat thread conversation.\n"
        "\n"
        "RULES:\n"
        "- One line only, under 120 characters\n"
        "- Start with 1-2 emojis that meaningfully represent the topic — the emoji should "
        "help a reader scanning a list of threads instantly understand what the thread is about\n"
        "- Capture the main topic AND the current status or outcome\n"
        "- If the conversation references a ticket, issue number, or any identifier "
        "(e.g. PROJ-123, #42, BUG-7), include it near the start after the emoji\n"
        "- Be consistent: similar threads should produce similar-style summaries\n"
        "- No quotes, no prefixes like 'Summary:', no trailing punctuation\n"
        "\n"
        "GOOD EXAMPLES:\n"
        "- \U0001f41b PROJ-42: fix login crash on expired tokens — merged\n"
        "- \u2705 Database migration to v3 completed successfully\n"
        "- \U0001f4ac Discussing vacation schedule for July\n"
        "- \U0001f527 Nginx config updated for new subdomain\n"
        "- \U0001f6a8 Production outage — root cause identified, fix deployed\n"
        "- \U0001f4e6 #127: add CSV export to reports — in progress\n"
        "- \U0001f3a8 Redesigning sidebar navigation — wireframes shared\n"
        "- \U0001f4b0 Q2 budget review — approved with minor adjustments\n"
        "\n"
        "BAD EXAMPLES (do NOT produce these):\n"
        "- 'Thread about fixing a bug' (too vague, no emoji, quoted)\n"
        "- 'Summary: The team discussed the login issue' (has prefix, no emoji, no outcome)\n"
        "- '\U0001f4ac Discussion' (way too vague, no topic)\n"
        "- '\U0001f41b\U0001f527\u2705\U0001f680\U0001f525 Fixed the thing' (too many emojis, vague)\n"
        "- 'The conversation was about updating the configuration files for nginx' (too long, no emoji, no outcome)\n"
        "\n"
        "Now summarize this thread:\n\n"
        f"{conversation}"
    )
    session_hash = hashlib.sha256(conversation.encode()).hexdigest()[:8]
    response = await cached_agent_run(
        agent=agent,
        full_prompt=prompt,
        session_id=f"thread_summary_{session_hash}",
    )
    content = response.content
    if isinstance(content, _ThreadSummary):
        return content.summary
    return str(content) if content else None


async def _send_summary_event(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    summary: str,
    message_count: int,
    model_name: str,
) -> str | None:
    """Send a thread summary as a standard Matrix notice event."""
    content: dict[str, Any] = {
        "msgtype": "m.notice",
        "body": summary,
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": thread_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": thread_id},
        },
        "io.mindroom.thread_summary": {
            "version": 1,
            "summary": summary,
            "message_count": message_count,
            "generated_at": datetime.now(UTC).isoformat(),
            "model": model_name,
        },
    }
    response = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content,
    )
    if isinstance(response, nio.RoomSendResponse):
        logger.info(
            "Sent thread summary",
            room_id=room_id,
            thread_id=thread_id,
            message_count=message_count,
        )
        return str(response.event_id)
    logger.warning("Failed to send thread summary", room_id=room_id, thread_id=thread_id, response=str(response))
    return None


async def maybe_generate_thread_summary(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    """Generate and send a thread summary if the message count crosses a threshold."""
    thread_history = await fetch_thread_history(client, room_id, thread_id)
    message_count = len(thread_history)

    key = f"{room_id}:{thread_id}"

    # Recover from existing summary events on cache miss (e.g., after restart)
    if key not in _last_summary_counts:
        recovered = await _recover_last_summary_count(client, room_id, thread_id)
        if recovered > 0:
            _last_summary_counts[key] = recovered

    last_count = _last_summary_counts.get(key, 0)
    threshold = _next_threshold(last_count)

    if message_count < threshold:
        return

    try:
        summary = await _generate_summary(thread_history, config, runtime_paths)
    except Exception:
        logger.exception("Thread summary generation failed", room_id=room_id, thread_id=thread_id)
        # Record current count to prevent retry storms until next threshold
        _last_summary_counts[key] = message_count
        return

    if summary is None:
        logger.warning("Thread summary generation returned None", room_id=room_id, thread_id=thread_id)
        # Record current count to prevent retry storms until next threshold
        _last_summary_counts[key] = message_count
        return

    model_name = config.defaults.thread_summary_model or "default"
    # Record count before sending — the LLM cost is already incurred, so don't
    # retry on Matrix send failure (avoids cost amplification loop).
    _last_summary_counts[key] = message_count
    await _send_summary_event(client, room_id, thread_id, summary, message_count, model_name)
