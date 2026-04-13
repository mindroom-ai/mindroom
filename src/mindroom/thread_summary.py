"""AI-generated one-line summaries for Matrix threads."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import nio
from agno.agent import Agent
from pydantic import BaseModel, Field

from mindroom.ai import cached_agent_run, get_model_instance
from mindroom.logging_config import get_logger
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.conversation_access import ConversationReadAccess

logger = get_logger(__name__)
THREAD_SUMMARY_MAX_LENGTH = 300
_MARKDOWN_LINK_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)|\[([^\]]+)\]\([^)]+\)")
_MARKDOWN_CODE_BLOCK_RE = re.compile(r"```(?:[^\n`]*)\n?(.*?)```", re.DOTALL)
_MARKDOWN_DOUBLE_EMPHASIS_RE = re.compile(r"(\*\*|__)(.*?)\1", re.DOTALL)
_MARKDOWN_SINGLE_ASTERISK_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)
_MARKDOWN_STRIKETHROUGH_RE = re.compile(r"~~(.*?)~~", re.DOTALL)
_MARKDOWN_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_MARKDOWN_BLOCKQUOTE_RE = re.compile(r"(?m)^\s{0,3}>\s?")
_MARKDOWN_LIST_ITEM_RE = re.compile(r"(?m)^\s*(?:[-+*]|\d+\.)\s+")
_PREQUEUE_CONCURRENCY_MARGIN = 2

# In-memory tracking of last summarized message count per thread.
# Key: "{room_id}:{thread_id}", value: message count at last summary.
_last_summary_counts: dict[str, int] = {}
_thread_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class _ThreadSummary(BaseModel):
    """Structured thread summary response."""

    summary: str = Field(
        max_length=THREAD_SUMMARY_MAX_LENGTH,
        description="One-line summary of the thread conversation",
    )


def normalize_thread_summary_text(raw_text: str) -> str:
    """Strip common markdown formatting and collapse the result to one plain-text line."""
    normalized = raw_text.strip()
    if not normalized:
        return ""

    normalized = _MARKDOWN_CODE_BLOCK_RE.sub(r"\1", normalized)
    normalized = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1) or match.group(2) or "", normalized)
    normalized = _MARKDOWN_HEADING_RE.sub("", normalized)
    normalized = _MARKDOWN_BLOCKQUOTE_RE.sub("", normalized)
    normalized = _MARKDOWN_LIST_ITEM_RE.sub("", normalized)
    normalized = _MARKDOWN_DOUBLE_EMPHASIS_RE.sub(r"\2", normalized)
    normalized = _MARKDOWN_SINGLE_ASTERISK_RE.sub(r"\1", normalized)
    normalized = _MARKDOWN_STRIKETHROUGH_RE.sub(r"\1", normalized)
    normalized = _MARKDOWN_INLINE_CODE_RE.sub(r"\1", normalized)
    return " ".join(normalized.split())


def thread_summary_cache_key(room_id: str, thread_id: str) -> str:
    """Return the in-memory cache key for one room/thread pair."""
    return f"{room_id}:{thread_id}"


def thread_summary_lock(room_id: str, thread_id: str) -> asyncio.Lock:
    """Return the shared per-thread lock for summary writes."""
    return _thread_locks[thread_summary_cache_key(room_id, thread_id)]


def update_last_summary_count(room_id: str, thread_id: str, message_count: int) -> None:
    """Record the latest summarized message count for one thread monotonically."""
    cache_key = thread_summary_cache_key(room_id, thread_id)
    existing_count = _last_summary_counts.get(cache_key, 0)
    if message_count > existing_count:
        _last_summary_counts[cache_key] = message_count


def _next_threshold(
    last_summarized_count: int,
    *,
    first_threshold: int,
    subsequent_interval: int,
) -> int:
    """Return the next message count at which a summary should be generated."""
    if last_summarized_count <= 0:
        return first_threshold
    return last_summarized_count + subsequent_interval


def _is_thread_summary_message(message: ResolvedVisibleMessage) -> bool:
    """Return whether a visible thread message is itself a summary notice."""
    return isinstance(message.content.get("io.mindroom.thread_summary"), dict)


def _count_non_summary_messages(thread_history: Sequence[ResolvedVisibleMessage]) -> int:
    """Count visible thread messages while excluding summary notices."""
    return sum(1 for message in thread_history if not _is_thread_summary_message(message))


def thread_summary_message_count_hint(
    thread_history: Sequence[ResolvedVisibleMessage],
) -> int:
    """Return a lower-bound post-response thread size without refetching history."""
    return _count_non_summary_messages(thread_history) + 1
def next_thread_summary_threshold(
    room_id: str,
    thread_id: str,
    config: Config,
) -> int:
    """Return the next summary threshold using the current in-memory baseline."""
    return _next_threshold(
        _last_summary_counts.get(thread_summary_cache_key(room_id, thread_id), 0),
        first_threshold=config.defaults.thread_summary_first_threshold,
        subsequent_interval=config.defaults.thread_summary_subsequent_interval,
    )


def should_queue_thread_summary(
    room_id: str,
    thread_id: str,
    config: Config,
    *,
    message_count_hint: int | None,
) -> bool:
    """Return whether the lower-bound hint is close enough to justify a live recheck."""
    if message_count_hint is None:
        return True
    threshold = next_thread_summary_threshold(room_id, thread_id, config)
    return message_count_hint >= threshold - _PREQUEUE_CONCURRENCY_MARGIN


async def _load_thread_history(
    conversation_access: ConversationReadAccess,
    room_id: str,
    thread_id: str,
) -> list[ResolvedVisibleMessage]:
    """Load thread history through the explicit conversation-access seam."""
    return list(await conversation_access.get_thread_history(room_id, thread_id))


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


_SUMMARY_INSTRUCTIONS = [
    "You are a thread summary writer. Produce a single concise summary line for a chat thread conversation.",
    "",
    "RULES:",
    "- One line only, under 120 characters",
    "- Start with 1-2 emojis that meaningfully represent the topic — the emoji should "
    "help a reader scanning a list of threads instantly understand what the thread is about",
    "- Capture the main topic AND the current status or outcome",
    "- Plain text only — no markdown formatting (no bold, headers, bullets, links)",
    "- If the conversation references a ticket, issue number, or any identifier "
    "(e.g. PROJ-123, #42, BUG-7), include it near the start after the emoji",
    "- Be consistent: similar threads should produce similar-style summaries",
    "- No quotes, no prefixes like 'Summary:', no trailing punctuation",
    "- Write a NOVEL summary in your own words. Do NOT copy, quote, or truncate any "
    "message from the thread. Synthesize the key topic and outcome.",
    "",
    "GOOD EXAMPLES:",
    "- \U0001f41b PROJ-42: fix login crash on expired tokens — merged",
    "- \u2705 Database migration to v3 completed successfully",
    "- \U0001f4ac Discussing vacation schedule for July",
    "- \U0001f527 Nginx config updated for new subdomain",
    "- \U0001f6a8 Production outage — root cause identified, fix deployed",
    "- \U0001f4e6 #127: add CSV export to reports — in progress",
    "- \U0001f3a8 Redesigning sidebar navigation — wireframes shared",
    "- \U0001f4b0 Q2 budget review — approved with minor adjustments",
    "",
    "BAD EXAMPLES (do NOT produce these):",
    "- 'Thread about fixing a bug' (too vague, no emoji, quoted)",
    "- 'Summary: The team discussed the login issue' (has prefix, no emoji, no outcome)",
    "- '\U0001f4ac Discussion' (way too vague, no topic)",
    "- '\U0001f41b\U0001f527\u2705\U0001f680\U0001f525 Fixed the thing' (too many emojis, vague)",
    "- 'The conversation was about updating the configuration files for nginx' (too long, no emoji, no outcome)",
    "- 'I wanted to discuss the implementation plan for the new auth system' "
    "(verbatim copy of a message — summarize the topic, don't quote it)",
]

_MAX_MESSAGES_BEFORE_TRUNCATION = 50
_TRUNCATION_SAMPLE_SIZE = 3


def _build_conversation_text(thread_history: Sequence[ResolvedVisibleMessage]) -> str:
    """Build conversation text from thread history.

    Prior thread summary notices (``io.mindroom.thread_summary``) are excluded
    so they don't pollute the conversation.

    For threads exceeding ``_MAX_MESSAGES_BEFORE_TRUNCATION`` messages, samples
    the first and last few messages with an omission note in between.
    """
    lines: list[str] = []
    for msg in thread_history:
        if _is_thread_summary_message(msg):
            continue
        sender = msg.sender or "unknown"
        body = msg.body or ""
        if body:
            lines.append(f"{sender}: {body}")

    if len(lines) > _MAX_MESSAGES_BEFORE_TRUNCATION:
        n = _TRUNCATION_SAMPLE_SIZE
        omitted = len(lines) - 2 * n
        lines = [*lines[:n], f"[... {omitted} messages omitted ...]", *lines[-n:]]

    return "\n".join(lines)


async def _generate_summary(
    thread_history: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Generate a one-line summary of a thread conversation via LLM."""
    model_name = config.defaults.thread_summary_model or "default"
    model = get_model_instance(config, runtime_paths, model_name)

    conversation = _build_conversation_text(thread_history)
    session_hash = hashlib.sha256(conversation.encode()).hexdigest()[:8]

    prompt = f"<thread_messages>\n{conversation}\n</thread_messages>\n\nSummarize the above thread."
    agent = Agent(
        name="ThreadSummarizer",
        instructions=list(_SUMMARY_INSTRUCTIONS),
        model=model,
        output_schema=_ThreadSummary,
    )
    response = await cached_agent_run(
        agent=agent,
        full_prompt=prompt,
        session_id=f"thread_summary_{session_hash}",
    )
    content = response.content
    if isinstance(content, _ThreadSummary):
        return content.summary
    return str(content) if content else None


