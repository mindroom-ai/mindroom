"""Focused tests for the shared file-memory knowledge overlay."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.resolution import memory_search
from mindroom.embeddings.batching import RefreshEmbedder
from mindroom.knowledge.file_indexer import FileIndexer
from mindroom.knowledge.file_listing import list_knowledge_files
from mindroom.knowledge.file_memory_knowledge import (
    resolve_agent_file_memory_knowledge,
    resolve_file_memory_knowledge,
)
from mindroom.knowledge.indexing_config import (
    IndexingSettings,
    indexing_settings_key,
    published_index_settings_compatible,
)
from mindroom.knowledge.vector_store import KnowledgeCollection
from mindroom.memory_scope_ids import agent_scope_user_id
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.knowledge_test_support import _FakeEmbedder

if TYPE_CHECKING:
    from pathlib import Path


def _identity(agent_name: str, requester_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )


def test_memory_and_agent_resolvers_share_base_and_index_identity(tmp_path: Path) -> None:
    """Memory and knowledge callers must resolve one compatible published index."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"alpha": AgentConfig(display_name="Alpha")},
            memory={
                "backend": "file",
                "search": {
                    "mode": "semantic",
                    "include_entrypoint": True,
                },
            },
        ),
        runtime_paths,
    )
    root = agent_workspace_root_path(runtime_paths.storage_root, "alpha")
    search_config = memory_search(config, "alpha")

    memory_resolution = resolve_file_memory_knowledge(
        scope_user_id=agent_scope_user_id("alpha"),
        root=root,
        config=config,
        search_config=search_config,
    )
    agent_resolution = resolve_agent_file_memory_knowledge(
        "alpha",
        config,
        runtime_paths,
        execution_identity=None,
    )

    assert agent_resolution is not None
    assert memory_resolution.base_id == agent_resolution.base_id
    assert memory_resolution.root == agent_resolution.root == root.resolve()
    assert memory_resolution.config.knowledge_bases[memory_resolution.base_id].include_patterns == [
        "memory/**/*.md",
        "MEMORY.md",
    ]
    assert agent_resolution.config.knowledge_bases[agent_resolution.base_id].include_patterns == [
        "memory/**/*.md",
        "MEMORY.md",
    ]
    assert indexing_settings_key(
        memory_resolution.config,
        runtime_paths.storage_root,
        memory_resolution.base_id,
        memory_resolution.root,
    ) == indexing_settings_key(
        agent_resolution.config,
        runtime_paths.storage_root,
        agent_resolution.base_id,
        agent_resolution.root,
    )


def test_agent_resolver_returns_none_for_non_agent_before_runtime_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router resolution must not attempt to materialize agent-scoped file memory."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"alpha": AgentConfig(display_name="Alpha")},
            memory={"backend": "file", "search": {"mode": "semantic"}},
        ),
        runtime_paths,
    )

    def fail_runtime_resolution(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Non-agent names must return before runtime resolution")

    monkeypatch.setattr("mindroom.knowledge.file_memory_knowledge.resolve_agent_runtime", fail_runtime_resolution)

    assert (
        resolve_agent_file_memory_knowledge(
            "router",
            config,
            runtime_paths,
            execution_identity=None,
        )
        is None
    )


@pytest.mark.parametrize(
    ("backend", "mode"),
    [
        ("mem0", "semantic"),
        ("file", "keyword"),
    ],
)
def test_agent_resolver_requires_file_backend_and_semantic_mode(
    tmp_path: Path,
    backend: str,
    mode: str,
) -> None:
    """Only effective semantic file-memory agents receive a runtime overlay."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"alpha": AgentConfig(display_name="Alpha")},
            memory={"backend": backend, "search": {"mode": mode}},
        ),
        runtime_paths,
    )

    assert (
        resolve_agent_file_memory_knowledge(
            "alpha",
            config,
            runtime_paths,
            execution_identity=None,
        )
        is None
    )


def test_private_agent_resolver_fails_closed_and_scopes_requesters(tmp_path: Path) -> None:
    """Private file-memory overlays require identity and remain requester-isolated."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(
                    display_name="Alpha",
                    private=AgentPrivateConfig(per="user", root="mind_data"),
                ),
            },
            memory={"backend": "file", "search": {"mode": "semantic"}},
        ),
        runtime_paths,
    )

    with pytest.raises(ValueError, match="requires an active execution identity"):
        resolve_agent_file_memory_knowledge(
            "alpha",
            config,
            runtime_paths,
            execution_identity=None,
        )

    alice = resolve_agent_file_memory_knowledge(
        "alpha",
        config,
        runtime_paths,
        execution_identity=_identity("alpha", "@alice:localhost"),
    )
    bob = resolve_agent_file_memory_knowledge(
        "alpha",
        config,
        runtime_paths,
        execution_identity=_identity("alpha", "@bob:localhost"),
    )

    assert alice is not None
    assert bob is not None
    assert alice.root != bob.root
    assert alice.base_id != bob.base_id


