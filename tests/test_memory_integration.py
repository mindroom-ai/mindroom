"""Integration tests for memory-enhanced AI responses."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.agent_config import load_config
from mindroom.ai import ai_response
from mindroom.background_tasks import wait_for_background_tasks


class TestMemoryIntegration:
    """Test memory integration with AI responses."""

    @pytest.fixture
    def mock_agent_run(self) -> AsyncMock:
        """Mock the agent run function."""
        mock = AsyncMock()
        mock.return_value = MagicMock(content="Test response")
        return mock

    @pytest.fixture
    def mock_memory_functions(self) -> Generator[tuple[AsyncMock, AsyncMock], None, None]:
        """Mock all memory functions."""
        with (
            patch("mindroom.ai.build_memory_enhanced_prompt", new_callable=AsyncMock) as mock_build,
            patch("mindroom.ai.store_conversation_memory", new_callable=AsyncMock) as mock_store,
        ):
            # Set up async side effects
            async def build_side_effect(prompt: str, *args: Any, **kwargs: Any) -> str:
                return f"[Enhanced] {prompt}"

            mock_build.side_effect = build_side_effect
            yield mock_build, mock_store

    @pytest.fixture
    def config(self) -> Any:
        """Load config for testing."""
        return load_config()

    @pytest.mark.asyncio
    async def test_ai_response_with_memory(
        self, mock_agent_run: AsyncMock, mock_memory_functions: tuple[AsyncMock, AsyncMock], tmp_path: Any, config: Any
    ) -> None:
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
                config=config,
                room_id="!test:room",
            )

            # Verify response
            assert response == "Test response"

            # Verify memory enhancement was applied
            mock_build.assert_called_once_with("What is 2+2?", "calculator", tmp_path, config, "!test:room")

            # Verify enhanced prompt was used
            mock_agent_run.assert_called_once()
            call_args = mock_agent_run.call_args[0]
            assert call_args[1] == "[Enhanced] What is 2+2?"  # Enhanced prompt

            await wait_for_background_tasks(timeout=1.0)

            # Verify conversation was stored
            mock_store.assert_called_once_with(
                "What is 2+2?", "calculator", tmp_path, "test_session", config, "!test:room"
            )

    @pytest.mark.asyncio
    async def test_ai_response_without_room_id(
        self, mock_agent_run: AsyncMock, mock_memory_functions: tuple[AsyncMock, AsyncMock], tmp_path: Any, config: Any
    ) -> None:
        """Test AI response without room context."""
        mock_build, mock_store = mock_memory_functions

        with (
            patch("mindroom.ai._cached_agent_run", mock_agent_run),
            patch("mindroom.ai.get_model_instance", return_value=MagicMock()),
        ):
            await ai_response(
                agent_name="general",
                prompt="Hello",
                session_id="test_session",
                storage_path=tmp_path,
                config=config,
                room_id=None,
            )

            # Verify memory enhancement without room_id
            mock_build.assert_called_once_with("Hello", "general", tmp_path, config, None)

            await wait_for_background_tasks(timeout=1.0)

            # Verify storage without room_id
            mock_store.assert_called_once_with("Hello", "general", tmp_path, "test_session", config, None)

    @pytest.mark.asyncio
    async def test_ai_response_error_handling(self, tmp_path: Any, config: Any) -> None:
        """Test error handling in AI response."""
        # Mock memory to prevent real memory instance creation during error handling
        mock_memory = AsyncMock()
        mock_memory.search.return_value = {"results": []}

        with (
            patch("mindroom.ai.get_model_instance", side_effect=Exception("Model error")),
            patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory),
        ):
            response = await ai_response(
                agent_name="general", prompt="Test", session_id="session", storage_path=tmp_path, config=config
            )

            # Should return error message
            assert "Sorry, I encountered an error" in response
            assert "Model error" in response

    @pytest.mark.asyncio
    async def test_memory_persistence_across_calls(self, tmp_path: Any, config: Any) -> None:
        """Test that memory persists across multiple AI calls."""
        # This is more of a documentation test showing expected behavior
        mock_memory = AsyncMock()

        # First call - no memories
        mock_memory.search.return_value = {"results": []}

        with (
            patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory),
            patch("mindroom.ai._cached_agent_run", AsyncMock(return_value=MagicMock(content="First response"))),
            patch("mindroom.ai.get_model_instance", return_value=MagicMock()),
            patch("mindroom.agent_config.create_agent", return_value=MagicMock()),
        ):
            # First interaction
            await ai_response(
                agent_name="general",
                prompt="Remember this: A=1",
                session_id="session1",
                storage_path=tmp_path,
                config=config,
            )

            await wait_for_background_tasks(timeout=1.0)

            # Verify memory was stored (only user prompt)
            assert mock_memory.add.called
            stored_content = mock_memory.add.call_args[0][0][0]["content"]
            assert stored_content == "Remember this: A=1"

            # Reset for second call
            mock_memory.reset_mock()

            # Second call - should find previous memory (only user prompt stored)
            mock_memory.search.return_value = {"results": [{"memory": "Remember this: A=1", "id": "1"}]}

            await ai_response(
                agent_name="general", prompt="What is A?", session_id="session2", storage_path=tmp_path, config=config
            )

            # Memory search should have been called
            mock_memory.search.assert_called_with("What is A?", user_id="agent_general", limit=3)
