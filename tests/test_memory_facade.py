"""Tests for the public memory facade."""
# ruff: noqa: D101, D102, D103

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.memory import (
    MemoryPromptParts,
)
from mindroom.memory import (
    add_agent_memory as public_add_agent_memory,
)
from mindroom.memory import (
    build_memory_prompt_parts as public_build_memory_prompt_parts,
)
from mindroom.memory import (
    delete_agent_memory as public_delete_agent_memory,
)
from mindroom.memory import (
    get_agent_memory as public_get_agent_memory,
)
from mindroom.memory import (
    list_all_agent_memories as public_list_all_agent_memories,
)
from mindroom.memory import (
    search_agent_memories as public_search_agent_memories,
)
from mindroom.memory import (
    store_conversation_memory as public_store_conversation_memory,
)
from mindroom.memory import (
    update_agent_memory as public_update_agent_memory,
)
from mindroom.memory._prompting import _format_memories_as_context
from mindroom.memory._shared import MemoryNotFoundError
from mindroom.tool_system.worker_routing import agent_state_root_path, agent_workspace_root_path
from tests.conftest import bind_runtime_paths, make_visible_message, runtime_paths_for
from tests.memory_test_support import FakeMem0ScopedMemory, MockTeamConfig

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.memory._shared import MemoryResult