def _semantic_overlay(
    tmp_path: Path,
    agent: AgentConfig | None = None,
    search: dict[str, object] | None = None,
) -> tuple[Config, str, Path]:
    """Return the effective config, base ID, and root of one semantic file-memory agent's overlay."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"alpha": agent or AgentConfig(display_name="Alpha")},
            memory={"backend": "file", "search": {"mode": "semantic", **(search or {})}},
        ),
        runtime_paths,
    )
    root = tmp_path / "root"
    (root / "memory").mkdir(parents=True, exist_ok=True)
    resolution = resolve_file_memory_knowledge(
        scope_user_id=agent_scope_user_id("alpha"),
        root=root,
        config=config,
        search_config=memory_search(config, "alpha"),
    )
    return resolution.config, resolution.base_id, resolution.root


def _indexed_files(
    tmp_path: Path,
    agent: AgentConfig | None = None,
    search: dict[str, object] | None = None,
) -> list[str]:
    """Return the memory files one semantic file-memory overlay indexes, of a note and an archived note."""
    for relative in ("memory/notes.md", "memory/archive/2026-01-01.md"):
        target = tmp_path / "root" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("- [id=m_20260101000000_aaaa1111] A note.\n")
    config, base_id, root = _semantic_overlay(tmp_path, agent, search)
    return [path.relative_to(root).as_posix() for path in list_knowledge_files(config, base_id, root)]


def test_overlay_indexes_archive_by_default(tmp_path: Path) -> None:
    """Nothing is excluded by default, so archived memory is indexed too."""
    assert memory_search(Config(), "alpha").exclude == []
    assert _indexed_files(tmp_path) == ["memory/archive/2026-01-01.md", "memory/notes.md"]


def test_overlay_excludes_configured_patterns(tmp_path: Path) -> None:
    """A configured exclude leaves archived memory out, and an agent can index it again."""
    archive_excluded = {"exclude": ["memory/archive/**"]}

    assert _indexed_files(tmp_path / "global", search=archive_excluded) == ["memory/notes.md"]
    assert _indexed_files(
        tmp_path / "agent",
        AgentConfig(display_name="Alpha", memory_search=archive_excluded),
    ) == ["memory/notes.md"]
    assert _indexed_files(
        tmp_path / "override",
        AgentConfig(display_name="Alpha", memory_search={"exclude": []}),
        archive_excluded,
    ) == ["memory/archive/2026-01-01.md", "memory/notes.md"]


def test_overlay_uses_memory_entry_chunks(tmp_path: Path) -> None:
    """The file-memory index holds one chunk per memory entry, with the metadata fixed-size chunks carry."""
    config, base_id, root = _semantic_overlay(tmp_path)
    source = root / "memory" / "2026-01-10.md"
    source.write_text(
        "# 2026-01-10\n\n"
        "- [id=m_20260110090000_aaaa1111] The garden shed is painted green.\n"
        "- [id=m_20260110100000_bbbb2222] Pixel eats   salmon kibble.\n",
    )
    indexer = FileIndexer(
        base_id=base_id,
        config=config,
        runtime_paths=test_runtime_paths(tmp_path),
        knowledge_root=root,
        collection=KnowledgeCollection(path=tmp_path / "collections", name="memory"),
        build_embedder=lambda: RefreshEmbedder(_FakeEmbedder()),
        max_concurrent_file_indexes=1,
    )

    prepared = indexer._read_file(source)

    assert prepared is not None
    assert [chunk.text for chunk in prepared.chunks] == [
        "- [id=m_20260110090000_aaaa1111] The garden shed is painted green.",
        "- [id=m_20260110100000_bbbb2222] Pixel eats salmon kibble.",
    ]
    assert [chunk.name for chunk in prepared.chunks] == ["2026-01-10.md", "2026-01-10.md"]
    assert [(chunk.metadata["chunk"], chunk.metadata["chunk_size"]) for chunk in prepared.chunks] == [
        (1, len(prepared.chunks[0].text)),
        (2, len(prepared.chunks[1].text)),
    ]


def test_entry_chunks_are_capped_at_the_chunk_size(tmp_path: Path) -> None:
    """The overlay caps entries at its 1,200-character chunk size, which a heading-only file is chunked at too."""
    config, base_id, root = _semantic_overlay(tmp_path)
    assert config.knowledge_bases[base_id].chunk_size == 1_200
    small = config.with_runtime_knowledge_base_overlay(
        base_id,
        config.knowledge_bases[base_id].model_copy(update={"chunk_size": 300}),
    )
    indexer = FileIndexer(
        base_id=base_id,
        config=small,
        runtime_paths=test_runtime_paths(tmp_path),
        knowledge_root=root,
        collection=KnowledgeCollection(path=tmp_path / "collections", name="memory"),
        build_embedder=lambda: RefreshEmbedder(_FakeEmbedder()),
        max_concurrent_file_indexes=1,
    )
    entry = root / "memory" / "notes.md"
    entry.write_text("- [id=m_20260110090000_aaaa1111] " + "word " * 140)
    headings = root / "memory" / "headings.md"
    headings.write_text("".join(f"# Heading number {number}\n" for number in range(40)))

    entry_chunks = indexer._read_file(entry)
    heading_chunks = indexer._read_file(headings)

    assert entry_chunks is not None
    assert heading_chunks is not None
    assert len(entry_chunks.chunks) == 3
    assert len(heading_chunks.chunks) > 1
    assert all(len(chunk.text) <= 300 for chunk in [*entry_chunks.chunks, *heading_chunks.chunks])


def test_chunk_strategy_is_part_of_the_index_settings(tmp_path: Path) -> None:
    """Index settings differ by chunk strategy, also from an index written before the strategy was recorded.

    The old index stays compatible for search meanwhile, as after a `chunk_size` change.
    """
    config, base_id, root = _semantic_overlay(tmp_path)
    base_config = config.knowledge_bases[base_id]
    fixed_config = config.with_runtime_knowledge_base_overlay(
        base_id,
        base_config.model_copy(update={"chunk_strategy": "fixed"}),
    )
    entries = indexing_settings_key(config, tmp_path, base_id, root)
    fixed = indexing_settings_key(fixed_config, tmp_path, base_id, root)
    unrecorded = fixed.to_metadata()
    del unrecorded["chunk_strategy"]

    assert base_config.chunk_strategy == "memory_entries"
    assert entries != fixed
    assert IndexingSettings.from_metadata(entries.to_metadata()) == entries
    assert IndexingSettings.from_metadata(unrecorded) == fixed
    assert published_index_settings_compatible(fixed, entries)


def test_rerank_flag_keeps_index_signature(tmp_path: Path) -> None:
    """Turning memory reranking on changes no index setting, so the memory index is not embedded again."""
    plain_config, base_id, root = _semantic_overlay(tmp_path)
    reranked_config, reranked_base_id, reranked_root = _semantic_overlay(tmp_path, search={"rerank": True})

    assert reranked_config.memory.search.rerank
    assert (reranked_base_id, reranked_root) == (base_id, root)
    assert indexing_settings_key(reranked_config, tmp_path, base_id, root) == indexing_settings_key(
        plain_config,
        tmp_path,
        base_id,
        root,
    )
