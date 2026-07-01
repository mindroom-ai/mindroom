"""Tests for the file-backed memory implementation and file-specific facade paths."""
# ruff: noqa: D103, ANN201

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import mindroom.memory._semantic_file_search as semantic_file_search
import mindroom.memory.functions as memory_functions
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.memory import MemoryPromptParts
from mindroom.memory import add_agent_memory as public_add_agent_memory
from mindroom.memory import build_memory_enhanced_prompt as public_build_memory_enhanced_prompt
from mindroom.memory import build_memory_prompt_parts as public_build_memory_prompt_parts
from mindroom.memory import delete_agent_memory as public_delete_agent_memory
from mindroom.memory import get_agent_memory as public_get_agent_memory
from mindroom.memory import list_all_agent_memories as public_list_all_agent_memories
from mindroom.memory import search_agent_memories as public_search_agent_memories
from mindroom.memory import store_conversation_memory as public_store_conversation_memory
from mindroom.memory import update_agent_memory as public_update_agent_memory
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.timing import timing_scope
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    _private_instance_state_root_path,
    agent_state_root_path,
    agent_workspace_root_path,
    get_tool_execution_identity,
    resolve_worker_key,
    tool_execution_identity,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for
from tests.memory_test_support import MockTeamConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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
        execution_identity=get_tool_execution_identity(),
    )


def append_agent_daily_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    preserve_resolved_storage_path: bool = False,
):
    return memory_functions.append_agent_daily_memory(
        content,
        agent_name,
        storage_path,
        config,
        runtime_paths_for(config),
        get_tool_execution_identity(),
        preserve_resolved_storage_path=preserve_resolved_storage_path,
    )


async def search_agent_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    limit: int = 3,
):
    return await public_search_agent_memories(
        query,
        agent_name,
        storage_path,
        config,
        runtime_paths_for(config),
        limit,
        get_tool_execution_identity(),
    )


async def list_all_agent_memories(
    agent_name: str,
    storage_path: Path,
    config: Config,
    limit: int = 100,
    *,
    preserve_resolved_storage_path: bool = False,
):
    return await public_list_all_agent_memories(
        agent_name,
        storage_path,
        config,
        runtime_paths_for(config),
        limit,
        get_tool_execution_identity(),
        preserve_resolved_storage_path=preserve_resolved_storage_path,
    )


async def get_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
):
    return await public_get_agent_memory(
        memory_id,
        caller_context,
        storage_path,
        config,
        runtime_paths_for(config),
        get_tool_execution_identity(),
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
        get_tool_execution_identity(),
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
        get_tool_execution_identity(),
    )


async def _build_memory_enhanced_prompt(
    prompt: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
) -> str:
    return await public_build_memory_enhanced_prompt(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths_for(config),
        get_tool_execution_identity(),
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
        get_tool_execution_identity(),
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
        execution_identity=get_tool_execution_identity(),
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
                "general": AgentConfig(display_name="General"),
                "calculator": AgentConfig(display_name="Calculator"),
                "helper": AgentConfig(display_name="Helper"),
                "test_agent": AgentConfig(display_name="Test Agent"),
                "a_b": AgentConfig(display_name="A B"),
                "c": AgentConfig(display_name="C"),
                "a": AgentConfig(display_name="A"),
                "b_c": AgentConfig(display_name="B C"),
            },
        ),
        runtime_paths,
    )


