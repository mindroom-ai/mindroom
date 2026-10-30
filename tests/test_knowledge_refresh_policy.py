"""Knowledge refresh-policy decisions, and the scheduler's cooldown that throttles them.

Both clocks are injected, so the Git poll-interval boundary and the cooldown
window are pinned to exact instants rather than approximated against wall time.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

import mindroom.knowledge.refresh_runner as refresh_runner_module
import mindroom.knowledge.refresh_scheduler as refresh_scheduler_module
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import AgentConfig, Config
from mindroom.knowledge import utils as knowledge_utils
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.index_metadata import Publication, RefreshStatus
from mindroom.knowledge.refresh_policy import (
    ready_index_effective_availability,
    refresh_cooldown_key,
    refresh_trigger,
)
from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
from mindroom.knowledge.registry import (
    PublishedIndexResolution,
    index_availability,
    refresh_target_for_published_index_key,
    resolve_published_index_key,
)
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.index_reader import PublishedIndexReader
    from mindroom.knowledge.refresh_runner import KnowledgeRefreshResult

_WALL_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
#: How long ago a test publication was published, unless a test says otherwise: long enough for any poll to be due.
_PUBLISHED_AGE = timedelta(days=365)


def _config(
    tmp_path: Path,
    *,
    git: KnowledgeGitConfig | None = None,
    poll_interval_seconds: int | None = None,
) -> Config:
    docs_path = tmp_path / "docs"
    docs_path.mkdir(exist_ok=True)
    return bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=["docs"])},
            knowledge_bases={
                "docs": KnowledgeBaseConfig(path=str(docs_path), git=git, poll_interval_seconds=poll_interval_seconds),
            },
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
    published_age: timedelta = _PUBLISHED_AGE,
    last_error: str | None = None,
) -> PublishedIndexResolution:
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    publication = Publication(
        settings=key.indexing_settings,
        collection="docs_collection",
        indexed_count=1,
        source_signature="docs-signature",
        published_revision=None,
        last_published_at=(_WALL_NOW - published_age).isoformat(),
    )
    status = RefreshStatus(
        last_error=last_error,
        last_refresh_at=None if last_refresh_age is None else (_WALL_NOW - last_refresh_age).isoformat(),
    )
    return PublishedIndexResolution(
        key=key,
        reader=cast("PublishedIndexReader", object()),
        publication=publication,
        status=status,
        availability=availability,
        schedule_refresh_on_access=schedule_refresh_on_access,
    )


@pytest.fixture
def scheduler_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Drive the scheduler's cooldown clock by hand; only the scheduler module sees it, never the event loop."""
    clock = [0.0]
    monkeypatch.setattr(refresh_scheduler_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return clock


@pytest.fixture
def idle_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every refresh child with one that runs until the scheduler shuts down."""

    async def _refresh(_base_id: str, **_kwargs: object) -> KnowledgeRefreshResult:
        await asyncio.Event().wait()
        raise AssertionError

    monkeypatch.setattr(refresh_runner_module, "refresh_knowledge_binding_in_subprocess", _refresh)


def _schedule_after_cooldown(
    scheduler: KnowledgeRefreshScheduler,
    lookup: PublishedIndexResolution,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Schedule the lookup's refresh as a turn does, under the default 300-second cooldown."""
    return scheduler.schedule_refresh_after_cooldown(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=None,
        cooldown_key=refresh_cooldown_key(lookup, config, runtime_paths, lookup.availability),
        cooldown_seconds=300.0,
    )


def test_ready_index_without_on_access_refresh_wants_nothing(tmp_path: Path) -> None:
    """A READY index not due for an on-access refresh wants nothing, before the caller builds its cooldown key."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=KnowledgeAvailability.READY)

    assert (
        refresh_trigger(
            lookup=lookup,
            availability=KnowledgeAvailability.READY,
            config=config,
            wall_now=_WALL_NOW,
        )
        is None
    )


def test_ready_index_due_for_on_access_refresh_reports_stale_while_refreshing(tmp_path: Path) -> None:
    """A due on-access refresh downgrades the turn's view of a READY index to STALE."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
    )

    trigger = refresh_trigger(
        lookup=lookup,
        availability=KnowledgeAvailability.READY,
        config=config,
        wall_now=_WALL_NOW,
    )
    assert trigger is not None
    assert trigger.availability_while_refreshing is KnowledgeAvailability.STALE
    assert trigger.cooldown_seconds == 300.0


