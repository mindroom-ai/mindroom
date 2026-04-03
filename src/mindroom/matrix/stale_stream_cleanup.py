"""Cleanup stale streaming messages left behind by restarts."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio
from nio.api import RelationshipType

from mindroom.authorization import get_effective_sender_id_for_reply_permissions
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client import (
    ResolvedVisibleMessage,
    build_threaded_edit_content,
    edit_message,
    get_joined_rooms,
    resolve_latest_visible_messages,
    send_message,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID, extract_agent_name
from mindroom.matrix.message_builder import build_message_content, markdown_to_html
from mindroom.matrix.message_content import extract_and_resolve_message, extract_edit_body
from mindroom.streaming import (
    _RESTART_INTERRUPTED_RESPONSE_NOTE,
    build_restart_interrupted_body,
    is_in_progress_message,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_ROOM_HISTORY_PAGE_SIZE = 100
# Startup cleanup runs before this process starts its Matrix sync loop, so it cannot
# clobber streams created by the same process. The remaining race is another
# concurrently running instance cleaning up a message during a long provider/tool stall
# where no new chunks arrive for a while, so keep a generous recency guard here.
_STALE_STREAM_RECENCY_GUARD_MS = 10_000
# Restart cleanup should only touch messages from the current outage window.
# Older interrupted replies are better left untouched than unexpectedly edited
# and auto-resumed on some later restart.
_STALE_STREAM_LOOKBACK_MS = 6 * 60 * 60 * 1000
_RATE_LIMIT_DELAY_SECONDS = 0.15
_STOP_REACTION_KEYS = frozenset({"🛑", "⏹️"})
_MAX_REQUESTER_RESOLUTION_DEPTH = 10
_INTERRUPTED_PARTIAL_TEXT_LIMIT = 280
_AUTO_RESUME_MESSAGE = (
    "[System: Previous response was interrupted by service restart. Please continue where you left off.]"
)
_TERMINAL_STREAM_STATUSES = frozenset(
    {STREAM_STATUS_CANCELLED, STREAM_STATUS_COMPLETED, STREAM_STATUS_ERROR},
)


@dataclass(frozen=True)
class InterruptedThread:
    """One interrupted thread that can be resumed after restart."""

    room_id: str
    thread_id: str | None
    target_event_id: str
    partial_text: str
    agent_name: str
    original_sender_id: str | None = None
    timestamp_ms: int = field(default=0, compare=False)


@dataclass
class _MessageState:
    """Latest visible state for one original Matrix message."""

    latest_body: str | None = None
    latest_timestamp: int = 0
    latest_event_id: str = ""
    latest_content: dict[str, Any] | None = None
    thread_id: str | None = None
    stream_status: str | None = None
    requester_user_id: str | None = None
    stop_reaction_event_ids: set[str] = field(default_factory=set)


def _requester_resolution_message(
    *,
    event_id: str,
    sender: str,
    content: dict[str, Any] | None,
    body: str | None,
    timestamp: int | None,
) -> ResolvedVisibleMessage:
    """Build a typed visible message for requester-resolution fetches."""
    normalized_content = {key: value for key, value in (content or {}).items() if isinstance(key, str)}
    resolved_body = body if isinstance(body, str) else ""
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=resolved_body,
        event_id=event_id,
        timestamp=timestamp or 0,
        content=normalized_content or None,
    )


async def cleanup_stale_streaming_messages(
    client: nio.AsyncClient,
    *,
    bot_user_id: str,
    bot_user_ids: set[str] | None = None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[int, list[InterruptedThread]]:
    """Clean stale in-progress bot messages across currently joined rooms."""
    joined_room_ids = await get_joined_rooms(client)
    if not joined_room_ids:
        return 0, []

    sender_domain = MatrixID.parse(bot_user_id).domain
    exact_bot_user_ids = {bot_user_id} if bot_user_ids is None else set(bot_user_ids)
    cleaned_count = 0
    interrupted_threads: list[InterruptedThread] = []

    for room_id in joined_room_ids:
        try:
            room_cleaned_count, room_interrupted_threads = await _cleanup_room_stale_streaming_messages(
                client,
                room_id=room_id,
                bot_user_id=bot_user_id,
                bot_user_ids=exact_bot_user_ids,
                sender_domain=sender_domain,
                config=config,
                runtime_paths=runtime_paths,
            )
            cleaned_count += room_cleaned_count
            interrupted_threads.extend(room_interrupted_threads)
        except Exception as exc:
            logger.warning(
                "Failed stale stream cleanup for room",
                room_id=room_id,
                error=str(exc),
            )

    return cleaned_count, interrupted_threads


async def auto_resume_interrupted_threads(
    client: nio.AsyncClient,
    interrupted: list[InterruptedThread],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    max_resumes: int = 10,
    delay: float = 2.0,
) -> int:
    """Send resume prompts for interrupted threaded conversations."""
    if not interrupted or max_resumes <= 0:
        return 0

    selected_threads = _select_threads_to_resume(interrupted, max_resumes=max_resumes)
    if not selected_threads:
        return 0

    resumed_count = 0
    for index, interrupted_thread in enumerate(selected_threads):
        try:
            content = _build_auto_resume_content(
                interrupted_thread,
                config=config,
                runtime_paths=runtime_paths,
            )
            response_event_id = await send_message(client, interrupted_thread.room_id, content)
            if response_event_id:
                logger.info(
                    "Queued auto-resume after restart",
                    room_id=interrupted_thread.room_id,
                    thread_id=interrupted_thread.thread_id,
                    target_event_id=interrupted_thread.target_event_id,
                    event_id=response_event_id,
                )
                resumed_count += 1
            else:
                logger.warning(
                    "Failed to queue auto-resume after restart",
                    room_id=interrupted_thread.room_id,
                    thread_id=interrupted_thread.thread_id,
                    target_event_id=interrupted_thread.target_event_id,
                )
        except Exception as exc:
            logger.warning(
                "Failed to send auto-resume message",
                room_id=interrupted_thread.room_id,
                thread_id=interrupted_thread.thread_id,
                target_event_id=interrupted_thread.target_event_id,
                error=str(exc),
            )
        if index < len(selected_threads) - 1:
            await asyncio.sleep(delay)

    return resumed_count


async def _cleanup_room_stale_streaming_messages(
    client: nio.AsyncClient,
    *,
    room_id: str,
    bot_user_id: str,
    bot_user_ids: set[str],
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[int, list[InterruptedThread]]:
    """Clean stale bot messages in one room."""
    current_time_ms = int(time.time() * 1000)
    message_states = await _scan_room_message_states(
        client,
        room_id=room_id,
        bot_user_id=bot_user_id,
        config=config,
        runtime_paths=runtime_paths,
        now_ms=current_time_ms,
    )
    if not message_states:
        return 0, []

    cleaned_count = 0
    prior_edit_succeeded = False
    interrupted_threads: list[InterruptedThread] = []
    agent_name = _agent_name_for_bot_user_id(bot_user_id, config, runtime_paths)
    if agent_name is None:
        return 0, []
    candidate_items = sorted(
        ((k, v) for k, v in message_states.items() if v.latest_body is not None),
        key=lambda item: (item[1].latest_timestamp, item[0]),
    )

    for target_event_id, state in candidate_items:
        assert state.latest_body is not None  # guaranteed by filter above
        if _is_recent_timestamp(state.latest_timestamp, now_ms=current_time_ms) or _is_older_than_cleanup_window(
            state.latest_timestamp,
            now_ms=current_time_ms,
        ):
            continue
        if _is_cleanup_candidate(state):
            edited, interrupted = await _cleanup_candidate_message(
                client,
                room_id=room_id,
                target_event_id=target_event_id,
                state=state,
                bot_user_ids=bot_user_ids,
                sender_domain=sender_domain,
                config=config,
                runtime_paths=runtime_paths,
                agent_name=agent_name,
                prior_edit_succeeded=prior_edit_succeeded,
            )
            if not edited:
                continue

            cleaned_count += 1
            prior_edit_succeeded = True
            if interrupted is not None:
                interrupted_threads.append(interrupted)
            continue

        if _has_restart_interrupted_note(state.latest_body):
            repaired = await _repair_restart_marked_message_metadata(
                client,
                room_id=room_id,
                target_event_id=target_event_id,
                state=state,
                sender_domain=sender_domain,
                config=config,
                runtime_paths=runtime_paths,
                prior_edit_succeeded=prior_edit_succeeded,
            )
            if repaired:
                cleaned_count += 1
                prior_edit_succeeded = True
            await _redact_stop_reactions(
                client,
                room_id=room_id,
                target_event_id=target_event_id,
                history_reaction_event_ids=state.stop_reaction_event_ids,
                bot_user_ids=bot_user_ids,
            )

    return cleaned_count, interrupted_threads


async def _repair_restart_marked_message_metadata(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
    prior_edit_succeeded: bool,
) -> bool:
    """Repair non-terminal stream metadata on already restart-marked messages."""
    assert state.latest_body is not None
    if not _has_non_terminal_stream_status(state.latest_content):
        return False

    try:
        if prior_edit_succeeded:
            await asyncio.sleep(_RATE_LIMIT_DELAY_SECONDS)
        return await _edit_stale_message(
            client,
            room_id=room_id,
            target_event_id=target_event_id,
            new_text=state.latest_body,
            preserved_content=_terminal_stream_content(state.latest_content),
            thread_id=state.thread_id,
            sender_domain=sender_domain,
            config=config,
            runtime_paths=runtime_paths,
        )
    except Exception as exc:
        logger.warning(
            "Failed stale message metadata repair",
            room_id=room_id,
            event_id=target_event_id,
            error=str(exc),
        )
        return False


async def _cleanup_one_stale_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    bot_user_ids: set[str],
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
) -> tuple[bool, InterruptedThread | None]:
    """Edit one stale message, redact stop reactions, return interrupted thread info."""
    assert state.latest_body is not None
    edit_succeeded = await _edit_stale_message(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        new_text=build_restart_interrupted_body(state.latest_body),
        preserved_content=_terminal_stream_content(state.latest_content),
        thread_id=state.thread_id,
        sender_domain=sender_domain,
        config=config,
        runtime_paths=runtime_paths,
    )
    if not edit_succeeded:
        return False, None

    interrupted: InterruptedThread | None = None
    if state.thread_id is not None:
        interrupted = InterruptedThread(
            room_id=room_id,
            thread_id=state.thread_id,
            target_event_id=target_event_id,
            partial_text=_truncate_partial_text(_extract_partial_text(state.latest_body)),
            agent_name=agent_name,
            original_sender_id=state.requester_user_id,
            timestamp_ms=state.latest_timestamp,
        )
    await _redact_stop_reactions(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        history_reaction_event_ids=state.stop_reaction_event_ids,
        bot_user_ids=bot_user_ids,
    )
    return True, interrupted


async def _cleanup_candidate_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    bot_user_ids: set[str],
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    prior_edit_succeeded: bool,
) -> tuple[bool, InterruptedThread | None]:
    """Best-effort cleanup of one stale candidate message."""
    try:
        if prior_edit_succeeded:
            await asyncio.sleep(_RATE_LIMIT_DELAY_SECONDS)
        return await _cleanup_one_stale_message(
            client,
            room_id=room_id,
            target_event_id=target_event_id,
            state=state,
            bot_user_ids=bot_user_ids,
            sender_domain=sender_domain,
            config=config,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
        )
    except Exception as exc:
        logger.warning(
            "Failed stale message cleanup",
            room_id=room_id,
            event_id=target_event_id,
            error=str(exc),
        )
        return False, None


async def _scan_room_message_states(
    client: nio.AsyncClient,
    *,
    room_id: str,
    bot_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    now_ms: int,
) -> dict[str, _MessageState]:
    """Scan recent room history and return latest state by original event ID."""
    message_states, message_events = await _collect_room_history_events(
        client,
        room_id=room_id,
        bot_user_id=bot_user_id,
        now_ms=now_ms,
    )

    resolved_messages = await resolve_latest_visible_messages(message_events, client, sender=bot_user_id)
    scanned_message_data_by_event_id = _scanned_message_data_by_event_id(message_events)
    requester_ids_by_event_id = await _derive_requester_ids_for_bot_messages(
        client,
        resolved_messages=resolved_messages,
        scanned_message_data_by_event_id=scanned_message_data_by_event_id,
        room_id=room_id,
        bot_user_id=bot_user_id,
        config=config,
        runtime_paths=runtime_paths,
    )
    _merge_bot_resolved_message_states(
        message_states,
        resolved_messages,
        bot_user_id=bot_user_id,
        requester_ids_by_event_id=requester_ids_by_event_id,
    )

    return message_states


async def _collect_room_history_events(
    client: nio.AsyncClient,
    *,
    room_id: str,
    bot_user_id: str,
    now_ms: int,
) -> tuple[dict[str, _MessageState], list[nio.RoomMessageText]]:
    """Return room history text events plus tracked stop reactions."""
    message_states: dict[str, _MessageState] = {}
    message_events: list[nio.RoomMessageText] = []
    from_token: str | None = None

    while True:
        response = await client.room_messages(
            room_id,
            start=from_token,
            limit=_ROOM_HISTORY_PAGE_SIZE,
            direction=nio.MessageDirection.back,
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            logger.warning(
                "Failed to fetch room history during stale stream cleanup",
                room_id=room_id,
                error=str(response),
            )
            return {}, []

        if not response.chunk:
            break

        for event in response.chunk:
            try:
                if isinstance(event, nio.RoomMessageText):
                    message_events.append(event)
                elif isinstance(event, nio.Event):
                    _record_stop_reaction(
                        message_states,
                        event=event,
                        bot_user_id=bot_user_id,
                    )
            except Exception as exc:
                event_id = event.event_id if isinstance(event, nio.Event) else None
                logger.warning(
                    "Failed to inspect room event during stale stream cleanup",
                    room_id=room_id,
                    event_id=event_id,
                    error=str(exc),
                )

        if not response.end:
            break
        if _chunk_reaches_cleanup_lookback_limit(response.chunk, now_ms=now_ms):
            break
        from_token = response.end

    return message_states, message_events


def _merge_bot_resolved_message_states(
    message_states: dict[str, _MessageState],
    resolved_messages: dict[str, ResolvedVisibleMessage],
    *,
    bot_user_id: str,
    requester_ids_by_event_id: dict[str, str],
) -> None:
    """Merge resolved bot-authored messages into cleanup state."""
    for target_event_id, message in resolved_messages.items():
        if message.sender != bot_user_id:
            continue
        requester_user_id = requester_ids_by_event_id.get(target_event_id)
        _merge_resolved_message_state(
            message_states,
            target_event_id=target_event_id,
            message=message,
            requester_user_id=requester_user_id,
        )


def _merge_resolved_message_state(
    message_states: dict[str, _MessageState],
    *,
    target_event_id: str,
    message: ResolvedVisibleMessage,
    requester_user_id: str | None,
) -> None:
    """Store one resolved message if it has the fields cleanup needs."""
    normalized_latest_content = {key: value for key, value in message.content.items() if isinstance(key, str)}
    state = message_states.setdefault(target_event_id, _MessageState())
    state.latest_body = message.body
    state.latest_timestamp = message.timestamp
    state.latest_event_id = message.visible_event_id
    state.latest_content = normalized_latest_content
    state.thread_id = message.thread_id
    state.stream_status = message.stream_status
    state.requester_user_id = requester_user_id


def _scanned_message_data_by_event_id(message_events: list[nio.RoomMessageText]) -> dict[str, ResolvedVisibleMessage]:
    """Return raw scanned room-history messages keyed by exact event ID."""
    message_data_by_event_id: dict[str, ResolvedVisibleMessage] = {}
    for event in message_events:
        event_id = event.event_id
        sender = event.sender
        if not isinstance(event_id, str) or not isinstance(sender, str):
            continue

        raw_content = _as_string_keyed_dict(event.source.get("content")) or {}
        message_data_by_event_id[event_id] = _requester_resolution_message(
            event_id=event_id,
            sender=sender,
            content=raw_content,
            body=event.body,
            timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
        )
    return message_data_by_event_id


def _scanned_message_requires_exact_requester_fetch(message_data: ResolvedVisibleMessage) -> bool:
    """Return whether requester resolution must fetch the exact event for this scanned message."""
    if "m.new_content" not in message_data.content:
        return False
    return message_data.reply_to_event_id is None


async def _derive_requester_ids_for_bot_messages(
    client: nio.AsyncClient,
    resolved_messages: dict[str, ResolvedVisibleMessage],
    scanned_message_data_by_event_id: dict[str, ResolvedVisibleMessage],
    *,
    room_id: str,
    bot_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, str]:
    """Return effective requester IDs for bot-authored messages."""
    requester_ids_by_event_id: dict[str, str] = {}
    requester_cache: dict[str, str | None] = {}
    fetched_message_data_by_event_id: dict[str, ResolvedVisibleMessage | None] = {}
    sorted_messages = sorted(
        resolved_messages.items(),
        key=lambda item: (item[1].timestamp, item[0]),
    )

    for target_event_id, message_data in sorted_messages:
        sender = message_data.sender
        if sender != bot_user_id:
            continue

        try:
            requester_user_id = await _resolve_requester_for_bot_message(
                client,
                room_id=room_id,
                target_event_id=target_event_id,
                message_data=message_data,
                resolved_messages=resolved_messages,
                scanned_message_data_by_event_id=scanned_message_data_by_event_id,
                requester_cache=requester_cache,
                fetched_message_data_by_event_id=fetched_message_data_by_event_id,
                config=config,
                runtime_paths=runtime_paths,
            )
        except Exception as exc:
            logger.warning(
                "Failed to resolve requester for bot message",
                room_id=room_id,
                event_id=target_event_id,
                error=str(exc),
            )
            continue
        if requester_user_id is None:
            continue
        requester_ids_by_event_id[target_event_id] = requester_user_id

    return requester_ids_by_event_id


async def _resolve_requester_for_bot_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    message_data: ResolvedVisibleMessage,
    resolved_messages: dict[str, ResolvedVisibleMessage],
    scanned_message_data_by_event_id: dict[str, ResolvedVisibleMessage],
    requester_cache: dict[str, str | None],
    fetched_message_data_by_event_id: dict[str, ResolvedVisibleMessage | None],
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Resolve the requester for one bot-authored message from its exact reply target."""
    reply_to_event_id = message_data.reply_to_event_id
    if reply_to_event_id is None:
        original_message_data = await _load_scanned_or_fetched_message_data(
            client,
            room_id=room_id,
            event_id=target_event_id,
            scanned_message_data_by_event_id=scanned_message_data_by_event_id,
            fetched_message_data_by_event_id=fetched_message_data_by_event_id,
        )
        if original_message_data is None:
            return None
        reply_to_event_id = original_message_data.reply_to_event_id
    if reply_to_event_id is None or reply_to_event_id == target_event_id:
        return None
    return await _resolve_requester_for_event_id(
        client,
        room_id=room_id,
        event_id=reply_to_event_id,
        resolved_messages=resolved_messages,
        scanned_message_data_by_event_id=scanned_message_data_by_event_id,
        requester_cache=requester_cache,
        fetched_message_data_by_event_id=fetched_message_data_by_event_id,
        config=config,
        runtime_paths=runtime_paths,
        visited_event_ids={target_event_id},
    )


