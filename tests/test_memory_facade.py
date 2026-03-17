"""Tests for the public memory facade."""
# ruff: noqa: D101, D102, D103

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

import mindroom.memory.functions as memory_functions
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.memory._prompting import _format_memories_as_context
from mindroom.tool_system.worker_routing import agent_state_root_path, agent_workspace_root_path
from tests.conftest import bind_runtime_paths, runtime_paths_for
from tests.memory_test_support import MockTeamConfig

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.memory._shared import MemoryResult


async def add_agent_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    metadata: dict | None = None,
) -> None:
    await memory_functions.add_agent_memory(
        content,
        agent_name,
        storage_path,
        config,
        runtime_paths_for(config),
        metadata,
    )


async def search_agent_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    limit: int = 3,
) -> list[MemoryResult]:
    return await memory_functions.search_agent_memories(
        query,
        agent_name,
        storage_path,
        config,
        runtime_paths_for(config),
        limit,
    )


async def list_all_agent_memories(
    agent_name: str,
    storage_path: Path,
    config: Config,
    limit: int = 100,
    *,
    preserve_resolved_storage_path: bool = False,
) -> list[MemoryResult]:
    return await memory_functions.list_all_agent_memories(
        agent_name,
        storage_path,
        config,
        runtime_paths_for(config),
        limit,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
    )


async def get_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> MemoryResult | None:
    return await memory_functions.get_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        runtime_paths_for(config),
    )


async def update_agent_memory(
    memory_id: str,
    content: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> None:
    await memory_functions.update_agent_memory(
        memory_id,
        content,
        caller_context,
        storage_path,
        config,
        runtime_paths_for(config),
    )


async def delete_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> None:
    await memory_functions.delete_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        runtime_paths_for(config),
    )


async def build_memory_enhanced_prompt(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
) -> str:
    return await memory_functions.build_memory_enhanced_prompt(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths_for(config),
    )


