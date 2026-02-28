"""Simple memory management functions following Mem0 patterns."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from mindroom.constants import resolve_config_relative_path
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


class ScopedMemoryReader(Protocol):
    """Minimal protocol for reading a memory by ID."""

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        """Return the memory payload for a given memory ID."""


class MemoryNotFoundError(ValueError):
    """Raised when a memory ID does not exist in the caller's allowed scope."""

    def __init__(self, memory_id: str) -> None:
        super().__init__(f"No memory found with id={memory_id}")


_FILE_MEMORY_DEFAULT_DIRNAME = "memory_files"
_FILE_MEMORY_ENTRYPOINT = "MEMORY.md"
_FILE_MEMORY_DAILY_DIR = "memory"
_FILE_MEMORY_ENTRY_PATTERN = re.compile(r"^- \[id=(?P<id>[^\]]+)\]\s*(?P<memory>.+?)\s*$")
_FILE_MEMORY_PATH_ID_PATTERN = re.compile(r"^file:(?P<path>[^:]+):(?P<line>\d+)$")


def _use_file_memory_backend(config: Config, *, agent_name: str | None = None) -> bool:
    if agent_name is None:
        return config.memory.backend == "file"
    return config.get_agent_memory_backend(agent_name) == "file"


def _caller_uses_file_memory_backend(config: Config, caller_context: str | list[str]) -> bool:
    if isinstance(caller_context, str):
        return _use_file_memory_backend(config, agent_name=caller_context)
    return _team_uses_file_memory_backend(config, caller_context)


def _team_uses_file_memory_backend(config: Config, agent_names: list[str]) -> bool:
    """Return whether all team members resolve to file-backed memory."""
    return all(_use_file_memory_backend(config, agent_name=agent_name) for agent_name in agent_names)


def _file_memory_root(storage_path: Path, config: Config) -> Path:
    configured_path = config.memory.file.path
    if configured_path:
        return resolve_config_relative_path(configured_path)
    return (storage_path.expanduser().resolve() / _FILE_MEMORY_DEFAULT_DIRNAME).resolve()


def _scope_dir_name(scope_user_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._+-]+", "_", scope_user_id).strip("_") or "default"


def _resolve_scope_markdown_path(scope_path: Path, relative_path: str) -> Path | None:
    candidate = (scope_path / relative_path).resolve()
    resolved_scope = scope_path.resolve()
    try:
        candidate.relative_to(resolved_scope)
    except ValueError:
        return None
    if candidate.suffix.lower() != ".md":
        return None
    return candidate


def _scope_dir(scope_user_id: str, storage_path: Path, config: Config, *, create: bool) -> Path:
    scope_path = _file_memory_root(storage_path, config) / _scope_dir_name(scope_user_id)
    if create:
        scope_path.mkdir(parents=True, exist_ok=True)
    return scope_path


def _scope_entrypoint_path(scope_user_id: str, storage_path: Path, config: Config, *, create: bool) -> Path:
    scope_path = _scope_dir(scope_user_id, storage_path, config, create=create)
    entrypoint_path = scope_path / _FILE_MEMORY_ENTRYPOINT
    if create and not entrypoint_path.exists():
        entrypoint_path.write_text("# Memory\n\n", encoding="utf-8")
    return entrypoint_path


def _scope_markdown_files(scope_path: Path) -> list[Path]:
    return sorted(
        (path for path in scope_path.rglob("*.md") if path.is_file()),
        key=lambda path: path.relative_to(scope_path).as_posix(),
    )


