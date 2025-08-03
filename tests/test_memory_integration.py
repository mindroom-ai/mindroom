"""Integration tests for memory-enhanced AI responses."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.ai import ai_response


class TestMemoryIntegration:
    """Test memory integration with AI responses."""

    @pytest.fixture
    def mock_agent_run(self):
        """Mock the agent run function."""
        mock = AsyncMock()
        mock.return_value = MagicMock(content="Test response")
        return mock

    @pytest.fixture
    def mock_memory_functions(self):
        """Mock all memory functions."""
        with (
            patch("mindroom.ai.build_memory_enhanced_prompt") as mock_build,
            patch("mindroom.ai.store_conversation_memory") as mock_store,
        ):
            mock_build.side_effect = lambda prompt, *args, **kwargs: f"[Enhanced] {prompt}"
            yield mock_build, mock_store

    @pytest.mark.asyncio
    async def test_ai_response_with_memory(self, mock_agent_run, mock_memory_functions, tmp_path):
        """Test that AI response uses memory enhancement."""
        mock_build, mock_store = mock_memory_functions

        with (
            patch("mindroom.ai._cached_agent_run", mock_agent_run),
            patch("mindroom.ai.get_model_instance", return_value=MagicMock()),
        ):
            response = await ai_response(
                agent_name="calculator",
                prompt="What is 2+2?",
                session_id="test_session",
                storage_path=tmp_path,
                room_id="!test:room",
            )

            # Verify response
            assert response == "Test response"

            # Verify memory enhancement was applied
            mock_build.assert_called_once_with("What is 2+2?", "calculator", tmp_path, "!test:room")

            # Verify enhanced prompt was used
            mock_agent_run.assert_called_once()
            call_args = mock_agent_run.call_args[0]
            assert call_args[1] == "[Enhanced] What is 2+2?"  # Enhanced prompt

            # Verify conversation was stored
            mock_store.assert_called_once_with(
                "What is 2+2?", "Test response", "calculator", tmp_path, "test_session", "!test:room"
            )

    @pytest.mark.asyncio
    async def test_ai_response_without_room_id(self, mock_agent_run, mock_memory_functions, tmp_path):
        """Test AI response without room context."""
        mock_build, mock_store = mock_memory_functions

        with (
            patch("mindroom.ai._cached_agent_run", mock_agent_run),
            patch("mindroom.ai.get_model_instance", return_value=MagicMock()),
        ):
            await ai_response(
                agent_name="general", prompt="Hello", session_id="test_session", storage_path=tmp_path, room_id=None
            )

            # Verify memory enhancement without room_id
            mock_build.assert_called_once_with("Hello", "general", tmp_path, None)

            # Verify storage without room_id
            mock_store.assert_called_once_with("Hello", "Test response", "general", tmp_path, "test_session", None)

    @pytest.mark.asyncio
    async def test_ai_response_error_handling(self, tmp_path):
        """Test error handling in AI response."""
        with patch("mindroom.ai.get_model_instance", side_effect=Exception("Model error")):
            response = await ai_response(agent_name="test", prompt="Test", session_id="session", storage_path=tmp_path)

            # Should return error message
            assert "Sorry, I encountered an error" in response
            assert "Model error" in response

    @pytest.mark.asyncio
    async def test_memory_persistence_across_calls(self, tmp_path):
        """Test that memory persists across multiple AI calls."""
        # This is more of a documentation test showing expected behavior
        mock_memory = MagicMock()

        # First call - no memories
        mock_memory.search.return_value = {"results": []}

        with (
            patch("mindroom.memory.functions.get_memory", return_value=mock_memory),
            patch("mindroom.ai._cached_agent_run", AsyncMock(return_value=MagicMock(content="First response"))),
            patch("mindroom.ai.get_model_instance", return_value=MagicMock()),
            patch("mindroom.ai.create_agent", return_value=MagicMock()),
        ):
            # First interaction
            await ai_response(
                agent_name="test_agent", prompt="Remember this: A=1", session_id="session1", storage_path=tmp_path
            )

            # Verify memory was stored
            assert mock_memory.add.called
            stored_content = mock_memory.add.call_args[0][0][0]["content"]
            assert "A=1" in stored_content
            assert "First response" in stored_content

            # Reset for second call
            mock_memory.reset_mock()

            # Second call - should find previous memory
            mock_memory.search.return_value = {
                "results": [{"memory": "User asked: Remember this: A=1 I responded: First response", "id": "1"}]
            }

            await ai_response(
                agent_name="test_agent", prompt="What is A?", session_id="session2", storage_path=tmp_path
            )

            # Memory search should have been called
            mock_memory.search.assert_called_with("What is A?", user_id="agent_test_agent", limit=3)
