"""Published knowledge snapshot lookup for request-safe RAG access."""

from __future__ import annotations

import json
from dataclasses import dataclass
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
_QUERY_COMPATIBLE_SETTINGS_PREFIX_LENGTH = 7


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
    )


def _default_collection_name(key: KnowledgeSnapshotKey) -> str:
    return manager_module._collection_name(key.base_id, Path(key.knowledge_path))


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


def _snapshot_state_queryable(key: KnowledgeSnapshotKey, state: PublishedIndexingState) -> bool:
    return state.status == "complete" and indexing_settings_query_compatible(state.settings, key.indexing_settings)


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
    if state is None or state.status != "complete":
        return KnowledgeAvailability.INITIALIZING
    if state.settings != key.indexing_settings:
        return KnowledgeAvailability.CONFIG_MISMATCH
    if state.availability == KnowledgeAvailability.REFRESH_FAILED.value:
        return KnowledgeAvailability.REFRESH_FAILED
    if state.availability in {None, "", KnowledgeAvailability.READY.value}:
        return KnowledgeAvailability.READY
    return KnowledgeAvailability.INITIALIZING


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
    return KnowledgeSnapshotLookup(key=key, snapshot=snapshot, availability=availability)


def publish_snapshot(
    key: KnowledgeSnapshotKey,
    *,
    knowledge: Knowledge,
    state: PublishedIndexingState,
    metadata_path: Path | None = None,
) -> PublishedKnowledgeSnapshot:
    """Publish a completed read handle into the process-local registry."""
    snapshot = PublishedKnowledgeSnapshot(
        key=key,
        knowledge=knowledge,
        state=state,
        metadata_path=metadata_path or snapshot_metadata_path(key),
    )
    _published_snapshots[key] = snapshot
    return snapshot


def snapshot_indexed_count(snapshot: PublishedKnowledgeSnapshot) -> int:
    """Return the number of distinct indexed source files in a snapshot."""
    vector_db = cast("_SnapshotVectorDb | None", snapshot.knowledge.vector_db)
    if vector_db is None or not vector_db.exists():
        return 0
    if vector_db.client is None:
        return 0
    collection = vector_db.client.get_collection(name=vector_db.collection_name)
    indexed_files: set[str] = set()
    offset = 0
    batch_size = 1_000
    while True:
        result = collection.get(limit=batch_size, offset=offset, include=["metadatas"])
        raw_metadatas = result.get("metadatas")
        metadatas = raw_metadatas if isinstance(raw_metadatas, list) else []
        for metadata in metadatas:
            if not isinstance(metadata, dict):
                continue
            metadata_values = cast("dict[str, object]", metadata)
            source_path = metadata_values.get(manager_module._SOURCE_PATH_KEY)
            if isinstance(source_path, str) and source_path:
                indexed_files.add(source_path)
        raw_ids = result.get("ids")
        ids = raw_ids if isinstance(raw_ids, list) else []
        fetched_count = len(ids)
        if fetched_count == 0:
            break
        offset += fetched_count
    return len(indexed_files)


def clear_published_snapshots() -> None:
    """Clear all process-local snapshot read handles."""
    _published_snapshots.clear()
