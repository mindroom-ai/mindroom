"""Shared helpers for memory tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.memory.shared import MemoryResult


class MockTeamConfig:
    """Mock team configuration for tests."""

    def __init__(self, agents: list[str]) -> None:
        self.agents = agents


class FakeMem0ScopedMemory:
    """Small fake mem0 store used by backend tests."""

    def __init__(self, *, id_prefix: str = "mem") -> None:
        """Initialize a fake scoped store with deterministic IDs."""
        self._id_prefix = id_prefix
        self._entries: dict[str, MemoryResult] = {}
        self._next_id = 1

    async def add(
        self,
        messages: list[dict],
        *,
        user_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Store a memory entry derived from the last message."""
        memory_id = f"{self._id_prefix}-{self._next_id}"
        self._next_id += 1
        self._entries[memory_id] = {
            "id": memory_id,
            "memory": messages[-1]["content"],
            "user_id": user_id,
            "metadata": metadata,
        }

    async def get(self, memory_id: str) -> MemoryResult | None:
        """Return one stored memory by ID."""
        return self._entries.get(memory_id)

    async def get_all(self, *, user_id: str | None = None, limit: int = 100) -> dict[str, list[MemoryResult]]:
        """Return all stored memories for a scope."""
        entries = [entry for entry in self._entries.values() if user_id is None or entry.get("user_id") == user_id]
        return {"results": entries[:limit]}

    async def search(
        self,
        query: str,
        *,
        user_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, list[MemoryResult]]:
        """Return entries with a simple substring-based score."""
        lowered = query.lower()
        entries: list[MemoryResult] = []
        for entry in self._entries.values():
            if user_id is not None and entry.get("user_id") != user_id:
                continue
            memory_text = entry["memory"]
            if lowered.split()[0] not in memory_text.lower():
                continue
            entries.append(
                {
                    **entry,
                    "score": 1.0 if lowered in memory_text.lower() else 0.5,
                },
            )
        return {"results": entries[:limit]}

    async def update(self, memory_id: str, content: str) -> None:
        """Update one stored memory entry."""
        self._entries[memory_id]["memory"] = content

    async def delete(self, memory_id: str) -> None:
        """Delete one stored memory entry."""
        self._entries.pop(memory_id, None)
