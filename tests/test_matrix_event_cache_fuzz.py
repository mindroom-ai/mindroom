"""Property and replay tests for the Matrix event-cache fuzz framework."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from scripts.testing.fuzz_matrix_event_cache import (
    FUZZ_PRINCIPAL,
    CacheFuzzRunner,
    FuzzOperation,
    FuzzScenario,
    OperationKind,
    ReferenceCacheModel,
    _reduce_thread_invalidation_reason,
    ciphertext_source,
    concurrent_fanout_scenario,
    edit_source,
    message_id,
    model_based_scenario,
    reply_id,
    room_id,
    run_scenario,
    scenario_from_seed,
    thread_id,
    threaded_message_source,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.matrix.cache import ConversationEventCache


_LIFECYCLE_KINDS = (OperationKind.REOPEN_CACHE, OperationKind.REJOIN_ROOM)
_EVENT_KINDS = tuple(kind for kind in OperationKind if kind not in _LIFECYCLE_KINDS)


def _operation_strategy(
    kinds: tuple[OperationKind, ...] = _EVENT_KINDS,
) -> st.SearchStrategy[FuzzOperation]:
    return st.builds(
        FuzzOperation,
        kind=st.sampled_from(kinds),
        room=st.integers(min_value=0, max_value=1),
        thread=st.integers(min_value=0, max_value=2),
        slot=st.integers(min_value=0, max_value=7),
        target=st.integers(min_value=0, max_value=7),
        variant=st.integers(min_value=0, max_value=15),
    )


def _scenario_strategy() -> st.SearchStrategy[FuzzScenario]:
    concurrent_batch = st.lists(
        _operation_strategy(),
        min_size=1,
        max_size=4,
    ).map(tuple)
    lifecycle_batch = _operation_strategy(_LIFECYCLE_KINDS).map(lambda operation: (operation,))
    return st.lists(
        st.one_of(concurrent_batch, lifecycle_batch),
        min_size=1,
        max_size=4,
    ).map(
        lambda batches: FuzzScenario(
            batches=tuple(batches),
            room_count=2,
            thread_count=3,
        ),
    )


def _semantic_scenario_strategy() -> st.SearchStrategy[FuzzScenario]:
    semantic_operation = st.builds(
        FuzzOperation,
        kind=st.sampled_from(_EVENT_KINDS),
        room=st.just(0),
        thread=st.integers(min_value=0, max_value=1),
        slot=st.integers(min_value=0, max_value=7),
        target=st.integers(min_value=0, max_value=7),
        variant=st.integers(min_value=0, max_value=15),
    )
    return st.lists(
        semantic_operation.map(lambda operation: (operation,)),
        min_size=1,
        max_size=5,
    ).map(
        lambda batches: FuzzScenario(
            batches=tuple(batches),
            room_count=1,
            thread_count=2,
            verify_reference_model=True,
        ),
    )


_CONCURRENT_REDACTION_REPLAY = FuzzScenario(
    batches=(
        (
            FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 0, 0, 0),
            FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 1, 0, 0, 0),
        ),
        (
            FuzzOperation(OperationKind.EDIT, 0, 0, 1, 0, 0),
            FuzzOperation(OperationKind.REACTION, 0, 0, 1, 0, 0),
            FuzzOperation(OperationKind.PLAIN_REPLY, 0, 0, 1, 0, 0),
        ),
        (
            FuzzOperation(OperationKind.REDACTION, 0, 0, 0, 0, 1),
            FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 0, 0, 0),
            FuzzOperation(OperationKind.CIPHERTEXT_REPLAY, 0, 0, 0, 0, 1),
            FuzzOperation(OperationKind.REPLACE_THREAD, 0, 0, 0, 0, 4),
        ),
    ),
    room_count=2,
    thread_count=3,
)


@example(_CONCURRENT_REDACTION_REPLAY)
@settings(
    deadline=None,
    derandomize=True,
    max_examples=8,
    print_blob=True,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(scenario=_scenario_strategy())
def test_hypothesis_matrix_cache_traces_preserve_sqlite_invariants(
    scenario: FuzzScenario,
) -> None:
    """Shrunk concurrent mutation traces preserve public cache invariants."""
    with tempfile.TemporaryDirectory(prefix="mindroom-hypothesis-cache-") as temp_dir:
        db_path = Path(temp_dir) / "event_cache.db"
        asyncio.run(
            run_scenario(
                lambda: SqliteEventCache(db_path),
                scenario,
                verify_restart=False,
            ),
        )


@settings(
    deadline=None,
    derandomize=True,
    max_examples=6,
    print_blob=True,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(scenario=_semantic_scenario_strategy())
def test_hypothesis_sequential_traces_match_reference_model(
    scenario: FuzzScenario,
) -> None:
    """Generated sequential operations must match the independent semantic model."""
    with tempfile.TemporaryDirectory(prefix="mindroom-hypothesis-model-") as temp_dir:
        db_path = Path(temp_dir) / "event_cache.db"
        asyncio.run(
            run_scenario(
                lambda: SqliteEventCache(db_path),
                scenario,
                verify_restart=False,
            ),
        )


@pytest.mark.asyncio
async def test_every_operation_kind_has_deterministic_reference_coverage(tmp_path: Path) -> None:
    """Every state-machine transition must run against the independent model."""
    scenario = FuzzScenario(
        batches=tuple(
            (
                FuzzOperation(
                    kind=kind,
                    room=0,
                    thread=index % 2,
                    slot=index,
                    target=index,
                    variant=index,
                ),
            )
            for index, kind in enumerate(OperationKind)
        ),
        room_count=1,
        thread_count=2,
        verify_reference_model=True,
    )

    await run_scenario(
        lambda: SqliteEventCache(tmp_path / "transition-coverage.db"),
        scenario,
        verify_restart=False,
    )


@pytest.mark.asyncio
async def test_reference_model_rejects_tombstoned_replay_and_cleans_invalidated_mapping(tmp_path: Path) -> None:
    """Rejected replay and full invalidation cannot leave model-only indexes."""
    message = FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 0, 0, 0)
    scenario = FuzzScenario(
        batches=(
            (message,),
            (FuzzOperation(OperationKind.REDACTION, 0, 0, 1, 0, 1),),
            (message,),
            (FuzzOperation(OperationKind.INVALIDATE_THREAD, 0, 1, 0, 0, 0),),
        ),
        room_count=1,
        thread_count=2,
        verify_reference_model=True,
    )

    await run_scenario(
        lambda: SqliteEventCache(tmp_path / "rejected-replay.db"),
        scenario,
        verify_restart=False,
    )


@pytest.mark.asyncio
async def test_reference_model_rejects_tombstoned_snapshot_replacement(tmp_path: Path) -> None:
    """A later authoritative snapshot cannot resurrect a tombstoned event."""
    scenario = FuzzScenario(
        batches=(
            (FuzzOperation(OperationKind.REDACTION, 0, 0, 0, 0, 0),),
            (FuzzOperation(OperationKind.REPLACE_THREAD, 0, 0, 0, 0, 1),),
        ),
        room_count=1,
        thread_count=1,
        verify_reference_model=True,
    )

    await run_scenario(
        lambda: SqliteEventCache(tmp_path / "tombstoned-replacement.db"),
        scenario,
        verify_restart=False,
    )


@pytest.mark.asyncio
async def test_reference_model_removes_point_only_opaque_root_mapping(tmp_path: Path) -> None:
    """Redacting an opaque point event must remove its model thread index."""
    scenario = FuzzScenario(
        batches=(
            (FuzzOperation(OperationKind.CIPHERTEXT_REPLAY, 0, 0, 3, 0, 0),),
            (FuzzOperation(OperationKind.REDACTION, 0, 0, 0, 3, 1),),
        ),
        room_count=1,
        thread_count=1,
        verify_reference_model=True,
    )

    await run_scenario(
        lambda: SqliteEventCache(tmp_path / "opaque-redaction.db"),
        scenario,
        verify_restart=False,
    )


@pytest.mark.asyncio
async def test_reference_model_preserves_tombstoned_reaction_and_ciphertext_semantics(tmp_path: Path) -> None:
    """Late reaction replay is inert while late ciphertext keeps opaque staleness."""
    scenario = FuzzScenario(
        batches=(
            (FuzzOperation(OperationKind.REACTION, 0, 0, 4, 0, 0),),
            (FuzzOperation(OperationKind.REDACTION, 0, 0, 4, 0, 3),),
            (FuzzOperation(OperationKind.REACTION, 0, 0, 4, 0, 0),),
            (FuzzOperation(OperationKind.REDACTION, 0, 0, 0, 3, 1),),
            (FuzzOperation(OperationKind.CIPHERTEXT_REPLAY, 0, 0, 3, 0, 0),),
        ),
        room_count=1,
        thread_count=1,
        verify_reference_model=True,
    )

    await run_scenario(
        lambda: SqliteEventCache(tmp_path / "tombstoned-replay-types.db"),
        scenario,
        verify_restart=False,
    )


@pytest.mark.asyncio
async def test_runner_initializes_and_certifies_the_bound_principal_generation(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """The selected fuzz principal must be initialized before generation checks."""
    root = event_cache_factory()
    await root.initialize()
    runner = CacheFuzzRunner(
        root,
        event_cache_factory,
        FuzzScenario(batches=()),
        room_count=1,
        thread_count=1,
    )

    try:
        await runner.run()
        assert runner.cache.is_initialized
        assert runner.cache_generation == root.for_principal(FUZZ_PRINCIPAL).cache_generation
        assert runner.cache_generation != root.cache_generation
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_concurrent_batch_uses_independent_cache_connections(tmp_path: Path) -> None:
    """One concurrent batch must cross separate backend runtimes."""
    factory_calls = 0

    def cache_factory() -> SqliteEventCache:
        nonlocal factory_calls
        factory_calls += 1
        return SqliteEventCache(tmp_path / "concurrent-connections.db")

    state = await run_scenario(
        cache_factory,
        FuzzScenario(
            batches=(
                (
                    FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 1, 0, 0),
                    FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 1, 1, 0, 0),
                ),
            ),
            room_count=1,
            thread_count=2,
        ),
        verify_restart=False,
    )

    assert factory_calls == 3
    observed_events = {(current_room_id, event_id): payload for current_room_id, event_id, payload in state.events}
    assert observed_events[(room_id(0), message_id(0, 0, 1))] is not None
    assert observed_events[(room_id(0), message_id(0, 1, 1))] is not None


@pytest.mark.asyncio
async def test_concurrent_batch_rejects_point_only_thread_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent success requires thread indexes, not merely surviving point rows."""

    async def store_point_only(runner: CacheFuzzRunner, operation: FuzzOperation) -> None:
        source = threaded_message_source(operation)
        runner._remember_source(source)
        await runner.cache.store_event(
            source["event_id"],
            source["room_id"],
            source,
        )

    monkeypatch.setattr(CacheFuzzRunner, "_apply_source_operation", store_point_only)
    scenario = FuzzScenario(
        batches=(
            (
                FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 1, 0, 0),
                FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 1, 1, 0, 0),
            ),
        ),
        room_count=1,
        thread_count=2,
    )

    with pytest.raises(AssertionError, match="concurrent thread member disappeared"):
        await run_scenario(
            lambda: SqliteEventCache(tmp_path / "point-only-concurrent.db"),
            scenario,
            verify_restart=False,
        )


