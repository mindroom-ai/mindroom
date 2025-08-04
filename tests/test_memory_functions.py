"""Tests for memory functions."""

from unittest.mock import MagicMock, patch

import pytest

from mindroom.memory.functions import (
    MemoryResult,
    add_agent_memory,
    add_room_memory,
    build_memory_enhanced_prompt,
    format_memories_as_context,
    get_memory,
    search_agent_memories,
    store_conversation_memory,
)


class TestMemoryFunctions:
    """Test memory management functions."""

    @pytest.fixture
    def mock_memory(self):
        """Create a mock memory instance."""
        memory = MagicMock()
        memory.add.return_value = None
        memory.search.return_value = {"results": []}
        return memory

    @pytest.fixture
    def storage_path(self, tmp_path):
        """Create a temporary storage path."""
        return tmp_path

    def test_get_memory_singleton(self, mock_memory, storage_path):
        """Test that get_memory returns singleton instance."""
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            # First call creates instance
            memory1 = get_memory(storage_path)
            assert memory1 == mock_memory

            # Second call returns same instance
            memory2 = get_memory(storage_path)
            assert memory2 == memory1

            # create_memory_instance should only be called once
            mock_create.assert_called_once_with(storage_path)

    def test_add_agent_memory(self, mock_memory, storage_path):
        """Test adding agent memory."""
        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            add_agent_memory("Test memory content", "test_agent", storage_path, metadata={"test": "value"})

            # Verify memory.add was called correctly
            mock_memory.add.assert_called_once()
            call_args = mock_memory.add.call_args

            # Check messages format
            messages = call_args[0][0]
            assert messages == [{"role": "assistant", "content": "Test memory content"}]

            # Check user_id and metadata
            assert call_args[1]["user_id"] == "agent_test_agent"
            assert call_args[1]["metadata"]["agent"] == "test_agent"
            assert call_args[1]["metadata"]["test"] == "value"

    def test_add_agent_memory_error_handling(self, mock_memory, storage_path):
        """Test error handling in add_agent_memory."""
        mock_memory.add.side_effect = Exception("Memory error")

        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            # Should not raise, just log error
            add_agent_memory("Test content", "test_agent", storage_path)

    def test_search_agent_memories(self, mock_memory, storage_path):
        """Test searching agent memories."""
        # Mock search results
        mock_results = [
            {"id": "1", "memory": "Previous calculation: 2+2=4", "score": 0.9, "metadata": {"agent": "calculator"}}
        ]
        mock_memory.search.return_value = {"results": mock_results}

        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            results = search_agent_memories("calculation", "calculator", storage_path, limit=5)

            # Verify search was called correctly
            mock_memory.search.assert_called_once_with("calculation", user_id="agent_calculator", limit=5)

            # Verify results
            assert results == mock_results

    def test_search_agent_memories_handles_dict_response(self, mock_memory, storage_path):
        """Test that search handles dict response with 'results' key."""
        # This tests the bug we found where Mem0 returns dict not list
        mock_memory.search.return_value = {"results": [{"memory": "test"}]}

        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            results = search_agent_memories("query", "agent", storage_path)
            assert results == [{"memory": "test"}]

    def test_search_agent_memories_handles_list_response(self, mock_memory, storage_path):
        """Test that search handles direct list response."""
        # In case Mem0 API changes to return list directly
        mock_memory.search.return_value = [{"memory": "test"}]

        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            results = search_agent_memories("query", "agent", storage_path)
            assert results == []  # Current implementation expects dict

    def test_add_room_memory(self, mock_memory, storage_path):
        """Test adding room memory."""
        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            add_room_memory(
                "Room discussion content", "!room:server", storage_path, agent_name="helper", metadata={"topic": "math"}
            )

            # Verify memory.add was called
            call_args = mock_memory.add.call_args

            # Check room_id sanitization
            assert call_args[1]["user_id"] == "room_room_server"
            assert call_args[1]["metadata"]["room_id"] == "!room:server"
            assert call_args[1]["metadata"]["contributed_by"] == "helper"
            assert call_args[1]["metadata"]["topic"] == "math"

    def test_format_memories_as_context(self):
        """Test formatting memories into context string."""
        memories: list[MemoryResult] = [
            {"memory": "First memory", "id": "1"},  # type: ignore
            {"memory": "Second memory", "id": "2"},  # type: ignore
        ]

        context = format_memories_as_context(memories, "agent")

        expected = "[Automatically extracted agent memories - may not be relevant to current context]\nPrevious agent memories that might be related:\n- First memory\n- Second memory"
        assert context == expected

    def test_format_memories_as_context_empty(self):
        """Test formatting empty memories."""
        context = format_memories_as_context([], "room")
        assert context == ""

    def test_build_memory_enhanced_prompt(self, mock_memory, storage_path):
        """Test building memory-enhanced prompts."""
        # Mock search results
        agent_memories = [{"memory": "I previously calculated 2+2=4", "id": "1"}]
        room_memories = [{"memory": "We discussed math earlier", "id": "2"}]

        mock_memory.search.side_effect = [{"results": agent_memories}, {"results": room_memories}]

        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            enhanced = build_memory_enhanced_prompt("What is 3+3?", "calculator", storage_path, room_id="!room:server")

            # Should include both contexts
            assert "[Automatically extracted agent memories - may not be relevant to current context]" in enhanced
            assert "I previously calculated 2+2=4" in enhanced
            assert "[Automatically extracted room memories - may not be relevant to current context]" in enhanced
            assert "We discussed math earlier" in enhanced
            assert "What is 3+3?" in enhanced

    def test_build_memory_enhanced_prompt_no_memories(self, mock_memory, storage_path):
        """Test prompt enhancement with no memories found."""
        mock_memory.search.return_value = {"results": []}

        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            enhanced = build_memory_enhanced_prompt("Original prompt", "agent", storage_path)

            # Should return original prompt unchanged
            assert enhanced == "Original prompt"

    def test_store_conversation_memory(self, mock_memory, storage_path):
        """Test storing conversation memory."""
        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            store_conversation_memory(
                "What is 2+2?", "The answer is 4", "calculator", storage_path, "session123", room_id="!room:server"
            )

            # Should be called twice (agent and room)
            assert mock_memory.add.call_count == 2

            # Check agent memory call
            agent_call = mock_memory.add.call_args_list[0]
            agent_messages = agent_call[0][0]
            assert "User asked: What is 2+2?" in agent_messages[0]["content"]
            assert "I responded: The answer is 4" in agent_messages[0]["content"]
            assert agent_call[1]["user_id"] == "agent_calculator"

            # Check room memory call
            room_call = mock_memory.add.call_args_list[1]
            room_messages = room_call[0][0]
            assert "calculator discussed: The answer is 4" in room_messages[0]["content"]
            assert room_call[1]["user_id"] == "room_room_server"

    def test_store_conversation_memory_no_response(self, mock_memory, storage_path):
        """Test that empty responses are not stored."""
        with patch("mindroom.memory.functions.get_memory", return_value=mock_memory):
            store_conversation_memory(
                "Question",
                "",  # Empty response
                "agent",
                storage_path,
                "session123",
            )

            # Should not call add
            mock_memory.add.assert_not_called()

    def test_memory_result_typed_dict(self):
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