async def add_agent_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    metadata: dict | None = None,
) -> None:
    await public_add_agent_memory(
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
    return await public_search_agent_memories(
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
    return await public_list_all_agent_memories(
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
    return await public_get_agent_memory(
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
    await public_update_agent_memory(
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
    await public_delete_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        runtime_paths_for(config),
    )


async def build_memory_prompt_parts(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
) -> MemoryPromptParts:
    return await public_build_memory_prompt_parts(
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
    await public_store_conversation_memory(
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


def _fake_mem0_factory(
    mem0_stores: dict[Path, FakeMem0ScopedMemory],
) -> Callable[..., Awaitable[FakeMem0ScopedMemory]]:
    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
        timing_scope: str | None = None,
    ) -> FakeMem0ScopedMemory:
        del runtime_paths, timing_scope
        if scope_storage_path not in mem0_stores:
            mem0_stores[scope_storage_path] = FakeMem0ScopedMemory()
        return mem0_stores[scope_storage_path]

    return create_fake_memory_instance


def _team_file_memory_path(storage_path: Path, agent_name: str, team_id: str) -> Path:
    return agent_state_root_path(storage_path, agent_name) / "memory_files" / team_id / "MEMORY.md"


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

            mock_memory.search.assert_called_once_with(
                "calculation",
                filters={"user_id": "agent_calculator"},
                top_k=5,
            )
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
    async def test_build_memory_prompt_parts(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        agent_memories = [{"memory": "I previously calculated 2+2=4", "id": "1"}]
        mock_memory.search.return_value = {"results": agent_memories}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            prompt_parts = await build_memory_prompt_parts(
                "What is 3+3?",
                "calculator",
                storage_path,
                config,
            )

        assert prompt_parts.session_preamble == ""
        assert "[Automatically extracted agent memories - may not be relevant to current context]" in (
            prompt_parts.turn_context
        )
        assert "I previously calculated 2+2=4" in prompt_parts.turn_context

    @pytest.mark.asyncio
    async def test_build_memory_prompt_parts_no_memories(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        mock_memory.search.return_value = {"results": []}

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory):
            prompt_parts = await build_memory_prompt_parts("Original prompt", "agent", storage_path, config)

        assert prompt_parts == MemoryPromptParts()

    @pytest.mark.asyncio
    async def test_disabled_backend_build_memory_prompt_parts_skips_mem0(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "none"

        with patch(
            "mindroom.memory.functions.create_memory_instance",
            side_effect=AssertionError("disabled memory must not create Mem0"),
        ) as mock_create:
            prompt_parts = await build_memory_prompt_parts("Original prompt", "agent", storage_path, config)

        assert prompt_parts == MemoryPromptParts()
        mock_create.assert_not_called()

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
    async def test_disabled_backend_store_conversation_memory_is_noop(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "none"

        with patch(
            "mindroom.memory.functions.create_memory_instance",
            side_effect=AssertionError("disabled memory must not create Mem0"),
        ) as mock_create:
            await store_conversation_memory("Remember this", "calculator", storage_path, "session123", config)
            await store_conversation_memory(
                "Team should also stay stateless",
                ["calculator", "finance"],
                storage_path,
                "session-team",
                config,
            )

        mock_create.assert_not_called()
        assert not any(storage_path.rglob("MEMORY.md"))

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
            make_visible_message(sender="@user:matrix.org", body="I need help with math"),
            make_visible_message(sender="@router:matrix.org", body="@calculator can help with that"),
            make_visible_message(sender="@user:matrix.org", body="Yes please"),
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
    async def test_store_conversation_memory_team_writes_each_members_effective_backend(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"

        with patch("mindroom.memory.functions.create_memory_instance", return_value=mock_memory) as mock_create:
            await store_conversation_memory(
                "Analyze our quarterly metrics",
                ["calculator", "finance"],
                storage_path,
                "session-team",
                config,
            )

        expected_runtime_paths = runtime_paths_for(config)
        mock_create.assert_called_once_with(
            agent_state_root_path(storage_path, "calculator"),
            config,
            runtime_paths=expected_runtime_paths,
        )
        mock_memory.add.assert_called_once()
        mem0_call = mock_memory.add.call_args
        assert mem0_call[1]["user_id"] == "team_calculator+finance"
        assert mem0_call[1]["metadata"]["team_members"] == ["calculator", "finance"]

        file_team_memory = (
            agent_state_root_path(storage_path, "finance") / "memory_files" / "team_calculator+finance" / "MEMORY.md"
        )
        assert file_team_memory.exists()
        assert "Analyze our quarterly metrics" in file_team_memory.read_text(encoding="utf-8")
        mem0_team_memory_file = (
            agent_state_root_path(storage_path, "calculator") / "memory_files" / "team_calculator+finance" / "MEMORY.md"
        )
        assert not mem0_team_memory_file.exists()

    @pytest.mark.asyncio
    async def test_mixed_team_memory_reads_through_each_members_effective_backend(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"
        config.teams = {"mixed_team": MockTeamConfig(agents=["calculator", "finance"])}
        mem0_stores: dict[Path, FakeMem0ScopedMemory] = {}

        with patch("mindroom.memory.functions.create_memory_instance", side_effect=_fake_mem0_factory(mem0_stores)):
            await store_conversation_memory(
                "Mixed backend team insight",
                ["calculator", "finance"],
                storage_path,
                "session-team",
                config,
            )
            calculator_results = await search_agent_memories(
                "Mixed backend team",
                "calculator",
                storage_path,
                config,
                limit=5,
            )

        finance_results = await search_agent_memories("Mixed backend team", "finance", storage_path, config, limit=5)

        assert any(
            result.get("memory") == "Mixed backend team insight" and result.get("user_id") == "team_calculator+finance"
            for result in calculator_results
        )
        assert any(
            result.get("memory") == "Mixed backend team insight" and result.get("user_id") == "team_calculator+finance"
            for result in finance_results
        )
        assert set(mem0_stores) == {agent_state_root_path(storage_path, "calculator")}

    @pytest.mark.asyncio
    async def test_file_search_ignores_stale_team_memory_in_mem0_member_root(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"
        config.teams = {"mixed_team": MockTeamConfig(agents=["calculator", "finance"])}

        stale_team_file = _team_file_memory_path(storage_path, "calculator", "team_calculator+finance")
        stale_team_file.parent.mkdir(parents=True)
        stale_team_file.write_text("# Memory\n\n- [id=stale-file-id] stale migrated team memory\n", encoding="utf-8")

        results = await search_agent_memories(
            "stale migrated",
            "finance",
            storage_path,
            config,
            limit=5,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_mem0_single_agent_crud_ignores_stale_team_memory_in_file_member_root(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"
        config.teams = {"mixed_team": MockTeamConfig(agents=["calculator", "finance"])}
        mem0_stores: dict[Path, FakeMem0ScopedMemory] = {
            agent_state_root_path(storage_path, "finance"): FakeMem0ScopedMemory(),
        }
        finance_store = mem0_stores[agent_state_root_path(storage_path, "finance")]
        await finance_store.add(
            [{"role": "user", "content": "stale mem0 team copy"}],
            user_id="team_calculator+finance",
            metadata={"type": "conversation", "is_team": True, "team_members": ["calculator", "finance"]},
        )

        with patch("mindroom.memory.functions.create_memory_instance", side_effect=_fake_mem0_factory(mem0_stores)):
            stale_result = await get_agent_memory("mem-1", "calculator", storage_path, config)
            with pytest.raises(MemoryNotFoundError):
                await update_agent_memory("mem-1", "fresh mem0 team copy", "calculator", storage_path, config)
            with pytest.raises(MemoryNotFoundError):
                await delete_agent_memory("mem-1", "calculator", storage_path, config)

        stale_entries = await finance_store.get_all(filters={"user_id": "team_calculator+finance"})
        assert stale_result is None
        assert stale_entries["results"][0]["memory"] == "stale mem0 team copy"

    @pytest.mark.asyncio
    async def test_disabled_backend_crud_facade_does_not_fall_through_to_mem0(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "none"

        with patch(
            "mindroom.memory.functions.create_memory_instance",
            side_effect=AssertionError("disabled memory must not create Mem0"),
        ) as mock_create:
            await add_agent_memory("Remember this", "general", storage_path, config)
            search_results = await search_agent_memories("Remember", "general", storage_path, config)
            list_results = await list_all_agent_memories("general", storage_path, config)
            get_result = await get_agent_memory("memory-1", "general", storage_path, config)
            await update_agent_memory("memory-1", "Updated", "general", storage_path, config)
            await delete_agent_memory("memory-1", "general", storage_path, config)

        assert search_results == []
        assert list_results == []
        assert get_result is None
        mock_create.assert_not_called()
        assert not any(storage_path.rglob("MEMORY.md"))

    @pytest.mark.asyncio
    async def test_mixed_team_context_crud_keeps_backend_copies_in_sync(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"
        config.teams = {"mixed_team": MockTeamConfig(agents=["calculator", "finance"])}
        team_agents = ["calculator", "finance"]
        mem0_stores: dict[Path, FakeMem0ScopedMemory] = {}

        with patch("mindroom.memory.functions.create_memory_instance", side_effect=_fake_mem0_factory(mem0_stores)):
            await store_conversation_memory(
                "Mixed team stale-sensitive memory",
                team_agents,
                storage_path,
                "session-team",
                config,
            )
            calculator_results = await search_agent_memories(
                "stale-sensitive",
                "calculator",
                storage_path,
                config,
                limit=5,
            )
            finance_results = await search_agent_memories(
                "stale-sensitive",
                "finance",
                storage_path,
                config,
                limit=5,
            )
            assert len(calculator_results) == 1
            assert len(finance_results) == 1

            loaded_file_copy = await get_agent_memory(
                finance_results[0]["id"],
                team_agents,
                storage_path,
                config,
            )
            assert loaded_file_copy is not None
            assert loaded_file_copy["memory"] == "Mixed team stale-sensitive memory"

            await update_agent_memory(
                calculator_results[0]["id"],
                "Updated mixed team memory",
                team_agents,
                storage_path,
                config,
            )

            calculator_updated = await search_agent_memories(
                "updated mixed",
                "calculator",
                storage_path,
                config,
                limit=5,
            )
            finance_updated = await search_agent_memories(
                "updated mixed",
                "finance",
                storage_path,
                config,
                limit=5,
            )
            assert any(result.get("memory") == "Updated mixed team memory" for result in calculator_updated)
            assert any(result.get("memory") == "Updated mixed team memory" for result in finance_updated)
            assert not await search_agent_memories("stale-sensitive", "finance", storage_path, config, limit=5)

            await delete_agent_memory(
                calculator_results[0]["id"],
                team_agents,
                storage_path,
                config,
            )

            assert not await search_agent_memories("updated mixed", "calculator", storage_path, config, limit=5)
            assert not await search_agent_memories("updated mixed", "finance", storage_path, config, limit=5)

    @pytest.mark.asyncio
    async def test_single_agent_update_of_mixed_team_memory_syncs_backend_copies(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"
        config.teams = {"mixed_team": MockTeamConfig(agents=["calculator", "finance"])}
        team_agents = ["calculator", "finance"]
        mem0_stores: dict[Path, FakeMem0ScopedMemory] = {}

        with patch("mindroom.memory.functions.create_memory_instance", side_effect=_fake_mem0_factory(mem0_stores)):
            await store_conversation_memory(
                "Retired alpha source",
                team_agents,
                storage_path,
                "session-team",
                config,
            )
            calculator_results = await search_agent_memories(
                "Retired alpha",
                "calculator",
                storage_path,
                config,
                limit=5,
            )
            assert len(calculator_results) == 1

            await update_agent_memory(
                calculator_results[0]["id"],
                "Fresh beta team memory",
                "calculator",
                storage_path,
                config,
            )

            calculator_updated = await search_agent_memories(
                "Fresh beta",
                "calculator",
                storage_path,
                config,
                limit=5,
            )
            finance_updated = await search_agent_memories(
                "Fresh beta",
                "finance",
                storage_path,
                config,
                limit=5,
            )
            assert any(result.get("memory") == "Fresh beta team memory" for result in calculator_updated)
            assert any(result.get("memory") == "Fresh beta team memory" for result in finance_updated)
            assert not await search_agent_memories("Retired alpha", "finance", storage_path, config, limit=5)

    @pytest.mark.asyncio
    async def test_single_agent_delete_of_mixed_team_memory_syncs_backend_copies(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.memory.backend = "file"
        config.agents["calculator"].memory_backend = "mem0"
        config.teams = {"mixed_team": MockTeamConfig(agents=["calculator", "finance"])}
        team_agents = ["calculator", "finance"]
        mem0_stores: dict[Path, FakeMem0ScopedMemory] = {}

        with patch("mindroom.memory.functions.create_memory_instance", side_effect=_fake_mem0_factory(mem0_stores)):
            await store_conversation_memory(
                "Explicit tool delete source",
                team_agents,
                storage_path,
                "session-team",
                config,
            )
            finance_results = await search_agent_memories(
                "Explicit tool",
                "finance",
                storage_path,
                config,
                limit=5,
            )
            assert len(finance_results) == 1

            await delete_agent_memory(
                finance_results[0]["id"],
                "finance",
                storage_path,
                config,
            )

            assert not await search_agent_memories("delete source", "calculator", storage_path, config, limit=5)
            assert not await search_agent_memories("delete source", "finance", storage_path, config, limit=5)

    @pytest.mark.asyncio
    async def test_search_agent_memories_with_teams(
        self,
        mock_memory: AsyncMock,
        storage_path: Path,
        config: Config,
    ) -> None:
        config.teams = {"finance_team": MockTeamConfig(agents=["calculator", "data_analyst", "finance"])}

        def search_side_effect(query: str, *, filters: dict[str, str], top_k: int) -> dict:  # noqa: ARG001
            del top_k
            user_id = filters["user_id"]
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
