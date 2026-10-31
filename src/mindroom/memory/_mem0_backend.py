"""Mem0-backed memory implementation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, ClassVar, cast

from mindroom.embeddings.embedder_health import (
    EmbedderHealthRecorder,
    capture_embedder_health_recorder,
)
from mindroom.embeddings.errors import EmbedderRequestError, classified_embedder_error
from mindroom.helper_usage import HelperUsageOwner, get_helper_usage_owner, record_helper_usage, record_system_usage
from mindroom.logging_config import get_logger
from mindroom.memory_scope_ids import agent_scope_user_id
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.timing import timed

from ._mem0_usage import counted_mem0_usage, mem0_usage
from ._policy import MemoryScope, team_memory_root, team_user_id, team_user_ids
from ._prompting import build_memory_messages
from ._shared import (
    MemoryEntrypointContext,
    MemoryNotFoundError,
    MemoryResult,
    MemorySearchOutcome,
    ScopedMemoryCrud,
)
from .config import mem0_store_exists, recorded_embedder_failures

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import TokenCounter
    from mindroom.llm.usage import RequestUsage
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_MemoryFactory = Callable[..., Awaitable[ScopedMemoryCrud]]

logger = get_logger(__name__)

# Mem0 scores a Chroma hit 1 / (1 + d), where d is the squared L2 distance its collections use, and drops hits below
# the threshold. A search keeps the memories with d <= 1.8, which for unit-length embeddings (d = 2 - 2 * cosine) are
# those with cosine similarity of at least 0.1; Mem0's default threshold of 0.1 would keep every unit-length hit.
_MIN_COSINE_SIMILARITY = 0.1
_MEM0_SEARCH_THRESHOLD = 1 / (1 + 2 * (1 - _MIN_COSINE_SIMILARITY))


def _record_classified_embedder_failure(
    exc: BaseException,
    health_recorder: EmbedderHealthRecorder,
) -> str | None:
    detail = classified_embedder_error(exc)
    if detail is not None:
        health_recorder.record(detail)
    return detail


async def _run_mem0_embedder_mutation(
    operation: Callable[[], Awaitable[None]],
    health_recorder: EmbedderHealthRecorder,
) -> None:
    """Run a Mem0 write, recording a classified embedder failure as embedder health and raising it alone.

    A raw provider failure, such as one from Mem0's memory model, propagates as raised and records no embedder health.
    """
    try:
        await operation()
    except EmbedderRequestError as error:
        health_recorder.record(str(error))
        # Raise the classified error itself, so it keeps its retry judgment, without the raw provider exception.
        raise error from None
    health_recorder.record(None)


async def _strict_mem0_add(
    memory: ScopedMemoryCrud,
    messages: list[dict],
    *,
    user_id: str,
    metadata: dict[str, object],
) -> None:
    """Add through Mem0 and raise the add's first embedding failure, which Mem0 may have caught."""
    with recorded_embedder_failures() as failures:
        await memory.add(messages, user_id=user_id, metadata=metadata)
    if failures:
        raise failures[0]


def _mem0_results(payload: object) -> list[MemoryResult]:
    if isinstance(payload, dict):
        payload_dict = cast("dict[str, object]", payload)
        results = payload_dict.get("results")
        if isinstance(results, list):
            return cast("list[MemoryResult]", results)
    return []


@dataclass(frozen=True)
class _Mem0Store:
    """One Mem0 store and the scopes in it an agent reads."""

    root: Path
    user_ids: tuple[str, ...]


@timed("system_prompt_assembly.memory_search.mem0.create_memory_instance")
async def _create_mem0_memory_instance(
    store_root: Path,
    config: Config,
    create_memory: _MemoryFactory,
) -> ScopedMemoryCrud:
    return await create_memory(store_root, config)


@timed("system_prompt_assembly.memory_search.mem0.store_search")
async def _search_mem0_store(
    store: _Mem0Store,
    query: str,
    config: Config,
    create_memory: _MemoryFactory,
    limit: int,
    results: list[MemoryResult],
) -> None:
    """Build one store and add each of its scopes' hits to ``results``; the store, and a local model it loaded, go after."""
    memory = await _create_mem0_memory_instance(store.root, config, create_memory)
    for user_id in store.user_ids:
        payload = await memory.search(
            query,
            filters={"user_id": user_id},
            top_k=limit,
            threshold=_MEM0_SEARCH_THRESHOLD,
        )
        results.extend(_mem0_results(payload))


