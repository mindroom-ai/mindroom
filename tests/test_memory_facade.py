"""Tests for the public memory facade."""
# ruff: noqa: D101, D102, D103

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from openai import AuthenticationError

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.embeddings.embedder_health import capture_embedder_health_recorder, get_embedder_failure
from mindroom.embeddings.errors import EmbedderRequestError, openai_embedder_error
from mindroom.memory import MemoryPromptParts
from mindroom.memory import add_agent_memory as public_add_agent_memory
from mindroom.memory import build_memory_prompt_parts as public_build_memory_prompt_parts
from mindroom.memory import delete_agent_memory as public_delete_agent_memory
from mindroom.memory import get_agent_memory as public_get_agent_memory
from mindroom.memory import list_all_agent_memories as public_list_all_agent_memories
from mindroom.memory import search_agent_memories as public_search_agent_memories
from mindroom.memory import store_conversation_memory as public_store_conversation_memory
from mindroom.memory import update_agent_memory as public_update_agent_memory
from mindroom.memory._prompting import format_memories_as_context
from mindroom.memory.config import _Mem0Embedder, _Mem0StrictOpenAIEmbedder, mem0_chroma_path
from mindroom.prompts import MEMORY_CONTEXT_PROMPT_TEMPLATE
from mindroom.tool_system.worker_routing import agent_state_root_path, agent_workspace_root_path
from tests.conftest import bind_runtime_paths, make_visible_message, runtime_paths_for
from tests.fakes.token_counter import fake_token_counter
from tests.memory_test_support import MockTeamConfig

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.memory._shared import MemoryResult


async def add_agent_memory(
    content: str,
    agent_name: str,
    config: Config,
    metadata: dict | None = None,
) -> None:
    await public_add_agent_memory(
        content,
        agent_name,
        config,
        runtime_paths_for(config),
        metadata,
    )


async def search_agent_memories(
    query: str,
    agent_name: str,
    config: Config,
    limit: int = 3,
) -> list[MemoryResult]:
    outcome = await public_search_agent_memories(
        query,
        agent_name,
        config,
        runtime_paths_for(config),
        limit,
    )
    assert outcome.degraded_reason is None
    return outcome.results


async def list_all_agent_memories(
    agent_name: str,
    config: Config,
    limit: int = 100,
) -> list[MemoryResult]:
    return await public_list_all_agent_memories(
        agent_name,
        config,
        runtime_paths_for(config),
        limit,
    )


async def get_agent_memory(
    memory_id: str,
    agent_name: str,
    config: Config,
) -> MemoryResult | None:
    return await public_get_agent_memory(
        memory_id,
        agent_name,
        config,
        runtime_paths_for(config),
    )


async def update_agent_memory(
    memory_id: str,
    content: str,
    agent_name: str,
    config: Config,
) -> None:
    await public_update_agent_memory(
        memory_id,
        content,
        agent_name,
        config,
        runtime_paths_for(config),
    )


async def delete_agent_memory(
    memory_id: str,
    agent_name: str,
    config: Config,
) -> None:
    await public_delete_agent_memory(
        memory_id,
        agent_name,
        config,
        runtime_paths_for(config),
    )


async def build_memory_prompt_parts(
    prompt: str,
    agent_name: str,
    config: Config,
) -> MemoryPromptParts:
    return await public_build_memory_prompt_parts(
        prompt,
        agent_name,
        config,
        runtime_paths_for(config),
        counter=fake_token_counter(),
    )


async def store_conversation_memory(
    prompt: str,
    caller: str | list[str],
    session_id: str,
    config: Config,
    **kwargs: object,
) -> None:
    await public_store_conversation_memory(
        prompt,
        caller,
        session_id,
        config,
        runtime_paths_for(config),
        **kwargs,
    )


