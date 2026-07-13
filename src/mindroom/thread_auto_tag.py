"""One-shot cheap-model auto-tagging for new Matrix threads.

Mirrors the thread-summary sidecar: queued fire-and-forget after a response
delivers, it tags each thread at most once — right after the first user
message and agent reply exist — using the daily tag-vocabulary snapshot to
strongly prefer tags already in use.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from agno.agent import Agent
from pydantic import BaseModel, Field

from mindroom import model_loading
from mindroom.ai_runtime import cached_agent_run
from mindroom.logging_config import get_logger
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.thread_summary import count_non_summary_thread_messages, is_thread_summary_message
from mindroom.thread_tag_vocabulary import (
    claim_vocabulary_check,
    format_tag_vocabulary_with_counts,
    load_tag_vocabulary_snapshot,
    maybe_rebuild_tag_vocabulary,
)
from mindroom.thread_tags import ThreadTagsError, coerce_tag_name, get_thread_tags, set_thread_tag

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

logger = get_logger(__name__)

_MAX_AUTO_TAGS = 3
_FIRST_MESSAGE_MAX_CHARS = 500
# Lower-bound thread size (history + this response) up to which a thread still
# counts as a first exchange. Allows one coalesced extra user message.
_FIRST_EXCHANGE_MAX_COUNT_HINT = 3
_MAX_DONE_THREAD_MARKERS = 4096

# Threads this process already auto-tagged or confirmed as tagged.
_auto_tagged_threads: OrderedDict[str, None] = OrderedDict()
_thread_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


class _ThreadAutoTags(BaseModel):
    """Structured auto-tagger response."""

    tags: list[str] = Field(
        max_length=_MAX_AUTO_TAGS,
        description="1-3 topic tags for the thread, most relevant first",
    )


def _thread_key(room_id: str, thread_id: str) -> str:
    return f"{room_id}:{thread_id}"


def _thread_lock(thread_key: str) -> asyncio.Lock:
    """Return the shared live auto-tag lock for one thread."""
    lock = _thread_locks.get(thread_key)
    if lock is None:
        lock = asyncio.Lock()
        _thread_locks[thread_key] = lock
    return lock


def _thread_marked_done(thread_key: str) -> bool:
    if thread_key not in _auto_tagged_threads:
        return False
    _auto_tagged_threads.move_to_end(thread_key)
    return True


def _mark_thread_done(thread_key: str) -> None:
    """Remember one completed thread while bounding process-lifetime state."""
    _auto_tagged_threads[thread_key] = None
    _auto_tagged_threads.move_to_end(thread_key)
    while len(_auto_tagged_threads) > _MAX_DONE_THREAD_MARKERS:
        _auto_tagged_threads.popitem(last=False)


def should_queue_thread_auto_tag(
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    message_count_hint: int | None,
) -> bool:
    """Return whether the auto-tag background task is worth queueing.

    Purely in-memory: queue when the thread may still need its one-shot
    tagging pass, or when the daily vocabulary snapshot may be due a rebuild.
    """
    if claim_vocabulary_check(
        room_id,
        config,
        runtime_paths,
        now=datetime.now(UTC),
    ):
        return True
    if _thread_marked_done(_thread_key(room_id, thread_id)):
        return False
    return message_count_hint is None or message_count_hint <= _FIRST_EXCHANGE_MAX_COUNT_HINT


def _first_thread_message_text(thread_history: Sequence[ResolvedVisibleMessage]) -> str | None:
    """Return the truncated opening message of a thread, skipping summary notices."""
    for message in thread_history:
        if is_thread_summary_message(message):
            continue
        body = (message.body or "").strip()
        if body:
            return body[:_FIRST_MESSAGE_MAX_CHARS]
    return None


def _resolve_auto_tag_model_name(config: Config) -> str:
    """Return the model config name for auto-tagging."""
    return config.defaults.thread_auto_tag_model or config.defaults.thread_summary_model or "default"


async def _generate_tags(
    first_message: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[str]:
    """Generate 1-3 coerced tags for one thread opening via the cheap model."""
    model_name = _resolve_auto_tag_model_name(config)
    model = model_loading.get_model_instance(config, runtime_paths, model_name)

    vocabulary_text = format_tag_vocabulary_with_counts(
        load_tag_vocabulary_snapshot(runtime_paths, room_id),
    )
    prompt = config.render_prompt(
        "THREAD_AUTO_TAG_USER_PROMPT_TEMPLATE",
        tag_vocabulary=vocabulary_text,
        first_message=escape(first_message),
    )
    session_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    agent = Agent(
        name="ThreadAutoTagger",
        instructions=config.get_prompt("THREAD_AUTO_TAG_INSTRUCTIONS").splitlines(),
        model=model,
        output_schema=_ThreadAutoTags,
        telemetry=False,
    )
    response = await cached_agent_run(
        agent=agent,
        run_input=prompt,
        session_id=f"thread_auto_tag_{session_hash}",
    )
    content = response.content
    if not isinstance(content, _ThreadAutoTags):
        return []

    coerced_tags: list[str] = []
    for raw_tag in content.tags:
        tag = coerce_tag_name(raw_tag)
        if tag is not None and tag not in coerced_tags:
            coerced_tags.append(tag)
    return coerced_tags[:_MAX_AUTO_TAGS]


async def _apply_auto_tags(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    tags: Sequence[str],
    *,
    set_by: str,
) -> list[str]:
    """Write generated tags, continuing past individual write failures."""
    applied_tags: list[str] = []
    for tag in tags:
        try:
            await set_thread_tag(client, room_id, thread_id, tag, set_by=set_by)
        except ThreadTagsError as exc:
            logger.warning(
                "Failed to write auto tag",
                room_id=room_id,
                thread_id=thread_id,
                tag=tag,
                error=str(exc),
            )
            continue
        applied_tags.append(tag)
    return applied_tags


async def _authoritative_thread_history(
    room_id: str,
    thread_id: str,
    thread_key: str,
    conversation_cache: ConversationCacheProtocol,
) -> Sequence[ResolvedVisibleMessage] | None:
    """Return authoritative history only while the thread is a first exchange."""
    thread_history = await conversation_cache.get_strict_thread_history(
        room_id,
        thread_id,
        caller_label="thread_auto_tag_background",
    )
    if not thread_history.is_full_history or is_thread_history_degraded(thread_history):
        logger.warning(
            "Skipping thread auto-tag because authoritative history is unavailable",
            room_id=room_id,
            thread_id=thread_id,
        )
        return None
    if count_non_summary_thread_messages(thread_history) > _FIRST_EXCHANGE_MAX_COUNT_HINT:
        _mark_thread_done(thread_key)
        return None
    return thread_history


async def _authoritative_first_message(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    thread_key: str,
    conversation_cache: ConversationCacheProtocol,
) -> str | None:
    """Return the opening text only while the thread is authoritatively eligible."""
    existing_state = await get_thread_tags(client, room_id, thread_id)
    if existing_state is not None and existing_state.tags:
        _mark_thread_done(thread_key)
        return None

    thread_history = await _authoritative_thread_history(
        room_id,
        thread_id,
        thread_key,
        conversation_cache,
    )
    if thread_history is None:
        return None
    return _first_thread_message_text(thread_history)


async def _generate_thread_tags_once(
    first_message: str,
    room_id: str,
    thread_id: str,
    thread_key: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[str] | None:
    """Run the model once, returning None only when generation failed."""
    try:
        tags = await _generate_tags(first_message, room_id, config, runtime_paths)
    except Exception:
        logger.exception("Thread auto-tag generation failed", room_id=room_id, thread_id=thread_id)
        _mark_thread_done(thread_key)
        return None
    _mark_thread_done(thread_key)
    return tags


async def _auto_tag_thread_once(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    conversation_cache: ConversationCacheProtocol,
) -> None:
    """Run the once-per-thread tagging pass for one candidate thread."""
    thread_key = _thread_key(room_id, thread_id)
    async with _thread_lock(thread_key):
        if _thread_marked_done(thread_key):
            return

        first_message = await _authoritative_first_message(
            client,
            room_id,
            thread_id,
            thread_key,
            conversation_cache,
        )
        if first_message is None:
            return

        set_by = client.user_id
        if not set_by:
            return

        tags = await _generate_thread_tags_once(
            first_message,
            room_id,
            thread_id,
            thread_key,
            config,
            runtime_paths,
        )
        if not tags:
            if tags is not None:
                logger.warning(
                    "Thread auto-tag generation returned no usable tags",
                    room_id=room_id,
                    thread_id=thread_id,
                )
            return

        latest_history = await _authoritative_thread_history(
            room_id,
            thread_id,
            thread_key,
            conversation_cache,
        )
        if latest_history is None:
            return

        latest_state = await get_thread_tags(client, room_id, thread_id)
        if latest_state is not None and latest_state.tags:
            logger.info(
                "Skipping auto tags because the thread was tagged concurrently",
                room_id=room_id,
                thread_id=thread_id,
            )
            return

        applied_tags = await _apply_auto_tags(
            client,
            room_id,
            thread_id,
            tags,
            set_by=set_by,
        )
        if applied_tags:
            logger.info(
                "Auto-tagged thread",
                room_id=room_id,
                thread_id=thread_id,
                tags=applied_tags,
            )


async def run_thread_auto_tag(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    conversation_cache: ConversationCacheProtocol,
    message_count_hint: int | None = None,
) -> None:
    """Refresh the daily tag vocabulary and auto-tag the thread when it is new.

    Fire-and-forget entry point queued after response delivery; it must never
    block or fail the main agent turn.
    """
    try:
        await maybe_rebuild_tag_vocabulary(
            client,
            room_id,
            config,
            runtime_paths,
            now=datetime.now(UTC),
        )
    except Exception:
        logger.exception("Tag vocabulary rebuild failed", room_id=room_id)

    if _thread_marked_done(_thread_key(room_id, thread_id)):
        return
    if message_count_hint is not None and message_count_hint > _FIRST_EXCHANGE_MAX_COUNT_HINT:
        return
    try:
        await _auto_tag_thread_once(
            client,
            room_id,
            thread_id,
            config,
            runtime_paths,
            conversation_cache=conversation_cache,
        )
    except Exception:
        logger.exception("Thread auto-tag background task failed", room_id=room_id, thread_id=thread_id)
