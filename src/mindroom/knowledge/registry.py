"""Internal published knowledge collection registry.

Code outside ``mindroom.knowledge`` should use package facades such as
``mindroom.knowledge.status`` or ``mindroom.knowledge.utils`` instead of
importing this module directly.
"""

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
# - KnowledgeSourceRoot: one physical source root. It gates source mutation locks and alias fanout.
# - KnowledgeRefreshTarget: one refresh target. It coalesces background work for a source and base ID.
# - PublishedIndexKey: one published, query-compatible index. It includes indexing settings for read paths.


@dataclass(frozen=True)
class PublishedIndexKey:
    """Stable key for one configured knowledge binding."""

    base_id: str
    storage_root: str
    knowledge_path: str
    indexing_settings: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeRefreshTarget:
    """Stable key for refresh work for one physical knowledge binding."""

    base_id: str
    storage_root: str
    knowledge_path: str


@dataclass(frozen=True)
class KnowledgeSourceRoot:
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
class PublishedKnowledgeIndex:
    """Read handle for the active published knowledge collection."""

    key: PublishedIndexKey
    knowledge: Knowledge
    state: PublishedIndexingState
    metadata_path: Path


@dataclass(frozen=True)
class KnowledgeIndexLookup:
    """Result of resolving the active collection for one knowledge base."""

    key: PublishedIndexKey
    index: PublishedKnowledgeIndex | None
    state: PublishedIndexingState | None
    availability: KnowledgeAvailability
    schedule_refresh_on_access: bool = False


class _PublishedIndexVectorDb(Protocol):
    client: object | None
    collection_name: str

    def exists(self) -> bool:
        """Return whether the collection exists."""
        ...


_published_indexes: dict[PublishedIndexKey, PublishedKnowledgeIndex] = {}
_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX = "__agent_private__:"
_MAX_PRIVATE_PUBLISHED_INDEXES = 128
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


