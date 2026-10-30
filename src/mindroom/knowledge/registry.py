"""Internal published knowledge index registry.

Code outside ``mindroom.knowledge`` should use package facades such as
``mindroom.knowledge.status`` or ``mindroom.knowledge.utils`` instead of
importing this module directly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.agent_policy import PRIVATE_KNOWLEDGE_BASE_ID_PREFIX
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.embeddings.factory import create_configured_embedder, embedder_client_signature
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.index_metadata import (
    Publication,
    RefreshStatus,
    load_publication,
    load_refresh_status,
    marked_stale,
    publication_unreadable,
    update_refresh_status,
    utc_now,
)
from mindroom.knowledge.indexing_config import (
    IndexingSettings,
    indexing_settings_key,
    storage_key_for_base,
)
from mindroom.knowledge.vector_store import KnowledgeCollection
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_knowledge_binding
from mindroom.tool_system.dependencies import ensure_optional_deps
from mindroom.tool_system.worker_routing import shared_storage_root, written_by_other_workers

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.index_reader import PublishedIndexReader
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
    indexing_settings: IndexingSettings


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
class _PublishedIndexHandle:
    """A read handle for one key's publication; its reader selects the current publication at each read."""

    reader: PublishedIndexReader
    embedder_client_signature: str | None


@dataclass(frozen=True)
class PublishedIndexResolution:
    """One knowledge base's publication, refresh status, and read handle, as read for one lookup.

    ``availability`` is what the persisted state says. ``while_refreshing`` gives the view while the runtime's
    scheduler refreshes the base, which only the scheduler knows.
    """

    key: PublishedIndexKey
    reader: PublishedIndexReader | None
    publication: Publication | None
    status: RefreshStatus
    availability: KnowledgeAvailability
    publication_unreadable: bool = False
    schedule_refresh_on_access: bool = False

    def while_refreshing(self) -> PublishedIndexResolution:
        """Return this view while a refresh runs: its last failure is being retried, and a ready index may change."""
        status = reported_refresh_status(self.status, refreshing=True)
        availability = index_availability(
            self.key,
            self.publication,
            status,
            publication_unreadable=self.publication_unreadable,
            refreshing=True,
        )
        return replace(self, status=status, availability=availability)


#: What the dashboard shows of a base's refresh.
KnowledgeRefreshState = Literal["none", "stale", "refreshing", "refresh_failed"]
_published_indexes: dict[PublishedIndexKey, _PublishedIndexHandle] = {}
_CONSECUTIVE_REFRESH_FAILURE_ALERT_THRESHOLD = 3
_MAX_PRIVATE_PUBLISHED_INDEXES = 128
#: Vectors one search may load from a base whose files worker code can write, so code that grows those files cannot
#: make a search exhaust the primary's memory. About 43,000 chunks of the default 1,536-dimension embedder.
_WORKER_WRITTEN_BASE_MATRIX_BYTES = 256 * 1024 * 1024


