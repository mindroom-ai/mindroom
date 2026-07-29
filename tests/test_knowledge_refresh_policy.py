"""Pure knowledge refresh policy and the bounded map the knowledge caches share.

Every decision here is taken with no scheduler, no event loop, and no process-global
cooldown state: the cooldown bookkeeping is passed in as a plain mapping and the
clock as a plain float.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import AgentConfig, Config
from mindroom.knowledge import registry as knowledge_registry
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.bounded_map import BoundedMap
from mindroom.knowledge.refresh_policy import (
    RefreshCooldownKey,
    _RefreshDecision,
    decide_refresh,
    ready_index_effective_availability,
    scheduler_probe_required,
)
from mindroom.knowledge.registry import (
    PublishedIndexResolution,
    PublishedIndexState,
    published_index_metadata_path,
    resolve_published_index_key,
)
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from agno.knowledge.knowledge import Knowledge

    from mindroom.constants import RuntimePaths

_NO_COOLDOWNS: dict[RefreshCooldownKey, float] = {}


def _config(tmp_path: Path, *, git: KnowledgeGitConfig | None = None) -> Config:
    docs_path = tmp_path / "docs"
    docs_path.mkdir(exist_ok=True)
    return bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=["docs"])},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), git=git)},
        ),
        test_runtime_paths(tmp_path),
    )


def _resolution(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    availability: KnowledgeAvailability,
    schedule_refresh_on_access: bool = False,
    last_refresh_age: timedelta | None = None,
) -> PublishedIndexResolution:
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    last_refresh_at = None if last_refresh_age is None else (datetime.now(tz=UTC) - last_refresh_age).isoformat()
    state = PublishedIndexState(
        settings=key.indexing_settings,
        status="complete",
        collection="docs_collection",
        last_refresh_at=last_refresh_at,
    )
    index = knowledge_registry._PublishedIndexHandle(
        key=key,
        knowledge=cast("Knowledge", object()),
        state=state,
        metadata_path=published_index_metadata_path(key),
    )
    return PublishedIndexResolution(
        key=key,
        index=index,
        state=state,
        availability=availability,
        schedule_refresh_on_access=schedule_refresh_on_access,
    )


def _decide(
    config: Config,
    runtime_paths: RuntimePaths,
    lookup: PublishedIndexResolution | None,
    *,
    availability: KnowledgeAvailability,
    scheduler_is_refreshing: bool = False,
    now: float = 1_000.0,
    scheduled_at: dict[RefreshCooldownKey, float] | None = None,
) -> _RefreshDecision:
    return decide_refresh(
        lookup=lookup,
        availability=availability,
        config=config,
        runtime_paths=runtime_paths,
        scheduler_is_refreshing=scheduler_is_refreshing,
        now=now,
        scheduled_at=_NO_COOLDOWNS if scheduled_at is None else scheduled_at,
    )


def test_unresolved_base_decides_nothing(tmp_path: Path) -> None:
    """A base with no resolution cannot be scheduled and reports its resolved availability."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)

    assert not scheduler_probe_required(
        lookup=None,
        availability=KnowledgeAvailability.INITIALIZING,
        config=config,
    )
    decision = _decide(config, runtime_paths, None, availability=KnowledgeAvailability.INITIALIZING)
    assert decision == _RefreshDecision(availability=KnowledgeAvailability.INITIALIZING)
    assert not decision.schedule_refresh


def test_ready_index_without_on_access_refresh_skips_the_scheduler_probe(tmp_path: Path) -> None:
    """The common READY read must not pay for a scheduler probe to decide to do nothing."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=KnowledgeAvailability.READY)

    assert not scheduler_probe_required(
        lookup=lookup,
        availability=KnowledgeAvailability.READY,
        config=config,
    )
    decision = _decide(config, runtime_paths, lookup, availability=KnowledgeAvailability.READY)
    assert decision == _RefreshDecision(availability=KnowledgeAvailability.READY)


def test_ready_index_due_for_on_access_refresh_schedules_and_reports_stale(tmp_path: Path) -> None:
    """A due on-access refresh is scheduled, and the turn is told the index may be stale."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
    )

    assert scheduler_probe_required(lookup=lookup, availability=KnowledgeAvailability.READY, config=config)
    decision = _decide(config, runtime_paths, lookup, availability=KnowledgeAvailability.READY)
    assert decision.availability is KnowledgeAvailability.STALE
    assert decision.schedule_refresh
    assert decision.cooldown_key == (
        knowledge_registry.refresh_target_for_published_index_key(lookup.key),
        KnowledgeAvailability.READY,
        lookup.key.indexing_settings,
    )


