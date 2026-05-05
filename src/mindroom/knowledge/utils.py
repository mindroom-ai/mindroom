"""Shared knowledge base utilities used by both bot.py and openai_compat.py."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from agno.knowledge.knowledge import Knowledge

from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.redaction import embedded_http_userinfo
from mindroom.knowledge.registry import (
    KnowledgeRefreshTarget,
    PublishedIndexResolution,
    get_published_index,
    refresh_target_for_published_index_key,
)
from mindroom.logging_config import get_logger
from mindroom.runtime_protocols import SupportsConfigOrchestrator  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.knowledge.document import Document
    from structlog.stdlib import BoundLogger

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)
_REFRESH_RETRY_COOLDOWN_SECONDS = 300.0
_MAX_REFRESH_SCHEDULED_COOLDOWNS = 512
_refresh_scheduled_at: dict[tuple[KnowledgeRefreshTarget, KnowledgeAvailability, tuple[str, ...] | None], float] = {}
_EMBEDDED_GIT_USERINFO_FINGERPRINT_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class KnowledgeAvailabilityDetail:
    """Availability plus whether this turn received a last-good index."""

    availability: KnowledgeAvailability
    search_available: bool


@dataclass(frozen=True)
class _KnowledgeResolution:
    """Resolved knowledge plus availability diagnostics for one agent."""

    knowledge: Knowledge | None
    missing: tuple[str, ...] = ()
    unavailable: Mapping[str, KnowledgeAvailabilityDetail] = field(default_factory=dict)


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


def _refresh_schedule_due(
    key: KnowledgeRefreshTarget,
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
    """Bound refresh cooldown bookkeeping for private agent knowledge bindings."""
    if len(_refresh_scheduled_at) <= _MAX_REFRESH_SCHEDULED_COOLDOWNS:
        return
    excess = len(_refresh_scheduled_at) - _MAX_REFRESH_SCHEDULED_COOLDOWNS
    for cache_key, _scheduled_at in sorted(_refresh_scheduled_at.items(), key=lambda item: item[1])[:excess]:
        _refresh_scheduled_at.pop(cache_key, None)


def _published_index_age_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        published_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return max((datetime.now(tz=UTC) - published_at).total_seconds(), 0.0)


def _git_poll_interval_seconds(lookup: PublishedIndexResolution, config: Config) -> float | None:
    git_config = config.get_knowledge_base_config(lookup.key.base_id).git
    if git_config is None:
        return None
    return max(float(git_config.poll_interval_seconds), 0.0)


def _git_poll_due(lookup: PublishedIndexResolution, config: Config) -> bool:
    if lookup.index is None:
        return False
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    if poll_interval_seconds is None:
        return False
    published_age_seconds = _published_index_age_seconds(
        lookup.index.state.last_refresh_at or lookup.index.state.last_published_at,
    )
    return published_age_seconds is None or published_age_seconds >= poll_interval_seconds


def _ready_index_effective_availability(
    lookup: PublishedIndexResolution,
    config: Config,
) -> KnowledgeAvailability:
    """Return request-path availability for a ready index without eager rescans."""
    availability = lookup.availability
    if availability is KnowledgeAvailability.READY and lookup.index is not None and _git_poll_due(lookup, config):
        availability = KnowledgeAvailability.STALE
    return availability


def _refresh_cooldown_seconds(
    lookup: PublishedIndexResolution | None,
    config: Config,
    availability: KnowledgeAvailability,
) -> float:
    if lookup is None or availability is not KnowledgeAvailability.STALE:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    if poll_interval_seconds is None:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    return max(poll_interval_seconds, 1.0)


def _failed_refresh_retry_fingerprint(
    lookup: PublishedIndexResolution,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, ...]:
    """Return a secret-free fingerprint for Git refresh/auth settings that can fix a failed retry."""
    git_config = config.get_knowledge_base_config(lookup.key.base_id).git
    if git_config is None:
        return ()

    fingerprint = [
        "git-refresh",
        f"credentials_service:{git_config.credentials_service or ''}",
        f"sync_timeout_seconds:{git_config.sync_timeout_seconds}",
        f"embedded_userinfo:{_embedded_userinfo_fingerprint(git_config.repo_url)}",
    ]
    if git_config.credentials_service is None:
        return tuple(fingerprint)

    credentials_path = get_runtime_shared_credentials_manager(runtime_paths).get_credentials_path(
        git_config.credentials_service,
    )
    try:
        credentials_stat = credentials_path.stat()
    except OSError:
        fingerprint.extend(("credentials_mtime_ns:", "credentials_size:"))
    else:
        fingerprint.extend(
            (
                f"credentials_mtime_ns:{credentials_stat.st_mtime_ns}",
                f"credentials_size:{credentials_stat.st_size}",
            ),
        )
    return tuple(fingerprint)


def _embedded_userinfo_fingerprint(repo_url: str) -> str:
    userinfo = embedded_http_userinfo(repo_url)
    if userinfo is None:
        return ""
    username, secret = userinfo
    payload = f"{username}\0{secret}".encode()
    return hmac.new(_EMBEDDED_GIT_USERINFO_FINGERPRINT_KEY, payload, hashlib.sha256).hexdigest()


def _refresh_retry_settings(
    lookup: PublishedIndexResolution,
    config: Config,
    runtime_paths: RuntimePaths,
    availability: KnowledgeAvailability,
) -> tuple[str, ...] | None:
    if availability is KnowledgeAvailability.CONFIG_MISMATCH:
        return lookup.key.indexing_settings
    if availability is KnowledgeAvailability.REFRESH_FAILED:
        return lookup.key.indexing_settings + _failed_refresh_retry_fingerprint(lookup, config, runtime_paths)
    return None


def _schedule_refresh_on_access_cooldown_seconds(lookup: PublishedIndexResolution, config: Config) -> float:
    """Return READY refresh throttle without request-path source scans."""
    if config.get_knowledge_base_config(lookup.key.base_id).git is None:
        return _REFRESH_RETRY_COOLDOWN_SECONDS
    poll_interval_seconds = _git_poll_interval_seconds(lookup, config)
    return max(poll_interval_seconds or _REFRESH_RETRY_COOLDOWN_SECONDS, 1.0)


def _schedule_refresh_on_access_due(lookup: PublishedIndexResolution, config: Config) -> bool:
    """Return whether READY on-access refresh should be scheduled without source scans."""
    if config.get_knowledge_base_config(lookup.key.base_id).git is None:
        return True
    return _git_poll_due(lookup, config)


def _schedule_refresh_for_availability(
    refresh_scheduler: KnowledgeRefreshScheduler,
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    lookup: PublishedIndexResolution | None,
    availability: KnowledgeAvailability,
) -> KnowledgeAvailability:
    if lookup is None:
        return availability

    refresh_target = refresh_target_for_published_index_key(lookup.key)
    if availability is KnowledgeAvailability.READY:
        if not lookup.schedule_refresh_on_access or not _schedule_refresh_on_access_due(lookup, config):
            return availability

        scheduler_is_refreshing = refresh_scheduler.is_refreshing(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
        schedule_due = (
            False
            if scheduler_is_refreshing
            else _refresh_schedule_due(
                refresh_target,
                KnowledgeAvailability.READY,
                settings=lookup.key.indexing_settings,
                cooldown_seconds=_schedule_refresh_on_access_cooldown_seconds(lookup, config),
            )
        )
        if schedule_due:
            refresh_scheduler.schedule_refresh(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            )
        return KnowledgeAvailability.STALE if schedule_due or scheduler_is_refreshing else KnowledgeAvailability.READY

    if availability is KnowledgeAvailability.INITIALIZING:
        scheduler_is_refreshing = refresh_scheduler.is_refreshing(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
        if not scheduler_is_refreshing and _refresh_schedule_due(
            refresh_target,
            availability,
            settings=lookup.key.indexing_settings,
        ):
            refresh_scheduler.schedule_refresh(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            )
    elif not refresh_scheduler.is_refreshing(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    ) and _refresh_schedule_due(
        refresh_target,
        availability,
        settings=_refresh_retry_settings(lookup, config, runtime_paths, availability),
        cooldown_seconds=_refresh_cooldown_seconds(lookup, config, availability),
    ):
        refresh_scheduler.schedule_refresh(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
    return availability


def resolve_agent_knowledge_access(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> _KnowledgeResolution:
    """Resolve configured knowledge base(s) with diagnostics for one agent."""
    resolved_knowledge: dict[str, tuple[Knowledge | None, KnowledgeAvailability]] = {}

    def _resolve(base_id: str) -> tuple[Knowledge | None, KnowledgeAvailability]:
        if base_id in resolved_knowledge:
            return resolved_knowledge[base_id]

        lookup = _lookup_knowledge_for_base(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
        availability = lookup.availability if lookup is not None else KnowledgeAvailability.INITIALIZING
        if lookup is not None and availability is KnowledgeAvailability.READY:
            availability = _ready_index_effective_availability(lookup, config)
        knowledge = lookup.index.knowledge if lookup is not None and lookup.index is not None else None
        if refresh_scheduler is not None:
            availability = _schedule_refresh_for_availability(
                refresh_scheduler,
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                lookup=lookup,
                availability=availability,
            )
        resolved_knowledge[base_id] = (knowledge, availability)
        return resolved_knowledge[base_id]

    base_ids = config.get_agent_knowledge_base_ids(agent_name)
    if not base_ids:
        return _KnowledgeResolution(knowledge=None)

    missing_base_ids: list[str] = []
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    knowledges: list[Knowledge] = []
    for base_id in base_ids:
        knowledge, availability = _resolve(base_id)
        if availability is not KnowledgeAvailability.READY:
            unavailable_bases[base_id] = KnowledgeAvailabilityDetail(
                availability=availability,
                search_available=knowledge is not None,
            )
        if knowledge is None:
            missing_base_ids.append(base_id)
            continue
        knowledges.append(knowledge)

    return _KnowledgeResolution(
        knowledge=_merge_knowledge(agent_name, knowledges),
        missing=tuple(missing_base_ids),
        unavailable=unavailable_bases,
    )


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
            if search_available:
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

        resolution = resolve_agent_knowledge_access(
            agent_name,
            self.runtime.config,
            self.runtime_paths,
            refresh_scheduler=refresh_scheduler,
            execution_identity=execution_identity,
        )
        if resolution.missing:
            self.logger.warning(
                "Knowledge bases not available for agent",
                agent_name=agent_name,
                knowledge_bases=list(resolution.missing),
            )
        return resolution


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
                    except NotImplementedError:
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
    vector_db_sources: list[_KnowledgeVectorDb] = [
        cast("_KnowledgeVectorDb", knowledge.vector_db) for knowledge in knowledges if knowledge.vector_db is not None
    ]
    if not vector_db_sources:
        return None
    return Knowledge(
        name=f"{agent_name}_multi_knowledge",
        vector_db=_MultiKnowledgeVectorDb(vector_dbs=vector_db_sources),
        max_results=max(knowledge.max_results for knowledge in knowledges),
    )