def _test_config(storage_path: Path) -> Config:
    runtime_paths = resolve_runtime_paths(
        config_path=storage_path / "config.yaml",
        storage_path=storage_path,
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    return bind_runtime_paths(
        Config(
            agents={
                "agent": AgentConfig(display_name="Agent"),
                "calculator": AgentConfig(display_name="Calculator"),
                "data_analyst": AgentConfig(display_name="Data Analyst"),
                "finance": AgentConfig(display_name="Finance"),
                "general": AgentConfig(display_name="General"),
                "helper": AgentConfig(display_name="Helper"),
                "test_agent": AgentConfig(display_name="Test Agent"),
            },
        ),
        runtime_paths,
    )


def _mem0_team_store_root(config: Config) -> Path:
    return runtime_paths_for(config).storage_root / "team_memory"


def _team_file_dir(config: Config, scope_user_id: str) -> Path:
    return runtime_paths_for(config).storage_root / "team_memory" / scope_user_id


def _create_team_store(config: Config) -> None:
    """Create the Mem0 team store a team turn would have written, since reads never create it."""
    mem0_chroma_path(_mem0_team_store_root(config)).mkdir(parents=True)


def _embedder_auth_error() -> EmbedderRequestError:
    """Return the classified error of an embedder rejecting its API key."""
    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(401, request=request, json={"error": {"message": "bad key"}})
    return openai_embedder_error(AuthenticationError("Error code: 401", response=response, body=None))


class TestMemoryFacade:
    @pytest.fixture
    def mock_memory(self) -> AsyncMock:
        memory = AsyncMock()
        memory.add.return_value = None
        memory.search.return_value = {"results": []}
        return memory

    @pytest.fixture
    def storage_path(self, tmp_path: Path) -> Path:
        return tmp_path

    @pytest.fixture
    def config(self, storage_path: Path) -> Config:
        return _test_config(storage_path)

    @pytest.mark.asyncio
    async def test_memory_instance_creation(self, mock_memory: AsyncMock, storage_path: Path, config: Config) -> None:
        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory) as mock_create:
            await add_agent_memory("Test content", "test_agent", config)
            assert mock_create.call_args[0][0] == agent_state_root_path(storage_path, "test_agent")

            await search_agent_memories("query", "test_agent", config)
            assert mock_create.call_args[0][0] == agent_state_root_path(storage_path, "test_agent")

    @pytest.mark.asyncio
    async def test_add_agent_memory(self, mock_memory: AsyncMock, config: Config) -> None:
        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            await add_agent_memory(
                "Test memory content",
                "test_agent",
                config,
                metadata={"test": "value"},
            )

            mock_memory.add.assert_called_once()
            call_args = mock_memory.add.call_args
            assert call_args[0][0] == [{"role": "user", "content": "Test memory content"}]
            assert call_args[1]["user_id"] == "agent_test_agent"
            assert call_args[1]["metadata"]["agent"] == "test_agent"
            assert call_args[1]["metadata"]["test"] == "value"

    @pytest.mark.asyncio
    async def test_add_agent_memory_surfaces_embedding_failure_swallowed_by_mem0(
        self,
        config: Config,
    ) -> None:
        """An empty normal return from Mem0 cannot turn a failed write into success."""
        failure_detail = "embedder authentication failed (HTTP 401)"

        class FailingEmbedder:
            sends_batches = True

            def embed(self, _texts: list[str], *, purpose: str) -> list[list[float]]:
                del purpose
                raise EmbedderRequestError(failure_detail)

            async def aembed(self, _texts: list[str], *, purpose: str) -> list[list[float]]:
                del purpose
                raise EmbedderRequestError(failure_detail)

        class SwallowingMemory:
            def __init__(self) -> None:
                self.embedding_model = _Mem0Embedder(
                    _Mem0StrictOpenAIEmbedder(FailingEmbedder()),
                    openai_embedder_error,
                )

            async def add(self, messages: list[dict], **_kwargs: object) -> dict[str, list]:
                try:
                    self.embedding_model.embed_batch([message["content"] for message in messages])
                except EmbedderRequestError:
                    for message in messages:
                        with suppress(EmbedderRequestError):
                            self.embedding_model.embed(message["content"])
                return {"results": []}

        try:
            with (
                patch("mindroom.memory._backend.create_memory_instance", return_value=SwallowingMemory()),
                pytest.raises(EmbedderRequestError, match="embedder authentication failed"),
            ):
                await add_agent_memory("Never stored", "test_agent", config)

            assert get_embedder_failure() == "embedder authentication failed (HTTP 401)"
        finally:
            capture_embedder_health_recorder().record(None)

    @pytest.mark.asyncio
    async def test_add_agent_memory_error_handling(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        mock_memory.add.side_effect = Exception("Memory error")

        with (
            patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory),
            pytest.raises(Exception, match="Memory error"),
        ):
            await add_agent_memory("Test content", "test_agent", config)

    @pytest.mark.asyncio
    async def test_search_agent_memories(self, mock_memory: AsyncMock, config: Config) -> None:
        mock_results = [
            {"id": "1", "memory": "Previous calculation: 2+2=4", "score": 0.9, "metadata": {"agent": "calculator"}},
        ]
        mock_memory.search.return_value = {"results": mock_results}

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories("calculation", "calculator", config, limit=5)

            mock_memory.search.assert_called_once_with(
                "calculation",
                filters={"user_id": "agent_calculator"},
                top_k=5,
                threshold=pytest.approx(1 / 2.8),
            )
            assert results == mock_results

    @pytest.mark.asyncio
    async def test_search_agent_memories_handles_list_response(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        mock_memory.search.return_value = [{"memory": "test"}]

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories("query", "agent", config)
            assert results == []

    @pytest.mark.asyncio
    async def test_mem0_search_classifies_propagated_auth_error(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        mock_memory.search.side_effect = _embedder_auth_error()

        try:
            with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
                outcome = await public_search_agent_memories(
                    "query",
                    "agent",
                    config,
                    runtime_paths_for(config),
                )

            assert outcome.results == []
            assert outcome.degraded_reason == "embedder authentication failed (HTTP 401)"
            # Mem0 traffic never passes through MindRoom's embedder, so the
            # backend itself must keep /api/health in sync.
            assert get_embedder_failure() == "embedder authentication failed (HTTP 401)"
        finally:
            capture_embedder_health_recorder().record(None)

    @pytest.mark.asyncio
    async def test_mem0_search_classifies_provider_failure_during_initialization(
        self,
        config: Config,
    ) -> None:
        """An embedder failure while Mem0 is built, which the build raises typed, degrades instead of aborting the turn."""
        with patch("mindroom.memory._backend.create_memory_instance", side_effect=_embedder_auth_error()):
            outcome = await public_search_agent_memories(
                "query",
                "agent",
                config,
                runtime_paths_for(config),
            )

        assert outcome.results == []
        assert outcome.degraded_reason == "embedder authentication failed (HTTP 401)"
        capture_embedder_health_recorder().record(None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failing_scope", "kept_ids"),
        [
            ("team_agent+calculator", ["personal"]),
            ("agent_agent", []),
        ],
    )
    async def test_mem0_store_failure_ends_the_search_with_the_earlier_stores_hits(
        self,
        mock_memory: AsyncMock,
        config: Config,
        failing_scope: str,
        kept_ids: list[str],
    ) -> None:
        """Every store embeds with one embedder, so its outage in one store ends the search and keeps what came before."""
        config.teams = {
            "first": MockTeamConfig(agents=["agent", "calculator"]),
            "second": MockTeamConfig(agents=["agent", "finance"]),
        }
        _create_team_store(config)
        hits = {
            "agent_agent": {"id": "personal", "memory": "personal", "score": 0.9},
            "team_agent+calculator": {"id": "first team", "memory": "first team", "score": 0.8},
            "team_agent+finance": {"id": "second team", "memory": "second team", "score": 0.7},
        }
        searched: list[str] = []

        async def search(_query: str, *, filters: dict[str, str], **_kwargs: object) -> dict[str, list[dict]]:
            searched.append(filters["user_id"])
            if filters["user_id"] == failing_scope:
                raise _embedder_auth_error()
            return {"results": [hits[filters["user_id"]]]}

        mock_memory.search.side_effect = search
        try:
            with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
                outcome = await public_search_agent_memories(
                    "query",
                    "agent",
                    config,
                    runtime_paths_for(config),
                )

            assert [result["id"] for result in outcome.results] == kept_ids
            assert searched[-1] == failing_scope
            assert outcome.degraded_reason == "embedder authentication failed (HTTP 401)"
        finally:
            capture_embedder_health_recorder().record(None)

    @pytest.mark.asyncio
    async def test_mem0_search_success_clears_recorded_failure(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        """A completed mem0 search (even empty) proves recovery and clears stale health."""
        mock_memory.search.return_value = {"results": []}
        capture_embedder_health_recorder().record("embedder authentication failed (HTTP 401)")
        try:
            with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
                outcome = await public_search_agent_memories(
                    "query",
                    "agent",
                    config,
                    runtime_paths_for(config),
                )

            assert outcome.results == []
            assert outcome.degraded_reason is None
            assert get_embedder_failure() is None
        finally:
            capture_embedder_health_recorder().record(None)

    @pytest.mark.asyncio
    async def test_mem0_search_non_provider_error_raises(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        mock_memory.search.side_effect = RuntimeError("sqlite corrupt")

        with (
            patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory),
            pytest.raises(RuntimeError, match="sqlite corrupt"),
        ):
            await public_search_agent_memories(
                "query",
                "agent",
                config,
                runtime_paths_for(config),
            )

    @pytest.mark.asyncio
    async def test_get_agent_memory_allows_agent_scope(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Own memory", "user_id": "agent_test_agent"}

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            result = await get_agent_memory("mem-1", "test_agent", config)

        assert result is not None
        assert result["id"] == "mem-1"
        mock_memory.get.assert_called_once_with("mem-1")

    @pytest.mark.asyncio
    async def test_get_agent_memory_rejects_other_agent_scope(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Other memory", "user_id": "agent_other_agent"}

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            result = await get_agent_memory("mem-1", "test_agent", config)

        assert result is None
        mock_memory.get.assert_called_once_with("mem-1")

    @pytest.mark.asyncio
    async def test_get_agent_memory_allows_team_scope(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.teams = {"test_team": MockTeamConfig(agents=["helper", "test_agent"])}
        _create_team_store(config)
        mock_memory.get.return_value = {"id": "mem-team", "memory": "Team memory", "user_id": "team_helper+test_agent"}

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory) as mock_create:
            result = await get_agent_memory("mem-team", "test_agent", config)

        assert result is not None
        assert result["id"] == "mem-team"
        # The agent's own store holds no such scope, so the team's one store answers.
        assert [call.args[0] for call in mock_create.call_args_list] == [
            agent_state_root_path(storage_path, "test_agent"),
            _mem0_team_store_root(config),
        ]

    @pytest.mark.asyncio
    async def test_get_agent_memory_rejects_teammate_agent_scope(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        """A shared team never exposes one member's own memories to another member."""
        config.teams = {"test_team": MockTeamConfig(agents=["helper", "test_agent"])}
        _create_team_store(config)
        mock_memory.get.return_value = {"id": "mem-member", "memory": "Member memory", "user_id": "agent_helper"}

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            result = await get_agent_memory("mem-member", "test_agent", config)

        assert result is None
        assert mock_memory.get.call_count == 2

    @pytest.mark.asyncio
    async def test_update_agent_memory_rejects_other_agent_scope(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Other memory", "user_id": "agent_other_agent"}

        with (
            patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory),
            pytest.raises(ValueError, match="No memory found with id=mem-1"),
        ):
            await update_agent_memory("mem-1", "Updated content", "test_agent", config)

        mock_memory.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_agent_memory_rejects_other_agent_scope(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Other memory", "user_id": "agent_other_agent"}

        with (
            patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory),
            pytest.raises(ValueError, match="No memory found with id=mem-1"),
        ):
            await delete_agent_memory("mem-1", "test_agent", config)

        mock_memory.delete.assert_not_called()

    def test_format_memories_as_context(self) -> None:
        memories: list[MemoryResult] = [
            {"memory": "First memory", "id": "1"},
            {"memory": "Second memory", "id": "2"},
        ]

        context = format_memories_as_context(
            memories,
            "agent",
            prompt_template=MEMORY_CONTEXT_PROMPT_TEMPLATE,
        )
        expected = (
            "[Automatically extracted agent memories - may not be relevant to current context]\n"
            "Previous agent memories that might be related:\n"
            "- First memory\n"
            "- Second memory"
        )
        assert context == expected

    def test_format_memories_as_context_empty(self) -> None:
        assert format_memories_as_context([], "agent", prompt_template=MEMORY_CONTEXT_PROMPT_TEMPLATE) == ""

    @pytest.mark.asyncio
    async def test_build_memory_prompt_parts(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        agent_memories = [{"memory": "I previously calculated 2+2=4", "id": "1"}]
        mock_memory.search.return_value = {"results": agent_memories}

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            prompt_parts = await build_memory_prompt_parts(
                "What is 3+3?",
                "calculator",
                config,
            )

        assert prompt_parts.session_preamble == ""
        assert "[Automatically extracted agent memories - may not be relevant to current context]" in (
            prompt_parts.transient_turn_context
        )
        assert "I previously calculated 2+2=4" in prompt_parts.transient_turn_context

    @pytest.mark.asyncio
    async def test_build_memory_prompt_parts_no_memories(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        mock_memory.search.return_value = {"results": []}

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            prompt_parts = await build_memory_prompt_parts("Original prompt", "agent", config)

        assert prompt_parts == MemoryPromptParts()

    @pytest.mark.asyncio
    async def test_build_memory_prompt_parts_surfaces_degradation_without_matches(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        """The automatic per-turn path carries the degradation notice, not silence."""
        mock_memory.search.side_effect = _embedder_auth_error()

        try:
            with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
                prompt_parts = await build_memory_prompt_parts("Original prompt", "agent", config)
        finally:
            capture_embedder_health_recorder().record(None)

        assert "Semantic memory search is unavailable this turn" in prompt_parts.transient_turn_context
        assert "embedder authentication failed (HTTP 401)" in prompt_parts.transient_turn_context
        assert "Do not claim to have checked stored memories." in prompt_parts.transient_turn_context

    @pytest.mark.asyncio
    async def test_disabled_backend_build_memory_prompt_parts_skips_mem0(
        self,
        config: Config,
    ) -> None:
        config.memory.backend = "none"

        with patch(
            "mindroom.memory._backend.create_memory_instance",
            side_effect=AssertionError("disabled memory must not create Mem0"),
        ) as mock_create:
            prompt_parts = await build_memory_prompt_parts("Original prompt", "agent", config)

        assert prompt_parts == MemoryPromptParts()
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_conversation_memory(self, mock_memory: AsyncMock, config: Config) -> None:
        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory(
                "What is 2+2?",
                "calculator",
                "session123",
                config,
            )

        assert mock_memory.add.call_count == 1
        agent_call = mock_memory.add.call_args_list[0]
        assert agent_call[0][0] == [{"role": "user", "content": "What is 2+2?"}]
        assert agent_call[1]["user_id"] == "agent_calculator"
        assert agent_call[1]["metadata"]["type"] == "conversation"

    @pytest.mark.asyncio
    async def test_store_conversation_memory_no_prompt(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory("", "agent", "session123", config)

        mock_memory.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_backend_store_conversation_memory_is_noop(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "none"

        with patch(
            "mindroom.memory._backend.create_memory_instance",
            side_effect=AssertionError("disabled memory must not create Mem0"),
        ) as mock_create:
            await store_conversation_memory("Remember this", "calculator", "session123", config)
            await store_conversation_memory(
                "Team should also stay stateless",
                ["calculator", "finance"],
                "session-team",
                config,
            )

        mock_create.assert_not_called()
        assert not any(storage_path.rglob("MEMORY.md"))

    @pytest.mark.asyncio
    async def test_store_conversation_memory_with_thread_history(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        thread_history = [
            make_visible_message(sender="@user:matrix.org", body="I need help with math"),
            make_visible_message(sender="@router:matrix.org", body="@calculator can help with that"),
            make_visible_message(sender="@user:matrix.org", body="Yes please"),
        ]

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory(
                "What is 2+2?",
                "calculator",
                "session123",
                config,
                thread_history=thread_history,
                user_id="@user:matrix.org",
            )

        messages = mock_memory.add.call_args_list[0][0][0]
        assert messages == [
            {"role": "user", "content": "I need help with math"},
            {"role": "assistant", "content": "@calculator can help with that"},
            {"role": "user", "content": "Yes please"},
            {"role": "user", "content": "What is 2+2?"},
        ]

    @pytest.mark.asyncio
    async def test_store_conversation_memory_for_team(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        team_agents = ["calculator", "data_analyst", "finance"]

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory) as mock_create:
            await store_conversation_memory(
                "Analyze our Q4 financial data",
                team_agents,
                "session123",
                config,
            )

        # A team turn runs one extraction into the team's one store, not one per member.
        mock_create.assert_called_once_with(
            _mem0_team_store_root(config),
            config,
            runtime_paths=runtime_paths_for(config),
        )
        mock_memory.add.assert_called_once()
        team_call = mock_memory.add.call_args
        assert team_call.kwargs["user_id"] == "team_calculator+data_analyst+finance"
        assert team_call.kwargs["metadata"] == {
            "type": "conversation",
            "session_id": "session123",
            "is_team": True,
            "team_members": team_agents,
        }

    @pytest.mark.asyncio
    async def test_store_conversation_memory_respects_agent_backend_override(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory) as mock_create:
            await store_conversation_memory("What is 2+2?", "calculator", "session123", config)

        mock_create.assert_called_once_with(
            agent_state_root_path(storage_path, "calculator"),
            config,
            runtime_paths=runtime_paths_for(config),
        )
        mock_memory.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_conversation_memory_team_uses_mem0_when_any_member_overrides(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory) as mock_create:
            await store_conversation_memory(
                "Analyze our quarterly metrics",
                ["calculator", "finance"],
                "session-team",
                config,
            )

        mock_create.assert_called_once_with(
            _mem0_team_store_root(config),
            config,
            runtime_paths=runtime_paths_for(config),
        )
        mock_memory.add.assert_called_once()
        assert not (_team_file_dir(config, "team_calculator+finance") / "MEMORY.md").exists()

    @pytest.mark.asyncio
    async def test_disabled_backend_crud_facade_does_not_fall_through_to_mem0(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "none"

        with patch(
            "mindroom.memory._backend.create_memory_instance",
            side_effect=AssertionError("disabled memory must not create Mem0"),
        ) as mock_create:
            await add_agent_memory("Remember this", "general", config)
            search_results = await search_agent_memories("Remember", "general", config)
            list_results = await list_all_agent_memories("general", config)
            get_result = await get_agent_memory("memory-1", "general", config)
            await update_agent_memory("memory-1", "Updated", "general", config)
            await delete_agent_memory("memory-1", "general", config)

        assert search_results == []
        assert list_results == []
        assert get_result is None
        mock_create.assert_not_called()
        assert not any(storage_path.rglob("MEMORY.md"))

    @pytest.mark.asyncio
    async def test_search_agent_memories_with_teams(
        self,
        mock_memory: AsyncMock,
        config: Config,
    ) -> None:
        config.teams = {"finance_team": MockTeamConfig(agents=["calculator", "data_analyst", "finance"])}
        _create_team_store(config)
        thresholds: dict[str, float] = {}

        def search_side_effect(query: str, *, filters: dict[str, str], top_k: int, threshold: float) -> dict:  # noqa: ARG001
            del top_k
            user_id = filters["user_id"]
            thresholds[user_id] = threshold
            if user_id == "agent_calculator":
                return {"results": [{"id": "1", "memory": "Individual fact", "score": 0.9}]}
            if user_id == "team_calculator+data_analyst+finance":
                return {"results": [{"id": "2", "memory": "Team fact", "score": 0.85}]}
            return {"results": []}

        mock_memory.search = AsyncMock(side_effect=search_side_effect)

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories("test query", "calculator", config, limit=5)

        assert len(results) == 2
        assert results[0]["memory"] == "Individual fact"
        assert results[1]["memory"] == "Team fact"
        assert mock_memory.search.call_count == 2
        # Both scopes keep the same cosine-0.1 cutoff on Mem0's score scale.
        assert thresholds == {
            "agent_calculator": pytest.approx(1 / 2.8),
            "team_calculator+data_analyst+finance": pytest.approx(1 / 2.8),
        }

    @pytest.mark.asyncio
    async def test_agent_memory_backend_override_to_file_uses_file_storage(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "mem0"
        config.agents["general"].memory_backend = "file"

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory) as mock_create:
            await add_agent_memory("Remember this", "general", config)

        mock_create.assert_not_called()
        memory_file = agent_workspace_root_path(storage_path, "general") / "MEMORY.md"
        assert memory_file.exists()
        assert "Remember this" in memory_file.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_agent_memory_backend_override_to_mem0_uses_mem0_storage(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["general"].memory_backend = "mem0"

        with patch("mindroom.memory._backend.create_memory_instance", return_value=mock_memory) as mock_create:
            await add_agent_memory("Remember this", "general", config)

        mock_create.assert_called_once_with(
            agent_state_root_path(storage_path, "general"),
            config,
            runtime_paths=runtime_paths_for(config),
        )
        mock_memory.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_team_turn_resolves_file_backend_from_agent_overrides(
        self,
        config: Config,
    ) -> None:
        """A team whose members all use file memory writes one team `MEMORY.md`, which every member reads."""
        config.memory.backend = "mem0"
        config.agents["calculator"].memory_backend = "file"
        config.agents["general"].memory_backend = "file"
        config.teams = {"pair": MockTeamConfig(agents=["general", "calculator"])}

        with patch(
            "mindroom.memory._backend.create_memory_instance",
            side_effect=AssertionError("Mem0 should not be used for a file-backed team"),
        ):
            await store_conversation_memory(
                "Quarterly numbers are due Friday",
                ["general", "calculator"],
                "session-team",
                config,
            )
            [team_memory] = await search_agent_memories("Quarterly", "calculator", config)
            from_general = await get_agent_memory(team_memory["id"], "general", config)

        team_file = _team_file_dir(config, "team_calculator+general") / "MEMORY.md"
        assert "Quarterly numbers are due Friday" in team_file.read_text(encoding="utf-8")
        assert from_general is not None
        assert from_general["memory"] == "Quarterly numbers are due Friday"
