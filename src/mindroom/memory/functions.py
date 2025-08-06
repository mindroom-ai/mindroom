"""Simple memory management functions following Mem0 patterns."""

from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from ..logging_config import get_logger
from .config import create_memory_instance

if TYPE_CHECKING:
    from mem0 import Memory  # type: ignore[import-untyped]


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
        logger.info("Creating memory instance", path=storage_path)
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

    if metadata is None:
        metadata = {}
    metadata["agent"] = agent_name

    messages = [{"role": "assistant", "content": content}]

    # Use agent_name as user_id to namespace memories per agent
    try:
        memory.add(messages, user_id=f"agent_{agent_name}", metadata=metadata)
        logger.info("Memory added", agent=agent_name)
    except Exception as e:
        logger.error("Failed to add memory", agent=agent_name, error=str(e))


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

    results = search_result["results"] if isinstance(search_result, dict) and "results" in search_result else []

    logger.debug("Memories found", count=len(results), agent=agent_name)
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

    if metadata is None:
        metadata = {}
    metadata["room_id"] = room_id
    if agent_name:
        metadata["contributed_by"] = agent_name

    messages = [{"role": "assistant", "content": content}]

    safe_room_id = room_id.replace(":", "_").replace("!", "")
    memory.add(messages, user_id=f"room_{safe_room_id}", metadata=metadata)
    logger.debug("Room memory added", room_id=room_id)


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

    results = search_result["results"] if isinstance(search_result, dict) and "results" in search_result else []

    logger.debug("Room memories found", count=len(results), room_id=room_id)
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

    context_parts = [
        f"[Automatically extracted {context_type} memories - may not be relevant to current context]",
        f"Previous {context_type} memories that might be related:",
    ]
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
    logger.debug("Building enhanced prompt", agent=agent_name)
    enhanced_prompt = prompt

    agent_memories = search_agent_memories(prompt, agent_name, storage_path)
    if agent_memories:
        agent_context = format_memories_as_context(agent_memories, "agent")
        enhanced_prompt = f"{agent_context}\n\n{prompt}"
        logger.debug("Agent memories added", count=len(agent_memories))

    if room_id:
        room_memories = search_room_memories(prompt, room_id, storage_path)
        if room_memories:
            room_context = format_memories_as_context(room_memories, "room")
            enhanced_prompt = f"{room_context}\n\n{enhanced_prompt}"
            logger.debug("Room memories added", count=len(room_memories))

    return enhanced_prompt


def store_conversation_memory(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    session_id: str,
    room_id: str | None = None,
) -> None:
    """Store conversation in memory for future recall.

    Following mem0 best practices, only stores user prompts to allow
    intelligent extraction of relevant facts, preferences, and context.
    AI responses are not stored as they don't contain valuable user information.

    Args:
        prompt: The user's prompt
        agent_name: Name of the agent
        storage_path: Path for memory storage
        session_id: Session ID for the conversation
        room_id: Optional room ID for room memory
    """
    if not prompt:
        return

    # Store only the user's input - let mem0 extract what's valuable
    add_agent_memory(
        prompt,
        agent_name,
        storage_path,
        metadata={"type": "user_input", "session_id": session_id},
    )

    if room_id:
        # For room memory, also store user input for room context
        add_room_memory(
            prompt,
            room_id,
            storage_path,
            agent_name=agent_name,
            metadata={"type": "user_input", "session_id": session_id},
        )
