"""Small status facade for callers outside the knowledge package."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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


@dataclass(frozen=True)
class KnowledgeSourceRootBinding:
    """One dashboard-visible source root and the owner binding that produced it."""

    root: Path
    owner_agent: str | None


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


def get_knowledge_source_roots(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    create: bool = False,
) -> tuple[Path, ...]:
    """Return every source root used by the base's query-time bindings."""
    return tuple(
        root_binding.root
        for root_binding in get_knowledge_source_root_bindings(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            create=create,
        )
    )


def get_knowledge_source_root_bindings(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    create: bool = False,
) -> tuple[KnowledgeSourceRootBinding, ...]:
    """Return every source root with the owner binding that produced it."""
    return tuple(
        KnowledgeSourceRootBinding(
            root=Path(resolved_binding.key.knowledge_path),
            owner_agent=resolved_binding.owner_agent,
        )
        for resolved_binding in registry.resolve_published_index_bindings(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            create=create,
        )
    )


def get_knowledge_index_status(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> KnowledgeIndexStatus:
    """Resolve persisted published index metadata into the small status shape callers need."""
    resolved_bindings = registry.resolve_published_index_bindings(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    if not resolved_bindings:
        return KnowledgeIndexStatus()
    if len(resolved_bindings) > 1:
        return _aggregate_knowledge_index_statuses(
            [_knowledge_index_status_for_key(resolved_binding.key) for resolved_binding in resolved_bindings],
        )
    return _knowledge_index_status_for_key(resolved_bindings[0].key)


def _knowledge_index_status_for_key(key: registry.PublishedIndexKey) -> KnowledgeIndexStatus:
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


def _aggregate_knowledge_index_statuses(statuses: list[KnowledgeIndexStatus]) -> KnowledgeIndexStatus:
    if not statuses:
        return KnowledgeIndexStatus()
    return KnowledgeIndexStatus(
        indexed_count=sum(status.indexed_count for status in statuses),
        refresh_state=_aggregate_refresh_state(statuses),
        availability=_aggregate_availability(statuses),
        persisted_index_status=_aggregate_persisted_index_status(statuses),
        last_error=next((status.last_error for status in statuses if status.last_error is not None), None),
        last_published_at=max(
            (status.last_published_at for status in statuses if status.last_published_at is not None),
            default=None,
        ),
        published_revision=None,
        metadata_exists=any(status.metadata_exists for status in statuses),
    )


def _aggregate_refresh_state(statuses: list[KnowledgeIndexStatus]) -> _KnowledgeRefreshState:
    for refresh_state in ("refreshing", "refresh_failed", "stale"):
        if any(status.refresh_state == refresh_state for status in statuses):
            return refresh_state
    if all(status.refresh_state == "none" for status in statuses):
        return "none"
    return "stale"


def _aggregate_availability(statuses: list[KnowledgeIndexStatus]) -> KnowledgeAvailability:
    if all(status.availability is KnowledgeAvailability.READY for status in statuses):
        return KnowledgeAvailability.READY
    for availability in (
        KnowledgeAvailability.REFRESH_FAILED,
        KnowledgeAvailability.CONFIG_MISMATCH,
        KnowledgeAvailability.STALE,
        KnowledgeAvailability.INITIALIZING,
    ):
        if any(status.availability is availability for status in statuses):
            return availability
    return KnowledgeAvailability.INITIALIZING


def _aggregate_persisted_index_status(statuses: list[KnowledgeIndexStatus]) -> _PersistedIndexStatus | None:
    persisted_statuses = [status.persisted_index_status for status in statuses if status.persisted_index_status]
    if not persisted_statuses:
        return None
    if all(status == "complete" for status in persisted_statuses) and len(persisted_statuses) == len(statuses):
        return "complete"
    for persisted_status in ("failed", "indexing", "resetting"):
        if persisted_status in persisted_statuses:
            return persisted_status
    return None


def _mark_existing_semantic_state_stale(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    reason: str,
) -> bool:
    marked = False
    for resolved_binding in registry.resolve_published_index_bindings(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        create=False,
    ):
        key = resolved_binding.key
        if not registry.published_index_metadata_path(key).exists():
            continue
        marked = registry.mark_published_index_stale_and_evict(key, reason=reason) or marked
    return marked


def reconcile_knowledge_mode_transition_states(
    previous_config: Config,
    current_config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, ...]:
    """Mark semantic indexes stale when config changes cross the files/semantic boundary."""
    changed_base_ids: list[str] = []
    for base_id in sorted(previous_config.knowledge_bases.keys() & current_config.knowledge_bases.keys()):
        previous_mode = previous_config.get_knowledge_base_config(base_id).mode
        current_mode = current_config.get_knowledge_base_config(base_id).mode
        if previous_mode == current_mode:
            continue

        marked = False
        if previous_mode == "semantic":
            marked = _mark_existing_semantic_state_stale(
                base_id,
                config=previous_config,
                runtime_paths=runtime_paths,
                reason=f"mode_changed_to_{current_mode}",
            )
        if current_mode == "semantic":
            marked = (
                _mark_existing_semantic_state_stale(
                    base_id,
                    config=current_config,
                    runtime_paths=runtime_paths,
                    reason="mode_changed_to_semantic",
                )
                or marked
            )
        if marked:
            changed_base_ids.append(base_id)
    return tuple(changed_base_ids)


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
