"""Tests for the file-backed memory implementation and file-specific facade paths."""
# ruff: noqa: D103, ANN201

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import tiktoken

import mindroom.memory._semantic_file_search as semantic_file_search
import mindroom.memory.functions as memory_functions
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.file_memory_knowledge import resolve_file_memory_knowledge
from mindroom.knowledge.read_protocol import DISTANCE_KEY, SearchHit
from mindroom.knowledge.utils import KnowledgeBaseAccessResolution
from mindroom.knowledge.vector_store import KnowledgeCollection, StoredRow
from mindroom.llm.provider import ModelIdentity, TokenCounter
from mindroom.memory import ConversationRef, MemoryPromptParts
from mindroom.memory import add_agent_memory as public_add_agent_memory
from mindroom.memory import build_memory_prompt_parts as public_build_memory_prompt_parts
from mindroom.memory import delete_agent_memory as public_delete_agent_memory
from mindroom.memory import get_agent_memory as public_get_agent_memory
from mindroom.memory import list_all_agent_memories as public_list_all_agent_memories
from mindroom.memory import search_agent_memories as public_search_agent_memories
from mindroom.memory import store_conversation_memory as public_store_conversation_memory
from mindroom.memory import update_agent_memory as public_update_agent_memory
from mindroom.memory._file_backend import FileMemoryBackend
from mindroom.memory._shared import MemoryNotFoundError
from mindroom.prompts import PROMPT_DEFAULTS
from mindroom.timing import timing_scope
from mindroom.token_counting import families
from mindroom.token_counting.counts import count
from mindroom.token_counting.families import counter_for_model, load_o200k
from mindroom.tool_system.runtime_context import current_execution_identity, detached_tool_scope
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_state_root_path,
    agent_workspace_root_path,
    private_instance_scope_root_path,
    resolve_worker_key,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for
from tests.fakes.knowledge import install_recording_refresh_scheduler
from tests.fakes.token_counter import fake_token_counter
from tests.knowledge_test_support import collection_meta
from tests.memory_test_support import MockTeamConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.fakes.knowledge import RecordingRefreshScheduler


async def add_agent_memory(
    content: str,
    agent_name: str,
    config: Config,
) -> None:
    await public_add_agent_memory(
        content,
        agent_name,
        config,
        runtime_paths_for(config),
        execution_identity=current_execution_identity(),
    )


def append_agent_daily_memory(
    content: str,
    agent_name: str,
    config: Config,
):
    return memory_functions.append_agent_daily_memory(
        content,
        agent_name,
        config,
        runtime_paths_for(config),
        current_execution_identity(),
    )


async def search_agent_memories(
    query: str,
    agent_name: str,
    config: Config,
    limit: int = 3,
):
    outcome = await public_search_agent_memories(
        query,
        agent_name,
        config,
        runtime_paths_for(config),
        limit,
        current_execution_identity(),
    )
    assert outcome.degraded_reason is None
    return outcome.results


async def list_all_agent_memories(
    agent_name: str,
    config: Config,
    limit: int = 100,
):
    return await public_list_all_agent_memories(
        agent_name,
        config,
        runtime_paths_for(config),
        limit,
        current_execution_identity(),
    )


async def get_agent_memory(
    memory_id: str,
    agent_name: str,
    config: Config,
):
    return await public_get_agent_memory(
        memory_id,
        agent_name,
        config,
        runtime_paths_for(config),
        current_execution_identity(),
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
        current_execution_identity(),
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
        current_execution_identity(),
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
        current_execution_identity(),
        counter=fake_token_counter(),
    )


async def store_conversation_memory(
    prompt: str,
    agent_name: str | list[str],
    session_id: str,
    config: Config,
) -> None:
    await public_store_conversation_memory(
        prompt,
        agent_name,
        session_id,
        config,
        runtime_paths_for(config),
        execution_identity=current_execution_identity(),
    )


def _team_memory_file(storage_path: Path, team_scope_id: str) -> Path:
    return storage_path / "team_memory" / team_scope_id / "MEMORY.md"


def _identity(name: str, agent_name: str = "general") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=f"@{name}:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=f"session-{name}",
    )


@pytest.fixture(autouse=True)
def refresh_scheduler(monkeypatch: pytest.MonkeyPatch) -> RecordingRefreshScheduler:
    """Record semantic refreshes instead of starting real refresh children that outlive the test.

    A child still starting when the test's event loop closes hangs the loop's teardown.
    """
    return install_recording_refresh_scheduler(monkeypatch)


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
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General"),
                "calculator": AgentConfig(display_name="Calculator"),
                "a_b": AgentConfig(display_name="A B"),
                "c": AgentConfig(display_name="C"),
                "a": AgentConfig(display_name="A"),
                "b_c": AgentConfig(display_name="B C"),
            },
        ),
        runtime_paths,
    )
    config.memory.backend = "file"
    return config


def test_semantic_memory_index_is_a_runtime_config_overlay(storage_path: Path, config: Config) -> None:
    """Synthetic file-memory indexes must not leak into authored config serialization."""
    root = storage_path / "workspace"

    resolution = resolve_file_memory_knowledge(
        scope_user_id="agent_general",
        root=root,
        config=config,
        search_config=config.memory.search,
    )
    knowledge_config = resolution.config
    base_id = resolution.base_id

    assert knowledge_config.knowledge_bases[base_id].path == str(root.resolve())
    assert knowledge_config.runtime_knowledge_base_overlay(base_id) is knowledge_config.knowledge_bases[base_id]
    assert base_id not in knowledge_config.authored_model_dump().get("knowledge_bases", {})
    assert base_id not in config.knowledge_bases
    assert config.runtime_knowledge_base_overlay(base_id) is None
    assert knowledge_config.memory is config.memory


@pytest.mark.parametrize(
    "base_id",
    ["", " ", ".", "..", "group/research", "group\\research", "foo\nbar", "foo\rbar", "__agent_private__:mind"],
)
def test_runtime_knowledge_base_overlay_rejects_unsafe_ids(
    storage_path: Path,
    config: Config,
    base_id: str,
) -> None:
    """Runtime overlays must enforce the same ID constraints as authored bases."""
    runtime_base = KnowledgeBaseConfig(path=str(storage_path / "workspace"))

    with pytest.raises(ValueError, match="knowledge_bases keys"):
        config.with_runtime_knowledge_base_overlay(base_id, runtime_base)


