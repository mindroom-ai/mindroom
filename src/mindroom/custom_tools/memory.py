"""Explicit memory tools for MindRoom agents.

Gives agents conscious control over their memory — they can deliberately
store and search facts on demand, complementing the automatic/unconscious
memory extraction that happens after every response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.embedder_health import classified_embedder_error, is_embedder_auth_failure_detail
from mindroom.logging_config import get_logger
from mindroom.memory import (
    add_agent_memory,
    delete_agent_memory,
    get_agent_memory,
    list_all_agent_memories,
    search_agent_memories,
    update_agent_memory,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.memory import MemoryResult, MemorySearchOutcome
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_EMBEDDER_CREDENTIAL_ADVICE = (
    "Update the embedder credential: set memory.embedder.config.api_key in config, "
    "store an 'embedder' credential, or set EMBEDDER_API_KEY."
)


def _memory_result_lines(results: list[MemoryResult]) -> list[str]:
    lines = [f"Found {len(results)} memory(ies):"]
    for i, mem in enumerate(results, 1):
        mid = mem.get("id", "?")
        metadata = mem.get("metadata")
        search_mode = metadata.get("search_mode") if isinstance(metadata, dict) else None
        mode_label = f" [{search_mode}]" if search_mode in {"keyword", "semantic"} else ""
        lines.append(f"{i}. [id={mid}]{mode_label} {mem.get('memory', '')}")
    return lines


def _degraded_search_text(outcome: MemorySearchOutcome) -> str:
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
        lines.append(_EMBEDDER_CREDENTIAL_ADVICE)
    if outcome.results:
        lines.append("")
        lines.extend(_memory_result_lines(outcome.results))
    return "\n".join(lines)


def _memory_tool_failure(exc: Exception, *, action: str, **context: object) -> str:
    """Log and render provider failures without exposing raw response text."""
    if (detail := classified_embedder_error(exc)) is not None:
        logger.warning(f"Failed to {action} memory via tool", error=detail, **context)
        return detail
    logger.exception(f"Failed to {action} memory via tool", error_type=type(exc).__name__, **context)
    return str(exc)


class MemoryTools(Toolkit):
    """Tools that let an agent explicitly store and search its own memories."""

    def __init__(
        self,
        agent_name: str,
        storage_path: Path,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._storage_path = storage_path
        self._config = config
        self._runtime_paths = runtime_paths
        self._execution_identity = execution_identity

        super().__init__(
            name="memory",
            tools=[
                self.add_memory,
                self.search_memories,
                self.list_memories,
                self.get_memory,
                self.update_memory,
                self.delete_memory,
            ],
        )

    async def add_memory(self, content: str) -> str:
        """Store a specific fact or piece of information in your memory.

        Use this when explicitly asked to remember something, or when you
        encounter important information worth retaining for future conversations.

        Args:
            content: The fact or information to memorize.

        Returns:
            Confirmation message.

        """
        try:
            await add_agent_memory(
                content,
                self._agent_name,
                self._storage_path,
                self._config,
                self._runtime_paths,
                metadata={"source": "explicit_tool"},
                execution_identity=self._execution_identity,
            )
        except Exception as e:
            detail = _memory_tool_failure(e, action="add", agent=self._agent_name)
            return f"Failed to store memory: {detail}"
        else:
            return f"Memorized: {content}"

    async def search_memories(self, query: str, limit: int = 5) -> str:
        """Search your memories for information relevant to a query.

        Use this when you need to recall previously stored facts, notes, or context.

        Args:
            query: What to search for in your memories.
            limit: Maximum number of results to return (default 5).

        Returns:
            Formatted list of matching memories, or a message if none found.

        """
        try:
            outcome = await search_agent_memories(
                query,
                self._agent_name,
                self._storage_path,
                self._config,
                self._runtime_paths,
                limit=limit,
                execution_identity=self._execution_identity,
            )
            if outcome.degraded_reason is not None:
                return _degraded_search_text(outcome)
            if not outcome.results:
                return "No relevant memories found."
            return "\n".join(_memory_result_lines(outcome.results))
        except Exception as e:
            detail = _memory_tool_failure(e, action="search", agent=self._agent_name)
            return f"Failed to search memories: {detail}"

    async def list_memories(self, limit: int = 50) -> str:
        """List all your stored memories.

        Use this when asked to show, list, or dump all memories.

        Args:
            limit: Maximum number of memories to return (default 50).

        Returns:
            Formatted list of all memories, or a message if none exist.

        """
        try:
            results = await list_all_agent_memories(
                self._agent_name,
                self._storage_path,
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

    async def get_memory(self, memory_id: str) -> str:
        """Retrieve a single memory by its ID.

        Use this to inspect the full details of a specific memory.

        Args:
            memory_id: The ID of the memory to retrieve (shown in search/list results as [id=...]).

        Returns:
            The memory content, or an error message if not found.

        """
        try:
            result = await get_agent_memory(
                memory_id,
                self._agent_name,
                self._storage_path,
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

    async def update_memory(self, memory_id: str, new_content: str) -> str:
        """Update the content of a specific memory by its ID.

        Use this to correct or refine a previously stored memory.

        Args:
            memory_id: The ID of the memory to update (shown in search/list results as [id=...]).
            new_content: The new content to replace the existing memory with.

        Returns:
            Confirmation message.

        """
        try:
            await update_agent_memory(
                memory_id,
                new_content,
                self._agent_name,
                self._storage_path,
                self._config,
                self._runtime_paths,
                execution_identity=self._execution_identity,
            )
        except Exception as e:
            detail = _memory_tool_failure(e, action="update", agent=self._agent_name, memory_id=memory_id)
            return f"Failed to update memory: {detail}"
        else:
            return f"Updated memory [id={memory_id}]: {new_content}"

    async def delete_memory(self, memory_id: str) -> str:
        """Delete a single memory by its ID.

        Use this to remove a specific outdated or incorrect memory
        without affecting other memories.

        Args:
            memory_id: The ID of the memory to delete (shown in search/list results as [id=...]).

        Returns:
            Confirmation message.

        """
        try:
            await delete_agent_memory(
                memory_id,
                self._agent_name,
                self._storage_path,
                self._config,
                self._runtime_paths,
                execution_identity=self._execution_identity,
            )
        except Exception as e:
            detail = _memory_tool_failure(e, action="delete", agent=self._agent_name, memory_id=memory_id)
            return f"Failed to delete memory: {detail}"
        else:
            return f"Deleted memory [id={memory_id}]"
