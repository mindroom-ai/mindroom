"""Atomicity of the invalidate-then-append sequence behind every threaded cache mutation.

A threaded mutation used to mark its thread stale, append the event, and only then restore
validation, each as a separate durable operation. Between the first and the last the snapshot was
observably untrusted even though the mutation was going to succeed, so any read landing in that
window rejected a perfectly good cache and paid for a full history scan.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.cache import thread_cache_rejection_reason
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_cache_state import ThreadAppendOutcome
from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.thread_writes import _apply_thread_message_mutation
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact, MutationThreadImpactState
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine
    from pathlib import Path

    from mindroom.matrix.cache import ConversationEventCache

ROOM_ID = "!room:localhost"
THREAD_ID = "$thread:localhost"
PRINCIPAL_ID = "@agent:localhost"


def _event(event_id: str, timestamp: int, *, thread_id: str | None = None) -> dict[str, Any]:
    content: dict[str, Any] = {"body": event_id, "msgtype": "m.text"}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": content,
    }


@pytest_asyncio.fixture
async def cache(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> AsyncGenerator[ConversationEventCache, None]:
    """Yield one initialized principal-scoped cache for each supported backend."""
    root_cache = event_cache_factory()
    principal_cache = root_cache.for_principal(PRINCIPAL_ID)
    await principal_cache.initialize()
    try:
        yield principal_cache
    finally:
        await root_cache.close()


async def _seed_valid_thread(cache: ConversationEventCache) -> None:
    await cache.replace_thread_if_not_newer(
        ROOM_ID,
        THREAD_ID,
        [_event(THREAD_ID, 1000), _event("$initial", 1500, thread_id=THREAD_ID)],
        expected_membership_epoch=await cache.room_membership_epoch(ROOM_ID),
        fetch_started_at=0.0,
    )
    assert thread_cache_rejection_reason(await cache.get_thread_cache_state(ROOM_ID, THREAD_ID)) is None


async def _collect_rejections_while(
    cache: ConversationEventCache,
    mutate: Callable[[], Coroutine[Any, Any, None]],
) -> list[str]:
    """Run one mutation loop while a reader polls, returning every rejection it observed."""
    rejections: list[str] = []
    stop_reading = asyncio.Event()

    async def read_until_stopped() -> None:
        while not stop_reading.is_set():
            reason = thread_cache_rejection_reason(await cache.get_thread_cache_state(ROOM_ID, THREAD_ID))
            if reason is not None:
                rejections.append(reason)
            await asyncio.sleep(0)

    reader = asyncio.create_task(read_until_stopped())
    try:
        await mutate()
    finally:
        stop_reading.set()
        await reader
    return rejections


@pytest.mark.asyncio
async def test_appending_a_mutation_never_exposes_an_invalid_snapshot(cache: ConversationEventCache) -> None:
    """A concurrent reader must never see a valid thread go stale for a mutation that succeeds."""
    await _seed_valid_thread(cache)

    async def mutate() -> None:
        for index in range(25):
            outcome = await cache.apply_thread_mutation_append(
                ROOM_ID,
                THREAD_ID,
                _event(f"$edit-{index}", 2000 + index, thread_id=THREAD_ID),
                append_failed_reason="sync_append_failed",
            )
            assert outcome is ThreadAppendOutcome.APPENDED

    rejections = await _collect_rejections_while(cache, mutate)

    assert rejections == [], f"reader observed {len(rejections)} rejections of a thread that stayed appendable"


@pytest.mark.asyncio
async def test_mutation_on_a_snapshotless_thread_reports_it_distinctly(cache: ConversationEventCache) -> None:
    """A thread with no rows to append into must be reported apart from a refused append."""
    outcome = await cache.apply_thread_mutation_append(
        ROOM_ID,
        THREAD_ID,
        _event("$live", 2000, thread_id=THREAD_ID),
        append_failed_reason="sync_append_failed",
    )
    state = await cache.get_thread_cache_state(ROOM_ID, THREAD_ID)

    assert outcome is ThreadAppendOutcome.SNAPSHOT_MISSING
    assert outcome.needs_full_repair is True
    assert outcome.wrote_event is False
    assert thread_cache_rejection_reason(state) is not None


@pytest.mark.asyncio
async def test_append_clears_an_invalidation_left_by_an_earlier_mutation(cache: ConversationEventCache) -> None:
    """The incremental revalidation allowlist must still apply when a prior mutation left a marker."""
    await _seed_valid_thread(cache)
    await cache.mark_thread_stale(ROOM_ID, THREAD_ID, reason="sync_thread_mutation")
    assert thread_cache_rejection_reason(await cache.get_thread_cache_state(ROOM_ID, THREAD_ID)) is not None

    outcome = await cache.apply_thread_mutation_append(
        ROOM_ID,
        THREAD_ID,
        _event("$live", 2000, thread_id=THREAD_ID),
        append_failed_reason="sync_append_failed",
    )
    state = await cache.get_thread_cache_state(ROOM_ID, THREAD_ID)

    assert outcome is ThreadAppendOutcome.APPENDED
    assert thread_cache_rejection_reason(state) is None


@pytest.mark.asyncio
async def test_append_does_not_clear_an_invalidation_outside_the_allowlist(cache: ConversationEventCache) -> None:
    """A non-incremental reason must survive an append, exactly as it did before."""
    await _seed_valid_thread(cache)
    await cache.mark_thread_stale(ROOM_ID, THREAD_ID, reason="retained_thread_delta_missing")

    outcome = await cache.apply_thread_mutation_append(
        ROOM_ID,
        THREAD_ID,
        _event("$live", 2000, thread_id=THREAD_ID),
        append_failed_reason="sync_append_failed",
    )
    state = await cache.get_thread_cache_state(ROOM_ID, THREAD_ID)

    assert outcome is ThreadAppendOutcome.APPENDED_STALE
    assert outcome.wrote_event is True
    assert outcome.needs_full_repair is False
    assert thread_cache_rejection_reason(state) is not None


@pytest.mark.asyncio
async def test_room_invalidation_still_blocks_revalidation_after_append(cache: ConversationEventCache) -> None:
    """A room-wide marker must keep outranking an incremental append."""
    await _seed_valid_thread(cache)
    await cache.mark_room_threads_stale(ROOM_ID, reason="limited_sync_timeline")

    outcome = await cache.apply_thread_mutation_append(
        ROOM_ID,
        THREAD_ID,
        _event("$live", 2000, thread_id=THREAD_ID),
        append_failed_reason="sync_append_failed",
    )
    state = await cache.get_thread_cache_state(ROOM_ID, THREAD_ID)

    assert outcome is ThreadAppendOutcome.APPENDED_STALE
    assert thread_cache_rejection_reason(state) is not None


def _cache_ops(tmp_path: Path, cache: ConversationEventCache) -> ThreadMutationCacheOps:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=[ROOM_ID])},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    runtime = BotRuntimeState(
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=cache,
        event_cache_write_coordinator=EventCacheWriteCoordinator(logger=MagicMock()),
    )
    return ThreadMutationCacheOps(logger_getter=MagicMock, runtime=runtime)


@pytest.mark.asyncio
async def test_write_policy_mutation_never_exposes_an_invalid_snapshot(tmp_path: Path) -> None:
    """The production write policy, not just the cache operation, must close the window."""
    root_cache = SqliteEventCache(tmp_path / "event_cache.db")
    cache = root_cache.for_principal(PRINCIPAL_ID)
    await cache.initialize()
    cache_ops = _cache_ops(tmp_path, cache)
    impact = MutationThreadImpact(state=MutationThreadImpactState.THREADED, thread_id=THREAD_ID)

    async def mutate() -> None:
        for index in range(25):
            event_source = _event(f"$edit-{index}", 2000 + index, thread_id=THREAD_ID)
            await _apply_thread_message_mutation(
                cache_ops=cache_ops,
                room_id=ROOM_ID,
                event_info=EventInfo.from_event(event_source),
                impact=impact,
                event_source=event_source,
                event_id=str(event_source["event_id"]),
                context="sync",
                room_level_skip_message="skip",
                invalidate_on_append_failure=True,
            )

    try:
        await _seed_valid_thread(cache)
        rejections = await _collect_rejections_while(cache, mutate)
    finally:
        await root_cache.close()

    assert rejections == [], f"reader observed {len(rejections)} rejections during successful sync mutations"


@pytest.mark.asyncio
async def test_mutation_for_a_redacted_event_never_writes_its_payload(cache: ConversationEventCache) -> None:
    """A redacted event must not reach the point-lookup table, snapshot or no snapshot."""
    redacted_reply = _event("$redacted", 2000, thread_id=THREAD_ID)
    await cache.store_events_batch([("$redacted", ROOM_ID, redacted_reply)])
    await cache.redact_event(ROOM_ID, "$redacted")
    assert await cache.get_event(ROOM_ID, "$redacted") is None

    # No snapshot rows exist for this thread, which is the path that skipped the redaction guard.
    outcome = await cache.apply_thread_mutation_append(
        ROOM_ID,
        THREAD_ID,
        redacted_reply,
        append_failed_reason="sync_append_failed",
    )

    assert outcome is ThreadAppendOutcome.APPEND_REFUSED
    assert await cache.get_event(ROOM_ID, "$redacted") is None


@pytest.mark.asyncio
async def test_a_failed_cache_write_never_leaves_a_trusted_snapshot(tmp_path: Path) -> None:
    """When the atomic operation rolls back, its marker rolls back too and must be rewritten."""
    root_cache = SqliteEventCache(tmp_path / "event_cache.db")
    cache = root_cache.for_principal(PRINCIPAL_ID)
    await cache.initialize()
    cache_ops = _cache_ops(tmp_path, cache)
    impact = MutationThreadImpact(state=MutationThreadImpactState.THREADED, thread_id=THREAD_ID)
    event_source = _event("$live", 2000, thread_id=THREAD_ID)

    try:
        await _seed_valid_thread(cache)
        with patch.object(
            cache,
            "apply_thread_mutation_append",
            AsyncMock(side_effect=RuntimeError("cache write failed")),
        ):
            await _apply_thread_message_mutation(
                cache_ops=cache_ops,
                room_id=ROOM_ID,
                event_info=EventInfo.from_event(event_source),
                impact=impact,
                event_source=event_source,
                event_id="$live",
                context="sync",
                room_level_skip_message="skip",
                invalidate_on_append_failure=True,
            )
        state = await cache.get_thread_cache_state(ROOM_ID, THREAD_ID)
    finally:
        await root_cache.close()

    assert thread_cache_rejection_reason(state) == "thread_invalidated_after_validation"
    assert state is not None
    assert state.invalidation_reason == "sync_append_failed"


@pytest.mark.asyncio
async def test_a_cancelled_cache_write_never_leaves_a_trusted_snapshot(tmp_path: Path) -> None:
    """Cancellation rolls the transaction back too, so it must still leave a durable marker."""
    root_cache = SqliteEventCache(tmp_path / "event_cache.db")
    cache = root_cache.for_principal(PRINCIPAL_ID)
    await cache.initialize()
    cache_ops = _cache_ops(tmp_path, cache)
    impact = MutationThreadImpact(state=MutationThreadImpactState.THREADED, thread_id=THREAD_ID)
    event_source = _event("$live", 2000, thread_id=THREAD_ID)

    async def cancelled_append(*_args: object, **_kwargs: object) -> ThreadAppendOutcome:
        raise asyncio.CancelledError

    try:
        await _seed_valid_thread(cache)
        with (
            patch.object(cache, "apply_thread_mutation_append", cancelled_append),
            pytest.raises(asyncio.CancelledError),
        ):
            await _apply_thread_message_mutation(
                cache_ops=cache_ops,
                room_id=ROOM_ID,
                event_info=EventInfo.from_event(event_source),
                impact=impact,
                event_source=event_source,
                event_id="$live",
                context="sync",
                room_level_skip_message="skip",
                invalidate_on_append_failure=True,
            )
        state = await cache.get_thread_cache_state(ROOM_ID, THREAD_ID)
    finally:
        await root_cache.close()

    assert thread_cache_rejection_reason(state) == "thread_invalidated_after_validation"
    assert state is not None
    assert state.invalidation_reason == "sync_append_failed"
