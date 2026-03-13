"""Tests for the file-backed memory implementation and file-specific facade paths."""
# ruff: noqa: D103

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.main import Config
from mindroom.memory.functions import (
    add_agent_memory,
    add_room_memory,
    append_agent_daily_memory,
    build_memory_enhanced_prompt,
    delete_agent_memory,
    get_agent_memory,
    list_all_agent_memories,
    search_agent_memories,
    search_room_memories,
    store_conversation_memory,
    update_agent_memory,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_state_root_path,
    agent_workspace_root_path,
    tool_execution_identity,
)
from tests.memory_test_support import MockTeamConfig

if TYPE_CHECKING:
    from pathlib import Path


def _canonical_absolute_agent_path(storage_path: Path, agent_name: str, source_path: Path) -> Path:
    parts = list(source_path.parts)
    if source_path.anchor and parts and parts[0] == source_path.anchor:
        parts = parts[1:]

    canonical_path = agent_workspace_root_path(storage_path, agent_name) / "_absolute"
    if source_path.anchor not in {"", "/", "\\"}:
        canonical_path /= re.sub(r"[^a-zA-Z0-9._@+-]+", "_", source_path.anchor.replace(":", "")).strip("_") or "root"
    return canonical_path.joinpath(*parts)


@pytest.fixture
def storage_path(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def config() -> Config:
    return Config.from_yaml()


@pytest.mark.asyncio
async def test_file_backend_add_and_list_memories(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    await add_agent_memory("User prefers concise responses", "general", storage_path, config)

    results = await list_all_agent_memories("general", storage_path, config)
    assert len(results) == 1
    assert results[0]["memory"] == "User prefers concise responses"
    assert results[0]["id"].startswith("m_")

    memory_file = agent_state_root_path(storage_path, "general") / "memory_files" / "agent_general" / "MEMORY.md"
    assert memory_file.exists()
    assert "User prefers concise responses" in memory_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_file_backend_user_scoped_workers_share_agent_memory_across_requesters(
    storage_path: Path,
    config: Config,
) -> None:
    """Requester-scoped workers still share one durable agent memory root."""
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].worker_scope = "user"

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-bob",
    )

    with tool_execution_identity(alice_identity):
        await add_agent_memory("Alice-authored shared agent memory", "general", storage_path, config)
        alice_results = await search_agent_memories("Alice-authored shared", "general", storage_path, config, limit=5)
        alice_prompt = await build_memory_enhanced_prompt("What do you remember?", "general", storage_path, config)

    with tool_execution_identity(bob_identity):
        bob_results = await search_agent_memories("Alice-authored shared", "general", storage_path, config, limit=5)
        bob_prompt = await build_memory_enhanced_prompt("What do you remember?", "general", storage_path, config)

    assert any(result.get("memory") == "Alice-authored shared agent memory" for result in alice_results)
    assert any(result.get("memory") == "Alice-authored shared agent memory" for result in bob_results)
    assert "Alice-authored shared agent memory" in alice_prompt
    assert "Alice-authored shared agent memory" in bob_prompt

    memory_file = agent_state_root_path(storage_path, "general") / "memory_files" / "agent_general" / "MEMORY.md"
    assert memory_file.exists()


@pytest.mark.asyncio
async def test_file_backend_worker_scope_prompt_reads_daily_memory_from_base_storage_path(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].worker_scope = "user"
    config.memory.file.max_entrypoint_lines = 0

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    with tool_execution_identity(alice_identity):
        append_agent_daily_memory("Worker daily note", "general", storage_path, config)
        prompt = await build_memory_enhanced_prompt("daily note", "general", storage_path, config)

    assert "Worker daily note" in prompt