def test_refresh_already_in_flight_reports_stale_without_scheduling_again(tmp_path: Path) -> None:
    """An in-flight refresh still downgrades READY to STALE but must not queue a duplicate."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
    )

    decision = _decide(
        config,
        runtime_paths,
        lookup,
        availability=KnowledgeAvailability.READY,
        scheduler_is_refreshing=True,
    )
    assert decision == _RefreshDecision(availability=KnowledgeAvailability.STALE)
    assert not decision.schedule_refresh


def test_cooldown_window_suppresses_a_second_on_access_refresh(tmp_path: Path) -> None:
    """Within the cooldown the index reads READY again; past it the refresh is rescheduled."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
    )
    first = _decide(config, runtime_paths, lookup, availability=KnowledgeAvailability.READY, now=1_000.0)
    assert first.cooldown_key is not None
    scheduled_at = {first.cooldown_key: 1_000.0}

    within = _decide(
        config,
        runtime_paths,
        lookup,
        availability=KnowledgeAvailability.READY,
        now=1_299.0,
        scheduled_at=scheduled_at,
    )
    assert within == _RefreshDecision(availability=KnowledgeAvailability.READY)

    elapsed = _decide(
        config,
        runtime_paths,
        lookup,
        availability=KnowledgeAvailability.READY,
        now=1_300.0,
        scheduled_at=scheduled_at,
    )
    assert elapsed.schedule_refresh


def test_git_poll_interval_replaces_the_default_cooldown(tmp_path: Path) -> None:
    """A Git-backed base throttles on-access refreshes at its own poll interval."""
    config = _config(tmp_path, git=KnowledgeGitConfig(repo_url="https://example.com/x.git", poll_interval_seconds=60))
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
        last_refresh_age=timedelta(seconds=600),
    )
    first = _decide(config, runtime_paths, lookup, availability=KnowledgeAvailability.READY, now=1_000.0)
    assert first.cooldown_key is not None
    scheduled_at = {first.cooldown_key: 1_000.0}

    assert not _decide(
        config,
        runtime_paths,
        lookup,
        availability=KnowledgeAvailability.READY,
        now=1_059.0,
        scheduled_at=scheduled_at,
    ).schedule_refresh
    assert _decide(
        config,
        runtime_paths,
        lookup,
        availability=KnowledgeAvailability.READY,
        now=1_060.0,
        scheduled_at=scheduled_at,
    ).schedule_refresh


def test_freshly_polled_git_index_is_not_due_for_a_refresh(tmp_path: Path) -> None:
    """An index refreshed inside its poll interval stays READY and needs no probe."""
    config = _config(tmp_path, git=KnowledgeGitConfig(repo_url="https://example.com/x.git", poll_interval_seconds=600))
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
        last_refresh_age=timedelta(seconds=30),
    )

    assert ready_index_effective_availability(lookup, config) is KnowledgeAvailability.READY
    assert not scheduler_probe_required(lookup=lookup, availability=KnowledgeAvailability.READY, config=config)


def test_git_index_past_its_poll_interval_reads_stale(tmp_path: Path) -> None:
    """A Git index older than its poll interval is reported stale before any refresh runs."""
    config = _config(tmp_path, git=KnowledgeGitConfig(repo_url="https://example.com/x.git", poll_interval_seconds=60))
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        last_refresh_age=timedelta(seconds=600),
    )

    assert ready_index_effective_availability(lookup, config) is KnowledgeAvailability.STALE