@timed("maybe_generate_thread_summary")
async def _timed_generate_summary(
    thread_history: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Run the summary generation attempt with timing instrumentation."""
    return await _generate_summary(thread_history, config, runtime_paths)


async def send_thread_summary_event(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    summary: str,
    message_count: int,
    model_name: str,
) -> str | None:
    """Send a thread summary as a standard Matrix notice event."""
    normalized_summary = normalize_thread_summary_text(summary)
    if not normalized_summary:
        logger.warning(
            "Refusing to send empty normalized thread summary",
            room_id=room_id,
            thread_id=thread_id,
            message_count=message_count,
        )
        return None

    truncated_summary = (
        normalized_summary[: THREAD_SUMMARY_MAX_LENGTH - 3] + "..."
        if len(normalized_summary) > THREAD_SUMMARY_MAX_LENGTH
        else normalized_summary
    )
    content: dict[str, Any] = {
        "msgtype": "m.notice",
        "body": truncated_summary,
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": thread_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": thread_id},
        },
        "io.mindroom.thread_summary": {
            "version": 1,
            "summary": truncated_summary,
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
    *,
    conversation_access: ConversationReadAccess,
    message_count_hint: int | None = None,
) -> None:
    """Generate and send a thread summary if the message count crosses a threshold."""
    async with thread_summary_lock(room_id, thread_id):
        cache_key = thread_summary_cache_key(room_id, thread_id)
        # Recover from existing summary events on cache miss (e.g., after restart)
        if cache_key not in _last_summary_counts:
            recovered = await _recover_last_summary_count(client, room_id, thread_id)
            if recovered > 0:
                update_last_summary_count(room_id, thread_id, recovered)

        threshold = next_thread_summary_threshold(room_id, thread_id, config)

        # message_count_hint comes from a pre-send snapshot and is only a
        # lower bound. Other agents or humans can post before this background
        # task runs, so a stale hint must never suppress the live re-fetch.
        thread_history = await _load_thread_history(conversation_access, room_id, thread_id)
        message_count = _count_non_summary_messages(thread_history)
        if message_count_hint is not None:
            message_count = max(message_count, message_count_hint)
        if message_count < threshold:
            return
        try:
            summary = await _timed_generate_summary(thread_history, config, runtime_paths)
        except Exception:
            logger.exception("Thread summary generation failed", room_id=room_id, thread_id=thread_id)
            # Record current count to prevent retry storms until next threshold
            update_last_summary_count(room_id, thread_id, message_count)
            return

        if summary is None:
            logger.warning("Thread summary generation returned None", room_id=room_id, thread_id=thread_id)
            # Record current count to prevent retry storms until next threshold
            update_last_summary_count(room_id, thread_id, message_count)
            return

        normalized_summary = normalize_thread_summary_text(summary)
        if not normalized_summary:
            logger.warning(
                "Thread summary generation returned no plain-text content",
                room_id=room_id,
                thread_id=thread_id,
            )
            update_last_summary_count(room_id, thread_id, message_count)
            return

        model_name = config.defaults.thread_summary_model or "default"
        # Record count before sending — the LLM cost is already incurred, so don't
        # retry on Matrix send failure (avoids cost amplification loop).
        update_last_summary_count(room_id, thread_id, message_count)
        await send_thread_summary_event(client, room_id, thread_id, normalized_summary, message_count, model_name)
