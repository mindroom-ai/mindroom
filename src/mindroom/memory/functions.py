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


def get_team_ids_for_agent(agent_name: str, config: Config) -> list[str]:
    """Get all team IDs that include the specified agent.

    Args:
        agent_name: Name of the agent to find teams for
        config: Application configuration containing team definitions

    Returns:
        List of team IDs (in the format "team_agent1+agent2+...")

    """
    team_ids: list[str] = []

    if not config.teams:
        return team_ids

    for team_config in config.teams.values():
        if agent_name in team_config.agents:
            # Create the same team ID format used in storage
            sorted_agents = sorted(team_config.agents)
            team_id = f"team_{'+'.join(sorted_agents)}"
            team_ids.append(team_id)

    return team_ids


async def search_agent_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    limit: int = 3,
) -> list[MemoryResult]:
    """Search agent memories including team memories.

    Args:
        query: Search query
        agent_name: Name of the agent
        storage_path: Storage path for memory
        config: Application configuration
        limit: Maximum number of results

    Returns:
        List of relevant memories from both individual and team contexts

    """
    memory = await create_memory_instance(storage_path, config)

    # Search individual agent memories
    search_result = await memory.search(query, user_id=f"agent_{agent_name}", limit=limit)
    results = search_result["results"] if isinstance(search_result, dict) and "results" in search_result else []

    # Also search team memories
    team_ids = get_team_ids_for_agent(agent_name, config)
    for team_id in team_ids:
        team_result = await memory.search(query, user_id=team_id, limit=limit)
        team_memories = team_result["results"] if isinstance(team_result, dict) and "results" in team_result else []

        # Merge results, avoiding duplicates based on memory content
        existing_memories = {r.get("memory", "") for r in results}
        for mem in team_memories:
            if mem.get("memory", "") not in existing_memories:
                results.append(mem)

        logger.debug("Team memories found", team_id=team_id, count=len(team_memories))

    logger.debug("Total memories found", count=len(results), agent=agent_name)

    # Return top results after merging
    return results[:limit]


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


def _build_conversation_messages(
    thread_history: list[dict],
    current_prompt: str,
    user_id: str,
) -> list[dict]:
    """Build conversation messages in mem0 format from thread history.

    Args:
        thread_history: List of messages with sender and body
        current_prompt: The current user prompt being processed
        user_id: The Matrix user ID to identify user messages

    Returns:
        List of messages in mem0 format with role and content

    """
    messages = []

    # Process thread history
    for msg in thread_history:
        body = msg.get("body", "").strip()
        if not body:
            continue

        sender = msg.get("sender", "")
        # Determine role based on sender
        # If sender matches the user, it's a user message; otherwise it's assistant
        role = "user" if sender == user_id else "assistant"
        messages.append({"role": role, "content": body})

    # Add the current prompt as a user message
    messages.append({"role": "user", "content": current_prompt})

    return messages


async def store_conversation_memory(
    prompt: str,
    agent_name: str | list[str],
    storage_path: Path,
    session_id: str,
    config: Config,
    room_id: str | None = None,
    thread_history: list[dict] | None = None,
    user_id: str | None = None,
) -> None:
    """Store conversation in memory for future recall.

    Uses mem0's intelligent extraction to identify relevant facts, preferences,
    and context from the conversation. Provides full conversation context when
    available to allow better understanding of user intent.

    For teams, pass a list of agent names to store memory once under a shared
    namespace, avoiding duplicate LLM processing.

    Args:
        prompt: The current user prompt
        agent_name: Name of the agent or list of agent names for teams
        storage_path: Path for memory storage
        session_id: Session ID for the conversation
        config: Application configuration
        room_id: Optional room ID for room memory
        thread_history: Optional thread history for context
        user_id: Optional user ID to identify user messages in thread

    """
    if not prompt:
        return

    # Build conversation messages in mem0 format
    if thread_history and user_id:
        # Use structured messages with roles for better context
        messages = _build_conversation_messages(thread_history, prompt, user_id)
    else:
        # Fallback to simple user message
        messages = [{"role": "user", "content": prompt}]

    # Store for agent memory with structured messages
    memory = await create_memory_instance(storage_path, config)

    # Handle both single agents and teams
    is_team = isinstance(agent_name, list)

    if is_team:
        # For teams, store once under a team namespace
        # Sort agent names for consistent team ID
        sorted_agents = sorted(agent_name)
        team_id = f"team_{'+'.join(sorted_agents)}"

        metadata = {
            "type": "conversation",
            "session_id": session_id,
            "is_team": True,
            "team_members": agent_name,  # Keep original order for reference
        }

        try:
            await memory.add(messages, user_id=team_id, metadata=metadata)
            logger.info("Team memory added", team_id=team_id, members=agent_name)
        except Exception as e:
            logger.exception("Failed to add team memory", team_id=team_id, error=str(e))
    else:
        # Single agent - store normally
        metadata = {
            "type": "conversation",
            "session_id": session_id,
            "agent": agent_name,
        }

        try:
            await memory.add(messages, user_id=f"agent_{agent_name}", metadata=metadata)
            logger.info("Memory added", agent=agent_name)
        except Exception as e:
            logger.exception("Failed to add memory", agent=agent_name, error=str(e))

    if room_id:
        # Also store for room context
        contributed_by = agent_name if isinstance(agent_name, str) else f"team:{','.join(agent_name)}"
        room_metadata = {
            "type": "conversation",
            "session_id": session_id,
            "room_id": room_id,
            "contributed_by": contributed_by,
        }

        safe_room_id = room_id.replace(":", "_").replace("!", "")
        try:
            await memory.add(messages, user_id=f"room_{safe_room_id}", metadata=room_metadata)
            logger.debug("Room memory added", room_id=room_id)
        except Exception as e:
            logger.exception("Failed to add room memory", room_id=room_id, error=str(e))
