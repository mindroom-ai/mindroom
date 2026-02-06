"""Tests for the explicit memory tool (MemoryTools toolkit)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config import Config
from mindroom.custom_tools.memory import MemoryTools
from mindroom.tools_metadata import TOOL_METADATA

if TYPE_CHECKING:
    from pathlib import Path


class TestMemoryTools:
    """Tests for the MemoryTools Toolkit."""

    @pytest.fixture
    def storage_path(self, tmp_path: Path) -> Path:
        """Create a temporary storage path."""
        return tmp_path

    @pytest.fixture
    def config(self) -> Config:
        """Load config for testing."""
        return Config.from_yaml()

    @pytest.fixture
    def tools(self, storage_path: Path, config: Config) -> MemoryTools:
        """Create a MemoryTools instance for testing."""
        return MemoryTools(agent_name="test_agent", storage_path=storage_path, config=config)

    @pytest.fixture
    def mock_memory(self) -> AsyncMock:
        """Create a mock memory instance."""
        memory = AsyncMock()
        memory.add.return_value = None
        memory.search.return_value = {"results": []}
        return memory

    @pytest.mark.asyncio
    async def test_add_memory(self, tools: MemoryTools) -> None:
        """Test that add_memory stores content via add_agent_memory."""
        with patch("mindroom.custom_tools.memory.add_agent_memory", new_callable=AsyncMock) as mock_add:
            result = await tools.add_memory("The user prefers dark mode")

            mock_add.assert_called_once_with(
                "The user prefers dark mode",
                "test_agent",
                tools._storage_path,
                tools._config,
                metadata={"source": "explicit_tool"},
            )
            assert "Memorized" in result
            assert "dark mode" in result

    @pytest.mark.asyncio
    async def test_add_memory_error(self, tools: MemoryTools) -> None:
        """Test that add_memory handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.add_agent_memory",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            result = await tools.add_memory("something")

            assert "Failed to store memory" in result
            assert "DB down" in result

    @pytest.mark.asyncio
    async def test_search_memories(self, tools: MemoryTools) -> None:
        """Test that search_memories calls search_agent_memories and formats results."""
        mock_results = [
            {"memory": "User likes Python", "score": 0.9},
            {"memory": "User prefers dark mode", "score": 0.8},
        ]

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=mock_results,
        ) as mock_search:
            result = await tools.search_memories("preferences", limit=3)

            mock_search.assert_called_once_with(
                "preferences",
                "test_agent",
                tools._storage_path,
                tools._config,
                limit=3,
            )
            assert "Found 2 memory(ies)" in result
            assert "User likes Python" in result
            assert "User prefers dark mode" in result

    @pytest.mark.asyncio
    async def test_search_memories_empty(self, tools: MemoryTools) -> None:
        """Test that search_memories returns a message when no results found."""
        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await tools.search_memories("nonexistent")

            assert result == "No relevant memories found."

    @pytest.mark.asyncio
    async def test_search_memories_error(self, tools: MemoryTools) -> None:
        """Test that search_memories handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Search failed"),
        ):
            result = await tools.search_memories("anything")

            assert "Failed to search memories" in result
            assert "Search failed" in result

    def test_toolkit_name(self, tools: MemoryTools) -> None:
        """Test that the toolkit is registered with the correct name."""
        assert tools.name == "memory"

    def test_toolkit_has_two_tools(self, tools: MemoryTools) -> None:
        """Test that the toolkit exposes both add_memory and search_memories."""
        # Async tools are registered in async_functions (agno Function uses .name)
        func_names = [f.name for f in tools.async_functions.values()]
        assert "add_memory" in func_names
        assert "search_memories" in func_names


class TestMemoryToolRegistration:
    """Test that the memory tool is properly registered in the metadata registry."""

    def test_memory_in_tool_metadata(self) -> None:
        """Test that memory tool appears in the metadata registry."""
        assert "memory" in TOOL_METADATA
        meta = TOOL_METADATA["memory"]
        assert meta.display_name == "Agent Memory"
        assert meta.status.value == "available"
        assert meta.setup_type.value == "none"
        assert meta.category.value == "productivity"
