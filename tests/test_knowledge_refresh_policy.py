"""Knowledge refresh-policy decisions, taken with no scheduler and no globals.

Both clocks are injected, so the Git poll-interval boundary and the cooldown
window are pinned to exact instants rather than approximated against wall time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import AgentConfig, Config
from mindroom.knowledge import registry as knowledge_registry
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.refresh_policy import (
    RefreshCooldownKey,
    cooldown_elapsed,
    ready_index_effective_availability,
    refresh_cooldown_key,
    refresh_trigger,
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

_WALL_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


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
    last_refresh_at = None if last_refresh_age is None else (_WALL_NOW - last_refresh_age).isoformat()
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


def test_ready_index_without_on_access_refresh_wants_nothing(tmp_path: Path) -> None:
    """The common READY read decides to do nothing before any scheduler probe."""
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


def test_cooldown_window_boundary_is_exact(tmp_path: Path) -> None:
    """A cooldown suppresses rescheduling right up to its boundary, then allows it."""
    config = _config(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=KnowledgeAvailability.STALE)
    key = refresh_cooldown_key(lookup, config, runtime_paths, KnowledgeAvailability.STALE)
    scheduled_at: dict[RefreshCooldownKey, float] = {key: 1_000.0}

    assert not cooldown_elapsed(scheduled_at, key, monotonic_now=1_299.0, cooldown_seconds=300.0)
    assert cooldown_elapsed(scheduled_at, key, monotonic_now=1_300.0, cooldown_seconds=300.0)
    assert cooldown_elapsed({}, key, monotonic_now=0.0, cooldown_seconds=300.0)


def test_changed_git_credentials_retry_a_failed_refresh_before_the_cooldown(tmp_path: Path) -> None:
    """Rotating the credential that broke a refresh must not wait out the retry cooldown."""
    git = KnowledgeGitConfig(repo_url="https://user:old-secret@example.com/x.git")
    config = _config(tmp_path, git=git)
    runtime_paths = test_runtime_paths(tmp_path)
    lookup = _resolution(config, runtime_paths, availability=KnowledgeAvailability.REFRESH_FAILED)
    failed_key = refresh_cooldown_key(lookup, config, runtime_paths, KnowledgeAvailability.REFRESH_FAILED)
    scheduled_at: dict[RefreshCooldownKey, float] = {failed_key: 1_000.0}

    assert not cooldown_elapsed(scheduled_at, failed_key, monotonic_now=1_100.0, cooldown_seconds=300.0)

    rotated_config = _config(
        tmp_path,
        git=git.model_copy(update={"repo_url": "https://user:new-secret@example.com/x.git"}),
    )
    rotated_lookup = _resolution(rotated_config, runtime_paths, availability=KnowledgeAvailability.REFRESH_FAILED)
    rotated_key = refresh_cooldown_key(
        rotated_lookup,
        rotated_config,
        runtime_paths,
        KnowledgeAvailability.REFRESH_FAILED,
    )

    assert rotated_key != failed_key
    assert cooldown_elapsed(scheduled_at, rotated_key, monotonic_now=1_100.0, cooldown_seconds=300.0)
    assert "new-secret" not in repr(rotated_key)
    assert "old-secret" not in repr(rotated_key)


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