async def _resolve_requester_for_event_id(
    client: nio.AsyncClient,
    *,
    room_id: str,
    event_id: str,
    resolved_messages: dict[str, ResolvedVisibleMessage],
    scanned_message_data_by_event_id: dict[str, ResolvedVisibleMessage],
    requester_cache: dict[str, str | None],
    fetched_message_data_by_event_id: dict[str, ResolvedVisibleMessage | None],
    config: Config,
    runtime_paths: RuntimePaths,
    visited_event_ids: set[str],
    max_depth: int = _MAX_REQUESTER_RESOLUTION_DEPTH,
) -> str | None:
    """Resolve the effective requester for one event by following reply-chain edges."""
    if event_id in requester_cache:
        return requester_cache[event_id]
    if event_id in visited_event_ids:
        return None
    if max_depth <= 0:
        return None

    requester_user_id: str | None = None
    message_data, sender = await _load_message_data_for_requester_resolution(
        client,
        room_id=room_id,
        event_id=event_id,
        resolved_messages=resolved_messages,
        scanned_message_data_by_event_id=scanned_message_data_by_event_id,
        fetched_message_data_by_event_id=fetched_message_data_by_event_id,
    )
    if message_data is not None and sender is not None:
        requester_user_id = _effective_requester_for_message(
            message_data,
            config=config,
            runtime_paths=runtime_paths,
        )
        if (
            requester_user_id is not None
            and requester_user_id == sender
            and _is_internal_sender(sender, config, runtime_paths)
        ):
            requester_user_id = await _resolve_requester_from_internal_reply(
                client,
                room_id=room_id,
                event_id=event_id,
                message_data=message_data,
                resolved_messages=resolved_messages,
                scanned_message_data_by_event_id=scanned_message_data_by_event_id,
                requester_cache=requester_cache,
                fetched_message_data_by_event_id=fetched_message_data_by_event_id,
                config=config,
                runtime_paths=runtime_paths,
                visited_event_ids=visited_event_ids,
                max_depth=max_depth - 1,
            )
    requester_cache[event_id] = requester_user_id
    return requester_user_id


