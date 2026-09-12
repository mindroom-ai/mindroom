"""Cleanup stale streaming messages left behind by restarts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio
from nio.api import RelationshipType

from mindroom.authorization import get_effective_sender_id_for_reply_permissions
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    SOURCE_KIND_KEY,
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
from mindroom.dispatch_source import (
    AUTO_RESUME_MESSAGE,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    is_auto_resume_relay_body,
)
from mindroom.entity_resolution import (
    MissingManagedEntityAccountError,
    current_entity_id,
    current_internal_sender_ids,
    entity_identity_registry,
)
from mindroom.event_journal.models import UnreadableMatrixDelivery
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import edit_message_result, send_message_result
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage, fetch_latest_visible_message
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content, markdown_to_html
from mindroom.matrix.message_content import extract_and_resolve_message, extract_edit_body
from mindroom.matrix.room_history_reads import fetch_thread_messages_from_source
from mindroom.streaming import (
    INTERRUPTED_RESPONSE_NOTE,
    RESTART_INTERRUPTED_RESPONSE_NOTE,
    build_restart_interrupted_body,
    clean_partial_reply_text,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Sequence

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
# outage window. Explicit terminal interrupted notes may still be auto-resumed
# later because they are already user-visible interrupted outcomes.
_STALE_STREAM_LOOKBACK_MS = 6 * 60 * 60 * 1000
_RATE_LIMIT_DELAY_SECONDS = 0.15
_RECOVERY_ROOM_CONCURRENCY = 8
_STOP_REACTION_KEYS = frozenset({"🛑", "⏹️"})
_MAX_REQUESTER_RESOLUTION_DEPTH = 10
_INTERRUPTED_PARTIAL_TEXT_LIMIT = 280
_TERMINAL_STREAM_STATUSES = frozenset(
    {STREAM_STATUS_CANCELLED, STREAM_STATUS_COMPLETED, STREAM_STATUS_ERROR, STREAM_STATUS_INTERRUPTED},
)


@dataclass(frozen=True)
class _InterruptedThread:
    """One interrupted thread that can be resumed after restart."""

    room_id: str
    thread_id: str | None
    target_event_id: str
    partial_text: str
    agent_name: str
    original_sender_id: str | None = None
    timestamp_ms: int = field(default=0, compare=False)


@dataclass(frozen=True)
class _StaleStreamRecoveryResult:
    """Aggregate outcome from one startup recovery sweep."""

    room_count: int
    cleaned_count: int
    resumed_count: int


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
    bot_user_id: str | None = None


@dataclass(frozen=True)
class _CleanupScanPolicy:
    """History scan bounds for one startup stale-stream cleanup run."""

    startup_cutoff_ms: int | None
    collect_terminal_interrupted_for_resume: bool
    terminal_interrupted_only: bool


def _cleanup_scan_policy(
    config: Config,
    *,
    startup_cutoff_ms: int | None,
    terminal_interrupted_only: bool = False,
) -> _CleanupScanPolicy:
    """Return history scan policy for one stale-stream cleanup run."""
    collect_terminal_interrupted_for_resume = config.defaults.auto_resume_after_restart
    return _CleanupScanPolicy(
        startup_cutoff_ms=startup_cutoff_ms,
        collect_terminal_interrupted_for_resume=collect_terminal_interrupted_for_resume,
        terminal_interrupted_only=terminal_interrupted_only,
    )


def _auto_resume_threads_for_room(
    room_id: str,
    interrupted_threads: list[_InterruptedThread],
    *,
    auto_resume_enabled: bool,
    resume_client: nio.AsyncClient | None,
    resume_room_ids: frozenset[str] | None,
    scanned_room_ids: set[str],
) -> list[_InterruptedThread]:
    """Keep eligible work and leave membership-deferred rooms retryable."""
    if (
        not auto_resume_enabled
        or not interrupted_threads
        or resume_client is None
        or (resume_room_ids is not None and room_id in resume_room_ids)
    ):
        return interrupted_threads
    scanned_room_ids.discard(room_id)
    logger.info(
        "Deferring auto-resume until resume identity membership is available",
        room_id=room_id,
        resume_user_id=resume_client.user_id,
        membership_known=resume_room_ids is not None,
        interrupted_count=len(interrupted_threads),
    )
    return []


def _requester_resolution_message(
    *,
    event_id: str,
    sender: str,
    content: dict[str, Any] | None,
    body: str | None,
    timestamp: int | None,
    thread_id: str | None = None,
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
        thread_id=thread_id,
    )


async def _recovery_room_targets(
    principals: dict[str, PrincipalStore],
    target_room_ids: set[str] | None,
) -> tuple[dict[str, dict[str, tuple[str, str | None]]], set[str]]:
    """Page durable candidates without waiting on individual response locks."""
    room_targets: dict[str, dict[str, tuple[str, str | None]]] = {}
    failed_room_ids: set[str] = set()
    for bot_user_id, principal in principals.items():
        cursor: tuple[int, str] | None = None
        while batch := await principal.recovery_initial_deliveries(after=cursor):
            cursor = (batch[-1].created_at_ns, batch[-1].delivery_id)
            for delivery in batch:
                if target_room_ids is not None and delivery.room_id not in target_room_ids:
                    continue
                if isinstance(delivery, UnreadableMatrixDelivery):
                    logger.warning("Unreadable startup recovery delivery", delivery_id=delivery.delivery_id)
                    failed_room_ids.add(delivery.room_id)
                    continue
                event_id = delivery.acknowledged_event_id
                assert event_id is not None
                room_targets.setdefault(delivery.room_id, {})[event_id] = (bot_user_id, delivery.thread_id)
    return room_targets, failed_room_ids


async def recover_stale_streaming_messages(
    actors: dict[str, nio.AsyncClient],
    *,
    principals: dict[str, PrincipalStore],
    resume_client: nio.AsyncClient | None,
    response_recovery_scope: _ResponseRecoveryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    startup_cutoff_ms: int | None,
    scanned_room_ids: set[str],
    target_room_ids: set[str] | None = None,
    room_concurrency: int = _RECOVERY_ROOM_CONCURRENCY,
) -> _StaleStreamRecoveryResult:
    """Recover exact owned INITIALs; an empty outbox needs no Matrix history."""
    agent_names = {
        user_id: name
        for user_id in actors
        if (name := _agent_name_for_bot_user_id(user_id, config, runtime_paths)) is not None
    }
    room_targets, failed_room_ids = await _recovery_room_targets(
        {user_id: principal for user_id, principal in principals.items() if user_id in agent_names},
        target_room_ids,
    )
    # This set reports completed replacement rooms; it is not a discovery cache.
    # A late INITIAL ACK in an already visited room must be visible on the next pass.
    scanned_room_ids.update(target_room_ids or ())
    scanned_room_ids.difference_update(failed_room_ids)
    if not room_targets:
        return _StaleStreamRecoveryResult(room_count=0, cleaned_count=0, resumed_count=0)

    resume_room_ids = await _resume_membership(
        resume_client if config.defaults.auto_resume_after_restart else None,
    )
    semaphore = asyncio.Semaphore(max(1, room_concurrency))

    async def recover_room(
        room_id: str,
        targets: dict[str, tuple[str, str | None]],
    ) -> tuple[int, list[_InterruptedThread]]:
        async with semaphore:
            cleaned = 0
            interrupted: list[_InterruptedThread] = []
            failed = room_id in failed_room_ids
            for event_id, (bot_user_id, thread_id) in targets.items():
                try:
                    async with response_recovery_scope(agent_names[bot_user_id], room_id, event_id) as permitted:
                        if not permitted:
                            continue
                    count, threads = await _cleanup_stale_streaming_room(
                        actors[bot_user_id],
                        room_id=room_id,
                        actors={bot_user_id: actors[bot_user_id]},
                        target_thread_ids={event_id: thread_id},
                        bot_user_ids=set(actors),
                        config=config,
                        runtime_paths=runtime_paths,
                        startup_cutoff_ms=startup_cutoff_ms,
                        terminal_interrupted_only=target_room_ids is not None,
                        response_recovery_scope=response_recovery_scope,
                    )
                    cleaned += count
                    interrupted.extend(threads)
                except Exception:
                    failed = True
                    logger.warning(
                        "Failed exact startup response recovery",
                        room_id=room_id,
                        event_id=event_id,
                        exc_info=True,
                    )
            if failed:
                scanned_room_ids.discard(room_id)
            else:
                scanned_room_ids.add(room_id)
            return cleaned, _auto_resume_threads_for_room(
                room_id,
                interrupted,
                auto_resume_enabled=config.defaults.auto_resume_after_restart,
                resume_client=resume_client,
                resume_room_ids=resume_room_ids,
                scanned_room_ids=scanned_room_ids,
            )

    tasks = [
        asyncio.create_task(recover_room(room_id, targets), name=f"response_recovery:{room_id}")
        for room_id, targets in room_targets.items()
    ]
    cleaned_count = resumed_count = 0
    try:
        for completed in asyncio.as_completed(tasks):
            cleaned, interrupted = await completed
            cleaned_count += cleaned
            if resume_client is not None and config.defaults.auto_resume_after_restart and interrupted:
                resumed_count += await _auto_resume_interrupted_threads(
                    resume_client,
                    interrupted,
                    response_recovery_scope=response_recovery_scope,
                    config=config,
                    runtime_paths=runtime_paths,
                    delay_before_first=resumed_count > 0,
                )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return _StaleStreamRecoveryResult(len(room_targets), cleaned_count, resumed_count)


async def _resume_membership(client: nio.AsyncClient | None) -> frozenset[str] | None:
    """Keep a resume-identity outage from preventing owned response cleanup."""
    if client is None:
        return None
    try:
        joined = await get_joined_rooms(client)
        return frozenset(joined) if joined is not None else None
    except Exception:
        logger.warning("Failed to read startup resume membership; retaining recovery", exc_info=True)
        return None


async def _auto_resume_interrupted_threads(
    client: nio.AsyncClient,
    interrupted: list[_InterruptedThread],
    *,
    response_recovery_scope: _ResponseRecoveryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    delay: float = 2.0,
    delay_before_first: bool = False,
) -> int:
    """Send resume prompts for interrupted threaded conversations."""
    if not interrupted:
        return 0

    candidate_threads = _ordered_auto_resume_candidates(interrupted)
    resumed_count = 0
    delay_due = delay_before_first
    for interrupted_thread in candidate_threads:
        if interrupted_thread.original_sender_id is None:
            logger.warning(
                "Skipping auto-resume because requester identity could not be resolved",
                room_id=interrupted_thread.room_id,
                thread_id=interrupted_thread.thread_id,
                target_event_id=interrupted_thread.target_event_id,
            )
            continue
        if delay_due:
            await asyncio.sleep(delay)
            delay_due = False
        if not await _interrupted_target_remains_latest_human_work(
            interrupted_thread,
            client=client,
            config=config,
            runtime_paths=runtime_paths,
        ):
            continue
        try:
            async with response_recovery_scope(
                interrupted_thread.agent_name,
                interrupted_thread.room_id,
                interrupted_thread.target_event_id,
            ) as permitted:
                if not permitted:
                    continue
                content = _build_auto_resume_content(
                    interrupted_thread,
                    config=config,
                    runtime_paths=runtime_paths,
                )
                delay_due = True
                delivered = await send_message_result(client, interrupted_thread.room_id, content)
                if delivered is not None:
                    logger.info(
                        "Queued auto-resume after restart",
                        room_id=interrupted_thread.room_id,
                        thread_id=interrupted_thread.thread_id,
                        target_event_id=interrupted_thread.target_event_id,
                        event_id=delivered.event_id,
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

    return resumed_count


async def _interrupted_target_remains_latest_human_work(
    interrupted_thread: _InterruptedThread,
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether authoritative history has no newer effective human activity.

    Reads the homeserver rather than the projection. This runs at startup to
    decide whether a turn interrupted by the last shutdown is still the newest
    thing in its thread, and anything written while this process was down has
    by definition not reached its local state yet.
    """
    if interrupted_thread.thread_id is None:
        return False

    try:
        history = await fetch_thread_messages_from_source(
            client,
            interrupted_thread.room_id,
            interrupted_thread.thread_id,
        )
        later_messages = _authoritative_history_after_target(
            history,
            target_event_id=interrupted_thread.target_event_id,
        )
        if later_messages and interrupted_thread.target_event_id in _auto_resume_target_event_ids(
            later_messages,
            bot_user_ids=current_internal_sender_ids(config, runtime_paths),
        ):
            return False
        remains_latest = _later_thread_activity_is_internal(
            later_messages,
            config=config,
            runtime_paths=runtime_paths,
        )
    except Exception as exc:
        logger.warning(
            "Skipping auto-resume because authoritative freshness check failed",
            target_event_id=interrupted_thread.target_event_id,
            error=str(exc),
        )
        return False

    if not remains_latest:
        logger.info(
            "Skipping stale auto-resume after newer human activity",
            target_event_id=interrupted_thread.target_event_id,
        )
    return remains_latest


