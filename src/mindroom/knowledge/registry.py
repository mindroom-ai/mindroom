"""Published knowledge collection lookup and lightweight refresh state."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, ParamSpec, Protocol, TypeVar, cast

import mindroom.knowledge.manager as manager_module
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_knowledge_binding

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.knowledge.knowledge import Knowledge

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.runtime_resolution import ResolvedKnowledgeBinding
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

# Identity levels:
# - KnowledgeSourceKey: one physical source root. It gates source mutation locks and alias fanout.
# - KnowledgeRefreshKey: one refresh target. It coalesces background work for a source and base ID.
# - KnowledgeSnapshotKey: one published, query-compatible index. It includes indexing settings for read paths.


@dataclass(frozen=True)
class KnowledgeSnapshotKey:
    """Stable key for one configured knowledge binding."""

    base_id: str
    storage_root: str
    knowledge_path: str
    indexing_settings: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeRefreshKey:
    """Stable key for refresh work for one physical knowledge binding."""

    base_id: str
    storage_root: str
    knowledge_path: str


@dataclass(frozen=True)
class KnowledgeSourceKey:
    """Stable key for source filesystem mutations shared by aliases."""

    storage_root: str
    knowledge_path: str


@dataclass(frozen=True)
class PublishedIndexingState:
    """Persisted state for the active knowledge collection."""

    settings: tuple[str, ...]
    status: Literal["resetting", "indexing", "complete", "failed"]
    collection: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    indexed_count: int | None = None
    source_signature: str | None = None
    refresh_job: Literal["idle", "pending", "running", "failed"] = "idle"
    reason: str | None = None
    last_error: str | None = None
    updated_at: str | None = None
    last_refresh_at: str | None = None


@dataclass(frozen=True)
class PublishedKnowledgeSnapshot:
    """Read handle for the active published knowledge collection."""

    key: KnowledgeSnapshotKey
    knowledge: Knowledge
    state: PublishedIndexingState
    metadata_path: Path


@dataclass(frozen=True)
class KnowledgeSnapshotLookup:
    """Result of resolving the active collection for one knowledge base."""

    key: KnowledgeSnapshotKey
    snapshot: PublishedKnowledgeSnapshot | None
    state: PublishedIndexingState | None
    availability: KnowledgeAvailability
    refresh_on_access: bool = False


class _SnapshotVectorDb(Protocol):
    client: object | None
    collection_name: str

    def exists(self) -> bool:
        """Return whether the collection exists."""
        ...


_published_snapshots: dict[KnowledgeSnapshotKey, PublishedKnowledgeSnapshot] = {}
_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX = "__agent_private__:"
_MAX_PRIVATE_PUBLISHED_SNAPSHOTS = 128
_P = ParamSpec("_P")
_T = TypeVar("_T")


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


async def _run_to_thread_to_completion_on_cancel(
    func: Callable[_P, _T],
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    thread_task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(thread_task)
    except asyncio.CancelledError:
        await asyncio.shield(thread_task)
        raise


def _snapshot_key_from_binding(
    base_id: str,
    binding: ResolvedKnowledgeBinding,
    *,
    config: Config,
) -> KnowledgeSnapshotKey:
    storage_root = binding.storage_root.expanduser().resolve()
    knowledge_path = binding.knowledge_path.resolve()
    return KnowledgeSnapshotKey(
        base_id=base_id,
        storage_root=str(storage_root),
        knowledge_path=str(knowledge_path),
        indexing_settings=manager_module._indexing_settings_key(
            config,
            storage_root,
            base_id,
            knowledge_path,
        ),
    )


def _resolve_snapshot_key_and_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> tuple[KnowledgeSnapshotKey, ResolvedKnowledgeBinding]:
    binding = resolve_knowledge_binding(
        base_id,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        start_watchers=False,
        create=create,
    )
    return _snapshot_key_from_binding(base_id, binding, config=config), binding


def resolve_snapshot_key(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> KnowledgeSnapshotKey:
    """Resolve one base ID to its current collection metadata key."""
    key, _binding = _resolve_snapshot_key_and_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    return key


def refresh_key_for_snapshot_key(key: KnowledgeSnapshotKey) -> KnowledgeRefreshKey:
    """Return the refresh key for one snapshot key."""
    return KnowledgeRefreshKey(
        base_id=key.base_id,
        storage_root=key.storage_root,
        knowledge_path=key.knowledge_path,
    )


def source_key_for_refresh_key(key: KnowledgeRefreshKey) -> KnowledgeSourceKey:
    """Return the physical source key for one refresh key."""
    return KnowledgeSourceKey(storage_root=key.storage_root, knowledge_path=key.knowledge_path)


def source_key_for_snapshot_key(key: KnowledgeSnapshotKey) -> KnowledgeSourceKey:
    """Return the physical source key for one snapshot key."""
    return KnowledgeSourceKey(storage_root=key.storage_root, knowledge_path=key.knowledge_path)


def resolve_refresh_key(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> KnowledgeRefreshKey:
    """Resolve one base ID to its refresh key."""
    return refresh_key_for_snapshot_key(
        resolve_snapshot_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            create=create,
        ),
    )


def snapshot_base_storage_path(key: KnowledgeSnapshotKey) -> Path:
    """Return the storage directory for one resolved knowledge base."""
    knowledge_path = Path(key.knowledge_path)
    return (
        Path(key.storage_root) / "knowledge_db" / manager_module._base_storage_key(key.base_id, knowledge_path)
    ).resolve()


def snapshot_metadata_path(key: KnowledgeSnapshotKey) -> Path:
    """Return the single persisted state file for one knowledge base."""
    return snapshot_base_storage_path(key) / "indexing_settings.json"


def _coerce_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _coerce_status(value: object) -> Literal["resetting", "indexing", "complete", "failed"] | None:
    if value in {"resetting", "indexing", "complete", "failed"}:
        return cast('Literal["resetting", "indexing", "complete", "failed"]', value)
    return None


def _coerce_refresh_job(value: object) -> Literal["idle", "pending", "running", "failed"]:
    if value in {"idle", "pending", "running", "failed"}:
        return cast('Literal["idle", "pending", "running", "failed"]', value)
    return "idle"


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def load_published_indexing_state(metadata_path: Path) -> PublishedIndexingState | None:
    """Load active collection metadata."""
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    raw_settings = payload.get("settings")
    status = _coerce_status(payload.get("status"))
    if not isinstance(raw_settings, list) or not all(isinstance(item, str) for item in raw_settings) or status is None:
        return None

    collection = _optional_str(payload.get("collection"))
    indexed_count = _coerce_nonnegative_int(payload.get("indexed_count"))
    source_signature = _optional_str(payload.get("source_signature"))
    if status == "complete" and (collection is None or indexed_count is None or source_signature is None):
        return None

    return PublishedIndexingState(
        settings=tuple(raw_settings),
        status=status,
        collection=collection,
        last_published_at=_optional_str(payload.get("last_published_at")),
        published_revision=_optional_str(payload.get("published_revision")),
        indexed_count=indexed_count,
        source_signature=source_signature,
        refresh_job=_coerce_refresh_job(payload.get("refresh_job")),
        reason=_optional_str(payload.get("reason")),
        last_error=_optional_str(payload.get("last_error")),
        updated_at=_optional_str(payload.get("updated_at")),
        last_refresh_at=_optional_str(payload.get("last_refresh_at")),
    )


def save_published_indexing_state(metadata_path: Path, state: PublishedIndexingState) -> None:
    """Atomically persist active collection metadata."""
    payload: dict[str, object] = {
        "settings": list(state.settings),
        "status": state.status,
        "refresh_job": state.refresh_job,
    }
    payload.update(
        {
            key: value
            for key, value in (
                ("collection", state.collection),
                ("last_published_at", state.last_published_at),
                ("published_revision", state.published_revision),
                ("indexed_count", state.indexed_count),
                ("source_signature", state.source_signature),
                ("reason", state.reason),
                ("last_error", state.last_error),
                ("updated_at", state.updated_at),
                ("last_refresh_at", state.last_refresh_at),
            )
            if value is not None
        },
    )

    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp_path.replace(metadata_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def snapshot_refresh_state(
    state: PublishedIndexingState | None,
    *,
    metadata_exists: bool = False,
) -> Literal["none", "stale", "refreshing", "refresh_failed"]:
    """Return the UI refresh state derived from the single metadata file."""
    if state is None:
        return "refresh_failed" if metadata_exists else "none"
    if state.status == "failed" or state.refresh_job == "failed" or state.last_error is not None:
        return "refresh_failed"
    if state.refresh_job == "running":
        return "refreshing"
    if state.refresh_job == "pending" or state.reason is not None:
        return "stale"
    return "none"


def _state_with_refresh_fields(
    key: KnowledgeSnapshotKey,
    *,
    refresh_job: Literal["idle", "pending", "running", "failed"],
    status_when_missing: Literal["indexing", "failed"],
    reason: str | None = None,
    last_error: str | None = None,
    clear_error: bool = False,
) -> PublishedIndexingState:
    current = load_published_indexing_state(snapshot_metadata_path(key))
    now = _utc_now()
    if current is None:
        return PublishedIndexingState(
            settings=key.indexing_settings,
            status=status_when_missing,
            refresh_job=refresh_job,
            reason=reason,
            last_error=last_error,
            updated_at=now,
            last_refresh_at=now if refresh_job in {"idle", "failed"} else None,
        )
    return replace(
        current,
        refresh_job=refresh_job,
        reason=reason,
        last_error=None if clear_error else last_error,
        updated_at=now,
        last_refresh_at=now if refresh_job in {"idle", "failed"} else current.last_refresh_at,
    )


def save_snapshot_dirty_state(
    key: KnowledgeSnapshotKey,
    *,
    reason: str,
    refresh_job: Literal["idle", "pending", "running", "failed"] = "pending",
) -> None:
    """Mark the active collection stale without changing the published pointer."""
    save_published_indexing_state(
        snapshot_metadata_path(key),
        _state_with_refresh_fields(
            key,
            refresh_job=refresh_job,
            status_when_missing="indexing",
            reason=reason,
        ),
    )


def save_snapshot_refreshing_state(key: KnowledgeSnapshotKey, *, reason: str = "refreshing") -> None:
    """Mark refresh work running while keeping the old active collection readable."""
    save_published_indexing_state(
        snapshot_metadata_path(key),
        _state_with_refresh_fields(
            key,
            refresh_job="running",
            status_when_missing="indexing",
            reason=reason,
        ),
    )


def save_snapshot_refresh_failed_state(key: KnowledgeSnapshotKey, *, error: str) -> None:
    """Record refresh failure while keeping any old active collection pointer."""
    current = load_published_indexing_state(snapshot_metadata_path(key))
    state = _state_with_refresh_fields(
        key,
        refresh_job="failed",
        status_when_missing="failed",
        reason="refresh_failed",
        last_error=error,
    )
    if current is not None and current.status == "complete":
        state = replace(state, status="complete")
    save_published_indexing_state(snapshot_metadata_path(key), state)


def save_snapshot_refresh_success_state(key: KnowledgeSnapshotKey) -> None:
    """Clear refresh status after a successful publish."""
    state = load_published_indexing_state(snapshot_metadata_path(key))
    if state is None:
        return
    save_published_indexing_state(
        snapshot_metadata_path(key),
        replace(
            state,
            refresh_job="idle",
            reason=None,
            last_error=None,
            updated_at=_utc_now(),
            last_refresh_at=_utc_now(),
        ),
    )


def _state_collection_name(key: KnowledgeSnapshotKey, state: PublishedIndexingState) -> str:
    _ = key
    if state.collection is None:
        msg = "Published knowledge metadata is missing a collection name"
        raise ValueError(msg)
    return state.collection


def _build_snapshot_vector_db(
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> _SnapshotVectorDb:
    return cast(
        "_SnapshotVectorDb",
        manager_module.ChromaDb(
            collection=_state_collection_name(key, state),
            path=str(snapshot_base_storage_path(key)),
            persistent_client=True,
            embedder=manager_module._create_embedder(config, runtime_paths),
        ),
    )


def _build_snapshot_knowledge(
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Knowledge:
    return manager_module.Knowledge(
        vector_db=_build_snapshot_vector_db(key, state, config=config, runtime_paths=runtime_paths),
    )


def snapshot_collection_exists_for_state(key: KnowledgeSnapshotKey, state: PublishedIndexingState) -> bool:
    """Return whether persisted metadata points at an existing active collection."""
    if state.status != "complete" or state.collection is None:
        return False
    try:
        return manager_module.chroma_collection_exists(snapshot_base_storage_path(key), state.collection)
    except Exception:
        logger.warning(
            "Published knowledge collection existence check failed",
            base_id=key.base_id,
            collection=state.collection,
            exc_info=True,
        )
        return False


def indexing_settings_query_compatible(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether current queries can use a collection from published settings."""
    prefix_length = manager_module.INDEXING_SETTINGS_QUERY_COMPATIBLE_PREFIX_LENGTH
    if len(published_settings) < prefix_length or len(current_settings) < prefix_length:
        return published_settings == current_settings
    return published_settings[:prefix_length] == current_settings[:prefix_length]


