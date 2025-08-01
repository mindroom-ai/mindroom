"""Simple memory management functions following Mem0 patterns."""

from pathlib import Path
from typing import Any, TypedDict

from ..logging_config import get_logger
from .config import create_memory_instance


class MemoryResult(TypedDict):
    """Type for memory search results."""

    memory: str
    metadata: dict[str, Any] | None


logger = get_logger(__name__)

# Global memory instance cache
_memory_instance = None


def get_memory(storage_path: Path) -> Any:
    """Get or create the global memory instance."""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = create_memory_instance(storage_path)
    return _memory_instance


def add_agent_memory(
    content: str, agent_name: str, storage_path: Path, user_id: str | None = None, metadata: dict | None = None
) -> None:
    """Add a memory for an agent.

    Args:
        content: The memory content to store
        agent_name: Name of the agent
        storage_path: Storage path for memory
        user_id: Optional user ID to associate memory with
        metadata: Optional metadata to store with memory
    """
    memory = get_memory(storage_path)

    # Add agent context to metadata
    if metadata is None:
        metadata = {}
    metadata["agent"] = agent_name

    # Create message format for Mem0
    messages = [{"role": "assistant", "content": content}]

    # Use agent_name as user_id to namespace memories per agent
    memory.add(messages, user_id=f"agent_{agent_name}", metadata=metadata)
    logger.debug(f"Added memory for agent {agent_name}: {content[:50]}...")


def search_agent_memories(query: str, agent_name: str, storage_path: Path, limit: int = 3) -> list[MemoryResult]:
    """Search agent memories.

    Args:
        query: Search query
        agent_name: Name of the agent
        storage_path: Storage path for memory
        limit: Maximum number of results

    Returns:
        List of relevant memories
    """
    memory = get_memory(storage_path)
    results = memory.search(query, user_id=f"agent_{agent_name}", limit=limit)
    logger.debug(f"Found {len(results)} memories for agent {agent_name}")
    return results  # type: ignore[no-any-return]


def add_room_memory(
    content: str, room_id: str, storage_path: Path, agent_name: str | None = None, metadata: dict | None = None
) -> None:
    """Add a memory for a room.

    Args:
        content: The memory content to store
        room_id: Room ID
        storage_path: Storage path for memory
        agent_name: Optional agent that created this memory
        metadata: Optional metadata to store with memory
    """
    memory = get_memory(storage_path)

    # Add room context to metadata
    if metadata is None:
        metadata = {}
    metadata["room_id"] = room_id
    if agent_name:
        metadata["contributed_by"] = agent_name

    # Create message format for Mem0
    messages = [{"role": "assistant", "content": content}]

    # Use sanitized room_id as user_id
    safe_room_id = room_id.replace(":", "_").replace("!", "")
    memory.add(messages, user_id=f"room_{safe_room_id}", metadata=metadata)
    logger.debug(f"Added memory for room {room_id}: {content[:50]}...")


def search_room_memories(query: str, room_id: str, storage_path: Path, limit: int = 3) -> list[MemoryResult]:
    """Search room memories.

    Args:
        query: Search query
        room_id: Room ID
        storage_path: Storage path for memory
        limit: Maximum number of results

    Returns:
        List of relevant memories
    """
    memory = get_memory(storage_path)
    safe_room_id = room_id.replace(":", "_").replace("!", "")
    results = memory.search(query, user_id=f"room_{safe_room_id}", limit=limit)
    logger.debug(f"Found {len(results)} memories for room {room_id}")
    return results  # type: ignore[no-any-return]


def format_memories_as_context(memories: list[MemoryResult], context_type: str = "agent") -> str:
    """Format memories into a context string.

    Args:
        memories: List of memory objects from search
        context_type: Type of context ("agent" or "room")

    Returns:
        Formatted context string
    """
    if not memories:
        return ""

    context_parts = [f"Relevant {context_type} memories:"]
    for memory in memories:
        content = memory.get("memory", "")
        context_parts.append(f"- {content}")

    return "\n".join(context_parts)
