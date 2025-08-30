"""Integration tests for memory-enhanced AI responses."""

from __future__ import annotations

from collections.abc import Generator  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.ai import ai_response
from mindroom.config import Config

if TYPE_CHECKING:
    from pathlib import Path


class TestMemoryIntegration:
    """Test memory integration with AI responses."""

    @pytest.fixture
    def mock_agent_run(self) -> AsyncMock:
        """Mock the agent run function."""
        mock = AsyncMock()
        mock.return_value = MagicMock(content="Test response")
        return mock

    @pytest.fixture
    def mock_memory_functions(self) -> Generator[AsyncMock, None, None]:
        """Mock memory enhancement function."""
        with patch("mindroom.ai.build_memory_enhanced_prompt", new_callable=AsyncMock) as mock_build:
            # Set up async side effects
            async def build_side_effect(prompt: str, *_args: object, **_kwargs: dict[str, object]) -> str:
                return f"[Enhanced] {prompt}"

            mock_build.side_effect = build_side_effect
            yield mock_build

    @pytest.fixture
    def config(self) -> Config:
        """Load config for testing."""
        return Config.from_yaml()

    @pytest.mark.asyncio
    async def test_ai_response_with_memory(
        self,
        mock_agent_run: AsyncMock,
        mock_memory_functions: AsyncMock,
        tmp_path: Path,
        config: Config,
    ) -> None:
        """Test that AI response uses memory enhancement."""
        mock_build = mock_memory_functions

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

            # Note: Memory storage now happens at the bot level, not in ai_response

    @pytest.mark.asyncio
    async def test_ai_response_without_room_id(
        self,
        mock_agent_run: AsyncMock,
        mock_memory_functions: AsyncMock,
        tmp_path: Path,
        config: Config,
    ) -> None:
        """Test AI response without room context."""
        mock_build = mock_memory_functions

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

            # Note: Memory storage now happens at the bot level, not in ai_response

    @pytest.mark.asyncio
    async def test_ai_response_error_handling(self, tmp_path: Path, config: Config) -> None:
        """Test error handling in AI response."""
        # Mock memory to prevent real memory instance creation during error handling
        mock_memory = AsyncMock()
        mock_memory.search.return_value = {"results": []}

        with (
            patch("mindroom.ai.get_model_instance", side_effect=Exception("Model error")),
            patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory),
        ):
            response = await ai_response(
                agent_name="general",
                prompt="Test",
                session_id="session",
                storage_path=tmp_path,
                config=config,
            )

            # Should return user-friendly error message with the actual error
            assert "Error: Model error" in response

    @pytest.mark.asyncio
    async def test_memory_persistence_across_calls(self, tmp_path: Path, config: Config) -> None:
        """Test that memory persists across multiple AI calls."""
        # This is more of a documentation test showing expected behavior
        mock_memory = AsyncMock()

        # First call - no memories
        mock_memory.search.return_value = {"results": []}

        with (
            patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory),
            patch("mindroom.ai._cached_agent_run", AsyncMock(return_value=MagicMock(content="First response"))),
            patch("mindroom.ai.get_model_instance", return_value=MagicMock()),
            patch("mindroom.agents.create_agent", return_value=MagicMock()),
        ):
            # First interaction
            await ai_response(
                agent_name="general",
                prompt="Remember this: A=1",
                session_id="session1",
                storage_path=tmp_path,
                config=config,
            )

            # Note: Memory storage now happens at the bot level, not in ai_response
            # This test just demonstrates the memory integration with prompt enhancement

            # Reset for second call
            mock_memory.reset_mock()

            # Second call - should find previous memory (only user prompt stored)
            mock_memory.search.return_value = {"results": [{"memory": "Remember this: A=1", "id": "1"}]}

            await ai_response(
                agent_name="general",
                prompt="What is A?",
                session_id="session2",
                storage_path=tmp_path,
                config=config,
            )

            # Memory search should have been called
            mock_memory.search.assert_called_with("What is A?", user_id="agent_general", limit=3)