def _published_index_key_from_binding(
    base_id: str,
    binding: ResolvedKnowledgeBinding,
    *,
    config: Config,
) -> PublishedIndexKey:
    storage_root = binding.storage_root.expanduser().resolve()
    knowledge_path = binding.knowledge_path.resolve()
    return PublishedIndexKey(
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


def _resolve_published_index_key_and_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> tuple[PublishedIndexKey, ResolvedKnowledgeBinding]:
    binding = resolve_knowledge_binding(
        base_id,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        start_watchers=False,
        create=create,
    )
    return _published_index_key_from_binding(base_id, binding, config=config), binding


def resolve_published_index_key(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> PublishedIndexKey:
    """Resolve one base ID to its current collection metadata key."""
    key, _binding = _resolve_published_index_key_and_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    return key


def refresh_target_for_published_index_key(key: PublishedIndexKey) -> KnowledgeRefreshTarget:
    """Return the refresh target for one published index key."""
    return KnowledgeRefreshTarget(
        base_id=key.base_id,
        storage_root=key.storage_root,
        knowledge_path=key.knowledge_path,
    )


def source_root_for_refresh_target(key: KnowledgeRefreshTarget) -> KnowledgeSourceRoot:
    """Return the physical source root for one refresh target."""
    return KnowledgeSourceRoot(storage_root=key.storage_root, knowledge_path=key.knowledge_path)


def source_root_for_published_index_key(key: PublishedIndexKey) -> KnowledgeSourceRoot:
    """Return the physical source root for one published index key."""
    return KnowledgeSourceRoot(storage_root=key.storage_root, knowledge_path=key.knowledge_path)


def resolve_refresh_target(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> KnowledgeRefreshTarget:
    """Resolve one base ID to its refresh target."""
    return refresh_target_for_published_index_key(
        resolve_published_index_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            create=create,
        ),
    )


def published_index_storage_path(key: PublishedIndexKey) -> Path:
    """Return the storage directory for one resolved knowledge base."""
    knowledge_path = Path(key.knowledge_path)
    return (
        Path(key.storage_root) / "knowledge_db" / manager_module._base_storage_key(key.base_id, knowledge_path)
    ).resolve()


def published_index_metadata_path(key: PublishedIndexKey) -> Path:
    """Return the single persisted state file for one knowledge base."""
    return published_index_storage_path(key) / "indexing_settings.json"


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


def published_index_refresh_state(
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
    key: PublishedIndexKey,
    *,
    refresh_job: Literal["idle", "pending", "running", "failed"],
    status_when_missing: Literal["indexing", "failed"],
    reason: str | None = None,
    last_error: str | None = None,
    clear_error: bool = False,
) -> PublishedIndexingState:
    current = load_published_indexing_state(published_index_metadata_path(key))
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


def mark_published_index_stale(
    key: PublishedIndexKey,
    *,
    reason: str,
    refresh_job: Literal["idle", "pending", "running", "failed"] = "pending",
) -> None:
    """Mark the active collection stale without changing the published pointer."""
    save_published_indexing_state(
        published_index_metadata_path(key),
        _state_with_refresh_fields(
            key,
            refresh_job=refresh_job,
            status_when_missing="indexing",
            reason=reason,
        ),
    )


def mark_published_index_refresh_running(key: PublishedIndexKey, *, reason: str = "refreshing") -> None:
    """Mark refresh work running while keeping the old active collection readable."""
    save_published_indexing_state(
        published_index_metadata_path(key),
        _state_with_refresh_fields(
            key,
            refresh_job="running",
            status_when_missing="indexing",
            reason=reason,
        ),
    )


def mark_published_index_refresh_failed_preserving_last_good(key: PublishedIndexKey, *, error: str) -> None:
    """Record refresh failure while keeping any old active collection pointer."""
    current = load_published_indexing_state(published_index_metadata_path(key))
    state = _state_with_refresh_fields(
        key,
        refresh_job="failed",
        status_when_missing="failed",
        reason="refresh_failed",
        last_error=error,
    )
    if current is not None and current.status == "complete":
        state = replace(state, status="complete")
    save_published_indexing_state(published_index_metadata_path(key), state)


def mark_published_index_refresh_succeeded(key: PublishedIndexKey) -> None:
    """Clear refresh status after a successful publish."""
    state = load_published_indexing_state(published_index_metadata_path(key))
    if state is None:
        return
    save_published_indexing_state(
        published_index_metadata_path(key),
        replace(
            state,
            refresh_job="idle",
            reason=None,
            last_error=None,
            updated_at=_utc_now(),
            last_refresh_at=_utc_now(),
        ),
    )


def _state_collection_name(key: PublishedIndexKey, state: PublishedIndexingState) -> str:
    _ = key
    if state.collection is None:
        msg = "Published knowledge metadata is missing a collection name"
        raise ValueError(msg)
    return state.collection


def _build_published_index_vector_db(
    key: PublishedIndexKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> _PublishedIndexVectorDb:
    return cast(
        "_PublishedIndexVectorDb",
        manager_module.ChromaDb(
            collection=_state_collection_name(key, state),
            path=str(published_index_storage_path(key)),
            persistent_client=True,
            embedder=manager_module._create_embedder(config, runtime_paths),
        ),
    )


def _build_published_index_knowledge(
    key: PublishedIndexKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Knowledge:
    return manager_module.Knowledge(
        vector_db=_build_published_index_vector_db(key, state, config=config, runtime_paths=runtime_paths),
    )


def published_index_collection_exists_for_state(key: PublishedIndexKey, state: PublishedIndexingState) -> bool:
    """Return whether persisted metadata points at an existing active collection."""
    if state.status != "complete" or state.collection is None:
        return False
    try:
        return manager_module.chroma_collection_exists(published_index_storage_path(key), state.collection)
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


def published_index_settings_compatible(
    published_settings: tuple[str, ...],
    current_settings: tuple[str, ...],
) -> bool:
    """Return whether a published collection can be queried under the current config."""
    return indexing_settings_query_compatible(
        published_settings,
        current_settings,
    ) and indexing_settings_corpus_compatible(published_settings, current_settings)


def _published_index_state_queryable(key: PublishedIndexKey, state: PublishedIndexingState) -> bool:
    return (
        state.status == "complete"
        and state.collection is not None
        and published_index_settings_compatible(state.settings, key.indexing_settings)
    )


def _published_index_availability(
    *,
    key: PublishedIndexKey,
    state: PublishedIndexingState | None,
    metadata_exists: bool = False,
) -> KnowledgeAvailability:
    refresh_state = published_index_refresh_state(state, metadata_exists=metadata_exists)
    if state is None:
        availability = (
            KnowledgeAvailability.REFRESH_FAILED
            if refresh_state == "refresh_failed"
            else KnowledgeAvailability.INITIALIZING
        )
    elif state.collection is None and refresh_state == "refresh_failed":
        availability = KnowledgeAvailability.REFRESH_FAILED
    elif not published_index_settings_compatible(
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


def published_index_availability_for_state(
    *,
    key: PublishedIndexKey,
    state: PublishedIndexingState | None,
    metadata_exists: bool = False,
) -> KnowledgeAvailability:
    """Return the public availability value for active collection state."""
    return _published_index_availability(
        key=key,
        state=state,
        metadata_exists=metadata_exists,
    )


def _cached_index_still_queryable(index: PublishedKnowledgeIndex) -> bool:
    if not _published_index_state_queryable(index.key, index.state):
        return False
    vector_db = cast("_PublishedIndexVectorDb | None", index.knowledge.vector_db)
    return vector_db is not None and vector_db.exists()


def _load_queryable_index_from_state(
    key: PublishedIndexKey,
    state: PublishedIndexingState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Knowledge | None:
    if not _published_index_state_queryable(key, state):
        return None
    if not published_index_collection_exists_for_state(key, state):
        return None
    return _build_published_index_knowledge(key, state, config=config, runtime_paths=runtime_paths)


def get_published_index(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> KnowledgeIndexLookup:
    """Return the currently active collection, if one is usable."""
    key, binding = _resolve_published_index_key_and_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    metadata_path = published_index_metadata_path(key)
    state = load_published_indexing_state(metadata_path)
    availability = _published_index_availability(key=key, state=state, metadata_exists=metadata_path.exists())

    index = _published_indexes.get(key)
    if index is not None:
        if _cached_index_still_queryable(index):
            return KnowledgeIndexLookup(
                key=key,
                index=index,
                state=state,
                availability=availability,
                schedule_refresh_on_access=binding.incremental_sync_on_access,
            )
        _published_indexes.pop(key, None)

    if state is None:
        return KnowledgeIndexLookup(
            key=key,
            index=None,
            state=state,
            availability=availability,
            schedule_refresh_on_access=binding.incremental_sync_on_access,
        )

    knowledge = _load_queryable_index_from_state(key, state, config=config, runtime_paths=runtime_paths)
    if knowledge is None:
        return KnowledgeIndexLookup(
            key=key,
            index=None,
            state=state,
            availability=availability
            if availability is not KnowledgeAvailability.READY
            else KnowledgeAvailability.REFRESH_FAILED,
            schedule_refresh_on_access=binding.incremental_sync_on_access,
        )

    index = PublishedKnowledgeIndex(
        key=key,
        knowledge=knowledge,
        state=state,
        metadata_path=published_index_metadata_path(key),
    )
    _cache_published_index(index)
    return KnowledgeIndexLookup(
        key=key,
        index=index,
        state=state,
        availability=availability,
        schedule_refresh_on_access=binding.incremental_sync_on_access,
    )


def publish_knowledge_index(
    key: PublishedIndexKey,
    *,
    knowledge: Knowledge,
    state: PublishedIndexingState,
    metadata_path: Path | None = None,
) -> PublishedKnowledgeIndex:
    """Publish a read handle in this process."""
    _evict_published_indexes_for_refresh_target(refresh_target_for_published_index_key(key))
    index = PublishedKnowledgeIndex(
        key=key,
        knowledge=knowledge,
        state=state,
        metadata_path=metadata_path or published_index_metadata_path(key),
    )
    _cache_published_index(index)
    return index


def publish_knowledge_index_from_state(
    key: PublishedIndexKey,
    *,
    state: PublishedIndexingState,
    config: Config,
    runtime_paths: RuntimePaths,
    metadata_path: Path | None = None,
) -> PublishedKnowledgeIndex | None:
    """Publish a read handle rebuilt from persisted metadata."""
    knowledge = _load_queryable_index_from_state(key, state, config=config, runtime_paths=runtime_paths)
    if knowledge is None:
        return None
    return publish_knowledge_index(key, knowledge=knowledge, state=state, metadata_path=metadata_path)


def published_indexed_count(index: PublishedKnowledgeIndex) -> int:
    """Return the persisted indexed source file count."""
    return index.state.indexed_count or 0


def _same_physical_binding(key: PublishedIndexKey, refresh_key: KnowledgeRefreshTarget) -> bool:
    return (
        key.base_id == refresh_key.base_id
        and key.storage_root == refresh_key.storage_root
        and key.knowledge_path == refresh_key.knowledge_path
    )


def _same_physical_source(left: PublishedIndexKey, right: PublishedIndexKey) -> bool:
    return left.storage_root == right.storage_root and left.knowledge_path == right.knowledge_path


def _published_index_key_is_private(key: PublishedIndexKey) -> bool:
    return key.base_id.startswith(_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX)


def prune_private_index_bookkeeping() -> None:
    """Bound PrivateAgentKnowledge in-process published index handles."""
    private_index_keys = [key for key in _published_indexes if _published_index_key_is_private(key)]
    for key in private_index_keys[:-_MAX_PRIVATE_PUBLISHED_INDEXES]:
        _published_indexes.pop(key, None)


def _cache_published_index(index: PublishedKnowledgeIndex) -> None:
    _published_indexes[index.key] = index
    prune_private_index_bookkeeping()


def _evict_published_indexes_for_refresh_target(refresh_key: KnowledgeRefreshTarget) -> None:
    for cached_key in tuple(_published_indexes):
        if _same_physical_binding(cached_key, refresh_key):
            _published_indexes.pop(cached_key, None)


def _published_index_keys_for_shared_source(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> tuple[PublishedIndexKey, ...]:
    key = resolve_published_index_key(
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
            candidate_key = resolve_published_index_key(
                candidate_base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
        except Exception:
            logger.warning(
                "Could not resolve related published knowledge index while marking dirty",
                base_id=base_id,
                related_base_id=candidate_base_id,
                exc_info=True,
            )
            continue
        if _same_physical_source(candidate_key, key):
            matching_keys.append(candidate_key)
    return tuple(matching_keys)


def _mark_published_index_key_stale_on_disk(matching_key: PublishedIndexKey, *, reason: str) -> bool:
    mark_published_index_stale(matching_key, reason=reason)
    _evict_published_indexes_for_refresh_target(refresh_target_for_published_index_key(matching_key))
    return True


def mark_source_dirty(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
) -> tuple[str, ...]:
    """Mark same-source published indexes stale after a source mutation."""
    matching_keys = _published_index_keys_for_shared_source(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    for matching_key in matching_keys:
        _mark_published_index_key_stale_on_disk(matching_key, reason=reason)
    return tuple(dict.fromkeys(key.base_id for key in matching_keys))


async def mark_source_dirty_async(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
) -> tuple[str, ...]:
    """Async stale marker that keeps metadata I/O off the event loop."""
    return await _run_to_thread_to_completion_on_cancel(
        mark_source_dirty,
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        reason=reason,
    )


def clear_published_indexes() -> None:
    """Clear process-local read handles."""
    _published_indexes.clear()
