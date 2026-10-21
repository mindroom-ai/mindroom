"""Integration tests for memory-enhanced AI responses."""

from __future__ import annotations

from collections.abc import Generator  # noqa: TC003
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.ollama import Ollama
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom.ai import _memory_conversation, ai_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.delegation.personas import inline_persona
from mindroom.dispatch_source import ScheduledHistoryBudget
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.hooks import render_transient_context
from mindroom.memory import ConversationRef, MemoryPromptParts
from tests.conftest import make_turn_context
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


class TestMemoryIntegration:
    """Test memory integration with AI responses."""

    @staticmethod
    def _config() -> Config:
        return Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        )

    @pytest.fixture
    def mock_agent_run(self) -> AsyncMock:
        """Mock the agent run function."""
        mock = AsyncMock()
        mock.return_value = MagicMock(content="Test response")
        return mock

    @pytest.fixture
    def mock_memory_functions(self) -> Generator[AsyncMock, None, None]:
        """Mock memory prompt splitting."""
        with patch("mindroom.ai.build_memory_prompt_parts", new_callable=AsyncMock) as mock_build:
            # Set up async side effects
            async def build_side_effect(
                prompt: str,
                *_args: object,
                **_kwargs: dict[str, object],
            ) -> MemoryPromptParts:
                return MemoryPromptParts(transient_turn_context=f"[Enhanced memory] {prompt}")

            mock_build.side_effect = build_side_effect
            yield mock_build

    @pytest.fixture
    def config(self) -> Config:
        """Build the minimal config needed for memory integration tests."""
        return self._config()

    @staticmethod
    def _runtime_paths(tmp_path: Path) -> RuntimePaths:
        return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    @pytest.mark.asyncio
    async def test_ai_response_with_memory(
        self,
        mock_agent_run: AsyncMock,
        mock_memory_functions: AsyncMock,
        tmp_path: Path,
        config: Config,
    ) -> None:
        """Test that AI response uses memory enhancement and names the turn's conversation for file memory."""
        mock_build = mock_memory_functions
        config.memory.backend = "file"
        runtime_paths = self._runtime_paths(tmp_path)
        persist_entity_accounts(config, runtime_paths)

        with (
            patch("mindroom.ai_runtime.cached_agent_run", mock_agent_run),
            patch("mindroom.providers.model_loading.get_model_instance", return_value=Ollama(id="test-model")),
            patch("mindroom.ai.create_agent", return_value=MagicMock()),
        ):
            response = await ai_response(
                make_turn_context("general", session_id="test_session", room_id="!test:room"),
                prompt="What is 2+2?",
                runtime_paths=runtime_paths,
                config=config,
            )

            # Verify response
            assert response == "Test response"

            # Verify memory enhancement was applied
            mock_build.assert_called_once_with(
                "What is 2+2?",
                "general",
                tmp_path,
                config,
                runtime_paths,
                execution_identity=None,
                conversation=ConversationRef("test_session", (None, None)),
            )

            # Verify enhanced prompt was used
            mock_agent_run.assert_called_once()
            call_args = mock_agent_run.call_args[0]
            assert len(call_args[1]) == 2
            assert call_args[1][0].role == "user"
            assert call_args[1][0].content == render_transient_context(("[Enhanced memory] What is 2+2?",))
            assert call_args[1][0].add_to_agent_memory is False
            assert call_args[1][1].role == "user"
            assert call_args[1][1].content == "What is 2+2?"
            assert call_args[1][1].add_to_agent_memory is True

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
        runtime_paths = self._runtime_paths(tmp_path)

        with (
            patch("mindroom.ai_runtime.cached_agent_run", mock_agent_run),
            patch("mindroom.providers.model_loading.get_model_instance", return_value=Ollama(id="test-model")),
            patch("mindroom.ai.create_agent", return_value=MagicMock()),
        ):
            await ai_response(
                make_turn_context("general", session_id="test_session", room_id=None),
                prompt="Hello",
                runtime_paths=runtime_paths,
                config=config,
            )

            # Verify memory enhancement remains agent-scoped; mem0 inlines no MEMORY.md to keep.
            mock_build.assert_called_once_with(
                "Hello",
                "general",
                tmp_path,
                config,
                runtime_paths,
                execution_identity=None,
                conversation=None,
            )

            # Note: Memory storage now happens at the bot level, not in ai_response

    @pytest.mark.asyncio
    async def test_ai_response_error_handling(self, tmp_path: Path, config: Config) -> None:
        """Test error handling in AI response."""
        # Mock memory to prevent real memory instance creation during error handling
        mock_memory = AsyncMock()
        mock_memory.search.return_value = {"results": []}

        with (
            patch("mindroom.ai.create_agent", side_effect=Exception("Model error")),
            patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory),
        ):
            response = await ai_response(
                make_turn_context("general", session_id="session"),
                prompt="Test",
                runtime_paths=self._runtime_paths(tmp_path),
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
            patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory),
            patch("mindroom.ai_runtime.cached_agent_run", AsyncMock(return_value=MagicMock(content="First response"))),
            patch("mindroom.providers.model_loading.get_model_instance", return_value=Ollama(id="test-model")),
            patch("mindroom.ai.create_agent", return_value=MagicMock()),
        ):
            # First interaction
            await ai_response(
                make_turn_context("general", session_id="session1"),
                prompt="Remember this: A=1",
                runtime_paths=self._runtime_paths(tmp_path),
                config=config,
            )

            # Note: Memory storage now happens at the bot level, not in ai_response
            # This test just demonstrates the memory integration with prompt enhancement

            # Reset for second call
            mock_memory.reset_mock()

            # Second call - should find previous memory (only user prompt stored)
            mock_memory.search.return_value = {"results": [{"memory": "Remember this: A=1", "id": "1"}]}

            await ai_response(
                make_turn_context("general", session_id="session2"),
                prompt="What is A?",
                runtime_paths=self._runtime_paths(tmp_path),
                config=config,
            )

            # Memory search should have been called
            mock_memory.search.assert_called_with("What is A?", filters={"user_id": "agent_general"}, top_k=3)