def _authoritative_history_after_target(
    history: Sequence[ResolvedVisibleMessage],
    *,
    target_event_id: str,
) -> Sequence[ResolvedVisibleMessage]:
    """Return history entries after an exact target, or raise if the target is absent.

    This used to take a `ThreadReadResult` and check three things before
    trusting it: that the read was complete, that it was not degraded, and that
    it came from the homeserver rather than a cache. All three are now
    discharged at the source instead of inspected here.

    `fetch_thread_messages_from_source` only reads `/messages`, so there is no
    non-authoritative source it could return. And it raises rather than
    returning a partial answer -- `ThreadRoomScanRootNotFoundError` when the
    scan ends without the root, `UnresolvedOpaqueRoomHistoryError` when
    relation-bearing ciphertext it cannot read would change the result. A
    caller that reaches this function therefore holds whole, authoritative
    history by construction, and the remaining question is only whether the
    target is in it.
    """
    target_index = next(
        (index for index, message in enumerate(history) if message.event_id == target_event_id),
        None,
    )
    if target_index is None:
        msg = f"Interrupted target absent from thread history: {target_event_id}"
        raise ValueError(msg)
    return history[target_index + 1 :]


def _later_thread_activity_is_internal(
    messages: Sequence[ResolvedVisibleMessage],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether every later event is from an effective internal requester."""
    if not messages:
        return True

    internal_sender_ids = current_internal_sender_ids(config, runtime_paths)
    for message in messages:
        if not message.sender:
            msg = "Thread history contains a message without a trustworthy sender"
            raise ValueError(msg)
        if message.sender in internal_sender_ids and is_auto_resume_relay_body(message.body):
            continue
        effective_requester = _effective_requester_for_message(
            message,
            config=config,
            runtime_paths=runtime_paths,
        )
        if not effective_requester:
            msg = "Thread history sender classification returned no requester"
            raise ValueError(msg)
        if effective_requester not in internal_sender_ids:
            return False
    return True


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
    terminal_interrupted_only: bool = False,
    response_recovery_scope: _ResponseRecoveryScope,
) -> tuple[int, list[_InterruptedThread]]:
    """Resolve owned targets and let each bot account repair its own messages."""
    if not actors:
        return 0, []
    current_time_ms = int(time.time() * 1000)
    scan_policy = _cleanup_scan_policy(
        config,
        startup_cutoff_ms=startup_cutoff_ms,
        terminal_interrupted_only=terminal_interrupted_only,
    )
    message_states = await _load_recovery_message_states(
        scan_client,
        room_id=room_id,
        target_thread_ids=target_thread_ids,
        cleanup_bot_user_ids=set(actors),
        bot_user_ids=bot_user_ids,
        config=config,
        runtime_paths=runtime_paths,
        now_ms=current_time_ms,
        scan_policy=scan_policy,
    )
    if not message_states:
        return 0, []

    cleaned_count = 0
    prior_edit_succeeded_by_bot: set[str] = set()
    interrupted_threads: list[_InterruptedThread] = []
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
            edited, interrupted = await _process_stale_room_candidate(
                actor_client,
                bot_user_id=bot_user_id,
                room_id=room_id,
                target_event_id=target_event_id,
                state=state,
                bot_user_ids=bot_user_ids,
                config=config,
                runtime_paths=runtime_paths,
                current_time_ms=current_time_ms,
                scan_policy=scan_policy,
                prior_edit_succeeded=bot_user_id in prior_edit_succeeded_by_bot,
            )
        if edited:
            cleaned_count += 1
            prior_edit_succeeded_by_bot.add(bot_user_id)
        if interrupted is not None:
            interrupted_threads.append(interrupted)

    return cleaned_count, interrupted_threads


async def _process_stale_room_candidate(
    client: nio.AsyncClient,
    *,
    bot_user_id: str,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
    current_time_ms: int,
    scan_policy: _CleanupScanPolicy,
    prior_edit_succeeded: bool,
) -> tuple[bool, _InterruptedThread | None]:
    """Repair or classify one bot-owned candidate from a shared room scan."""
    assert state.latest_body is not None
    agent_name = _agent_name_for_bot_user_id(bot_user_id, config, runtime_paths)
    if agent_name is None or not _needs_recovery(state, now_ms=current_time_ms, scan_policy=scan_policy):
        return False, None
    if _is_cleanup_candidate(state):
        return await _cleanup_candidate_message(
            client,
            room_id=room_id,
            target_event_id=target_event_id,
            state=state,
            bot_user_ids=bot_user_ids,
            config=config,
            runtime_paths=runtime_paths,
            agent_name=agent_name,
            prior_edit_succeeded=prior_edit_succeeded,
        )
    return await _handle_interrupted_message(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
        state=state,
        can_auto_resume=_has_resumable_interrupted_note(state),
        bot_user_ids=bot_user_ids,
        config=config,
        runtime_paths=runtime_paths,
        agent_name=agent_name,
        prior_edit_succeeded=prior_edit_succeeded,
    )


def _needs_recovery(state: _MessageState, *, now_ms: int, scan_policy: _CleanupScanPolicy) -> bool:
    """Select work before resolving requester chains, using the execution policy."""
    if state.latest_body is None or _should_skip_for_startup_cleanup_window(
        state,
        now_ms=now_ms,
        scan_policy=scan_policy,
    ):
        return False
    if scan_policy.terminal_interrupted_only and not _has_resumable_interrupted_note(state):
        return False
    return (
        _is_cleanup_candidate(state)
        or _has_restart_interrupted_note(state.latest_body)
        or _has_resumable_interrupted_note(state)
    )


async def _handle_interrupted_message(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    can_auto_resume: bool,
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    prior_edit_succeeded: bool,
) -> tuple[bool, _InterruptedThread | None]:
    """Handle an interrupted response or restart marker seen during startup cleanup."""
    interrupted = None
    if can_auto_resume:
        interrupted = _interrupted_thread_from_terminal_state(
            room_id=room_id,
            target_event_id=target_event_id,
            state=state,
            agent_name=agent_name,
        )
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
    return repaired, interrupted


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
    agent_name: str,
) -> tuple[bool, _InterruptedThread | None]:
    """Edit one stale message, redact stop reactions, return interrupted thread info."""
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
        return False, None

    interrupted: _InterruptedThread | None = None
    if state.thread_id is not None:
        interrupted = _InterruptedThread(
            room_id=room_id,
            thread_id=state.thread_id,
            target_event_id=target_event_id,
            partial_text=_truncate_partial_text(clean_partial_reply_text(state.latest_body)),
            agent_name=agent_name,
            original_sender_id=state.requester_user_id,
            timestamp_ms=state.latest_timestamp,
        )
    await _redact_stop_reactions(
        client,
        room_id=room_id,
        target_event_id=target_event_id,
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
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    prior_edit_succeeded: bool,
) -> tuple[bool, _InterruptedThread | None]:
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


async def _load_recovery_message_states(
    client: nio.AsyncClient,
    *,
    room_id: str,
    target_thread_ids: dict[str, str | None],
    cleanup_bot_user_ids: set[str],
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
    now_ms: int,
    scan_policy: _CleanupScanPolicy,
) -> dict[str, _MessageState]:
    """Resolve only exact outbox targets, including their newest replacements."""
    trusted = _cleanup_trusted_sender_ids(bot_user_ids=bot_user_ids, config=config, runtime_paths=runtime_paths)
    messages: dict[str, ResolvedVisibleMessage] = {}
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
        messages[event_id] = message
        _merge_resolved_message_state(
            states,
            target_event_id=event_id,
            message=message,
            bot_user_id=message.sender,
            requester_user_id=None,
            fallback_thread_id=thread_id,
        )
    candidates = {
        event_id for event_id, state in states.items() if _needs_recovery(state, now_ms=now_ms, scan_policy=scan_policy)
    }
    requesters = await _derive_requester_ids_for_bot_messages(
        client,
        messages,
        messages,
        target_event_ids=candidates,
        room_id=room_id,
        bot_user_ids=cleanup_bot_user_ids,
        config=config,
        runtime_paths=runtime_paths,
    )
    for event_id, requester in requesters.items():
        states[event_id].requester_user_id = requester
    return states


def _auto_resume_target_event_ids(
    messages: Iterable[ResolvedVisibleMessage],
    *,
    bot_user_ids: AbstractSet[str],
) -> set[str]:
    """Return interrupted event IDs that already have a queued auto-resume relay."""
    target_event_ids: set[str] = set()
    for message in messages:
        if message.sender not in bot_user_ids or not is_auto_resume_relay_body(message.body):
            continue
        reply_to_event_id = message.reply_to_event_id
        if reply_to_event_id is not None:
            target_event_ids.add(reply_to_event_id)
    return target_event_ids


def _merge_resolved_message_state(
    message_states: dict[str, _MessageState],
    *,
    target_event_id: str,
    message: ResolvedVisibleMessage,
    bot_user_id: str,
    requester_user_id: str | None,
    fallback_thread_id: str | None = None,
) -> None:
    """Store one resolved message if it has the fields cleanup needs."""
    normalized_latest_content = {key: value for key, value in message.content.items() if isinstance(key, str)}
    state = message_states.setdefault(target_event_id, _MessageState())
    state.latest_body = message.body
    # The last time this message changed, not when it was created. ``timestamp`` deliberately
    # stays the original event's so an edit cannot reorder a thread, which means it is the
    # wrong clock for "is this stream still active": a placeholder posted eight hours ago and
    # edited seconds before a restart would read as older than the cleanup window and be
    # skipped, leaving it displaying ``streaming`` forever.
    state.latest_timestamp = message.edited_timestamp or message.timestamp
    state.latest_event_id = message.visible_event_id
    state.latest_content = normalized_latest_content
    state.thread_id = message.thread_id or fallback_thread_id
    state.stream_status = message.stream_status
    state.requester_user_id = requester_user_id
    state.bot_user_id = bot_user_id


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
    target_event_ids: set[str],
    room_id: str,
    bot_user_ids: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, str]:
    """Return effective requester IDs for bot-authored messages."""
    requester_ids_by_event_id: dict[str, str] = {}
    requester_cache: dict[str, str | None] = {}
    fetched_message_data_by_event_id: dict[str, ResolvedVisibleMessage | None] = {}
    trusted_sender_ids = set(
        _cleanup_trusted_sender_ids(
            bot_user_ids=bot_user_ids,
            config=config,
            runtime_paths=runtime_paths,
        ),
    )
    sorted_messages = sorted(
        ((event_id, message) for event_id, message in resolved_messages.items() if event_id in target_event_ids),
        key=lambda item: (item[1].timestamp, item[0]),
    )

    for target_event_id, message_data in sorted_messages:
        sender = message_data.sender
        if sender not in bot_user_ids:
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
                trusted_sender_ids=trusted_sender_ids,
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
    trusted_sender_ids: set[str],
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
            trusted_sender_ids=trusted_sender_ids,
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
        trusted_sender_ids=trusted_sender_ids,
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
    trusted_sender_ids: set[str],
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
        trusted_sender_ids=trusted_sender_ids,
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
                trusted_sender_ids=trusted_sender_ids,
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
    trusted_sender_ids: set[str],
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
        trusted_sender_ids=trusted_sender_ids,
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
    trusted_sender_ids: set[str],
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
            trusted_sender_ids=trusted_sender_ids,
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
        trusted_sender_ids=trusted_sender_ids,
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
    trusted_sender_ids: set[str],
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
        trusted_sender_ids=trusted_sender_ids,
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
    trusted_sender_ids: set[str],
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
    if isinstance(event, (nio.RoomMessageText, nio.RoomMessageNotice)):
        if event_info.is_edit:
            edited_body, edited_content = await extract_edit_body(
                event_source,
                client,
                trusted_sender_ids=trusted_sender_ids,
            )
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

        extracted_message = await extract_and_resolve_message(
            event,
            client,
            trusted_sender_ids=trusted_sender_ids,
        )
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


def _is_internal_sender(
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether the sender is one of MindRoom's own Matrix accounts."""
    return sender_id in current_internal_sender_ids(config, runtime_paths)


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


def _truncate_partial_text(text: str, *, limit: int = _INTERRUPTED_PARTIAL_TEXT_LIMIT) -> str:
    """Return a compact partial-text preview."""
    stripped_text = text.strip()
    if len(stripped_text) <= limit:
        return stripped_text
    return f"{stripped_text[: limit - 1]}…"


def _ordered_auto_resume_candidates(
    interrupted: list[_InterruptedThread],
) -> list[_InterruptedThread]:
    """Return each unique interrupted thread once in timestamp order."""
    latest_by_key: dict[tuple[str, str, str], _InterruptedThread] = {}

    for interrupted_thread in interrupted:
        if interrupted_thread.thread_id is None:
            continue
        key = (interrupted_thread.room_id, interrupted_thread.thread_id, interrupted_thread.agent_name)
        existing = latest_by_key.get(key)
        if existing is None or interrupted_thread.timestamp_ms >= existing.timestamp_ms:
            latest_by_key[key] = interrupted_thread

    return sorted(
        latest_by_key.values(),
        key=lambda interrupted_thread: (
            interrupted_thread.timestamp_ms,
            interrupted_thread.room_id,
            interrupted_thread.thread_id or "",
            interrupted_thread.agent_name,
        ),
    )


def _has_restart_interrupted_note(body: str) -> bool:
    """Return whether the body already contains the restart interruption note."""
    return body.rstrip().endswith(RESTART_INTERRUPTED_RESPONSE_NOTE)


def _has_generic_interrupted_note(body: str) -> bool:
    """Return whether the body has a terminal generic interrupted note."""
    return body.rstrip().endswith(INTERRUPTED_RESPONSE_NOTE)


def _has_resumable_interrupted_note(state: _MessageState) -> bool:
    """Return whether the visible body represents a restart-resumable interruption."""
    assert state.latest_body is not None
    if _has_restart_interrupted_note(state.latest_body):
        return state.stream_status in {None, STREAM_STATUS_ERROR, STREAM_STATUS_INTERRUPTED}
    return state.stream_status in {
        STREAM_STATUS_ERROR,
        STREAM_STATUS_INTERRUPTED,
    } and _has_generic_interrupted_note(state.latest_body)


def _interrupted_thread_from_terminal_state(
    *,
    room_id: str,
    target_event_id: str,
    state: _MessageState,
    agent_name: str,
) -> _InterruptedThread | None:
    """Build an auto-resume record for an already-terminal interrupted response."""
    assert state.latest_body is not None
    if state.thread_id is None:
        return None
    return _InterruptedThread(
        room_id=room_id,
        thread_id=state.thread_id,
        target_event_id=target_event_id,
        partial_text=_truncate_partial_text(clean_partial_reply_text(state.latest_body)),
        agent_name=agent_name,
        original_sender_id=state.requester_user_id,
        timestamp_ms=state.latest_timestamp,
    )


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
    scan_policy: _CleanupScanPolicy,
) -> bool:
    """Return whether startup cleanup should ignore one candidate by age."""
    timestamp_ms = state.latest_timestamp
    if _is_at_or_after_startup_cutoff(timestamp_ms, startup_cutoff_ms=scan_policy.startup_cutoff_ms):
        return True
    # A terminal interruption cannot still be receiving chunks. Targeted
    # replacement recovery passes no cutoff because local and Matrix clocks
    # are not comparable.
    if _is_recent_timestamp(timestamp_ms, now_ms=now_ms) and not (
        scan_policy.collect_terminal_interrupted_for_resume and _has_resumable_interrupted_note(state)
    ):
        return True
    if _is_older_than_cleanup_window(timestamp_ms, now_ms=now_ms):
        return not (scan_policy.collect_terminal_interrupted_for_resume and _has_resumable_interrupted_note(state))
    return False


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