def _load_scope_id_entries(
    scope_user_id: str,
    storage_path: Path,
    config: Config,
) -> tuple[list[MemoryResult], dict[str, Path]]:
    scope_path = _scope_dir(scope_user_id, storage_path, config, create=False)
    if not scope_path.exists():
        return [], {}

    markdown_files = _scope_markdown_files(scope_path)
    entrypoint_path = scope_path / _FILE_MEMORY_ENTRYPOINT
    ordered_files = ([entrypoint_path] if entrypoint_path in markdown_files else []) + [
        p for p in markdown_files if p != entrypoint_path
    ]

    results: list[MemoryResult] = []
    id_to_file: dict[str, Path] = {}
    for file_path in ordered_files:
        relative_path = file_path.relative_to(scope_path).as_posix()
        for line_no, raw_line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
            match = _FILE_MEMORY_ENTRY_PATTERN.match(raw_line.strip())
            if not match:
                continue
            memory_id = match.group("id").strip()
            memory_text = match.group("memory").strip()
            if not memory_id or not memory_text:
                continue
            result: MemoryResult = {
                "id": memory_id,
                "memory": memory_text,
                "user_id": scope_user_id,
                "metadata": {"source_file": relative_path, "line": line_no},
            }
            results.append(result)
            id_to_file.setdefault(memory_id, file_path)

    return results, id_to_file


def _extract_query_tokens(query: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) > 1}


