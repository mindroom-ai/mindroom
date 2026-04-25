"""Shared knowledge base utilities used by both bot.py and openai_compat.py."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from agno.knowledge.knowledge import Knowledge

from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.registry import (
    KnowledgeRefreshKey,
    KnowledgeSnapshotLookup,
    get_published_snapshot,
    mark_ready_snapshot_stale,
    ready_snapshot_marked_stale,
    refresh_key_for_snapshot_key,
)
from mindroom.logging_config import get_logger
from mindroom.runtime_protocols import SupportsConfigOrchestrator  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agno.knowledge.document import Document
    from structlog.stdlib import BoundLogger

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_owner import KnowledgeRefreshOwner
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)
_REFRESH_RETRY_COOLDOWN_SECONDS = 300.0
_MAX_REFRESH_SCHEDULED_COOLDOWNS = 512
_refresh_scheduled_at: dict[tuple[KnowledgeRefreshKey, KnowledgeAvailability, tuple[str, ...] | None], float] = {}


@dataclass(frozen=True)
class KnowledgeAvailabilityDetail:
    """Availability plus whether this turn received a last-good snapshot."""

    availability: KnowledgeAvailability
    snapshot_attached: bool


class _KnowledgeVectorDb(Protocol):
    """Subset of vector DB interface this module requires."""

    def search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]: ...


@runtime_checkable
class _AsyncKnowledgeVectorDb(_KnowledgeVectorDb, Protocol):
    """Vector DBs that support the async search path directly."""

    async def async_search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]: ...


def _lookup_knowledge_for_base(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    on_availability: Callable[[KnowledgeAvailability], None] | None = None,
) -> KnowledgeSnapshotLookup | None:
    """Resolve one configured base ID to its current Knowledge instance."""
    try:
        lookup = get_published_snapshot(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
    except Exception:
        logger.exception("Knowledge snapshot lookup failed", base_id=base_id)
        if on_availability is not None:
            on_availability(KnowledgeAvailability.INITIALIZING)
        return None

    if on_availability is not None:
        on_availability(lookup.availability)
    return lookup


def _refresh_schedule_due(
    key: KnowledgeRefreshKey,
    availability: KnowledgeAvailability,
    *,
    settings: tuple[str, ...] | None = None,
    cooldown_seconds: float = _REFRESH_RETRY_COOLDOWN_SECONDS,
) -> bool:
    now = time.monotonic()
    cache_key = (key, availability, settings)
    last_scheduled_at = _refresh_scheduled_at.get(cache_key)
    if last_scheduled_at is not None and now - last_scheduled_at < cooldown_seconds:
        return False
    _refresh_scheduled_at[cache_key] = now
    _prune_refresh_schedule_bookkeeping()
    return True


def _prune_refresh_schedule_bookkeeping() -> None:
    """Bound advisory refresh cooldown bookkeeping for request-scoped bindings."""
    if len(_refresh_scheduled_at) <= _MAX_REFRESH_SCHEDULED_COOLDOWNS:
        return
    excess = len(_refresh_scheduled_at) - _MAX_REFRESH_SCHEDULED_COOLDOWNS
    for cache_key, _scheduled_at in sorted(_refresh_scheduled_at.items(), key=lambda item: item[1])[:excess]:
        _refresh_scheduled_at.pop(cache_key, None)


def _published_snapshot_age_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        published_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return max((datetime.now(tz=UTC) - published_at).total_seconds(), 0.0)


def _git_poll_interval_seconds(lookup: KnowledgeSnapshotLookup, config: Config) -> float | None:
    git_config = config.get_knowledge_base_config(lookup.key.base_id).git
    if git_config is None:
        return None
    return max(float(git_config.poll_interval_seconds), 0.0)


def _git_poll_due(lookup: KnowledgeSnapshotLookup, config: Config) -> bool:
    if lookup.snapshot is None:
        return False
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    if poll_interval_seconds is None:
        return False
    published_age_seconds = _published_snapshot_age_seconds(lookup.snapshot.state.last_published_at)
    return published_age_seconds is None or published_age_seconds >= poll_interval_seconds


def _ready_snapshot_effective_availability(
    lookup: KnowledgeSnapshotLookup,
    config: Config,
) -> KnowledgeAvailability:
    """Return request-path availability for a ready snapshot without eager rescans."""
    availability = lookup.availability
    if availability is KnowledgeAvailability.READY and lookup.snapshot is not None:
        refresh_key = refresh_key_for_snapshot_key(lookup.key)
        source_signature = lookup.snapshot.state.source_signature
        if source_signature is None:
            mark_ready_snapshot_stale(refresh_key, source_signature)
            availability = KnowledgeAvailability.STALE
        elif _git_poll_due(lookup, config) or ready_snapshot_marked_stale(refresh_key, source_signature):
            availability = KnowledgeAvailability.STALE
    return availability


def _refresh_cooldown_seconds(
    lookup: KnowledgeSnapshotLookup | None,
    config: Config,
    availability: KnowledgeAvailability,
) -> float:
    if lookup is None or availability is not KnowledgeAvailability.STALE:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    if poll_interval_seconds is None:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    return max(poll_interval_seconds, 1.0)


def _ready_refresh_on_access_cooldown_seconds(lookup: KnowledgeSnapshotLookup, config: Config) -> float:
    """Return READY refresh throttle without request-path source scans."""
    if config.get_knowledge_base_config(lookup.key.base_id).git is None:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    return _REFRESH_RETRY_COOLDOWN_SECONDS


def _refresh_owner_is_refreshing(
    refresh_owner: KnowledgeRefreshOwner,
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> bool:
    """Return whether the owner reports an active refresh without requiring a concrete implementation."""
    try:
        active = refresh_owner.is_refreshing(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
    except Exception:
        logger.debug("Knowledge refresh active check failed", base_id=base_id, exc_info=True)
        return False
    return active is True


def _schedule_refresh_for_availability(
    refresh_owner: KnowledgeRefreshOwner,
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    lookup: KnowledgeSnapshotLookup | None,
    availability: KnowledgeAvailability,
) -> None:
    if lookup is None:
        return

    refresh_key = refresh_key_for_snapshot_key(lookup.key)
    if availability is KnowledgeAvailability.READY:
        if (
            lookup.refresh_on_access
            and not _refresh_owner_is_refreshing(
                refresh_owner,
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            )
            and _refresh_schedule_due(
                refresh_key,
                KnowledgeAvailability.READY,
                settings=lookup.key.indexing_settings,
                cooldown_seconds=_ready_refresh_on_access_cooldown_seconds(lookup, config),
            )
        ):
            refresh_owner.schedule_refresh(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            )
        return

    if availability is KnowledgeAvailability.INITIALIZING:
        if _refresh_schedule_due(refresh_key, availability):
            refresh_owner.schedule_initial_load(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            )
        return

    if _refresh_schedule_due(
        refresh_key,
        availability,
        settings=lookup.key.indexing_settings if availability is KnowledgeAvailability.CONFIG_MISMATCH else None,
        cooldown_seconds=_refresh_cooldown_seconds(lookup, config, availability),
    ):
        refresh_owner.schedule_refresh(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )


def get_agent_knowledge(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    on_missing_bases: Callable[[list[str]], None] | None = None,
    on_unavailable_bases: Callable[[Mapping[str, KnowledgeAvailability]], None] | None = None,
    on_unavailable_base_details: Callable[[Mapping[str, KnowledgeAvailabilityDetail]], None] | None = None,
    refresh_owner: KnowledgeRefreshOwner | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Knowledge | None:
    """Resolve configured knowledge base(s) for one agent into one Knowledge instance."""
    resolved_knowledge: dict[str, tuple[Knowledge | None, KnowledgeAvailability]] = {}

    def _resolve(base_id: str) -> tuple[Knowledge | None, KnowledgeAvailability]:
        if base_id in resolved_knowledge:
            return resolved_knowledge[base_id]

        availability = KnowledgeAvailability.READY

        def _set_availability(value: KnowledgeAvailability) -> None:
            nonlocal availability
            availability = value

        lookup = _lookup_knowledge_for_base(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            on_availability=_set_availability,
        )
        if lookup is not None and availability is KnowledgeAvailability.READY:
            availability = _ready_snapshot_effective_availability(lookup, config)
        knowledge = lookup.snapshot.knowledge if lookup is not None and lookup.snapshot is not None else None
        if refresh_owner is not None:
            _schedule_refresh_for_availability(
                refresh_owner,
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                lookup=lookup,
                availability=availability,
            )
        resolved_knowledge[base_id] = (knowledge, availability)
        return resolved_knowledge[base_id]

    return resolve_agent_knowledge(
        agent_name,
        config,
        lambda base_id: _resolve(base_id)[0],
        on_missing_bases=on_missing_bases,
        get_availability=lambda base_id: _resolve(base_id)[1],
        on_unavailable_bases=on_unavailable_bases,
        on_unavailable_base_details=on_unavailable_base_details,
    )


def _stale_availability_notice(base_id: str, *, snapshot_attached: bool) -> str:
    if snapshot_attached:
        return (
            f"Knowledge base `{base_id}` may be stale while a refresh is pending this turn. "
            "Do not claim to have searched the latest contents."
        )
    return (
        f"Knowledge base `{base_id}` is unavailable for semantic search this turn because its stale published snapshot "
        "could not be loaded. Do not claim to have searched it."
    )


def format_knowledge_availability_notice(
    unavailable_bases: Mapping[str, KnowledgeAvailability | KnowledgeAvailabilityDetail],
) -> str | None:
    """Render one user-facing notice for unavailable or stale knowledge bases."""
    if not unavailable_bases:
        return None

    lines: list[str] = []
    for base_id, availability_value in sorted(unavailable_bases.items()):
        if isinstance(availability_value, KnowledgeAvailabilityDetail):
            availability = availability_value.availability
            snapshot_attached = availability_value.snapshot_attached
        else:
            availability = availability_value
            snapshot_attached = True

        if availability is KnowledgeAvailability.INITIALIZING:
            lines.append(
                f"Knowledge base `{base_id}` is initializing and unavailable for semantic search this turn. "
                "Do not claim to have searched it.",
            )
        elif availability is KnowledgeAvailability.CONFIG_MISMATCH:
            if snapshot_attached:
                lines.append(
                    f"Knowledge base `{base_id}` is refreshing against newer config and may be stale this turn. "
                    "Do not claim to have searched the latest contents.",
                )
            else:
                lines.append(
                    f"Knowledge base `{base_id}` is unavailable for semantic search this turn because its "
                    "published snapshot does not match current config. Do not claim to have searched it.",
                )
        elif availability is KnowledgeAvailability.STALE:
            lines.append(_stale_availability_notice(base_id, snapshot_attached=snapshot_attached))
        elif availability is KnowledgeAvailability.REFRESH_FAILED:
            if snapshot_attached:
                lines.append(
                    f"Knowledge base `{base_id}` had a recent refresh failure and may be stale this turn. "
                    "Do not claim to have searched the latest contents.",
                )
            else:
                lines.append(
                    f"Knowledge base `{base_id}` is unavailable for semantic search this turn after a refresh "
                    "failure. Do not claim to have searched it.",
                )
    return "\n".join(lines) if lines else None


@dataclass
class KnowledgeAccessSupport:
    """Resolve live knowledge access for one runtime without routing through AgentBot."""

    runtime: SupportsConfigOrchestrator
    logger: BoundLogger
    runtime_paths: RuntimePaths

    def for_agent(
        self,
        agent_name: str,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
        on_unavailable_bases: Callable[[Mapping[str, KnowledgeAvailability]], None] | None = None,
        on_unavailable_base_details: Callable[[Mapping[str, KnowledgeAvailabilityDetail]], None] | None = None,
    ) -> Knowledge | None:
        """Return the current knowledge assigned to one or more agent bases."""
        orchestrator = self.runtime.orchestrator
        refresh_owner = orchestrator.knowledge_refresh_owner if orchestrator is not None else None

        return get_agent_knowledge(
            agent_name,
            self.runtime.config,
            self.runtime_paths,
            on_missing_bases=lambda missing_base_ids: self.logger.warning(
                "Knowledge bases not available for agent",
                agent_name=agent_name,
                knowledge_bases=missing_base_ids,
            ),
            on_unavailable_bases=on_unavailable_bases,
            on_unavailable_base_details=on_unavailable_base_details,
            refresh_owner=refresh_owner,
            execution_identity=execution_identity,
        )


@dataclass
class MultiKnowledgeVectorDb:
    """Thin vector DB wrapper that queries multiple vector DBs and merges results.

    Duck-types the vector_db interface expected by agno's ``Knowledge.__post_init__``.
    ``exists()`` returns True and ``create()`` is a no-op so that Knowledge skips its
    own initialization; the underlying snapshots are already-published read handles.
    If agno changes the ``__post_init__`` protocol, this adapter will need updating.
    """

    # Agno Knowledge.__post_init__ calls exists()/create(); this adapter intentionally
    # presents already-published read handles as initialized.
    vector_dbs: list[_KnowledgeVectorDb | Callable[[], _KnowledgeVectorDb | None]]

    def _resolved_vector_dbs(self) -> list[_KnowledgeVectorDb]:
        """Return the current vector DB instances for every merged source."""
        resolved_vector_dbs: list[_KnowledgeVectorDb] = []
        for source in self.vector_dbs:
            vector_db = source() if callable(source) else source
            if vector_db is None:
                continue
            resolved_vector_dbs.append(vector_db)
        return resolved_vector_dbs

    def exists(self) -> bool:
        """Present as already-initialized to satisfy Knowledge.__post_init__."""
        return True

    def create(self) -> None:
        """No-op because underlying snapshots are already published."""
        return

    def search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        """Search each assigned vector database and interleave merged results."""
        results_by_db: list[list[Document]] = []
        for vector_db in self._resolved_vector_dbs():
            try:
                results = vector_db.search(query=query, limit=limit, filters=filters)
            except Exception:
                logger.warning(
                    "Knowledge vector database search failed",
                    vector_db_type=type(vector_db).__name__,
                    exc_info=True,
                )
                continue
            results_by_db.append(results)
        return _interleave_documents(results_by_db, limit)

    async def async_search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        """Async variant of ``search`` that searches DBs concurrently."""

        async def _search_one(vdb: _KnowledgeVectorDb) -> list[Document]:
            results: list[Document]
            try:
                if isinstance(vdb, _AsyncKnowledgeVectorDb):
                    try:
                        results = await vdb.async_search(query=query, limit=limit, filters=filters)
                    except (NotImplementedError, AttributeError):
                        results = vdb.search(query=query, limit=limit, filters=filters)
                else:
                    results = vdb.search(query=query, limit=limit, filters=filters)
            except Exception:
                logger.warning(
                    "Knowledge vector database async search failed",
                    vector_db_type=type(vdb).__name__,
                    exc_info=True,
                )
                return []
            return results

        results_by_db = await asyncio.gather(*[_search_one(vdb) for vdb in self._resolved_vector_dbs()])
        return _interleave_documents(list(results_by_db), limit)


def _interleave_documents(results_by_db: list[list[Document]], limit: int) -> list[Document]:
    """Interleave per-db results so one knowledge base cannot dominate top-k."""
    if limit <= 0 or not results_by_db:
        return []

    merged: list[Document] = []
    index = 0
    while len(merged) < limit:
        added = False
        for results in results_by_db:
            if index < len(results):
                merged.append(results[index])
                added = True
                if len(merged) >= limit:
                    return merged
        if not added:
            break
        index += 1
    return merged


def _merge_knowledge(agent_name: str, knowledges: list[Knowledge]) -> Knowledge | None:
    """Return a single Knowledge instance, merging when multiple bases are assigned."""
    if not knowledges:
        return None
    if len(knowledges) == 1:
        return knowledges[0]
    vector_db_sources = [
        (lambda knowledge=knowledge: cast("_KnowledgeVectorDb | None", knowledge.vector_db))
        for knowledge in knowledges
        if knowledge.vector_db is not None
    ]
    if not vector_db_sources:
        return None
    return Knowledge(
        name=f"{agent_name}_multi_knowledge",
        vector_db=MultiKnowledgeVectorDb(vector_dbs=vector_db_sources),
        max_results=max(knowledge.max_results for knowledge in knowledges),
    )


def resolve_agent_knowledge(
    agent_name: str,
    config: Config,
    get_knowledge: Callable[[str], Knowledge | None],
    *,
    on_missing_bases: Callable[[list[str]], None] | None = None,
    get_availability: Callable[[str], KnowledgeAvailability] | None = None,
    on_unavailable_bases: Callable[[Mapping[str, KnowledgeAvailability]], None] | None = None,
    on_unavailable_base_details: Callable[[Mapping[str, KnowledgeAvailabilityDetail]], None] | None = None,
) -> Knowledge | None:
    """Resolve configured knowledge base(s) for an agent into one Knowledge instance."""
    base_ids = config.get_agent_knowledge_base_ids(agent_name)
    if not base_ids:
        return None

    missing_base_ids: list[str] = []
    unavailable_base_ids: dict[str, KnowledgeAvailability] = {}
    unavailable_base_details: dict[str, KnowledgeAvailabilityDetail] = {}
    knowledges: list[Knowledge] = []
    for base_id in base_ids:
        knowledge = get_knowledge(base_id)
        if get_availability is not None:
            availability = get_availability(base_id)
            if availability is not KnowledgeAvailability.READY:
                unavailable_base_ids[base_id] = availability
                unavailable_base_details[base_id] = KnowledgeAvailabilityDetail(
                    availability=availability,
                    snapshot_attached=knowledge is not None,
                )
        if knowledge is None:
            missing_base_ids.append(base_id)
            continue
        knowledges.append(knowledge)

    if missing_base_ids and on_missing_bases is not None:
        on_missing_bases(missing_base_ids)
    if unavailable_base_ids and on_unavailable_bases is not None:
        on_unavailable_bases(unavailable_base_ids)
    if unavailable_base_details and on_unavailable_base_details is not None:
        on_unavailable_base_details(unavailable_base_details)

    return _merge_knowledge(agent_name, knowledges)