async def _load_message_data_for_requester_resolution(
    client: nio.AsyncClient,
    *,
    room_id: str,
    event_id: str,
    resolved_messages: dict[str, ResolvedVisibleMessage],
    scanned_message_data_by_event_id: dict[str, ResolvedVisibleMessage],
    fetched_message_data_by_event_id: dict[str, ResolvedVisibleMessage | None],
) -> tuple[ResolvedVisibleMessage | None, str | None]:
    """Load one message from scanned history or the Matrix API with its sender ID."""
    message_data = resolved_messages.get(event_id)
    sender = message_data.sender if message_data is not None else None
    if sender is not None:
        return message_data, sender

    message_data = await _load_scanned_or_fetched_message_data(
        client,
        room_id=room_id,
        event_id=event_id,
        scanned_message_data_by_event_id=scanned_message_data_by_event_id,
        fetched_message_data_by_event_id=fetched_message_data_by_event_id,
    )
    return message_data, message_data.sender if message_data is not None else None


async def _resolve_requester_from_internal_reply(
    client: nio.AsyncClient,
    *,
    room_id: str,
    event_id: str,
    message_data: ResolvedVisibleMessage,
    resolved_messages: dict[str, ResolvedVisibleMessage],
    scanned_message_data_by_event_id: dict[str, ResolvedVisibleMessage],
    requester_cache: dict[str, str | None],
    fetched_message_data_by_event_id: dict[str, ResolvedVisibleMessage | None],
    config: Config,
    runtime_paths: RuntimePaths,
    visited_event_ids: set[str],
    max_depth: int = _MAX_REQUESTER_RESOLUTION_DEPTH,
) -> str | None:
    """Follow an internal sender's reply edge until a real requester is found."""
    reply_to_event_id = message_data.reply_to_event_id
    if reply_to_event_id is None:
        original_message_data = await _load_scanned_or_fetched_message_data(
            client,
            room_id=room_id,
            event_id=event_id,
            scanned_message_data_by_event_id=scanned_message_data_by_event_id,
            fetched_message_data_by_event_id=fetched_message_data_by_event_id,
        )
        if original_message_data is not None:
            reply_to_event_id = original_message_data.reply_to_event_id
    if reply_to_event_id is None or reply_to_event_id == event_id:
        return None

    return await _resolve_requester_for_event_id(
        client,
        room_id=room_id,
        event_id=reply_to_event_id,
        resolved_messages=resolved_messages,
        scanned_message_data_by_event_id=scanned_message_data_by_event_id,
        requester_cache=requester_cache,
        fetched_message_data_by_event_id=fetched_message_data_by_event_id,
        config=config,
        runtime_paths=runtime_paths,
        visited_event_ids=visited_event_ids | {event_id},
        max_depth=max_depth - 1,
    )