def _match_score(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    lowered = text.lower()
    overlap = sum(1 for token in query_tokens if token in lowered)
    if overlap == 0:
        return 0.0
    return overlap / len(query_tokens)


def _format_entry_line(memory_id: str, content: str) -> str:
    normalized_content = " ".join(content.strip().split())
    return f"- [id={memory_id}] {normalized_content}"


def _new_file_memory_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"m_{timestamp}_{uuid4().hex[:8]}"


def _append_scope_memory_entry(
    scope_user_id: str,
    content: str,
    storage_path: Path,
    config: Config,
    *,
    target_relative_path: str | None = None,
) -> MemoryResult:
    scope_path = _scope_dir(scope_user_id, storage_path, config, create=True)
    if target_relative_path is None:
        target_path = scope_path / _FILE_MEMORY_ENTRYPOINT
        if not target_path.exists():
            target_path.write_text("# Memory\n\n", encoding="utf-8")
    else:
        target_path = _resolve_scope_markdown_path(scope_path, target_relative_path)
        if target_path is None:
            msg = f"Invalid markdown memory path: {target_relative_path}"
            raise ValueError(msg)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            target_path.touch()

    relative_path = target_path.relative_to(scope_path).as_posix()
    memory_id = _new_file_memory_id()
    line = _format_entry_line(memory_id, content)

    text = target_path.read_text(encoding="utf-8")
    needs_separator = bool(text) and not text.endswith("\n")
    separator = "\n" if needs_separator else ""
    target_path.write_text(f"{text}{separator}{line}\n", encoding="utf-8")

    return {
        "id": memory_id,
        "memory": " ".join(content.strip().split()),
        "user_id": scope_user_id,
        "metadata": {"source_file": relative_path},
    }


def _search_scope_memory_entries(  # noqa: C901
    scope_user_id: str,
    query: str,
    storage_path: Path,
    config: Config,
    *,
    limit: int,
) -> list[MemoryResult]:
    id_entries, _ = _load_scope_id_entries(scope_user_id, storage_path, config)
    query_tokens = _extract_query_tokens(query)

    scored_entries: list[MemoryResult] = []
    seen_scored_text: set[str] = set()
    for entry in id_entries:
        text = entry.get("memory", "")
        normalized_text = text.strip().lower()
        if normalized_text in seen_scored_text:
            continue
        score = _match_score(query_tokens, text)
        if score <= 0:
            continue
        enriched = dict(entry)
        enriched["score"] = score
        scored_entries.append(cast("MemoryResult", enriched))
        if normalized_text:
            seen_scored_text.add(normalized_text)

    scored_entries.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    scored_entries = scored_entries[:limit]

    scope_path = _scope_dir(scope_user_id, storage_path, config, create=False)
    if not scope_path.exists() or limit <= len(scored_entries):
        return scored_entries

    remaining_limit = limit - len(scored_entries)
    entrypoint_path = scope_path / _FILE_MEMORY_ENTRYPOINT
    query_tokens = _extract_query_tokens(query)
    snippet_results: list[MemoryResult] = []
    existing_memory_text = {
        memory_text for entry in scored_entries if (memory_text := entry.get("memory", "").strip().lower())
    }
    for markdown_path in _scope_markdown_files(scope_path):
        if markdown_path == entrypoint_path:
            continue
        relative_path = markdown_path.relative_to(scope_path).as_posix()
        for line_no, line in enumerate(markdown_path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Structured ID entries are already indexed above; skip here to avoid duplicates.
            if _FILE_MEMORY_ENTRY_PATTERN.match(stripped):
                continue
            normalized_stripped = stripped.lower()
            if normalized_stripped in existing_memory_text:
                continue
            score = _match_score(query_tokens, stripped)
            if score <= 0:
                continue
            existing_memory_text.add(normalized_stripped)
            snippet_results.append(
                {
                    "id": f"file:{relative_path}:{line_no}",
                    "memory": stripped,
                    "user_id": scope_user_id,
                    "score": score,
                    "metadata": {"source_file": relative_path, "line": line_no},
                },
            )

    snippet_results.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    return scored_entries + snippet_results[:remaining_limit]


def _get_scope_memory_by_path_id(
    scope_user_id: str,
    memory_id: str,
    storage_path: Path,
    config: Config,
) -> MemoryResult | None:
    scope_path = _scope_dir(scope_user_id, storage_path, config, create=False)
    match = _FILE_MEMORY_PATH_ID_PATTERN.match(memory_id)
    if not scope_path.exists() or match is None:
        return None

    path = _resolve_scope_markdown_path(scope_path, match.group("path"))
    if path is None or not path.is_file():
        return None

    line_no = int(match.group("line"))
    lines = path.read_text(encoding="utf-8").splitlines()
    if line_no < 1 or line_no > len(lines):
        return None

    content = lines[line_no - 1].strip()
    if not content:
        return None

    return {
        "id": memory_id,
        "memory": content,
        "user_id": scope_user_id,
        "metadata": {"source_file": path.relative_to(scope_path).as_posix(), "line": line_no},
    }


def _get_scope_memory_by_id(
    scope_user_id: str,
    memory_id: str,
    storage_path: Path,
    config: Config,
) -> MemoryResult | None:
    if _FILE_MEMORY_PATH_ID_PATTERN.match(memory_id):
        return _get_scope_memory_by_path_id(scope_user_id, memory_id, storage_path, config)

    entries, _ = _load_scope_id_entries(scope_user_id, storage_path, config)
    for entry in entries:
        if entry.get("id") == memory_id:
            return entry
    return None


def _replace_scope_memory_entry(
    scope_user_id: str,
    memory_id: str,
    content: str | None,
    storage_path: Path,
    config: Config,
) -> bool:
    _, id_to_file = _load_scope_id_entries(scope_user_id, storage_path, config)
    target_file = id_to_file.get(memory_id)
    if target_file is None:
        return False

    found = False
    updated_lines: list[str] = []
    for line in target_file.read_text(encoding="utf-8").splitlines():
        match = _FILE_MEMORY_ENTRY_PATTERN.match(line.strip())
        if not match or match.group("id").strip() != memory_id:
            updated_lines.append(line)
            continue
        found = True
        if content is not None:
            updated_lines.append(_format_entry_line(memory_id, content))

    if not found:
        return False

    final_text = "\n".join(updated_lines).rstrip("\n")
    if final_text:
        final_text = f"{final_text}\n"
    target_file.write_text(final_text, encoding="utf-8")
    return True


def _load_scope_entrypoint_context(scope_user_id: str, storage_path: Path, config: Config) -> str:
    entrypoint_path = _scope_entrypoint_path(scope_user_id, storage_path, config, create=False)
    if not entrypoint_path.is_file():
        return ""

    max_lines = config.memory.file.max_entrypoint_lines
    lines = entrypoint_path.read_text(encoding="utf-8").splitlines()
    if max_lines < len(lines):
        lines = lines[:max_lines]
    return "\n".join(lines).strip()


def _build_team_user_id(agent_names: list[str]) -> str:
    """Build a canonical team user_id from agent names."""
    return f"team_{'+'.join(sorted(agent_names))}"


def _get_allowed_memory_user_ids(caller_context: str | list[str], config: Config) -> set[str]:
    """Get all user_id scopes the caller is allowed to access."""
    if isinstance(caller_context, list):
        allowed_user_ids = {_build_team_user_id(caller_context)}
        if config.memory.team_reads_member_memory:
            allowed_user_ids.update(f"agent_{agent_name}" for agent_name in caller_context)
        return allowed_user_ids

    allowed_user_ids = {f"agent_{caller_context}"}
    allowed_user_ids.update(get_team_ids_for_agent(caller_context, config))
    return allowed_user_ids


async def _get_scoped_memory_by_id(
    memory: ScopedMemoryReader,
    memory_id: str,
    caller_context: str | list[str],
    config: Config,
) -> MemoryResult | None:
    """Fetch a memory and ensure it belongs to the caller's allowed scopes."""
    result = await memory.get(memory_id)
    if not isinstance(result, dict):
        return None

    allowed_user_ids = _get_allowed_memory_user_ids(caller_context, config)
    memory_user_id = result.get("user_id")
    if memory_user_id not in allowed_user_ids:
        logger.warning(
            "Memory access denied",
            memory_id=memory_id,
            memory_user_id=memory_user_id,
            allowed_user_ids=sorted(allowed_user_ids),
        )
        return None

    return cast("MemoryResult", result)


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
    if _use_file_memory_backend(config, agent_name=agent_name):
        _append_scope_memory_entry(f"agent_{agent_name}", content, storage_path, config)
        logger.info("File memory added", agent=agent_name)
        return

    memory = await create_memory_instance(storage_path, config)

    if metadata is None:
        metadata = {}
    metadata["agent"] = agent_name

    messages = [{"role": "user", "content": content}]

    # Use agent_name as user_id to namespace memories per agent
    try:
        await memory.add(messages, user_id=f"agent_{agent_name}", metadata=metadata)
        logger.info("Memory added", agent=agent_name)
    except Exception:
        logger.exception("Failed to add memory", agent=agent_name)
        raise


def append_agent_daily_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
) -> MemoryResult:
    """Append one memory entry to today's per-agent daily memory file."""
    current_date = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    daily_relative_path = f"{_FILE_MEMORY_DAILY_DIR}/{current_date}.md"
    result = _append_scope_memory_entry(
        f"agent_{agent_name}",
        content,
        storage_path,
        config,
        target_relative_path=daily_relative_path,
    )
    logger.info("File daily memory added", agent=agent_name, date=current_date)
    return result


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
    if _use_file_memory_backend(config, agent_name=agent_name):
        results = _search_scope_memory_entries(f"agent_{agent_name}", query, storage_path, config, limit=limit)
        existing_memories = {r.get("memory", "") for r in results}
        for team_id in get_team_ids_for_agent(agent_name, config):
            team_results = _search_scope_memory_entries(team_id, query, storage_path, config, limit=limit)
            for mem in team_results:
                memory_text = mem.get("memory", "")
                if memory_text in existing_memories:
                    continue
                existing_memories.add(memory_text)
                results.append(mem)
        results.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
        return results[:limit]

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


async def list_all_agent_memories(
    agent_name: str,
    storage_path: Path,
    config: Config,
    limit: int = 100,
) -> list[MemoryResult]:
    """List all memories for an agent.

    Args:
        agent_name: Name of the agent
        storage_path: Storage path for memory
        config: Application configuration
        limit: Maximum number of memories to return

    Returns:
        List of all agent memories

    """
    if _use_file_memory_backend(config, agent_name=agent_name):
        results, _ = _load_scope_id_entries(f"agent_{agent_name}", storage_path, config)
        return results[:limit]

    memory = await create_memory_instance(storage_path, config)
    result = await memory.get_all(user_id=f"agent_{agent_name}", limit=limit)
    return result["results"] if isinstance(result, dict) and "results" in result else []


async def get_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> MemoryResult | None:
    """Get a single memory by ID.

    Args:
        memory_id: The memory ID to retrieve
        caller_context: Agent name or team members requesting this memory
        storage_path: Storage path for memory
        config: Application configuration

    Returns:
        The memory dict, or None if not found

    """
    if _caller_uses_file_memory_backend(config, caller_context):
        for scope_user_id in sorted(_get_allowed_memory_user_ids(caller_context, config)):
            result = _get_scope_memory_by_id(scope_user_id, memory_id, storage_path, config)
            if result is not None:
                return result
        return None

    memory = await create_memory_instance(storage_path, config)
    return await _get_scoped_memory_by_id(memory, memory_id, caller_context, config)


async def update_agent_memory(
    memory_id: str,
    content: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> None:
    """Update a single memory by ID.

    Args:
        memory_id: The memory ID to update
        content: The new content for the memory
        caller_context: Agent name or team members requesting this update
        storage_path: Storage path for memory
        config: Application configuration

    """
    if _caller_uses_file_memory_backend(config, caller_context):
        for scope_user_id in sorted(_get_allowed_memory_user_ids(caller_context, config)):
            if _replace_scope_memory_entry(scope_user_id, memory_id, content, storage_path, config):
                logger.info("File memory updated", memory_id=memory_id, scope=scope_user_id)
                return
        raise MemoryNotFoundError(memory_id)

    memory = await create_memory_instance(storage_path, config)
    scoped_memory = await _get_scoped_memory_by_id(memory, memory_id, caller_context, config)
    if scoped_memory is None:
        raise MemoryNotFoundError(memory_id)
    await memory.update(memory_id, content)
    logger.info("Memory updated", memory_id=memory_id)


async def delete_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> None:
    """Delete a single memory by ID.

    Args:
        memory_id: The memory ID to delete
        caller_context: Agent name or team members requesting this deletion
        storage_path: Storage path for memory
        config: Application configuration

    """
    if _caller_uses_file_memory_backend(config, caller_context):
        for scope_user_id in sorted(_get_allowed_memory_user_ids(caller_context, config)):
            if _replace_scope_memory_entry(scope_user_id, memory_id, None, storage_path, config):
                logger.info("File memory deleted", memory_id=memory_id, scope=scope_user_id)
                return
        raise MemoryNotFoundError(memory_id)

    memory = await create_memory_instance(storage_path, config)
    scoped_memory = await _get_scoped_memory_by_id(memory, memory_id, caller_context, config)
    if scoped_memory is None:
        raise MemoryNotFoundError(memory_id)
    await memory.delete(memory_id)
    logger.info("Memory deleted", memory_id=memory_id)


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
    safe_room_id = room_id.replace(":", "_").replace("!", "")
    if _use_file_memory_backend(config, agent_name=agent_name):
        _append_scope_memory_entry(f"room_{safe_room_id}", content, storage_path, config)
        logger.debug("File room memory added", room_id=room_id)
        return

    memory = await create_memory_instance(storage_path, config)

    if metadata is None:
        metadata = {}
    metadata["room_id"] = room_id
    if agent_name:
        metadata["contributed_by"] = agent_name

    messages = [{"role": "user", "content": content}]

    await memory.add(messages, user_id=f"room_{safe_room_id}", metadata=metadata)
    logger.debug("Room memory added", room_id=room_id)


async def search_room_memories(
    query: str,
    room_id: str,
    storage_path: Path,
    config: Config,
    agent_name: str | None = None,
    limit: int = 3,
) -> list[MemoryResult]:
    """Search room memories.

    Args:
        query: Search query
        room_id: Room ID
        storage_path: Storage path for memory
        config: Application configuration
        agent_name: Optional agent to resolve per-agent memory backend
        limit: Maximum number of results

    Returns:
        List of relevant memories

    """
    safe_room_id = room_id.replace(":", "_").replace("!", "")
    if _use_file_memory_backend(config, agent_name=agent_name):
        return _search_scope_memory_entries(f"room_{safe_room_id}", query, storage_path, config, limit=limit)

    memory = await create_memory_instance(storage_path, config)
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
    if _use_file_memory_backend(config, agent_name=agent_name):
        return await _build_file_memory_enhanced_prompt(prompt, agent_name, storage_path, config, room_id)

    enhanced_prompt = prompt
    agent_memories = await search_agent_memories(prompt, agent_name, storage_path, config)
    if agent_memories:
        agent_context = format_memories_as_context(agent_memories, "agent")
        enhanced_prompt = f"{agent_context}\n\n{prompt}"
        logger.debug("Agent memories added", count=len(agent_memories))

    if room_id:
        room_memories = await search_room_memories(prompt, room_id, storage_path, config, agent_name=agent_name)
        if room_memories:
            room_context = format_memories_as_context(room_memories, "room")
            enhanced_prompt = f"{room_context}\n\n{enhanced_prompt}"
            logger.debug("Room memories added", count=len(room_memories))

    return enhanced_prompt


async def _build_file_memory_enhanced_prompt(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    room_id: str | None,
) -> str:
    context_chunks: list[str] = []

    agent_entrypoint = _load_scope_entrypoint_context(f"agent_{agent_name}", storage_path, config)
    if agent_entrypoint:
        context_chunks.append(f"[File memory entrypoint (agent)]\n{agent_entrypoint}")

    agent_memories = await search_agent_memories(prompt, agent_name, storage_path, config)
    if agent_memories:
        context_chunks.append(format_memories_as_context(agent_memories, "agent file"))

    if room_id:
        safe_room_id = room_id.replace(":", "_").replace("!", "")
        room_entrypoint = _load_scope_entrypoint_context(f"room_{safe_room_id}", storage_path, config)
        if room_entrypoint:
            context_chunks.append(f"[File memory entrypoint (room)]\n{room_entrypoint}")

        room_memories = await search_room_memories(prompt, room_id, storage_path, config, agent_name=agent_name)
        if room_memories:
            context_chunks.append(format_memories_as_context(room_memories, "room file"))

    if context_chunks:
        return f"{'\n\n'.join(context_chunks)}\n\n{prompt}"
    return prompt


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


def _build_memory_messages(prompt: str, thread_history: list[dict] | None, user_id: str | None) -> list[dict]:
    if thread_history and user_id:
        return _build_conversation_messages(thread_history, prompt, user_id)
    return [{"role": "user", "content": prompt}]


def _store_file_conversation_memory(
    prompt: str,
    agent_name: str | list[str],
    storage_path: Path,
    config: Config,
    room_id: str | None,
) -> None:
    condensed_prompt = " ".join(prompt.strip().split())
    if not condensed_prompt:
        return

    if isinstance(agent_name, list):
        scope_user_id = _build_team_user_id(agent_name)
        _append_scope_memory_entry(scope_user_id, condensed_prompt, storage_path, config)
        logger.info("File team memory added", team_id=scope_user_id, members=agent_name)
    else:
        scope_user_id = f"agent_{agent_name}"
        _append_scope_memory_entry(scope_user_id, condensed_prompt, storage_path, config)
        logger.info("File memory added", agent=agent_name)

    if room_id:
        safe_room_id = room_id.replace(":", "_").replace("!", "")
        _append_scope_memory_entry(f"room_{safe_room_id}", condensed_prompt, storage_path, config)
        logger.debug("File room memory added", room_id=room_id)


async def _store_mem0_conversation_memory(
    messages: list[dict],
    agent_name: str | list[str],
    storage_path: Path,
    session_id: str,
    config: Config,
    room_id: str | None,
) -> None:
    memory = await create_memory_instance(storage_path, config)

    if isinstance(agent_name, list):
        team_id = _build_team_user_id(agent_name)
        metadata = {
            "type": "conversation",
            "session_id": session_id,
            "is_team": True,
            "team_members": agent_name,
        }
        try:
            await memory.add(messages, user_id=team_id, metadata=metadata)
            logger.info("Team memory added", team_id=team_id, members=agent_name)
        except Exception as e:
            logger.exception("Failed to add team memory", team_id=team_id, error=str(e))
    else:
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

    messages = _build_memory_messages(prompt, thread_history, user_id)

    use_file_backend = (
        _use_file_memory_backend(config, agent_name=agent_name)
        if isinstance(agent_name, str)
        else _team_uses_file_memory_backend(config, agent_name)
    )

    if use_file_backend:
        _store_file_conversation_memory(prompt, agent_name, storage_path, config, room_id)
        return

    await _store_mem0_conversation_memory(messages, agent_name, storage_path, session_id, config, room_id)
