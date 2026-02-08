"""Explicit memory tools for MindRoom agents.

Gives agents conscious control over their memory — they can deliberately
store and search facts on demand, complementing the automatic/unconscious
memory extraction that happens after every response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.logging_config import get_logger
from mindroom.memory.functions import (
    add_agent_memory,
    delete_agent_memory,
    get_agent_memory,
    list_all_agent_memories,
    search_agent_memories,
    update_agent_memory,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config import Config

logger = get_logger(__name__)


class MemoryTools(Toolkit):
    """Tools that let an agent explicitly store and search its own memories."""

    def __init__(self, agent_name: str, storage_path: Path, config: Config) -> None:
        self._agent_name = agent_name
        self._storage_path = storage_path
        self._config = config

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
                metadata={"source": "explicit_tool"},
            )
        except Exception as e:
            logger.exception("Failed to add memory via tool", agent=self._agent_name, error=str(e))
            return f"Failed to store memory: {e}"
        else:
            return f"Memorized: {content}"

    async def search_memories(self, query: str, limit: int = 5) -> str:
        """Search your memories for information relevant to a query.

        Use this when you need to recall previously stored facts or context.

        Args:
            query: What to search for in your memories.
            limit: Maximum number of results to return (default 5).

        Returns:
            Formatted list of matching memories, or a message if none found.

        """
        try:
            results = await search_agent_memories(
                query,
                self._agent_name,
                self._storage_path,
                self._config,
                limit=limit,
            )
            if not results:
                return "No relevant memories found."

            lines = [f"Found {len(results)} memory(ies):"]
            for i, mem in enumerate(results, 1):
                mid = mem.get("id", "?")
                lines.append(f"{i}. [id={mid}] {mem.get('memory', '')}")
            return "\n".join(lines)
        except Exception as e:
            logger.exception("Failed to search memories via tool", agent=self._agent_name, error=str(e))
            return f"Failed to search memories: {e}"

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
                limit=limit,
            )
            if not results:
                return "No memories stored yet."

            lines = [f"All memories ({len(results)}):"]
            for i, mem in enumerate(results, 1):
                mid = mem.get("id", "?")
                lines.append(f"{i}. [id={mid}] {mem.get('memory', '')}")
            return "\n".join(lines)
        except Exception as e:
            logger.exception("Failed to list memories via tool", agent=self._agent_name, error=str(e))
            return f"Failed to list memories: {e}"

    async def get_memory(self, memory_id: str) -> str:
        """Retrieve a single memory by its ID.

        Use this to inspect the full details of a specific memory.

        Args:
            memory_id: The ID of the memory to retrieve (shown in search/list results as [id=...]).

        Returns:
            The memory content, or an error message if not found.

        """
        try:
            result = await get_agent_memory(memory_id, self._agent_name, self._storage_path, self._config)
            if result is None:
                return f"No memory found with id={memory_id}"
            return f"[id={result.get('id', memory_id)}] {result.get('memory', '')}"
        except Exception as e:
            logger.exception("Failed to get memory via tool", agent=self._agent_name, memory_id=memory_id, error=str(e))
            return f"Failed to get memory: {e}"

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
            await update_agent_memory(memory_id, new_content, self._agent_name, self._storage_path, self._config)
        except Exception as e:
            logger.exception(
                "Failed to update memory via tool",
                agent=self._agent_name,
                memory_id=memory_id,
                error=str(e),
            )
            return f"Failed to update memory: {e}"
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
            await delete_agent_memory(memory_id, self._agent_name, self._storage_path, self._config)
        except Exception as e:
            logger.exception(
                "Failed to delete memory via tool",
                agent=self._agent_name,
                memory_id=memory_id,
                error=str(e),
            )
            return f"Failed to delete memory: {e}"
        else:
            return f"Deleted memory [id={memory_id}]"