def indexing_settings_corpus_compatible(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether published content is safe for the current corpus config."""
    corpus_indexes = manager_module.INDEXING_SETTINGS_CORPUS_COMPATIBLE_INDEXES
    if len(published_settings) <= max(corpus_indexes) or len(current_settings) <= max(corpus_indexes):
        return published_settings == current_settings
    return all(published_settings[index] == current_settings[index] for index in corpus_indexes)


def indexing_settings_metadata_equal(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether persisted metadata exactly matches current indexing settings."""
    return published_settings == current_settings


def indexing_settings_snapshot_compatible(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether a published collection can be queried under the current config."""
    return indexing_settings_query_compatible(
        published_settings,
        current_settings,
    ) and indexing_settings_corpus_compatible(published_settings, current_settings)


def _snapshot_state_queryable(key: KnowledgeSnapshotKey, state: PublishedIndexingState) -> bool:
    return (
        state.status == "complete"
        and state.collection is not None
        and indexing_settings_snapshot_compatible(state.settings, key.indexing_settings)
    )


def _snapshot_availability(
    *,
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState | None,
    metadata_exists: bool = False,
) -> KnowledgeAvailability:
    refresh_state = snapshot_refresh_state(state, metadata_exists=metadata_exists)
    if state is None:
        availability = (
            KnowledgeAvailability.REFRESH_FAILED
            if refresh_state == "refresh_failed"
            else KnowledgeAvailability.INITIALIZING
        )
    elif state.collection is None and refresh_state == "refresh_failed":
        availability = KnowledgeAvailability.REFRESH_FAILED
    elif not indexing_settings_snapshot_compatible(
        state.settings,
        key.indexing_settings,
    ) or not indexing_settings_metadata_equal(state.settings, key.indexing_settings):
        availability = KnowledgeAvailability.CONFIG_MISMATCH
    elif refresh_state == "refresh_failed":
        availability = KnowledgeAvailability.REFRESH_FAILED
    elif state.status != "complete":
        availability = KnowledgeAvailability.INITIALIZING
    elif refresh_state in {"stale", "refreshing"}:
        availability = KnowledgeAvailability.STALE
    else:
        availability = KnowledgeAvailability.READY
    return availability


def snapshot_availability_for_state(
    *,
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState | None,
    metadata_exists: bool = False,
) -> KnowledgeAvailability:
    """Return the public availability value for active collection state."""
    return _snapshot_availability(
        key=key,
        state=state,
        metadata_exists=metadata_exists,
    )


def _cached_snapshot_still_queryable(snapshot: PublishedKnowledgeSnapshot) -> bool:
    if not _snapshot_state_queryable(snapshot.key, snapshot.state):
        return False
    vector_db = cast("_SnapshotVectorDb | None", snapshot.knowledge.vector_db)
    return vector_db is not None and vector_db.exists()


def _load_queryable_snapshot_from_state(
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Knowledge | None:
    if not _snapshot_state_queryable(key, state):
        return None
    if not snapshot_collection_exists_for_state(key, state):
        return None
    return _build_snapshot_knowledge(key, state, config=config, runtime_paths=runtime_paths)


def get_published_snapshot(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> KnowledgeSnapshotLookup:
    """Return the currently active collection, if one is usable."""
    key, binding = _resolve_snapshot_key_and_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    metadata_path = snapshot_metadata_path(key)
    state = load_published_indexing_state(metadata_path)
    availability = _snapshot_availability(key=key, state=state, metadata_exists=metadata_path.exists())

    snapshot = _published_snapshots.get(key)
    if snapshot is not None:
        if _cached_snapshot_still_queryable(snapshot):
            return KnowledgeSnapshotLookup(
                key=key,
                snapshot=snapshot,
                state=state,
                availability=availability,
                refresh_on_access=binding.incremental_sync_on_access,
            )
        _published_snapshots.pop(key, None)

    if state is None:
        return KnowledgeSnapshotLookup(
            key=key,
            snapshot=None,
            state=state,
            availability=availability,
            refresh_on_access=binding.incremental_sync_on_access,
        )

    knowledge = _load_queryable_snapshot_from_state(key, state, config=config, runtime_paths=runtime_paths)
    if knowledge is None:
        return KnowledgeSnapshotLookup(
            key=key,
            snapshot=None,
            state=state,
            availability=availability
            if availability is not KnowledgeAvailability.READY
            else KnowledgeAvailability.REFRESH_FAILED,
            refresh_on_access=binding.incremental_sync_on_access,
        )

    snapshot = PublishedKnowledgeSnapshot(
        key=key,
        knowledge=knowledge,
        state=state,
        metadata_path=snapshot_metadata_path(key),
    )
    _cache_published_snapshot(snapshot)
    return KnowledgeSnapshotLookup(
        key=key,
        snapshot=snapshot,
        state=state,
        availability=availability,
        refresh_on_access=binding.incremental_sync_on_access,
    )


def publish_snapshot(
    key: KnowledgeSnapshotKey,
    *,
    knowledge: Knowledge,
    state: PublishedIndexingState,
    metadata_path: Path | None = None,
) -> PublishedKnowledgeSnapshot:
    """Publish a read handle in this process."""
    _evict_published_snapshots_for_refresh_key(refresh_key_for_snapshot_key(key))
    snapshot = PublishedKnowledgeSnapshot(
        key=key,
        knowledge=knowledge,
        state=state,
        metadata_path=metadata_path or snapshot_metadata_path(key),
    )
    _cache_published_snapshot(snapshot)
    return snapshot


def publish_snapshot_from_state(
    key: KnowledgeSnapshotKey,
    *,
    state: PublishedIndexingState,
    config: Config,
    runtime_paths: RuntimePaths,
    metadata_path: Path | None = None,
) -> PublishedKnowledgeSnapshot | None:
    """Publish a read handle rebuilt from persisted metadata."""
    knowledge = _load_queryable_snapshot_from_state(key, state, config=config, runtime_paths=runtime_paths)
    if knowledge is None:
        return None
    return publish_snapshot(key, knowledge=knowledge, state=state, metadata_path=metadata_path)


def snapshot_indexed_count(snapshot: PublishedKnowledgeSnapshot) -> int:
    """Return the persisted indexed source file count."""
    return snapshot.state.indexed_count or 0


def _same_physical_binding(key: KnowledgeSnapshotKey, refresh_key: KnowledgeRefreshKey) -> bool:
    return (
        key.base_id == refresh_key.base_id
        and key.storage_root == refresh_key.storage_root
        and key.knowledge_path == refresh_key.knowledge_path
    )


def _same_physical_source(left: KnowledgeSnapshotKey, right: KnowledgeSnapshotKey) -> bool:
    return left.storage_root == right.storage_root and left.knowledge_path == right.knowledge_path


def _snapshot_key_is_private(key: KnowledgeSnapshotKey) -> bool:
    return key.base_id.startswith(_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX)


def prune_private_snapshot_bookkeeping() -> None:
    """Bound request-scoped in-process snapshot handles."""
    private_snapshot_keys = [key for key in _published_snapshots if _snapshot_key_is_private(key)]
    for key in private_snapshot_keys[:-_MAX_PRIVATE_PUBLISHED_SNAPSHOTS]:
        _published_snapshots.pop(key, None)


def _cache_published_snapshot(snapshot: PublishedKnowledgeSnapshot) -> None:
    _published_snapshots[snapshot.key] = snapshot
    prune_private_snapshot_bookkeeping()


def _evict_published_snapshots_for_refresh_key(refresh_key: KnowledgeRefreshKey) -> None:
    for cached_key in tuple(_published_snapshots):
        if _same_physical_binding(cached_key, refresh_key):
            _published_snapshots.pop(cached_key, None)


def _snapshot_keys_for_shared_source(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> tuple[KnowledgeSnapshotKey, ...]:
    key = resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    matching_keys = [key]
    for candidate_base_id in config.knowledge_bases:
        if candidate_base_id == base_id:
            continue
        try:
            candidate_key = resolve_snapshot_key(
                candidate_base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
        except Exception:
            logger.warning(
                "Could not resolve related knowledge snapshot while marking dirty",
                base_id=base_id,
                related_base_id=candidate_base_id,
                exc_info=True,
            )
            continue
        if _same_physical_source(candidate_key, key):
            matching_keys.append(candidate_key)
    return tuple(matching_keys)


def _mark_snapshot_key_dirty_on_disk(matching_key: KnowledgeSnapshotKey, *, reason: str) -> bool:
    save_snapshot_dirty_state(matching_key, reason=reason)
    _evict_published_snapshots_for_refresh_key(refresh_key_for_snapshot_key(matching_key))
    return True


def mark_snapshot_dirty(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
) -> tuple[str, ...]:
    """Mark same-source snapshots stale after a source mutation."""
    matching_keys = _snapshot_keys_for_shared_source(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    for matching_key in matching_keys:
        _mark_snapshot_key_dirty_on_disk(matching_key, reason=reason)
    return tuple(dict.fromkeys(key.base_id for key in matching_keys))


async def mark_snapshot_dirty_async(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
) -> tuple[str, ...]:
    """Async stale marker that keeps metadata I/O off the event loop."""
    return await _run_to_thread_to_completion_on_cancel(
        mark_snapshot_dirty,
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        reason=reason,
    )


def clear_published_snapshots() -> None:
    """Clear process-local read handles."""
    _published_snapshots.clear()
