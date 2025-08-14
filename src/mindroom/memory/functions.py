"""Simple memory management functions following Mem0 patterns."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from mindroom.logging_config import get_logger

from .config import create_memory_instance

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config import Config


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


async def add_agent_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    metadata: dict | None = None,
) -> None:
    """Add a memory for an agent.

    Args:
        content: The memory content to store
        agent_name: Name of the agent
        storage_path: Storage path for memory
        config: Application configuration
        metadata: Optional metadata to store with memory

    """
    memory = await create_memory_instance(storage_path, config)

    if metadata is None:
        metadata = {}
    metadata["agent"] = agent_name

    messages = [{"role": "user", "content": content}]

    # Use agent_name as user_id to namespace memories per agent
    try:
        await memory.add(messages, user_id=f"agent_{agent_name}", metadata=metadata)
        logger.info("Memory added", agent=agent_name)
    except Exception as e:
        logger.exception("Failed to add memory", agent=agent_name, error=str(e))


async def search_agent_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    limit: int = 3,
) -> list[MemoryResult]:
    """Search agent memories.

    Args:
        query: Search query
        agent_name: Name of the agent
        storage_path: Storage path for memory
        config: Application configuration
        limit: Maximum number of results

    Returns:
        List of relevant memories

    """
    memory = await create_memory_instance(storage_path, config)
    search_result = await memory.search(query, user_id=f"agent_{agent_name}", limit=limit)

    results = search_result["results"] if isinstance(search_result, dict) and "results" in search_result else []

    logger.debug("Memories found", count=len(results), agent=agent_name)
    return results


async def add_room_memory(
    content: str,
    room_id: str,
    storage_path: Path,
    config: Config,
    agent_name: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Add a memory for a room.

    Args:
        content: The memory content to store
        room_id: Room ID
        storage_path: Storage path for memory
        config: Application configuration
        agent_name: Optional agent that created this memory
        metadata: Optional metadata to store with memory

    """
    memory = await create_memory_instance(storage_path, config)

    if metadata is None:
        metadata = {}
    metadata["room_id"] = room_id
    if agent_name:
        metadata["contributed_by"] = agent_name

    messages = [{"role": "user", "content": content}]

    safe_room_id = room_id.replace(":", "_").replace("!", "")
    await memory.add(messages, user_id=f"room_{safe_room_id}", metadata=metadata)
    logger.debug("Room memory added", room_id=room_id)


async def search_room_memories(
    query: str,
    room_id: str,
    storage_path: Path,
    config: Config,
    limit: int = 3,
) -> list[MemoryResult]:
    """Search room memories.

    Args:
        query: Search query
        room_id: Room ID
        storage_path: Storage path for memory
        config: Application configuration
        limit: Maximum number of results

    Returns:
        List of relevant memories

    """
    memory = await create_memory_instance(storage_path, config)
    safe_room_id = room_id.replace(":", "_").replace("!", "")
    search_result = await memory.search(query, user_id=f"room_{safe_room_id}", limit=limit)

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


async def build_memory_enhanced_prompt(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    room_id: str | None = None,
) -> str:
    """Build a prompt enhanced with relevant memories.

    Args:
        prompt: The original user prompt
        agent_name: Name of the agent
        storage_path: Path for memory storage
        config: Application configuration
        room_id: Optional room ID for room context

    Returns:
        Enhanced prompt with memory context

    """
    logger.debug("Building enhanced prompt", agent=agent_name)
    enhanced_prompt = prompt

    agent_memories = await search_agent_memories(prompt, agent_name, storage_path, config)
    if agent_memories:
        agent_context = format_memories_as_context(agent_memories, "agent")
        enhanced_prompt = f"{agent_context}\n\n{prompt}"
        logger.debug("Agent memories added", count=len(agent_memories))

    if room_id:
        room_memories = await search_room_memories(prompt, room_id, storage_path, config)
        if room_memories:
            room_context = format_memories_as_context(room_memories, "room")
            enhanced_prompt = f"{room_context}\n\n{enhanced_prompt}"
            logger.debug("Room memories added", count=len(room_memories))

    return enhanced_prompt


def _build_conversation_context(
    thread_history: list[dict],
    current_prompt: str,
) -> str:
    """Build conversation context from thread history.

    Args:
        thread_history: List of messages with sender and body
        current_prompt: The current user prompt being processed

    Returns:
        Full conversation context as a single string
    """
    # Collect all messages from the thread
    context_parts = []
    
    for msg in thread_history:
        body = msg.get("body", "").strip()
        if body:
            context_parts.append(body)
    
    # Add the current prompt
    context_parts.append(current_prompt)
    
    # Join with clear separation
    return "\n\n".join(context_parts)


async def store_conversation_memory(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    session_id: str,
    config: Config,
    room_id: str | None = None,
    thread_history: list[dict] | None = None,
) -> None:
    """Store conversation in memory for future recall.

    Uses mem0's intelligent extraction to identify relevant facts, preferences,
    and context from the conversation. Provides full conversation context when
    available to allow better understanding of user intent.

    Args:
        prompt: The current user prompt
        agent_name: Name of the agent
        storage_path: Path for memory storage
        session_id: Session ID for the conversation
        config: Application configuration
        room_id: Optional room ID for room memory
        thread_history: Optional thread history for context

    """
    if not prompt:
        return

    # Build the full conversation context
    if thread_history:
        # Include thread history for better context
        full_context = _build_conversation_context(thread_history, prompt)
    else:
        # Just the current prompt
        full_context = prompt

    # Let mem0 intelligently extract facts from the conversation
    # We pass it as a single user message and let mem0 decide what's important
    await add_agent_memory(
        full_context,
        agent_name,
        storage_path,
        config,
        metadata={"type": "conversation", "session_id": session_id},
    )

    if room_id:
        # Also store for room context
        await add_room_memory(
            full_context,
            room_id,
            storage_path,
            config,
            agent_name=agent_name,
            metadata={"type": "conversation", "session_id": session_id},
        )