@pytest.fixture
def storage_path(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def config(storage_path: Path) -> Config:
    return _test_config(storage_path)


@pytest.mark.asyncio
async def test_semantic_memory_search_uses_ready_published_index_without_refresh(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = storage_path / "memory-root"
    memory_file = root / "memory" / "2026-06-02.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("Published semantic memory.\n", encoding="utf-8")
    runtime_paths = runtime_paths_for(config)

    class FakeKnowledge:
        def search(self, *, query: str, max_results: int) -> list[object]:
            assert query == "semantic memory"
            assert max_results == 5
            return [
                SimpleNamespace(
                    content="Published semantic memory.",
                    meta_data={"source_path": "memory/2026-06-02.md"},
                    reranking_score=0.8,
                ),
            ]

    access_base_ids: list[str] = []

    def resolve_access(
        base_id: str,
        access_config: Config,
        access_runtime_paths: object,
        *,
        execution_identity: object = None,
    ) -> object:
        del execution_identity
        access_base_ids.append(base_id)
        assert access_runtime_paths == runtime_paths
        base_config = access_config.knowledge_bases[base_id]
        assert base_config.path == str(root.resolve())
        assert base_config.include_patterns == ["memory/**/*.md"]
        assert base_config.include_extensions == [".md"]
        return SimpleNamespace(knowledge=FakeKnowledge(), availability=KnowledgeAvailability.READY)

    scheduled_base_ids: list[str] = []

    class FakeScheduler:
        def schedule_refresh(self, base_id: str, **_kwargs: object) -> None:
            scheduled_base_ids.append(base_id)

    def list_files(*_args: object, **_kwargs: object) -> list[Path]:
        return [memory_file.resolve()]

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", list_files)
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access", resolve_access, raising=False)
    monkeypatch.setattr(semantic_file_search, "_memory_refresh_scheduler", FakeScheduler(), raising=False)

    results = await semantic_file_search.search_semantic_file_memories(
        "semantic memory",
        scope_user_id="agent_general",
        root=root,
        config=config,
        runtime_paths=runtime_paths,
        search_config=config.memory.search,
        limit=5,
    )

    assert results[0]["memory"] == "Published semantic memory."
    assert access_base_ids
    assert scheduled_base_ids == []


class _FakeSemanticTimingKnowledge:
    def search(self, *, query: str, max_results: int) -> list[object]:
        assert query == "semantic memory"
        assert max_results == 5
        return [
            SimpleNamespace(
                content="Published semantic memory.",
                meta_data={"source_path": "memory/2026-06-02.md"},
                reranking_score=0.8,
            ),
        ]


@pytest.mark.asyncio
async def test_semantic_memory_search_emits_nested_query_timings(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = storage_path / "memory-root"
    memory_file = root / "memory" / "2026-06-02.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("Published semantic memory.\n", encoding="utf-8")
    runtime_paths = runtime_paths_for(config)
    fake_knowledge = _FakeSemanticTimingKnowledge()

    def resolve_access(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(knowledge=fake_knowledge, availability=KnowledgeAvailability.READY)

    emitted: list[tuple[str, str | None]] = []

    def emit_timing(label: str, _start: float, **_event_data: object) -> None:
        emitted.append((label, timing_scope.get()))

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", lambda *_args, **_kwargs: [memory_file.resolve()])
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access", resolve_access)
    monkeypatch.setattr(semantic_file_search, "emit_elapsed_timing", emit_timing)

    token = timing_scope.set("scope-123")
    try:
        results = await semantic_file_search.search_semantic_file_memories(
            "semantic memory",
            scope_user_id="agent_general",
            root=root,
            config=config,
            runtime_paths=runtime_paths,
            search_config=config.memory.search,
            limit=5,
        )
    finally:
        timing_scope.reset(token)

    assert [result["memory"] for result in results] == ["Published semantic memory."]
    assert ("system_prompt_assembly.memory_search.semantic.published_index.resolve", "scope-123") in emitted
    assert ("system_prompt_assembly.memory_search.semantic.published_index.schedule_refresh", "scope-123") in emitted
    assert ("system_prompt_assembly.memory_search.semantic.knowledge_search", "scope-123") in emitted
    assert ("system_prompt_assembly.memory_search.semantic.vector_query", "scope-123") in emitted
    assert ("system_prompt_assembly.memory_search.semantic.result_conversion", "scope-123") in emitted


@pytest.mark.asyncio
async def test_semantic_memory_missing_knowledge_index_schedules_refresh_and_raises_fallback(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = storage_path / "memory-root"
    memory_file = root / "memory" / "2026-06-02.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("Serialized semantic memory.\n", encoding="utf-8")

    scheduled_base_ids: list[str] = []

    class FakeScheduler:
        def schedule_refresh(self, base_id: str, **_kwargs: object) -> None:
            scheduled_base_ids.append(base_id)

    def list_files(*_args: object, **_kwargs: object) -> list[Path]:
        return [memory_file.resolve()]

    def resolve_access(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(knowledge=None, availability=KnowledgeAvailability.INITIALIZING)

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", list_files)
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access", resolve_access)
    monkeypatch.setattr(semantic_file_search, "_memory_refresh_scheduler", FakeScheduler())

    with pytest.raises(semantic_file_search.SemanticFileMemoryIndexUnavailableError):
        await semantic_file_search.search_semantic_file_memories(
            "semantic memory",
            scope_user_id="agent_general",
            root=root,
            config=config,
            runtime_paths=runtime_paths_for(config),
            search_config=config.memory.search,
            limit=5,
        )

    assert scheduled_base_ids


@pytest.mark.asyncio
async def test_file_backend_add_and_list_memories(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    await add_agent_memory("User prefers concise responses", "general", storage_path, config)

    results = await list_all_agent_memories("general", storage_path, config)
    assert len(results) == 1
    assert results[0]["memory"] == "User prefers concise responses"
    assert results[0]["id"].startswith("m_")

    memory_file = agent_workspace_root_path(storage_path, "general") / "MEMORY.md"
    assert memory_file.exists()
    assert "User prefers concise responses" in memory_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_file_backend_lists_unstructured_daily_memory_lines(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"

    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text(
        "# Daily notes\n\nBas prefers repo-grounded answers.\n- [id=m_existing] Structured daily note.\n",
        encoding="utf-8",
    )

    results = await list_all_agent_memories("general", storage_path, config)

    assert [result["memory"] for result in results] == [
        "Structured daily note.",
        "Bas prefers repo-grounded answers.",
    ]
    assert [result["id"] for result in results] == [
        "m_existing",
        "file:memory/2026-06-13.md:3",
    ]


@pytest.mark.asyncio
async def test_file_backend_deduplicates_unstructured_lines_with_internal_whitespace(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"

    await add_agent_memory("Bas prefers repo-grounded answers.", "general", storage_path, config)

    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text(
        "Bas   prefers\trepo-grounded   answers.\nUnique raw note.\n",
        encoding="utf-8",
    )

    results = await list_all_agent_memories("general", storage_path, config)

    assert [result["memory"] for result in results] == [
        "Bas prefers repo-grounded answers.",
        "Unique raw note.",
    ]


@pytest.mark.asyncio
async def test_file_backend_list_all_skips_unstructured_entrypoint_lines(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"

    workspace = agent_workspace_root_path(storage_path, "general")
    memory_file = workspace / "MEMORY.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text(
        "# Memory\n\nCurated raw entrypoint line.\n- [id=m_existing] Structured entrypoint note.\n",
        encoding="utf-8",
    )
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text("Daily raw note.\n", encoding="utf-8")

    results = await list_all_agent_memories("general", storage_path, config)

    assert [result["memory"] for result in results] == [
        "Structured entrypoint note.",
        "Daily raw note.",
    ]


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
        alice_prompt = await _build_memory_enhanced_prompt("What do you remember?", "general", storage_path, config)

    with tool_execution_identity(bob_identity):
        bob_results = await search_agent_memories("Alice-authored shared", "general", storage_path, config, limit=5)
        bob_prompt = await _build_memory_enhanced_prompt("What do you remember?", "general", storage_path, config)

    assert any(result.get("memory") == "Alice-authored shared agent memory" for result in alice_results)
    assert any(result.get("memory") == "Alice-authored shared agent memory" for result in bob_results)
    assert "Alice-authored shared agent memory" in alice_prompt
    assert "Alice-authored shared agent memory" in bob_prompt

    memory_file = agent_workspace_root_path(storage_path, "general") / "MEMORY.md"
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
        prompt = await _build_memory_enhanced_prompt("daily note", "general", storage_path, config)

    assert "Worker daily note" in prompt


@pytest.mark.asyncio
async def test_file_backend_worker_scope_uses_canonical_agent_workspace(
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
    assert (agent_workspace_root_path(storage_path, "general") / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_file_backend_worker_scope_workspace_file_memory_uses_workspace_root(
    storage_path: Path,
    config: Config,
    build_private_template_dir: Callable[..., Path],
) -> None:
    template_dir = build_private_template_dir(
        files={
            "SOUL.md": "Template soul.\n",
            "MEMORY.md": "# Memory\n",
            "memory/notes.md": "Private note.\n",
        },
    )
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
        context_files=["SOUL.md"],
    )

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
        await add_agent_memory("Alice workspace memory", "general", storage_path, config)
        alice_results = await search_agent_memories("Alice workspace", "general", storage_path, config, limit=5)

    with tool_execution_identity(bob_identity):
        bob_results = await search_agent_memories("Alice workspace", "general", storage_path, config, limit=5)

    assert any(result.get("memory") == "Alice workspace memory" for result in alice_results)
    assert not any(result.get("memory") == "Alice workspace memory" for result in bob_results)

    alice_worker_key = resolve_worker_key("user", alice_identity)
    assert alice_worker_key is not None
    alice_memory_file = (
        _private_instance_state_root_path(
            storage_path,
            worker_key=alice_worker_key,
            agent_name="general",
        )
        / "mind_data"
        / "MEMORY.md"
    )
    assert alice_memory_file.exists()
    assert "Alice workspace memory" in alice_memory_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_file_backend_semantic_search_reads_daily_memory_root(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.search.mode = "semantic"
    config.agents["general"].memory_backend = "file"

    workspace = agent_workspace_root_path(storage_path, "general")
    (workspace / "memory").mkdir(parents=True)
    (workspace / "memory" / "2026-06-02.md").write_text("Bas prefers small precise plans.\n", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("Entrypoint should not be indexed by default.\n", encoding="utf-8")

    with patch("mindroom.memory._file_backend.search_semantic_file_memories") as semantic_search:
        semantic_search.return_value = [
            {
                "id": "semantic:memory/2026-06-02.md:0",
                "memory": "Bas prefers small precise plans.",
                "user_id": "agent_general",
                "score": 1.0,
                "metadata": {"source_file": "memory/2026-06-02.md", "semantic": True, "search_mode": "semantic"},
            },
        ]

        results = await search_agent_memories("precise planning", "general", storage_path, config, limit=5)

    assert results[0]["memory"] == "Bas prefers small precise plans."
    semantic_search.assert_called_once()
    assert semantic_search.call_args.kwargs["limit"] == 5
    assert semantic_search.call_args.kwargs["root"] == workspace


@pytest.mark.asyncio
async def test_file_backend_semantic_search_falls_back_to_keyword_on_index_error(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.memory.search.mode = "semantic"
    config.agents["general"].memory_backend = "file"

    await add_agent_memory("Keyword fallback memory", "general", storage_path, config)

    with patch(
        "mindroom.memory._file_backend.search_semantic_file_memories",
        side_effect=RuntimeError("embedder offline"),
    ):
        results = await search_agent_memories("Keyword fallback", "general", storage_path, config, limit=5)

    assert any(result.get("memory") == "Keyword fallback memory" for result in results)
    assert all((result.get("metadata") or {}).get("search_mode") == "keyword" for result in results)


@pytest.mark.asyncio
async def test_file_backend_add_schedules_semantic_refresh_when_semantic_search_enabled(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.memory.backend = "file"
    config.memory.search.mode = "semantic"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    scheduled: list[tuple[str, Config, object, object]] = []

    class FakeScheduler:
        def schedule_refresh(self, base_id: str, **kwargs: object) -> None:
            scheduled.append((base_id, kwargs["config"], kwargs["runtime_paths"], kwargs["execution_identity"]))

    monkeypatch.setattr(semantic_file_search, "_memory_refresh_scheduler", FakeScheduler())

    with tool_execution_identity(identity):
        await add_agent_memory("Semantic refresh memory", "general", storage_path, config)

    assert len(scheduled) == 1
    base_id, scheduled_config, scheduled_runtime_paths, scheduled_identity = scheduled[0]
    assert scheduled_runtime_paths == runtime_paths_for(config)
    assert scheduled_identity == identity
    scheduled_path = scheduled_config.knowledge_bases[base_id].path
    assert "private_instances" in scheduled_path
    assert scheduled_path.endswith("mind_data")


@pytest.mark.asyncio
async def test_file_backend_update_and_delete_schedule_semantic_refresh_when_semantic_search_enabled(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.memory.backend = "file"
    config.memory.search.mode = "semantic"
    config.agents["general"].memory_backend = "file"

    scheduled: list[str] = []

    class FakeScheduler:
        def schedule_refresh(self, base_id: str, **_kwargs: object) -> None:
            scheduled.append(base_id)

    monkeypatch.setattr(semantic_file_search, "_memory_refresh_scheduler", FakeScheduler())

    await add_agent_memory("Original semantic memory", "general", storage_path, config)
    memory_id = (await list_all_agent_memories("general", storage_path, config))[0]["id"]

    scheduled.clear()
    await update_agent_memory(memory_id, "Updated semantic memory", "general", storage_path, config)
    assert len(scheduled) == 1

    scheduled.clear()
    await delete_agent_memory(memory_id, "general", storage_path, config)
    assert len(scheduled) == 1


@pytest.mark.asyncio
async def test_file_backend_store_conversation_memory_schedules_semantic_refresh_when_semantic_search_enabled(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.memory.backend = "file"
    config.memory.search.mode = "semantic"
    config.agents["general"].memory_backend = "file"

    scheduled: list[tuple[str, Config]] = []

    class FakeScheduler:
        def schedule_refresh(self, base_id: str, **kwargs: object) -> None:
            scheduled.append((base_id, kwargs["config"]))

    monkeypatch.setattr(semantic_file_search, "_memory_refresh_scheduler", FakeScheduler())

    await store_conversation_memory(
        "Conversation semantic memory",
        "general",
        storage_path,
        "session-general",
        config,
    )

    workspace = agent_workspace_root_path(storage_path, "general")
    assert len(scheduled) == 1
    base_id, scheduled_config = scheduled[0]
    assert scheduled_config.knowledge_bases[base_id].path == str(workspace.resolve())


@pytest.mark.asyncio
async def test_file_backend_add_skips_semantic_refresh_when_search_mode_is_keyword(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.memory.backend = "file"
    config.memory.search.mode = "keyword"
    config.agents["general"].memory_backend = "file"

    scheduled: list[str] = []

    class FakeScheduler:
        def schedule_refresh(self, base_id: str, **_kwargs: object) -> None:
            scheduled.append(base_id)

    monkeypatch.setattr(semantic_file_search, "_memory_refresh_scheduler", FakeScheduler())

    await add_agent_memory("Keyword mode memory", "general", storage_path, config)

    assert scheduled == []


@pytest.mark.asyncio
async def test_file_backend_private_semantic_search_uses_requester_root(
    storage_path: Path,
    config: Config,
    build_private_template_dir: Callable[..., Path],
) -> None:
    template_dir = build_private_template_dir(
        files={"MEMORY.md": "# Memory\n", "memory/notes.md": "Alice private semantic note.\n"},
    )
    config.memory.backend = "file"
    config.memory.search.mode = "semantic"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    with (
        tool_execution_identity(identity),
        patch(
            "mindroom.memory._file_backend.search_semantic_file_memories",
        ) as semantic_search,
    ):
        semantic_search.return_value = [
            {
                "id": "semantic:memory/notes.md:0",
                "memory": "Alice private semantic note.",
                "user_id": "agent_general",
                "score": 1.0,
                "metadata": {"source_file": "memory/notes.md", "semantic": True, "search_mode": "semantic"},
            },
        ]
        results = await search_agent_memories("private semantic", "general", storage_path, config, limit=5)

    assert results[0]["memory"] == "Alice private semantic note."
    root = semantic_search.call_args.kwargs["root"]
    assert "private_instances" in str(root)
    assert str(root).endswith("mind_data")
    assert semantic_search.call_args.kwargs["execution_identity"] == identity


@pytest.mark.asyncio
async def test_private_template_file_memory_is_visible_on_first_prompt(
    storage_path: Path,
    config: Config,
    build_private_template_dir: Callable[..., Path],
) -> None:
    template_dir = build_private_template_dir(
        files={
            "MEMORY.md": "# Memory\nFirst-turn memory.\n",
            "memory/notes.md": "Private note.\n",
        },
    )
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
    )

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    with tool_execution_identity(identity):
        prompt = await _build_memory_enhanced_prompt(
            "What do you remember?",
            "general",
            storage_path,
            config,
        )
        note_results = await search_agent_memories("Private note", "general", storage_path, config, limit=5)

    worker_key = resolve_worker_key("user", identity)
    assert worker_key is not None
    memory_file = (
        _private_instance_state_root_path(
            storage_path,
            worker_key=worker_key,
            agent_name="general",
        )
        / "mind_data"
        / "MEMORY.md"
    )
    assert memory_file.exists()
    assert "First-turn memory." in prompt
    assert any(result.get("memory") == "Private note." for result in note_results)


@pytest.mark.asyncio
async def test_private_file_memory_only_reads_memory_files(
    storage_path: Path,
    config: Config,
    build_private_template_dir: Callable[..., Path],
) -> None:
    template_dir = build_private_template_dir(
        files={
            "SOUL.md": "Template soul secret.\n",
            "MEMORY.md": "# Memory\n",
            "docs/runbook.md": "Runbook secret.\n",
            "memory/notes.md": "Private note.\n",
        },
    )
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
        context_files=["SOUL.md"],
    )

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    with tool_execution_identity(identity):
        resolve_agent_runtime("general", config, runtime_paths_for(config), execution_identity=identity, create=True)
        soul_results = await search_agent_memories("Template soul", "general", storage_path, config, limit=5)
        runbook_results = await search_agent_memories("Runbook secret", "general", storage_path, config, limit=5)
        note_results = await search_agent_memories("Private note", "general", storage_path, config, limit=5)
        prompt = await _build_memory_enhanced_prompt(
            "What should I remember about the runbook and private note?",
            "general",
            storage_path,
            config,
        )

    assert not any(result.get("memory") == "Template soul secret." for result in soul_results)
    assert not any(result.get("memory") == "Runbook secret." for result in runbook_results)
    assert any(result.get("memory") == "Private note." for result in note_results)
    assert "Private note." in prompt
    assert "Template soul secret." not in prompt
    assert "Runbook secret." not in prompt


@pytest.mark.asyncio
async def test_private_file_memory_crud_uses_canonical_private_instance_root(
    storage_path: Path,
    config: Config,
    build_private_template_dir: Callable[..., Path],
) -> None:
    template_dir = build_private_template_dir(
        files={
            "MEMORY.md": "# Memory\n",
            "memory/notes.md": "Private note.\n",
        },
    )
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
    )

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    with tool_execution_identity(identity):
        await add_agent_memory("Private CRUD memory", "general", storage_path, config)
        memory_id = (await list_all_agent_memories("general", storage_path, config))[0]["id"]

        loaded = await get_agent_memory(memory_id, "general", storage_path, config)
        assert loaded is not None
        assert loaded["memory"] == "Private CRUD memory"

        await update_agent_memory(memory_id, "Updated private CRUD memory", "general", storage_path, config)
        updated = await get_agent_memory(memory_id, "general", storage_path, config)
        assert updated is not None
        assert updated["memory"] == "Updated private CRUD memory"

        await delete_agent_memory(memory_id, "general", storage_path, config)
        assert await get_agent_memory(memory_id, "general", storage_path, config) is None

    worker_key = resolve_worker_key("user", identity)
    assert worker_key is not None
    memory_file = (
        _private_instance_state_root_path(
            storage_path,
            worker_key=worker_key,
            agent_name="general",
        )
        / "mind_data"
        / "MEMORY.md"
    )
    assert memory_file.exists()
    assert "Updated private CRUD memory" not in memory_file.read_text(encoding="utf-8")


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
async def test_file_backend_mixed_private_team_conversation_memory_is_rejected(
    storage_path: Path,
    config: Config,
) -> None:
    """File-backed team memory should reject private team members outright."""
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["calculator"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    with pytest.raises(
        ValueError,
        match="private agents are only supported in explicit Matrix ad hoc teams with requester identity",
    ):
        await store_conversation_memory(
            "Alice-authored private team memory",
            ["general", "calculator"],
            storage_path,
            "session-alice",
            config,
        )


@pytest.mark.asyncio
async def test_file_backend_team_search_reuses_shared_team_scope(
    storage_path: Path,
    config: Config,
) -> None:
    """Team memory should stay visible through the shared team scope."""
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["calculator"].memory_backend = "file"
    config.teams = {"shared_team": MockTeamConfig(agents=["general", "calculator"])}

    await store_conversation_memory(
        "Team note remains shared",
        ["general", "calculator"],
        storage_path,
        "session-team",
        config,
    )

    general_results = await search_agent_memories("Team note remains shared", "general", storage_path, config, limit=5)
    calculator_results = await search_agent_memories(
        "Team note remains shared",
        "calculator",
        storage_path,
        config,
        limit=5,
    )

    assert any(result.get("memory") == "Team note remains shared" for result in general_results)
    assert any(result.get("memory") == "Team note remains shared" for result in calculator_results)
    assert (
        agent_state_root_path(storage_path, "general") / "memory_files" / "team_calculator+general" / "MEMORY.md"
    ).exists()


@pytest.mark.asyncio
async def test_file_backend_mixed_private_team_member_crud_is_rejected(
    storage_path: Path,
    config: Config,
) -> None:
    """File-backed team member CRUD should reject private team members."""
    workspace = agent_workspace_root_path(storage_path, "calculator")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nCalculator note.\n", encoding="utf-8")

    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["calculator"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.memory.team_reads_member_memory = True
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    await add_agent_memory("Calculator workspace note", "calculator", storage_path, config)
    memory_id = (await list_all_agent_memories("calculator", storage_path, config))[0]["id"]

    with pytest.raises(
        ValueError,
        match="private agents are only supported in explicit Matrix ad hoc teams with requester identity",
    ):
        await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config)

    with pytest.raises(
        ValueError,
        match="private agents are only supported in explicit Matrix ad hoc teams with requester identity",
    ):
        await update_agent_memory(
            memory_id,
            "Updated calculator workspace note",
            ["general", "calculator"],
            storage_path,
            config,
        )

    with pytest.raises(
        ValueError,
        match="private agents are only supported in explicit Matrix ad hoc teams with requester identity",
    ):
        await delete_agent_memory(memory_id, ["general", "calculator"], storage_path, config)

    memory_content = (workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert "Calculator note." in memory_content
    assert "Calculator workspace note" in memory_content


@pytest.mark.asyncio
async def test_file_backend_prompt_includes_entrypoint(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nKey facts:\n- Project uses FastAPI.\n", encoding="utf-8")

    enhanced = await _build_memory_enhanced_prompt("How do we build the API?", "general", storage_path, config)
    assert "[File memory entrypoint (agent)]" in enhanced
    assert "Project uses FastAPI." in enhanced
    assert "How do we build the API?" in enhanced


@pytest.mark.asyncio
async def test_file_backend_build_memory_prompt_parts_splits_entrypoint_from_turn_context(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nProject uses FastAPI.\n", encoding="utf-8")
    await add_agent_memory("Deployment runbook lives in docs/deploy.md", "general", storage_path, config)

    prompt_parts = await build_memory_prompt_parts("deployment runbook", "general", storage_path, config)

    assert "[File memory entrypoint (agent)]" in prompt_parts.session_preamble
    assert "Project uses FastAPI." in prompt_parts.session_preamble
    assert "Deployment runbook lives in docs/deploy.md" in prompt_parts.turn_context
    assert "Project uses FastAPI." not in prompt_parts.turn_context


@pytest.mark.asyncio
async def test_file_backend_prompt_preserves_curated_entrypoint_lines_with_structured_memory(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")
    config.memory.file.max_entrypoint_lines = 10

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nCurated fact.\n- [id=m1] Structured fact.\n", encoding="utf-8")

    enhanced = await _build_memory_enhanced_prompt("What should I remember?", "general", storage_path, config)
    assert "Curated fact." in enhanced
    assert "- [id=m1] Structured fact." in enhanced


@pytest.mark.asyncio
async def test_file_backend_prompt_respects_max_entrypoint_lines(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")
    config.memory.file.max_entrypoint_lines = 2

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text(
        "# Memory\nCurated fact.\n- [id=m1] Structured fact.\nTrailing fact.\n",
        encoding="utf-8",
    )

    enhanced = await _build_memory_enhanced_prompt("What should I remember?", "general", storage_path, config)
    assert "# Memory\nCurated fact." in enhanced
    assert "Structured fact." not in enhanced
    assert "Trailing fact." not in enhanced


@pytest.mark.asyncio
async def test_file_backend_search_skips_structured_line_duplicates(storage_path: Path, config: Config) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    await add_agent_memory("Project owner is Bas", "general", storage_path, config)
    memories = await list_all_agent_memories("general", storage_path, config)
    memory_id = memories[0]["id"]

    daily_file = agent_workspace_root_path(storage_path, "general") / "memory" / "2026-02-28.md"
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
async def test_file_backend_can_update_and_delete_unstructured_file_memory_line(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"

    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text("Old raw note.\nKeep this line.\n", encoding="utf-8")

    memory_id = "file:memory/2026-06-13.md:1"
    await update_agent_memory(memory_id, "Updated raw note.", "general", storage_path, config)

    assert daily_file.read_text(encoding="utf-8") == "Updated raw note.\nKeep this line.\n"
    updated = await get_agent_memory(memory_id, "general", storage_path, config)
    assert updated is not None
    assert updated["memory"] == "Updated raw note."

    await delete_agent_memory(memory_id, "general", storage_path, config)
    assert daily_file.read_text(encoding="utf-8") == "\nKeep this line.\n"
    assert await get_agent_memory(memory_id, "general", storage_path, config) is None


@pytest.mark.asyncio
async def test_file_backend_whitespace_only_update_deletes_unstructured_file_memory_line(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"

    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text("  Old raw note.\nKeep this line.\n", encoding="utf-8")

    memory_id = "file:memory/2026-06-13.md:1"
    await update_agent_memory(memory_id, "   \t", "general", storage_path, config)

    assert daily_file.read_text(encoding="utf-8") == "\nKeep this line.\n"
    assert await get_agent_memory(memory_id, "general", storage_path, config) is None


@pytest.mark.asyncio
async def test_file_backend_rejects_path_ids_outside_memory_files(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"

    workspace = agent_workspace_root_path(storage_path, "general")
    docs_file = workspace / "docs" / "runbook.md"
    docs_file.parent.mkdir(parents=True, exist_ok=True)
    docs_file.write_text("Runbook instruction.\n", encoding="utf-8")
    soul_file = workspace / "SOUL.md"
    soul_file.write_text("Protected instruction.\n", encoding="utf-8")

    assert await get_agent_memory("file:docs/runbook.md:1", "general", storage_path, config) is None
    assert await get_agent_memory("file:SOUL.md:1", "general", storage_path, config) is None

    with pytest.raises(ValueError, match=r"No memory found with id=file:docs/runbook\.md:1"):
        await update_agent_memory("file:docs/runbook.md:1", "Changed.", "general", storage_path, config)
    with pytest.raises(ValueError, match=r"No memory found with id=file:SOUL\.md:1"):
        await delete_agent_memory("file:SOUL.md:1", "general", storage_path, config)

    assert docs_file.read_text(encoding="utf-8") == "Runbook instruction.\n"
    assert soul_file.read_text(encoding="utf-8") == "Protected instruction.\n"


@pytest.mark.asyncio
async def test_file_backend_rejects_unstructured_entrypoint_path_ids(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"

    workspace = agent_workspace_root_path(storage_path, "general")
    memory_file = workspace / "MEMORY.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text(
        "# Memory\n\nCurated raw entrypoint line.\n- [id=m_existing] Structured entrypoint note.\n",
        encoding="utf-8",
    )
    original_content = memory_file.read_text(encoding="utf-8")

    structured = await get_agent_memory("m_existing", "general", storage_path, config)
    assert structured is not None
    assert structured["memory"] == "Structured entrypoint note."
    assert await get_agent_memory("file:MEMORY.md:3", "general", storage_path, config) is None

    with pytest.raises(ValueError, match=r"No memory found with id=file:MEMORY\.md:3"):
        await update_agent_memory("file:MEMORY.md:3", "Changed.", "general", storage_path, config)
    with pytest.raises(ValueError, match=r"No memory found with id=file:MEMORY\.md:3"):
        await delete_agent_memory("file:MEMORY.md:3", "general", storage_path, config)

    assert memory_file.read_text(encoding="utf-8") == original_content


@pytest.mark.asyncio
async def test_file_backend_store_conversation_memory_uses_agent_scope_only(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")

    await store_conversation_memory(
        "Remember this requirement",
        "general",
        storage_path,
        "session123",
        config,
    )

    agent_results = await search_agent_memories("requirement", "general", storage_path, config, limit=5)
    assert any("Remember this requirement" in result.get("memory", "") for result in agent_results)
    assert not (storage_path / "memory-files" / "room_room_server").exists()


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
async def test_team_can_crud_member_memory_in_canonical_workspace(
    storage_path: Path,
    config: Config,
) -> None:
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nCanonical note.\n", encoding="utf-8")

    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["calculator"].memory_backend = "file"
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
    assert "Canonical note." in (workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert "Updated general private note" in (workspace / "MEMORY.md").read_text(encoding="utf-8")

    await delete_agent_memory(memory_id, ["general", "calculator"], storage_path, config)
    assert await get_agent_memory(memory_id, ["general", "calculator"], storage_path, config) is None
    assert "Updated general private note" not in (workspace / "MEMORY.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_team_can_crud_member_memory_in_worker_scoped_canonical_workspace(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["calculator"].memory_backend = "file"
    config.agents["general"].worker_scope = "user_agent"
    config.agents["calculator"].worker_scope = "user_agent"
    config.memory.team_reads_member_memory = True
    config.teams = {"gc": MockTeamConfig(agents=["general", "calculator"])}
    canonical_workspace = agent_workspace_root_path(storage_path, "general")
    canonical_workspace.mkdir(parents=True, exist_ok=True)
    (canonical_workspace / "MEMORY.md").write_text("# Memory\n\nCanonical note.\n", encoding="utf-8")

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

    canonical_memory_file = canonical_workspace / "MEMORY.md"
    canonical_content = canonical_memory_file.read_text(encoding="utf-8")
    assert "Canonical note." in canonical_content
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
async def test_worker_scoped_file_memory_uses_canonical_agent_workspace(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].worker_scope = "user"

    canonical_workspace = agent_workspace_root_path(storage_path, "general")
    canonical_workspace.mkdir(parents=True, exist_ok=True)
    (canonical_workspace / "MEMORY.md").write_text("# Memory\n\nExisting worker memory.\n", encoding="utf-8")

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
        await add_agent_memory("New worker memory", "general", storage_path, config)
        prompt = await _build_memory_enhanced_prompt("worker memory", "general", storage_path, config)

    content = (canonical_workspace / "MEMORY.md").read_text(encoding="utf-8")

    assert "Existing worker memory." in content
    assert "New worker memory" in content
    assert "Existing worker memory." in prompt
    assert not (storage_path / "memory_files" / "agent_general").exists()


@pytest.mark.asyncio
async def test_workspace_entrypoint_loaded_in_prompt(storage_path: Path, config: Config) -> None:
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nI prefer Python over JavaScript.\n", encoding="utf-8")

    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"

    enhanced = await _build_memory_enhanced_prompt("What language?", "general", storage_path, config)
    assert "I prefer Python over JavaScript." in enhanced
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8").startswith("# Memory")


@pytest.mark.asyncio
async def test_workspace_daily_files_live_in_canonical_scope(storage_path: Path, config: Config) -> None:
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True)

    config.memory.backend = "file"
    config.agents["general"].memory_backend = "file"

    result = append_agent_daily_memory("Daily note", "general", storage_path, config)
    assert result["memory"] == "Daily note"

    daily_files = list((workspace / "memory").rglob("*.md"))
    assert len(daily_files) == 1
    assert "Daily note" in daily_files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_shared_file_memory_uses_workspace_root_without_affecting_other_agents(
    storage_path: Path,
    config: Config,
) -> None:
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True)

    config.memory.backend = "file"
    config.memory.file.path = str(storage_path / "memory-files")
    config.agents["general"].memory_backend = "file"

    await add_agent_memory("Custom workspace memory", "general", storage_path, config)
    await add_agent_memory("Default scope memory", "calculator", storage_path, config)

    general_memories = await list_all_agent_memories("general", storage_path, config)
    assert any(memory["memory"] == "Custom workspace memory" for memory in general_memories)

    calc_memories = await list_all_agent_memories("calculator", storage_path, config)
    assert any(memory["memory"] == "Default scope memory" for memory in calc_memories)
    assert (workspace / "MEMORY.md").exists()
    assert (agent_workspace_root_path(storage_path, "calculator") / "MEMORY.md").exists()
