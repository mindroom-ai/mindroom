"""Explicit memory tools for MindRoom agents.

Gives agents conscious control over their memory — they can deliberately
store and search facts on demand, complementing the automatic/unconscious
memory extraction that happens after every response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mindroom.embeddings.errors import classified_embedder_error, is_embedder_auth_failure_detail
from mindroom.failure_text import failure_text
from mindroom.logging_config import get_logger
from mindroom.memory import (
    MemoryNotFoundError,
    add_agent_memory,
    delete_agent_memory,
    get_agent_memory,
    list_all_agent_memories,
    search_agent_memories,
    update_agent_memory,
)
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.memory import MemoryResult, MemorySearchOutcome
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_SIMILAR_MEMORY_LIMIT = 3


def _memory_result_lines(results: list[MemoryResult]) -> list[str]:
    lines = [f"Found {len(results)} memory(ies):"]
    for i, mem in enumerate(results, 1):
        mid = mem.get("id", "?")
        metadata = mem.get("metadata")
        search_mode = metadata.get("search_mode") if isinstance(metadata, dict) else None
        mode_label = f" [{search_mode}]" if search_mode in {"keyword", "semantic"} else ""
        lines.append(f"{i}. [id={mid}]{mode_label} {mem.get('memory', '')}")
    return lines


def _embedder_credential_advice(config: Config) -> str:
    """Describe the active credential binding without recommending a fallback that cannot win."""
    if config.memory.embedder.provider == "ollama":
        # The Mem0 backend's own Ollama client reads OLLAMA_API_KEY only from the process environment, while the file
        # backend's embedder also reads the .env beside config.yaml, so the advice names the place both read.
        return (
            "Repair the embedder credential: the Ollama embedder sends only OLLAMA_API_KEY, "
            "so set it in the environment of the MindRoom process and restart MindRoom."
        )
    if config.memory.embedder.provider == "vertex_ai":
        return (
            "Repair the embedder credential: the Vertex AI embedder authenticates with GOOGLE_APPLICATION_CREDENTIALS "
            "or Application Default Credentials, which need permission to call Vertex AI in its project, and then "
            "restart MindRoom."
        )
    credentials_service = config.memory.embedder.config.credentials_service
    if credentials_service is not None:
        return (
            "Repair the embedder credential: set memory.embedder.config.api_key in config "
            f"or replace the key in the '{credentials_service}' credential service."
        )
    return (
        "Repair the embedder credential: set memory.embedder.config.api_key in config, "
        "store an 'embedder' credential, or set EMBEDDER_API_KEY."
    )


def _degraded_search_text(outcome: MemorySearchOutcome, config: Config) -> str:
    def is_keyword_result(result: MemoryResult) -> bool:
        metadata = result.get("metadata")
        return isinstance(metadata, dict) and metadata.get("search_mode") == "keyword"

    keyword_only = bool(outcome.results) and all(is_keyword_result(result) for result in outcome.results)
    availability = "unavailable" if not outcome.results or keyword_only else "partially unavailable"
    failure_line = f"Semantic memory search {availability}: {outcome.degraded_reason}."
    if outcome.results:
        result_detail = "Showing keyword matches only." if keyword_only else "Showing results from available scopes."
        failure_line = f"{failure_line} {result_detail}"
    lines = [failure_line]
    if is_embedder_auth_failure_detail(outcome.degraded_reason):
        lines.append(_embedder_credential_advice(config))
    if outcome.results:
        lines.append("")
        lines.extend(_memory_result_lines(outcome.results))
    return "\n".join(lines)


def _memory_tool_failure(exc: Exception, *, action: str, **context: object) -> str:
    """Log a failure and return what the model may see of it, without credentials.

    A classified embedder failure becomes its fixed detail, and a missing memory ID logs a warning without a
    traceback. Any non-embedder failure keeps its text with credentials redacted, because a failed memory LLM call
    carries the provider's error body, which can quote the rejected key.
    """
    if (detail := classified_embedder_error(exc)) is not None:
        logger.warning(f"Failed to {action} memory via tool", error=detail, **context)
        return detail
    detail = redact_sensitive_text(failure_text(exc))
    if isinstance(exc, MemoryNotFoundError):
        logger.warning(f"Failed to {action} memory via tool", error=detail, **context)
        return detail
    logger.exception(f"Failed to {action} memory via tool", error_type=type(exc).__name__, **context)
    return detail


_ALLOWED_MEMORY_PARAMS: dict[str, frozenset[str]] = {
    "add": frozenset({"content"}),
    "search": frozenset({"query", "limit"}),
    "list": frozenset({"limit"}),
    "get": frozenset({"memory_id"}),
    "update": frozenset({"memory_id", "content"}),
    "delete": frozenset({"memory_id"}),
}


def _validate_memory_params(
    action: str,
    *,
    content: str | None,
    query: str | None,
    memory_id: str | None,
    limit: int | None,
) -> str | None:
    """Validate required and allowed parameters for ``action``."""
    allowed = _ALLOWED_MEMORY_PARAMS.get(action)
    if allowed is None:
        return f"Error: unsupported memory action {action!r}. Expected one of: add, search, list, get, update, delete."
    provided = {
        name: value is not None
        for name, value in (
            ("content", content),
            ("query", query),
            ("memory_id", memory_id),
            ("limit", limit),
        )
    }
    unexpected = [name for name, is_set in provided.items() if is_set and name not in allowed]
    if unexpected:
        names = ", ".join(f"`{name}`" for name in unexpected)
        return f"Error: parameter(s) {names} cannot be used with action={action!r}."
    required = {
        "add": (("content", content),),
        "search": (("query", query),),
        "list": (),
        "get": (("memory_id", memory_id),),
        "update": (("memory_id", memory_id), ("content", content)),
        "delete": (("memory_id", memory_id),),
    }[action]
    for name, value in required:
        if value is None or not value.strip():
            return f"Error: `{name}` is required for action={action!r}."
    return None


class MemoryTools(Toolkit):
    """Tools that let an agent explicitly store and search its own memories."""

    def __init__(
        self,
        agent_name: str,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._runtime_paths = runtime_paths
        self._execution_identity = execution_identity

        super().__init__(
            name="memory",
            tools=[self.memory],
        )

    async def memory(  # noqa: PLR0911 - action dispatch across six memory operations
        self,
        action: Literal["add", "search", "list", "get", "update", "delete"],
        content: str | None = None,
        query: str | None = None,
        memory_id: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Store, search, list, fetch, update, or delete long-term facts and notes across conversations.

        - `add`: persist `content` (lists similar existing memories so you can refine with `update` instead of duplicating).
        - `search`: search by `query` (optional `limit`, default 5). File memory uses configured Markdown paths (default: memory/**/*.md) and falls back to keyword search over MEMORY.md and memory/**/*.md. Persisted and `file:*` IDs work with `get`, `update`, or `delete`; `semantic:*` IDs are read-only locators.
        - `list`: list stored memories (optional `limit`, default 50).
        - `get` / `delete`: fetch or delete one entry by `memory_id`.
        - `update`: replace an entry's `content` by `memory_id`.

        Args:
            action: Operation to perform.
            content: Fact or note to store (`add`) or replacement text (`update`).
            query: Search query (`search`).
            memory_id: Memory ID from `search`/`list` (`get`, `update`, `delete`).
            limit: Max results (`search` default 5; `list` default 50).

        Returns:
            Formatted result or error message.

        """
        if err := _validate_memory_params(
            action,
            content=content,
            query=query,
            memory_id=memory_id,
            limit=limit,
        ):
            return err
        if action == "add":
            assert content is not None
            return await self._add_memory(content)
        if action == "search":
            assert query is not None
            return await self._search_memories(query, limit=5 if limit is None else limit)
        if action == "list":
            return await self._list_memories(limit=50 if limit is None else limit)
        if action == "get":
            assert memory_id is not None
            return await self._get_memory(memory_id)
        if action == "update":
            assert memory_id is not None
            assert content is not None
            return await self._update_memory(memory_id, content)
        assert memory_id is not None
        return await self._delete_memory(memory_id)

    async def _add_memory(self, content: str) -> str:
        # Look up before storing, so the new entry cannot list itself as similar.
        similar = await self._similar_memories(content)
        try:
            await add_agent_memory(
                content,
                self._agent_name,
                self._config,
                self._runtime_paths,
                metadata={"source": "explicit_tool"},
                execution_identity=self._execution_identity,
            )
        except Exception as e:
            detail = _memory_tool_failure(e, action="add", agent=self._agent_name)
            return f"Failed to store memory: {detail}"
        if not similar:
            return f"Memorized: {content}"
        lines = [f"Memorized: {content}", "", "Similar existing memories:"]
        for i, mem in enumerate(similar, 1):
            lines.append(f"{i}. [id={mem.get('id', '?')}] {mem.get('memory', '')}")
        lines.append(
            'If the new memory corrects one of these, rewrite that entry with memory(action="update", ...), '
            "keeping its other details, so no stored memory contradicts the correction.",
        )
        return "\n".join(lines)

    async def _similar_memories(self, content: str) -> list[MemoryResult]:
        """Return a few existing memories related to ``content``; a failed lookup returns none."""
        try:
            outcome = await search_agent_memories(
                content,
                self._agent_name,
                self._config,
                self._runtime_paths,
                limit=_SIMILAR_MEMORY_LIMIT,
                execution_identity=self._execution_identity,
                editable=True,
            )
        except Exception as e:
            _memory_tool_failure(e, action="find similar", agent=self._agent_name)
            return []
        return outcome.results

    async def _search_memories(self, query: str, limit: int = 5) -> str:
        try:
            outcome = await search_agent_memories(
                query,
                self._agent_name,
                self._config,
                self._runtime_paths,
                limit=limit,
                execution_identity=self._execution_identity,
            )
            if outcome.degraded_reason is not None:
                return _degraded_search_text(outcome, self._config)
            if not outcome.results:
                return "No relevant memories found."
            return "\n".join(_memory_result_lines(outcome.results))
        except Exception as e:
            detail = _memory_tool_failure(e, action="search", agent=self._agent_name)
            return f"Failed to search memories: {detail}"

    async def _list_memories(self, limit: int = 50) -> str:
        try:
            results = await list_all_agent_memories(
                self._agent_name,
                self._config,
                self._runtime_paths,
                limit=limit,
                execution_identity=self._execution_identity,
            )
            if not results:
                return "No memories stored yet."

            lines = [f"All memories ({len(results)}):"]
            for i, mem in enumerate(results, 1):
                mid = mem.get("id", "?")
                lines.append(f"{i}. [id={mid}] {mem.get('memory', '')}")
            return "\n".join(lines)
        except Exception as e:
            detail = _memory_tool_failure(e, action="list", agent=self._agent_name)
            return f"Failed to list memories: {detail}"

    async def _get_memory(self, memory_id: str) -> str:
        try:
            result = await get_agent_memory(
                memory_id,
                self._agent_name,
                self._config,
                self._runtime_paths,
                execution_identity=self._execution_identity,
            )
            if result is None:
                return f"No memory found with id={memory_id}"
            return f"[id={result.get('id', memory_id)}] {result.get('memory', '')}"
        except Exception as e:
            detail = _memory_tool_failure(e, action="get", agent=self._agent_name, memory_id=memory_id)
            return f"Failed to get memory: {detail}"

    async def _update_memory(self, memory_id: str, new_content: str) -> str:
        try:
            await update_agent_memory(
                memory_id,
                new_content,
                self._agent_name,
                self._config,
                self._runtime_paths,
                execution_identity=self._execution_identity,
            )
        except Exception as e:
            detail = _memory_tool_failure(e, action="update", agent=self._agent_name, memory_id=memory_id)
            return f"Failed to update memory: {detail}"
        else:
            return f"Updated memory [id={memory_id}]: {new_content}"

    async def _delete_memory(self, memory_id: str) -> str:
        try:
            await delete_agent_memory(
                memory_id,
                self._agent_name,
                self._config,
                self._runtime_paths,
                execution_identity=self._execution_identity,
            )
        except Exception as e:
            detail = _memory_tool_failure(e, action="delete", agent=self._agent_name, memory_id=memory_id)
            return f"Failed to delete memory: {detail}"
        else:
            return f"Deleted memory [id={memory_id}]"
