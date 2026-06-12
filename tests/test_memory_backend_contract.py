"""Contract tests running the same behavior against both memory backend adapters."""
# ruff: noqa: D103

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.memory._backend import resolve_memory_backend
from mindroom.memory._file_backend import FileMemoryBackend
from mindroom.memory._mem0_backend import Mem0MemoryBackend
from mindroom.memory._shared import MemoryNotFoundError
from tests.conftest import bind_runtime_paths, runtime_paths_for
from tests.memory_test_support import FakeMem0ScopedMemory, MockTeamConfig

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.memory._backend import MemoryBackend


@dataclass
class BackendEnv:
    """One adapter under test plus the config and storage it operates on."""

    backend: MemoryBackend
    config: Config
    storage_path: Path


def _contract_config(tmp_path: Path, memory_backend: str) -> Config:
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha", role="First assistant"),
                "beta": AgentConfig(display_name="Beta", role="Second assistant"),
            },
        ),
        runtime_paths,
    )
    config.memory.backend = memory_backend
    config.teams = {"pair": MockTeamConfig(agents=["alpha", "beta"])}
    return config


@pytest.fixture(params=["file", "mem0"])
def backend_env(request: pytest.FixtureRequest, tmp_path: Path) -> BackendEnv:
    config = _contract_config(tmp_path, request.param)
    runtime_paths = runtime_paths_for(config)
    backend: MemoryBackend
    if request.param == "file":
        backend = FileMemoryBackend(runtime_paths=runtime_paths)
    else:
        stores: dict[Path, FakeMem0ScopedMemory] = {}

        async def create_fake_memory(
            scope_storage_path: Path,
            _config: Config,
            *,
            timing_scope: str | None = None,
        ) -> FakeMem0ScopedMemory:
            del timing_scope
            return stores.setdefault(scope_storage_path, FakeMem0ScopedMemory())

        backend = Mem0MemoryBackend(runtime_paths=runtime_paths, create_memory=create_fake_memory)
    return BackendEnv(backend=backend, config=config, storage_path=tmp_path)


@pytest.mark.asyncio
async def test_add_search_get_update_delete_roundtrip(backend_env: BackendEnv) -> None:
    backend, config, storage_path = backend_env.backend, backend_env.config, backend_env.storage_path

    await backend.add("Prefers green tea in the morning", "alpha", storage_path, config)
    results = await backend.search("green tea", "alpha", storage_path, config, limit=5)
    assert [result["memory"] for result in results] == ["Prefers green tea in the morning"]
    memory_id = results[0]["id"]

    loaded = await backend.get(memory_id, "alpha", storage_path, config)
    assert loaded is not None
    assert loaded["memory"] == "Prefers green tea in the morning"

    await backend.update(memory_id, "Prefers black coffee instead", "alpha", storage_path, config)
    updated = await backend.get(memory_id, "alpha", storage_path, config)
    assert updated is not None
    assert updated["memory"] == "Prefers black coffee instead"

    await backend.delete(memory_id, "alpha", storage_path, config)
    assert await backend.get(memory_id, "alpha", storage_path, config) is None
    assert await backend.search("black coffee", "alpha", storage_path, config, limit=5) == []
    with pytest.raises(MemoryNotFoundError):
        await backend.delete(memory_id, "alpha", storage_path, config)


@pytest.mark.asyncio
async def test_list_all_returns_only_agent_scope(backend_env: BackendEnv) -> None:
    backend, config, storage_path = backend_env.backend, backend_env.config, backend_env.storage_path

    await backend.add("Alpha fact one", "alpha", storage_path, config)
    await backend.add("Alpha fact two", "alpha", storage_path, config)
    await backend.add("Beta fact", "beta", storage_path, config)

    alpha_memories = await backend.list_all("alpha", storage_path, config, limit=10)
    assert sorted(memory["memory"] for memory in alpha_memories) == ["Alpha fact one", "Alpha fact two"]
    beta_memories = await backend.list_all("beta", storage_path, config, limit=10)
    assert [memory["memory"] for memory in beta_memories] == ["Beta fact"]


@pytest.mark.asyncio
async def test_agent_scope_memories_invisible_to_other_agents(backend_env: BackendEnv) -> None:
    backend, config, storage_path = backend_env.backend, backend_env.config, backend_env.storage_path

    await backend.add("Alpha secret preference", "alpha", storage_path, config)
    assert await backend.search("Alpha secret", "beta", storage_path, config, limit=5) == []

    alpha_results = await backend.search("Alpha secret", "alpha", storage_path, config, limit=5)
    assert len(alpha_results) == 1
    memory_id = alpha_results[0]["id"]
    assert await backend.get(memory_id, "beta", storage_path, config) is None
    with pytest.raises(MemoryNotFoundError):
        await backend.update(memory_id, "Hijacked by beta", "beta", storage_path, config)


@pytest.mark.asyncio
async def test_team_conversation_memory_visible_to_members_but_not_member_scoped(backend_env: BackendEnv) -> None:
    backend, config, storage_path = backend_env.backend, backend_env.config, backend_env.storage_path

    await backend.store_conversation(
        "Quarterly metrics reviewed by the pair team",
        ["alpha", "beta"],
        storage_path,
        "session-team",
        config,
    )

    for member in ("alpha", "beta"):
        results = await backend.search("Quarterly metrics", member, storage_path, config, limit=5)
        assert [result["user_id"] for result in results] == ["team_alpha+beta"]
        assert results[0]["memory"] == "Quarterly metrics reviewed by the pair team"

    for member in ("alpha", "beta"):
        assert await backend.list_all(member, storage_path, config, limit=10) == []


@pytest.mark.asyncio
async def test_single_agent_conversation_memory_stays_member_scoped(backend_env: BackendEnv) -> None:
    backend, config, storage_path = backend_env.backend, backend_env.config, backend_env.storage_path

    await backend.store_conversation("Alpha solo reflection", "alpha", storage_path, "session-solo", config)

    alpha_results = await backend.search("Alpha solo", "alpha", storage_path, config, limit=5)
    assert [result["user_id"] for result in alpha_results] == ["agent_alpha"]
    assert await backend.search("Alpha solo", "beta", storage_path, config, limit=5) == []


def test_resolve_memory_backend_maps_scopes_to_adapters(tmp_path: Path) -> None:
    config = _contract_config(tmp_path, "file")
    config.agents["beta"].memory_backend = "mem0"
    runtime_paths = runtime_paths_for(config)

    assert isinstance(resolve_memory_backend("alpha", config, runtime_paths), FileMemoryBackend)
    assert isinstance(resolve_memory_backend("beta", config, runtime_paths), Mem0MemoryBackend)
    assert isinstance(resolve_memory_backend(["alpha", "alpha"], config, runtime_paths), FileMemoryBackend)
    # Mixed teams currently resolve to mem0; PR #798's partitioned-team fix
    # slots in here as a new MemoryBackend implementation.
    assert isinstance(resolve_memory_backend(["alpha", "beta"], config, runtime_paths), Mem0MemoryBackend)

    config.agents["alpha"].memory_backend = "none"
    assert resolve_memory_backend("alpha", config, runtime_paths) is None
    assert resolve_memory_backend(["alpha", "alpha"], config, runtime_paths) is None
