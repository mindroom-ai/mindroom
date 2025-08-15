"""Tests for memory functions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config import Config
from mindroom.memory.functions import (
    MemoryResult,
    add_agent_memory,
    add_room_memory,
    build_memory_enhanced_prompt,
    format_memories_as_context,
    search_agent_memories,
    store_conversation_memory,
)

if TYPE_CHECKING:
    from pathlib import Path


class MockTeamConfig:
    """Mock team configuration for tests."""

    def __init__(self, agents: list[str]) -> None:
        """Initialize mock team config."""
        self.agents = agents


class TestMemoryFunctions:
    """Test memory management functions."""

    @pytest.fixture
    def mock_memory(self) -> AsyncMock:
        """Create a mock memory instance."""
        memory = AsyncMock()
        memory.add.return_value = None
        memory.search.return_value = {"results": []}
        return memory

    @pytest.fixture
    def storage_path(self, tmp_path: Path) -> Path:
        """Create a temporary storage path."""
        return tmp_path

    @pytest.fixture
    def config(self) -> Config:
        """Load config for testing."""
        return Config.from_yaml()

    @pytest.mark.asyncio
    async def test_memory_instance_creation(self, mock_memory: AsyncMock, storage_path: Path, config: Config) -> None:
        """Test that memory instances are created correctly."""
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            # Test add_agent_memory creates instance
            await add_agent_memory("Test content", "test_agent", storage_path, config)
            # The function now loads config internally, so we check the first arg
            assert mock_create.call_args[0][0] == storage_path

            # Test search_agent_memories creates instance
            await search_agent_memories("query", "test_agent", storage_path, config)
            assert mock_create.call_args[0][0] == storage_path

    @pytest.mark.asyncio
    async def test_add_agent_memory(self, mock_memory: AsyncMock, storage_path: Path, config: Config) -> None:
        """Test adding agent memory."""
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await add_agent_memory(
                "Test memory content",
                "test_agent",
                storage_path,
                config,
                metadata={"test": "value"},
            )

            # Verify memory.add was called correctly
            mock_memory.add.assert_called_once()
            call_args = mock_memory.add.call_args

            # Check messages format
            messages = call_args[0][0]
            assert messages == [{"role": "user", "content": "Test memory content"}]

            # Check user_id and metadata
            assert call_args[1]["user_id"] == "agent_test_agent"
            assert call_args[1]["metadata"]["agent"] == "test_agent"
            assert call_args[1]["metadata"]["test"] == "value"

    @pytest.mark.asyncio
    async def test_add_agent_memory_error_handling(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test error handling in add_agent_memory."""
        mock_memory.add.side_effect = Exception("Memory error")

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            # Should not raise, just log error
            await add_agent_memory("Test content", "test_agent", storage_path, config)

    @pytest.mark.asyncio
    async def test_search_agent_memories(self, mock_memory: AsyncMock, storage_path: Path, config: Config) -> None:
        """Test searching agent memories."""
        # Mock search results
        mock_results = [
            {"id": "1", "memory": "Previous calculation: 2+2=4", "score": 0.9, "metadata": {"agent": "calculator"}},
        ]
        mock_memory.search.return_value = {"results": mock_results}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories("calculation", "calculator", storage_path, config, limit=5)

            # Verify search was called correctly
            mock_memory.search.assert_called_once_with("calculation", user_id="agent_calculator", limit=5)

            # Verify results
            assert results == mock_results

    @pytest.mark.asyncio
    async def test_search_agent_memories_handles_dict_response(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test that search handles dict response with 'results' key."""
        # This tests the bug we found where Mem0 returns dict not list
        mock_memory.search.return_value = {"results": [{"memory": "test"}]}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories("query", "agent", storage_path, config)
            assert results == [{"memory": "test"}]

    @pytest.mark.asyncio
    async def test_search_agent_memories_handles_list_response(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test that search handles direct list response."""
        # In case Mem0 API changes to return list directly
        mock_memory.search.return_value = [{"memory": "test"}]

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories("query", "agent", storage_path, config)
            assert results == []  # Current implementation expects dict

    @pytest.mark.asyncio
    async def test_add_room_memory(self, mock_memory: AsyncMock, storage_path: Path, config: Config) -> None:
        """Test adding room memory."""
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await add_room_memory(
                "Room discussion content",
                "!room:server",
                storage_path,
                config,
                agent_name="helper",
                metadata={"topic": "math"},
            )

            # Verify memory.add was called
            call_args = mock_memory.add.call_args

            # Check room_id sanitization
            assert call_args[1]["user_id"] == "room_room_server"
            assert call_args[1]["metadata"]["room_id"] == "!room:server"
            assert call_args[1]["metadata"]["contributed_by"] == "helper"
            assert call_args[1]["metadata"]["topic"] == "math"

    def test_format_memories_as_context(self) -> None:
        """Test formatting memories into context string."""
        memories: list[MemoryResult] = [
            {"memory": "First memory", "id": "1"},
            {"memory": "Second memory", "id": "2"},
        ]

        context = format_memories_as_context(memories, "agent")

        expected = "[Automatically extracted agent memories - may not be relevant to current context]\nPrevious agent memories that might be related:\n- First memory\n- Second memory"
        assert context == expected

    def test_format_memories_as_context_empty(self) -> None:
        """Test formatting empty memories."""
        context = format_memories_as_context([], "room")
        assert context == ""

    @pytest.mark.asyncio
    async def test_build_memory_enhanced_prompt(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test building memory-enhanced prompts."""
        # Mock search results
        agent_memories = [{"memory": "I previously calculated 2+2=4", "id": "1"}]
        room_memories = [{"memory": "We discussed math earlier", "id": "2"}]

        mock_memory.search.side_effect = [{"results": agent_memories}, {"results": room_memories}]

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            enhanced = await build_memory_enhanced_prompt(
                "What is 3+3?",
                "calculator",
                storage_path,
                config,
                room_id="!room:server",
            )

            # Should include both contexts
            assert "[Automatically extracted agent memories - may not be relevant to current context]" in enhanced
            assert "I previously calculated 2+2=4" in enhanced
            assert "[Automatically extracted room memories - may not be relevant to current context]" in enhanced
            assert "We discussed math earlier" in enhanced
            assert "What is 3+3?" in enhanced

    @pytest.mark.asyncio
    async def test_build_memory_enhanced_prompt_no_memories(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test prompt enhancement with no memories found."""
        mock_memory.search.return_value = {"results": []}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            enhanced = await build_memory_enhanced_prompt("Original prompt", "agent", storage_path, config)

            # Should return original prompt unchanged
            assert enhanced == "Original prompt"

    @pytest.mark.asyncio
    async def test_store_conversation_memory(self, mock_memory: AsyncMock, storage_path: Path, config: Config) -> None:
        """Test storing conversation memory."""
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory(
                "What is 2+2?",
                "calculator",
                storage_path,
                "session123",
                config,
                room_id="!room:server",
            )

            # Should be called twice (agent and room)
            assert mock_memory.add.call_count == 2

            # Check agent memory call
            agent_call = mock_memory.add.call_args_list[0]
            agent_messages = agent_call[0][0]
            assert len(agent_messages) == 1
            assert agent_messages[0]["role"] == "user"
            assert agent_messages[0]["content"] == "What is 2+2?"
            assert agent_call[1]["user_id"] == "agent_calculator"
            assert agent_call[1]["metadata"]["type"] == "conversation"

            # Check room memory call
            room_call = mock_memory.add.call_args_list[1]
            room_messages = room_call[0][0]
            assert len(room_messages) == 1
            assert room_messages[0]["role"] == "user"
            assert room_messages[0]["content"] == "What is 2+2?"
            assert room_call[1]["user_id"] == "room_room_server"
            assert room_call[1]["metadata"]["type"] == "conversation"

    @pytest.mark.asyncio
    async def test_store_conversation_memory_no_prompt(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test that empty prompts are not stored."""
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory(
                "",  # Empty prompt
                "agent",
                storage_path,
                "session123",
                config,
            )

            # Should not call add when prompt is empty
            mock_memory.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_conversation_memory_with_empty_response(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test that user prompts are still stored even with empty responses."""
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory(
                "What is 2+2?",
                "calculator",
                storage_path,
                "session123",
                config,
            )

            # Should still store the user prompt
            assert mock_memory.add.call_count == 1
            agent_call = mock_memory.add.call_args_list[0]
            agent_messages = agent_call[0][0]
            assert len(agent_messages) == 1
            assert agent_messages[0]["role"] == "user"
            assert agent_messages[0]["content"] == "What is 2+2?"

    @pytest.mark.asyncio
    async def test_store_conversation_memory_with_thread_history(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test storing conversation memory with thread history and user identification."""
        thread_history = [
            {"sender": "@user:matrix.org", "body": "I need help with math"},
            {"sender": "@router:matrix.org", "body": "@calculator can help with that"},
            {"sender": "@user:matrix.org", "body": "Yes please"},
        ]

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory(
                "What is 2+2?",
                "calculator",
                storage_path,
                "session123",
                config,
                thread_history=thread_history,
                user_id="@user:matrix.org",
            )

            # Should store structured messages with roles
            assert mock_memory.add.call_count == 1
            agent_call = mock_memory.add.call_args_list[0]
            messages = agent_call[0][0]

            # Check the structured messages
            assert len(messages) == 4  # 3 from history + 1 current
            assert messages[0] == {"role": "user", "content": "I need help with math"}
            assert messages[1] == {"role": "assistant", "content": "@calculator can help with that"}
            assert messages[2] == {"role": "user", "content": "Yes please"}
            assert messages[3] == {"role": "user", "content": "What is 2+2?"}

    @pytest.mark.asyncio
    async def test_store_conversation_memory_for_team(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test storing conversation memory for a team (stores once, not per member)."""
        team_agents = ["calculator", "data_analyst", "finance"]

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory(
                "Analyze our Q4 financial data",
                team_agents,  # Pass list of agents for team
                storage_path,
                "session123",
                config,
            )

            # Should be called only ONCE for the team (not 3 times)
            assert mock_memory.add.call_count == 1

            team_call = mock_memory.add.call_args_list[0]

            # Check team user_id format (sorted for consistency)
            expected_team_id = "team_calculator+data_analyst+finance"
            assert team_call[1]["user_id"] == expected_team_id

            # Check metadata
            metadata = team_call[1]["metadata"]
            assert metadata["type"] == "conversation"
            assert metadata["is_team"] is True
            assert metadata["team_members"] == team_agents  # Original order preserved

    @pytest.mark.asyncio
    async def test_search_agent_memories_with_teams(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test that agents can find both individual and team memories."""
        # Setup config with a team
        config.teams = {
            "finance_team": MockTeamConfig(agents=["calculator", "data_analyst", "finance"]),
        }

        # Mock search results
        individual_result = {
            "results": [
                {"id": "1", "memory": "Individual fact", "score": 0.9},
            ],
        }
        team_result = {
            "results": [
                {"id": "2", "memory": "Team fact", "score": 0.85},
            ],
        }

        # Setup mock to return different results for different user_ids
        def search_side_effect(query: str, user_id: str, limit: int) -> dict:  # noqa: ARG001
            if user_id == "agent_calculator":
                return individual_result
            if user_id == "team_calculator+data_analyst+finance":
                return team_result
            return {"results": []}

        mock_memory.search = AsyncMock(side_effect=search_side_effect)

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories(
                "test query",
                "calculator",
                storage_path,
                config,
                limit=5,
            )

            # Should have both individual and team memories
            assert len(results) == 2
            assert results[0]["memory"] == "Individual fact"
            assert results[1]["memory"] == "Team fact"

            # Should have called search twice (once for agent, once for team)
            assert mock_memory.search.call_count == 2

    def test_get_team_ids_for_agent(self, config: Config) -> None:
        """Test getting team IDs for an agent."""
        from mindroom.memory.functions import get_team_ids_for_agent  # noqa: PLC0415

        # Setup config with multiple teams
        config.teams = {
            "finance_team": MockTeamConfig(agents=["calculator", "data_analyst", "finance"]),
            "science_team": MockTeamConfig(agents=["calculator", "researcher"]),
            "other_team": MockTeamConfig(agents=["general", "assistant"]),
        }

        # Calculator is in two teams
        team_ids = get_team_ids_for_agent("calculator", config)
        assert len(team_ids) == 2
        assert "team_calculator+data_analyst+finance" in team_ids
        assert "team_calculator+researcher" in team_ids

        # General is in one team
        team_ids = get_team_ids_for_agent("general", config)
        assert len(team_ids) == 1
        assert "team_assistant+general" in team_ids  # Sorted alphabetically

        # Unknown agent has no teams
        team_ids = get_team_ids_for_agent("unknown", config)
        assert len(team_ids) == 0

    def test_memory_result_typed_dict(self) -> None:
        """Test MemoryResult TypedDict structure."""
        # This is mainly for documentation, but ensures the type is importable
        result: MemoryResult = {
            "id": "123",
            "memory": "Test memory",
            "score": 0.95,
            "metadata": {"key": "value"},
        }

        # Should be valid TypedDict
        assert result["id"] == "123"
        assert result["memory"] == "Test memory"
