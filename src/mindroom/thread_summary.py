"""AI-generated one-line summaries for Matrix threads."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agno.agent import Agent
from pydantic import BaseModel, Field

from mindroom import model_loading
from mindroom.ai_runtime import cached_agent_run
from mindroom.entity_resolution import resolve_room_scoped_model_override
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.model_instance_checks import isinstance_of_loaded
from mindroom.thread_tag_vocabulary import (
    claim_vocabulary_check,
    format_tag_vocabulary_with_counts,
    load_tag_vocabulary_snapshot,
    maybe_rebuild_tag_vocabulary,
)
from mindroom.thread_tags import coerce_tag_name, get_thread_tags, set_thread_tag
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

logger = get_logger(__name__)
_VERTEXAI_CLAUDE_CLASS = ("agno.models.vertexai.claude", "Claude")
THREAD_SUMMARY_MAX_LENGTH = 300
_MAX_INITIAL_TAGS = 3
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


class ThreadSummaryWriteError(RuntimeError):
    """Raised when a manual thread summary cannot be written."""


@dataclass(frozen=True)
class _ThreadSummaryWriteResult:
    """Successful manual thread summary write details."""

    event_id: str
    message_count: int
    summary: str


class _ThreadSummary(BaseModel):
    """Structured thread summary response."""

    summary: str = Field(
        max_length=THREAD_SUMMARY_MAX_LENGTH,
        description="One-line summary of the thread conversation",
    )


class _ThreadEnrichment(_ThreadSummary):
    """Structured first-summary response with one-shot topic tags."""

    tags: list[str] = Field(
        min_length=1,
        max_length=_MAX_INITIAL_TAGS,
        description="1-3 durable topic tags, most relevant first",
    )


@runtime_checkable
class _SupportsTemperature(Protocol):
    """Protocol for model instances that accept a temperature override."""

    temperature: float | None


def _configure_summary_model_temperature(
    model: object,
    *,
    summary_temperature: float | None,
    model_name: str,
) -> None:
    """Prepare the summary model's temperature setting for one request."""
    if isinstance(model, _SupportsTemperature):
        if isinstance_of_loaded(model, _VERTEXAI_CLAUDE_CLASS):
            # Vertex Claude's rawPredict helper rejects a temperature field entirely.
            model.temperature = None
        else:
            model.temperature = summary_temperature
        return
    if summary_temperature is None:
        return

    model_class = type(model).__name__
    logger.warning(
        f"Thread summary model class {model_class} does not support a runtime temperature override; continuing with provider defaults",
        model_class=model_class,
        model_name=model_name,
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


def _thread_summary_cache_key(room_id: str, thread_id: str) -> str:
    """Return the in-memory cache key for one room/thread pair."""
    return f"{room_id}:{thread_id}"


def _thread_summary_lock(room_id: str, thread_id: str) -> asyncio.Lock:
    """Return the shared per-thread lock for summary writes."""
    return _thread_locks[_thread_summary_cache_key(room_id, thread_id)]


def update_last_summary_count(room_id: str, thread_id: str, message_count: int) -> None:
    """Record the latest summarized message count for one thread monotonically."""
    cache_key = _thread_summary_cache_key(room_id, thread_id)
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


def _count_non_summary_thread_messages(thread_history: Sequence[ResolvedVisibleMessage]) -> int:
    """Count visible thread messages while excluding summary notices."""
    return sum(1 for message in thread_history if not _is_thread_summary_message(message))


def thread_summary_message_count_hint(
    thread_history: Sequence[ResolvedVisibleMessage],
) -> int:
    """Return a lower-bound post-response thread size without refetching history."""
    return _count_non_summary_thread_messages(thread_history) + 1


def _next_thread_summary_threshold(
    room_id: str,
    thread_id: str,
    config: Config,
) -> int:
    """Return the next summary threshold using the current in-memory baseline."""
    return _next_threshold(
        _last_summary_counts.get(_thread_summary_cache_key(room_id, thread_id), 0),
        first_threshold=config.defaults.thread_summary_first_threshold,
        subsequent_interval=config.defaults.thread_summary_subsequent_interval,
    )


def should_queue_thread_summary(
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    message_count_hint: int | None,
) -> bool:
    """Return whether summary generation or room-vocabulary upkeep is due."""
    if message_count_hint is None:
        return True
    threshold = _next_thread_summary_threshold(room_id, thread_id, config)
    return message_count_hint >= threshold - _PREQUEUE_CONCURRENCY_MARGIN or claim_vocabulary_check(
        room_id,
        config,
        runtime_paths,
        now=datetime.now(UTC),
    )


async def _load_thread_history(
    conversation_cache: ConversationCacheProtocol,
    room_id: str,
    thread_id: str,
) -> list[ResolvedVisibleMessage]:
    """Load fresh authoritative history without inherited turn memoization."""
    return list(
        await conversation_cache.get_fresh_strict_thread_history(
            room_id,
            thread_id,
            caller_label="thread_summary_background",
        ),
    )


def _recover_last_summary_count(thread_history: Sequence[ResolvedVisibleMessage]) -> int:
    """Recover the highest durable summary count from authoritative thread history."""
    best_count = 0
    for message in thread_history:
        meta = message.content.get("io.mindroom.thread_summary")
        if not isinstance(meta, dict):
            continue
        count = meta.get("message_count")
        if not isinstance(count, int) or isinstance(count, bool):
            continue
        best_count = max(best_count, count)
    return best_count


def _recover_initial_enrichment_complete(
    thread_history: Sequence[ResolvedVisibleMessage],
) -> bool:
    """Return whether durable summary metadata records completed initial tags."""
    for message in thread_history:
        meta = message.content.get("io.mindroom.thread_summary")
        if isinstance(meta, dict) and meta.get("initial_enrichment_complete") is True:
            return True
    return False


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


def _resolve_thread_summary_model_name(
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str | None,
    *,
    entity_name: str | None = None,
) -> str:
    """Return the model name for automatic thread summaries in one room.

    Precedence: room-scoped override (alias or raw room ID) > responding
    entity's name as a ``room_thread_summary_models`` key (covers ad-hoc
    rooms with no managed alias) > ``defaults.thread_summary_model``.
    """
    if override := resolve_room_scoped_model_override(
        config.room_thread_summary_models,
        room_id,
        runtime_paths,
        allow_raw_room_id=True,
    ):
        return override
    if entity_name and entity_name in config.room_thread_summary_models:
        return config.room_thread_summary_models[entity_name]
    return config.defaults.thread_summary_model or "default"


async def _generate_summary(
    thread_history: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    model_name: str | None = None,
    tag_vocabulary: str | None = None,
) -> str | _ThreadEnrichment | None:
    """Generate a summary and, on the first call, one-shot tags via one LLM run."""
    resolved_model_name = model_name or config.defaults.thread_summary_model or "default"
    model = model_loading.get_model_instance(config, runtime_paths, resolved_model_name)
    _configure_summary_model_temperature(
        model,
        summary_temperature=config.defaults.thread_summary_temperature,
        model_name=resolved_model_name,
    )

    conversation = escape(_build_conversation_text(thread_history))
    prompt = config.render_prompt(
        "THREAD_SUMMARY_USER_PROMPT_TEMPLATE",
        conversation=conversation,
        tag_vocabulary=(
            escape(tag_vocabulary)
            if tag_vocabulary is not None
            else "(tags are not requested for this summary refresh)"
        ),
    )
    session_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    agent = Agent(
        name="ThreadSummarizer",
        instructions=config.get_prompt("THREAD_SUMMARY_INSTRUCTIONS").splitlines(),
        model=model,
        output_schema=_ThreadEnrichment if tag_vocabulary is not None else _ThreadSummary,
        telemetry=False,
    )
    response = await cached_agent_run(
        agent=agent,
        run_input=prompt,
        session_id=f"thread_summary_{session_hash}",
    )
    content = response.content
    if tag_vocabulary is not None:
        if not isinstance(content, _ThreadEnrichment):
            return None
        normalized_tags: list[str] = []
        for raw_tag in content.tags:
            normalized_tag = coerce_tag_name(raw_tag)
            if normalized_tag is not None and normalized_tag not in normalized_tags:
                normalized_tags.append(normalized_tag)
        if not normalized_tags:
            return None
        return _ThreadEnrichment(
            summary=content.summary,
            tags=normalized_tags[:_MAX_INITIAL_TAGS],
        )
    if isinstance(content, _ThreadEnrichment):
        return None
    if isinstance(content, _ThreadSummary):
        return content.summary
    return None


@timed("maybe_generate_thread_summary")
async def _timed_generate_summary(
    thread_history: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    model_name: str | None = None,
    tag_vocabulary: str | None = None,
) -> str | _ThreadEnrichment | None:
    """Run the summary generation attempt with timing instrumentation."""
    return await _generate_summary(
        thread_history,
        config,
        runtime_paths,
        model_name=model_name,
        tag_vocabulary=tag_vocabulary,
    )


async def _apply_initial_tags(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    tags: Sequence[str],
) -> bool:
    """Apply generated tags only when no manual or concurrent tags exist."""
    set_by = client.user_id
    if not tags or not set_by:
        return False
    try:
        existing_state = await get_thread_tags(client, room_id, thread_id)
    except Exception:
        logger.exception(
            "Failed to check existing thread tags; skipping automatic tags",
            room_id=room_id,
            thread_id=thread_id,
        )
        return False
    if existing_state is not None and existing_state.tags:
        logger.info(
            "Skipping automatic tags because the thread already has tags",
            room_id=room_id,
            thread_id=thread_id,
        )
        return True

    applied_tags: list[str] = []
    for tag in tags:
        try:
            await set_thread_tag(client, room_id, thread_id, tag, set_by=set_by)
        except Exception:
            logger.exception(
                "Failed to write automatic thread tag",
                room_id=room_id,
                thread_id=thread_id,
                tag=tag,
            )
            continue
        applied_tags.append(tag)
    if applied_tags:
        logger.info(
            "Automatically tagged thread",
            room_id=room_id,
            thread_id=thread_id,
            tags=applied_tags,
        )
    return bool(applied_tags)


async def _refresh_tag_vocabulary(
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Refresh vocabulary and return text when the check already loaded a snapshot."""
    try:
        snapshot = await maybe_rebuild_tag_vocabulary(
            client,
            room_id,
            config,
            runtime_paths,
            now=datetime.now(UTC),
        )
    except Exception:
        logger.exception("Tag vocabulary rebuild failed", room_id=room_id)
        return None
    if snapshot is None:
        return None
    return format_tag_vocabulary_with_counts(snapshot)


async def _deliver_generated_summary(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    generated: str | _ThreadEnrichment,
    normalized_summary: str,
    message_count: int,
    model_name: str,
    conversation_cache: ConversationCacheProtocol,
) -> None:
    """Apply initial tags, then independently deliver the generated summary."""
    initial_enrichment_complete: bool | None = None
    if isinstance(generated, _ThreadEnrichment):
        initial_enrichment_complete = await _apply_initial_tags(
            client,
            room_id,
            thread_id,
            generated.tags,
        )

    try:
        await send_thread_summary_event(
            client,
            room_id,
            thread_id,
            normalized_summary,
            message_count,
            model_name,
            conversation_cache,
            initial_enrichment_complete=initial_enrichment_complete,
        )
    except Exception:
        logger.exception("Thread summary send failed", room_id=room_id, thread_id=thread_id)


async def send_thread_summary_event(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    summary: str,
    message_count: int,
    model_name: str,
    conversation_cache: ConversationCacheProtocol,
    *,
    initial_enrichment_complete: bool | None = None,
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
    try:
        latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
            room_id,
            thread_id,
            caller_label="thread_summary_send",
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
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model_name,
    }
    if initial_enrichment_complete is not None:
        summary_metadata["initial_enrichment_complete"] = initial_enrichment_complete

    content = build_message_content(
        truncated_summary,
        thread_event_id=thread_id,
        latest_thread_event_id=latest_thread_event_id or thread_id,
        extra_content={
            "msgtype": "m.notice",
            "io.mindroom.thread_summary": summary_metadata,
        },
    )
    delivered = await send_message_result(client, room_id, content)
    if delivered is not None:
        conversation_cache.notify_outbound_message(
            room_id,
            delivered.event_id,
            delivered.content_sent,
        )
        logger.info(
            "Sent thread summary",
            room_id=room_id,
            thread_id=thread_id,
            message_count=message_count,
        )
        return delivered.event_id
    logger.warning("Failed to send thread summary", room_id=room_id, thread_id=thread_id)
    return None


async def set_manual_thread_summary(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    summary: str,
    *,
    conversation_cache: ConversationCacheProtocol,
) -> _ThreadSummaryWriteResult:
    """Write one validated manual summary for a canonical thread root."""
    if not isinstance(summary, str) or not summary.strip():
        msg = "summary must be a non-empty string."
        raise ThreadSummaryWriteError(msg)

    normalized_summary = normalize_thread_summary_text(summary)
    if not normalized_summary:
        msg = "summary must be a non-empty string."
        raise ThreadSummaryWriteError(msg)
    if len(normalized_summary) > THREAD_SUMMARY_MAX_LENGTH:
        msg = f"summary must be {THREAD_SUMMARY_MAX_LENGTH} characters or fewer after whitespace normalization."
        raise ThreadSummaryWriteError(msg)

    async with _thread_summary_lock(room_id, thread_id):
        try:
            thread_history = await _load_thread_history(
                conversation_cache,
                room_id,
                thread_id,
            )
        except Exception as exc:
            msg = "Failed to fetch thread history for the target thread."
            raise ThreadSummaryWriteError(msg) from exc

        message_count = _count_non_summary_thread_messages(thread_history)
        try:
            event_id = await send_thread_summary_event(
                client,
                room_id,
                thread_id,
                normalized_summary,
                message_count,
                "manual",
                conversation_cache,
            )
        except Exception as exc:
            msg = "Failed to send thread summary event."
            raise ThreadSummaryWriteError(msg) from exc
        if event_id is None:
            msg = "Failed to send thread summary event."
            raise ThreadSummaryWriteError(msg)

        update_last_summary_count(room_id, thread_id, message_count)
        return _ThreadSummaryWriteResult(
            event_id=event_id,
            message_count=message_count,
            summary=normalized_summary,
        )


async def maybe_generate_thread_summary(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    conversation_cache: ConversationCacheProtocol,
    entity_name: str | None = None,
) -> None:
    """Generate a summary and one-shot initial tags when a threshold is crossed."""
    refreshed_tag_vocabulary = await _refresh_tag_vocabulary(client, room_id, config, runtime_paths)
    async with _thread_summary_lock(room_id, thread_id):
        # This background task inherits the response turn's ContextVars, so it
        # must bypass per-turn memoization to observe the delivered response.
        try:
            thread_history = await _load_thread_history(conversation_cache, room_id, thread_id)
        except Exception:
            logger.exception(
                "Authoritative thread history load failed",
                room_id=room_id,
                thread_id=thread_id,
            )
            return
        recovered_summary_count = _recover_last_summary_count(thread_history)
        if recovered_summary_count > 0:
            update_last_summary_count(room_id, thread_id, recovered_summary_count)

        threshold = _next_thread_summary_threshold(room_id, thread_id, config)
        message_count = _count_non_summary_thread_messages(thread_history)
        if message_count < threshold:
            return

        initial_enrichment = not _recover_initial_enrichment_complete(thread_history)
        tag_vocabulary = None
        if initial_enrichment:
            tag_vocabulary = refreshed_tag_vocabulary or format_tag_vocabulary_with_counts(
                load_tag_vocabulary_snapshot(runtime_paths, room_id),
            )
        try:
            model_name = _resolve_thread_summary_model_name(
                config,
                runtime_paths,
                room_id,
                entity_name=entity_name,
            )
            generated = await _timed_generate_summary(
                thread_history,
                config,
                runtime_paths,
                model_name=model_name,
                tag_vocabulary=tag_vocabulary,
            )
        except Exception:
            logger.exception("Thread summary generation failed", room_id=room_id, thread_id=thread_id)
            # Record current count to prevent retry storms until next threshold
            update_last_summary_count(room_id, thread_id, message_count)
            return

        if generated is None:
            logger.warning("Thread summary generation returned None", room_id=room_id, thread_id=thread_id)
            # Record current count to prevent retry storms until next threshold
            update_last_summary_count(room_id, thread_id, message_count)
            return

        summary = generated.summary if isinstance(generated, _ThreadEnrichment) else generated
        normalized_summary = normalize_thread_summary_text(summary)
        if not normalized_summary:
            logger.warning(
                "Thread summary generation returned no plain-text content",
                room_id=room_id,
                thread_id=thread_id,
            )
            update_last_summary_count(room_id, thread_id, message_count)
            return

        await _deliver_generated_summary(
            client,
            room_id,
            thread_id,
            generated,
            normalized_summary,
            message_count,
            model_name,
            conversation_cache,
        )
        # Record after the delivery attempt so cancellation cannot leave a
        # partially delivered initial enrichment marked complete.
        update_last_summary_count(room_id, thread_id, message_count)