@pytest.mark.parametrize(
    "availability",
    [
        KnowledgeAvailability.INITIALIZING,
        KnowledgeAvailability.STALE,
        KnowledgeAvailability.CONFIG_MISMATCH,
        KnowledgeAvailability.REFRESH_FAILED,
    ],
)
def test_unusable_index_schedules_a_refresh_without_changing_availability(
    tmp_path: Path,
    availability: KnowledgeAvailability,
) -> None:
    """Every non-READY availability schedules a refresh and is reported to the turn unchanged."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=availability)

    assert scheduler_probe_required(lookup=lookup, availability=availability, config=config)
    decision = _decide(config, runtime_paths, lookup, availability=availability)
    assert decision.availability is availability
    assert decision.schedule_refresh

    in_flight = _decide(config, runtime_paths, lookup, availability=availability, scheduler_is_refreshing=True)
    assert in_flight == _RefreshDecision(availability=availability)


def test_changed_git_credentials_retry_a_failed_refresh_before_the_cooldown(tmp_path: Path) -> None:
    """Rotating the credential that broke a refresh must not wait out the retry cooldown."""
    git = KnowledgeGitConfig(repo_url="https://user:old-secret@example.com/x.git")
    config = _config(tmp_path, git=git)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=KnowledgeAvailability.REFRESH_FAILED)

    failed = _decide(config, runtime_paths, lookup, availability=KnowledgeAvailability.REFRESH_FAILED, now=1_000.0)
    assert failed.cooldown_key is not None
    scheduled_at = {failed.cooldown_key: 1_000.0}

    assert not _decide(
        config,
        runtime_paths,
        lookup,
        availability=KnowledgeAvailability.REFRESH_FAILED,
        now=1_100.0,
        scheduled_at=scheduled_at,
    ).schedule_refresh

    rotated_config = _config(
        tmp_path,
        git=git.model_copy(update={"repo_url": "https://user:new-secret@example.com/x.git"}),
    )
    rotated_lookup = _resolution(rotated_config, runtime_paths, availability=KnowledgeAvailability.REFRESH_FAILED)
    rotated = _decide(
        rotated_config,
        runtime_paths,
        rotated_lookup,
        availability=KnowledgeAvailability.REFRESH_FAILED,
        now=1_100.0,
        scheduled_at=scheduled_at,
    )
    assert rotated.schedule_refresh
    assert "new-secret" not in repr(rotated.cooldown_key)
    assert "old-secret" not in repr(rotated.cooldown_key)


def test_bounded_map_evicts_its_oldest_entries_on_insert() -> None:
    """Inserting past the capacity drops the entries inserted longest ago."""
    entries: BoundedMap[str, int] = BoundedMap(capacity=3)
    for index in range(5):
        entries[f"key{index}"] = index

    assert list(entries) == ["key2", "key3", "key4"]


def test_bounded_map_bounds_only_tracked_entries() -> None:
    """Untracked entries neither consume capacity nor get evicted."""
    entries: BoundedMap[str, int] = BoundedMap(capacity=2, tracked=lambda key, _value: key.startswith("private:"))
    entries["shared:a"] = 0
    entries["shared:b"] = 1
    for index in range(4):
        entries[f"private:{index}"] = index

    assert list(entries) == ["shared:a", "shared:b", "private:2", "private:3"]


def test_bounded_map_never_evicts_a_pinned_entry() -> None:
    """A pinned entry survives eviction as the oldest entry, and still consumes capacity."""
    entries: BoundedMap[str, int] = BoundedMap(capacity=2, pinned=lambda _key, value: value < 0)
    entries["held"] = -1
    for index in range(4):
        entries[f"idle{index}"] = index

    assert list(entries) == ["held", "idle3"]


def test_bounded_map_evicts_by_eviction_order_not_insertion_order() -> None:
    """A timestamped cache evicts its stalest entries, not the ones inserted first."""
    entries: BoundedMap[str, float] = BoundedMap(
        capacity=2,
        eviction_order=lambda _key, scheduled_at: scheduled_at,
    )
    entries["first"] = 30.0
    entries["second"] = 10.0
    entries["third"] = 20.0

    assert sorted(entries) == ["first", "third"]


def test_bounded_map_prune_reclaims_entries_once_they_unpin() -> None:
    """Releasing a pinned entry lets the next prune reclaim the space it held."""
    released: set[str] = set()
    entries: BoundedMap[str, int] = BoundedMap(capacity=1, pinned=lambda key, _value: key not in released)
    entries["a"] = 0
    entries["b"] = 1
    assert len(entries) == 2

    released.add("a")
    entries.prune()
    assert list(entries) == ["b"]


def test_refresh_lock_entries_stay_stable_while_a_borrower_holds_them() -> None:
    """The shared cap must never hand two callers different locks for one source root."""
    from mindroom.knowledge import refresh_runner  # noqa: PLC0415

    async def _exercise() -> None:
        held = refresh_runner.KnowledgeSourceRoot(storage_root="/root", knowledge_path="/root/docs")
        entry = refresh_runner._borrow_refresh_lock_for_key(held)
        await entry.lock.acquire()
        try:
            for index in range(refresh_runner._refresh_locks.capacity + 5):
                idle = refresh_runner.KnowledgeSourceRoot(
                    storage_root=f"/other{index}",
                    knowledge_path=f"/other{index}/docs",
                )
                refresh_runner._release_refresh_lock_for_key(idle, refresh_runner._borrow_refresh_lock_for_key(idle))
            assert refresh_runner._refresh_locks.get(held) is entry
        finally:
            entry.lock.release()
            refresh_runner._release_refresh_lock_for_key(held, entry)

    refresh_runner._refresh_locks.clear()
    try:
        asyncio.run(_exercise())
    finally:
        refresh_runner._refresh_locks.clear()