@pytest.mark.asyncio
async def test_semantic_memory_search_uses_ready_published_index_without_refresh(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    refresh_scheduler: RecordingRefreshScheduler,
) -> None:
    root = storage_path / "memory-root"
    memory_file = root / "memory" / "2026-06-02.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("Published semantic memory.\n", encoding="utf-8")
    runtime_paths = runtime_paths_for(config)

    class FakeKnowledge:
        async def asearch(self, query: str, limit: int) -> list[SearchHit]:
            assert query == "semantic memory"
            assert limit == 5
            return [
                SearchHit(
                    content="Published semantic memory.",
                    meta_data={"source_path": "memory/2026-06-02.md", DISTANCE_KEY: 0.2},
                ),
            ]

    access_base_ids: list[str] = []

    async def resolve_access(
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

    def list_files(*_args: object, **_kwargs: object) -> list[Path]:
        return [memory_file.resolve()]

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", list_files)
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access_async", resolve_access)

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
    assert refresh_scheduler.scheduled == []


@pytest.mark.asyncio
async def test_semantic_memory_scores_each_hit_by_its_similarity(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A semantic hit scores its similarity, one minus its cosine distance."""
    root = storage_path / "memory-root"
    memory_file = root / "memory" / "notes.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("Close note.\nFar note.\n", encoding="utf-8")

    class FakeKnowledge:
        async def asearch(self, _query: str, _limit: int) -> list[SearchHit]:
            return [
                SearchHit(content="Close note.", meta_data={"source_path": "memory/notes.md", DISTANCE_KEY: 0.25}),
                SearchHit(content="Far note.", meta_data={"source_path": "memory/notes.md", DISTANCE_KEY: 0.75}),
            ]

    async def resolve_access(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(knowledge=FakeKnowledge(), availability=KnowledgeAvailability.READY)

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", lambda *_args: [memory_file.resolve()])
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access_async", resolve_access)

    results = await semantic_file_search.search_semantic_file_memories(
        "note",
        scope_user_id="agent_general",
        root=root,
        config=config,
        runtime_paths=runtime_paths_for(config),
        search_config=config.memory.search,
        limit=5,
    )

    assert [(result["memory"], result["score"]) for result in results] == [("Close note.", 0.75), ("Far note.", 0.25)]


@pytest.mark.asyncio
async def test_semantic_memory_scores_the_hits_the_owned_store_returns(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hits from a real collection search score as one minus their cosine distance, nearest first."""
    root = storage_path / "memory-root"
    memory_file = root / "memory" / "notes.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("Close note.\nFar note.\n", encoding="utf-8")
    collection = KnowledgeCollection(path=storage_path, name="mindroom_knowledge_memory_hits")
    collection.create(collection_meta())
    collection.write_file(
        "memory/notes.md",
        (1, 1, "notes"),
        [
            StoredRow(
                id=row_id,
                document=document,
                metadata={"source_path": "memory/notes.md", "name": "notes"},
                embedding=embedding,
            )
            for row_id, document, embedding in (("far", "Far note.", [0.0, 1.0]), ("close", "Close note.", [1.0, 0.0]))
        ],
    )
    hits = collection.search([1.0, 0.0], limit=2)

    class RealHitsKnowledge:
        async def asearch(self, _query: str, _limit: int) -> list[SearchHit]:
            return hits

    async def resolve_access(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(knowledge=RealHitsKnowledge(), availability=KnowledgeAvailability.READY)

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", lambda *_args: [memory_file.resolve()])
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access_async", resolve_access)

    results = await semantic_file_search.search_semantic_file_memories(
        "note",
        scope_user_id="agent_general",
        root=root,
        config=config,
        runtime_paths=runtime_paths_for(config),
        search_config=config.memory.search,
        limit=5,
    )

    assert [result["memory"] for result in results] == ["Close note.", "Far note."]
    assert [result["score"] for result in results] == [pytest.approx(1.0, abs=1e-6), pytest.approx(0.0, abs=1e-6)]


class _FakeSemanticTimingKnowledge:
    async def asearch(self, query: str, limit: int) -> list[SearchHit]:
        assert query == "semantic memory"
        assert limit == 5
        return [
            SearchHit(
                content="Published semantic memory.",
                meta_data={"source_path": "memory/2026-06-02.md", DISTANCE_KEY: 0.2},
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

    async def resolve_access(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(knowledge=fake_knowledge, availability=KnowledgeAvailability.READY)

    emitted: list[tuple[str, str | None]] = []

    def emit_timing(label: str, _start: float, **_event_data: object) -> None:
        emitted.append((label, timing_scope.get()))

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", lambda *_args, **_kwargs: [memory_file.resolve()])
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access_async", resolve_access)
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
    refresh_scheduler: RecordingRefreshScheduler,
) -> None:
    root = storage_path / "memory-root"
    memory_file = root / "memory" / "2026-06-02.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("Serialized semantic memory.\n", encoding="utf-8")

    def list_files(*_args: object, **_kwargs: object) -> list[Path]:
        return [memory_file.resolve()]

    async def resolve_access(*_args: object, **_kwargs: object) -> KnowledgeBaseAccessResolution:
        return KnowledgeBaseAccessResolution(knowledge=None, availability=KnowledgeAvailability.INITIALIZING)

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", list_files)
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access_async", resolve_access)

    with pytest.raises(semantic_file_search.SemanticFileMemoryIndexUnavailableError) as excinfo:
        await semantic_file_search.search_semantic_file_memories(
            "semantic memory",
            scope_user_id="agent_general",
            root=root,
            config=config,
            runtime_paths=runtime_paths_for(config),
            search_config=config.memory.search,
            limit=5,
        )

    assert refresh_scheduler.scheduled
    # Plain warm-up carries no degradation cause.
    assert excinfo.value.degraded_reason is None


@pytest.mark.asyncio
async def test_semantic_memory_cold_failed_index_carries_classified_cause(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold index kept unpublished by an embedder failure surfaces the classified cause."""
    root = storage_path / "memory-root"
    memory_file = root / "memory" / "2026-06-02.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("Serialized semantic memory.\n", encoding="utf-8")

    def list_files(*_args: object, **_kwargs: object) -> list[Path]:
        return [memory_file.resolve()]

    async def resolve_access(*_args: object, **_kwargs: object) -> KnowledgeBaseAccessResolution:
        return KnowledgeBaseAccessResolution(
            knowledge=None,
            availability=KnowledgeAvailability.REFRESH_FAILED,
            last_error=(
                "Indexed 0 of 1 managed knowledge files (first error: embedder authentication failed (HTTP 401))"
            ),
        )

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", list_files)
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access_async", resolve_access)

    with pytest.raises(semantic_file_search.SemanticFileMemoryIndexUnavailableError) as excinfo:
        await semantic_file_search.search_semantic_file_memories(
            "semantic memory",
            scope_user_id="agent_general",
            root=root,
            config=config,
            runtime_paths=runtime_paths_for(config),
            search_config=config.memory.search,
            limit=5,
        )

    assert excinfo.value.degraded_reason == "embedder authentication failed (HTTP 401)"


@pytest.mark.asyncio
async def test_file_backend_add_and_list_memories(storage_path: Path, config: Config) -> None:

    await add_agent_memory("User prefers concise responses", "general", config)

    results = await list_all_agent_memories("general", config)
    assert len(results) == 1
    assert results[0]["memory"] == "User prefers concise responses"
    assert results[0]["id"].startswith("m_")

    memory_file = agent_workspace_root_path(storage_path, "general") / "MEMORY.md"
    assert memory_file.exists()
    assert "User prefers concise responses" in memory_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_file_backend_lists_unstructured_daily_memory_lines(storage_path: Path, config: Config) -> None:

    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text(
        "# Daily notes\n\nBas prefers repo-grounded answers.\n- [id=m_existing] Structured daily note.\n",
        encoding="utf-8",
    )

    results = await list_all_agent_memories("general", config)

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

    await add_agent_memory("Bas prefers repo-grounded answers.", "general", config)

    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text(
        "Bas   prefers\trepo-grounded   answers.\nUnique raw note.\n",
        encoding="utf-8",
    )

    results = await list_all_agent_memories("general", config)

    assert [result["memory"] for result in results] == [
        "Bas prefers repo-grounded answers.",
        "Unique raw note.",
    ]


@pytest.mark.asyncio
async def test_file_backend_list_all_skips_unstructured_entrypoint_lines(
    storage_path: Path,
    config: Config,
) -> None:

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

    results = await list_all_agent_memories("general", config)

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
    config.agents["general"].worker_scope = "user"

    alice_identity = _identity("alice")
    bob_identity = _identity("bob")

    with detached_tool_scope(alice_identity):
        await add_agent_memory("Alice-authored shared agent memory", "general", config)
        alice_results = await search_agent_memories("Alice-authored shared", "general", config, limit=5)
        alice_parts = await build_memory_prompt_parts("What do you remember?", "general", config)

    with detached_tool_scope(bob_identity):
        bob_results = await search_agent_memories("Alice-authored shared", "general", config, limit=5)
        bob_parts = await build_memory_prompt_parts("What do you remember?", "general", config)

    assert any(result.get("memory") == "Alice-authored shared agent memory" for result in alice_results)
    assert any(result.get("memory") == "Alice-authored shared agent memory" for result in bob_results)
    assert "Alice-authored shared agent memory" in alice_parts.session_preamble
    assert "Alice-authored shared agent memory" in bob_parts.session_preamble

    memory_file = agent_workspace_root_path(storage_path, "general") / "MEMORY.md"
    assert memory_file.exists()


@pytest.mark.asyncio
async def test_user_scoped_worker_prompt_reads_daily_memory(
    config: Config,
) -> None:
    config.agents["general"].worker_scope = "user"
    config.memory.file.max_entrypoint_lines = 0

    alice_identity = _identity("alice")

    with detached_tool_scope(alice_identity):
        append_agent_daily_memory("Worker daily note", "general", config)
        prompt_parts = await build_memory_prompt_parts("daily note", "general", config)

    assert "Worker daily note" in prompt_parts.transient_turn_context


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
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
        context_files=["SOUL.md"],
    )

    alice_identity = _identity("alice")
    bob_identity = _identity("bob")

    with detached_tool_scope(alice_identity):
        await add_agent_memory("Alice workspace memory", "general", config)
        alice_results = await search_agent_memories("Alice workspace", "general", config, limit=5)

    with detached_tool_scope(bob_identity):
        bob_results = await search_agent_memories("Alice workspace", "general", config, limit=5)

    assert any(result.get("memory") == "Alice workspace memory" for result in alice_results)
    assert not any(result.get("memory") == "Alice workspace memory" for result in bob_results)

    alice_worker_key = resolve_worker_key("user", alice_identity)
    assert alice_worker_key is not None
    alice_memory_file = (
        private_instance_scope_root_path(storage_path, alice_worker_key) / "general" / "mind_data" / "MEMORY.md"
    )
    assert alice_memory_file.exists()
    assert "Alice workspace memory" in alice_memory_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_file_backend_semantic_search_reads_daily_memory_root(storage_path: Path, config: Config) -> None:
    config.memory.search.mode = "semantic"

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

        results = await search_agent_memories("precise planning", "general", config, limit=5)

    assert results[0]["memory"] == "Bas prefers small precise plans."
    semantic_search.assert_called_once()
    assert semantic_search.call_args.kwargs["limit"] == 5
    assert semantic_search.call_args.kwargs["root"] == workspace


@pytest.mark.asyncio
async def test_semantic_search_ids_are_read_only_crud_locators(config: Config) -> None:
    """A semantic result ID must not be advertised as a round-trippable CRUD ID."""
    config.memory.search.mode = "semantic"
    semantic_id = "semantic:memory/notes.md:1"

    with patch(
        "mindroom.memory._file_backend.search_semantic_file_memories",
        return_value=[
            {
                "id": semantic_id,
                "memory": "Read-only semantic match.",
                "user_id": "agent_general",
                "score": 1.0,
                "metadata": {"source_file": "memory/notes.md", "semantic": True, "search_mode": "semantic"},
            },
        ],
    ):
        results = await search_agent_memories("semantic match", "general", config, limit=5)

    assert results[0]["id"] == semantic_id
    assert await get_agent_memory(semantic_id, "general", config) is None
    with pytest.raises(MemoryNotFoundError):
        await update_agent_memory(semantic_id, "replacement", "general", config)
    with pytest.raises(MemoryNotFoundError):
        await delete_agent_memory(semantic_id, "general", config)


@pytest.mark.asyncio
async def test_keyword_search_uses_fixed_memory_corpus_not_semantic_include_patterns(
    storage_path: Path,
    config: Config,
) -> None:
    """Keyword mode searches MEMORY.md and memory/**/*.md regardless of semantic includes."""
    config.memory.search.mode = "keyword"
    config.memory.search.include = ["notes/**/*.md"]
    workspace = agent_workspace_root_path(storage_path, "general")
    notes_file = workspace / "notes" / "custom.md"
    memory_file = workspace / "memory" / "daily.md"
    notes_file.parent.mkdir(parents=True)
    memory_file.parent.mkdir(parents=True)
    notes_file.write_text("customincludeuniquemarker\n", encoding="utf-8")
    memory_file.write_text("fixedkeyworduniquemarker\n", encoding="utf-8")

    custom_results = await search_agent_memories("customincludeuniquemarker", "general", config)
    memory_results = await search_agent_memories("fixedkeyworduniquemarker", "general", config)

    assert custom_results == []
    assert [result["memory"] for result in memory_results] == ["fixedkeyworduniquemarker"]


@pytest.mark.asyncio
async def test_file_backend_semantic_search_falls_back_to_keyword_on_index_error(
    config: Config,
) -> None:
    config.memory.search.mode = "semantic"

    await add_agent_memory("Keyword fallback memory", "general", config)

    with patch(
        "mindroom.memory._file_backend.search_semantic_file_memories",
        side_effect=RuntimeError("embedder offline"),
    ):
        outcome = await public_search_agent_memories(
            "Keyword fallback",
            "general",
            config,
            runtime_paths_for(config),
            5,
            current_execution_identity(),
        )

    assert outcome.degraded_reason == "semantic memory search failed (RuntimeError)"
    assert any(result.get("memory") == "Keyword fallback memory" for result in outcome.results)
    assert all((result.get("metadata") or {}).get("search_mode") == "keyword" for result in outcome.results)


@pytest.mark.asyncio
async def test_file_backend_cold_failed_index_degrades_instead_of_healthy_fallback(
    storage_path: Path,
    config: Config,
) -> None:
    """A never-built index kept down by an auth failure is not a healthy keyword fallback."""
    config.memory.search.mode = "semantic"
    config.memory.search.include = ["notes/**/*.md"]

    await add_agent_memory("Keyword fallback memory", "general", config)
    custom_note = agent_workspace_root_path(storage_path, "general") / "notes" / "custom.md"
    custom_note.parent.mkdir(parents=True)
    custom_note.write_text("Custom semantic-only path.\n", encoding="utf-8")

    with patch(
        "mindroom.memory._file_backend.search_semantic_file_memories",
        side_effect=semantic_file_search.SemanticFileMemoryIndexUnavailableError(
            "Semantic file-memory index is not ready",
            degraded_reason="embedder authentication failed (HTTP 401)",
        ),
    ):
        outcome = await public_search_agent_memories(
            "Keyword fallback",
            "general",
            config,
            runtime_paths_for(config),
            5,
            current_execution_identity(),
        )

    assert outcome.degraded_reason == "embedder authentication failed (HTTP 401)"
    assert any(result.get("memory") == "Keyword fallback memory" for result in outcome.results)
    assert all(result.get("memory") != "Custom semantic-only path." for result in outcome.results)


@pytest.mark.asyncio
async def test_prompt_parts_carry_degradation_notice_with_keyword_matches(
    config: Config,
) -> None:
    """Automatic prompt assembly keeps both the notice and the keyword fallback context."""
    config.memory.search.mode = "semantic"

    await add_agent_memory("Keyword fallback memory", "general", config)

    with patch(
        "mindroom.memory._file_backend.search_semantic_file_memories",
        side_effect=RuntimeError("embedder offline"),
    ):
        prompt_parts = await public_build_memory_prompt_parts(
            "Keyword fallback",
            "general",
            config,
            runtime_paths_for(config),
            counter=fake_token_counter(),
        )

    assert "Semantic memory search is unavailable this turn" in prompt_parts.transient_turn_context
    assert "semantic memory search failed (RuntimeError)" in prompt_parts.transient_turn_context
    assert "Keyword fallback memory" in prompt_parts.transient_turn_context


@pytest.mark.asyncio
async def test_file_backend_add_schedules_semantic_refresh_when_semantic_search_enabled(
    config: Config,
    refresh_scheduler: RecordingRefreshScheduler,
) -> None:
    config.memory.search.mode = "semantic"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    identity = _identity("alice")

    with detached_tool_scope(identity):
        await add_agent_memory("Semantic refresh memory", "general", config)

    [scheduled] = refresh_scheduler.scheduled
    assert scheduled.runtime_paths == runtime_paths_for(config)
    assert scheduled.execution_identity == identity
    scheduled_path = scheduled.config.knowledge_bases[scheduled.base_id].path
    assert "private_instances" in scheduled_path
    assert scheduled_path.endswith("mind_data")


@pytest.mark.asyncio
async def test_file_backend_update_and_delete_schedule_semantic_refresh_when_semantic_search_enabled(
    config: Config,
    refresh_scheduler: RecordingRefreshScheduler,
) -> None:
    config.memory.search.mode = "semantic"

    await add_agent_memory("Original semantic memory", "general", config)
    memory_id = (await list_all_agent_memories("general", config))[0]["id"]

    refresh_scheduler.scheduled.clear()
    await update_agent_memory(memory_id, "Updated semantic memory", "general", config)
    assert len(refresh_scheduler.scheduled) == 1

    refresh_scheduler.scheduled.clear()
    await delete_agent_memory(memory_id, "general", config)
    assert len(refresh_scheduler.scheduled) == 1


@pytest.mark.asyncio
async def test_file_backend_store_conversation_memory_schedules_semantic_refresh_when_semantic_search_enabled(
    storage_path: Path,
    config: Config,
    refresh_scheduler: RecordingRefreshScheduler,
) -> None:
    config.memory.search.mode = "semantic"

    await store_conversation_memory(
        "Conversation semantic memory",
        "general",
        "session-general",
        config,
    )

    workspace = agent_workspace_root_path(storage_path, "general")
    [scheduled] = refresh_scheduler.scheduled
    assert scheduled.config.knowledge_bases[scheduled.base_id].path == str(workspace.resolve())


@pytest.mark.asyncio
async def test_file_backend_add_skips_semantic_refresh_when_search_mode_is_keyword(
    config: Config,
    refresh_scheduler: RecordingRefreshScheduler,
) -> None:
    config.memory.search.mode = "keyword"

    await add_agent_memory("Keyword mode memory", "general", config)

    assert refresh_scheduler.scheduled == []


@pytest.mark.asyncio
async def test_file_backend_private_semantic_search_uses_requester_root(
    config: Config,
    build_private_template_dir: Callable[..., Path],
) -> None:
    template_dir = build_private_template_dir(
        files={"MEMORY.md": "# Memory\n", "memory/notes.md": "Alice private semantic note.\n"},
    )
    config.memory.search.mode = "semantic"
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
    )
    identity = _identity("alice")

    with (
        detached_tool_scope(identity),
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
        results = await search_agent_memories("private semantic", "general", config, limit=5)

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
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
    )

    identity = _identity("alice")

    with detached_tool_scope(identity):
        prompt_parts = await build_memory_prompt_parts(
            "What do you remember?",
            "general",
            config,
        )
        note_results = await search_agent_memories("Private note", "general", config, limit=5)

    worker_key = resolve_worker_key("user", identity)
    assert worker_key is not None
    memory_file = private_instance_scope_root_path(storage_path, worker_key) / "general" / "mind_data" / "MEMORY.md"
    assert memory_file.exists()
    assert "First-turn memory." in prompt_parts.session_preamble
    assert any(result.get("memory") == "Private note." for result in note_results)


@pytest.mark.asyncio
async def test_private_file_memory_only_reads_memory_files(
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
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
        context_files=["SOUL.md"],
    )

    identity = _identity("alice")

    with detached_tool_scope(identity):
        soul_results = await search_agent_memories("Template soul", "general", config, limit=5)
        runbook_results = await search_agent_memories("Runbook secret", "general", config, limit=5)
        note_results = await search_agent_memories("Private note", "general", config, limit=5)
        prompt_parts = await build_memory_prompt_parts(
            "What should I remember about the runbook and private note?",
            "general",
            config,
        )

    assert not any(result.get("memory") == "Template soul secret." for result in soul_results)
    assert not any(result.get("memory") == "Runbook secret." for result in runbook_results)
    assert any(result.get("memory") == "Private note." for result in note_results)
    assert "Private note." in prompt_parts.transient_turn_context
    for prompt_text in (prompt_parts.session_preamble, prompt_parts.transient_turn_context):
        assert "Template soul secret." not in prompt_text
        assert "Runbook secret." not in prompt_text


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
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
    )

    identity = _identity("alice")

    with detached_tool_scope(identity):
        await add_agent_memory("Private CRUD memory", "general", config)
        memory_id = (await list_all_agent_memories("general", config))[0]["id"]

        loaded = await get_agent_memory(memory_id, "general", config)
        assert loaded is not None
        assert loaded["memory"] == "Private CRUD memory"

        await update_agent_memory(memory_id, "Updated private CRUD memory", "general", config)
        updated = await get_agent_memory(memory_id, "general", config)
        assert updated is not None
        assert updated["memory"] == "Updated private CRUD memory"

        await delete_agent_memory(memory_id, "general", config)
        assert await get_agent_memory(memory_id, "general", config) is None

    worker_key = resolve_worker_key("user", identity)
    assert worker_key is not None
    memory_file = private_instance_scope_root_path(storage_path, worker_key) / "general" / "mind_data" / "MEMORY.md"
    assert memory_file.exists()
    assert "Updated private CRUD memory" not in memory_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_file_backend_team_conversation_memory_lives_once_in_the_team_store(
    storage_path: Path,
    config: Config,
) -> None:
    """A team turn appends one entry to the team's own MEMORY.md, shared by every requester and member."""
    config.agents["general"].worker_scope = "user"
    config.agents["calculator"].worker_scope = "user"
    config.teams = {"shared_team": MockTeamConfig(agents=["general", "calculator"])}

    alice_identity = _identity("alice", agent_name="team")
    bob_identity = _identity("bob", agent_name="team")

    with detached_tool_scope(alice_identity):
        await store_conversation_memory(
            "Alice-authored shared team memory",
            ["general", "calculator"],
            "session-alice",
            config,
        )
        alice_results = await search_agent_memories("Alice-authored shared team", "general", config, limit=5)

    with detached_tool_scope(bob_identity):
        bob_results = await search_agent_memories("Alice-authored shared team", "calculator", config, limit=5)

    assert [result["memory"] for result in alice_results] == ["Alice-authored shared team memory"]
    assert [result["id"] for result in bob_results] == [result["id"] for result in alice_results]
    team_text = _team_memory_file(storage_path, "team_calculator+general").read_text(encoding="utf-8")
    assert team_text.count("Alice-authored shared team memory") == 1
    for member in ("general", "calculator"):
        assert not (agent_state_root_path(storage_path, member) / "memory_files").exists()
        assert not (agent_workspace_root_path(storage_path, member) / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_file_backend_team_conversation_memory_removes_credentials(
    storage_path: Path,
    config: Config,
) -> None:
    """A team keeps each message it answers in MEMORY.md, so credential-like values are removed first."""
    config.teams = {"shared_team": MockTeamConfig(agents=["general", "calculator"])}
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

    await store_conversation_memory(
        f"Use token {token}\npassword: hunter2\nfor the staging deploy on Friday",
        ["general", "calculator"],
        "session-alice",
        config,
    )

    text = _team_memory_file(storage_path, "team_calculator+general").read_text(encoding="utf-8")
    assert token not in text
    assert "hunter2" not in text
    assert "for the staging deploy on Friday" in text


@pytest.mark.asyncio
async def test_file_backend_mixed_private_team_conversation_memory_is_rejected(
    config: Config,
) -> None:
    """File-backed team memory should reject private team members outright."""
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    with pytest.raises(
        ValueError,
        match="private agents are only supported in explicit Matrix ad hoc teams with requester identity",
    ):
        await store_conversation_memory(
            "Alice-authored private team memory",
            ["general", "calculator"],
            "session-alice",
            config,
        )


@pytest.mark.asyncio
async def test_file_backend_team_search_reuses_shared_team_scope(
    storage_path: Path,
    config: Config,
) -> None:
    """Every member finds the team's one memory, and a read never creates the team store."""
    config.teams = {"shared_team": MockTeamConfig(agents=["general", "calculator"])}

    assert await search_agent_memories("Team note remains shared", "general", config, limit=5) == []
    assert not (storage_path / "team_memory").exists()

    await store_conversation_memory(
        "Team note remains shared",
        ["general", "calculator"],
        "session-team",
        config,
    )

    general_results = await search_agent_memories("Team note remains shared", "general", config, limit=5)
    calculator_results = await search_agent_memories("Team note remains shared", "calculator", config, limit=5)

    assert [result["memory"] for result in general_results] == ["Team note remains shared"]
    assert [result["id"] for result in calculator_results] == [result["id"] for result in general_results]
    assert general_results[0]["user_id"] == "team_calculator+general"
    assert _team_memory_file(storage_path, "team_calculator+general").exists()


@pytest.mark.asyncio
async def test_file_backend_mixed_private_team_member_crud_is_rejected(
    storage_path: Path,
    config: Config,
) -> None:
    """A member of a configured team with a private agent cannot reach memories by ID."""
    workspace = agent_workspace_root_path(storage_path, "calculator")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nCalculator note.\n", encoding="utf-8")

    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    await add_agent_memory("Calculator workspace note", "calculator", config)
    memory_id = (await list_all_agent_memories("calculator", config))[0]["id"]
    unsupported = "private agents are only supported in explicit Matrix ad hoc teams with requester identity"

    with pytest.raises(ValueError, match=unsupported):
        await get_agent_memory(memory_id, "calculator", config)
    with pytest.raises(ValueError, match=unsupported):
        await update_agent_memory(memory_id, "Updated calculator workspace note", "calculator", config)
    with pytest.raises(ValueError, match=unsupported):
        await delete_agent_memory(memory_id, "calculator", config)

    memory_content = (workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert "Calculator note." in memory_content
    assert "Calculator workspace note" in memory_content


@pytest.mark.asyncio
async def test_file_backend_build_memory_prompt_parts_splits_entrypoint_from_turn_context(
    storage_path: Path,
    config: Config,
) -> None:

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nProject uses FastAPI.\n", encoding="utf-8")
    await add_agent_memory("Deployment runbook lives in docs/deploy.md", "general", config)

    prompt_parts = await build_memory_prompt_parts("deployment runbook", "general", config)

    assert "[File memory entrypoint (agent)]" in prompt_parts.session_preamble
    assert "Project uses FastAPI." in prompt_parts.session_preamble
    assert "Deployment runbook lives in docs/deploy.md" in prompt_parts.transient_turn_context
    assert "Project uses FastAPI." not in prompt_parts.transient_turn_context


@pytest.mark.asyncio
async def test_file_backend_prompt_preserves_curated_entrypoint_lines_with_structured_memory(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.file.max_entrypoint_lines = 10

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nCurated fact.\n- [id=m1] Structured fact.\n", encoding="utf-8")

    prompt_parts = await build_memory_prompt_parts("What should I remember?", "general", config)
    assert "Curated fact." in prompt_parts.session_preamble
    assert "- [id=m1] Structured fact." in prompt_parts.session_preamble


@pytest.mark.asyncio
async def test_file_backend_prompt_respects_max_entrypoint_lines(storage_path: Path, config: Config) -> None:
    config.memory.file.max_entrypoint_lines = 2

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text(
        "# Memory\nCurated fact.\n- [id=m1] Structured fact.\nTrailing fact.\n",
        encoding="utf-8",
    )

    prompt_parts = await build_memory_prompt_parts("What should I remember?", "general", config)
    assert "# Memory\nCurated fact." in prompt_parts.session_preamble
    assert "Structured fact." not in prompt_parts.session_preamble
    assert "Trailing fact." not in prompt_parts.session_preamble


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", ["lines", "tokens"])
async def test_file_backend_capped_entrypoint_withholds_the_oldest_entries(
    storage_path: Path,
    config: Config,
    cap: str,
) -> None:
    """add_memory appends, so a capped preload keeps the curated head and the newest entries, not the oldest."""
    if cap == "lines":
        config.memory.file.max_entrypoint_lines = 3
    else:
        config.memory.file.max_entrypoint_tokens = 15

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text(
        "# Memory\nCurated fact.\n- [id=m1] Oldest entry.\n- [id=m2] Middle entry.\n- [id=m3] Newest entry.\n",
        encoding="utf-8",
    )

    prompt_parts = await build_memory_prompt_parts("What should I remember?", "general", config)

    assert "# Memory\nCurated fact.\n- [id=m3] Newest entry." in prompt_parts.session_preamble
    assert "Oldest entry." not in prompt_parts.session_preamble
    assert "Middle entry." not in prompt_parts.session_preamble
    assert "[Memory entrypoint truncated - showing 3 of 5 lines" in prompt_parts.session_preamble


@pytest.mark.asyncio
async def test_file_backend_oversized_oldest_entry_does_not_withhold_newer_entries(
    storage_path: Path,
    config: Config,
) -> None:
    """An oldest entry too long for the token cap is withheld alone, not with every newer entry."""
    config.memory.file.max_entrypoint_tokens = 15

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text(
        f"# Memory\nCurated fact.\n- [id=m1] {'Long oldest entry. ' * 5}\n- [id=m2] Newest entry.\n",
        encoding="utf-8",
    )

    prompt_parts = await build_memory_prompt_parts("What should I remember?", "general", config)

    assert "# Memory\nCurated fact.\n- [id=m2] Newest entry." in prompt_parts.session_preamble
    assert "Long oldest entry." not in prompt_parts.session_preamble


@pytest.mark.asyncio
async def test_file_backend_entrypoint_preamble_announces_automatic_preload(
    storage_path: Path,
    config: Config,
) -> None:
    """The preamble must tell the agent its MEMORY.md is already inlined, and where it lives."""
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    memory_path = workspace / "MEMORY.md"
    memory_path.write_text("# Memory\nCurated fact.\n", encoding="utf-8")

    prompt_parts = await build_memory_prompt_parts(
        "What should I remember?",
        "general",
        config,
    )

    assert "is inlined below automatically every turn" in prompt_parts.session_preamble
    assert str(memory_path) in prompt_parts.session_preamble
    assert "truncated" not in prompt_parts.session_preamble


@pytest.mark.asyncio
async def test_file_backend_conversation_keeps_its_entrypoint_after_a_memory_is_added(
    storage_path: Path,
    config: Config,
) -> None:
    """An entry added mid-conversation leaves its inlined MEMORY.md unchanged; a deleted line reloads it.

    Only a kept copy tells the agent that newer entries may be missing, and an edited header loads again.
    """
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\nCurated fact.\n", encoding="utf-8")

    async def preamble(conversation: ConversationRef | None) -> str:
        prompt_parts = await public_build_memory_prompt_parts(
            "What should I remember?",
            "general",
            config,
            runtime_paths_for(config),
            current_execution_identity(),
            conversation=conversation,
            counter=fake_token_counter(),
        )
        return prompt_parts.session_preamble

    first = await preamble(ConversationRef("thread", (None, None)))
    await add_agent_memory("Added fact.", "general", config)

    assert await preamble(ConversationRef("thread", (None, None))) == first
    assert "Added fact." not in first
    assert "Added fact." in await preamble(ConversationRef("other-thread", (None, None)))
    assert PROMPT_DEFAULTS["FILE_MEMORY_ENTRYPOINT_KEPT_COPY_NOTICE"] in first
    fresh = await preamble(None)
    assert "Added fact." in fresh
    assert PROMPT_DEFAULTS["FILE_MEMORY_ENTRYPOINT_KEPT_COPY_NOTICE"] not in fresh

    (workspace / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    assert "Curated fact." not in await preamble(ConversationRef("thread", (None, None)))

    # A prompt override applies through hot reload, so an edited header loads again.
    config.prompts["FILE_MEMORY_ENTRYPOINT_HEADER_TEMPLATE"] = "Edited header for `{memory_path}`."
    assert (await preamble(ConversationRef("thread", (None, None)))).startswith("Edited header")


@pytest.mark.asyncio
async def test_file_backend_entrypoint_preamble_reports_truncated_lines(
    storage_path: Path,
    config: Config,
) -> None:
    """A capped MEMORY.md preload must say how much was withheld and where to read it."""
    config.memory.file.max_entrypoint_lines = 2

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    memory_path = workspace / "MEMORY.md"
    memory_path.write_text("# Memory\nCurated fact.\nThird fact.\nFourth fact.\n", encoding="utf-8")

    prompt_parts = await build_memory_prompt_parts(
        "What should I remember?",
        "general",
        config,
    )

    assert "[Memory entrypoint truncated - showing 2 of 4 lines" in prompt_parts.session_preamble
    assert "memory.file.max_entrypoint_lines=2" in prompt_parts.session_preamble
    assert str(memory_path) in prompt_parts.session_preamble


@pytest.mark.asyncio
async def test_file_backend_entrypoint_reports_truncation_when_preloaded_head_is_blank(
    storage_path: Path,
    config: Config,
) -> None:
    """Truncation must be reported even when the preloaded lines carry no text."""
    config.memory.file.max_entrypoint_lines = 2

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    memory_path = workspace / "MEMORY.md"
    memory_path.write_text("\n\n# Memory\nCurated fact.\nThird fact.\n", encoding="utf-8")

    prompt_parts = await build_memory_prompt_parts(
        "What should I remember?",
        "general",
        config,
    )

    assert "[Memory entrypoint truncated - showing 2 of 5 lines" in prompt_parts.session_preamble
    assert str(memory_path) in prompt_parts.session_preamble


@pytest.mark.asyncio
async def test_file_backend_entrypoint_token_cap_keeps_whole_lines(
    storage_path: Path,
    config: Config,
) -> None:
    """A few very long lines must not slip past the line cap: the token cap bounds the preload size."""
    config.memory.file.max_entrypoint_tokens = 250

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    memory_path = workspace / "MEMORY.md"
    lines = [f"fact {index} " + "x" * 392 for index in range(10)]
    memory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    prompt_parts = await build_memory_prompt_parts("What should I remember?", "general", config)

    assert lines[1] in prompt_parts.session_preamble
    assert lines[2] not in prompt_parts.session_preamble
    assert "[Memory entrypoint truncated - showing 2 of 10 lines" in prompt_parts.session_preamble
    assert "memory.file.max_entrypoint_tokens=250" in prompt_parts.session_preamble
    assert str(memory_path) in prompt_parts.session_preamble


def _fact_lines(storage_path: Path, config: Config, count_lines: int) -> list[str]:
    """Write a MEMORY.md of ``count_lines`` 41-character facts under a 100-token cap, and return its lines."""
    config.memory.file.max_entrypoint_lines = 500
    config.memory.file.max_entrypoint_tokens = 100
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    lines = [f"fact {index:02} " + "z" * 32 for index in range(count_lines)]
    (workspace / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


async def _preloaded(lines: list[str], config: Config, counter: TokenCounter) -> list[str]:
    parts = await public_build_memory_prompt_parts("x", "general", config, runtime_paths_for(config), counter=counter)
    return [line for line in lines if line in parts.session_preamble]


def _line_tokens(counter: TokenCounter, lines: list[str]) -> int:
    return sum(count(counter, f"{line}\n", upper_bound=True) for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [ModelIdentity(id="gpt-4o", provider="openai"), ModelIdentity(id="claude-opus-5-5", provider="anthropic")],
    ids=["own-tokenizer", "family-estimate"],
)
async def test_file_backend_entrypoint_fills_the_token_cap_as_the_model_counts_it(
    storage_path: Path,
    config: Config,
    identity: ModelIdentity,
) -> None:
    """The preload keeps as many whole lines as fit the cap, each counted with its newline, and the text fits it."""
    load_o200k()
    lines = _fact_lines(storage_path, config, 40)
    counter = counter_for_model(identity, local_encoder=identity.provider == "openai")

    kept = await _preloaded(lines, config, counter)

    assert count(counter, "\n".join(kept)) <= _line_tokens(counter, kept) <= 100
    assert _line_tokens(counter, lines[: len(kept) + 1]) > 100


@pytest.mark.asyncio
async def test_file_backend_entrypoint_fits_the_cap_before_the_vocabulary_loads(
    storage_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before o200k_base loads, each line counts its UTF-8 bytes, which no model's tokenizer exceeds.

    A quarter token per character would put the file at 420 tokens, under the 500-token cap.
    """
    lines = _fact_lines(storage_path, config, 40)
    config.memory.file.max_entrypoint_tokens = 500
    release = threading.Event()
    real_get_encoding = tiktoken.get_encoding

    def slow_get_encoding(name: str) -> tiktoken.Encoding:
        release.wait()
        return real_get_encoding(name)

    monkeypatch.setitem(families._encoders, "o200k_base", families._Encoder())
    monkeypatch.setattr(tiktoken, "get_encoding", slow_get_encoding)
    counter = counter_for_model(ModelIdentity(id="claude-opus-5-5", provider="anthropic"))
    try:
        kept = await _preloaded(lines, config, counter)
    finally:
        release.set()

    def utf8_bytes(chosen: list[str]) -> int:
        return sum(len(f"{line}\n".encode()) for line in chosen)

    assert utf8_bytes(kept) <= 500 < utf8_bytes(lines[: len(kept) + 1])


@pytest.mark.asyncio
async def test_file_backend_entrypoint_over_the_cap_keeps_the_head_and_the_newest_entries(
    storage_path: Path,
    config: Config,
) -> None:
    """A file over the cap keeps its curated head, then the newest entries that fit."""
    config.memory.file.max_entrypoint_tokens = 120
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    entries = [f"- [id=m{index:02}] Entry {index:02} " + "w" * 24 for index in range(20)]
    (workspace / "MEMORY.md").write_text("# Memory\nCurated fact.\n" + "\n".join(entries) + "\n", encoding="utf-8")

    counter = TokenCounter(estimate=lambda text: len(text) // 2)
    counted = await public_build_memory_prompt_parts("x", "general", config, runtime_paths_for(config), counter=counter)

    kept = [entry for entry in entries if entry in counted.session_preamble]
    assert "# Memory\nCurated fact." in counted.session_preamble
    assert 0 < len(kept) < len(entries)
    assert kept == entries[len(entries) - len(kept) :]
    assert _line_tokens(counter, ["# Memory", "Curated fact.", *kept]) <= 120


@pytest.mark.asyncio
async def test_file_backend_entrypoint_token_cap_reports_an_oversized_first_line(
    storage_path: Path,
    config: Config,
) -> None:
    """A first line larger than the token cap is withheld and still reported, never silently cut."""
    config.memory.file.max_entrypoint_tokens = 10

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("y" * 400 + "\nshort\n", encoding="utf-8")

    prompt_parts = await build_memory_prompt_parts("What should I remember?", "general", config)

    assert "y" * 400 not in prompt_parts.session_preamble
    assert "[Memory entrypoint truncated - showing 0 of 2 lines" in prompt_parts.session_preamble


@pytest.mark.asyncio
async def test_file_backend_search_skips_structured_line_duplicates(storage_path: Path, config: Config) -> None:

    await add_agent_memory("Project owner is Bas", "general", config)
    memories = await list_all_agent_memories("general", config)
    memory_id = memories[0]["id"]

    daily_file = agent_workspace_root_path(storage_path, "general") / "memory" / "2026-02-28.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text(f"- [id={memory_id}] Project owner is Bas\nProject owner is Bas\n", encoding="utf-8")

    results = await search_agent_memories("owner bas", "general", config, limit=10)
    matching_results = [result for result in results if result.get("memory") == "Project owner is Bas"]
    assert len(matching_results) == 1


@pytest.mark.asyncio
async def test_file_backend_memory_crud_and_scope(config: Config) -> None:

    await add_agent_memory("Original memory", "general", config)
    listed = await list_all_agent_memories("general", config)
    memory_id = listed[0]["id"]

    result = await get_agent_memory(memory_id, "general", config)
    assert result is not None
    assert result["memory"] == "Original memory"

    await update_agent_memory(memory_id, "Updated memory", "general", config)
    updated = await get_agent_memory(memory_id, "general", config)
    assert updated is not None
    assert updated["memory"] == "Updated memory"

    await delete_agent_memory(memory_id, "general", config)
    assert await get_agent_memory(memory_id, "general", config) is None

    await add_agent_memory("Private memory", "general", config)
    private_id = (await list_all_agent_memories("general", config))[0]["id"]
    assert await get_agent_memory(private_id, "calculator", config) is None
    with pytest.raises(ValueError, match=f"No memory found with id={private_id}"):
        await update_agent_memory(private_id, "Tampered", "calculator", config)


@pytest.mark.asyncio
async def test_file_backend_can_update_and_delete_unstructured_file_memory_line(
    storage_path: Path,
    config: Config,
) -> None:

    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text("Old raw note.\nKeep this line.\n", encoding="utf-8")

    memory_id = "file:memory/2026-06-13.md:1"
    await update_agent_memory(memory_id, "Updated raw note.", "general", config)

    assert daily_file.read_text(encoding="utf-8") == "Updated raw note.\nKeep this line.\n"
    updated = await get_agent_memory(memory_id, "general", config)
    assert updated is not None
    assert updated["memory"] == "Updated raw note."

    await delete_agent_memory(memory_id, "general", config)
    assert daily_file.read_text(encoding="utf-8") == "\nKeep this line.\n"
    assert await get_agent_memory(memory_id, "general", config) is None


@pytest.mark.asyncio
async def test_file_backend_whitespace_only_update_deletes_unstructured_file_memory_line(
    storage_path: Path,
    config: Config,
) -> None:

    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text("  Old raw note.\nKeep this line.\n", encoding="utf-8")

    memory_id = "file:memory/2026-06-13.md:1"
    await update_agent_memory(memory_id, "   \t", "general", config)

    assert daily_file.read_text(encoding="utf-8") == "\nKeep this line.\n"
    assert await get_agent_memory(memory_id, "general", config) is None


@pytest.mark.asyncio
async def test_file_backend_rejects_path_ids_outside_memory_files(
    storage_path: Path,
    config: Config,
) -> None:

    workspace = agent_workspace_root_path(storage_path, "general")
    docs_file = workspace / "docs" / "runbook.md"
    docs_file.parent.mkdir(parents=True, exist_ok=True)
    docs_file.write_text("Runbook instruction.\n", encoding="utf-8")
    soul_file = workspace / "SOUL.md"
    soul_file.write_text("Protected instruction.\n", encoding="utf-8")

    assert await get_agent_memory("file:docs/runbook.md:1", "general", config) is None
    assert await get_agent_memory("file:SOUL.md:1", "general", config) is None

    with pytest.raises(ValueError, match=r"No memory found with id=file:docs/runbook\.md:1"):
        await update_agent_memory("file:docs/runbook.md:1", "Changed.", "general", config)
    with pytest.raises(ValueError, match=r"No memory found with id=file:SOUL\.md:1"):
        await delete_agent_memory("file:SOUL.md:1", "general", config)

    assert docs_file.read_text(encoding="utf-8") == "Runbook instruction.\n"
    assert soul_file.read_text(encoding="utf-8") == "Protected instruction.\n"


@pytest.mark.asyncio
async def test_file_backend_rejects_unstructured_entrypoint_path_ids(
    storage_path: Path,
    config: Config,
) -> None:

    workspace = agent_workspace_root_path(storage_path, "general")
    memory_file = workspace / "MEMORY.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text(
        "# Memory\n\nCurated raw entrypoint line.\n- [id=m_existing] Structured entrypoint note.\n",
        encoding="utf-8",
    )
    original_content = memory_file.read_text(encoding="utf-8")

    structured = await get_agent_memory("m_existing", "general", config)
    assert structured is not None
    assert structured["memory"] == "Structured entrypoint note."
    assert await get_agent_memory("file:MEMORY.md:3", "general", config) is None

    with pytest.raises(ValueError, match=r"No memory found with id=file:MEMORY\.md:3"):
        await update_agent_memory("file:MEMORY.md:3", "Changed.", "general", config)
    with pytest.raises(ValueError, match=r"No memory found with id=file:MEMORY\.md:3"):
        await delete_agent_memory("file:MEMORY.md:3", "general", config)

    assert memory_file.read_text(encoding="utf-8") == original_content


@pytest.mark.asyncio
async def test_file_backend_store_conversation_memory_uses_agent_scope_only(
    config: Config,
) -> None:

    await store_conversation_memory(
        "Remember this requirement",
        "general",
        "session123",
        config,
    )

    agent_results = await search_agent_memories("requirement", "general", config, limit=5)
    assert any("Remember this requirement" in result.get("memory", "") for result in agent_results)


@pytest.mark.asyncio
async def test_file_backend_team_scopes_do_not_collide(storage_path: Path, config: Config) -> None:

    await store_conversation_memory("Team one memory", ["a_b", "c"], "session-one", config)
    await store_conversation_memory("Team two memory", ["a", "b_c"], "session-two", config)

    team_one = _team_memory_file(storage_path, "team_a_b+c").read_text(encoding="utf-8")
    team_two = _team_memory_file(storage_path, "team_a+b_c").read_text(encoding="utf-8")
    assert "Team one memory" in team_one
    assert "Team two memory" not in team_one
    assert "Team two memory" in team_two
    assert "Team one memory" not in team_two


@pytest.mark.asyncio
async def test_worker_scoped_team_file_memory_can_be_read_updated_and_deleted(
    storage_path: Path,
    config: Config,
) -> None:
    """An edit through one member changes only that team record, and every other member sees it."""
    config.agents["general"].worker_scope = "user_agent"
    config.agents["calculator"].worker_scope = "user_agent"
    config.teams = {"gc": MockTeamConfig(agents=["general", "calculator"])}
    team_file = _team_memory_file(storage_path, "team_calculator+general")

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="!room:example.org:$thread",
    )

    with detached_tool_scope(execution_identity):
        await store_conversation_memory("Team shared note", ["general", "calculator"], "session-alice", config)
        await store_conversation_memory("Unrelated deploy fact", ["general", "calculator"], "session-alice", config)

        general_results = await search_agent_memories("shared note", "general", config, limit=10)
        calculator_results = await search_agent_memories("shared note", "calculator", config, limit=10)
        assert len(general_results) == 1
        memory_id = general_results[0]["id"]
        assert [result["id"] for result in calculator_results] == [memory_id]

        loaded = await get_agent_memory(memory_id, "calculator", config)
        assert loaded is not None
        assert loaded["memory"] == "Team shared note"

        await update_agent_memory(memory_id, "Updated team shared note", "general", config)
        updated = await get_agent_memory(memory_id, "calculator", config)
        assert updated is not None
        assert updated["memory"] == "Updated team shared note"
        calculator_updated = await search_agent_memories("updated team", "calculator", config, limit=10)
        assert [result["memory"] for result in calculator_updated] == ["Updated team shared note"]
        assert "Unrelated deploy fact" in team_file.read_text(encoding="utf-8")

        await delete_agent_memory(memory_id, "calculator", config)
        assert await get_agent_memory(memory_id, "general", config) is None
        assert await search_agent_memories("updated team", "general", config, limit=10) == []

    team_text = team_file.read_text(encoding="utf-8")
    assert "Team shared note" not in team_text
    assert "Unrelated deploy fact" in team_text


@pytest.mark.asyncio
async def test_file_backend_rejects_path_traversal_memory_id(storage_path: Path, config: Config) -> None:

    await add_agent_memory("Safe memory", "general", config)
    secret_file = storage_path / "secret.md"
    secret_file.write_text("Do not read", encoding="utf-8")

    assert await get_agent_memory("file:../../secret.md:1", "general", config) is None


@pytest.mark.asyncio
async def test_worker_scoped_file_memory_uses_canonical_agent_workspace(
    storage_path: Path,
    config: Config,
) -> None:
    config.agents["general"].worker_scope = "user"

    canonical_workspace = agent_workspace_root_path(storage_path, "general")
    canonical_workspace.mkdir(parents=True, exist_ok=True)
    (canonical_workspace / "MEMORY.md").write_text("# Memory\n\nExisting worker memory.\n", encoding="utf-8")

    alice_identity = _identity("alice")

    with detached_tool_scope(alice_identity):
        await add_agent_memory("New worker memory", "general", config)
        prompt_parts = await build_memory_prompt_parts("worker memory", "general", config)

    content = (canonical_workspace / "MEMORY.md").read_text(encoding="utf-8")

    assert "Existing worker memory." in content
    assert "New worker memory" in content
    assert "Existing worker memory." in prompt_parts.session_preamble


@pytest.mark.asyncio
async def test_workspace_entrypoint_loaded_in_prompt(storage_path: Path, config: Config) -> None:
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("# Memory\n\nI prefer Python over JavaScript.\n", encoding="utf-8")

    prompt_parts = await build_memory_prompt_parts("What language?", "general", config)
    assert "I prefer Python over JavaScript." in prompt_parts.session_preamble
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8").startswith("# Memory")


@pytest.mark.asyncio
async def test_workspace_daily_files_live_in_canonical_scope(storage_path: Path, config: Config) -> None:
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True)

    result = append_agent_daily_memory("Daily note", "general", config)
    assert result["memory"] == "Daily note"

    daily_files = list((workspace / "memory").rglob("*.md"))
    assert len(daily_files) == 1
    assert "Daily note" in daily_files[0].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("agent_timezone", "expected_date"),
    [("Europe/Amsterdam", "2026-03-21"), (None, "2026-03-20")],
)
def test_daily_file_date_follows_the_agent_timezone(
    storage_path: Path,
    config: Config,
    agent_timezone: str | None,
    expected_date: str,
) -> None:
    """The daily file is named by the agent's own date, or by the global timezone's date when it has none."""
    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True)
    config.timezone = "America/Los_Angeles"
    config.agents["general"].timezone = agent_timezone
    instant = datetime(2026, 3, 20, 23, 30, tzinfo=UTC)

    with patch("mindroom.memory._file_backend.datetime") as mock_datetime:
        mock_datetime.now.side_effect = instant.astimezone
        append_agent_daily_memory("Daily note", "general", config)

    assert [path.name for path in (workspace / "memory").rglob("*.md")] == [f"{expected_date}.md"]


@pytest.mark.asyncio
async def test_file_backend_ignores_symlinked_memory_entries(
    storage_path: Path,
    config: Config,
) -> None:
    """Links planted in the tool workspace must never be read by the primary process."""
    outside_dir = storage_path / "outside"
    outside_dir.mkdir()
    secret = outside_dir / "environ.md"
    secret.write_text("ANTHROPIC_API_KEY=super-secret-value\n", encoding="utf-8")

    workspace = agent_workspace_root_path(storage_path, "general")
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").symlink_to(secret)
    (workspace / "memory" / "planted.md").symlink_to(secret)
    (workspace / "memory" / "linked").symlink_to(outside_dir, target_is_directory=True)

    prompt_parts = await build_memory_prompt_parts("api key", "general", config)
    assert "super-secret-value" not in prompt_parts.session_preamble
    assert "super-secret-value" not in prompt_parts.transient_turn_context
    assert await list_all_agent_memories("general", config) == []
    assert await search_agent_memories("secret", "general", config, limit=5) == []
    assert await get_agent_memory("file:memory/planted.md:1", "general", config) is None
    assert await get_agent_memory("file:memory/linked/environ.md:1", "general", config) is None


@pytest.mark.asyncio
async def test_file_backend_add_refuses_to_write_through_a_symlinked_entrypoint(
    storage_path: Path,
    config: Config,
) -> None:
    """A planted link must not redirect a memory write outside the scope."""
    target = storage_path / "outside.md"
    target.write_text("Untouched target.\n", encoding="utf-8")

    workspace = agent_workspace_root_path(storage_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").symlink_to(target)

    with pytest.raises(OSError, match="Too many levels of symbolic links"):
        await add_agent_memory("Fresh memory entry", "general", config)

    assert target.read_text(encoding="utf-8") == "Untouched target.\n"


@pytest.mark.asyncio
# A regression blocks forever on the FIFO, so the run is aborted instead of hanging.
@pytest.mark.timeout(10, method="thread")
async def test_file_backend_skips_non_regular_memory_entries(storage_path: Path, config: Config) -> None:
    """A FIFO entrypoint must not block the per-turn prompt build."""
    workspace = agent_workspace_root_path(storage_path, "general")
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    os.mkfifo(workspace / "MEMORY.md")
    os.mkfifo(workspace / "memory" / "blocking.md")

    prompt_parts = await build_memory_prompt_parts("anything", "general", config)

    assert "[File memory entrypoint (agent)]" not in prompt_parts.session_preamble
    assert await list_all_agent_memories("general", config) == []


@pytest.mark.asyncio
async def test_file_backend_caps_oversized_memory_files(storage_path: Path, config: Config) -> None:
    """An oversized memory file is read up to its cap instead of unbounded."""
    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "big.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    filler_line = "Filler note about deployment runbooks.\n"
    daily_file.write_text(
        filler_line * (1 + (1 << 20) // len(filler_line)) + "Sentinel note beyond the cap.\n",
        encoding="utf-8",
    )

    results = await list_all_agent_memories("general", config)

    assert [result["memory"] for result in results] == ["Filler note about deployment runbooks."]
    assert await search_agent_memories("sentinel", "general", config, limit=5) == []


@pytest.mark.asyncio
async def test_file_backend_lists_nested_memory_files_and_skips_bad_siblings(
    storage_path: Path,
    config: Config,
) -> None:
    """The no-follow walk finds nested notes while a planted sibling is skipped."""
    outside = storage_path / "outside.md"
    outside.write_text("Outside secret note.\n", encoding="utf-8")

    workspace = agent_workspace_root_path(storage_path, "general")
    nested = workspace / "memory" / "projects" / "api.md"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("Nested project note.\n", encoding="utf-8")
    (workspace / "memory" / "daily.md").write_text("Daily note.\n", encoding="utf-8")
    (workspace / "memory" / "planted.md").symlink_to(outside)

    results = await list_all_agent_memories("general", config)

    assert [(result["id"], result["memory"]) for result in results] == [
        ("file:memory/daily.md:1", "Daily note."),
        ("file:memory/projects/api.md:1", "Nested project note."),
    ]
    nested_memory = await get_agent_memory("file:memory/projects/api.md:1", "general", config)
    assert nested_memory is not None
    assert nested_memory["memory"] == "Nested project note."


@pytest.mark.asyncio
async def test_file_backend_limits_memory_directory_depth(storage_path: Path, config: Config) -> None:

    workspace = agent_workspace_root_path(storage_path, "general")
    directory = workspace / "memory"
    for level in range(1, 10):
        directory /= f"level{level}"
        directory.mkdir(parents=True)
        (directory / "note.md").write_text(f"Note at level {level}.\n", encoding="utf-8")

    results = await list_all_agent_memories("general", config)

    assert sorted(result["memory"] for result in results) == sorted(f"Note at level {level}." for level in range(1, 9))


@pytest.mark.asyncio
async def test_file_backend_append_writes_header_and_separator(storage_path: Path, config: Config) -> None:

    workspace = agent_workspace_root_path(storage_path, "general")
    entrypoint = workspace / "MEMORY.md"

    await add_agent_memory("First fact", "general", config)
    first_id = (await list_all_agent_memories("general", config))[0]["id"]
    assert entrypoint.read_text(encoding="utf-8") == f"# Memory\n\n- [id={first_id}] First fact\n"

    entrypoint.write_text("# Memory\n\nCurated line without newline", encoding="utf-8")
    await add_agent_memory("Second fact", "general", config)
    second_id = (await list_all_agent_memories("general", config))[0]["id"]
    assert entrypoint.read_text(encoding="utf-8") == (
        f"# Memory\n\nCurated line without newline\n- [id={second_id}] Second fact\n"
    )


@pytest.mark.asyncio
async def test_file_backend_rewrite_keeps_file_permissions(storage_path: Path, config: Config) -> None:

    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    daily_file.write_text("Old raw note.\n", encoding="utf-8")
    daily_file.chmod(0o640)

    await update_agent_memory("file:memory/2026-06-13.md:1", "New raw note.", "general", config)

    assert daily_file.read_text(encoding="utf-8") == "New raw note.\n"
    assert daily_file.stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
async def test_file_backend_refuses_to_rewrite_undecodable_memory_file(storage_path: Path, config: Config) -> None:
    """A lossy decode must fail loudly instead of writing replacement characters back."""
    workspace = agent_workspace_root_path(storage_path, "general")
    daily_file = workspace / "memory" / "2026-06-13.md"
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    original = b"Caf\xe9 note.\n- [id=m_latin] Structured caf\xe9 note.\n"
    daily_file.write_bytes(original)

    listed = await list_all_agent_memories("general", config)
    assert {result["id"] for result in listed} == {"m_latin", "file:memory/2026-06-13.md:1"}

    with pytest.raises(ValueError, match="not valid UTF-8"):
        await update_agent_memory("m_latin", "Changed.", "general", config)
    with pytest.raises(ValueError, match="not valid UTF-8"):
        await delete_agent_memory("file:memory/2026-06-13.md:1", "general", config)

    assert daily_file.read_bytes() == original


def test_semantic_memory_scores_keep_the_hybrid_rank_order() -> None:
    """A keyword-lifted hit keeps its rank: each score is the best cosine similarity at or below it."""
    hits = [
        SearchHit(content="keyword match", meta_data={"source_path": "a.md", DISTANCE_KEY: 0.7}),
        SearchHit(content="close vector", meta_data={"source_path": "b.md", DISTANCE_KEY: 0.2}),
        SearchHit(content="far vector", meta_data={"source_path": "c.md", DISTANCE_KEY: 0.5}),
    ]

    results = semantic_file_search._memory_results_from_hits(hits, scope_user_id="agent")

    assert [result["memory"] for result in results] == ["keyword match", "close vector", "far vector"]
    assert [result["score"] for result in results] == pytest.approx([0.8, 0.8, 0.5])


@pytest.mark.asyncio
async def test_only_edits_of_the_agents_own_memory_refresh_its_semantic_index(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantic search indexes the agent's own scope, so editing a team memory schedules no re-index."""
    config.memory.search.mode = "semantic"
    config.teams = {"pair": MockTeamConfig(agents=["general", "calculator"])}
    refreshed: list[str] = []
    monkeypatch.setattr(
        "mindroom.memory._file_backend.schedule_semantic_file_memory_refresh",
        lambda **kwargs: refreshed.append(str(kwargs["scope_user_id"])),
    )
    await store_conversation_memory("Quarterly metrics reviewed", ["general", "calculator"], "session", config)
    await add_agent_memory("Quarterly notes kept", "general", config)
    refreshed.clear()
    # An editable search matches by keyword, so it finds both memories before the semantic index is built.
    outcome = await public_search_agent_memories(
        "Quarterly",
        "general",
        config,
        runtime_paths_for(config),
        limit=5,
        editable=True,
    )
    entries = {entry["memory"]: entry["id"] for entry in outcome.results}

    await update_agent_memory(
        entries["Quarterly metrics reviewed"],
        "Quarterly metrics reviewed twice",
        "general",
        config,
    )
    assert refreshed == []
    await update_agent_memory(entries["Quarterly notes kept"], "Quarterly notes kept twice", "general", config)
    assert refreshed == ["agent_general"]


@pytest.mark.asyncio
async def test_agent_and_team_matches_rank_together_within_the_limit(config: Config) -> None:
    """A full team match outranks the agent's partial matches, and the limit cuts across both scopes."""
    config.teams = {"pair": MockTeamConfig(agents=["general", "calculator"])}
    await add_agent_memory("Quarterly notes kept", "general", config)
    await add_agent_memory("Budget notes kept", "general", config)
    await store_conversation_memory("Quarterly budget approved", ["general", "calculator"], "session", config)

    results = await search_agent_memories("quarterly budget", "general", config, 2)

    assert [(result["memory"], result["user_id"]) for result in results] == [
        ("Quarterly budget approved", "team_calculator+general"),
        ("Quarterly notes kept", "agent_general"),
    ]


@pytest.mark.asyncio
async def test_file_backend_refuses_an_agent_without_file_memory(config: Config) -> None:
    """Only an agent on file memory gets a file-memory root, the one this backend reads and writes."""
    config.agents["general"].memory_backend = "mem0"
    backend = FileMemoryBackend(runtime_paths=runtime_paths_for(config))

    with pytest.raises(ValueError, match="did not resolve a file-memory root"):
        await backend.list_all("general", config, limit=5)
