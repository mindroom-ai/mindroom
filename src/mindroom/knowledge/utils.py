"""Shared knowledge base utilities used by both bot.py and openai_compat.py."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from mindroom.embedding_errors import extract_classified_embedder_detail
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.refresh_policy import (
    RefreshCooldownKey,
    cooldown_elapsed,
    ready_index_effective_availability,
    refresh_cooldown_key,
    refresh_trigger,
)
from mindroom.knowledge.registry import PublishedIndexResolution, get_published_index
from mindroom.knowledge_source_descriptions import KnowledgeSourceDescription, KnowledgeWithSourceDescriptions
from mindroom.logging_config import get_logger
from mindroom.runtime_protocols import SupportsConfigOrchestrator  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.knowledge.document import Document
    from agno.knowledge.knowledge import Knowledge

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)
_MAX_REFRESH_SCHEDULED_COOLDOWNS = 512
_refresh_scheduled_at: dict[RefreshCooldownKey, float] = {}


@dataclass(frozen=True)
class KnowledgeAvailabilityDetail:
    """Availability plus whether this turn received a last-good index."""

    availability: KnowledgeAvailability
    search_available: bool
    last_error: str | None = None


@dataclass(frozen=True)
class _KnowledgeResolution:
    """Resolved knowledge plus availability diagnostics for one agent."""

    knowledge: Knowledge | None
    unavailable: Mapping[str, KnowledgeAvailabilityDetail] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeBaseAccessResolution:
    """Resolved access for one configured knowledge base."""

    knowledge: Knowledge | None
    availability: KnowledgeAvailability
    last_error: str | None = None


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


def _knowledge_source_description(base_id: str, config: Config) -> KnowledgeSourceDescription:
    """Return configured source metadata for one queryable Knowledge handle."""
    base_config = config.get_knowledge_base_config(base_id)
    description = " ".join(base_config.description.split())
    private_agent = config.get_private_knowledge_base_agent(base_id)
    if not description and private_agent is not None:
        description = f"Private knowledge for agent '{private_agent}' scoped to the current requester."
    return KnowledgeSourceDescription(base_id=base_id, description=description)


def _apply_knowledge_metadata(base_id: str, knowledge: Knowledge, config: Config) -> None:
    """Attach configured source metadata to one queryable Knowledge handle."""
    source_description = _knowledge_source_description(base_id, config)
    knowledge.name = base_id
    knowledge.description = source_description.description or None


def _lookup_knowledge_for_base(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> PublishedIndexResolution | None:
    """Resolve one configured base ID to its current Knowledge instance."""
    try:
        return get_published_index(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
    except ValueError:
        logger.exception("Published knowledge index lookup failed", base_id=base_id)
        return None


def _schedule_refresh_for_availability(
    refresh_scheduler: KnowledgeRefreshScheduler,
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    lookup: PublishedIndexResolution | None,
    availability: KnowledgeAvailability,
    wall_now: datetime,
) -> KnowledgeAvailability:
    """Apply the refresh policy for one resolved base: probe, throttle, schedule, report."""
    if lookup is None:
        return availability
    trigger = refresh_trigger(
        lookup=lookup,
        availability=availability,
        config=config,
        wall_now=wall_now,
    )
    if trigger is None:
        return availability

    if refresh_scheduler.is_refreshing(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    ):
        return trigger.availability_while_refreshing

    # Key first, then the clock: the stamp should record when the refresh was
    # actually scheduled, so nothing slow may run between sampling and stamping.
    cooldown_key = refresh_cooldown_key(lookup, config, runtime_paths, availability)
    monotonic_now = time.monotonic()
    if not cooldown_elapsed(
        _refresh_scheduled_at,
        cooldown_key,
        monotonic_now=monotonic_now,
        cooldown_seconds=trigger.cooldown_seconds,
    ):
        return availability

    _refresh_scheduled_at[cooldown_key] = monotonic_now
    _prune_refresh_schedule_bookkeeping()
    refresh_scheduler.schedule_refresh(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    return trigger.availability_while_refreshing


def _prune_refresh_schedule_bookkeeping() -> None:
    """Bound refresh cooldown bookkeeping for private agent knowledge bindings."""
    if len(_refresh_scheduled_at) <= _MAX_REFRESH_SCHEDULED_COOLDOWNS:
        return
    excess = len(_refresh_scheduled_at) - _MAX_REFRESH_SCHEDULED_COOLDOWNS
    for cache_key, _scheduled_at in sorted(_refresh_scheduled_at.items(), key=lambda item: item[1])[:excess]:
        _refresh_scheduled_at.pop(cache_key, None)


def _semantic_agent_knowledge_base_ids(agent_name: str, config: Config) -> tuple[str, ...]:
    return tuple(
        base_id
        for base_id in config.resolve_entity(agent_name).knowledge_base_ids
        if config.get_knowledge_base_config(base_id).mode == "semantic"
    )


def _resolve_base_knowledge(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    execution_identity: ToolExecutionIdentity | None,
) -> tuple[Knowledge | None, KnowledgeAvailability, str | None]:
    """Resolve one knowledge base handle with its effective availability and last error."""
    lookup = _lookup_knowledge_for_base(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    # One instant per resolve: the poll-interval boundary must not be evaluated
    # against two different clock readings within a single turn.
    wall_now = datetime.now(tz=UTC)
    availability = lookup.availability if lookup is not None else KnowledgeAvailability.INITIALIZING
    if lookup is not None and availability is KnowledgeAvailability.READY:
        availability = ready_index_effective_availability(lookup, config, wall_now=wall_now)
    knowledge = lookup.index.knowledge if lookup is not None and lookup.index is not None else None
    if knowledge is not None:
        _apply_knowledge_metadata(base_id, knowledge, config)
    if refresh_scheduler is not None:
        availability = _schedule_refresh_for_availability(
            refresh_scheduler,
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            lookup=lookup,
            availability=availability,
            wall_now=wall_now,
        )
    last_error = lookup.state.last_error if lookup is not None and lookup.state is not None else None
    return knowledge, availability, last_error


def resolve_agent_knowledge_access(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> _KnowledgeResolution:
    """Resolve configured knowledge base(s) with diagnostics for one agent."""
    base_ids = _semantic_agent_knowledge_base_ids(agent_name, config)
    if not base_ids:
        return _KnowledgeResolution(knowledge=None)

    missing_base_ids: list[str] = []
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    knowledges: list[Knowledge] = []
    for base_id in base_ids:
        knowledge, availability, last_error = _resolve_base_knowledge(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            refresh_scheduler=refresh_scheduler,
            execution_identity=execution_identity,
        )
        if availability is not KnowledgeAvailability.READY:
            unavailable_bases[base_id] = KnowledgeAvailabilityDetail(
                availability=availability,
                search_available=knowledge is not None,
                last_error=last_error,
            )
        if knowledge is None:
            missing_base_ids.append(base_id)
            continue
        knowledges.append(knowledge)

    if missing_base_ids:
        logger.warning(
            "Knowledge bases not available for agent",
            agent_name=agent_name,
            knowledge_bases=missing_base_ids,
        )
    return _KnowledgeResolution(
        knowledge=_merge_knowledge(agent_name, knowledges),
        unavailable=unavailable_bases,
    )


def resolve_knowledge_base_access(
    base_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
) -> KnowledgeBaseAccessResolution:
    """Resolve one knowledge base without going through an agent assignment."""
    lookup = _lookup_knowledge_for_base(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    availability = lookup.availability if lookup is not None else KnowledgeAvailability.INITIALIZING
    if lookup is not None and availability is KnowledgeAvailability.READY:
        availability = ready_index_effective_availability(lookup, config, wall_now=datetime.now(tz=UTC))
    knowledge = lookup.index.knowledge if lookup is not None and lookup.index is not None else None
    if knowledge is not None:
        _apply_knowledge_metadata(base_id, knowledge, config)
    last_error = lookup.state.last_error if lookup is not None and lookup.state is not None else None
    return KnowledgeBaseAccessResolution(knowledge=knowledge, availability=availability, last_error=last_error)


def _stale_availability_notice(base_id: str, *, search_available: bool) -> str:
    if search_available:
        return (
            f"Knowledge base `{base_id}` may be stale while a refresh is pending this turn. "
            "Do not claim to have searched the latest contents."
        )
    return (
        f"Knowledge base `{base_id}` is unavailable for semantic search this turn because its stale published index "
        "could not be loaded. Do not claim to have searched it."
    )


def format_knowledge_availability_notice(
    unavailable_bases: Mapping[str, KnowledgeAvailabilityDetail],
) -> str | None:
    """Render one user-facing notice for unavailable or stale knowledge bases."""
    if not unavailable_bases:
        return None

    lines: list[str] = []
    for base_id, detail in sorted(unavailable_bases.items()):
        availability = detail.availability
        search_available = detail.search_available

        if availability is KnowledgeAvailability.INITIALIZING:
            lines.append(
                f"Knowledge base `{base_id}` is initializing and unavailable for semantic search this turn. "
                "Do not claim to have searched it.",
            )
        elif availability is KnowledgeAvailability.CONFIG_MISMATCH:
            if search_available:
                lines.append(
                    f"Knowledge base `{base_id}` is refreshing against newer config and may be stale this turn. "
                    "Do not claim to have searched the latest contents.",
                )
            else:
                lines.append(
                    f"Knowledge base `{base_id}` is unavailable for semantic search this turn because its "
                    "published index does not match current config. Do not claim to have searched it.",
                )
        elif availability is KnowledgeAvailability.STALE:
            lines.append(_stale_availability_notice(base_id, search_available=search_available))
        elif availability is KnowledgeAvailability.REFRESH_FAILED:
            # Persisted last_error is operator-grade free text; only the fixed
            # classified embedder vocabulary may enter model-facing prompts.
            classified_cause = extract_classified_embedder_detail(detail.last_error)
            cause = f" Last error: {classified_cause}" if classified_cause else ""
            if search_available:
                lines.append(
                    f"Knowledge base `{base_id}` had a recent refresh failure and may be stale this turn. "
                    f"Do not claim to have searched the latest contents.{cause}",
                )
            else:
                lines.append(
                    f"Knowledge base `{base_id}` is unavailable for semantic search this turn after a refresh "
                    f"failure. Do not claim to have searched it.{cause}",
                )
    return "\n".join(lines) if lines else None


@dataclass
class KnowledgeAccessSupport:
    """Resolve live knowledge access for one runtime without routing through AgentBot."""

    runtime: SupportsConfigOrchestrator
    runtime_paths: RuntimePaths

    def for_agent(
        self,
        agent_name: str,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> Knowledge | None:
        """Return the current knowledge assigned to one or more agent bases."""
        return self.resolve_for_agent(agent_name, execution_identity=execution_identity).knowledge

    def resolve_for_agent(
        self,
        agent_name: str,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> _KnowledgeResolution:
        """Return current knowledge and availability diagnostics for one agent."""
        orchestrator = self.runtime.orchestrator
        refresh_scheduler = orchestrator.knowledge_refresh_scheduler if orchestrator is not None else None

        return resolve_agent_knowledge_access(
            agent_name,
            self.runtime.config,
            self.runtime_paths,
            refresh_scheduler=refresh_scheduler,
            execution_identity=execution_identity,
        )


@dataclass
class _MultiKnowledgeVectorDb:
    """Thin vector DB wrapper that queries multiple vector DBs and merges results.

    Duck-types the vector_db interface expected by agno's ``Knowledge.__post_init__``.
    ``exists()`` returns True and ``create()`` is a no-op so that Knowledge skips its
    own initialization; the underlying indexes are already-published read handles.
    If agno changes the ``__post_init__`` protocol, this adapter will need updating.
    """

    # Agno Knowledge.__post_init__ calls exists()/create(); this adapter intentionally
    # presents already-published read handles as initialized.
    vector_dbs: list[_KnowledgeVectorDb]

    def _resolved_vector_dbs(self) -> list[_KnowledgeVectorDb]:
        """Return the current vector DB instances for every merged source."""
        return self.vector_dbs.copy()

    def exists(self) -> bool:
        """Present as already-initialized to satisfy Knowledge.__post_init__."""
        return True

    def create(self) -> None:
        """No-op because underlying indexes are already published."""
        return

    def search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        """Search each assigned vector database and interleave merged results.

        Partial failures warn and merge the surviving sources; when every
        source failed the first captured exception re-raises so the caller
        sees the real cause instead of silently empty results (ISSUE-237).
        """
        results_by_db: list[list[Document]] = []
        first_error: Exception | None = None
        for vector_db in self._resolved_vector_dbs():
            try:
                results = vector_db.search(query=query, limit=limit, filters=filters)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                logger.warning(
                    "Knowledge vector database search failed",
                    vector_db_type=type(vector_db).__name__,
                    exc_info=True,
                )
                continue
            results_by_db.append(results)
        if first_error is not None and not results_by_db:
            raise first_error
        return _interleave_documents(results_by_db, limit)

    async def async_search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        """Async variant of ``search`` that searches DBs concurrently."""

        async def _search_one(
            vdb: _KnowledgeVectorDb,
        ) -> tuple[list[Document] | None, Exception | None]:
            results: list[Document]
            try:
                if isinstance(vdb, _AsyncKnowledgeVectorDb):
                    try:
                        results = await vdb.async_search(query=query, limit=limit, filters=filters)
                    except NotImplementedError:
                        results = vdb.search(query=query, limit=limit, filters=filters)
                else:
                    results = vdb.search(query=query, limit=limit, filters=filters)
            except Exception as exc:
                logger.warning(
                    "Knowledge vector database async search failed",
                    vector_db_type=type(vdb).__name__,
                    exc_info=True,
                )
                return None, exc
            return results, None

        outcomes = await asyncio.gather(*[_search_one(vdb) for vdb in self._resolved_vector_dbs()])
        results_by_db = [results for results, _error in outcomes if results is not None]
        if not results_by_db:
            for _results, error in outcomes:
                if error is not None:
                    raise error
        return _interleave_documents(results_by_db, limit)


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
    queryable_knowledges = [knowledge for knowledge in knowledges if knowledge.vector_db is not None]
    vector_db_sources: list[_KnowledgeVectorDb] = [
        cast("_KnowledgeVectorDb", knowledge.vector_db) for knowledge in queryable_knowledges
    ]
    if not vector_db_sources:
        return None
    source_descriptions = tuple(
        KnowledgeSourceDescription(
            base_id=cast("str", knowledge.name),
            description=knowledge.description or "",
        )
        for knowledge in queryable_knowledges
    )
    return KnowledgeWithSourceDescriptions(
        name=f"{agent_name}_multi_knowledge",
        vector_db=_MultiKnowledgeVectorDb(vector_dbs=vector_db_sources),
        max_results=max(knowledge.max_results for knowledge in queryable_knowledges),
        source_descriptions=source_descriptions,
    )


def knowledge_runtime_identity(knowledge: Knowledge | None) -> tuple[int, ...]:
    """Identify the stable runtime handles behind one resolved knowledge view."""
    if knowledge is None:
        return ()
    vector_db = knowledge.vector_db
    if isinstance(knowledge, KnowledgeWithSourceDescriptions) and isinstance(vector_db, _MultiKnowledgeVectorDb):
        return tuple(id(source) for source in vector_db.vector_dbs)
    return (id(knowledge),)
