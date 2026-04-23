"""Shared knowledge base utilities used by both bot.py and openai_compat.py."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from agno.knowledge.knowledge import Knowledge

from mindroom.knowledge.shared_managers import (
    ensure_agent_knowledge_managers,
    get_published_shared_knowledge_manager,
    get_shared_knowledge_manager_for_config,
)
from mindroom.logging_config import get_logger
from mindroom.runtime_protocols import SupportsConfigOrchestrator  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agno.knowledge.document import Document
    from structlog.stdlib import BoundLogger

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.manager import KnowledgeManager
    from mindroom.knowledge.refresh_owner import KnowledgeRefreshOwner
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


class KnowledgeAvailability(Enum):
    """Availability state for one shared knowledge base on the request path."""

    READY = "ready"
    INITIALIZING = "initializing"
    REFRESH_FAILED = "refresh_failed"
    CONFIG_MISMATCH = "config_mismatch"


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


async def ensure_request_knowledge_managers(
    agent_names: list[str],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> dict[str, KnowledgeManager]:
    """Ensure and collect request-scoped knowledge managers for one agent set."""
    managers: dict[str, KnowledgeManager] = {}
    for agent_name in agent_names:
        managers.update(
            await ensure_agent_knowledge_managers(
                agent_name,
                config,
                runtime_paths,
                execution_identity=execution_identity,
            ),
        )
    return managers


def _get_knowledge_for_base(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    request_knowledge_managers: Mapping[str, KnowledgeManager] | None = None,
    shared_manager_lookup: Callable[[str], KnowledgeManager | None] | None = None,
    on_availability: Callable[[KnowledgeAvailability], None] | None = None,
) -> Knowledge | None:
    """Resolve one configured base ID to its current Knowledge instance."""
    request_manager = request_knowledge_managers.get(base_id) if request_knowledge_managers is not None else None
    if request_manager is not None:
        return request_manager.get_knowledge()
    if config.get_private_knowledge_base_agent(base_id):
        return None

    candidate_manager = shared_manager_lookup(base_id) if shared_manager_lookup is not None else None
    published_manager = get_published_shared_knowledge_manager(
        base_id,
        candidate_manager=candidate_manager,
    )
    if published_manager is None:
        if on_availability is not None:
            on_availability(KnowledgeAvailability.INITIALIZING)
        return None

    manager = get_shared_knowledge_manager_for_config(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        candidate_manager=published_manager,
    )
    if manager is None and on_availability is not None:
        on_availability(KnowledgeAvailability.CONFIG_MISMATCH)
    return published_manager.get_knowledge()


def get_agent_knowledge(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    request_knowledge_managers: Mapping[str, KnowledgeManager] | None = None,
    shared_manager_lookup: Callable[[str], KnowledgeManager | None] | None = None,
    on_missing_bases: Callable[[list[str]], None] | None = None,
    on_unavailable_bases: Callable[[Mapping[str, KnowledgeAvailability]], None] | None = None,
    refresh_owner: KnowledgeRefreshOwner | None = None,
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

        knowledge = _get_knowledge_for_base(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            request_knowledge_managers=request_knowledge_managers,
            shared_manager_lookup=shared_manager_lookup,
            on_availability=_set_availability,
        )
        if refresh_owner is not None:
            if availability is KnowledgeAvailability.INITIALIZING:
                refresh_owner.schedule_initial_load(base_id)
            elif availability is not KnowledgeAvailability.READY:
                refresh_owner.schedule_refresh(base_id)
        resolved_knowledge[base_id] = (knowledge, availability)
        return resolved_knowledge[base_id]

    return resolve_agent_knowledge(
        agent_name,
        config,
        lambda base_id: _resolve(base_id)[0],
        on_missing_bases=on_missing_bases,
        get_availability=lambda base_id: _resolve(base_id)[1],
        on_unavailable_bases=on_unavailable_bases,
    )


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
        request_knowledge_managers: Mapping[str, KnowledgeManager] | None = None,
    ) -> Knowledge | None:
        """Return the current knowledge assigned to one or more agent bases."""
        orchestrator = self.runtime.orchestrator
        refresh_owner = orchestrator.knowledge_refresh_owner if orchestrator is not None else None

        def _shared_manager(base_id: str) -> KnowledgeManager | None:
            if orchestrator is None:
                return None
            return orchestrator.knowledge_managers.get(base_id)

        return get_agent_knowledge(
            agent_name,
            self.runtime.config,
            self.runtime_paths,
            request_knowledge_managers=request_knowledge_managers,
            shared_manager_lookup=_shared_manager,
            on_missing_bases=lambda missing_base_ids: self.logger.warning(
                "Knowledge bases not available for agent",
                agent_name=agent_name,
                knowledge_bases=missing_base_ids,
            ),
            refresh_owner=refresh_owner,
        )


@dataclass
class MultiKnowledgeVectorDb:
    """Thin vector DB wrapper that queries multiple vector DBs and merges results.

    Duck-types the vector_db interface expected by agno's ``Knowledge.__post_init__``.
    ``exists()`` returns True and ``create()`` is a no-op so that Knowledge skips its
    own initialization — the underlying knowledge managers already own the DB lifecycle.
    If agno changes the ``__post_init__`` protocol, this adapter will need updating.
    """

    # Agno Knowledge.__post_init__ calls exists()/create(); this adapter intentionally
    # lies because KnowledgeManager already owns the underlying DB lifecycle.
    vector_dbs: list[_KnowledgeVectorDb]

    def exists(self) -> bool:
        """Present as already-initialized to satisfy Knowledge.__post_init__."""
        return True

    def create(self) -> None:
        """No-op because underlying knowledge managers own DB lifecycle."""
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
        for vector_db in self.vector_dbs:
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

        results_by_db = await asyncio.gather(*[_search_one(vdb) for vdb in self.vector_dbs])
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
    vector_dbs = [knowledge.vector_db for knowledge in knowledges if knowledge.vector_db is not None]
    if not vector_dbs:
        return None
    return Knowledge(
        name=f"{agent_name}_multi_knowledge",
        vector_db=MultiKnowledgeVectorDb(vector_dbs=[cast("_KnowledgeVectorDb", vdb) for vdb in vector_dbs]),
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
) -> Knowledge | None:
    """Resolve configured knowledge base(s) for an agent into one Knowledge instance."""
    base_ids = config.get_agent_knowledge_base_ids(agent_name)
    if not base_ids:
        return None

    missing_base_ids: list[str] = []
    unavailable_base_ids: dict[str, KnowledgeAvailability] = {}
    knowledges: list[Knowledge] = []
    for base_id in base_ids:
        knowledge = get_knowledge(base_id)
        if get_availability is not None:
            availability = get_availability(base_id)
            if availability is not KnowledgeAvailability.READY:
                unavailable_base_ids[base_id] = availability
        if knowledge is None:
            missing_base_ids.append(base_id)
            continue
        knowledges.append(knowledge)

    if missing_base_ids and on_missing_bases is not None:
        on_missing_bases(missing_base_ids)
    if unavailable_base_ids and on_unavailable_bases is not None:
        on_unavailable_bases(unavailable_base_ids)

    return _merge_knowledge(agent_name, knowledges)