@pytest.mark.parametrize(
    "availability",
    [
        KnowledgeAvailability.INITIALIZING,
        KnowledgeAvailability.STALE,
        KnowledgeAvailability.CONFIG_MISMATCH,
        KnowledgeAvailability.REFRESH_FAILED,
    ],
)
def test_unusable_index_wants_a_refresh_without_changing_availability(
    tmp_path: Path,
    availability: KnowledgeAvailability,
) -> None:
    """Every non-READY availability warrants a refresh and is reported to the turn unchanged."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=availability)

    trigger = refresh_trigger(lookup=lookup, availability=availability, config=config, wall_now=_WALL_NOW)
    assert trigger is not None
    assert trigger.availability_while_refreshing is availability
    assert trigger.cooldown_seconds == 300.0


@pytest.mark.parametrize(
    ("age_seconds", "expected_due"),
    [(59, False), (60, True), (61, True)],
)
def test_git_poll_interval_boundary_is_exact(tmp_path: Path, age_seconds: int, expected_due: bool) -> None:
    """The poll interval fires exactly at its boundary, not a second early or late."""
    config = _config(tmp_path, git=KnowledgeGitConfig(repo_url="https://example.com/x.git", poll_interval_seconds=60))
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
        last_refresh_age=timedelta(seconds=age_seconds),
    )

    trigger = refresh_trigger(
        lookup=lookup,
        availability=KnowledgeAvailability.READY,
        config=config,
        wall_now=_WALL_NOW,
    )
    assert (trigger is not None) is expected_due
    effective = ready_index_effective_availability(lookup, config, wall_now=_WALL_NOW)
    assert (effective is KnowledgeAvailability.STALE) is expected_due


@pytest.mark.parametrize(
    ("age_seconds", "expected_due"),
    [(59, False), (60, True), (61, True)],
)
def test_local_poll_interval_boundary_is_exact(tmp_path: Path, age_seconds: int, expected_due: bool) -> None:
    """A local base with a poll interval turns STALE exactly at its boundary, like a Git base."""
    config = _config(tmp_path, poll_interval_seconds=60)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
        last_refresh_age=timedelta(seconds=age_seconds),
    )

    effective = ready_index_effective_availability(lookup, config, wall_now=_WALL_NOW)
    assert (effective is KnowledgeAvailability.STALE) is expected_due


@pytest.mark.parametrize(
    ("age_seconds", "expected_due"),
    [(59, False), (60, True)],
)
def test_poll_age_falls_back_to_the_publication_time(tmp_path: Path, age_seconds: int, expected_due: bool) -> None:
    """A base no refresh has finished for since it published ages from its publication instead."""
    config = _config(tmp_path, poll_interval_seconds=60)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
        published_age=timedelta(seconds=age_seconds),
    )

    effective = ready_index_effective_availability(lookup, config, wall_now=_WALL_NOW)
    assert (effective is KnowledgeAvailability.STALE) is expected_due


def test_local_base_without_poll_interval_stays_ready(tmp_path: Path) -> None:
    """A local base that sets no poll interval never turns STALE from age alone."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(
        config,
        runtime_paths,
        availability=KnowledgeAvailability.READY,
        schedule_refresh_on_access=True,
        last_refresh_age=timedelta(days=30),
    )

    assert ready_index_effective_availability(lookup, config, wall_now=_WALL_NOW) is KnowledgeAvailability.READY


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

    trigger = refresh_trigger(
        lookup=lookup,
        availability=KnowledgeAvailability.READY,
        config=config,
        wall_now=_WALL_NOW,
    )
    assert trigger is not None
    assert trigger.cooldown_seconds == 60.0


