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
    delete_agent_memory,
    format_memories_as_context,
    get_agent_memory,
    list_all_agent_memories,
    search_agent_memories,
    search_room_memories,
    store_conversation_memory,
    update_agent_memory,
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

        with (
            patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory),
            pytest.raises(Exception, match="Memory error"),
        ):
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
    async def test_get_agent_memory_allows_agent_scope(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test get by ID allows memories in the caller's agent scope."""
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Own memory", "user_id": "agent_test_agent"}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            result = await get_agent_memory("mem-1", "test_agent", storage_path, config)

            assert result is not None
            assert result["id"] == "mem-1"
            mock_memory.get.assert_called_once_with("mem-1")

    @pytest.mark.asyncio
    async def test_get_agent_memory_rejects_other_agent_scope(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test get by ID rejects memories outside caller scope."""
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Other memory", "user_id": "agent_other_agent"}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            result = await get_agent_memory("mem-1", "test_agent", storage_path, config)

            assert result is None
            mock_memory.get.assert_called_once_with("mem-1")

    @pytest.mark.asyncio
    async def test_get_agent_memory_allows_team_scope(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test get by ID allows team memories for team members."""
        config.teams = {
            "test_team": MockTeamConfig(agents=["helper", "test_agent"]),
        }
        mock_memory.get.return_value = {"id": "mem-team", "memory": "Team memory", "user_id": "team_helper+test_agent"}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            result = await get_agent_memory("mem-team", "test_agent", storage_path, config)

            assert result is not None
            assert result["id"] == "mem-team"
            mock_memory.get.assert_called_once_with("mem-team")

    @pytest.mark.asyncio
    async def test_get_agent_memory_team_context_rejects_member_scope_by_default(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Team caller context should not read member agent scope by default."""
        mock_memory.get.return_value = {"id": "mem-member", "memory": "Member memory", "user_id": "agent_helper"}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            result = await get_agent_memory("mem-member", ["helper", "test_agent"], storage_path, config)

            assert result is None
            mock_memory.get.assert_called_once_with("mem-member")

    @pytest.mark.asyncio
    async def test_get_agent_memory_team_context_allows_member_scope_when_enabled(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Team caller context can read member scopes when explicitly enabled."""
        config.memory.team_reads_member_memory = True
        mock_memory.get.return_value = {"id": "mem-member", "memory": "Member memory", "user_id": "agent_helper"}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            result = await get_agent_memory("mem-member", ["helper", "test_agent"], storage_path, config)

            assert result is not None
            assert result["id"] == "mem-member"
            mock_memory.get.assert_called_once_with("mem-member")

    @pytest.mark.asyncio
    async def test_update_agent_memory_rejects_other_agent_scope(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test update by ID rejects memories outside caller scope."""
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Other memory", "user_id": "agent_other_agent"}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            with pytest.raises(ValueError, match="No memory found with id=mem-1"):
                await update_agent_memory("mem-1", "Updated content", "test_agent", storage_path, config)

            mock_memory.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_agent_memory_rejects_other_agent_scope(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Test delete by ID rejects memories outside caller scope."""
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Other memory", "user_id": "agent_other_agent"}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            with pytest.raises(ValueError, match="No memory found with id=mem-1"):
                await delete_agent_memory("mem-1", "test_agent", storage_path, config)

            mock_memory.delete.assert_not_called()

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
    async def test_store_conversation_memory_respects_agent_backend_override(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Conversation storage should resolve backend from per-agent override."""
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            await store_conversation_memory(
                "What is 2+2?",
                "calculator",
                storage_path,
                "session123",
                config,
            )

        mock_create.assert_called_once_with(storage_path, config)
        mock_memory.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_conversation_memory_team_uses_mem0_when_any_member_overrides(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Team conversation storage should use Mem0 when any team member resolves to Mem0."""
        config.memory.backend = "file"
        config.memory.file.path = str(storage_path / "memory-files")
        config.agents["calculator"].memory_backend = "mem0"

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            await store_conversation_memory(
                "Analyze our quarterly metrics",
                ["calculator", "finance"],
                storage_path,
                "session-team",
                config,
            )

        mock_create.assert_called_once_with(storage_path, config)
        mock_memory.add.assert_called_once()
        team_memory_file = storage_path / "memory-files" / "team_calculator+finance" / "MEMORY.md"
        assert not team_memory_file.exists()

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

    @pytest.mark.asyncio
    async def test_file_backend_add_and_list_memories(self, storage_path: Path, config: Config) -> None:
        """File backend should persist entries in MEMORY.md and list them."""
        config.memory.backend = "file"
        config.memory.file.path = str(storage_path / "memory-files")

        await add_agent_memory("User prefers concise responses", "general", storage_path, config)

        results = await list_all_agent_memories("general", storage_path, config)
        assert len(results) == 1
        assert results[0]["memory"] == "User prefers concise responses"
        assert results[0]["id"].startswith("m_")

        memory_file = storage_path / "memory-files" / "agent_general" / "MEMORY.md"
        assert memory_file.exists()
        content = memory_file.read_text(encoding="utf-8")
        assert "User prefers concise responses" in content

    @pytest.mark.asyncio
    async def test_agent_memory_backend_override_to_file_uses_file_storage(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Per-agent file override should use file storage even when global backend is mem0."""
        config.memory.backend = "mem0"
        config.memory.file.path = str(storage_path / "memory-files")
        config.agents["general"].memory_backend = "file"

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            await add_agent_memory("Remember this", "general", storage_path, config)

        mock_create.assert_not_called()
        memory_file = storage_path / "memory-files" / "agent_general" / "MEMORY.md"
        assert memory_file.exists()
        assert "Remember this" in memory_file.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_agent_memory_backend_override_to_mem0_uses_mem0_storage(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Per-agent mem0 override should use Mem0 even when global backend is file."""
        config.memory.backend = "file"
        config.agents["general"].memory_backend = "mem0"

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            await add_agent_memory("Remember this", "general", storage_path, config)

        mock_create.assert_called_once_with(storage_path, config)
        mock_memory.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_file_backend_prompt_includes_entrypoint(self, storage_path: Path, config: Config) -> None:
        """File backend should include MEMORY.md entrypoint context in the prompt."""
        config.memory.backend = "file"
        config.memory.file.path = str(storage_path / "memory-files")

        memory_dir = storage_path / "memory-files" / "agent_general"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "MEMORY.md").write_text(
            "# Memory\n\nKey facts:\n- Project uses FastAPI.\n",
            encoding="utf-8",
        )

        enhanced = await build_memory_enhanced_prompt("How do we build the API?", "general", storage_path, config)
        assert "[File memory entrypoint (agent)]" in enhanced
        assert "Project uses FastAPI." in enhanced
        assert "How do we build the API?" in enhanced

    @pytest.mark.asyncio
    async def test_file_backend_room_prompt_search_uses_agent_override(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Room memory search in file prompt path should honor per-agent file overrides."""
        config.memory.backend = "mem0"
        config.memory.file.path = str(storage_path / "memory-files")
        config.agents["general"].memory_backend = "file"

        await add_room_memory(
            "Room memory note",
            "!room:server",
            storage_path,
            config,
            agent_name="general",
        )

        with patch(
            "mindroom.memory.functions.create_memory_instance",
            side_effect=AssertionError("Mem0 should not be used for file-backed agent prompt building"),
        ):
            enhanced = await build_memory_enhanced_prompt(
                "Room memory note",
                "general",
                storage_path,
                config,
                room_id="!room:server",
            )

        assert "Room memory note" in enhanced

    @pytest.mark.asyncio
    async def test_file_backend_search_skips_structured_line_duplicates(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Search should not return duplicate hits from structured ID lines in daily files."""
        config.memory.backend = "file"
        config.memory.file.path = str(storage_path / "memory-files")

        await add_agent_memory("Project owner is Bas", "general", storage_path, config)
        memories = await list_all_agent_memories("general", storage_path, config)
        memory_id = memories[0]["id"]

        daily_file = storage_path / "memory-files" / "agent_general" / "memory" / "2026-02-28.md"
        daily_file.parent.mkdir(parents=True, exist_ok=True)
        daily_file.write_text(
            f"- [id={memory_id}] Project owner is Bas\nProject owner is Bas\n",
            encoding="utf-8",
        )

        results = await search_agent_memories("owner bas", "general", storage_path, config, limit=10)
        matching_results = [r for r in results if r.get("memory") == "Project owner is Bas"]
        assert len(matching_results) == 1

    @pytest.mark.asyncio
    async def test_file_backend_memory_crud_and_scope(self, storage_path: Path, config: Config) -> None:
        """File backend should support CRUD and enforce caller scope rules."""
        config.memory.backend = "file"
        config.memory.file.path = str(storage_path / "memory-files")

        await add_agent_memory("Original memory", "general", storage_path, config)
        listed = await list_all_agent_memories("general", storage_path, config)
        memory_id = listed[0]["id"]

        # Owner can read/update/delete
        result = await get_agent_memory(memory_id, "general", storage_path, config)
        assert result is not None
        assert result["memory"] == "Original memory"

        await update_agent_memory(memory_id, "Updated memory", "general", storage_path, config)
        updated = await get_agent_memory(memory_id, "general", storage_path, config)
        assert updated is not None
        assert updated["memory"] == "Updated memory"

        await delete_agent_memory(memory_id, "general", storage_path, config)
        deleted = await get_agent_memory(memory_id, "general", storage_path, config)
        assert deleted is None

        # Different agent cannot read or mutate another scope
        await add_agent_memory("Private memory", "general", storage_path, config)
        listed_again = await list_all_agent_memories("general", storage_path, config)
        private_id = listed_again[0]["id"]
        assert await get_agent_memory(private_id, "other_agent", storage_path, config) is None
        with pytest.raises(ValueError, match=f"No memory found with id={private_id}"):
            await update_agent_memory(private_id, "Tampered", "other_agent", storage_path, config)

    @pytest.mark.asyncio
    async def test_file_backend_store_conversation_memory_with_room(self, storage_path: Path, config: Config) -> None:
        """File backend should store both agent and room memory from conversation saves."""
        config.memory.backend = "file"
        config.memory.file.path = str(storage_path / "memory-files")

        await store_conversation_memory(
            "Remember this requirement",
            "general",
            storage_path,
            "session123",
            config,
            room_id="!room:server",
        )

        agent_results = await search_agent_memories("requirement", "general", storage_path, config, limit=5)
        room_results = await search_room_memories("requirement", "!room:server", storage_path, config, limit=5)
        assert any("Remember this requirement" in r.get("memory", "") for r in agent_results)
        assert any("Remember this requirement" in r.get("memory", "") for r in room_results)

    @pytest.mark.asyncio
    async def test_file_backend_team_scopes_do_not_collide(self, storage_path: Path, config: Config) -> None:
        """Distinct team IDs should map to distinct file-memory directories."""
        config.memory.backend = "file"
        config.memory.file.path = str(storage_path / "memory-files")

        await store_conversation_memory(
            "Team one memory",
            ["a_b", "c"],
            storage_path,
            "session-one",
            config,
        )
        await store_conversation_memory(
            "Team two memory",
            ["a", "b_c"],
            storage_path,
            "session-two",
            config,
        )

        memory_root = storage_path / "memory-files"
        assert (memory_root / "team_a_b+c" / "MEMORY.md").exists()
        assert (memory_root / "team_a+b_c" / "MEMORY.md").exists()

    @pytest.mark.asyncio
    async def test_file_backend_team_context_member_scope_toggle(self, storage_path: Path, config: Config) -> None:
        """Team-context reads should honor the member-scope toggle in file backend."""
        config.memory.backend = "file"
        config.memory.file.path = str(storage_path / "memory-files")

        await add_agent_memory("Helper private memory", "helper", storage_path, config)
        helper_memories = await list_all_agent_memories("helper", storage_path, config)
        helper_memory_id = helper_memories[0]["id"]

        blocked = await get_agent_memory(helper_memory_id, ["helper", "test_agent"], storage_path, config)
        assert blocked is None

        config.memory.team_reads_member_memory = True
        allowed = await get_agent_memory(helper_memory_id, ["helper", "test_agent"], storage_path, config)
        assert allowed is not None
        assert allowed["memory"] == "Helper private memory"

    @pytest.mark.asyncio
    async def test_team_context_resolves_file_backend_from_agent_overrides(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Team-context reads should use file backend when all members override to file."""
        config.memory.backend = "mem0"
        config.memory.file.path = str(storage_path / "memory-files")
        config.memory.team_reads_member_memory = True
        config.agents["calculator"].memory_backend = "file"
        config.agents["general"].memory_backend = "file"

        await add_agent_memory("Calculator private memory", "calculator", storage_path, config)
        calculator_memories = await list_all_agent_memories("calculator", storage_path, config)
        calculator_memory_id = calculator_memories[0]["id"]

        with patch(
            "mindroom.memory.functions.create_memory_instance",
            side_effect=AssertionError("Mem0 should not be used for file-backed team context"),
        ):
            allowed = await get_agent_memory(
                calculator_memory_id,
                ["calculator", "general"],
                storage_path,
                config,
            )

        assert allowed is not None
        assert allowed["memory"] == "Calculator private memory"

    @pytest.mark.asyncio
    async def test_file_backend_rejects_path_traversal_memory_id(self, storage_path: Path, config: Config) -> None:
        """Path-based IDs should not be able to escape the scope directory."""
        config.memory.backend = "file"
        config.memory.file.path = str(storage_path / "memory-files")

        await add_agent_memory("Safe memory", "general", storage_path, config)
        secret_file = storage_path / "secret.md"
        secret_file.write_text("Do not read", encoding="utf-8")

        result = await get_agent_memory("file:../../secret.md:1", "general", storage_path, config)
        assert result is None

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