def _published_index_key_from_binding(
    base_id: str,
    binding: ResolvedKnowledgeBinding,
    *,
    config: Config,
) -> PublishedIndexKey:
    storage_root = binding.storage_root.expanduser().resolve()
    # The binding already resolved its path; resolving again would follow a link swapped onto it since.
    knowledge_path = binding.knowledge_path
    return PublishedIndexKey(
        base_id=base_id,
        storage_root=str(storage_root),
        knowledge_path=str(knowledge_path),
        indexing_settings=indexing_settings_key(
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
    """Resolve one base ID to its current published index key."""
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
    """Return the private storage directory backing one resolved knowledge base."""
    knowledge_path = Path(key.knowledge_path)
    return (Path(key.storage_root) / "knowledge_db" / storage_key_for_base(key.base_id, knowledge_path)).resolve()


def reported_refresh_status(status: RefreshStatus, *, refreshing: bool) -> RefreshStatus:
    """Return ``status`` as a base reports it: a refresh in flight supersedes the failure it retries."""
    return replace(status, last_error=None) if refreshing else status


def index_availability(
    key: PublishedIndexKey,
    publication: Publication | None,
    status: RefreshStatus,
    *,
    publication_unreadable: bool,
    refreshing: bool,
) -> KnowledgeAvailability:
    """Return what a base can serve, from its publication, its reported refresh status, and whether a refresh runs."""
    if publication is None:
        return (
            KnowledgeAvailability.REFRESH_FAILED
            if publication_unreadable or status.last_error is not None
            else KnowledgeAvailability.INITIALIZING
        )
    if publication.settings != key.indexing_settings:
        return KnowledgeAvailability.CONFIG_MISMATCH
    if status.last_error is not None:
        return KnowledgeAvailability.REFRESH_FAILED
    if refreshing or status.reason is not None:
        return KnowledgeAvailability.STALE
    return KnowledgeAvailability.READY


def index_refresh_state(
    status: RefreshStatus,
    *,
    publication_unreadable: bool,
    refreshing: bool,
) -> KnowledgeRefreshState:
    """Return the dashboard's refresh state from a base's reported refresh status."""
    if publication_unreadable:
        return "refresh_failed"
    if refreshing:
        return "refreshing"
    if status.last_error is not None:
        return "refresh_failed"
    if status.reason is not None:
        return "stale"
    return "none"


def _build_published_index_reader(
    key: PublishedIndexKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> PublishedIndexReader:
    from mindroom.knowledge.index_reader import PublishedIndexReader  # noqa: PLC0415

    # Searching needs numpy, which installs on first use.
    ensure_optional_deps(["numpy"], "numpy", runtime_paths)
    return PublishedIndexReader(
        path=published_index_storage_path(key),
        embedder=create_configured_embedder(config, runtime_paths),
        settings=key.indexing_settings,
        max_matrix_bytes=(
            _WORKER_WRITTEN_BASE_MATRIX_BYTES
            if _workers_write_sources(Path(key.knowledge_path), shared_storage_root(runtime_paths.storage_root))
            else None
        ),
    )


def _workers_write_sources(knowledge_path: Path, storage_root: Path) -> bool:
    """Return whether the base's path lies in, or holds, storage that sandbox workers write.

    That covers private knowledge, file memory, and shared bases inside a workspace or shared directory, such as one
    over thread exports. Other shared bases stay unlimited, since the operator chose and sized their sources.
    """
    if storage_root.is_relative_to(knowledge_path):
        return True
    if not knowledge_path.is_relative_to(storage_root):
        return False
    relative = knowledge_path.relative_to(storage_root)
    # A base at `agents` or `agents/<name>` also indexes the workspaces below it.
    return written_by_other_workers(relative) or (relative.parts[:1] == ("agents",) and len(relative.parts) <= 2)


def published_collection_exists(key: PublishedIndexKey, publication: Publication) -> bool:
    """Return whether the publication names a collection whose file exists."""
    if publication.collection is None:
        return False
    try:
        return KnowledgeCollection(path=published_index_storage_path(key), name=publication.collection).exists()
    except Exception:
        logger.warning(
            "Published knowledge collection existence check failed",
            base_id=key.base_id,
            collection=publication.collection,
            exc_info=True,
        )
        return False


def get_published_index(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> PublishedIndexResolution:
    """Return the base's publication and status, with a read handle when its collection can serve the current config.

    This is the only place read handles are made. A handle is kept per key and rebuilt only when the embedder
    configuration changes: its reader selects the current compatible publication at each read.
    """
    key, binding = _resolve_published_index_key_and_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    storage = published_index_storage_path(key)
    publication = load_publication(storage)
    collection_exists = publication is not None and published_collection_exists(key, publication)
    if publication is not None and publication.collection is not None and not collection_exists:
        # A publication that replaced this one, and deleted its collection, may have landed since it was read.
        publication = load_publication(storage)
        collection_exists = publication is not None and published_collection_exists(key, publication)
    unreadable = publication_unreadable(storage, publication)
    status = load_refresh_status(storage)
    availability = index_availability(key, publication, status, publication_unreadable=unreadable, refreshing=False)
    reader: PublishedIndexReader | None = None
    # A publication whose settings differ only in ways queries do not see, such as chunking, keeps serving until the
    # refresh its config mismatch schedules replaces it.
    if publication is not None and publication.serves(key.indexing_settings):
        # Checked once per publication read: the reader picks the current publication at each search, so one that
        # replaced this one since keeps the base searchable.
        if collection_exists:
            try:
                reader = _cached_reader(key, config=config, runtime_paths=runtime_paths)
            except Exception:
                logger.warning("Published knowledge index handle could not be opened", base_id=base_id, exc_info=True)
                availability = KnowledgeAvailability.REFRESH_FAILED
        elif availability is KnowledgeAvailability.READY:
            availability = KnowledgeAvailability.REFRESH_FAILED
    return PublishedIndexResolution(
        key=key,
        reader=reader,
        publication=publication,
        status=status,
        availability=availability,
        publication_unreadable=unreadable,
        schedule_refresh_on_access=binding.incremental_sync_on_access,
    )


def _cached_reader(key: PublishedIndexKey, *, config: Config, runtime_paths: RuntimePaths) -> PublishedIndexReader:
    signature = embedder_client_signature(config, runtime_paths)
    handle = _published_indexes.get(key)
    if handle is None or handle.embedder_client_signature != signature:
        handle = _PublishedIndexHandle(
            reader=_build_published_index_reader(key, config=config, runtime_paths=runtime_paths),
            embedder_client_signature=signature,
        )
        # A config change gives the same binding a new key; the old key's handle serves nothing new.
        target = refresh_target_for_published_index_key(key)
        for cached in [
            cached for cached in _published_indexes if refresh_target_for_published_index_key(cached) == target
        ]:
            del _published_indexes[cached]
        _published_indexes[key] = handle
        # Private knowledge has one key per requester, so only the most recent private handles stay cached.
        private_keys = [
            cached for cached in _published_indexes if cached.base_id.startswith(PRIVATE_KNOWLEDGE_BASE_ID_PREFIX)
        ]
        for stale in private_keys[:-_MAX_PRIVATE_PUBLISHED_INDEXES]:
            _published_indexes.pop(stale, None)
    return handle.reader


def mark_published_index_stale(key: PublishedIndexKey, *, reason: str) -> None:
    """Record that the base's source changed since its publication, without touching the publication."""
    update_refresh_status(published_index_storage_path(key), lambda status: marked_stale(status, reason))


def record_refresh_succeeded(key: PublishedIndexKey, *, reason_generation: int) -> None:
    """Record a refresh that left a current publication; a source change marked since it started stays marked."""

    def succeeded(status: RefreshStatus) -> RefreshStatus:
        reason = None if status.reason_generation == reason_generation else status.reason
        return replace(
            status,
            reason=reason,
            last_error=None,
            consecutive_refresh_failures=0,
            last_refresh_at=utc_now(),
        )

    update_refresh_status(published_index_storage_path(key), succeeded)


def record_refresh_failed(key: PublishedIndexKey, *, error: str) -> None:
    """Record a failed refresh; the last publication stays served, and an unpublished source change stays marked."""

    def failed(status: RefreshStatus) -> RefreshStatus:
        return replace(
            status,
            last_error=error,
            consecutive_refresh_failures=status.consecutive_refresh_failures + 1,
            last_refresh_at=utc_now(),
        )

    status = update_refresh_status(published_index_storage_path(key), failed)
    if status.consecutive_refresh_failures >= _CONSECUTIVE_REFRESH_FAILURE_ALERT_THRESHOLD:
        logger.error(
            "knowledge_refresh_failing_repeatedly",
            base_id=key.base_id,
            consecutive_refresh_failures=status.consecutive_refresh_failures,
            last_error=error,
        )


def refresh_reason_generation(key: PublishedIndexKey) -> int:
    """Return the base's current source-change generation, which a refresh records before it starts."""
    return load_refresh_status(published_index_storage_path(key)).reason_generation


def _same_physical_source(left: PublishedIndexKey, right: PublishedIndexKey) -> bool:
    return left.storage_root == right.storage_root and left.knowledge_path == right.knowledge_path


def _published_index_keys_for_shared_source(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> tuple[PublishedIndexKey, ...]:
    base_mode = config.get_knowledge_base_config(base_id).mode
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=False,
    )
    matching_keys = [key] if base_mode == "semantic" else []
    for candidate_base_id in config.knowledge_bases:
        if candidate_base_id == base_id:
            continue
        if config.get_knowledge_base_config(candidate_base_id).mode != "semantic":
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
                "Could not resolve related published knowledge index while marking source changed",
                base_id=base_id,
                related_base_id=candidate_base_id,
                exc_info=True,
            )
            continue
        if _same_physical_source(candidate_key, key):
            matching_keys.append(candidate_key)
    return tuple(matching_keys)


def _mark_knowledge_source_changed(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
    excluding: PublishedIndexKey | None = None,
) -> tuple[str, ...]:
    """Mark same-source published indexes stale after a source mutation, except ``excluding``."""
    matching_keys = tuple(
        key
        for key in _published_index_keys_for_shared_source(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
        if key != excluding
    )
    for matching_key in matching_keys:
        mark_published_index_stale(matching_key, reason=reason)
    return tuple(key.base_id for key in matching_keys)


async def mark_knowledge_source_changed_async(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    reason: str = "source_mutated",
    excluding: PublishedIndexKey | None = None,
) -> tuple[str, ...]:
    """Async stale marker that keeps status I/O off the event loop and finishes even when cancelled."""
    return await run_blocking_until_complete(
        partial(
            _mark_knowledge_source_changed,
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            reason=reason,
            excluding=excluding,
        ),
    )
