"""Cleanup stale streaming messages left behind by restarts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio
from nio.api import RelationshipType

from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    STREAM_VISIBLE_BODY_KEY,
    STREAM_WARMUP_SUFFIX_KEY,
)
from mindroom.entity_resolution import current_internal_sender_ids, entity_identity_registry
from mindroom.event_journal.models import UnreadableMatrixDelivery
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import edit_message_result
from mindroom.matrix.client_visible_messages import fetch_latest_visible_message
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE, build_restart_interrupted_body

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal.store import PrincipalStore

logger = get_logger(__name__)

type _ResponseRecoveryScope = Callable[[str, str, str], AbstractAsyncContextManager[bool]]

# Startup cleanup receives a pre-sync cutoff and ignores messages at or after
# that timestamp, so post-sync cleanup cannot clobber streams created by this
# process. The remaining race is another concurrently running instance cleaning
# up a message during a long provider/tool stall where no new chunks arrive for
# a while, so keep a generous recency guard here.
_STALE_STREAM_RECENCY_GUARD_MS = 10_000
# Restart cleanup should only edit active-looking messages from the current
# outage window.
_STALE_STREAM_LOOKBACK_MS = 6 * 60 * 60 * 1000
_RATE_LIMIT_DELAY_SECONDS = 0.15
_RECOVERY_ROOM_CONCURRENCY = 8
_STOP_REACTION_KEYS = frozenset({"🛑", "⏹️"})
_TERMINAL_STREAM_STATUSES = frozenset(
    {STREAM_STATUS_CANCELLED, STREAM_STATUS_COMPLETED, STREAM_STATUS_ERROR, STREAM_STATUS_INTERRUPTED},
)


@dataclass(frozen=True)
class _StaleStreamRecoveryResult:
    """Aggregate outcome from one startup recovery sweep."""

    room_count: int
    cleaned_count: int


@dataclass
class _MessageState:
    """Latest visible state for one original Matrix message."""

    latest_body: str | None = None
    latest_timestamp: int = 0
    latest_event_id: str = ""
    latest_content: dict[str, Any] | None = None
    thread_id: str | None = None
    stream_status: str | None = None
    bot_user_id: str | None = None


async def _recovery_room_targets(
    principals: dict[str, PrincipalStore],
) -> dict[str, dict[str, tuple[str, str | None]]]:
    """Page durable candidates without waiting on individual response locks."""
    room_targets: dict[str, dict[str, tuple[str, str | None]]] = {}
    for bot_user_id, principal in principals.items():
        cursor: tuple[int, str] | None = None
        while batch := await principal.recovery_initial_deliveries(after=cursor):
            cursor = (batch[-1].created_at_ns, batch[-1].delivery_id)
            for delivery in batch:
                if isinstance(delivery, UnreadableMatrixDelivery):
                    logger.warning("Unreadable startup recovery delivery", delivery_id=delivery.delivery_id)
                    continue
                event_id = delivery.acknowledged_event_id
                assert event_id is not None
                room_targets.setdefault(delivery.room_id, {})[event_id] = (bot_user_id, delivery.thread_id)
    return room_targets


async def recover_stale_streaming_messages(
    actors: dict[str, nio.AsyncClient],
    *,
    principals: dict[str, PrincipalStore],
    response_recovery_scope: _ResponseRecoveryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    startup_cutoff_ms: int | None,
    room_concurrency: int = _RECOVERY_ROOM_CONCURRENCY,
) -> _StaleStreamRecoveryResult:
    """Finish exact owned INITIALs left streaming; an empty outbox needs no Matrix history."""
    agent_names = {
        user_id: name
        for user_id in actors
        if (name := _agent_name_for_bot_user_id(user_id, config, runtime_paths)) is not None
    }
    room_targets = await _recovery_room_targets(
        {user_id: principal for user_id, principal in principals.items() if user_id in agent_names},
    )
    if not room_targets:
        return _StaleStreamRecoveryResult(room_count=0, cleaned_count=0)

    semaphore = asyncio.Semaphore(max(1, room_concurrency))

    async def recover_room(room_id: str, targets: dict[str, tuple[str, str | None]]) -> int:
        async with semaphore:
            cleaned = 0
            prior_edit_succeeded_by_bot: set[str] = set()
            for event_id, (bot_user_id, thread_id) in targets.items():
                try:
                    async with response_recovery_scope(agent_names[bot_user_id], room_id, event_id) as permitted:
                        if not permitted:
                            continue
                    cleaned += await _cleanup_stale_streaming_room(
                        actors[bot_user_id],
                        room_id=room_id,
                        actors={bot_user_id: actors[bot_user_id]},
                        target_thread_ids={event_id: thread_id},
                        prior_edit_succeeded_by_bot=prior_edit_succeeded_by_bot,
                        bot_user_ids=set(actors),
                        config=config,
                        runtime_paths=runtime_paths,
                        startup_cutoff_ms=startup_cutoff_ms,
                        response_recovery_scope=response_recovery_scope,
                    )
                except Exception:
                    logger.warning(
                        "Failed exact startup response recovery",
                        room_id=room_id,
                        event_id=event_id,
                        exc_info=True,
                    )
            return cleaned

    tasks = [
        asyncio.create_task(recover_room(room_id, targets), name=f"response_recovery:{room_id}")
        for room_id, targets in room_targets.items()
    ]
    try:
        cleaned_count = sum(await asyncio.gather(*tasks))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return _StaleStreamRecoveryResult(len(room_targets), cleaned_count)


async def _cleanup_stale_streaming_room(
    scan_client: nio.AsyncClient,
    *,
    room_id: str,
    actors: dict[str, nio.AsyncClient],
    target_thread_ids: dict[str, str | None],
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
    startup_cutoff_ms: int | None = None,
    response_recovery_scope: _ResponseRecoveryScope,
    prior_edit_succeeded_by_bot: set[str] | None = None,
) -> int:
    """Resolve owned targets and let each bot account repair its own messages."""
    if not actors:
        return 0
    current_time_ms = int(time.time() * 1000)
    message_states = await _load_recovery_message_states(
        scan_client,
        room_id=room_id,
        target_thread_ids=target_thread_ids,
        cleanup_bot_user_ids=set(actors),
        bot_user_ids=bot_user_ids,
        config=config,
        runtime_paths=runtime_paths,
    )
    if not message_states:
        return 0

    cleaned_count = 0
    if prior_edit_succeeded_by_bot is None:
        prior_edit_succeeded_by_bot = set()
    candidate_items = sorted(
        ((k, v) for k, v in message_states.items() if v.latest_body is not None),
        key=lambda item: (item[1].latest_timestamp, item[0]),
    )

    for target_event_id, state in candidate_items:
        assert state.latest_body is not None  # guaranteed by filter above
        bot_user_id = state.bot_user_id
        actor_client = actors.get(bot_user_id) if bot_user_id is not None else None
        if bot_user_id is None or actor_client is None:
            continue
        agent_name = _agent_name_for_bot_user_id(bot_user_id, config, runtime_paths)
        if agent_name is None:
            continue
        async with response_recovery_scope(agent_name, room_id, target_event_id) as permitted:
            if not permitted:
                continue
            edited = await _process_stale_room_candidate(
                actor_client,
                room_id=room_id,
                target_event_id=target_event_id,
                state=state,
                bot_user_ids=bot_user_ids,
                config=config,
                runtime_paths=runtime_paths,
                current_time_ms=current_time_ms,
                startup_cutoff_ms=startup_cutoff_ms,
                prior_edit_succeeded=bot_user_id in prior_edit_succeeded_by_bot,
            )
        if edited:
            cleaned_count += 1
            prior_edit_succeeded_by_bot.add(bot_user_id)

    return cleaned_count


async def _process_stale_room_candidate(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
    current_time_ms: int,
    startup_cutoff_ms: int | None,
    prior_edit_succeeded: bool,
) -> bool:
    """Repair one bot-owned candidate from a shared room scan."""
    assert state.latest_body is not None
    if not _needs_recovery(state, now_ms=current_time_ms, startup_cutoff_ms=startup_cutoff_ms):
        return False
    if _is_cleanup_candidate(state):
        return await _cleanup_candidate_message(
            client,
            room_id=room_id,
            target_event_id=target_event_id,
            state=state,
            bot_user_ids=bot_user_ids,
            config=config,
            runtime_paths=runtime_paths,
            prior_edit_succeeded=prior_edit_succeeded,
        )
    return await _handle_interrupted_message(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        state=state,
        bot_user_ids=bot_user_ids,
        config=config,
        runtime_paths=runtime_paths,
        prior_edit_succeeded=prior_edit_succeeded,
    )


def _needs_recovery(state: _MessageState, *, now_ms: int, startup_cutoff_ms: int | None) -> bool:
    """Select stale streams and restart-marked messages that still advertise an active stream."""
    if state.latest_body is None or _should_skip_for_startup_cleanup_window(
        state,
        now_ms=now_ms,
        startup_cutoff_ms=startup_cutoff_ms,
    ):
        return False
    return _is_cleanup_candidate(state) or _has_restart_interrupted_note(state.latest_body)


async def _handle_interrupted_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
    prior_edit_succeeded: bool,
) -> bool:
    """Repair a restart-marked response seen during startup cleanup."""
    repaired = await _repair_restart_marked_message_metadata(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        state=state,
        config=config,
        runtime_paths=runtime_paths,
        prior_edit_succeeded=prior_edit_succeeded,
    )
    await _redact_stop_reactions(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        bot_user_ids=bot_user_ids,
    )
    return repaired


async def _repair_restart_marked_message_metadata(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
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
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Edit one stale message and redact its stop reactions."""
    assert state.latest_body is not None
    edit_succeeded = await _edit_stale_message(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        new_text=build_restart_interrupted_body(state.latest_body),
        preserved_content=_terminal_stream_content(state.latest_content),
        config=config,
        runtime_paths=runtime_paths,
    )
    if not edit_succeeded:
        return False
    await _redact_stop_reactions(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        bot_user_ids=bot_user_ids,
    )
    return True