async def _load_scanned_or_fetched_message_data(
    client: nio.AsyncClient,
    *,
    room_id: str,
    event_id: str,
    scanned_message_data_by_event_id: dict[str, ResolvedVisibleMessage],
    fetched_message_data_by_event_id: dict[str, ResolvedVisibleMessage | None],
) -> ResolvedVisibleMessage | None:
    """Load one message from scanned room history before falling back to the Matrix API."""
    scanned_message_data = scanned_message_data_by_event_id.get(event_id)
    if scanned_message_data is not None and not _scanned_message_requires_exact_requester_fetch(scanned_message_data):
        return scanned_message_data

    fetched_message_data = await _fetch_message_data_for_event_id(
        client,
        room_id=room_id,
        event_id=event_id,
        fetched_message_data_by_event_id=fetched_message_data_by_event_id,
    )
    if fetched_message_data is not None:
        return fetched_message_data
    return scanned_message_data


async def _fetch_message_data_for_event_id(
    client: nio.AsyncClient,
    *,
    room_id: str,
    event_id: str,
    fetched_message_data_by_event_id: dict[str, ResolvedVisibleMessage | None],
) -> ResolvedVisibleMessage | None:
    """Fetch basic message data for one exact Matrix event ID."""
    if event_id in fetched_message_data_by_event_id:
        return fetched_message_data_by_event_id[event_id]

    response = await client.room_get_event(room_id, event_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        fetched_message_data_by_event_id[event_id] = None
        return None

    event = response.event
    event_source = event.source if isinstance(event.source, dict) else None
    sender = event.sender if isinstance(event.sender, str) else None
    if event_source is None or sender is None:
        fetched_message_data_by_event_id[event_id] = None
        return None

    event_info = EventInfo.from_event(event_source)
    if isinstance(event, nio.RoomMessageText):
        if event_info.is_edit:
            edited_body, edited_content = await extract_edit_body(event_source, client)
            if edited_body is not None and edited_content is not None:
                message_data = _requester_resolution_message(
                    event_id=event_id,
                    sender=sender,
                    content=edited_content,
                    body=edited_body,
                    timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
                )
                fetched_message_data_by_event_id[event_id] = message_data
                return message_data

        extracted_message = await extract_and_resolve_message(event, client)
        message_data = ResolvedVisibleMessage.from_message_data(
            extracted_message,
            thread_id=None,
            latest_event_id=event_id,
        )
        fetched_message_data_by_event_id[event_id] = message_data
        return message_data

    content = event_source.get("content")
    body: str | None = None
    if isinstance(content, dict):
        body_value = content.get("body")
        if isinstance(body_value, str):
            body = body_value

    message_data = _requester_resolution_message(
        event_id=event_id,
        sender=sender,
        content=content if isinstance(content, dict) else {},
        body=body,
        timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
    )
    fetched_message_data_by_event_id[event_id] = message_data
    return message_data


def _as_string_keyed_dict(value: object) -> dict[str, object] | None:
    """Normalize one arbitrary JSON-like object into a string-keyed dict."""
    if not isinstance(value, dict):
        return None

    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        normalized[key] = item
    return normalized


def _is_internal_sender(
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether the sender is one of MindRoom's own Matrix accounts."""
    internal_ids = {matrix_id.full_id for matrix_id in config.get_ids(runtime_paths).values()}
    mindroom_user_id = config.get_mindroom_user_id(runtime_paths)
    return sender_id in internal_ids or sender_id == mindroom_user_id


def _effective_requester_for_message(
    message_data: ResolvedVisibleMessage,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Resolve the effective requester for one visible message."""
    sender = message_data.sender
    content = message_data.content
    event_source = {"content": content}
    return get_effective_sender_id_for_reply_permissions(sender, event_source, config, runtime_paths)


def _record_stop_reaction(
    message_states: dict[str, _MessageState],
    *,
    event: nio.Event,
    bot_user_id: str,
) -> None:
    """Track self-authored stop reactions by their target message ID."""
    event_sender = event.sender
    if event_sender != bot_user_id:
        return

    event_source = event.source
    if not isinstance(event_source, dict):
        return

    event_info = EventInfo.from_event(event_source)
    if not event_info.is_reaction or event_info.reaction_key not in _STOP_REACTION_KEYS:
        return

    target_event_id = event_info.reaction_target_event_id
    reaction_event_id = event.event_id
    if not isinstance(target_event_id, str) or not isinstance(reaction_event_id, str):
        return

    message_states.setdefault(target_event_id, _MessageState()).stop_reaction_event_ids.add(reaction_event_id)


async def _edit_stale_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    new_text: str,
    preserved_content: dict[str, Any] | None,
    thread_id: str | None,
    sender_domain: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Edit a stale message while preserving thread context when present."""
    extra_content = _preserved_cleanup_content(preserved_content)
    content = await build_threaded_edit_content(
        client,
        room_id=room_id,
        event_id=target_event_id,
        new_text=new_text,
        thread_id=thread_id,
        config=config,
        runtime_paths=runtime_paths,
        sender_domain=sender_domain,
        extra_content=extra_content,
    )

    response_event_id = await edit_message(
        client,
        room_id,
        target_event_id,
        content,
        new_text,
        extra_content=extra_content,
    )
    if response_event_id:
        return True

    logger.warning(
        "Failed to edit stale streaming message",
        room_id=room_id,
        event_id=target_event_id,
    )
    return False


def _preserved_cleanup_content(content: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the metadata fields that should survive a restart cleanup edit."""
    if content is None:
        return None

    preserved: dict[str, Any] = {}
    for key, value in content.items():
        if not isinstance(key, str):
            continue
        if (key.startswith("io.mindroom.") and key != "io.mindroom.long_text") or key in {
            ORIGINAL_SENDER_KEY,
            "m.mentions",
        }:
            preserved[key] = value

    return preserved or None


def _has_non_terminal_stream_status(content: dict[str, Any] | None) -> bool:
    """Return whether the message still advertises an active stream state."""
    if content is None:
        return False
    stream_status = content.get(STREAM_STATUS_KEY)
    return isinstance(stream_status, str) and stream_status not in _TERMINAL_STREAM_STATUSES


def _terminal_stream_content(content: dict[str, Any] | None) -> dict[str, Any]:
    """Return metadata with a terminal stream status for cleanup edits."""
    if content is None:
        return {STREAM_STATUS_KEY: STREAM_STATUS_ERROR}
    return {**content, STREAM_STATUS_KEY: STREAM_STATUS_ERROR}


async def _redact_stop_reactions(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    history_reaction_event_ids: Iterable[str],
    bot_user_ids: set[str],
) -> None:
    """Best-effort removal of stale bot-authored stop reactions."""
    reaction_event_ids = set(history_reaction_event_ids)
    try:
        reaction_event_ids.update(
            await _get_stop_reaction_event_ids_from_relations(
                client,
                room_id=room_id,
                target_event_id=target_event_id,
                bot_user_ids=bot_user_ids,
            ),
        )
    except Exception as exc:
        logger.warning(
            "Failed to fetch stop reactions from relations API, falling back to history scan",
            room_id=room_id,
            event_id=target_event_id,
            error=str(exc),
        )

    for reaction_event_id in sorted(reaction_event_ids):
        try:
            response = await client.room_redact(
                room_id=room_id,
                event_id=reaction_event_id,
                reason="Response interrupted by service restart",
            )
            if isinstance(response, nio.RoomRedactError):
                logger.warning(
                    "Failed to redact stale stop reaction",
                    room_id=room_id,
                    event_id=target_event_id,
                    reaction_event_id=reaction_event_id,
                    error=str(response),
                )
        except Exception as exc:
            logger.warning(
                "Failed to redact stale stop reaction",
                room_id=room_id,
                event_id=target_event_id,
                reaction_event_id=reaction_event_id,
                error=str(exc),
            )


async def _get_stop_reaction_event_ids_from_relations(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    bot_user_ids: set[str],
) -> set[str]:
    """Return bot-authored stop reactions for the original target event."""
    reaction_event_ids: set[str] = set()
    async for related_event in _iter_reaction_relation_events(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
    ):
        if not isinstance(related_event, nio.ReactionEvent):
            continue

        related_event_id = related_event.event_id
        if related_event.sender not in bot_user_ids or not isinstance(related_event_id, str):
            continue

        event_source = related_event.source
        if not isinstance(event_source, dict):
            continue

        event_info = EventInfo.from_event(event_source)
        if not event_info.is_reaction or event_info.reaction_target_event_id != target_event_id:
            continue
        if event_info.reaction_key not in _STOP_REACTION_KEYS:
            continue

        reaction_event_ids.add(related_event_id)

    return reaction_event_ids


async def _iter_reaction_relation_events(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
) -> AsyncIterator[nio.Event]:
    """Yield reaction relation events from nio's relations iterator."""
    async for related_event in client.room_get_event_relations(
        room_id,
        target_event_id,
        RelationshipType.annotation,
        "m.reaction",
    ):
        yield related_event


def _extract_partial_text(body: str) -> str:
    """Return partial text without the restart interruption note."""
    interrupted_body = build_restart_interrupted_body(body)
    if interrupted_body == _RESTART_INTERRUPTED_RESPONSE_NOTE:
        return ""
    return interrupted_body.removesuffix(f"\n\n{_RESTART_INTERRUPTED_RESPONSE_NOTE}")


def _truncate_partial_text(text: str, *, limit: int = _INTERRUPTED_PARTIAL_TEXT_LIMIT) -> str:
    """Return a compact partial-text preview."""
    stripped_text = text.strip()
    if len(stripped_text) <= limit:
        return stripped_text
    return f"{stripped_text[: limit - 1]}…"


def _select_threads_to_resume(
    interrupted: list[InterruptedThread],
    *,
    max_resumes: int,
) -> list[InterruptedThread]:
    """Return the newest unique threaded interruptions up to the resume cap."""
    latest_by_key: dict[tuple[str, str, str], InterruptedThread] = {}

    for interrupted_thread in interrupted:
        if interrupted_thread.thread_id is None:
            continue
        key = (interrupted_thread.room_id, interrupted_thread.thread_id, interrupted_thread.agent_name)
        existing = latest_by_key.get(key)
        if existing is None or interrupted_thread.timestamp_ms >= existing.timestamp_ms:
            latest_by_key[key] = interrupted_thread

    unique_threads = sorted(
        latest_by_key.values(),
        key=lambda interrupted_thread: (
            interrupted_thread.timestamp_ms,
            interrupted_thread.room_id,
            interrupted_thread.thread_id or "",
            interrupted_thread.agent_name,
        ),
    )
    if max_resumes >= len(unique_threads):
        return unique_threads
    return unique_threads[-max_resumes:]


def _has_restart_interrupted_note(body: str) -> bool:
    """Return whether the body already contains the restart interruption note."""
    return body.rstrip().endswith(_RESTART_INTERRUPTED_RESPONSE_NOTE)


def _is_cleanup_candidate(state: _MessageState) -> bool:
    """Return whether the latest visible state represents stale in-progress output."""
    assert state.latest_body is not None
    if _has_restart_interrupted_note(state.latest_body):
        return False
    if state.stream_status == STREAM_STATUS_COMPLETED:
        return False
    if state.stream_status in {STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING}:
        return True
    return is_in_progress_message(state.latest_body)


def _is_recent_timestamp(timestamp_ms: int, *, now_ms: int | None = None) -> bool:
    """Return whether a timestamp is still within the startup recency guard."""
    current_time_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return current_time_ms - timestamp_ms < _STALE_STREAM_RECENCY_GUARD_MS


def _is_older_than_cleanup_window(timestamp_ms: int, *, now_ms: int | None = None) -> bool:
    """Return whether a timestamp is older than the restart cleanup lookback window."""
    current_time_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return current_time_ms - timestamp_ms > _STALE_STREAM_LOOKBACK_MS


def _chunk_reaches_cleanup_lookback_limit(events: list[object], *, now_ms: int) -> bool:
    """Return whether the oldest event in this page is beyond the cleanup lookback window."""
    oldest_timestamp = min(
        (
            event.server_timestamp
            for event in events
            if isinstance(event, nio.Event) and isinstance(event.server_timestamp, int)
        ),
        default=None,
    )
    return oldest_timestamp is not None and _is_older_than_cleanup_window(oldest_timestamp, now_ms=now_ms)


def _build_auto_resume_content(
    interrupted_thread: InterruptedThread,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, object]:
    """Build the router-authored visible resume relay for one interrupted agent."""
    matrix_id = config.get_ids(runtime_paths).get(interrupted_thread.agent_name)
    target_user_id = matrix_id.full_id if matrix_id is not None else None
    display_name = _entity_display_name(interrupted_thread.agent_name, config)

    body = _AUTO_RESUME_MESSAGE
    formatted_body: str | None = None
    mentioned_user_ids: list[str] | None = None
    if target_user_id is not None:
        body = f"@{display_name} {_AUTO_RESUME_MESSAGE}"
        formatted_body = markdown_to_html(
            f"[@{display_name}](https://matrix.to/#/{target_user_id}) {_AUTO_RESUME_MESSAGE}",
        )
        mentioned_user_ids = [target_user_id]

    extra_content = (
        {ORIGINAL_SENDER_KEY: interrupted_thread.original_sender_id}
        if interrupted_thread.original_sender_id is not None
        else None
    )
    return build_message_content(
        body=body,
        formatted_body=formatted_body,
        mentioned_user_ids=mentioned_user_ids,
        thread_event_id=interrupted_thread.thread_id,
        reply_to_event_id=interrupted_thread.target_event_id,
        latest_thread_event_id=interrupted_thread.target_event_id,
        extra_content=extra_content,
    )


def _entity_display_name(agent_name: str, config: Config) -> str:
    """Return the configured display name for an agent or team."""
    if agent_name in config.agents:
        return config.agents[agent_name].display_name
    if agent_name in config.teams:
        return config.teams[agent_name].display_name
    return agent_name


def _agent_name_for_bot_user_id(
    bot_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Resolve a bot user ID back to its configured agent or team name."""
    direct_match = extract_agent_name(bot_user_id, config, runtime_paths)
    if direct_match is not None:
        return direct_match

    bot_username = MatrixID.parse(bot_user_id).username
    for agent_name, matrix_id in config.get_ids(runtime_paths).items():
        if matrix_id.username == bot_username:
            return agent_name
    return None
