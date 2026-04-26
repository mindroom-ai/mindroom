"""Published knowledge snapshot lookup for request-safe RAG access."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

import mindroom.knowledge.manager as manager_module
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_knowledge_binding

if TYPE_CHECKING:
    from agno.knowledge.knowledge import Knowledge

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.runtime_resolution import ResolvedKnowledgeBinding
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


@dataclass(frozen=True)
class KnowledgeSnapshotKey:
    """Stable cache key for one resolved knowledge binding and indexing shape."""

    base_id: str
    storage_root: str
    knowledge_path: str
    indexing_settings: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeRefreshKey:
    """Stable key for one authored base bound to a physical source."""

    base_id: str
    storage_root: str
    knowledge_path: str


@dataclass(frozen=True)
class KnowledgeSourceKey:
    """Stable key for source filesystem mutations shared across duplicate bases."""

    storage_root: str
    knowledge_path: str


@dataclass(frozen=True)
class PublishedIndexingState:
    """Metadata for the currently published collection on disk."""

    settings: tuple[str, ...]
    status: Literal["resetting", "indexing", "complete"]
    collection: str | None = None
    # Legacy fields may be loaded from old metadata, but new writes keep
    # published metadata limited to the immutable last-good pointer.
    availability: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    last_error: str | None = None
    indexed_count: int | None = None
    source_signature: str | None = None
    retained_collections: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublishedKnowledgeSnapshot:
    """Immutable read handle for an already-published knowledge collection."""

    key: KnowledgeSnapshotKey
    knowledge: Knowledge
    state: PublishedIndexingState
    metadata_path: Path


@dataclass(frozen=True)
class KnowledgeAdvisoryState:
    """Sidecar state for dirty/refresh/failure notices about a published snapshot."""

    state: Literal["none", "stale", "refreshing", "refresh_failed"] = "none"
    refresh_job: Literal["idle", "pending", "running", "failed"] = "idle"
    reason: str | None = None
    last_error: str | None = None
    updated_at: str | None = None
    last_refresh_at: str | None = None


@dataclass(frozen=True)
class KnowledgeSnapshotState:
    """Central state model for one knowledge binding."""

    published: PublishedIndexingState | None
    advisory: KnowledgeAdvisoryState


@dataclass(frozen=True)
class KnowledgeSnapshotLookup:
    """Result of a request-safe snapshot lookup."""

    key: KnowledgeSnapshotKey
    snapshot: PublishedKnowledgeSnapshot | None
    availability: KnowledgeAvailability
    advisory: KnowledgeAdvisoryState
    refresh_on_access: bool = False


@dataclass(frozen=True)
class _PublishedKnowledgeLease:
    """Collection protection tied to a returned Knowledge object's lifetime."""

    key: KnowledgeSnapshotKey
    collection: str


class _SnapshotCollection(Protocol):
    """Collection surface used for API status counters."""

    def get(
        self,
        *,
        limit: int,
        offset: int,
        include: list[str],
    ) -> dict[str, object]:
        """Return one batch of collection metadata."""
        ...


class _SnapshotVectorClient(Protocol):
    """Vector DB client surface used for API status counters."""

    def get_collection(self, *, name: str) -> _SnapshotCollection:
        """Return one named collection."""
        ...


class _SnapshotVectorDb(Protocol):
    """Vector DB surface used for API status counters."""

    client: _SnapshotVectorClient | None
    collection_name: str

    def exists(self) -> bool:
        """Return whether the collection exists."""
        ...


_published_snapshots: dict[KnowledgeSnapshotKey, PublishedKnowledgeSnapshot] = {}
_published_snapshot_handles: weakref.WeakValueDictionary[int, PublishedKnowledgeSnapshot] = (
    weakref.WeakValueDictionary()
)
_published_knowledge_leases: dict[int, _PublishedKnowledgeLease] = {}
_published_knowledge_finalizers: dict[int, weakref.finalize] = {}
_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX = "__agent_private__:"
_MAX_PRIVATE_PUBLISHED_SNAPSHOTS = 128


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
    return (
        _snapshot_key_from_binding(base_id, binding, config=config),
        binding,
    )


