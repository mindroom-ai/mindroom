"""Summary notices: sending one, ordering it after its predecessors, and the manual and welcome writes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mindroom.constants import THREAD_SUMMARY_CONTENT_KEY
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.thread_summary import schedule
from mindroom.thread_summary.state import (
    THREAD_SUMMARY_MAX_LENGTH,
    TruncatedThreadHistoryError,
    current_thread_summary_from_history,
    load_complete_thread_history,
)

if TYPE_CHECKING:
    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_reads import ConversationReader

logger = get_logger(__name__)
_MARKDOWN_LINK_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)|\[([^\]]+)\]\([^)]+\)")
_MARKDOWN_CODE_BLOCK_RE = re.compile(r"```(?:[^\n`]*)\n?(.*?)```", re.DOTALL)
_MARKDOWN_DOUBLE_EMPHASIS_RE = re.compile(r"(\*\*|__)(.*?)\1", re.DOTALL)
_MARKDOWN_SINGLE_ASTERISK_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)
_MARKDOWN_STRIKETHROUGH_RE = re.compile(r"~~(.*?)~~", re.DOTALL)
_MARKDOWN_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_MARKDOWN_BLOCKQUOTE_RE = re.compile(r"(?m)^\s{0,3}>\s?")
_MARKDOWN_LIST_ITEM_RE = re.compile(r"(?m)^\s*(?:[-+*]|\d+\.)\s+")


class ThreadSummaryWriteError(RuntimeError):
    """Raised when a manual thread summary cannot be written."""


@dataclass(frozen=True)
class _ThreadSummaryWriteResult:
    """Successful manual thread summary write details."""

    event_id: str
    message_count: int
    summary: str


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


def summary_generated_at(*predecessors: datetime | None) -> datetime:
    """Order a new summary after authorized predecessors despite clock skew."""
    generated_at = datetime.now(UTC)
    newest = max((predecessor for predecessor in predecessors if predecessor is not None), default=None)
    if newest is None or newest < generated_at:
        return generated_at
    try:
        return newest + timedelta(milliseconds=1)
    except OverflowError as exc:
        msg = "The existing summary timestamp is too large to replace."
        raise ThreadSummaryWriteError(msg) from exc


async def send_thread_summary_notice(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    summary: str,
    message_count: int,
    model_name: str,
    conversation_reader: ConversationReader,
    *,
    initial_enrichment_complete: bool | None = None,
    pinned: bool | None = None,
    generated_at: datetime | None = None,
) -> str | None:
    """Send a thread summary as a standard Matrix notice event.

    ``pinned`` records an explicit decision about whether this summary should
    stop automatic re-summarization. It defaults to ``None``, which omits the
    key entirely and leaves any existing pin decision untouched. Only callers
    acting on a user's intent should pass a boolean: writers that summarize as a
    side effect must not disturb pin state on a thread the user already pinned.
    """
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
    try:
        latest_thread_event_id = await conversation_reader.latest_thread_event_id(
            room_id=room_id,
            thread_id=thread_id,
        )
    except Exception as exc:
        logger.warning(
            "Falling back to thread root for summary send after latest-event lookup failure",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        latest_thread_event_id = None
    summary_metadata: dict[str, object] = {
        "version": 1,
        "summary": truncated_summary,
        "message_count": message_count,
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "model": model_name,
    }
    if initial_enrichment_complete is not None:
        summary_metadata["initial_enrichment_complete"] = initial_enrichment_complete
    if pinned is not None:
        summary_metadata["pinned"] = pinned

    content = build_message_content(
        truncated_summary,
        thread_event_id=thread_id,
        latest_thread_event_id=latest_thread_event_id or thread_id,
        extra_content={
            "msgtype": "m.notice",
            THREAD_SUMMARY_CONTENT_KEY: summary_metadata,
        },
    )
    delivered = await send_message_result(client, room_id, content)
    if delivered is not None:
        logger.info(
            "Sent thread summary",
            room_id=room_id,
            thread_id=thread_id,
            message_count=message_count,
        )
        return delivered.event_id
    logger.warning("Failed to send thread summary", room_id=room_id, thread_id=thread_id)
    return None


async def send_welcome_thread_summary(
    client: nio.AsyncClient,
    room_id: str,
    welcome_event_id: str,
    conversation_reader: ConversationReader,
) -> None:
    """Title the router welcome without counting it toward automatic summaries.

    A zero count keeps the first reply in the welcome thread due for an
    automatic summary, which then replaces this unpinned title.
    """
    await send_thread_summary_notice(
        client,
        room_id,
        welcome_event_id,
        "Welcome to MindRoom",
        0,
        "manual",
        conversation_reader,
    )


async def set_manual_thread_summary(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    summary: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_reader: ConversationReader,
    entity_name: str,
    membership_index: AgentReplyMembershipIndex,
    pin: bool = True,
) -> _ThreadSummaryWriteResult:
    """Write one validated manual summary for a canonical thread root.

    Pins the thread by default so the written title survives automatic
    re-summarization. ``pin=False`` writes the summary and releases any pin an
    earlier manual write established.
    """
    if not isinstance(summary, str) or not (normalized_summary := normalize_thread_summary_text(summary)):
        msg = "summary must be a non-empty string."
        raise ThreadSummaryWriteError(msg)
    if len(normalized_summary) > THREAD_SUMMARY_MAX_LENGTH:
        msg = f"summary must be {THREAD_SUMMARY_MAX_LENGTH} characters or fewer after whitespace normalization."
        raise ThreadSummaryWriteError(msg)

    baselines = schedule.THREAD_SUMMARY_BASELINES
    async with baselines.lock(room_id, thread_id):
        try:
            thread_history = await load_complete_thread_history(conversation_reader, room_id, thread_id)
        except TruncatedThreadHistoryError as exc:
            msg = "Thread history is longer than one page, so its message count cannot be established."
            raise ThreadSummaryWriteError(msg) from exc
        except Exception as exc:
            msg = "Failed to fetch thread history for the target thread."
            raise ThreadSummaryWriteError(msg) from exc

        state = current_thread_summary_from_history(
            client,
            room_id,
            thread_id,
            thread_history,
            config=config,
            runtime_paths=runtime_paths,
            entity_name=entity_name,
            membership_index=membership_index,
            trusted_sender_ids=current_internal_sender_ids(config, runtime_paths),
        )
        if state is None:
            msg = "Summary authorization awaits room membership; retry after membership is available."
            raise ThreadSummaryWriteError(msg)
        generated_at = summary_generated_at(state.newest_generated_at)
        try:
            event_id = await send_thread_summary_notice(
                client,
                room_id,
                thread_id,
                normalized_summary,
                state.message_count,
                "manual",
                conversation_reader,
                pinned=pin,
                generated_at=generated_at,
            )
        except Exception as exc:
            msg = "Failed to send thread summary event."
            raise ThreadSummaryWriteError(msg) from exc
        if event_id is None:
            msg = "Failed to send thread summary event."
            raise ThreadSummaryWriteError(msg)

        baselines.record(room_id, thread_id, state.message_count)
        return _ThreadSummaryWriteResult(
            event_id=event_id,
            message_count=state.message_count,
            summary=normalized_summary,
        )
