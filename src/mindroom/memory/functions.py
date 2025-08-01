"""Simple memory management functions following Mem0 patterns."""

from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from ..logging_config import get_logger
from .config import create_memory_instance

if TYPE_CHECKING:
    from mem0 import Memory


class MemoryResult(TypedDict, total=False):
    """Type for memory search results from Mem0."""

    id: str
    memory: str
    hash: str
    metadata: dict[str, Any] | None
    score: float
    created_at: str
    updated_at: str | None
    user_id: str


logger = get_logger(__name__)

# Global memory instance cache
_memory_instance = None


def get_memory(storage_path: Path) -> "Memory":
    """Get or create the global memory instance."""
    global _memory_instance
    if _memory_instance is None:
        logger.info(f"Creating memory instance with storage path: {storage_path}")
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
    try:
        memory.add(messages, user_id=f"agent_{agent_name}", metadata=metadata)
        logger.info(f"Successfully added memory for agent {agent_name}: {content[:50]}...")
    except Exception as e:
        logger.error(f"Failed to add memory for agent {agent_name}: {e}")


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
    search_result = memory.search(query, user_id=f"agent_{agent_name}", limit=limit)

    # Extract results list from the response
    results = search_result["results"] if isinstance(search_result, dict) and "results" in search_result else []

    logger.debug(f"Found {len(results)} memories for agent {agent_name}")
    return results


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
    search_result = memory.search(query, user_id=f"room_{safe_room_id}", limit=limit)

    # Extract results list from the response
    results = search_result["results"] if isinstance(search_result, dict) and "results" in search_result else []

    logger.debug(f"Found {len(results)} memories for room {room_id}")
    return results


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


def build_memory_enhanced_prompt(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    room_id: str | None = None,
) -> str:
    """Build a prompt enhanced with relevant memories.

    Args:
        prompt: The original user prompt
        agent_name: Name of the agent
        storage_path: Path for memory storage
        room_id: Optional room ID for room context

    Returns:
        Enhanced prompt with memory context
    """
    logger.debug(f"Building memory enhanced prompt for agent {agent_name}")
    enhanced_prompt = prompt

    # Search for relevant agent memories
    agent_memories = search_agent_memories(prompt, agent_name, storage_path)
    if agent_memories:
        agent_context = format_memories_as_context(agent_memories, "agent")
        enhanced_prompt = f"{agent_context}\n\n{prompt}"
        logger.debug(f"Added {len(agent_memories)} agent memories to prompt")

    # If room_id is provided, add room context
    if room_id:
        room_memories = search_room_memories(prompt, room_id, storage_path)
        if room_memories:
            room_context = format_memories_as_context(room_memories, "room")
            enhanced_prompt = f"{room_context}\n\n{enhanced_prompt}"
            logger.debug(f"Added {len(room_memories)} room memories to prompt")

    return enhanced_prompt


def store_conversation_memory(
    prompt: str,
    response: str,
    agent_name: str,
    storage_path: Path,
    session_id: str,
    room_id: str | None = None,
) -> None:
    """Store conversation in memory for future recall.

    Args:
        prompt: The user's prompt
        response: The agent's response
        agent_name: Name of the agent
        storage_path: Path for memory storage
        session_id: Session ID for the conversation
        room_id: Optional room ID for room memory
    """
    if not response:
        return

    # Store the full conversation in agent memory
    conversation_summary = f"User asked: {prompt} I responded: {response}"
    add_agent_memory(
        conversation_summary,
        agent_name,
        storage_path,
        metadata={"type": "conversation", "session_id": session_id},
    )

    # Also add to room memory if room_id provided
    if room_id:
        room_summary = f"{agent_name} discussed: {response}"
        add_room_memory(
            room_summary,
            room_id,
            storage_path,
            agent_name=agent_name,
            metadata={"type": "conversation", "user_prompt": prompt},
        )