async def _cleanup_candidate_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
    prior_edit_succeeded: bool,
) -> bool:
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
            config=config,
            runtime_paths=runtime_paths,
        )
    except Exception as exc:
        logger.warning(
            "Failed stale message cleanup",
            room_id=room_id,
            event_id=target_event_id,
            error=str(exc),
        )
        return False


async def _load_recovery_message_states(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_thread_ids: dict[str, str | None],
    cleanup_bot_user_ids: set[str],
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, _MessageState]:
    """Resolve only exact outbox targets, including their newest replacements."""
    trusted = _cleanup_trusted_sender_ids(bot_user_ids=bot_user_ids, config=config, runtime_paths=runtime_paths)
    states: dict[str, _MessageState] = {}
    for event_id, thread_id in target_thread_ids.items():
        message = await fetch_latest_visible_message(
            client,
            room_id=room_id,
            event_id=event_id,
            trusted_sender_ids=trusted,
        )
        if message is None or message.sender not in cleanup_bot_user_ids:
            msg = f"Cannot resolve owned recovery response {event_id}"
            raise RuntimeError(msg)
        state = states.setdefault(event_id, _MessageState())
        state.latest_body = message.body
        # The last time this message changed, not when it was created. ``timestamp`` deliberately
        # stays the original event's so an edit cannot reorder a thread, which means it is the
        # wrong clock for "is this stream still active": a placeholder posted eight hours ago and
        # edited seconds before a restart would read as older than the cleanup window and be
        # skipped, leaving it displaying ``streaming`` forever.
        state.latest_timestamp = message.edited_timestamp or message.timestamp
        state.latest_event_id = message.visible_event_id
        state.latest_content = {key: value for key, value in message.content.items() if isinstance(key, str)}
        state.thread_id = message.thread_id or thread_id
        state.stream_status = message.stream_status
        state.bot_user_id = message.sender
    return states


