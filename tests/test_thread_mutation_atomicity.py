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

from mindroom.background_tasks import _tasks_for_owner, create_background_task, wait_for_background_tasks
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
    await cache.replace_thread(
        ROOM_ID,
        THREAD_ID,
        [_event(THREAD_ID, 1000), _event("$initial", 1500, thread_id=THREAD_ID)],
        expected_membership_epoch=await cache.room_membership_epoch(ROOM_ID),
        fetch_started_at=0.0,
    )
    assert thread_cache_rejection_reason(await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID)) is None


async def _collect_rejections_while(
    cache: ConversationEventCache,
    mutate: Callable[[], Coroutine[Any, Any, None]],
) -> list[str]:
    """Run one mutation loop while a reader polls, returning every rejection it observed."""
    rejections: list[str] = []
    stop_reading = asyncio.Event()

    async def read_until_stopped() -> None:
        while not stop_reading.is_set():
            reason = thread_cache_rejection_reason(await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID))
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
    state = await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID)

    assert outcome is ThreadAppendOutcome.SNAPSHOT_MISSING
    assert outcome.wrote_event is False
    assert thread_cache_rejection_reason(state) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mark", "reason"),
    [
        pytest.param("thread", "sync_thread_mutation", id="thread_marker_from_a_mutation"),
        pytest.param("thread", "thread_history_opaque_encrypted_event", id="thread_marker_from_opaque_history"),
        pytest.param("room", "limited_sync_timeline", id="room_scoped_marker"),
    ],
)
async def test_an_append_lands_but_never_clears_a_gap_marker(
    cache: ConversationEventCache,
    mark: str,
    reason: str,
) -> None:
    """An incremental append extends the rows; only a full refetch may clear the marker.

    This replaces the incremental-revalidation allowlist. There is no longer a set of reasons an
    append is permitted to clear and a set it is not: an append never clears one, whatever wrote
    it and at whichever scope. The marker is what gates the read, so a thread that was gapped
    stays gapped until a fetch that covers the gap replaces the snapshot.
    """
    await _seed_valid_thread(cache)
    if mark == "thread":
        await cache.mark_thread_gap(ROOM_ID, THREAD_ID, reason=reason)
    else:
        await cache.mark_room_threads_gap(ROOM_ID, reason=reason)
    assert thread_cache_rejection_reason(await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID)) == reason

    outcome = await cache.apply_thread_mutation_append(
        ROOM_ID,
        THREAD_ID,
        _event("$live", 2000, thread_id=THREAD_ID),
        append_failed_reason="sync_append_failed",
    )
    state = await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID)

    # The event is durably appended -- refusing it would lose the mutation -- but the snapshot
    # stays unusable, so the next read refetches and picks the append up from the homeserver.
    assert outcome is ThreadAppendOutcome.APPENDED
    assert outcome.wrote_event is True
    assert thread_cache_rejection_reason(state) == reason


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
            )
        state = await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID)
    finally:
        await root_cache.close()

    assert state is not None
    assert thread_cache_rejection_reason(state) == "sync_append_failed"


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
            )
        state = await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID)
    finally:
        await root_cache.close()

    assert state is not None
    assert thread_cache_rejection_reason(state) == "sync_append_failed"


@pytest.mark.asyncio
async def test_a_cancelled_appends_marker_is_owned_by_the_shutdown_drain(tmp_path: Path) -> None:
    """The marker a rolled-back append owes must be drainable, not an untracked orphan.

    Shutdown cancels pending work in bounded rounds. A marker write that is merely shielded keeps
    running but is invisible to the drain, so a second round abandons it and the thread stays
    trusted while missing the event -- the fail-open this handler exists to prevent.
    """
    root_cache = SqliteEventCache(tmp_path / "event_cache.db")
    cache = root_cache.for_principal(PRINCIPAL_ID)
    await cache.initialize()
    cache_ops = _cache_ops(tmp_path, cache)
    coordinator = cache_ops.runtime.event_cache_write_coordinator
    assert coordinator is not None
    impact = MutationThreadImpact(state=MutationThreadImpactState.THREADED, thread_id=THREAD_ID)
    event_source = _event("$live", 2000, thread_id=THREAD_ID)

    append_started = asyncio.Event()
    marker_started = asyncio.Event()
    release_marker = asyncio.Event()
    real_mark_thread_gap = cache.mark_thread_gap

    async def hanging_append(*_args: object, **_kwargs: object) -> ThreadAppendOutcome:
        append_started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def blocked_mark_thread_gap(room_id: str, thread_id: str, *, reason: str) -> None:
        marker_started.set()
        await release_marker.wait()
        await real_mark_thread_gap(room_id, thread_id, reason=reason)

    try:
        await _seed_valid_thread(cache)
        with (
            patch.object(cache, "apply_thread_mutation_append", hanging_append),
            patch.object(cache, "mark_thread_gap", blocked_mark_thread_gap),
        ):
            mutation = asyncio.create_task(
                _apply_thread_message_mutation(
                    cache_ops=cache_ops,
                    room_id=ROOM_ID,
                    event_info=EventInfo.from_event(event_source),
                    impact=impact,
                    event_source=event_source,
                    event_id="$live",
                    context="sync",
                    room_level_skip_message="skip",
                ),
            )
            await asyncio.wait_for(append_started.wait(), timeout=5.0)
            mutation.cancel()
            await asyncio.wait_for(marker_started.wait(), timeout=5.0)

            # The marker is in flight and the mutation is already cancelled: the drain must be able
            # to see this write, or the next shutdown round drops it.
            owned = _tasks_for_owner(coordinator.failure_marker_task_owner)
            assert owned, "the marker write is untracked, so the shutdown drain cannot wait for it"

            release_marker.set()
            with pytest.raises(asyncio.CancelledError):
                await mutation
            await wait_for_background_tasks(timeout=5.0, owner=coordinator.failure_marker_task_owner)
        state = await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID)
    finally:
        await root_cache.close()

    assert thread_cache_rejection_reason(state) == "sync_append_failed"


