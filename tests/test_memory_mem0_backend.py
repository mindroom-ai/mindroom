"""Tests for mem0-specific memory behavior."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.memory.functions import (
    add_agent_memory,
    delete_agent_memory,
    get_agent_memory,
    list_all_agent_memories,
    search_agent_memories,
    store_conversation_memory,
    update_agent_memory,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_state_root_path,
    private_instance_state_root_path,
    resolve_worker_key,
    tool_execution_identity,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for
from tests.memory_test_support import FakeMem0ScopedMemory, MockTeamConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def storage_path(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def config(storage_path: Path) -> Config:
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
                "general": AgentConfig(display_name="General", role="General assistant"),
                "calculator": AgentConfig(display_name="Calculator", role="Calculator assistant"),
            },
        ),
        runtime_paths,
    )


@pytest.mark.asyncio
async def test_store_conversation_memory_uses_explicit_execution_identity_for_deferred_mem0_writes(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "mem0"
    config.agents["general"].worker_scope = "user"

    captured_calls: list[tuple[Path, str | None, dict[str, object]]] = []

    class FakeScopedMemory:
        def __init__(self, scope_storage_path: Path) -> None:
            self.scope_storage_path = scope_storage_path

        async def add(
            self,
            messages: list[dict],
            *,
            user_id: str | None = None,
            metadata: dict[str, object] | None = None,
        ) -> None:
            del messages
            captured_calls.append((self.scope_storage_path, user_id, metadata or {}))

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
    ) -> FakeScopedMemory:
        del runtime_paths
        return FakeScopedMemory(scope_storage_path)

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )

    with patch("mindroom.memory.functions.create_memory_instance", side_effect=create_fake_memory_instance):
        await store_conversation_memory(
            "Alice-authored shared memory",
            "general",
            storage_path,
            "session-alice",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )

    expected_storage_path = agent_state_root_path(storage_path, "general")
    assert captured_calls == [
        (
            expected_storage_path,
            "agent_general",
            {"type": "conversation", "session_id": "session-alice", "agent": "general"},
        ),
    ]


@pytest.mark.asyncio
async def test_private_agent_explicit_mem0_uses_private_instance_storage(
    storage_path: Path,
    config: Config,
) -> None:
    """Explicit mem0 CRUD for private agents should stay inside the private-instance root."""
    config.memory.backend = "mem0"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")

    memories_by_path: dict[Path, FakeMem0ScopedMemory] = {}

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
    ) -> FakeMem0ScopedMemory:
        del runtime_paths
        return memories_by_path.setdefault(scope_storage_path, FakeMem0ScopedMemory(id_prefix=scope_storage_path.name))

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    worker_key = resolve_worker_key("user", execution_identity, agent_name="general")

    assert worker_key is not None

    with (
        patch("mindroom.memory.functions.create_memory_instance", side_effect=create_fake_memory_instance),
        tool_execution_identity(execution_identity),
    ):
        await add_agent_memory("Private note", "general", storage_path, config, runtime_paths_for(config))
        search_results = await search_agent_memories(
            "Private note",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        listed = await list_all_agent_memories("general", storage_path, config, runtime_paths_for(config), limit=10)

        assert len(listed) == 1
        memory_id = listed[0]["id"]
        loaded = await get_agent_memory(memory_id, "general", storage_path, config, runtime_paths_for(config))
        assert loaded is not None

        await update_agent_memory(
            memory_id,
            "Updated private note",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
        )
        updated = await get_agent_memory(memory_id, "general", storage_path, config, runtime_paths_for(config))
        assert updated is not None
        assert updated["memory"] == "Updated private note"

        await delete_agent_memory(memory_id, "general", storage_path, config, runtime_paths_for(config))
        deleted = await get_agent_memory(memory_id, "general", storage_path, config, runtime_paths_for(config))
        assert deleted is None

    expected_private_path = private_instance_state_root_path(
        storage_path,
        worker_key=worker_key,
        agent_name="general",
    )
    assert set(memories_by_path) == {expected_private_path}
    assert agent_state_root_path(storage_path, "general") not in memories_by_path
    assert any(result.get("memory") == "Private note" for result in search_results)


@pytest.mark.asyncio
async def test_mem0_team_conversation_memory_is_shared_across_requesters_for_user_scoped_workers(
    storage_path: Path,
    config: Config,
) -> None:
    """User-scoped workers should still share one durable team memory view per agent set."""
    config.memory.backend = "mem0"
    config.agents["general"].worker_scope = "user"
    config.agents["calculator"].worker_scope = "user"
    config.teams = {"shared_team": MockTeamConfig(agents=["general", "calculator"])}

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="team",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="team",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-bob",
    )

    stored_memories: dict[tuple[Path, str], list[dict[str, object]]] = {}

    class FakeScopedMemory:
        def __init__(self, scope_storage_path: Path) -> None:
            self.scope_storage_path = scope_storage_path

        async def add(self, messages: list[dict], user_id: str, metadata: dict) -> None:
            entry = {
                "id": f"{user_id}-{len(stored_memories)}",
                "memory": " ".join(str(message["content"]).strip() for message in messages if message.get("content")),
                "user_id": user_id,
                "metadata": metadata,
            }
            stored_memories.setdefault((self.scope_storage_path, user_id), []).append(entry)

        async def search(self, query: str, user_id: str, limit: int = 3) -> dict[str, list[dict[str, object]]]:
            matches = [
                dict(entry)
                for entry in stored_memories.get((self.scope_storage_path, user_id), [])
                if query.lower() in str(entry["memory"]).lower()
            ]
            return {"results": matches[:limit]}

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
    ) -> FakeScopedMemory:
        del runtime_paths
        return FakeScopedMemory(scope_storage_path)

    with patch("mindroom.memory.functions.create_memory_instance", side_effect=create_fake_memory_instance):
        with tool_execution_identity(alice_identity):
            await store_conversation_memory(
                "Alice-authored shared team memory",
                ["general", "calculator"],
                storage_path,
                "session-alice",
                config,
                runtime_paths_for(config),
            )
            alice_results = await search_agent_memories(
                "Alice-authored shared team",
                "general",
                storage_path,
                config,
                runtime_paths_for(config),
                limit=5,
            )

        with tool_execution_identity(bob_identity):
            bob_results = await search_agent_memories(
                "Alice-authored shared team",
                "general",
                storage_path,
                config,
                runtime_paths_for(config),
                limit=5,
            )

    assert any(result.get("memory") == "Alice-authored shared team memory" for result in alice_results)
    assert any(result.get("memory") == "Alice-authored shared team memory" for result in bob_results)

    assert (agent_state_root_path(storage_path, "general"), "team_calculator+general") in stored_memories
    assert (agent_state_root_path(storage_path, "calculator"), "team_calculator+general") in stored_memories
    assert (storage_path, "team_calculator+general") not in stored_memories


@pytest.mark.asyncio
async def test_mixed_private_team_mem0_conversation_memory_is_rejected(
    storage_path: Path,
    config: Config,
) -> None:
    """Mem0 team memory should reject private team members outright."""
    config.memory.backend = "mem0"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    with pytest.raises(ValueError, match="private agents cannot participate in teams yet"):
        await store_conversation_memory(
            "Alice-authored private team memory",
            ["general", "calculator"],
            storage_path,
            "session-alice",
            config,
            runtime_paths_for(config),
        )


@pytest.mark.asyncio
async def test_mixed_private_team_mem0_member_crud_is_rejected(
    storage_path: Path,
    config: Config,
) -> None:
    """Mem0 team member CRUD should reject private team members."""
    config.memory.backend = "mem0"
    config.memory.team_reads_member_memory = True
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    memories_by_path: dict[Path, FakeMem0ScopedMemory] = {}

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
    ) -> FakeMem0ScopedMemory:
        del runtime_paths
        id_prefix = scope_storage_path.name.replace("/", "_") or "mem"
        return memories_by_path.setdefault(scope_storage_path, FakeMem0ScopedMemory(id_prefix=id_prefix))

    with (
        patch("mindroom.memory.functions.create_memory_instance", side_effect=create_fake_memory_instance),
    ):
        await add_agent_memory("Shared calculator note", "calculator", storage_path, config, runtime_paths_for(config))
        calculator_memory_id = (
            await list_all_agent_memories("calculator", storage_path, config, runtime_paths_for(config), limit=10)
        )[0]["id"]

        with pytest.raises(ValueError, match="private agents cannot participate in teams yet"):
            await get_agent_memory(
                calculator_memory_id,
                ["general", "calculator"],
                storage_path,
                config,
                runtime_paths_for(config),
            )

        with pytest.raises(ValueError, match="private agents cannot participate in teams yet"):
            await update_agent_memory(
                calculator_memory_id,
                "Updated shared calculator note",
                ["general", "calculator"],
                storage_path,
                config,
                runtime_paths_for(config),
            )

        with pytest.raises(ValueError, match="private agents cannot participate in teams yet"):
            await delete_agent_memory(
                calculator_memory_id,
                ["general", "calculator"],
                storage_path,
                config,
                runtime_paths_for(config),
            )

    assert agent_state_root_path(storage_path, "calculator") in memories_by_path


@pytest.mark.asyncio
async def test_worker_scoped_team_mem0_memory_can_be_read_updated_and_deleted_across_worker_roots(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "mem0"
    config.agents["general"].worker_scope = "user_agent"
    config.agents["calculator"].worker_scope = "user_agent"
    config.teams = {"gc": MockTeamConfig(agents=["general", "calculator"])}

    memories_by_path: dict[Path, FakeMem0ScopedMemory] = {}

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
    ) -> FakeMem0ScopedMemory:
        del runtime_paths
        id_prefix = scope_storage_path.name.replace("/", "_") or "mem"
        return memories_by_path.setdefault(scope_storage_path, FakeMem0ScopedMemory(id_prefix=id_prefix))

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="!room:example.org:$thread",
    )

    with (
        patch("mindroom.memory.functions.create_memory_instance", side_effect=create_fake_memory_instance),
        tool_execution_identity(execution_identity),
    ):
        await store_conversation_memory(
            "Team shared note",
            ["general", "calculator"],
            storage_path,
            "session-alice",
            config,
            runtime_paths_for(config),
        )

        general_results = await search_agent_memories(
            "shared note",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        calculator_results = await search_agent_memories(
            "shared note",
            "calculator",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        assert len(general_results) == 1
        assert len(calculator_results) == 1
        general_memory_id = general_results[0]["id"]
        calculator_memory_id = calculator_results[0]["id"]
        assert general_memory_id != calculator_memory_id

        general_loaded = await get_agent_memory(
            general_memory_id,
            ["general", "calculator"],
            storage_path,
            config,
            runtime_paths_for(config),
        )
        calculator_loaded = await get_agent_memory(
            calculator_memory_id,
            ["general", "calculator"],
            storage_path,
            config,
            runtime_paths_for(config),
        )
        assert general_loaded is not None
        assert calculator_loaded is not None
        assert general_loaded["memory"] == "Team shared note"
        assert calculator_loaded["memory"] == "Team shared note"

        await update_agent_memory(
            calculator_memory_id,
            "Updated team shared note",
            ["general", "calculator"],
            storage_path,
            config,
            runtime_paths_for(config),
        )

        general_updated = await search_agent_memories(
            "updated team",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        calculator_updated = await search_agent_memories(
            "updated team",
            "calculator",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        assert any(result.get("memory") == "Updated team shared note" for result in general_updated)
        assert any(result.get("memory") == "Updated team shared note" for result in calculator_updated)

        await delete_agent_memory(
            general_memory_id,
            ["general", "calculator"],
            storage_path,
            config,
            runtime_paths_for(config),
        )

        general_deleted = await search_agent_memories(
            "team",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        calculator_deleted = await search_agent_memories(
            "team",
            "calculator",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        assert not any(result.get("memory") == "Team shared note" for result in general_deleted)
        assert not any(result.get("memory") == "Team shared note" for result in calculator_deleted)
        assert not any(result.get("memory") == "Updated team shared note" for result in general_deleted)
        assert not any(result.get("memory") == "Updated team shared note" for result in calculator_deleted)