@pytest.mark.asyncio
async def test_concurrent_runtime_initialization_does_not_deadlock(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """New backend runtimes must initialize without inverting operation locks."""
    await run_scenario(
        event_cache_factory,
        FuzzScenario(
            batches=(
                (
                    FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 1, 0, 0),
                    FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 1, 1, 0, 0),
                ),
            ),
            room_count=1,
            thread_count=2,
        ),
        verify_restart=False,
        max_batch_seconds=10,
    )


@pytest.mark.asyncio
async def test_batch_timeout_emits_replayable_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stalled operation must fail within the bound and preserve its trace."""

    async def stall(_runner: CacheFuzzRunner, _operation: FuzzOperation) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(CacheFuzzRunner, "_apply_operation", stall)
    scenario = FuzzScenario(
        batches=((FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 0, 0, 0),),),
        room_count=1,
        thread_count=1,
    )

    with pytest.raises(AssertionError, match="Matrix cache fuzz trace"):
        await run_scenario(
            lambda: SqliteEventCache(tmp_path / "timeout.db"),
            scenario,
            verify_restart=False,
            max_batch_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_seeded_concurrent_trace_matches_every_cache_backend(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """One stable chaos trace guards SQLite and Postgres contract parity."""
    await run_scenario(
        event_cache_factory,
        scenario_from_seed(
            1637,
            steps=48,
            room_count=2,
            thread_count=4,
            max_batch_size=6,
        ),
    )


@pytest.mark.asyncio
async def test_model_trace_has_exact_semantics_and_backend_parity(
    tmp_path: Path,
    postgres_event_cache_url: str,
) -> None:
    """The explicit state model agrees across both durable implementations."""
    sqlite_path = tmp_path / "model-event-cache.db"
    postgres_namespace = f"test_model_fuzz_{uuid.uuid4().hex}"
    scenario = model_based_scenario()

    sqlite_state = await run_scenario(
        lambda: SqliteEventCache(sqlite_path),
        scenario,
        max_batch_seconds=5,
    )
    postgres_state = await run_scenario(
        lambda: PostgresEventCache(
            database_url=postgres_event_cache_url,
            namespace=postgres_namespace,
        ),
        scenario,
        max_batch_seconds=5,
    )

    assert sqlite_state.backend_parity_projection() == postgres_state.backend_parity_projection()
    thread_events = {
        (current_room_id, current_thread_id): event_ids
        for current_room_id, current_thread_id, event_ids in sqlite_state.threads
    }
    hot_thread = thread_events[(room_id(0), thread_id(0, 0))]
    assert hot_thread.index(message_id(0, 0, 6, 5, 8)) < hot_thread.index(message_id(0, 0, 5, 5, 8))
    assert reply_id(0, 0, 7, 6, 0) in hot_thread
    assert thread_events[(room_id(1), thread_id(1, 0))] == (thread_id(1, 0),)
    assert message_id(1, 2, 3) in thread_events[(room_id(1), thread_id(1, 2))]


def test_generated_event_ids_do_not_alias_different_matrix_events() -> None:
    """All operation inputs produce immutable IDs except cleartext upgrades."""
    operations = tuple(
        FuzzOperation(
            kind=kind,
            room=0,
            thread=1,
            slot=3,
            target=target,
            variant=variant,
        )
        for kind in (
            OperationKind.THREADED_MESSAGE,
            OperationKind.PLAIN_REPLY,
            OperationKind.EDIT,
            OperationKind.REACTION,
            OperationKind.REFERENCE,
            OperationKind.REDACTION,
        )
        for target in range(2)
        for variant in range(16)
    )

    FuzzScenario(batches=tuple((operation,) for operation in operations)).validate()

    operation = FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 1, 3, 1, 9)
    clear = threaded_message_source(operation)
    opaque = ciphertext_source(operation)
    assert clear["event_id"] == opaque["event_id"]
    assert clear["origin_server_ts"] == opaque["origin_server_ts"]
    assert clear["sender"] == opaque["sender"]
    assert clear["content"]["m.relates_to"] == opaque["content"]["m.relates_to"]


@pytest.mark.parametrize("target_kind", range(4))
def test_edit_variants_cover_wrong_sender_for_every_target_kind(target_kind: int) -> None:
    """Wrong-sender edits cover messages, roots, replies, and prior edits."""
    valid = edit_source(
        FuzzOperation(OperationKind.EDIT, 0, 1, 9, 6, target_kind),
    )
    wrong_sender = edit_source(
        FuzzOperation(OperationKind.EDIT, 0, 1, 9, 6, target_kind + 4),
    )

    assert valid["content"]["m.relates_to"]["event_id"] == wrong_sender["content"]["m.relates_to"]["event_id"]
    assert valid["sender"] != wrong_sender["sender"]


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_forty_five_thread_fanout_matches_every_cache_backend(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """The original 45-way load shape survives concurrent edits, replies, and reactions."""
    await run_scenario(
        event_cache_factory,
        concurrent_fanout_scenario(),
        verify_restart=False,
    )


def test_fuzz_workload_json_round_trip_preserves_semantic_inputs() -> None:
    """Workload operations and dimensions remain portable across runs."""
    scenario = scenario_from_seed(
        42,
        steps=25,
        room_count=2,
        thread_count=3,
        max_batch_size=5,
        verify_reference_model=True,
    )

    assert FuzzScenario.from_json(scenario.to_json()) == scenario
    assert (
        scenario_from_seed(
            42,
            steps=25,
            room_count=2,
            thread_count=3,
            max_batch_size=5,
            verify_reference_model=True,
        )
        == scenario
    )


def test_reference_model_rejects_concurrent_batches() -> None:
    """Reference-model mode cannot invent an unrecorded concurrent ordering."""
    scenario = FuzzScenario(
        batches=(
            (
                FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 0, 0, 0),
                FuzzOperation(OperationKind.INVALIDATE_THREAD, 0, 0, 0, 0, 0),
            ),
        ),
        room_count=1,
        thread_count=1,
        verify_reference_model=True,
    )

    with pytest.raises(ValueError, match="singleton batches"):
        scenario.validate()


@pytest.mark.asyncio
async def test_reference_model_detects_silently_dropped_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantic mode must reject a cache that silently drops a requested write."""
    scenario = FuzzScenario(
        batches=((FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 0, 0, 0),),),
        room_count=1,
        thread_count=1,
        verify_reference_model=True,
    )
    original_apply = CacheFuzzRunner._apply_operation

    async def drop_threaded_message(
        runner: CacheFuzzRunner,
        operation: FuzzOperation,
    ) -> None:
        if operation.kind is OperationKind.THREADED_MESSAGE:
            return
        await original_apply(runner, operation)

    monkeypatch.setattr(CacheFuzzRunner, "_apply_operation", drop_threaded_message)

    with pytest.raises(AssertionError, match="reference point payload mismatch"):
        await run_scenario(
            lambda: SqliteEventCache(tmp_path / "dropped-write.db"),
            scenario,
            verify_restart=False,
        )