def resolve_snapshot_key(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> KnowledgeSnapshotKey:
    """Resolve one base ID to the process-local snapshot key."""
    key, _binding = _resolve_snapshot_key_and_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    return key


def refresh_key_for_snapshot_key(key: KnowledgeSnapshotKey) -> KnowledgeRefreshKey:
    """Return the authored refresh key for one snapshot key."""
    return KnowledgeRefreshKey(
        base_id=key.base_id,
        storage_root=key.storage_root,
        knowledge_path=key.knowledge_path,
    )


def source_key_for_refresh_key(key: KnowledgeRefreshKey) -> KnowledgeSourceKey:
    """Return the physical source key shared by duplicate configured bases."""
    return KnowledgeSourceKey(
        storage_root=key.storage_root,
        knowledge_path=key.knowledge_path,
    )


def source_key_for_snapshot_key(key: KnowledgeSnapshotKey) -> KnowledgeSourceKey:
    """Return the physical source key for one snapshot key."""
    return KnowledgeSourceKey(
        storage_root=key.storage_root,
        knowledge_path=key.knowledge_path,
    )


def resolve_refresh_key(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> KnowledgeRefreshKey:
    """Resolve one base ID to its physical refresh/write key."""
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
    """Return the storage directory for one resolved snapshot key."""
    knowledge_path = Path(key.knowledge_path)
    return (
        Path(key.storage_root) / "knowledge_db" / manager_module._base_storage_key(key.base_id, knowledge_path)
    ).resolve()


def snapshot_metadata_path(key: KnowledgeSnapshotKey) -> Path:
    """Return the existing metadata file path for one resolved snapshot key."""
    return snapshot_base_storage_path(key) / "indexing_settings.json"


def snapshot_advisory_path(key: KnowledgeSnapshotKey) -> Path:
    """Return the advisory sidecar path for one resolved snapshot key."""
    return snapshot_base_storage_path(key) / "snapshot_advisory.json"


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _coerce_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _valid_advisory_state(value: object) -> bool:
    return value in {"none", "stale", "refreshing", "refresh_failed"}


def _valid_refresh_job(value: object) -> bool:
    return value in {"idle", "pending", "running", "failed"}


def load_snapshot_advisory_state(sidecar_path: Path) -> KnowledgeAdvisoryState:
    """Load advisory state; corrupt advisory data is treated as absent."""
    if not sidecar_path.exists():
        return KnowledgeAdvisoryState()
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return KnowledgeAdvisoryState()
    if not isinstance(payload, dict):
        return KnowledgeAdvisoryState()

    raw_state = payload.get("state")
    raw_refresh_job = payload.get("refresh_job")
    state = raw_state if _valid_advisory_state(raw_state) else "none"
    refresh_job = raw_refresh_job if _valid_refresh_job(raw_refresh_job) else "idle"
    reason = payload.get("reason")
    last_error = payload.get("last_error")
    updated_at = payload.get("updated_at")
    last_refresh_at = payload.get("last_refresh_at")
    return KnowledgeAdvisoryState(
        state=cast('Literal["none", "stale", "refreshing", "refresh_failed"]', state),
        refresh_job=cast('Literal["idle", "pending", "running", "failed"]', refresh_job),
        reason=reason if isinstance(reason, str) and reason else None,
        last_error=last_error if isinstance(last_error, str) and last_error else None,
        updated_at=updated_at if isinstance(updated_at, str) and updated_at else None,
        last_refresh_at=last_refresh_at if isinstance(last_refresh_at, str) and last_refresh_at else None,
    )


def save_snapshot_advisory_state(sidecar_path: Path, state: KnowledgeAdvisoryState) -> None:
    """Persist advisory state atomically without touching the published pointer."""
    payload: dict[str, object] = {
        "state": state.state,
        "refresh_job": state.refresh_job,
    }
    if state.reason is not None:
        payload["reason"] = state.reason
    if state.last_error is not None:
        payload["last_error"] = state.last_error
    if state.updated_at is not None:
        payload["updated_at"] = state.updated_at
    if state.last_refresh_at is not None:
        payload["last_refresh_at"] = state.last_refresh_at
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = sidecar_path.with_name(f".{sidecar_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp_path.replace(sidecar_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def load_knowledge_snapshot_state(key: KnowledgeSnapshotKey) -> KnowledgeSnapshotState:
    """Load published plus advisory state for one resolved snapshot key."""
    return KnowledgeSnapshotState(
        published=load_published_indexing_state(snapshot_metadata_path(key)),
        advisory=load_snapshot_advisory_state(snapshot_advisory_path(key)),
    )


def save_snapshot_dirty_state(
    key: KnowledgeSnapshotKey,
    *,
    reason: str,
    refresh_job: Literal["idle", "pending", "running", "failed"] = "pending",
) -> None:
    """Mark source/advisory dirty without mutating the published snapshot pointer."""
    save_snapshot_advisory_state(
        snapshot_advisory_path(key),
        KnowledgeAdvisoryState(
            state="stale",
            refresh_job=refresh_job,
            reason=reason,
            updated_at=_utc_now(),
        ),
    )


def save_snapshot_refreshing_state(key: KnowledgeSnapshotKey, *, reason: str = "refreshing") -> None:
    """Record that refresh work is currently building a private candidate."""
    existing = load_snapshot_advisory_state(snapshot_advisory_path(key))
    save_snapshot_advisory_state(
        snapshot_advisory_path(key),
        KnowledgeAdvisoryState(
            state="refreshing",
            refresh_job="running",
            reason=existing.reason or reason,
            last_error=existing.last_error,
            updated_at=_utc_now(),
            last_refresh_at=existing.last_refresh_at,
        ),
    )


def save_snapshot_refresh_failed_state(key: KnowledgeSnapshotKey, *, error: str) -> None:
    """Record a refresh failure while preserving any last-good published pointer."""
    save_snapshot_advisory_state(
        snapshot_advisory_path(key),
        KnowledgeAdvisoryState(
            state="refresh_failed",
            refresh_job="failed",
            last_error=error,
            updated_at=_utc_now(),
            last_refresh_at=_utc_now(),
        ),
    )


def save_snapshot_refresh_success_state(key: KnowledgeSnapshotKey) -> None:
    """Clear dirty/failure advisory state after a successful refresh."""
    save_snapshot_advisory_state(
        snapshot_advisory_path(key),
        KnowledgeAdvisoryState(
            state="none",
            refresh_job="idle",
            updated_at=_utc_now(),
            last_refresh_at=_utc_now(),
        ),
    )


def load_published_indexing_state(metadata_path: Path) -> PublishedIndexingState | None:
    """Load published snapshot metadata without constructing a manager."""
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    settings: tuple[str, ...] | None = None
    status: Literal["resetting", "indexing", "complete"] | None = None
    collection: str | None = None
    availability: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    last_error: str | None = None
    indexed_count: int | None = None
    source_signature: str | None = None
    retained_collections: tuple[str, ...] = ()
    if isinstance(payload, list):
        if all(isinstance(item, str) for item in payload):
            settings = tuple(payload)
            status = "complete"
    elif isinstance(payload, dict):
        raw_settings = payload.get("settings")
        raw_status = payload.get("status")
        if (
            isinstance(raw_settings, list)
            and all(isinstance(item, str) for item in raw_settings)
            and raw_status in {"resetting", "indexing", "complete"}
        ):
            settings = tuple(raw_settings)
            status = raw_status
            raw_collection = payload.get("collection")
            collection = raw_collection if isinstance(raw_collection, str) and raw_collection else None
            raw_availability = payload.get("availability")
            availability = raw_availability if isinstance(raw_availability, str) and raw_availability else None
            raw_last_published_at = payload.get("last_published_at")
            last_published_at = (
                raw_last_published_at if isinstance(raw_last_published_at, str) and raw_last_published_at else None
            )
            raw_published_revision = payload.get("published_revision")
            published_revision = (
                raw_published_revision if isinstance(raw_published_revision, str) and raw_published_revision else None
            )
            raw_last_error = payload.get("last_error")
            last_error = raw_last_error if isinstance(raw_last_error, str) and raw_last_error else None
            indexed_count = _coerce_nonnegative_int(payload.get("indexed_count"))
            raw_source_signature = payload.get("source_signature")
            source_signature = (
                raw_source_signature if isinstance(raw_source_signature, str) and raw_source_signature else None
            )
            raw_retained_collections = payload.get("retained_collections")
            if isinstance(raw_retained_collections, list) and all(
                isinstance(item, str) and item for item in raw_retained_collections
            ):
                retained_collections = tuple(dict.fromkeys(raw_retained_collections))

    if settings is None or status is None:
        return None
    return PublishedIndexingState(
        settings=settings,
        status=status,
        collection=collection,
        availability=availability,
        last_published_at=last_published_at,
        published_revision=published_revision,
        last_error=last_error,
        indexed_count=indexed_count,
        source_signature=source_signature,
        retained_collections=retained_collections,
    )


def save_published_indexing_state(metadata_path: Path, state: PublishedIndexingState) -> None:
    """Persist published snapshot metadata without constructing a manager."""
    payload: dict[str, object] = {
        "settings": list(state.settings),
        "status": state.status,
    }
    if state.collection is not None:
        payload["collection"] = state.collection
    if state.last_published_at is not None:
        payload["last_published_at"] = state.last_published_at
    if state.published_revision is not None:
        payload["published_revision"] = state.published_revision
    if state.indexed_count is not None:
        payload["indexed_count"] = state.indexed_count
    if state.source_signature is not None:
        payload["source_signature"] = state.source_signature
    if state.retained_collections:
        payload["retained_collections"] = list(state.retained_collections)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp_path.replace(metadata_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _default_collection_name(key: KnowledgeSnapshotKey) -> str:
    return manager_module._collection_name(key.base_id, Path(key.knowledge_path))


def _remember_snapshot_handle(snapshot: PublishedKnowledgeSnapshot) -> None:
    _published_snapshot_handles[id(snapshot)] = snapshot
    knowledge_id = id(snapshot.knowledge)
    _published_knowledge_leases[knowledge_id] = _PublishedKnowledgeLease(
        key=snapshot.key,
        collection=snapshot.state.collection or _default_collection_name(snapshot.key),
    )
    finalizer = _published_knowledge_finalizers.pop(knowledge_id, None)
    if finalizer is not None:
        finalizer.detach()
    try:
        _published_knowledge_finalizers[knowledge_id] = weakref.finalize(
            snapshot.knowledge,
            _forget_knowledge_handle,
            knowledge_id,
        )
    except TypeError:
        _published_knowledge_leases.pop(knowledge_id, None)
        logger.warning(
            "Knowledge object cannot be weak-referenced for snapshot collection protection",
            base_id=snapshot.key.base_id,
            collection=snapshot.state.collection,
        )


def _forget_knowledge_handle(knowledge_id: int) -> None:
    _published_knowledge_leases.pop(knowledge_id, None)
    _published_knowledge_finalizers.pop(knowledge_id, None)


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
    """Bound request-scoped snapshot bookkeeping without invalidating live handles."""
    private_snapshot_keys = [key for key in _published_snapshots if _snapshot_key_is_private(key)]
    for key in private_snapshot_keys[:-_MAX_PRIVATE_PUBLISHED_SNAPSHOTS]:
        _published_snapshots.pop(key, None)


def _evict_published_snapshots_for_refresh_key(refresh_key: KnowledgeRefreshKey) -> None:
    for cached_key in tuple(_published_snapshots):
        if _same_physical_binding(cached_key, refresh_key):
            _published_snapshots.pop(cached_key, None)


def active_snapshot_collection_names(refresh_key: KnowledgeRefreshKey) -> tuple[str, ...]:
    """Return collection names held by live published snapshot read handles."""
    names: list[str] = []
    for snapshot in list(_published_snapshot_handles.values()):
        if not _same_physical_binding(snapshot.key, refresh_key):
            continue
        names.append(snapshot.state.collection or _default_collection_name(snapshot.key))
    for lease in list(_published_knowledge_leases.values()):
        if not _same_physical_binding(lease.key, refresh_key):
            continue
        names.append(lease.collection)
    return tuple(dict.fromkeys(names))


def _build_snapshot_knowledge(
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Knowledge:
    vector_db = _build_snapshot_vector_db(key, state, config=config, runtime_paths=runtime_paths)
    return manager_module.Knowledge(vector_db=vector_db)


def _build_snapshot_vector_db(
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> _SnapshotVectorDb:
    collection_name = state.collection or _default_collection_name(key)
    return cast(
        "_SnapshotVectorDb",
        manager_module.ChromaDb(
            collection=collection_name,
            path=str(snapshot_base_storage_path(key)),
            persistent_client=True,
            embedder=manager_module._create_embedder(config, runtime_paths),
        ),
    )


def _snapshot_collection_exists(
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    _ = (config, runtime_paths)
    return snapshot_collection_exists_for_state(key, state)


def snapshot_collection_exists_for_state(key: KnowledgeSnapshotKey, state: PublishedIndexingState) -> bool:
    """Return whether persisted metadata still points at an existing collection."""
    if state.status != "complete":
        return False
    collection_name = state.collection or _default_collection_name(key)
    try:
        return manager_module.chroma_collection_exists(snapshot_base_storage_path(key), collection_name)
    except Exception:
        logger.warning(
            "Published knowledge snapshot collection existence check failed",
            base_id=key.base_id,
            collection=collection_name,
            exc_info=True,
        )
        return False


def indexing_settings_query_compatible(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether current queries can use a collection from published settings."""
    if (
        len(published_settings) < manager_module.INDEXING_SETTINGS_QUERY_COMPATIBLE_PREFIX_LENGTH
        or len(current_settings) < manager_module.INDEXING_SETTINGS_QUERY_COMPATIBLE_PREFIX_LENGTH
    ):
        return published_settings == current_settings
    return (
        published_settings[: manager_module.INDEXING_SETTINGS_QUERY_COMPATIBLE_PREFIX_LENGTH]
        == current_settings[: manager_module.INDEXING_SETTINGS_QUERY_COMPATIBLE_PREFIX_LENGTH]
    )


def indexing_settings_corpus_compatible(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether published content is safe for the current corpus config."""
    corpus_indexes = manager_module.INDEXING_SETTINGS_CORPUS_COMPATIBLE_INDEXES
    if len(published_settings) <= max(corpus_indexes) or len(current_settings) <= max(corpus_indexes):
        return published_settings == current_settings
    return all(
        _settings_values_compatible(published_settings[index], current_settings[index], index=index)
        for index in corpus_indexes
    )


def _settings_values_compatible(published_value: str, current_value: str, *, index: int) -> bool:
    if index != manager_module.INDEXING_SETTINGS_REPO_IDENTITY_INDEX:
        return published_value == current_value
    return _normalized_repo_identity_setting(published_value) == _normalized_repo_identity_setting(current_value)


def _normalized_repo_identity_setting(value: str) -> str:
    if not value or value.startswith("repo-url-sha256:"):
        return value
    return manager_module.credential_free_url_identity(value)


def indexing_settings_metadata_equal(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether persisted metadata exactly matches current indexing settings."""
    if len(published_settings) != len(current_settings):
        return False
    return all(
        _settings_values_compatible(published_value, current_value, index=index)
        for index, (published_value, current_value) in enumerate(zip(published_settings, current_settings, strict=True))
    )


def indexing_settings_snapshot_compatible(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether a snapshot can be queried under the current config."""
    return indexing_settings_query_compatible(
        published_settings,
        current_settings,
    ) and indexing_settings_corpus_compatible(published_settings, current_settings)


def _snapshot_state_queryable(key: KnowledgeSnapshotKey, state: PublishedIndexingState) -> bool:
    return state.status == "complete" and indexing_settings_snapshot_compatible(state.settings, key.indexing_settings)


def _load_queryable_snapshot_from_state(
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Knowledge | None:
    if not _snapshot_state_queryable(key, state):
        return None
    if not _snapshot_collection_exists(key, state, config=config, runtime_paths=runtime_paths):
        return None
    return _build_snapshot_knowledge(key, state, config=config, runtime_paths=runtime_paths)


def _cached_snapshot_still_queryable(snapshot: PublishedKnowledgeSnapshot) -> bool:
    if not _snapshot_state_queryable(snapshot.key, snapshot.state):
        return False
    vector_db = cast("_SnapshotVectorDb | None", snapshot.knowledge.vector_db)
    return vector_db is not None and vector_db.exists()


def _snapshot_availability(
    *,
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState | None,
    advisory: KnowledgeAdvisoryState,
) -> KnowledgeAvailability:
    availability = KnowledgeAvailability.READY
    if state is None:
        availability = (
            KnowledgeAvailability.REFRESH_FAILED
            if advisory.state == "refresh_failed"
            else KnowledgeAvailability.INITIALIZING
        )
    elif state.status == "complete" and (
        not indexing_settings_snapshot_compatible(state.settings, key.indexing_settings)
        or not indexing_settings_metadata_equal(state.settings, key.indexing_settings)
    ):
        availability = KnowledgeAvailability.CONFIG_MISMATCH
    elif state.status != "complete":
        availability = (
            KnowledgeAvailability.REFRESH_FAILED
            if advisory.state == "refresh_failed"
            else KnowledgeAvailability.INITIALIZING
        )
    elif advisory.state in {"stale", "refreshing"}:
        availability = KnowledgeAvailability.STALE
    elif advisory.state == "refresh_failed":
        availability = KnowledgeAvailability.REFRESH_FAILED
    return availability


def snapshot_availability_for_state(
    *,
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState | None,
    advisory: KnowledgeAdvisoryState | None = None,
) -> KnowledgeAvailability:
    """Return the public availability value for persisted snapshot state."""
    return _snapshot_availability(key=key, state=state, advisory=advisory or KnowledgeAdvisoryState())


def get_published_snapshot(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> KnowledgeSnapshotLookup:
    """Return the last-good published snapshot without running lifecycle work."""
    key, binding = _resolve_snapshot_key_and_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    try:
        advisory = load_snapshot_advisory_state(snapshot_advisory_path(key))
        snapshot = _published_snapshots.get(key)
        if snapshot is not None:
            if not _cached_snapshot_still_queryable(snapshot):
                _published_snapshots.pop(key, None)
                availability = _snapshot_availability(key=key, state=snapshot.state, advisory=advisory)
                if availability is KnowledgeAvailability.READY:
                    availability = KnowledgeAvailability.REFRESH_FAILED
                return KnowledgeSnapshotLookup(
                    key=key,
                    snapshot=None,
                    availability=availability,
                    advisory=advisory,
                    refresh_on_access=binding.incremental_sync_on_access,
                )
            return KnowledgeSnapshotLookup(
                key=key,
                snapshot=snapshot,
                availability=_snapshot_availability(key=key, state=snapshot.state, advisory=advisory),
                advisory=advisory,
                refresh_on_access=binding.incremental_sync_on_access,
            )

        metadata_path = snapshot_metadata_path(key)
        state = load_published_indexing_state(metadata_path)
        availability = _snapshot_availability(key=key, state=state, advisory=advisory)
        if state is None or state.status != "complete":
            return KnowledgeSnapshotLookup(
                key=key,
                snapshot=None,
                availability=availability,
                advisory=advisory,
                refresh_on_access=binding.incremental_sync_on_access,
            )

        knowledge = _load_queryable_snapshot_from_state(key, state, config=config, runtime_paths=runtime_paths)
        if knowledge is None:
            if availability is KnowledgeAvailability.READY:
                availability = KnowledgeAvailability.REFRESH_FAILED
            return KnowledgeSnapshotLookup(
                key=key,
                snapshot=None,
                availability=availability,
                advisory=advisory,
                refresh_on_access=binding.incremental_sync_on_access,
            )

        snapshot = PublishedKnowledgeSnapshot(
            key=key,
            knowledge=knowledge,
            state=state,
            metadata_path=metadata_path,
        )
        _published_snapshots[key] = snapshot
        _remember_snapshot_handle(snapshot)
        return KnowledgeSnapshotLookup(
            key=key,
            snapshot=snapshot,
            availability=availability,
            advisory=advisory,
            refresh_on_access=binding.incremental_sync_on_access,
        )
    except Exception:
        logger.exception("Published knowledge snapshot lookup failed", base_id=base_id, key=key)
        _published_snapshots.pop(key, None)
        return KnowledgeSnapshotLookup(
            key=key,
            snapshot=None,
            availability=KnowledgeAvailability.REFRESH_FAILED,
            advisory=KnowledgeAdvisoryState(state="refresh_failed", refresh_job="failed"),
            refresh_on_access=binding.incremental_sync_on_access,
        )


def publish_snapshot(
    key: KnowledgeSnapshotKey,
    *,
    knowledge: Knowledge,
    state: PublishedIndexingState,
    metadata_path: Path | None = None,
) -> PublishedKnowledgeSnapshot:
    """Publish a completed read handle into the process-local registry."""
    _evict_published_snapshots_for_refresh_key(refresh_key_for_snapshot_key(key))
    snapshot = PublishedKnowledgeSnapshot(
        key=key,
        knowledge=knowledge,
        state=state,
        metadata_path=metadata_path or snapshot_metadata_path(key),
    )
    _published_snapshots[key] = snapshot
    _remember_snapshot_handle(snapshot)
    prune_private_snapshot_bookkeeping()
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
    return publish_snapshot(
        key,
        knowledge=knowledge,
        state=state,
        metadata_path=metadata_path,
    )


def snapshot_indexed_count(snapshot: PublishedKnowledgeSnapshot) -> int:
    """Return the persisted number of distinct indexed source files."""
    return snapshot.state.indexed_count or 0


def _state_collection_names(key: KnowledgeSnapshotKey, state: PublishedIndexingState) -> tuple[str, ...]:
    collections = [
        collection
        for collection in (state.collection or _default_collection_name(key), *state.retained_collections)
        if collection
    ]
    return tuple(dict.fromkeys(collections))


def _snapshot_keys_for_shared_source(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> tuple[KnowledgeSnapshotKey, ...]:
    """Return snapshot keys reading the same physical source as ``base_id``."""
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
    return True


def mark_snapshot_dirty(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
) -> tuple[str, ...]:
    """Mark advisory state dirty after a direct source mutation.

    Callers that mutate files must hold ``knowledge_binding_mutation_lock`` so
    advisory writes stay serialized with refresh publishes for the same binding.
    Returns the configured base IDs sharing the mutated physical source.
    """
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
    """Async variant for request handlers that keeps advisory I/O off the event loop.

    The caller still holds ``knowledge_binding_mutation_lock`` while this runs.
    If cancellation arrives while the worker may still commit, this waits for
    every same-source advisory write before propagating cancellation.
    """
    write_task = asyncio.create_task(
        asyncio.to_thread(
            mark_snapshot_dirty,
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            reason=reason,
        ),
    )
    try:
        return await asyncio.shield(write_task)
    except asyncio.CancelledError:
        try:
            await write_task
        except Exception:
            logger.warning(
                "Knowledge advisory dirty marker write failed after cancellation",
                base_id=base_id,
                exc_info=True,
            )
        raise


def clear_published_snapshots() -> None:
    """Clear all process-local snapshot read handles."""
    _published_snapshots.clear()
    _published_snapshot_handles.clear()
    for finalizer in _published_knowledge_finalizers.values():
        finalizer.detach()
    _published_knowledge_finalizers.clear()
    _published_knowledge_leases.clear()
