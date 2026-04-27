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

_PersistedIndexStatus = Literal["resetting", "indexing", "complete", "failed"]
_KnowledgeRefreshState = Literal["none", "stale", "refreshing", "refresh_failed"]


@dataclass(frozen=True)
class KnowledgeIndexStatus:
    """Read-only published index status for API and runtime status surfaces."""

    indexed_count: int = 0
    refresh_state: _KnowledgeRefreshState = "none"
    availability: KnowledgeAvailability = KnowledgeAvailability.INITIALIZING
    persisted_index_status: _PersistedIndexStatus | None = None
    last_error: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    metadata_exists: bool = False

    @property
    def initial_sync_complete(self) -> bool:
        """Return whether a Git-backed source has produced at least one committed index."""
        return self.persisted_index_status == "complete" and self.published_revision is not None


def _indexed_count_for_state(
    key: registry.PublishedIndexKey,
    state: registry.PublishedIndexState | None,
) -> int:
    if state is None:
        return 0
    if state.status != "complete":
        return 0
    if not registry.published_index_settings_compatible(state.settings, key.indexing_settings):
        return 0
    return state.indexed_count or 0


def get_knowledge_index_status(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> KnowledgeIndexStatus:
    """Resolve persisted published index metadata into the small status shape callers need."""
    key = registry.resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    metadata_path = registry.published_index_metadata_path(key)
    metadata_exists = metadata_path.exists()
    state = registry.load_published_index_state(metadata_path)
    return KnowledgeIndexStatus(
        indexed_count=_indexed_count_for_state(key, state),
        refresh_state=registry.published_index_refresh_state(state, metadata_exists=metadata_exists),
        availability=registry.published_index_availability_for_state(
            key=key,
            state=state,
            metadata_exists=metadata_exists,
        ),
        persisted_index_status=state.status if state is not None else None,
        last_error=state.last_error if state is not None else None,
        last_published_at=state.last_published_at if state is not None else None,
        published_revision=state.published_revision if state is not None else None,
        metadata_exists=metadata_exists,
    )


async def mark_knowledge_source_changed_async(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
) -> tuple[str, ...]:
    """Mark same-source published indexes stale without exposing registry internals to callers."""
    return await registry.mark_knowledge_source_changed_async(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        reason=reason,
    )
