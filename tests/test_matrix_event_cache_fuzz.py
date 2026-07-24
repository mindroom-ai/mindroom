"""Property and replay tests for the Matrix event-cache fuzz framework."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from scripts.testing.fuzz_matrix_event_cache import (
    FuzzOperation,
    FuzzScenario,
    OperationKind,
    concurrent_fanout_scenario,
    run_scenario,
    scenario_from_seed,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.matrix.cache import ConversationEventCache


_OPERATION_KINDS = tuple(OperationKind)


def _operation_strategy() -> st.SearchStrategy[FuzzOperation]:
    return st.builds(
        FuzzOperation,
        kind=st.sampled_from(_OPERATION_KINDS),
        room=st.integers(min_value=0, max_value=1),
        thread=st.integers(min_value=0, max_value=2),
        slot=st.integers(min_value=0, max_value=7),
        target=st.integers(min_value=0, max_value=7),
        variant=st.integers(min_value=0, max_value=7),
    )


def _scenario_strategy() -> st.SearchStrategy[FuzzScenario]:
    batch = st.lists(
        _operation_strategy(),
        min_size=1,
        max_size=4,
    ).map(tuple)
    return st.lists(
        batch,
        min_size=1,
        max_size=4,
    ).map(lambda batches: FuzzScenario(batches=tuple(batches)))


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
                room_count=2,
                thread_count=3,
                verify_restart=False,
            ),
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
        room_count=2,
        thread_count=4,
    )


@pytest.mark.asyncio
async def test_forty_five_thread_fanout_matches_every_cache_backend(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """The original 45-way load shape survives concurrent edits, replies, and reactions."""
    await run_scenario(
        event_cache_factory,
        concurrent_fanout_scenario(),
        room_count=1,
        thread_count=45,
        verify_restart=False,
    )


def test_fuzz_trace_json_round_trip_is_exact() -> None:
    """Failure traces remain portable across local runs and CI."""
    scenario = scenario_from_seed(
        42,
        steps=25,
        room_count=2,
        thread_count=3,
        max_batch_size=5,
    )

    assert FuzzScenario.from_json(scenario.to_json()) == scenario
    assert (
        scenario_from_seed(
            42,
            steps=25,
            room_count=2,
            thread_count=3,
            max_batch_size=5,
        )
        == scenario
    )