@pytest.mark.parametrize(
    ("current", "incoming", "expected"),
    [
        (None, "sync_thread_mutation", "sync_thread_mutation"),
        ("sync_thread_mutation", "sync_opaque_encrypted_event", "sync_opaque_encrypted_event"),
        ("sync_opaque_encrypted_event", "sync_thread_mutation", "sync_opaque_encrypted_event"),
        ("sync_opaque_encrypted_event", "sync_redaction", "sync_redaction"),
    ],
)
def test_reference_thread_reason_reducer_keeps_nonincremental_staleness_sticky(
    current: str | None,
    incoming: str,
    expected: str,
) -> None:
    """The independent model matches durable stale-reason precedence."""
    assert _reduce_thread_invalidation_reason(current, incoming) == expected


def test_reference_model_keeps_opaque_staleness_after_cleartext_upgrade() -> None:
    """A later clear event cannot make an opaque-poisoned snapshot reusable."""
    operation = FuzzOperation(OperationKind.CIPHERTEXT_REPLAY, 0, 0, 3, 0, 0)
    model = ReferenceCacheModel.empty()
    model.seed_room(0, 1)

    model.apply_operation(operation, thread_count=1)
    model.apply_operation(
        FuzzOperation(OperationKind.THREADED_MESSAGE, 0, 0, 3, 0, 0),
        thread_count=1,
    )

    assert model.thread_reasons[(room_id(0), thread_id(0, 0))] == "sync_opaque_encrypted_event"