async def store_conversation_memory(
    prompt: str,
    agent_name: str | list[str],
    storage_path: Path,
    session_id: str,
    config: Config,
    **kwargs: object,
) -> None:
    await memory_functions.store_conversation_memory(
        prompt,
        agent_name,
        storage_path,
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
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            await add_agent_memory("Test content", "test_agent", storage_path, config)
            assert mock_create.call_args[0][0] == agent_state_root_path(storage_path, "test_agent")

            await search_agent_memories("query", "test_agent", storage_path, config)
            assert mock_create.call_args[0][0] == agent_state_root_path(storage_path, "test_agent")

    @pytest.mark.asyncio
    async def test_add_agent_memory(self, mock_memory: AsyncMock, storage_path: Path, config: Config) -> None:
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await add_agent_memory(
                "Test memory content",
                "test_agent",
                storage_path,
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
    async def test_add_agent_memory_error_handling(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        mock_memory.add.side_effect = Exception("Memory error")

        with (
            patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory),
            pytest.raises(Exception, match="Memory error"),
        ):
            await add_agent_memory("Test content", "test_agent", storage_path, config)

    @pytest.mark.asyncio
    async def test_search_agent_memories(self, mock_memory: AsyncMock, storage_path: Path, config: Config) -> None:
        mock_results = [
            {"id": "1", "memory": "Previous calculation: 2+2=4", "score": 0.9, "metadata": {"agent": "calculator"}},
        ]
        mock_memory.search.return_value = {"results": mock_results}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories("calculation", "calculator", storage_path, config, limit=5)

            mock_memory.search.assert_called_once_with("calculation", user_id="agent_calculator", limit=5)
            assert results == mock_results

    @pytest.mark.asyncio
    async def test_search_agent_memories_handles_dict_response(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
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
        mock_memory.search.return_value = [{"memory": "test"}]

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories("query", "agent", storage_path, config)
            assert results == []

    @pytest.mark.asyncio
    async def test_get_agent_memory_allows_agent_scope(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
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
        config.teams = {"test_team": MockTeamConfig(agents=["helper", "test_agent"])}
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
        mock_memory.get.return_value = {"id": "mem-member", "memory": "Member memory", "user_id": "agent_helper"}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            result = await get_agent_memory("mem-member", ["helper", "test_agent"], storage_path, config)

        assert result is None
        assert mock_memory.get.call_count == 2
        assert all(call.args == ("mem-member",) for call in mock_memory.get.call_args_list)

    @pytest.mark.asyncio
    async def test_get_agent_memory_team_context_allows_member_scope_when_enabled(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
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
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Other memory", "user_id": "agent_other_agent"}

        with (
            patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory),
            pytest.raises(ValueError, match="No memory found with id=mem-1"),
        ):
            await update_agent_memory("mem-1", "Updated content", "test_agent", storage_path, config)

        mock_memory.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_agent_memory_rejects_other_agent_scope(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        mock_memory.get.return_value = {"id": "mem-1", "memory": "Other memory", "user_id": "agent_other_agent"}

        with (
            patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory),
            pytest.raises(ValueError, match="No memory found with id=mem-1"),
        ):
            await delete_agent_memory("mem-1", "test_agent", storage_path, config)

        mock_memory.delete.assert_not_called()

    def test_format_memories_as_context(self) -> None:
        memories: list[MemoryResult] = [
            {"memory": "First memory", "id": "1"},
            {"memory": "Second memory", "id": "2"},
        ]

        context = _format_memories_as_context(memories, "agent")
        expected = (
            "[Automatically extracted agent memories - may not be relevant to current context]\n"
            "Previous agent memories that might be related:\n"
            "- First memory\n"
            "- Second memory"
        )
        assert context == expected

    def test_format_memories_as_context_empty(self) -> None:
        assert _format_memories_as_context([], "agent") == ""

    @pytest.mark.asyncio
    async def test_build_memory_enhanced_prompt(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        agent_memories = [{"memory": "I previously calculated 2+2=4", "id": "1"}]
        mock_memory.search.return_value = {"results": agent_memories}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            enhanced = await build_memory_enhanced_prompt(
                "What is 3+3?",
                "calculator",
                storage_path,
                config,
            )

        assert "[Automatically extracted agent memories - may not be relevant to current context]" in enhanced
        assert "I previously calculated 2+2=4" in enhanced
        assert "What is 3+3?" in enhanced

    @pytest.mark.asyncio
    async def test_build_memory_enhanced_prompt_no_memories(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        mock_memory.search.return_value = {"results": []}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            enhanced = await build_memory_enhanced_prompt("Original prompt", "agent", storage_path, config)

        assert enhanced == "Original prompt"

    @pytest.mark.asyncio
    async def test_store_conversation_memory(self, mock_memory: AsyncMock, storage_path: Path, config: Config) -> None:
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory(
                "What is 2+2?",
                "calculator",
                storage_path,
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
        storage_path: Path,
        config: Config,
    ) -> None:
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory("", "agent", storage_path, "session123", config)

        mock_memory.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_conversation_memory_with_empty_response(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory("What is 2+2?", "calculator", storage_path, "session123", config)

        assert mock_memory.add.call_count == 1
        agent_call = mock_memory.add.call_args_list[0]
        assert agent_call[0][0] == [{"role": "user", "content": "What is 2+2?"}]

    @pytest.mark.asyncio
    async def test_store_conversation_memory_with_thread_history(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
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
        storage_path: Path,
        config: Config,
    ) -> None:
        team_agents = ["calculator", "data_analyst", "finance"]

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            await store_conversation_memory(
                "Analyze our Q4 financial data",
                team_agents,
                storage_path,
                "session123",
                config,
            )

        assert mock_memory.add.call_count == len(team_agents)
        for team_call in mock_memory.add.call_args_list:
            assert team_call[1]["user_id"] == "team_calculator+data_analyst+finance"
            metadata = team_call[1]["metadata"]
            assert metadata["type"] == "conversation"
            assert metadata["is_team"] is True
            assert metadata["team_members"] == team_agents

    @pytest.mark.asyncio
    async def test_store_conversation_memory_respects_agent_backend_override(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            await store_conversation_memory("What is 2+2?", "calculator", storage_path, "session123", config)

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
        storage_path: Path,
        config: Config,
    ) -> None:
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

        assert mock_create.call_count == 2
        expected_runtime_paths = runtime_paths_for(config)
        assert [(call.args, call.kwargs) for call in mock_create.call_args_list] == [
            (
                (agent_state_root_path(storage_path, "calculator"), config),
                {"runtime_paths": expected_runtime_paths},
            ),
            (
                (agent_state_root_path(storage_path, "finance"), config),
                {"runtime_paths": expected_runtime_paths},
            ),
        ]
        assert mock_memory.add.call_count == 2
        team_memory_file = storage_path / "memory-files" / "team_calculator+finance" / "MEMORY.md"
        assert not team_memory_file.exists()

    @pytest.mark.asyncio
    async def test_search_agent_memories_with_teams(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.teams = {"finance_team": MockTeamConfig(agents=["calculator", "data_analyst", "finance"])}

        def search_side_effect(query: str, user_id: str, limit: int) -> dict:  # noqa: ARG001
            if user_id == "agent_calculator":
                return {"results": [{"id": "1", "memory": "Individual fact", "score": 0.9}]}
            if user_id == "team_calculator+data_analyst+finance":
                return {"results": [{"id": "2", "memory": "Team fact", "score": 0.85}]}
            return {"results": []}

        mock_memory.search = AsyncMock(side_effect=search_side_effect)

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            results = await search_agent_memories("test query", "calculator", storage_path, config, limit=5)

        assert len(results) == 2
        assert results[0]["memory"] == "Individual fact"
        assert results[1]["memory"] == "Team fact"
        assert mock_memory.search.call_count == 2

    @pytest.mark.asyncio
    async def test_agent_memory_backend_override_to_file_uses_file_storage(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "mem0"
        config.memory.file.path = str(storage_path / "memory-files")
        config.agents["general"].memory_backend = "file"

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            await add_agent_memory("Remember this", "general", storage_path, config)

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

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            await add_agent_memory("Remember this", "general", storage_path, config)

        mock_create.assert_called_once_with(
            agent_state_root_path(storage_path, "general"),
            config,
            runtime_paths=runtime_paths_for(config),
        )
        mock_memory.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_team_context_resolves_file_backend_from_agent_overrides(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
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

    def test_memory_result_typed_dict(self) -> None:
        result: MemoryResult = {
            "id": "123",
            "memory": "Test memory",
            "score": 0.95,
            "metadata": {"key": "value"},
        }

        assert result["id"] == "123"
        assert result["memory"] == "Test memory"
