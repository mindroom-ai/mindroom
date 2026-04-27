"""Small status facade for callers outside the knowledge package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.knowledge import registry
from mindroom.knowledge.availability import KnowledgeAvailability

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_KnowledgeIndexingStatus = Literal["resetting", "indexing", "complete", "failed"]
_KnowledgeRefreshState = Literal["none", "stale", "refreshing", "refresh_failed"]


@dataclass(frozen=True)
class KnowledgeSnapshotStatus:
    """Read-only snapshot status for API and runtime status surfaces."""

    indexed_count: int = 0
    refresh_state: _KnowledgeRefreshState = "none"
    availability: KnowledgeAvailability = KnowledgeAvailability.INITIALIZING
    indexing_status: _KnowledgeIndexingStatus | None = None
    last_error: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    metadata_exists: bool = False

    @property
    def initial_sync_complete(self) -> bool:
        """Return whether a Git-backed source has produced at least one committed snapshot."""
        return self.indexing_status == "complete" and self.published_revision is not None


def _indexed_count_for_state(
    key: registry.KnowledgeSnapshotKey,
    state: registry.PublishedIndexingState | None,
) -> int:
    if state is None:
        return 0
    if state.status != "complete":
        return 0
    if not registry.indexing_settings_snapshot_compatible(state.settings, key.indexing_settings):
        return 0
    return state.indexed_count or 0


def get_knowledge_snapshot_status(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> KnowledgeSnapshotStatus:
    """Resolve persisted snapshot metadata into the small status shape callers need."""
    key = registry.resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    metadata_path = registry.snapshot_metadata_path(key)
    metadata_exists = metadata_path.exists()
    state = registry.load_published_indexing_state(metadata_path)
    return KnowledgeSnapshotStatus(
        indexed_count=_indexed_count_for_state(key, state),
        refresh_state=registry.snapshot_refresh_state(state, metadata_exists=metadata_exists),
        availability=registry.snapshot_availability_for_state(
            key=key,
            state=state,
            metadata_exists=metadata_exists,
        ),
        indexing_status=state.status if state is not None else None,
        last_error=state.last_error if state is not None else None,
        last_published_at=state.last_published_at if state is not None else None,
        published_revision=state.published_revision if state is not None else None,
        metadata_exists=metadata_exists,
    )


async def mark_source_dirty_async(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
) -> tuple[str, ...]:
    """Mark same-source snapshots stale without exposing registry internals to callers."""
    return await registry.mark_source_dirty_async(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        reason=reason,
    )
