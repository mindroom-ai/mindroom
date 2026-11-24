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
from tests.memory_test_support import FakeMem0ScopedMemory, MockTeamConfig, fake_mem0_stores

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.memory._backend import ResolvedMemoryBackend


@dataclass
class BackendEnv:
    """One adapter under test plus the config and storage it operates on, and the Mem0 stores it opened."""

    backend: ResolvedMemoryBackend
    config: Config
    storage_path: Path
    stores: dict[Path, FakeMem0ScopedMemory]


def _contract_config(tmp_path: Path, memory_backend: str) -> Config:
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha", role="First assistant"),
                "beta": AgentConfig(display_name="Beta", role="Second assistant"),
                "gamma": AgentConfig(display_name="Gamma", role="Third assistant"),
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
    stores, create_memory = fake_mem0_stores()
    backend: ResolvedMemoryBackend
    if request.param == "file":
        backend = FileMemoryBackend(runtime_paths=runtime_paths)
    else:
        backend = Mem0MemoryBackend(runtime_paths=runtime_paths, create_memory=create_memory)
    return BackendEnv(backend=backend, config=config, storage_path=tmp_path, stores=stores)


@pytest.mark.asyncio
async def test_add_search_get_update_delete_roundtrip(backend_env: BackendEnv) -> None:
    backend, config = backend_env.backend, backend_env.config

    await backend.add("Prefers green tea in the morning", "alpha", config)
    results = (await backend.search("green tea", "alpha", config, limit=5)).results
    assert [result["memory"] for result in results] == ["Prefers green tea in the morning"]
    memory_id = results[0]["id"]

    loaded = await backend.get(memory_id, "alpha", config)
    assert loaded is not None
    assert loaded["memory"] == "Prefers green tea in the morning"

    await backend.update(memory_id, "Prefers black coffee instead", "alpha", config)
    updated = await backend.get(memory_id, "alpha", config)
    assert updated is not None
    assert updated["memory"] == "Prefers black coffee instead"

    await backend.delete(memory_id, "alpha", config)
    assert await backend.get(memory_id, "alpha", config) is None
    assert (await backend.search("black coffee", "alpha", config, limit=5)).results == []
    with pytest.raises(MemoryNotFoundError):
        await backend.delete(memory_id, "alpha", config)


@pytest.mark.asyncio
async def test_list_all_returns_only_agent_scope(backend_env: BackendEnv) -> None:
    backend, config = backend_env.backend, backend_env.config

    await backend.add("Alpha fact one", "alpha", config)
    await backend.add("Alpha fact two", "alpha", config)
    await backend.add("Beta fact", "beta", config)

    alpha_memories = await backend.list_all("alpha", config, limit=10)
    assert sorted(memory["memory"] for memory in alpha_memories) == ["Alpha fact one", "Alpha fact two"]
    beta_memories = await backend.list_all("beta", config, limit=10)
    assert [memory["memory"] for memory in beta_memories] == ["Beta fact"]


@pytest.mark.asyncio
async def test_agent_scope_memories_invisible_to_other_agents(backend_env: BackendEnv) -> None:
    backend, config = backend_env.backend, backend_env.config

    await backend.add("Alpha secret preference", "alpha", config)
    assert (await backend.search("Alpha secret", "beta", config, limit=5)).results == []

    alpha_results = (await backend.search("Alpha secret", "alpha", config, limit=5)).results
    assert len(alpha_results) == 1
    memory_id = alpha_results[0]["id"]
    assert await backend.get(memory_id, "beta", config) is None
    with pytest.raises(MemoryNotFoundError):
        await backend.update(memory_id, "Hijacked by beta", "beta", config)


@pytest.mark.asyncio
async def test_single_agent_conversation_memory_stays_member_scoped(backend_env: BackendEnv) -> None:
    backend, config = backend_env.backend, backend_env.config

    await backend.store_conversation("Alpha solo reflection", "alpha", "session-solo", config)

    alpha_results = (await backend.search("Alpha solo", "alpha", config, limit=5)).results
    assert [result["user_id"] for result in alpha_results] == ["agent_alpha"]
    assert (await backend.search("Alpha solo", "beta", config, limit=5)).results == []


