"""Published knowledge snapshot lookup for request-safe RAG access."""

from __future__ import annotations

import json
import weakref
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

import mindroom.knowledge.manager as manager_module
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.runtime_resolution import resolve_knowledge_binding

if TYPE_CHECKING:
    from agno.knowledge.knowledge import Knowledge

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class KnowledgeSnapshotKey:
    """Stable cache key for one resolved knowledge binding and indexing shape."""

    base_id: str
    storage_root: str
    knowledge_path: str
    indexing_settings: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeRefreshKey:
    """Stable key for the physical refresh/write target."""

    base_id: str
    storage_root: str
    knowledge_path: str


@dataclass(frozen=True)
class PublishedIndexingState:
    """Metadata for the currently published collection on disk."""

    settings: tuple[str, ...]
    status: Literal["resetting", "indexing", "complete"]
    collection: str | None = None
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
class KnowledgeSnapshotLookup:
    """Result of a request-safe snapshot lookup."""

    key: KnowledgeSnapshotKey
    snapshot: PublishedKnowledgeSnapshot | None
    availability: KnowledgeAvailability


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
_stale_ready_snapshots: set[tuple[KnowledgeRefreshKey, str | None]] = set()
_QUERY_COMPATIBLE_SETTINGS_PREFIX_LENGTH = 7
_CORPUS_COMPATIBLE_SETTINGS_INDEXES = (0, 1, 2, 9, 10, 11, 12, 13, 14, 15, 16)
_REPO_IDENTITY_SETTINGS_INDEX = 9