def _cleanup_trusted_sender_ids(
    *,
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> frozenset[str]:
    """Return the exact sender IDs cleanup may trust for canonical visible-body metadata."""
    trusted_sender_ids = set(current_internal_sender_ids(config, runtime_paths))
    trusted_sender_ids.update(bot_user_ids)
    return frozenset(trusted_sender_ids)


async def _edit_stale_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    new_text: str,
    preserved_content: dict[str, Any] | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Edit a stale message.

    No thread relation is built: ``build_edit_event_content`` pops ``m.relates_to`` off the
    replacement before sending, so a relation here would never reach the wire.
    """
    extra_content = _preserved_cleanup_content(preserved_content)
    should_preserve_visible_body = extra_content is not None and STREAM_VISIBLE_BODY_KEY in extra_content
    if should_preserve_visible_body and extra_content is not None:
        extra_content = dict(extra_content)
        extra_content.pop(STREAM_VISIBLE_BODY_KEY, None)
        extra_content.pop(STREAM_WARMUP_SUFFIX_KEY, None)
    content = format_message_with_mentions(
        config,
        runtime_paths,
        new_text,
        extra_content=extra_content,
    )
    if should_preserve_visible_body:
        canonical_visible_body = content["body"]
        content[STREAM_VISIBLE_BODY_KEY] = canonical_visible_body
        extra_content = dict(extra_content or {})
        extra_content[STREAM_VISIBLE_BODY_KEY] = canonical_visible_body

    delivered = await edit_message_result(
        client,
        room_id,
        target_event_id,
        content,
        new_text,
        extra_content=extra_content,
    )
    if delivered is not None:
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
    bot_user_ids: set[str],
) -> None:
    """Best-effort removal of stale bot-authored stop reactions."""
    reaction_event_ids: set[str] = set()
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
            "Failed to fetch exact stop reactions; leaving them for recovery retry",
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


def _has_restart_interrupted_note(body: str) -> bool:
    """Return whether the body already contains the restart interruption note."""
    return body.rstrip().endswith(RESTART_INTERRUPTED_RESPONSE_NOTE)


def _is_cleanup_candidate(state: _MessageState) -> bool:
    """Return whether the latest visible state represents stale in-progress output."""
    assert state.latest_body is not None
    if _has_restart_interrupted_note(state.latest_body):
        return False
    if state.stream_status == STREAM_STATUS_COMPLETED:
        return False
    return state.stream_status in {STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING}


def _should_skip_for_startup_cleanup_window(
    state: _MessageState,
    *,
    now_ms: int,
    startup_cutoff_ms: int | None,
) -> bool:
    """Return whether startup cleanup should ignore one candidate by age."""
    timestamp_ms = state.latest_timestamp
    return (
        _is_at_or_after_startup_cutoff(timestamp_ms, startup_cutoff_ms=startup_cutoff_ms)
        or _is_recent_timestamp(timestamp_ms, now_ms=now_ms)
        or _is_older_than_cleanup_window(timestamp_ms, now_ms=now_ms)
    )


def _is_at_or_after_startup_cutoff(timestamp_ms: int, *, startup_cutoff_ms: int | None) -> bool:
    """Return whether a message may have been created by the current process."""
    return startup_cutoff_ms is not None and timestamp_ms >= startup_cutoff_ms


def _is_recent_timestamp(timestamp_ms: int, *, now_ms: int | None = None) -> bool:
    """Return whether a timestamp is still within the startup recency guard."""
    current_time_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return current_time_ms - timestamp_ms < _STALE_STREAM_RECENCY_GUARD_MS


def _is_older_than_cleanup_window(timestamp_ms: int, *, now_ms: int | None = None) -> bool:
    """Return whether a timestamp is older than the restart cleanup lookback window."""
    current_time_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return current_time_ms - timestamp_ms > _STALE_STREAM_LOOKBACK_MS


def _agent_name_for_bot_user_id(
    bot_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Resolve a bot user ID back to its configured agent or team name."""
    return entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(bot_user_id)
