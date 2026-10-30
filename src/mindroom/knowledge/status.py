"""Small status facade for callers outside the knowledge package."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.knowledge import registry
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.collections import CollectionSpace, open_collection, owned_collection_names
from mindroom.knowledge.index_metadata import load_publication, load_refresh_status, publication_path
from mindroom.knowledge.indexing_config import IndexingSettings, published_index_settings_compatible

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_KnowledgeRefreshState = Literal["none", "stale", "refreshing", "refresh_failed"]


@dataclass(frozen=True)
class KnowledgeCandidateStatus:
    """Progress of the unfinished candidate index being built for one base.

    Deliberately separate from ``indexed_count``: candidate work is not
    queryable, and reporting it as published would tell operators an index is
    usable when nothing has been published yet.
    """

    collection: str
    status: Literal["building", "failed"]
    completed_count: int
    failed_count: int
    total_files: int
    target_revision: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def pending_count(self) -> int:
        """Return files the candidate still owes before it can be published."""
        return max(self.total_files - self.completed_count, 0)


@dataclass(frozen=True)
class KnowledgeIndexStatus:
    """Read-only published index status for API and runtime status surfaces."""

    indexed_count: int = 0
    refresh_state: _KnowledgeRefreshState = "none"
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
    found: list[KnowledgeCandidateStatus] = []
    for name in owned_collection_names(space):
        if name == published:
            continue
        collection = open_collection(space, name)
        try:
            meta = collection.meta()
            completed, failed = collection.claim_counts()
        except (sqlite3.DatabaseError, LookupError, TypeError, ValueError):
            continue
        if meta.stage != "building" or IndexingSettings.from_metadata(meta.settings) != key.indexing_settings:
            continue
        found.append(
            KnowledgeCandidateStatus(
                collection=name,
                status="failed" if failed else "building",
                completed_count=completed,
                failed_count=failed,
                total_files=meta.total_files,
                target_revision=meta.revision,
                created_at=meta.created_at,
                updated_at=meta.updated_at,
            ),
        )
    return max(found, key=lambda candidate: candidate.updated_at or "", default=None)


def get_knowledge_index_status(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
    refreshing: bool = False,
) -> KnowledgeIndexStatus:
    """Resolve one base's publication and refresh status into the small status shape callers need.

    ``refreshing`` is the scheduler's answer for the base, which only the runtime knows.
    """
    key = registry.resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    storage = registry.published_index_storage_path(key)
    publication = load_publication(storage)
    unreadable = publication is None and publication_path(storage).exists()
    status = load_refresh_status(storage)
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
        last_error=None if refreshing else status.last_error,
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
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> bool:
    """Return whether the runtime's scheduler is refreshing one base; a base whose binding cannot resolve is not."""
    if refresh_scheduler is None:
        return False
    try:
        target = registry.resolve_refresh_target(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            create=False,
        )
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