def resolve_snapshot_key(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> KnowledgeSnapshotKey:
    """Resolve one base ID to the process-local snapshot key."""
    binding = resolve_knowledge_binding(
        base_id,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        start_watchers=False,
        create=create,
    )
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


def refresh_key_for_snapshot_key(key: KnowledgeSnapshotKey) -> KnowledgeRefreshKey:
    """Return the physical refresh key for one snapshot key."""
    return KnowledgeRefreshKey(
        base_id=key.base_id,
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
    if state.availability is not None:
        payload["availability"] = state.availability
    if state.last_published_at is not None:
        payload["last_published_at"] = state.last_published_at
    if state.published_revision is not None:
        payload["published_revision"] = state.published_revision
    if state.last_error is not None:
        payload["last_error"] = state.last_error
    if state.indexed_count is not None:
        payload["indexed_count"] = state.indexed_count
    if state.source_signature is not None:
        payload["source_signature"] = state.source_signature
    if state.retained_collections:
        payload["retained_collections"] = list(state.retained_collections)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = metadata_path.with_suffix(f"{metadata_path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp_path.replace(metadata_path)


def _default_collection_name(key: KnowledgeSnapshotKey) -> str:
    return manager_module._collection_name(key.base_id, Path(key.knowledge_path))


def _remember_snapshot_handle(snapshot: PublishedKnowledgeSnapshot) -> None:
    _published_snapshot_handles[id(snapshot)] = snapshot


def _same_physical_binding(key: KnowledgeSnapshotKey, refresh_key: KnowledgeRefreshKey) -> bool:
    return (
        key.base_id == refresh_key.base_id
        and key.storage_root == refresh_key.storage_root
        and key.knowledge_path == refresh_key.knowledge_path
    )


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
    return tuple(dict.fromkeys(names))


def mark_ready_snapshot_stale(refresh_key: KnowledgeRefreshKey, source_signature: str | None) -> None:
    """Record a cheap stale marker for one READY snapshot source signature."""
    _stale_ready_snapshots.add((refresh_key, source_signature))


def ready_snapshot_marked_stale(refresh_key: KnowledgeRefreshKey, source_signature: str | None) -> bool:
    """Return whether a READY snapshot source signature has a pending stale marker."""
    return (refresh_key, source_signature) in _stale_ready_snapshots


def clear_stale_ready_snapshot_markers(refresh_key: KnowledgeRefreshKey) -> None:
    """Clear stale READY markers after a successful publish for one binding."""
    stale_keys = {stale_key for stale_key in _stale_ready_snapshots if stale_key[0] == refresh_key}
    _stale_ready_snapshots.difference_update(stale_keys)


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
    vector_db = _build_snapshot_vector_db(key, state, config=config, runtime_paths=runtime_paths)
    return vector_db.exists()


def indexing_settings_query_compatible(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether current queries can use a collection from published settings."""
    if (
        len(published_settings) < _QUERY_COMPATIBLE_SETTINGS_PREFIX_LENGTH
        or len(current_settings) < _QUERY_COMPATIBLE_SETTINGS_PREFIX_LENGTH
    ):
        return published_settings == current_settings
    return (
        published_settings[:_QUERY_COMPATIBLE_SETTINGS_PREFIX_LENGTH]
        == current_settings[:_QUERY_COMPATIBLE_SETTINGS_PREFIX_LENGTH]
    )


def indexing_settings_corpus_compatible(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether published content is safe for the current corpus config."""
    if len(published_settings) <= max(_CORPUS_COMPATIBLE_SETTINGS_INDEXES) or len(current_settings) <= max(
        _CORPUS_COMPATIBLE_SETTINGS_INDEXES,
    ):
        return published_settings == current_settings
    return all(
        _settings_values_compatible(published_settings[index], current_settings[index], index=index)
        for index in _CORPUS_COMPATIBLE_SETTINGS_INDEXES
    )


def _settings_values_compatible(published_value: str, current_value: str, *, index: int) -> bool:
    if index != _REPO_IDENTITY_SETTINGS_INDEX:
        return published_value == current_value
    return _normalized_repo_identity_setting(published_value) == _normalized_repo_identity_setting(current_value)


def _normalized_repo_identity_setting(value: str) -> str:
    if not value or value.startswith("repo-url-sha256:"):
        return value
    return manager_module.credential_free_url_identity(value)


def _metadata_settings_equal(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
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
) -> KnowledgeAvailability:
    if state is None:
        return KnowledgeAvailability.INITIALIZING

    availability = KnowledgeAvailability.INITIALIZING
    if state.status == "complete" and not indexing_settings_snapshot_compatible(state.settings, key.indexing_settings):
        availability = KnowledgeAvailability.CONFIG_MISMATCH
    elif state.availability == KnowledgeAvailability.STALE.value:
        availability = KnowledgeAvailability.STALE
    elif state.availability == KnowledgeAvailability.REFRESH_FAILED.value:
        availability = KnowledgeAvailability.REFRESH_FAILED
    elif state.status != "complete":
        availability = KnowledgeAvailability.INITIALIZING
    elif not _metadata_settings_equal(state.settings, key.indexing_settings):
        availability = KnowledgeAvailability.CONFIG_MISMATCH
    elif state.availability in {None, "", KnowledgeAvailability.READY.value}:
        availability = KnowledgeAvailability.READY
    return availability


def snapshot_availability_for_state(
    *,
    key: KnowledgeSnapshotKey,
    state: PublishedIndexingState | None,
) -> KnowledgeAvailability:
    """Return the public availability value for persisted snapshot state."""
    return _snapshot_availability(key=key, state=state)


def get_published_snapshot(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> KnowledgeSnapshotLookup:
    """Return the last-good published snapshot without running lifecycle work."""
    key = resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    try:
        snapshot = _published_snapshots.get(key)
        if snapshot is not None:
            if not _cached_snapshot_still_queryable(snapshot):
                _published_snapshots.pop(key, None)
                availability = _snapshot_availability(key=key, state=snapshot.state)
                if availability is KnowledgeAvailability.READY:
                    availability = KnowledgeAvailability.REFRESH_FAILED
                return KnowledgeSnapshotLookup(
                    key=key,
                    snapshot=None,
                    availability=availability,
                )
            return KnowledgeSnapshotLookup(
                key=key,
                snapshot=snapshot,
                availability=_snapshot_availability(key=key, state=snapshot.state),
            )

        metadata_path = snapshot_metadata_path(key)
        state = load_published_indexing_state(metadata_path)
        availability = _snapshot_availability(key=key, state=state)
        if state is None or state.status != "complete":
            return KnowledgeSnapshotLookup(key=key, snapshot=None, availability=availability)

        knowledge = _load_queryable_snapshot_from_state(key, state, config=config, runtime_paths=runtime_paths)
        if knowledge is None:
            if availability is KnowledgeAvailability.READY:
                availability = KnowledgeAvailability.REFRESH_FAILED
            return KnowledgeSnapshotLookup(key=key, snapshot=None, availability=availability)

        snapshot = PublishedKnowledgeSnapshot(
            key=key,
            knowledge=knowledge,
            state=state,
            metadata_path=metadata_path,
        )
        _published_snapshots[key] = snapshot
        _remember_snapshot_handle(snapshot)
        return KnowledgeSnapshotLookup(key=key, snapshot=snapshot, availability=availability)
    except Exception:
        _published_snapshots.pop(key, None)
        return KnowledgeSnapshotLookup(
            key=key,
            snapshot=None,
            availability=KnowledgeAvailability.REFRESH_FAILED,
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
    return snapshot


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


def mark_published_snapshot_stale(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> bool:
    """Mark the current published metadata stale after a direct source mutation."""
    key = resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    metadata_path = snapshot_metadata_path(key)
    state = load_published_indexing_state(metadata_path)
    if state is None:
        return False

    updated_state = replace(
        state,
        availability=KnowledgeAvailability.STALE.value,
        source_signature=None,
    )
    save_published_indexing_state(metadata_path, updated_state)
    _evict_published_snapshots_for_refresh_key(refresh_key_for_snapshot_key(key))
    return True


def remove_source_path_from_published_snapshots(
    base_id: str,
    relative_path: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> bool:
    """Remove one source path from all retained published collections for a binding."""
    key = resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    metadata_path = snapshot_metadata_path(key)
    state = load_published_indexing_state(metadata_path)
    if state is None:
        return False

    removed = False
    for collection_name in _state_collection_names(key, state):
        vector_db = _build_snapshot_vector_db(
            key,
            replace(state, collection=collection_name),
            config=config,
            runtime_paths=runtime_paths,
        )
        if not vector_db.exists():
            continue
        knowledge = manager_module.Knowledge(vector_db=vector_db)
        removed = (
            bool(knowledge.remove_vectors_by_metadata({manager_module._SOURCE_PATH_KEY: relative_path})) or removed
        )

    updated_state = replace(
        state,
        availability=KnowledgeAvailability.STALE.value,
        indexed_count=(
            max((state.indexed_count or 1) - 1, 0)
            if removed and state.indexed_count is not None
            else state.indexed_count
        ),
        source_signature=None,
    )
    save_published_indexing_state(metadata_path, updated_state)
    _evict_published_snapshots_for_refresh_key(refresh_key_for_snapshot_key(key))
    return removed


def clear_published_snapshots() -> None:
    """Clear all process-local snapshot read handles."""
    _published_snapshots.clear()
    _published_snapshot_handles.clear()
    _stale_ready_snapshots.clear()