def _build_auto_resume_content(
    interrupted_thread: _InterruptedThread,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, object]:
    """Build the router-authored visible resume relay for one interrupted agent."""
    target_user_id = _current_configured_entity_user_id(interrupted_thread.agent_name, config, runtime_paths)
    display_name = _entity_display_name(interrupted_thread.agent_name, config)

    body = AUTO_RESUME_MESSAGE
    formatted_body: str | None = None
    mentioned_user_ids: list[str] | None = None
    if target_user_id is not None:
        body = f"@{display_name} {AUTO_RESUME_MESSAGE}"
        formatted_body = markdown_to_html(
            f"[@{display_name}](https://matrix.to/#/{target_user_id}) {AUTO_RESUME_MESSAGE}",
        )
        mentioned_user_ids = [target_user_id]

    extra_content = None
    if interrupted_thread.original_sender_id is not None:
        extra_content = {
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            ORIGINAL_SENDER_KEY: interrupted_thread.original_sender_id,
        }
    return build_message_content(
        body=body,
        formatted_body=formatted_body,
        mentioned_user_ids=mentioned_user_ids,
        thread_event_id=interrupted_thread.thread_id,
        reply_to_event_id=interrupted_thread.target_event_id,
        latest_thread_event_id=interrupted_thread.target_event_id,
        extra_content=extra_content,
    )


def _current_configured_entity_user_id(
    entity_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return one configured entity user ID without resolving unrelated entities."""
    if entity_name not in config.agents and entity_name not in config.teams:
        return None
    try:
        return current_entity_id(entity_name, runtime_paths).full_id
    except MissingManagedEntityAccountError:
        logger.debug("auto_resume_target_entity_account_unavailable", entity_name=entity_name)
        return None


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
    return entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(bot_user_id)
