"""Shared file-memory knowledge overlay construction and agent resolution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.memory_scope_ids import agent_scope_user_id
from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.memory import MemorySearchConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_CHUNK_SIZE = 5000
_CHUNK_OVERLAP = 0
_MEMORY_KNOWLEDGE_PREFIX = "file_memory"
_FILE_MEMORY_DESCRIPTION = (
    "Configured file memory for this agent. "
    "Read-only semantic search over configured Markdown paths (default: memory/**/*.md)."
)


@dataclass(frozen=True)
class _FileMemoryKnowledgeResolution:
    """One file-memory scope's stable base ID, root, and effective config."""

    base_id: str
    root: Path
    config: Config


def _safe_identifier(value: str) -> str:
    sanitized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return sanitized or "default"


def _scope_digest(root: Path, scope_user_id: str) -> str:
    return hashlib.sha256(f"{scope_user_id}:{root.resolve()}".encode()).hexdigest()[:16]


def _memory_knowledge_base_id(root: Path, scope_user_id: str) -> str:
    return f"{_MEMORY_KNOWLEDGE_PREFIX}_{_safe_identifier(scope_user_id)}_{_scope_digest(root, scope_user_id)}"


def _memory_include_patterns(search_config: MemorySearchConfig) -> list[str]:
    patterns = list(search_config.include)
    if search_config.include_entrypoint:
        patterns.append("MEMORY.md")
    return patterns


def resolve_file_memory_knowledge(
    *,
    scope_user_id: str,
    root: Path,
    config: Config,
    search_config: MemorySearchConfig,
) -> _FileMemoryKnowledgeResolution:
    """Build the semantic knowledge overlay for one already-resolved file-memory scope."""
    resolved_root = root.resolve()
    base_id = _memory_knowledge_base_id(resolved_root, scope_user_id)
    base_config = KnowledgeBaseConfig(
        mode="semantic",
        description=_FILE_MEMORY_DESCRIPTION,
        path=str(resolved_root),
        watch=False,
        require_content_before_publish=True,
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
        include_extensions=[".md"],
        include_patterns=_memory_include_patterns(search_config),
    )
    return _FileMemoryKnowledgeResolution(
        base_id=base_id,
        root=resolved_root,
        config=config.with_runtime_knowledge_base_overlay(base_id, base_config),
    )


def resolve_agent_file_memory_knowledge(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> _FileMemoryKnowledgeResolution | None:
    """Resolve an agent's semantic file-memory overlay without widening its runtime scope."""
    if agent_name not in config.agents:
        return None

    entity = config.resolve_entity(agent_name)
    if entity.memory_backend != "file" or entity.memory_search.mode != "semantic":
        return None

    runtime = resolve_agent_runtime(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    if runtime.file_memory_root is None:
        msg = f"File-memory agent '{agent_name}' did not resolve a file-memory root"
        raise ValueError(msg)
    return resolve_file_memory_knowledge(
        scope_user_id=agent_scope_user_id(agent_name),
        root=runtime.file_memory_root,
        config=config,
        search_config=entity.memory_search,
    )
