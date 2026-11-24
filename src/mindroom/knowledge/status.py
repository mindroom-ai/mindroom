"""Small status facade for callers outside the knowledge package."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.knowledge import registry
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.collection_lifetime import collection_lifetime_lock
from mindroom.knowledge.collections import CollectionSpace, working_collections
from mindroom.knowledge.index_metadata import (
    load_publication,
    load_refresh_status,
    publication_path,
    publication_unreadable,
)
from mindroom.knowledge.indexing_config import published_index_settings_compatible

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class KnowledgeCandidateStatus:
    """Progress of the unfinished candidate index being built for one base.

    Deliberately separate from ``indexed_count``: candidate work is not
    queryable, and reporting it as published would tell operators an index is
    usable when nothing has been published yet.
    """

    status: Literal["building", "failed"]
    completed_count: int
    failed_count: int
    total_files: int
    target_revision: str | None
    created_at: str
    updated_at: str

    @property
    def pending_count(self) -> int:
        """Return files the candidate still owes before it can be published."""
        return max(self.total_files - self.completed_count, 0)


@dataclass(frozen=True)
class KnowledgeIndexStatus:
    """Read-only published index status for API and runtime status surfaces."""

    indexed_count: int = 0
    refresh_state: registry.KnowledgeRefreshState = "none"
    availability: KnowledgeAvailability = KnowledgeAvailability.INITIALIZING
    published: bool = False
    last_error: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    candidate: KnowledgeCandidateStatus | None = None

    @property
    def initial_sync_complete(self) -> bool:
        """Return whether a Git-backed source has produced at least one committed index."""
        return self.published and self.published_revision is not None


def _candidate_status_for_key(
    key: registry.PublishedIndexKey,
    published: str | None,
) -> KnowledgeCandidateStatus | None:
    """Return the progress of the base's unfinished build under the current settings, the newest when several."""
    space = CollectionSpace(
        base_id=key.base_id,
        knowledge_path=Path(key.knowledge_path),
        storage_path=registry.published_index_storage_path(key),
    )
    if not space.storage_path.is_dir():
        return None
    found: list[KnowledgeCandidateStatus] = []
    # Shared, so no cleanup deletes a listed collection before its claims are counted.
    with collection_lifetime_lock(space.storage_path, exclusive=False):
        for collection, meta in working_collections(space, published=published, settings=key.indexing_settings):
            if meta.stage != "building":
                continue
            completed, failed = collection.claim_counts()
            found.append(
                KnowledgeCandidateStatus(
                    status="failed" if failed else "building",
                    completed_count=completed,
                    failed_count=failed,
                    total_files=meta.total_files,
                    target_revision=meta.revision,
                    created_at=meta.created_at,
                    updated_at=meta.updated_at,
                ),
            )
    return max(found, key=lambda candidate: candidate.updated_at, default=None)


def get_knowledge_index_status(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    refreshing: bool = False,
) -> KnowledgeIndexStatus:
    """Resolve one base's publication and refresh status into the small status shape callers need.

    ``refreshing`` is the scheduler's answer for the base, which only the runtime knows.
    """
    key = registry.resolve_published_index_key(base_id, config=config, runtime_paths=runtime_paths)
    storage = registry.published_index_storage_path(key)
    publication = load_publication(storage)
    unreadable = publication_unreadable(storage, publication)
    status = registry.reported_refresh_status(load_refresh_status(storage), refreshing=refreshing)
    return KnowledgeIndexStatus(
        candidate=_candidate_status_for_key(key, None if publication is None else publication.collection),
        indexed_count=(
            publication.indexed_count
            if publication is not None
            and published_index_settings_compatible(publication.settings, key.indexing_settings)
            else 0
        ),
        refresh_state=registry.index_refresh_state(status, publication_unreadable=unreadable, refreshing=refreshing),
        availability=registry.index_availability(
            key,
            publication,
            status,
            publication_unreadable=unreadable,
            refreshing=refreshing,
        ),
        published=publication is not None,
        last_error=status.last_error,
        last_published_at=None if publication is None else publication.last_published_at,
        published_revision=None if publication is None else publication.published_revision,
    )


def reconcile_knowledge_mode_transition_states(
    previous_config: Config,
    current_config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, ...]:
    """Mark the semantic side's index stale when a config change crosses the files/semantic boundary."""
    changed_base_ids: list[str] = []
    for base_id in sorted(previous_config.knowledge_bases.keys() & current_config.knowledge_bases.keys()):
        previous_mode = previous_config.get_knowledge_base_config(base_id).mode
        current_mode = current_config.get_knowledge_base_config(base_id).mode
        if previous_mode == current_mode:
            continue
        semantic_config = previous_config if previous_mode == "semantic" else current_config
        key = registry.resolve_published_index_key(base_id, config=semantic_config, runtime_paths=runtime_paths)
        if publication_path(registry.published_index_storage_path(key)).exists():
            registry.mark_published_index_stale(key, reason=f"mode_changed_to_{current_mode}")
            changed_base_ids.append(base_id)
    return tuple(changed_base_ids)


def knowledge_base_refreshing(
    refresh_scheduler: KnowledgeRefreshScheduler,
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether the runtime's scheduler is refreshing one base; a base whose binding cannot resolve is not."""
    try:
        target = registry.resolve_refresh_target(base_id, config=config, runtime_paths=runtime_paths)
    except ValueError:
        return False
    return refresh_scheduler.is_refreshing(target)


async def mark_knowledge_source_changed_async(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
) -> tuple[str, ...]:
    """Mark same-source published indexes stale without exposing registry internals to callers.

    ``mindroom.api.knowledge`` is the caller this exists for: Tach does not let it
    depend on ``mindroom.knowledge.registry``, so the status facade is its only
    route to this operation. Callers inside the knowledge package use the registry
    function directly.
    """
    return await registry.mark_knowledge_source_changed_async(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        reason=reason,
    )
