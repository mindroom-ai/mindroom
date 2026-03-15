"""Shared memory types and constants."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, TypedDict
from uuid import uuid4

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


class MemoryResult(TypedDict, total=False):
    """Type for memory search results from the configured backend."""

    id: str
    memory: str
    hash: str
    metadata: dict[str, Any] | None
    score: float
    created_at: str
    updated_at: str | None
    user_id: str


class ScopedMemoryWriter(Protocol):
    """Minimal protocol for writing scoped memory entries."""

    async def add(
        self,
        messages: list[dict],
        *,
        user_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> object:
        """Persist messages for a scoped memory user ID."""


class ScopedMemoryCrud(ScopedMemoryWriter, Protocol):
    """Minimal protocol for mem0 CRUD operations used by this package."""

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        """Return the memory payload for a given memory ID."""

    async def get_all(
        self,
        *,
        user_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, list[MemoryResult]]:
        """List memories for one scoped user ID."""

    async def update(self, memory_id: str, data: str) -> object:
        """Update one memory by its backend-native ID."""

    async def delete(self, memory_id: str) -> object:
        """Delete one memory by its backend-native ID."""

    async def search(
        self,
        query: str,
        *,
        user_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, list[MemoryResult]] | list[MemoryResult]:
        """Search memories for one scoped user ID."""


class MemoryNotFoundError(ValueError):
    """Raised when a memory ID does not exist in the caller's allowed scope."""

    def __init__(self, memory_id: str) -> None:
        super().__init__(f"No memory found with id={memory_id}")


@dataclass(frozen=True)
class FileMemoryResolution:
    """Resolved file-memory storage settings for a specific caller/context."""

    storage_path: Path
    runtime_paths: RuntimePaths
    use_configured_path: bool
    agent_memory_scope_path: Path | None = None


FILE_MEMORY_DEFAULT_DIRNAME = "memory_files"
FILE_MEMORY_ENTRYPOINT = "MEMORY.md"
FILE_MEMORY_DAILY_DIR = "memory"
FILE_MEMORY_ENTRY_PATTERN = re.compile(r"^- \[id=(?P<id>[^\]]+)\]\s*(?P<memory>.+?)\s*$")
FILE_MEMORY_PATH_ID_PATTERN = re.compile(r"^file:(?P<path>[^:]+):(?P<line>\d+)$")
MEM0_REPLICA_KEY = "mindroom_replica_key"


def new_memory_id() -> str:
    """Return a timestamped unique memory ID."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"m_{timestamp}_{uuid4().hex[:8]}"