@pytest.mark.asyncio
async def test_file_backend_worker_scope_ignores_global_memory_file_path(
    storage_path: Path,
    config: Config,
) -> None:
    """User-scoped workers should still persist file memory under the agent-owned root."""
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "shared-memory")
    config.agents["general"].memory_backend = "file"
    config.agents["general"].worker_scope = "user"

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-bob",
    )

    with tool_execution_identity(alice_identity):
        await add_agent_memory("Alice-authored shared memory", "general", storage_path, config)

    with tool_execution_identity(bob_identity):
        bob_results = await search_agent_memories("Alice-authored shared", "general", storage_path, config, limit=5)

    assert any(result.get("memory") == "Alice-authored shared memory" for result in bob_results)
    assert not (storage_path / "shared-memory" / "agent_general" / "MEMORY.md").exists()
    assert (agent_state_root_path(storage_path, "general") / "memory_files" / "agent_general" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_file_backend_team_conversation_memory_reuses_member_agent_roots(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["calculator"].memory_backend = "file"
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

    with tool_execution_identity(alice_identity):
        await store_conversation_memory(
            "Alice-authored shared team memory",
            ["general", "calculator"],
            storage_path,
            "session-alice",
            config,
            room_id="!room:example.org",
        )
        alice_results = await search_agent_memories(
            "Alice-authored shared team",
            "general",
            storage_path,
            config,
            limit=5,
        )

    with tool_execution_identity(bob_identity):
        bob_results = await search_agent_memories(
            "Alice-authored shared team",
            "general",
            storage_path,
            config,
            limit=5,
        )

    assert any(result.get("memory") == "Alice-authored shared team memory" for result in alice_results)
    assert any(result.get("memory") == "Alice-authored shared team memory" for result in bob_results)
    assert (
        agent_state_root_path(storage_path, "general") / "memory_files" / "team_calculator+general" / "MEMORY.md"
    ).exists()
    assert (
        agent_state_root_path(storage_path, "calculator") / "memory_files" / "team_calculator+general" / "MEMORY.md"
    ).exists()
    assert not (storage_path / "memory_files" / "team_calculator+general" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_file_backend_prompt_includes_entrypoint(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    memory_dir = agent_state_root_path(storage_path, "general") / "memory_files" / "agent_general"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("# Memory\n\nKey facts:\n- Project uses FastAPI.\n", encoding="utf-8")

    enhanced = await build_memory_enhanced_prompt("How do we build the API?", "general", storage_path, config)
    assert "[File memory entrypoint (agent)]" in enhanced
    assert "Project uses FastAPI." in enhanced
    assert "How do we build the API?" in enhanced


@pytest.mark.asyncio
async def test_file_backend_prompt_preserves_curated_entrypoint_lines_with_structured_memory(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")
    config.memory.file.max_entrypoint_lines = 10

    memory_dir = agent_state_root_path(storage_path, "general") / "memory_files" / "agent_general"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("# Memory\n\nCurated fact.\n- [id=m1] Structured fact.\n", encoding="utf-8")

    enhanced = await build_memory_enhanced_prompt("What should I remember?", "general", storage_path, config)
    assert "Curated fact." in enhanced
    assert "- [id=m1] Structured fact." in enhanced


@pytest.mark.asyncio
async def test_file_backend_prompt_respects_max_entrypoint_lines(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")
    config.memory.file.max_entrypoint_lines = 2

    memory_dir = agent_state_root_path(storage_path, "general") / "memory_files" / "agent_general"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(
        "# Memory\nCurated fact.\n- [id=m1] Structured fact.\nTrailing fact.\n",
        encoding="utf-8",
    )

    enhanced = await build_memory_enhanced_prompt("What should I remember?", "general", storage_path, config)
    assert "# Memory\nCurated fact." in enhanced
    assert "Structured fact." not in enhanced
    assert "Trailing fact." not in enhanced


@pytest.mark.asyncio
async def test_file_backend_room_prompt_search_uses_agent_override(storage_path: Path, config: Config) -> None:
    config.memory.backend = "mem0"
    config.memory.file.path = str(storage_path / "memory-files")
    config.agents["general"].memory_backend = "file"

    await add_room_memory("Room memory note", "!room:server", storage_path, config, agent_name="general")

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
async def test_file_backend_search_skips_structured_line_duplicates(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    await add_agent_memory("Project owner is Bas", "general", storage_path, config)
    memories = await list_all_agent_memories("general", storage_path, config)
    memory_id = memories[0]["id"]

    daily_file = (
        agent_state_root_path(storage_path, "general") / "memory_files" / "agent_general" / "memory" / "2026-02-28.md"
    )
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text(f"- [id={memory_id}] Project owner is Bas\nProject owner is Bas\n", encoding="utf-8")

    results = await search_agent_memories("owner bas", "general", storage_path, config, limit=10)
    matching_results = [result for result in results if result.get("memory") == "Project owner is Bas"]
    assert len(matching_results) == 1


@pytest.mark.asyncio
async def test_file_backend_memory_crud_and_scope(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    await add_agent_memory("Original memory", "general", storage_path, config)
    listed = await list_all_agent_memories("general", storage_path, config)
    memory_id = listed[0]["id"]

    result = await get_agent_memory(memory_id, "general", storage_path, config)
    assert result is not None
    assert result["memory"] == "Original memory"

    await update_agent_memory(memory_id, "Updated memory", "general", storage_path, config)
    updated = await get_agent_memory(memory_id, "general", storage_path, config)
    assert updated is not None
    assert updated["memory"] == "Updated memory"

    await delete_agent_memory(memory_id, "general", storage_path, config)
    assert await get_agent_memory(memory_id, "general", storage_path, config) is None

    await add_agent_memory("Private memory", "general", storage_path, config)
    private_id = (await list_all_agent_memories("general", storage_path, config))[0]["id"]
    assert await get_agent_memory(private_id, "other_agent", storage_path, config) is None
    with pytest.raises(ValueError, match=f"No memory found with id={private_id}"):
        await update_agent_memory(private_id, "Tampered", "other_agent", storage_path, config)


@pytest.mark.asyncio
async def test_file_backend_store_conversation_memory_with_room(storage_path: Path, config: Config) -> None:
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
    room_results = await search_room_memories(
        "requirement",
        "!room:server",
        storage_path,
        config,
        agent_name="general",
        limit=5,
    )
    assert any("Remember this requirement" in result.get("memory", "") for result in agent_results)
    assert any("Remember this requirement" in result.get("memory", "") for result in room_results)


@pytest.mark.asyncio
async def test_file_backend_team_scopes_do_not_collide(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    await store_conversation_memory("Team one memory", ["a_b", "c"], storage_path, "session-one", config)
    await store_conversation_memory("Team two memory", ["a", "b_c"], storage_path, "session-two", config)

    assert (agent_state_root_path(storage_path, "a_b") / "memory_files" / "team_a_b+c" / "MEMORY.md").exists()
    assert (agent_state_root_path(storage_path, "a") / "memory_files" / "team_a+b_c" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_file_backend_team_context_member_scope_toggle(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    await add_agent_memory("Helper private memory", "helper", storage_path, config)
    helper_memory_id = (await list_all_agent_memories("helper", storage_path, config))[0]["id"]

    assert await get_agent_memory(helper_memory_id, ["helper", "test_agent"], storage_path, config) is None

    config.memory.team_reads_member_memory = True
    allowed = await get_agent_memory(helper_memory_id, ["helper", "test_agent"], storage_path, config)
    assert allowed is not None
    assert allowed["memory"] == "Helper private memory"


@pytest.mark.asyncio
async def test_team_can_crud_member_memory_in_custom_memory_file_path(
    storage_path: Path,
    config: Config,
) -> None:
    workspace = storage_path / "general-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nSeeded note.\n", encoding="utf-8")

    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["calculator"].memory_backend = "file"
    config.agents["general"].memory_file_path = str(workspace)
    config.memory.team_reads_member_memory = True
    config.teams = {"gc": MockTeamConfig(agents=["general", "calculator"])}

    await add_agent_memory("General private note", "general", storage_path, config)
    memory_id = (await list_all_agent_memories("general", storage_path, config))[0]["id"]

    loaded = await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
    assert loaded is not None
    assert loaded["memory"] == "General private note"

    await update_agent_memory(
        memory_id,
        "Updated general private note",
        ["general", "calculator"],
        storage_path,
        config,
    )
    updated = await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
    assert updated is not None
    assert updated["memory"] == "Updated general private note"
    canonical_workspace = _canonical_absolute_agent_path(storage_path, "general", workspace)
    assert "Seeded note." in (canonical_workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert "Updated general private note" in (canonical_workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert "Updated general private note" not in (workspace / "MEMORY.md").read_text(encoding="utf-8")

    await delete_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
    assert await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config) is None
    assert "Updated general private note" not in (canonical_workspace / "MEMORY.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_team_can_crud_member_memory_in_canonical_agent_memory_file_path(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["calculator"].memory_backend = "file"
    config.agents["general"].worker_scope = "user_agent"
    config.agents["calculator"].worker_scope = "user_agent"
    config.agents["general"].memory_file_path = "./mind_data"
    config.memory.team_reads_member_memory = True
    config.teams = {"gc": MockTeamConfig(agents=["general", "calculator"])}

    config_dir = storage_path / "cfg"
    source_workspace = config_dir / "mind_data"
    source_workspace.mkdir(parents=True, exist_ok=True)
    (source_workspace / "MEMORY.md").write_text("# Memory\n\nSeeded note.\n", encoding="utf-8")

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
        patch("mindroom.constants.CONFIG_PATH", config_dir / "config.yaml"),
        tool_execution_identity(execution_identity),
    ):
        await add_agent_memory("Runtime-authored general note", "general", storage_path, config)
        memory_id = (await list_all_agent_memories("general", storage_path, config))[0]["id"]

        loaded = await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
        assert loaded is not None
        assert loaded["memory"] == "Runtime-authored general note"

        await update_agent_memory(
            memory_id,
            "Updated runtime-authored general note",
            ["general", "calculator"],
            storage_path,
            config,
        )
        updated = await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
        assert updated is not None
        assert updated["memory"] == "Updated runtime-authored general note"

        await delete_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
        assert await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config) is None

    canonical_memory_file = agent_workspace_root_path(storage_path, "general") / "mind_data" / "MEMORY.md"
    canonical_content = canonical_memory_file.read_text(encoding="utf-8")
    assert "Seeded note." in canonical_content
    assert "Updated runtime-authored general note" not in canonical_content
    assert "Runtime-authored general note" not in canonical_content


@pytest.mark.asyncio
async def test_worker_scoped_team_file_memory_can_be_read_updated_and_deleted(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].worker_scope = "user_agent"
    config.agents["calculator"].worker_scope = "user_agent"
    config.teams = {"gc": MockTeamConfig(agents=["general", "calculator"])}

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="!room:example.org:$thread",
    )

    with tool_execution_identity(execution_identity):
        await store_conversation_memory(
            "Team shared note",
            ["general", "calculator"],
            storage_path,
            "session-alice",
            config,
        )

        general_results = await search_agent_memories("shared note", "general", storage_path, config, limit=10)
        calculator_results = await search_agent_memories("shared note", "calculator", storage_path, config, limit=10)
        assert len(general_results) == 1
        assert len(calculator_results) == 1
        memory_id = general_results[0]["id"]
        assert calculator_results[0]["id"] == memory_id

        loaded = await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
        assert loaded is not None
        assert loaded["memory"] == "Team shared note"

        await update_agent_memory(
            memory_id,
            "Updated team shared note",
            ["general", "calculator"],
            storage_path,
            config,
        )
        updated = await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
        assert updated is not None
        assert updated["memory"] == "Updated team shared note"

        general_updated = await search_agent_memories("updated team", "general", storage_path, config, limit=10)
        calculator_updated = await search_agent_memories("updated team", "calculator", storage_path, config, limit=10)
        assert any(result.get("memory") == "Updated team shared note" for result in general_updated)
        assert any(result.get("memory") == "Updated team shared note" for result in calculator_updated)

        await delete_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
        assert await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config) is None

        general_deleted = await search_agent_memories("updated team", "general", storage_path, config, limit=10)
        calculator_deleted = await search_agent_memories("updated team", "calculator", storage_path, config, limit=10)
        assert not any(result.get("memory") == "Updated team shared note" for result in general_deleted)
        assert not any(result.get("memory") == "Updated team shared note" for result in calculator_deleted)


@pytest.mark.asyncio
async def test_file_backend_rejects_path_traversal_memory_id(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    await add_agent_memory("Safe memory", "general", storage_path, config)
    secret_file = storage_path / "secret.md"
    secret_file.write_text("Do not read", encoding="utf-8")

    assert await get_agent_memory("file:../../secret.md:1", "general", storage_path, config) is None


@pytest.mark.asyncio
async def test_memory_file_path_bootstraps_absolute_path_into_canonical_agent_scope(
    storage_path: Path,
    config: Config,
) -> None:
    workspace = storage_path / "my-workspace"
    workspace.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nExisting curated memory.\n", encoding="utf-8")

    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = str(workspace)

    await add_agent_memory("New memory entry", "general", storage_path, config)

    canonical_workspace = _canonical_absolute_agent_path(storage_path, "general", workspace)
    content = (canonical_workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert "Existing curated memory." in content
    assert "New memory entry" in content
    assert "New memory entry" not in (workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert not (storage_path / "memory_files" / "agent_general").exists()


@pytest.mark.asyncio
async def test_relative_memory_file_path_supports_crud(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = "./mind_data"

    with patch("mindroom.constants.CONFIG_PATH", storage_path / "config.yaml"):
        await add_agent_memory("Original memory", "general", storage_path, config)
        memory_id = (await list_all_agent_memories("general", storage_path, config))[0]["id"]

        result = await get_agent_memory(memory_id, "general", storage_path, config)
        assert result is not None
        assert result["memory"] == "Original memory"

        await update_agent_memory(memory_id, "Updated memory", "general", storage_path, config)
        updated = await get_agent_memory(memory_id, "general", storage_path, config)
        assert updated is not None
        assert updated["memory"] == "Updated memory"

        await delete_agent_memory(memory_id, "general", storage_path, config)
        assert await get_agent_memory(memory_id, "general", storage_path, config) is None


@pytest.mark.asyncio
async def test_worker_scoped_memory_file_path_uses_canonical_agent_scope(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].worker_scope = "user"
    config.agents["general"].memory_file_path = "./mind_data"

    config_dir = storage_path / "cfg"
    source_workspace = config_dir / "mind_data"
    source_workspace.mkdir(parents=True, exist_ok=True)
    (source_workspace / "MEMORY.md").write_text("# Memory\n\nExisting worker memory.\n", encoding="utf-8")

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    with patch("mindroom.constants.CONFIG_PATH", config_dir / "config.yaml"), tool_execution_identity(alice_identity):
        await add_agent_memory("New worker memory", "general", storage_path, config)
        prompt = await build_memory_enhanced_prompt("worker memory", "general", storage_path, config)

    canonical_workspace = agent_workspace_root_path(storage_path, "general") / "mind_data"
    content = (canonical_workspace / "MEMORY.md").read_text(encoding="utf-8")

    assert "Existing worker memory." in content
    assert "New worker memory" in content
    assert "Existing worker memory." in prompt
    assert not (storage_path / "memory_files" / "agent_general").exists()


@pytest.mark.asyncio
async def test_memory_file_path_entrypoint_loaded_in_prompt(storage_path: Path, config: Config) -> None:
    workspace = storage_path / "my-workspace"
    workspace.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nI prefer Python over JavaScript.\n", encoding="utf-8")

    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = str(workspace)

    enhanced = await build_memory_enhanced_prompt("What language?", "general", storage_path, config)
    assert "I prefer Python over JavaScript." in enhanced
    canonical_workspace = _canonical_absolute_agent_path(storage_path, "general", workspace)
    assert (canonical_workspace / "MEMORY.md").read_text(encoding="utf-8").startswith("# Memory")


@pytest.mark.asyncio
async def test_memory_file_path_daily_files_in_custom_scope(storage_path: Path, config: Config) -> None:
    workspace = storage_path / "my-workspace"
    workspace.mkdir(parents=True)

    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = str(workspace)

    result = append_agent_daily_memory("Daily note", "general", storage_path, config)
    assert result["memory"] == "Daily note"

    canonical_workspace = _canonical_absolute_agent_path(storage_path, "general", workspace)
    daily_files = list((canonical_workspace / "memory").rglob("*.md"))
    assert len(daily_files) == 1
    assert "Daily note" in daily_files[0].read_text(encoding="utf-8")
    assert not list((workspace / "memory").rglob("*.md"))


@pytest.mark.asyncio
async def test_memory_file_path_does_not_affect_other_agents(storage_path: Path, config: Config) -> None:
    workspace = storage_path / "my-workspace"
    workspace.mkdir(parents=True)

    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")
    config.agents["general"].memory_file_path = str(workspace)

    await add_agent_memory("Custom workspace memory", "general", storage_path, config)
    await add_agent_memory("Default scope memory", "calculator", storage_path, config)

    general_memories = await list_all_agent_memories("general", storage_path, config)
    assert any(memory["memory"] == "Custom workspace memory" for memory in general_memories)

    calc_memories = await list_all_agent_memories("calculator", storage_path, config)
    assert any(memory["memory"] == "Default scope memory" for memory in calc_memories)
    general_workspace = _canonical_absolute_agent_path(storage_path, "general", workspace)
    assert (general_workspace / "MEMORY.md").exists()
    assert not (workspace / "MEMORY.md").exists()
    assert (
        agent_state_root_path(storage_path, "calculator") / "memory_files" / "agent_calculator" / "MEMORY.md"
    ).exists()