@pytest.mark.asyncio
@pytest.mark.usefixtures("idle_refreshes")
async def test_cooldown_window_boundary_is_exact(tmp_path: Path, scheduler_clock: list[float]) -> None:
    """A cooldown suppresses rescheduling right up to its boundary, then allows it."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=KnowledgeAvailability.STALE)
    scheduler = KnowledgeRefreshScheduler()
    try:
        scheduler_clock[0] = 1_000.0
        assert _schedule_after_cooldown(scheduler, lookup, config, runtime_paths)
        assert scheduler.is_refreshing(refresh_target_for_published_index_key(lookup.key))

        scheduler_clock[0] = 1_299.0
        assert not _schedule_after_cooldown(scheduler, lookup, config, runtime_paths)
        scheduler_clock[0] = 1_300.0
        assert _schedule_after_cooldown(scheduler, lookup, config, runtime_paths)
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.usefixtures("idle_refreshes")
async def test_changed_git_credentials_retry_a_failed_refresh_before_the_cooldown(
    tmp_path: Path,
    scheduler_clock: list[float],
) -> None:
    """Rotating the credential that broke a refresh must not wait out the retry cooldown."""
    git = KnowledgeGitConfig(repo_url="https://user:old-secret@example.com/x.git")
    config = _config(tmp_path, git=git)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=KnowledgeAvailability.REFRESH_FAILED)
    scheduler = KnowledgeRefreshScheduler()
    try:
        scheduler_clock[0] = 1_000.0
        assert _schedule_after_cooldown(scheduler, lookup, config, runtime_paths)
        scheduler_clock[0] = 1_100.0
        assert not _schedule_after_cooldown(scheduler, lookup, config, runtime_paths)

        rotated_config = _config(
            tmp_path,
            git=git.model_copy(update={"repo_url": "https://user:new-secret@example.com/x.git"}),
        )
        rotated_lookup = _resolution(rotated_config, runtime_paths, availability=KnowledgeAvailability.REFRESH_FAILED)
        failed_key = refresh_cooldown_key(lookup, config, runtime_paths, KnowledgeAvailability.REFRESH_FAILED)
        rotated_key = refresh_cooldown_key(
            rotated_lookup,
            rotated_config,
            runtime_paths,
            KnowledgeAvailability.REFRESH_FAILED,
        )

        assert rotated_key != failed_key
        assert _schedule_after_cooldown(scheduler, rotated_lookup, rotated_config, runtime_paths)
        assert "new-secret" not in repr(rotated_key)
        assert "old-secret" not in repr(rotated_key)
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.usefixtures("idle_refreshes")
@pytest.mark.parametrize("published", [True, False])
async def test_a_refresh_in_flight_supersedes_the_failure_it_retries(tmp_path: Path, published: bool) -> None:
    """While the scheduler refreshes a failed base, a turn sees it STALE, or INITIALIZING with no publication, and no error."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    failed = _resolution(config, runtime_paths, availability=KnowledgeAvailability.REFRESH_FAILED, last_error="boom")
    if not published:
        failed = replace(
            failed,
            reader=None,
            publication=None,
            availability=index_availability(
                failed.key,
                None,
                failed.status,
                publication_unreadable=False,
                refreshing=False,
            ),
        )
    assert failed.availability is KnowledgeAvailability.REFRESH_FAILED
    scheduler = KnowledgeRefreshScheduler()
    try:
        scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
        assert scheduler.is_refreshing(refresh_target_for_published_index_key(failed.key))
        source, availability, last_error = knowledge_utils._finish_base_knowledge_resolution(
            "docs",
            lookup=failed,
            config=config,
            runtime_paths=runtime_paths,
            refresh_scheduler=scheduler,
            execution_identity=None,
        )
        idle = knowledge_utils._finish_base_knowledge_resolution(
            "docs",
            lookup=failed,
            config=config,
            runtime_paths=runtime_paths,
            refresh_scheduler=None,
            execution_identity=None,
        )
    finally:
        await scheduler.shutdown()

    assert availability is (KnowledgeAvailability.STALE if published else KnowledgeAvailability.INITIALIZING)
    assert last_error is None
    assert (source is not None) is published
    assert idle[1:] == (KnowledgeAvailability.REFRESH_FAILED, "boom")


@pytest.mark.parametrize(
    "availability",
    [
        KnowledgeAvailability.READY,
        KnowledgeAvailability.INITIALIZING,
        KnowledgeAvailability.CONFIG_MISMATCH,
    ],
)
def test_cooldown_key_carries_indexing_settings(tmp_path: Path, availability: KnowledgeAvailability) -> None:
    """These availabilities fold indexing settings in, so newer config bypasses the cooldown."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=availability)

    _target, keyed_availability, settings = refresh_cooldown_key(lookup, config, runtime_paths, availability)
    assert keyed_availability is availability
    assert settings == lookup.key.indexing_settings


def test_stale_cooldown_key_omits_indexing_settings(tmp_path: Path) -> None:
    """STALE means the settings already matched, so the key deliberately carries none."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=KnowledgeAvailability.STALE)

    _target, keyed_availability, settings = refresh_cooldown_key(
        lookup,
        config,
        runtime_paths,
        KnowledgeAvailability.STALE,
    )
    assert keyed_availability is KnowledgeAvailability.STALE
    assert settings is None


def test_failed_cooldown_key_extends_indexing_settings_with_a_fingerprint(tmp_path: Path) -> None:
    """A failed retry keys on settings plus the Git credential that could fix it."""
    config = _config(tmp_path, git=KnowledgeGitConfig(repo_url="https://user:secret@example.com/x.git"))
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=KnowledgeAvailability.REFRESH_FAILED)

    _target, keyed_availability, settings = refresh_cooldown_key(
        lookup,
        config,
        runtime_paths,
        KnowledgeAvailability.REFRESH_FAILED,
    )
    assert keyed_availability is KnowledgeAvailability.REFRESH_FAILED
    assert isinstance(settings, tuple)
    assert settings[0] == lookup.key.indexing_settings
    assert len(settings) > 1
    assert "secret" not in repr(settings)
