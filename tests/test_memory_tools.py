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
            {"id": "abc-1", "memory": "User likes Python", "score": 0.9},
            {"id": "abc-2", "memory": "User prefers dark mode", "score": 0.8},
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
            assert "[id=abc-1]" in result
            assert "User likes Python" in result
            assert "[id=abc-2]" in result
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

    def test_toolkit_has_six_tools(self, tools: MemoryTools) -> None:
        """Test that the toolkit exposes all memory tools."""
        func_names = [f.name for f in tools.async_functions.values()]
        assert "add_memory" in func_names
        assert "search_memories" in func_names
        assert "list_memories" in func_names
        assert "get_memory" in func_names
        assert "update_memory" in func_names
        assert "delete_memory" in func_names

    @pytest.mark.asyncio
    async def test_list_memories(self, tools: MemoryTools) -> None:
        """Test that list_memories calls list_all_agent_memories and formats results."""
        mock_results = [
            {"id": "m1", "memory": "User likes Python"},
            {"id": "m2", "memory": "User prefers dark mode"},
            {"id": "m3", "memory": "Project uses FastAPI"},
        ]

        with patch(
            "mindroom.custom_tools.memory.list_all_agent_memories",
            new_callable=AsyncMock,
            return_value=mock_results,
        ) as mock_list:
            result = await tools.list_memories(limit=10)

            mock_list.assert_called_once_with(
                "test_agent",
                tools._storage_path,
                tools._config,
                limit=10,
            )
            assert "All memories (3)" in result
            assert "[id=m1]" in result
            assert "User likes Python" in result
            assert "[id=m2]" in result
            assert "User prefers dark mode" in result
            assert "[id=m3]" in result
            assert "Project uses FastAPI" in result

    @pytest.mark.asyncio
    async def test_list_memories_empty(self, tools: MemoryTools) -> None:
        """Test that list_memories returns a message when no memories exist."""
        with patch(
            "mindroom.custom_tools.memory.list_all_agent_memories",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await tools.list_memories()

            assert result == "No memories stored yet."

    @pytest.mark.asyncio
    async def test_list_memories_error(self, tools: MemoryTools) -> None:
        """Test that list_memories handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.list_all_agent_memories",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            result = await tools.list_memories()

            assert "Failed to list memories" in result
            assert "DB down" in result

    @pytest.mark.asyncio
    async def test_get_memory(self, tools: MemoryTools) -> None:
        """Test that get_memory retrieves a single memory by ID."""
        mock_result = {"id": "abc-123", "memory": "User likes Python"}

        with patch(
            "mindroom.custom_tools.memory.get_agent_memory",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_get:
            result = await tools.get_memory("abc-123")

            mock_get.assert_called_once_with("abc-123", "test_agent", tools._storage_path, tools._config)
            assert "[id=abc-123]" in result
            assert "User likes Python" in result

    @pytest.mark.asyncio
    async def test_get_memory_not_found(self, tools: MemoryTools) -> None:
        """Test that get_memory returns a message when memory not found."""
        with patch(
            "mindroom.custom_tools.memory.get_agent_memory",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await tools.get_memory("nonexistent")

            assert "No memory found" in result

    @pytest.mark.asyncio
    async def test_get_memory_error(self, tools: MemoryTools) -> None:
        """Test that get_memory handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.get_agent_memory",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            result = await tools.get_memory("abc-123")

            assert "Failed to get memory" in result
            assert "DB down" in result

    @pytest.mark.asyncio
    async def test_update_memory(self, tools: MemoryTools) -> None:
        """Test that update_memory updates a memory by ID."""
        with patch(
            "mindroom.custom_tools.memory.update_agent_memory",
            new_callable=AsyncMock,
        ) as mock_update:
            result = await tools.update_memory("abc-123", "Updated content")

            mock_update.assert_called_once_with(
                "abc-123",
                "Updated content",
                "test_agent",
                tools._storage_path,
                tools._config,
            )
            assert "Updated memory" in result
            assert "[id=abc-123]" in result
            assert "Updated content" in result

    @pytest.mark.asyncio
    async def test_update_memory_error(self, tools: MemoryTools) -> None:
        """Test that update_memory handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.update_agent_memory",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Not found"),
        ):
            result = await tools.update_memory("abc-123", "new content")

            assert "Failed to update memory" in result
            assert "Not found" in result

    @pytest.mark.asyncio
    async def test_delete_memory(self, tools: MemoryTools) -> None:
        """Test that delete_memory deletes a memory by ID."""
        with patch(
            "mindroom.custom_tools.memory.delete_agent_memory",
            new_callable=AsyncMock,
        ) as mock_delete:
            result = await tools.delete_memory("abc-123")

            mock_delete.assert_called_once_with("abc-123", "test_agent", tools._storage_path, tools._config)
            assert "Deleted memory" in result
            assert "[id=abc-123]" in result

    @pytest.mark.asyncio
    async def test_delete_memory_error(self, tools: MemoryTools) -> None:
        """Test that delete_memory handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.delete_agent_memory",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Not found"),
        ):
            result = await tools.delete_memory("abc-123")

            assert "Failed to delete memory" in result
            assert "Not found" in result


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