@dataclass(frozen=True)
class Mem0MemoryBackend:
    """Mem0-backed adapter implementing the shared memory backend surface."""

    runtime_paths: RuntimePaths
    create_memory: _MemoryFactory
    context_label: ClassVar[str] = "agent"

    def _agent_scope(
        self,
        agent_name: str,
        config: Config,
        execution_identity: ToolExecutionIdentity | None,
    ) -> MemoryScope:
        state_root = resolve_agent_storage(agent_name, config, self.runtime_paths, execution_identity).state_root
        return MemoryScope(agent_scope_user_id(agent_name), state_root)

    def _visible_stores(
        self,
        agent_name: str,
        config: Config,
        execution_identity: ToolExecutionIdentity | None,
    ) -> list[_Mem0Store]:
        """Return the agent's store, then the team store with the agent's teams' scopes, unless none is written yet."""
        agent_scope = self._agent_scope(agent_name, config, execution_identity)
        stores = [_Mem0Store(agent_scope.root, (agent_scope.user_id,))]
        team_root = team_memory_root(self.runtime_paths)
        # Reads never create the team store.
        if (team_ids := team_user_ids(agent_name, config)) and mem0_store_exists(team_root):
            stores.append(_Mem0Store(team_root, tuple(team_ids)))
        return stores

    async def _find(
        self,
        memory_id: str,
        agent_name: str,
        config: Config,
        execution_identity: ToolExecutionIdentity | None,
    ) -> tuple[ScopedMemoryCrud, MemoryResult] | None:
        """Return the store holding one memory of a scope the agent reads, with that memory."""
        for store in self._visible_stores(agent_name, config, execution_identity):
            if (found := await self._get_in(store, memory_id, config)) is not None:
                return found
        return None

    async def _get_in(
        self,
        store: _Mem0Store,
        memory_id: str,
        config: Config,
    ) -> tuple[ScopedMemoryCrud, MemoryResult] | None:
        """Return a store with one memory of a scope the agent reads in it; a store without one goes when this returns."""
        memory = await self.create_memory(store.root, config)
        result = await memory.get(memory_id)
        if isinstance(result, dict) and result.get("user_id") in store.user_ids:
            return memory, cast("MemoryResult", result)
        return None

    async def _record_extraction_usage(
        self,
        calls: list[RequestUsage],
        owner: HelperUsageOwner | None,
        requester_id: str | None,
    ) -> None:
        """Count Mem0's extraction tokens toward the conversation, or as internal work, which names no requester."""
        if (usage := mem0_usage(calls)) is None:
            return
        try:
            if owner is None:
                await record_system_usage(usage, runtime_paths=self.runtime_paths, kind="memory_extraction")
            else:
                await record_helper_usage(usage, owner=owner, kind="memory_extraction", requester_id=requester_id)
        except Exception:
            # Recording follows the memory write, which it must neither fail nor, after a failed write, replace.
            logger.exception("Memory extraction usage could not be recorded")

    async def add(
        self,
        content: str,
        agent_name: str,
        config: Config,
        *,
        metadata: dict | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Add one mem0 memory for an agent scope."""
        scope = self._agent_scope(agent_name, config, execution_identity)
        metadata = dict(metadata or {})
        metadata["agent"] = agent_name
        messages = [{"role": "user", "content": content}]
        health_recorder = capture_embedder_health_recorder()

        async def add_memory() -> None:
            memory = await self.create_memory(scope.root, config)
            await _strict_mem0_add(memory, messages, user_id=scope.user_id, metadata=metadata)

        with counted_mem0_usage() as calls:
            try:
                await _run_mem0_embedder_mutation(add_memory, health_recorder)
            finally:
                # Extraction tokens are spent even when storing the extracted facts fails afterwards.
                # The reply running the memory tool, or the call whose transcript this is, binds the owner.
                await self._record_extraction_usage(
                    calls,
                    get_helper_usage_owner(),
                    execution_identity.requester_id if execution_identity is not None else None,
                )
        logger.info("Memory added", agent=agent_name)

    @timed("system_prompt_assembly.memory_search.mem0_backend")
    async def search(
        self,
        query: str,
        agent_name: str,
        config: Config,
        *,
        limit: int,
        execution_identity: ToolExecutionIdentity | None = None,
        editable: bool = False,  # noqa: ARG002 - every mem0 ID is editable
    ) -> MemorySearchOutcome:
        """Search mem0 memories visible to an agent: its own store, then its teams' scopes in the one team store.

        Mem0 builds its own internal embedder, so passive health recording in
        MindRoom's embedder never sees this traffic: the backend records the
        outcome itself. Mem0's search embeds the query without swallowing
        (verified against mem0ai 2.2.1), so a completed search proves the
        embedder round-tripped and a propagated provider error classifies
        into a degraded outcome instead of raising. Every scope's search embeds
        the query with the same embedder, so the first failure ends the search
        with the hits found so far. The two stores are searched one at a time,
        each released before the next is built, because each Mem0 instance builds
        its own embedder, which for a local model is a full copy of that model.
        """
        health_recorder = capture_embedder_health_recorder()
        results: list[MemoryResult] = []
        degraded_reason: str | None = None
        for store in self._visible_stores(agent_name, config, execution_identity):
            try:
                await _search_mem0_store(store, query, config, self.create_memory, limit, results)
            except Exception as exc:
                if (degraded_reason := _record_classified_embedder_failure(exc, health_recorder)) is None:
                    raise
                logger.warning(
                    "Mem0 memory search degraded by embedder failure",
                    agent=agent_name,
                    store=str(store.root),
                    error=degraded_reason,
                )
                break
        else:
            health_recorder.record(None)
        logger.debug("Total memories found", count=len(results), agent=agent_name)
        # Every scope's store uses one embedder and score scale, so the limit keeps the best hits of any scope.
        results.sort(key=lambda result: result.get("score", 0.0), reverse=True)
        return MemorySearchOutcome(results=results[:limit], degraded_reason=degraded_reason)

    async def list_all(
        self,
        agent_name: str,
        config: Config,
        *,
        limit: int,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> list[MemoryResult]:
        """List mem0 memories stored for an agent."""
        scope = self._agent_scope(agent_name, config, execution_identity)
        memory = await self.create_memory(scope.root, config)
        return _mem0_results(await memory.get_all(filters={"user_id": scope.user_id}, top_k=limit))

    async def get(
        self,
        memory_id: str,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryResult | None:
        """Return one mem0 memory of a scope the agent reads."""
        found = await self._find(memory_id, agent_name, config, execution_identity)
        return found[1] if found is not None else None

    async def update(
        self,
        memory_id: str,
        content: str,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Update one mem0 memory of a scope the agent reads."""
        health_recorder = capture_embedder_health_recorder()

        async def update_memory() -> None:
            if (found := await self._find(memory_id, agent_name, config, execution_identity)) is None:
                raise MemoryNotFoundError(memory_id)
            await found[0].update(memory_id, content)

        await _run_mem0_embedder_mutation(update_memory, health_recorder)
        logger.info("Memory updated", memory_id=memory_id)

    async def delete(
        self,
        memory_id: str,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Delete one mem0 memory of a scope the agent reads."""
        if (found := await self._find(memory_id, agent_name, config, execution_identity)) is None:
            raise MemoryNotFoundError(memory_id)
        await found[0].delete(memory_id)
        logger.info("Memory deleted", memory_id=memory_id)

    async def store_conversation(
        self,
        prompt: str,
        caller: str | Sequence[str],
        session_id: str,
        config: Config,
        *,
        thread_history: Sequence[ResolvedVisibleMessage] | None = None,
        user_id: str | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Extract one conversation turn's memories into the agent's scope or the team's."""
        messages = build_memory_messages(prompt, thread_history, user_id)
        if not messages:
            return
        metadata: dict[str, object] = {"type": "conversation", "session_id": session_id}
        owner: HelperUsageOwner | None
        log_context: dict[str, object]
        if isinstance(caller, str):
            scope = self._agent_scope(caller, config, execution_identity)
            metadata["agent"] = caller
            log_context = {"agent": caller}
            owner = HelperUsageOwner(
                storage_factory=partial(
                    create_session_storage,
                    caller,
                    config,
                    self.runtime_paths,
                    execution_identity=execution_identity,
                ),
                session_id=session_id,
            )
        else:
            scope = MemoryScope(team_user_id(caller), team_memory_root(self.runtime_paths))
            metadata.update(is_team=True, team_members=list(caller))
            log_context = {"team_id": scope.user_id}
            # A team's conversation has no single agent session, so its extraction counts as internal work.
            owner = None
        health_recorder = capture_embedder_health_recorder()
        try:
            memory = await self.create_memory(scope.root, config)
        except Exception as exc:
            if (detail := _record_classified_embedder_failure(exc, health_recorder)) is None:
                raise
            logger.warning("Failed to add memory", error=detail, **log_context)
            return
        with counted_mem0_usage() as calls:
            try:
                await _strict_mem0_add(memory, messages, user_id=scope.user_id, metadata=metadata)
            except Exception as error:
                if (detail := _record_classified_embedder_failure(error, health_recorder)) is None:
                    logger.exception("Failed to add memory", error_type=type(error).__name__, **log_context)
                else:
                    logger.warning("Failed to add memory", error=detail, **log_context)
                return
            else:
                health_recorder.record(None)
            finally:
                # Extraction tokens are spent even when storing the extracted facts fails afterwards.
                await self._record_extraction_usage(calls, owner, user_id)
        logger.info("Memory added", **log_context)

    def load_entrypoint_context(
        self,
        agent_name: str,
        config: Config,
        *,
        counter: TokenCounter,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryEntrypointContext:
        """Return no stable entrypoint context; mem0 has no curated `MEMORY.md`."""
        del agent_name, config, counter, execution_identity
        return MemoryEntrypointContext()