@pytest.mark.parametrize("history_limit", [{"num_history_runs": 5}, {"max_tool_calls_from_history": 3}])
def test_memory_conversation_skips_turns_without_a_cached_system_prompt_to_keep(history_limit: dict[str, int]) -> None:
    """Only file memory inlines MEMORY.md; a persona recalls none, a minimal agent reads it on demand, and a replay limit shifts the history."""
    session = AgentSession(session_id="thread", summary=SessionSummary(summary=" Earlier work. "))
    scope_context = ScopeSessionContext(
        scope=HistoryScope("agent", "general"),
        storage=MagicMock(),
        session=session,
        session_id="thread",
    )
    ctx = make_turn_context("general", session_id="thread")
    mem0 = TestMemoryIntegration._config()
    config = Config(
        agents={"general": AgentConfig(display_name="General")},
        models={"default": ModelConfig(provider="openai", id="test-model")},
        memory={"backend": "file"},
    )
    limited = Config(
        agents={"general": AgentConfig(display_name="General", **history_limit)},
        models={"default": ModelConfig(provider="openai", id="test-model")},
        memory={"backend": "file"},
    )

    assert _memory_conversation(ctx, config, scope_context) == ConversationRef("thread", ("Earlier work.", None))
    assert _memory_conversation(ctx, mem0, scope_context) is None
    assert _memory_conversation(replace(ctx, agent_mode="minimal"), config, scope_context) is None
    assert _memory_conversation(ctx, limited, scope_context) is None
    scheduled = replace(ctx, scheduled_history_budget=ScheduledHistoryBudget(limit=0, source_event_id="$schedule"))
    assert _memory_conversation(scheduled, config, scope_context) is None
    persona = replace(ctx, persona=inline_persona("You are a reviewer.", None, None, None))
    assert _memory_conversation(persona, config, scope_context) is None
