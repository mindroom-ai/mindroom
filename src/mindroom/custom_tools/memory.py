"""Explicit memory tools for MindRoom agents.

Gives agents conscious control over their memory — they can deliberately
store and search facts on demand, complementing the automatic/unconscious
memory extraction that happens after every response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.logging_config import get_logger
from mindroom.memory.functions import add_agent_memory, search_agent_memories

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
            tools=[self.add_memory, self.search_memories],
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
                lines.append(f"{i}. {mem.get('memory', '')}")
            return "\n".join(lines)
        except Exception as e:
            logger.exception("Failed to search memories via tool", agent=self._agent_name, error=str(e))
            return f"Failed to search memories: {e}"