@pytest.mark.asyncio
async def test_a_team_turn_writes_once_to_the_one_store_every_member_reads_and_edits(backend_env: BackendEnv) -> None:
    backend, config, storage_path = backend_env.backend, backend_env.config, backend_env.storage_path

    for turn in ("Quarterly metrics reviewed", "Quarterly budget approved", "Quarterly hiring paused"):
        await backend.store_conversation(turn, ["alpha", "beta"], "session-team", config)
    found = {
        result["memory"]: result["id"]
        for result in (await backend.search("Quarterly", "alpha", config, limit=5)).results
    }

    await backend.update(found["Quarterly metrics reviewed"], "Quarterly metrics reviewed twice", "alpha", config)
    await backend.delete(found["Quarterly budget approved"], "beta", config)

    for member in ("alpha", "beta"):
        results = (await backend.search("Quarterly", member, config, limit=5)).results
        assert sorted(result["memory"] for result in results) == [
            "Quarterly hiring paused",
            "Quarterly metrics reviewed twice",
        ]
        assert {result["user_id"] for result in results} == {"team_alpha+beta"}
        assert await backend.list_all(member, config, limit=10) == []
    # Mem0 keeps every team's scope in one store; file memory gives each team its own directory.
    is_mem0 = isinstance(backend, Mem0MemoryBackend)
    team_store = storage_path / "team_memory" if is_mem0 else storage_path / "team_memory" / "team_alpha+beta"
    assert team_store.is_dir()
    assert not any((storage_path / "agents").glob("*/memory_files"))
    holding = {root for root, store in backend_env.stores.items() if (await store.get_all())["results"]}
    assert holding == ({team_store} if is_mem0 else set())


@pytest.mark.asyncio
async def test_a_member_never_sees_edits_or_deletes_another_teams_memories(backend_env: BackendEnv) -> None:
    """Each member reaches only its own teams' scopes, though Mem0 keeps every team's scope in one store."""
    backend, config = backend_env.backend, backend_env.config
    config.teams["other"] = MockTeamConfig(agents=["beta", "gamma"])
    await backend.store_conversation("Pair roadmap drafted", ["alpha", "beta"], "session-pair", config)
    await backend.store_conversation("Other roadmap drafted", ["beta", "gamma"], "session-other", config)
    (other,) = (await backend.search("roadmap", "gamma", config, limit=5)).results

    alpha_results = (await backend.search("roadmap", "alpha", config, limit=5)).results
    assert [result["memory"] for result in alpha_results] == ["Pair roadmap drafted"]
    assert await backend.get(other["id"], "alpha", config) is None
    with pytest.raises(MemoryNotFoundError):
        await backend.update(other["id"], "Other roadmap rewritten", "alpha", config)
    with pytest.raises(MemoryNotFoundError):
        await backend.delete(other["id"], "alpha", config)

    beta_results = (await backend.search("roadmap", "beta", config, limit=5)).results
    assert sorted(result["memory"] for result in beta_results) == ["Other roadmap drafted", "Pair roadmap drafted"]
    assert [result["memory"] for result in (await backend.search("roadmap", "gamma", config, limit=5)).results] == [
        "Other roadmap drafted",
    ]


@pytest.mark.asyncio
async def test_reads_create_no_team_store(backend_env: BackendEnv) -> None:
    backend, config, storage_path = backend_env.backend, backend_env.config, backend_env.storage_path

    await backend.add("Alpha keeps notes", "alpha", config)
    assert [result["memory"] for result in (await backend.search("Alpha", "alpha", config, limit=5)).results] == [
        "Alpha keeps notes",
    ]
    assert await backend.get("unknown-id", "alpha", config) is None

    assert not (storage_path / "team_memory").exists()


@pytest.mark.asyncio
async def test_a_member_still_reads_its_teams_store_after_a_teammate_turns_memory_off(backend_env: BackendEnv) -> None:
    backend, config = backend_env.backend, backend_env.config
    await backend.store_conversation("Quarterly metrics reviewed", ["alpha", "beta"], "session-team", config)

    config.agents["beta"].memory_backend = "none"

    results = (await backend.search("Quarterly", "alpha", config, limit=5)).results
    assert [result["memory"] for result in results] == ["Quarterly metrics reviewed"]


def test_resolve_memory_backend_maps_scopes_to_adapters(tmp_path: Path) -> None:
    config = _contract_config(tmp_path, "file")
    config.agents["beta"].memory_backend = "mem0"
    runtime_paths = runtime_paths_for(config)

    assert isinstance(resolve_memory_backend("alpha", config, runtime_paths), FileMemoryBackend)
    assert isinstance(resolve_memory_backend("beta", config, runtime_paths), Mem0MemoryBackend)
    assert isinstance(resolve_memory_backend(["alpha", "alpha"], config, runtime_paths), FileMemoryBackend)
    assert isinstance(resolve_memory_backend(["alpha", "beta"], config, runtime_paths), Mem0MemoryBackend)

    config.agents["alpha"].memory_backend = "none"
    assert resolve_memory_backend("alpha", config, runtime_paths) is None
    assert resolve_memory_backend(["alpha", "alpha"], config, runtime_paths) is None