@pytest.mark.asyncio
async def test_a_later_outbound_append_cannot_erase_an_earlier_failed_one(cache: ConversationEventCache) -> None:
    """A failed append must not leave a marker the next successful append can clear.

    Under the allowlist this depended on picking a reason outside it. It now holds unconditionally,
    because no append clears a marker -- but the guarantee still matters: without it the next
    outbound mutation would return a thread to readable while it is still missing the earlier
    event, and nothing downstream would ever notice.
    """
    await _seed_valid_thread(cache)
    missed = _event("$missed", 2000, thread_id=THREAD_ID)
    await cache.store_events_batch([("$missed", ROOM_ID, missed)])
    await cache.redact_event(ROOM_ID, "$missed")

    refused = await cache.apply_thread_mutation_append(
        ROOM_ID,
        THREAD_ID,
        missed,
        append_failed_reason="outbound_append_failed",
    )
    later = await cache.apply_thread_mutation_append(
        ROOM_ID,
        THREAD_ID,
        _event("$later", 3000, thread_id=THREAD_ID),
        append_failed_reason="outbound_append_failed",
    )
    state = await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID)
    cached_ids = {event["event_id"] for event in (await cache.get_thread_events(ROOM_ID, THREAD_ID) or [])}

    assert refused is ThreadAppendOutcome.APPEND_REFUSED
    assert later is ThreadAppendOutcome.APPENDED
    assert "$missed" not in cached_ids
    assert thread_cache_rejection_reason(state) is not None, (
        "the thread went back to trusted while still missing the refused event"
    )


@pytest.mark.asyncio
async def test_the_drain_that_cancels_an_append_still_lands_its_marker(tmp_path: Path) -> None:
    """The drain cancelling an append is the common way to owe a marker, so it must not race it.

    Sharing one owner means the append spends the whole budget and the marker, created inside its
    cancellation, is cancelled by the very next round.
    """
    root_cache = SqliteEventCache(tmp_path / "event_cache.db")
    cache = root_cache.for_principal(PRINCIPAL_ID)
    await cache.initialize()
    cache_ops = _cache_ops(tmp_path, cache)
    coordinator = cache_ops.runtime.event_cache_write_coordinator
    assert coordinator is not None
    event_source = _event("$live", 2000, thread_id=THREAD_ID)
    real_mark_thread_gap = cache.mark_thread_gap

    async def hanging_append(*_args: object, **_kwargs: object) -> ThreadAppendOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def slow_mark_thread_gap(room_id: str, thread_id: str, *, reason: str) -> None:
        # Longer than one cancel round, which is what makes a shared owner lose the write.
        await asyncio.sleep(0.3)
        await real_mark_thread_gap(room_id, thread_id, reason=reason)

    try:
        await _seed_valid_thread(cache)
        with (
            patch.object(cache, "apply_thread_mutation_append", hanging_append),
            patch.object(cache, "mark_thread_gap", slow_mark_thread_gap),
        ):
            create_background_task(
                _apply_thread_message_mutation(
                    cache_ops=cache_ops,
                    room_id=ROOM_ID,
                    event_info=EventInfo.from_event(event_source),
                    impact=MutationThreadImpact(state=MutationThreadImpactState.THREADED, thread_id=THREAD_ID),
                    event_source=event_source,
                    event_id="$live",
                    context="sync",
                    room_level_skip_message="skip",
                ),
                name="append_cancelled_by_the_drain",
                owner=coordinator.background_task_owner,
                log_exceptions=False,
            )
            await asyncio.sleep(0.05)
            await coordinator.close()
        state = await cache.get_thread_cache_gap(ROOM_ID, THREAD_ID)
    finally:
        await root_cache.close()

    assert state is not None
    assert thread_cache_rejection_reason(state) == "sync_append_failed"
