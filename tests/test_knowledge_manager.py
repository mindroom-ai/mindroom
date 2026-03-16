"""Tests for KnowledgeManager internals."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, call

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentPrivateKnowledgeConfig
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.knowledge.manager import (
    _FAILED_SIGNATURE_RETRY_NS,
    KnowledgeManager,
    _create_embedder,
    ensure_agent_knowledge_managers,
    get_knowledge_manager,
    initialize_knowledge_managers,
    shutdown_knowledge_managers,
)
from mindroom.knowledge.utils import bound_knowledge_managers, get_knowledge_for_base
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key, worker_root_path
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class _DummyVectorDb:
    def delete(self) -> bool:
        return True

    def create(self) -> None:
        return None

    def exists(self) -> bool:
        return True


class _DummyCollection:
    def count(self) -> int:
        return len(_DummyChromaDb.metadatas)

    def get(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        include: list[str] | None = None,
        where: dict[str, object] | None = None,
    ) -> dict[str, object]:
        _ = include
        selected_all = _DummyChromaDb.metadatas
        if where:
            key, value = next(iter(where.items()))
            selected_all = [
                metadata for metadata in selected_all if isinstance(metadata, dict) and metadata.get(key) == value
            ]
        selected = selected_all[offset:] if limit is None else selected_all[offset : offset + limit]
        ids = [str(index) for index in range(offset, offset + len(selected))]
        return {"ids": ids, "metadatas": selected}


class _DummyClient:
    def get_collection(self, name: str) -> _DummyCollection:
        _ = name
        return _DummyCollection()


class _DummyKnowledge:
    def __init__(self, vector_db: _DummyVectorDb) -> None:
        self.vector_db = vector_db
        self.insert_calls: list[dict[str, object]] = []
        self.remove_calls: list[dict[str, object]] = []

    def insert(
        self,
        *,
        path: str,
        metadata: dict[str, object],
        upsert: bool,
        reader: object | None = None,
    ) -> None:
        self.insert_calls.append({"path": path, "metadata": metadata, "upsert": upsert, "reader": reader})
        _DummyChromaDb.metadatas.append(dict(metadata))

    async def ainsert(
        self,
        *,
        path: str,
        metadata: dict[str, object],
        upsert: bool,
        reader: object | None = None,
    ) -> None:
        self.insert(path=path, metadata=metadata, upsert=upsert, reader=reader)

    def remove_vectors_by_metadata(self, metadata: dict[str, object]) -> bool:
        self.remove_calls.append(metadata)
        return True


class _DummyChromaDb:
    metadatas: ClassVar[list[object]] = []

    def __init__(self, **_: object) -> None:
        self.collection_name = "mindroom_knowledge"
        self.client = _DummyClient()

    def delete(self) -> bool:
        return True

    def create(self) -> None:
        return None

    def exists(self) -> bool:
        return True


def _mind_private_agent(
    *,
    watch: bool,
    template_dir: str,
    git: KnowledgeGitConfig | None = None,
) -> AgentConfig:
    """Return a worker-scoped private Mind agent config for knowledge tests."""
    return AgentConfig(
        display_name="Mind",
        memory_backend="file",
        private=AgentPrivateConfig(
            per="user",
            root="mind_data",
            template_dir=template_dir,
            context_files=["SOUL.md"],
            knowledge=AgentPrivateKnowledgeConfig(
                path="memory",
                watch=watch,
                git=git,
            ),
        ),
    )


def _make_config(path: Path, *, embedder_dimensions: int | None = None) -> Config:
    memory: dict[str, object] | None = None
    if embedder_dimensions is not None:
        memory = {
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "gemini-embedding-001",
                    "host": "http://example.com/v1",
                    "dimensions": embedder_dimensions,
                },
            },
        }
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(path), watch=False),
        },
        **({"memory": memory} if memory is not None else {}),
    )
    return bind_runtime_paths(
        config,
        _runtime_paths(path.parent / "config.yaml", path.parent / "storage"),
    )


def _make_git_config(
    path: Path,
    *,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> Config:
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(
                path=str(path),
                watch=False,
                git=KnowledgeGitConfig(
                    repo_url="https://github.com/example/knowledge.git",
                    branch="main",
                    poll_interval_seconds=30,
                    skip_hidden=True,
                    include_patterns=include_patterns or [],
                    exclude_patterns=exclude_patterns or [],
                ),
            ),
        },
    )
    return bind_runtime_paths(
        config,
        _runtime_paths(path.parent / "config.yaml", path.parent / "storage"),
    )


def _runtime_paths(config_path: Path, storage_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=config_path, storage_path=storage_path)


@pytest.fixture
def dummy_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KnowledgeManager:
    """Build a KnowledgeManager with lightweight fakes for vector operations."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_config(tmp_path / "knowledge")
    return KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )


def test_knowledge_base_relative_path_resolves_from_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Knowledge base relative paths should resolve from the config directory."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    runtime_paths = _runtime_paths(config_dir / "config.yaml", tmp_path / "storage")

    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path="knowledge", watch=False),
        },
    )
    manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths,
        storage_path=runtime_paths.storage_root,
        knowledge_path=(config_dir / "knowledge").resolve(),
    )

    assert manager.knowledge_path == (config_dir / "knowledge").resolve()


def test_knowledge_manager_reindexes_when_embedding_dimensions_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing embedder dimensions should invalidate the existing knowledge manager."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    storage_path = tmp_path / "storage"
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", storage_path)
    storage_path = runtime_paths.storage_root
    knowledge_path = (tmp_path / "knowledge").resolve()
    config_1536 = _make_config(tmp_path / "knowledge", embedder_dimensions=1536)
    config_3072 = _make_config(tmp_path / "knowledge", embedder_dimensions=3072)

    manager = KnowledgeManager(
        base_id="research",
        config=config_1536,
        runtime_paths=runtime_paths,
        storage_path=storage_path,
        knowledge_path=knowledge_path,
    )

    assert not manager.matches(config_3072, storage_path, knowledge_path)
    assert manager.needs_full_reindex(config_3072, storage_path, knowledge_path)


def test_knowledge_manager_keeps_index_for_equivalent_openai_default_dimensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equivalent OpenAI defaults should not trigger a full knowledge reindex."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    storage_path = tmp_path / "storage"
    knowledge_path = (tmp_path / "knowledge").resolve()
    implicit_default = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "knowledge"), watch=False),
        },
        memory={
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                },
            },
        },
    )
    explicit_default = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "knowledge"), watch=False),
        },
        memory={
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                    "dimensions": 1536,
                },
            },
        },
    )

    runtime_paths = _runtime_paths(tmp_path / "config.yaml", storage_path)
    manager = KnowledgeManager(
        base_id="research",
        config=implicit_default,
        runtime_paths=runtime_paths,
        storage_path=runtime_paths.storage_root,
        knowledge_path=knowledge_path,
    )

    assert manager.matches(explicit_default, storage_path, knowledge_path)
    assert not manager.needs_full_reindex(explicit_default, storage_path, knowledge_path)


def test_create_embedder_supports_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Knowledge embedders should support the local sentence-transformers provider."""
    sentinel = object()
    captured: dict[str, object] = {}

    def _fake_create(runtime_paths: object, model: str, *, dimensions: int | None = None) -> object:
        captured["runtime_paths"] = runtime_paths
        captured["model"] = model
        captured["dimensions"] = dimensions
        return sentinel

    monkeypatch.setattr("mindroom.knowledge.manager.create_sentence_transformers_embedder", _fake_create)

    config = Config(
        agents={},
        models={},
        memory={
            "embedder": {
                "provider": "sentence_transformers",
                "config": {
                    "model": "sentence-transformers/all-MiniLM-L6-v2",
                    "dimensions": 384,
                },
            },
        },
    )

    runtime_paths = resolve_runtime_paths()
    assert _create_embedder(config, runtime_paths) is sentinel
    assert captured == {
        "runtime_paths": runtime_paths,
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "dimensions": 384,
    }


def test_resolve_file_path_rejects_traversal(dummy_manager: KnowledgeManager) -> None:
    """resolve_file_path should reject escapes outside the knowledge root."""
    with pytest.raises(ValueError, match="outside knowledge folder"):
        dummy_manager.resolve_file_path("../escape.txt")


@pytest.mark.asyncio
async def test_index_file_upsert_removes_existing_vectors(dummy_manager: KnowledgeManager) -> None:
    """Upsert should remove vectors for the same source_path before insert."""
    file_path = dummy_manager.knowledge_path / "doc.txt"
    file_path.write_text("test", encoding="utf-8")

    indexed = await dummy_manager.index_file(file_path, upsert=True)

    assert indexed is True
    knowledge = dummy_manager.get_knowledge()
    assert isinstance(knowledge, _DummyKnowledge)
    assert knowledge.remove_calls == [{"source_path": "doc.txt"}]
    metadata = knowledge.insert_calls[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["source_path"] == "doc.txt"
    assert isinstance(metadata["source_mtime_ns"], int)
    assert metadata["source_size"] == 4
    assert knowledge.insert_calls[0]["upsert"] is True
    reader = knowledge.insert_calls[0]["reader"]
    assert reader is not None
    assert getattr(reader, "chunk", None) is True
    assert getattr(reader, "chunk_size", None) == 5000


@pytest.mark.asyncio
async def test_index_file_uses_configured_chunk_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Knowledge manager should apply per-base chunk settings when indexing text files."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(
                path=str(tmp_path / "knowledge"),
                watch=False,
                chunk_size=640,
                chunk_overlap=32,
            ),
        },
    )
    manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )
    file_path = manager.knowledge_path / "doc.md"
    file_path.write_text("test", encoding="utf-8")

    indexed = await manager.index_file(file_path, upsert=True)

    assert indexed is True
    knowledge = manager.get_knowledge()
    assert isinstance(knowledge, _DummyKnowledge)
    reader = knowledge.insert_calls[0]["reader"]
    assert reader is not None
    assert getattr(reader, "chunk_size", None) == 640
    chunking_strategy = getattr(reader, "chunking_strategy", None)
    assert chunking_strategy is not None
    assert getattr(chunking_strategy, "chunk_size", None) == 640
    assert getattr(chunking_strategy, "overlap", None) == 32


def test_knowledge_base_chunk_overlap_must_be_smaller_than_chunk_size() -> None:
    """KnowledgeBaseConfig should reject overlap >= size."""
    with pytest.raises(ValidationError, match="chunk_overlap must be smaller than chunk_size"):
        KnowledgeBaseConfig(path="./docs", chunk_size=500, chunk_overlap=500)


@pytest.mark.asyncio
async def test_load_indexed_files_recovers_source_paths(dummy_manager: KnowledgeManager) -> None:
    """Indexed source paths should be recovered from persisted vector metadata."""
    _DummyChromaDb.metadatas = [
        {"source_path": "docs/a.txt"},
        {"source_path": "docs/a.txt"},
        {"source_path": "notes/b.md"},
        {"other": "value"},
        None,
    ]

    indexed_count = await dummy_manager.load_indexed_files()

    assert indexed_count == 2
    status = dummy_manager.get_status()
    assert status["indexed_count"] == 2


@pytest.mark.asyncio
async def test_sync_indexed_files_skips_unchanged_files(dummy_manager: KnowledgeManager) -> None:
    """sync_indexed_files should not upsert files when persisted signatures match disk."""
    file_path = dummy_manager.knowledge_path / "doc.md"
    file_path.write_text("same", encoding="utf-8")
    stat = file_path.stat()
    _DummyChromaDb.metadatas = [
        {
            "source_path": "doc.md",
            "source_mtime_ns": stat.st_mtime_ns,
            "source_size": stat.st_size,
        },
    ]
    dummy_manager.index_file = AsyncMock(return_value=True)
    dummy_manager.remove_file = AsyncMock(return_value=True)

    result = await dummy_manager.sync_indexed_files()

    assert result == {"loaded_count": 1, "indexed_count": 0, "removed_count": 0}
    dummy_manager.index_file.assert_not_awaited()
    dummy_manager.remove_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_indexed_files_skips_legacy_entries_without_signatures(dummy_manager: KnowledgeManager) -> None:
    """Legacy entries with only source_path metadata should not force reindex on restart."""
    file_path = dummy_manager.knowledge_path / "doc.md"
    file_path.write_text("same", encoding="utf-8")
    _DummyChromaDb.metadatas = [{"source_path": "doc.md"}]
    dummy_manager.index_file = AsyncMock(return_value=True)
    dummy_manager.remove_file = AsyncMock(return_value=True)

    result = await dummy_manager.sync_indexed_files()

    assert result == {"loaded_count": 1, "indexed_count": 0, "removed_count": 0}
    dummy_manager.index_file.assert_not_awaited()
    dummy_manager.remove_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_indexed_files_skips_retries_for_failed_unchanged_files(
    dummy_manager: KnowledgeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed unchanged files should be skipped until content changes."""
    file_path = dummy_manager.knowledge_path / "doc.md"
    file_path.write_text("same", encoding="utf-8")
    stat = file_path.stat()
    _DummyChromaDb.metadatas = []

    now_ns = 1_000_000_000_000
    monkeypatch.setattr("mindroom.knowledge.manager.time.time_ns", lambda: now_ns)
    failed_signatures = {"doc.md": (stat.st_mtime_ns, stat.st_size, now_ns)}
    dummy_manager.index_file = AsyncMock(return_value=True)
    dummy_manager.remove_file = AsyncMock(return_value=True)
    dummy_manager._load_failed_signatures = lambda: failed_signatures
    saved: dict[str, tuple[int, int, int]] = {}
    dummy_manager._save_failed_signatures = lambda value: saved.update(value)

    result = await dummy_manager.sync_indexed_files()

    assert result == {"loaded_count": 0, "indexed_count": 0, "removed_count": 0}
    dummy_manager.index_file.assert_not_awaited()
    dummy_manager.remove_file.assert_not_awaited()
    assert saved == failed_signatures


@pytest.mark.asyncio
async def test_sync_indexed_files_retries_failed_unchanged_files_after_retry_window(
    dummy_manager: KnowledgeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed unchanged files should be retried after the retry window elapses."""
    file_path = dummy_manager.knowledge_path / "doc.md"
    file_path.write_text("same", encoding="utf-8")
    stat = file_path.stat()
    _DummyChromaDb.metadatas = []

    now_ns = 2_000_000_000_000
    monkeypatch.setattr("mindroom.knowledge.manager.time.time_ns", lambda: now_ns)
    failed_signatures = {"doc.md": (stat.st_mtime_ns, stat.st_size, now_ns - _FAILED_SIGNATURE_RETRY_NS - 1)}
    dummy_manager.index_file = AsyncMock(return_value=True)
    dummy_manager.remove_file = AsyncMock(return_value=True)
    dummy_manager._load_failed_signatures = lambda: failed_signatures
    saved: dict[str, tuple[int, int, int]] = {}
    dummy_manager._save_failed_signatures = lambda value: saved.update(value)

    result = await dummy_manager.sync_indexed_files()

    assert result == {"loaded_count": 0, "indexed_count": 1, "removed_count": 0}
    dummy_manager.index_file.assert_awaited_once_with("doc.md", upsert=True)
    dummy_manager.remove_file.assert_not_awaited()
    assert saved == {}


@pytest.mark.asyncio
async def test_sync_indexed_files_upserts_changed_and_removes_deleted(dummy_manager: KnowledgeManager) -> None:
    """sync_indexed_files should update changed files and remove stale indexed entries."""
    file_path = dummy_manager.knowledge_path / "doc.md"
    file_path.write_text("changed content", encoding="utf-8")
    _DummyChromaDb.metadatas = [
        {"source_path": "doc.md", "source_mtime_ns": 1, "source_size": 1},
        {"source_path": "deleted.md", "source_mtime_ns": 1, "source_size": 1},
    ]
    dummy_manager.index_file = AsyncMock(return_value=True)
    dummy_manager.remove_file = AsyncMock(return_value=True)

    result = await dummy_manager.sync_indexed_files()

    assert result == {"loaded_count": 2, "indexed_count": 1, "removed_count": 1}
    dummy_manager.index_file.assert_awaited_once_with("doc.md", upsert=True)
    dummy_manager.remove_file.assert_awaited_once_with("deleted.md")


@pytest.mark.asyncio
async def test_sync_indexed_files_does_not_suppress_retry_for_previously_indexed(
    dummy_manager: KnowledgeManager,
) -> None:
    """A previously-indexed file whose upsert fails should NOT be recorded as a persistent failure."""
    file_path = dummy_manager.knowledge_path / "doc.md"
    file_path.write_text("changed content", encoding="utf-8")
    _DummyChromaDb.metadatas = [
        {"source_path": "doc.md", "source_mtime_ns": 1, "source_size": 1},
    ]
    dummy_manager.index_file = AsyncMock(return_value=False)
    dummy_manager.remove_file = AsyncMock(return_value=True)
    saved: dict[str, tuple[int, int, int]] = {}
    dummy_manager._save_failed_signatures = lambda value: saved.update(value)

    result = await dummy_manager.sync_indexed_files()

    assert result == {"loaded_count": 1, "indexed_count": 0, "removed_count": 0}
    dummy_manager.index_file.assert_awaited_once_with("doc.md", upsert=True)
    # Failure must NOT be persisted — file was previously indexed and lost its
    # vectors during the upsert; it must be retried on next startup.
    assert saved == {}


@pytest.mark.asyncio
async def test_initialize_knowledge_managers_maintains_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry should track configured bases and remove stale managers."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "research"), watch=False),
            "legal": KnowledgeBaseConfig(path=str(tmp_path / "legal"), watch=False),
        },
    )
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage")

    managers = await initialize_knowledge_managers(config, runtime_paths, reindex_on_create=False)
    assert set(managers) == {"research", "legal"}
    assert get_knowledge_manager("research") is managers["research"]

    updated_config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "research"), watch=False),
        },
    )

    managers = await initialize_knowledge_managers(updated_config, runtime_paths, reindex_on_create=False)
    assert set(managers) == {"research"}
    assert get_knowledge_manager("legal") is None

    await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_knowledge_managers_full_reindex_on_settings_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index-affecting settings changes must trigger full reindex, not incremental sync."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "research"), watch=False),
        },
    )
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage")

    managers = await initialize_knowledge_managers(config, runtime_paths, reindex_on_create=False)
    original_manager = managers["research"]

    # Change chunk_size to trigger an index-affecting settings mismatch.
    updated_config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(
                path=str(tmp_path / "research"),
                watch=False,
                chunk_size=1234,
            ),
        },
    )

    initialize = AsyncMock()
    sync_indexed_files = AsyncMock(return_value={"loaded_count": 0, "indexed_count": 0, "removed_count": 0})
    monkeypatch.setattr(KnowledgeManager, "initialize", initialize)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)

    managers = await initialize_knowledge_managers(updated_config, runtime_paths, reindex_on_create=False)
    new_manager = managers["research"]
    assert new_manager is not original_manager
    initialize.assert_awaited_once()
    sync_indexed_files.assert_not_awaited()

    await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_knowledge_managers_non_index_setting_change_uses_incremental_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-index settings (like watch) should keep startup on incremental sync."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "research"), watch=False),
        },
    )
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage")

    managers = await initialize_knowledge_managers(config, runtime_paths, reindex_on_create=False)
    original_manager = managers["research"]

    updated_config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "research"), watch=True),
        },
    )

    initialize = AsyncMock()
    sync_indexed_files = AsyncMock(return_value={"loaded_count": 1, "indexed_count": 0, "removed_count": 0})
    monkeypatch.setattr(KnowledgeManager, "initialize", initialize)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)

    managers = await initialize_knowledge_managers(updated_config, runtime_paths, reindex_on_create=False)
    new_manager = managers["research"]
    assert new_manager is not original_manager
    initialize.assert_not_awaited()
    sync_indexed_files.assert_awaited_once()

    await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_private_knowledge_managers_copy_template_and_isolate_worker_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Private knowledge should copy the configured template into requester-scoped roots."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": _mind_private_agent(watch=False, template_dir=str(template_dir)),
        },
        models={},
    )
    config = bind_runtime_paths(config, _runtime_paths(tmp_path / "config.yaml", tmp_path))
    private_base_id = config.get_agent_private_knowledge_base_id("mind")
    assert private_base_id is not None

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-bob",
    )

    try:
        await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=alice_identity,
        )
        await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=bob_identity,
        )

        assert get_knowledge_manager(private_base_id) is None

        alice_manager = get_knowledge_manager(
            private_base_id,
            config=config,
            runtime_paths=runtime_paths_for(config),
            execution_identity=alice_identity,
        )
        bob_manager = get_knowledge_manager(
            private_base_id,
            config=config,
            runtime_paths=runtime_paths_for(config),
            execution_identity=bob_identity,
        )

        assert alice_manager is not None
        assert bob_manager is not None
        assert alice_manager is not bob_manager

        alice_worker_key = resolve_worker_key("user", alice_identity)
        bob_worker_key = resolve_worker_key("user", bob_identity)
        assert alice_worker_key is not None
        assert bob_worker_key is not None

        alice_workspace = worker_root_path(tmp_path, alice_worker_key) / "mind_data"
        bob_workspace = worker_root_path(tmp_path, bob_worker_key) / "mind_data"
        assert alice_manager.knowledge_path == (alice_workspace / "memory").resolve()
        assert bob_manager.knowledge_path == (bob_workspace / "memory").resolve()
        assert (alice_workspace / "SOUL.md").exists()
        assert (alice_workspace / "MEMORY.md").exists()
        assert (bob_workspace / "SOUL.md").exists()
        assert (bob_workspace / "MEMORY.md").exists()
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_private_knowledge_single_file_target_indexes_without_creating_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Private knowledge paths may point to a single file inside the private root."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir(
        files={
            "USER.md": "Private user profile.\n",
            "MEMORY.md": "# Memory\n",
        },
    )
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                private=AgentPrivateConfig(
                    per="user",
                    root="mind_data",
                    template_dir=str(template_dir),
                    knowledge=AgentPrivateKnowledgeConfig(path="USER.md", watch=False),
                ),
            ),
        },
        models={},
    )
    config = bind_runtime_paths(config, _runtime_paths(tmp_path / "config.yaml", tmp_path))
    private_base_id = config.get_agent_private_knowledge_base_id("mind")
    assert private_base_id is not None

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    try:
        managers = await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=identity,
        )
        manager = managers[private_base_id]

        worker_key = resolve_worker_key("user", identity)
        assert worker_key is not None
        knowledge_file = worker_root_path(tmp_path, worker_key) / "mind_data" / "USER.md"

        assert manager.knowledge_path == knowledge_file.resolve()
        assert knowledge_file.is_file()
        assert manager.list_files() == [knowledge_file.resolve()]
        assert _DummyChromaDb.metadatas == [
            {
                "source_path": "USER.md",
                "source_mtime_ns": knowledge_file.stat().st_mtime_ns,
                "source_size": knowledge_file.stat().st_size,
            },
        ]
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_shared_knowledge_missing_dotted_directory_path_is_not_misclassified_as_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing dotted directory names should stay directory-like instead of being forced into file mode."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_path = tmp_path / "nested" / "docs.v1"
    config = Config(
        agents={
            "researcher": AgentConfig(
                display_name="Researcher",
                knowledge_bases=["docs"],
            ),
        },
        models={},
        knowledge_bases={
            "docs": KnowledgeBaseConfig(path=str(docs_path), watch=False),
        },
    )
    config = bind_runtime_paths(config, _runtime_paths(tmp_path / "config.yaml", tmp_path))

    try:
        managers = await ensure_agent_knowledge_managers("researcher", config, runtime_paths_for(config))
        manager = managers["docs"]

        assert docs_path.parent.is_dir()
        assert not docs_path.exists()
        assert manager.list_files() == []

        docs_path.mkdir(parents=True, exist_ok=True)
        guide_path = docs_path / "guide.md"
        guide_path.write_text("Shared docs.\n", encoding="utf-8")

        assert await manager.index_file(guide_path, upsert=True)
        assert manager.list_files() == [guide_path.resolve()]
        assert _DummyChromaDb.metadatas == [
            {
                "source_path": "guide.md",
                "source_mtime_ns": guide_path.stat().st_mtime_ns,
                "source_size": guide_path.stat().st_size,
            },
        ]
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_worker_scoped_private_knowledge_refreshes_on_access_without_background_watchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Worker-scoped private knowledge should refresh on access instead of starting persistent watchers."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": _mind_private_agent(watch=True, template_dir=str(template_dir)),
        },
        models={},
    )
    config = bind_runtime_paths(config, _runtime_paths(tmp_path / "config.yaml", tmp_path))

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    sync_indexed_files = AsyncMock(return_value={"loaded_count": 0, "indexed_count": 0, "removed_count": 0})
    start_watcher = AsyncMock()
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)
    monkeypatch.setattr(KnowledgeManager, "start_watcher", start_watcher)

    try:
        await ensure_agent_knowledge_managers("mind", config, runtime_paths_for(config), execution_identity=identity)
        await ensure_agent_knowledge_managers("mind", config, runtime_paths_for(config), execution_identity=identity)

        assert sync_indexed_files.await_count == 2
        start_watcher.assert_not_awaited()
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_worker_scoped_git_private_knowledge_refreshes_on_access_without_background_watchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Worker-scoped Git knowledge should refresh on access instead of starting git polling tasks."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": _mind_private_agent(
                watch=True,
                template_dir=str(template_dir),
                git=KnowledgeGitConfig(
                    repo_url="https://github.com/example/memory.git",
                    branch="main",
                    poll_interval_seconds=30,
                ),
            ),
        },
        models={},
    )
    config = bind_runtime_paths(config, _runtime_paths(tmp_path / "config.yaml", tmp_path))

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    sync_git_repository = AsyncMock(return_value={"updated": False, "changed_count": 0, "removed_count": 0})
    sync_indexed_files = AsyncMock()
    start_watcher = AsyncMock()
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)
    monkeypatch.setattr(KnowledgeManager, "start_watcher", start_watcher)

    try:
        await ensure_agent_knowledge_managers("mind", config, runtime_paths_for(config), execution_identity=identity)
        await ensure_agent_knowledge_managers("mind", config, runtime_paths_for(config), execution_identity=identity)

        assert sync_git_repository.await_count == 2
        sync_indexed_files.assert_not_awaited()
        start_watcher.assert_not_awaited()
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_knowledge_managers_keeps_private_scoped_managers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Static manager initialization should not tear down live scoped private managers."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": _mind_private_agent(watch=False, template_dir=str(template_dir)),
        },
        models={},
    )
    config = bind_runtime_paths(config, _runtime_paths(tmp_path / "config.yaml", tmp_path))
    private_base_id = config.get_agent_private_knowledge_base_id("mind")
    assert private_base_id is not None

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    try:
        managers = await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=identity,
        )
        scoped_manager = managers[private_base_id]

        static_managers = await initialize_knowledge_managers(
            config,
            _runtime_paths(tmp_path / "config.yaml", tmp_path),
            reindex_on_create=False,
        )
        resolved_manager = get_knowledge_manager(
            private_base_id,
            config=config,
            runtime_paths=runtime_paths_for(config),
            execution_identity=identity,
        )

        assert static_managers == {}
        assert resolved_manager is scoped_manager
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_get_knowledge_for_base_reuses_shared_manager_created_by_agent_ensure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared-base lookups should find managers created through ensure_agent_knowledge_managers()."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "guide.md").write_text("Shared docs.\n", encoding="utf-8")
    config = Config(
        agents={
            "researcher": AgentConfig(
                display_name="Researcher",
                knowledge_bases=["docs"],
            ),
        },
        models={},
        knowledge_bases={
            "docs": KnowledgeBaseConfig(path=str(docs_path), watch=False),
        },
    )
    config = bind_runtime_paths(config, _runtime_paths(tmp_path / "config.yaml", tmp_path))

    try:
        managers = await ensure_agent_knowledge_managers("researcher", config, runtime_paths_for(config))
        manager = managers["docs"]
        knowledge = get_knowledge_for_base("docs", config=config, runtime_paths=runtime_paths_for(config))

        assert knowledge is manager.get_knowledge()
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_knowledge_managers_removes_private_scoped_managers_when_private_knowledge_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Config reload should tear down scoped private managers once the agent drops private knowledge."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    config_with_private = Config(
        agents={
            "mind": _mind_private_agent(watch=False, template_dir=str(template_dir)),
        },
        models={},
    )
    config_without_private = Config(
        agents={
            "mind": AgentConfig(display_name="Mind"),
        },
        models={},
    )
    config_with_private = bind_runtime_paths(config_with_private, _runtime_paths(tmp_path / "config.yaml", tmp_path))
    config_without_private = bind_runtime_paths(
        config_without_private,
        _runtime_paths(tmp_path / "config.yaml", tmp_path),
    )
    private_base_id = config_with_private.get_agent_private_knowledge_base_id("mind")
    assert private_base_id is not None

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    try:
        await ensure_agent_knowledge_managers(
            "mind",
            config_with_private,
            runtime_paths_for(config_with_private),
            execution_identity=identity,
        )
        assert (
            get_knowledge_manager(
                private_base_id,
                config=config_with_private,
                runtime_paths=runtime_paths_for(config_with_private),
                execution_identity=identity,
            )
            is not None
        )

        await initialize_knowledge_managers(
            config_without_private,
            _runtime_paths(tmp_path / "config.yaml", tmp_path),
            reindex_on_create=False,
        )

        assert (
            get_knowledge_manager(
                private_base_id,
                config=config_with_private,
                runtime_paths=runtime_paths_for(config_with_private),
                execution_identity=identity,
            )
            is None
        )
        assert get_knowledge_manager(private_base_id) is None
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_private_scoped_knowledge_manager_cache_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Older scoped private managers should be evicted instead of accumulating forever."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)
    monkeypatch.setattr("mindroom.knowledge.manager._SCOPED_PRIVATE_MANAGER_CACHE_LIMIT", 2)

    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                memory_backend="file",
                private=AgentPrivateConfig(
                    per="room_thread",
                    root="mind_data",
                    template_dir=str(template_dir),
                    context_files=["SOUL.md"],
                    knowledge=AgentPrivateKnowledgeConfig(path="memory", watch=False),
                ),
            ),
        },
        models={},
    )
    config = bind_runtime_paths(config, _runtime_paths(tmp_path / "config.yaml", tmp_path))
    private_base_id = config.get_agent_private_knowledge_base_id("mind")
    assert private_base_id is not None

    identities = [
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="mind",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=thread_id,
            resolved_thread_id=thread_id,
            session_id=f"session-{thread_id}",
        )
        for thread_id in ("thread-1", "thread-2", "thread-3")
    ]

    try:
        for identity in identities:
            await ensure_agent_knowledge_managers(
                "mind",
                config,
                runtime_paths_for(config),
                execution_identity=identity,
            )

        assert (
            get_knowledge_manager(
                private_base_id,
                config=config,
                runtime_paths=runtime_paths_for(config),
                execution_identity=identities[0],
            )
            is None
        )
        assert (
            get_knowledge_manager(
                private_base_id,
                config=config,
                runtime_paths=runtime_paths_for(config),
                execution_identity=identities[1],
            )
            is not None
        )
        assert (
            get_knowledge_manager(
                private_base_id,
                config=config,
                runtime_paths=runtime_paths_for(config),
                execution_identity=identities[2],
            )
            is not None
        )
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_request_bound_private_manager_survives_cache_eviction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """An in-flight request should keep using the manager it already ensured."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)
    monkeypatch.setattr("mindroom.knowledge.manager._SCOPED_PRIVATE_MANAGER_CACHE_LIMIT", 1)

    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                memory_backend="file",
                private=AgentPrivateConfig(
                    per="room_thread",
                    root="mind_data",
                    template_dir=str(template_dir),
                    context_files=["SOUL.md"],
                    knowledge=AgentPrivateKnowledgeConfig(path="memory", watch=False),
                ),
            ),
        },
        models={},
    )
    config = bind_runtime_paths(config, _runtime_paths(tmp_path / "config.yaml", tmp_path))
    private_base_id = config.get_agent_private_knowledge_base_id("mind")
    assert private_base_id is not None

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="thread-alice",
        resolved_thread_id="thread-alice",
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id="thread-bob",
        resolved_thread_id="thread-bob",
        session_id="session-bob",
    )

    try:
        alice_managers = await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=alice_identity,
        )
        assert private_base_id in alice_managers

        with bound_knowledge_managers(alice_managers):
            await ensure_agent_knowledge_managers(
                "mind",
                config,
                runtime_paths_for(config),
                execution_identity=bob_identity,
            )
            assert (
                get_knowledge_for_base(
                    private_base_id,
                    config=config,
                    runtime_paths=runtime_paths_for(config),
                    execution_identity=alice_identity,
                )
                is not None
            )

        assert (
            get_knowledge_manager(
                private_base_id,
                config=config,
                runtime_paths=runtime_paths_for(config),
                execution_identity=alice_identity,
            )
            is None
        )
    finally:
        await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_sync_git_repository_indexes_files_after_initial_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first git sync should index all tracked files cloned into a fresh workspace."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    def _run_git(*args: str) -> None:
        subprocess.run(list(args), cwd=remote_repo, check=True, capture_output=True, text=True)

    remote_repo = tmp_path / "remote"
    remote_repo.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_run_git, "git", "init", "-b", "main")
    await asyncio.to_thread(_run_git, "git", "config", "user.email", "tests@example.com")
    await asyncio.to_thread(_run_git, "git", "config", "user.name", "MindRoom Tests")
    (remote_repo / "doc.md").write_text("hello", encoding="utf-8")
    await asyncio.to_thread(_run_git, "git", "add", "doc.md")
    await asyncio.to_thread(_run_git, "git", "commit", "-m", "init")

    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(
                path=str(tmp_path / "knowledge"),
                watch=False,
                git=KnowledgeGitConfig(
                    repo_url=str(remote_repo),
                    branch="main",
                    poll_interval_seconds=30,
                ),
            ),
        },
    )

    manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
        storage_path=tmp_path / "storage",
    )

    result = await manager.sync_git_repository()

    assert result == {"updated": True, "changed_count": 1, "removed_count": 0}
    assert manager._indexed_files == {"doc.md"}
    assert _DummyChromaDb.metadatas == [
        {
            "source_path": "doc.md",
            "source_mtime_ns": (manager.knowledge_path / "doc.md").stat().st_mtime_ns,
            "source_size": (manager.knowledge_path / "doc.md").stat().st_size,
        },
    ]


@pytest.mark.asyncio
async def test_sync_git_repository_updates_index_for_changed_and_deleted_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git sync should remove deleted files and upsert changed files."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    async def _sync_once(_git_config: object) -> tuple[set[str], set[str], bool]:
        return {"docs/new.md", "docs/updated.md"}, {"docs/deleted.md"}, True

    monkeypatch.setattr(manager, "_sync_git_repository_once", _sync_once)
    manager.index_file = AsyncMock(return_value=True)
    manager.remove_file = AsyncMock(return_value=True)

    result = await manager.sync_git_repository()

    assert result == {"updated": True, "changed_count": 2, "removed_count": 1}
    manager.remove_file.assert_awaited_once_with("docs/deleted.md")
    manager.index_file.assert_has_awaits(
        [
            call("docs/new.md", upsert=True),
            call("docs/updated.md", upsert=True),
        ],
        any_order=False,
    )


@pytest.mark.asyncio
async def test_run_git_redacts_credentials_in_error_message(
    dummy_manager: KnowledgeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git command errors should not leak embedded URL credentials."""

    class _FailingProcess:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"",
                (
                    b"fatal: unable to access "
                    b"'https://x-access-token:secret-token@github.com/example/private.git/': "
                    b"The requested URL returned error: 403"
                ),
            )

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _FailingProcess:
        _ = args, kwargs
        return _FailingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="Git command failed") as exc_info:
        await dummy_manager._run_git(
            [
                "clone",
                "https://x-access-token:secret-token@github.com/example/private.git",
                "dest",
            ],
        )

    message = str(exc_info.value)
    assert "secret-token" not in message
    assert "x-access-token:***@github.com/example/private.git" in message


@pytest.mark.asyncio
async def test_run_git_cancellation_kills_subprocess(
    dummy_manager: KnowledgeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a git command should terminate and reap the child process."""
    wait_forever = asyncio.Event()

    class _HangingProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.kill_called = False
            self.wait_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await wait_forever.wait()
            return b"", b""

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> int:
            self.wait_called = True
            self.returncode = -9
            return -9

    process = _HangingProcess()

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _HangingProcess:
        _ = args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    task = asyncio.create_task(dummy_manager._run_git(["fetch", "origin", "main"]))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.kill_called is True
    assert process.wait_called is True


def test_list_files_skips_hidden_paths_when_git_skip_hidden_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden files and folders are ignored for git-backed knowledge bases by default."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    (manager.knowledge_path / "public").mkdir(parents=True, exist_ok=True)
    (manager.knowledge_path / "public" / "doc.md").write_text("ok", encoding="utf-8")
    (manager.knowledge_path / ".hidden").mkdir(parents=True, exist_ok=True)
    (manager.knowledge_path / ".hidden" / "secret.md").write_text("skip", encoding="utf-8")
    (manager.knowledge_path / "public" / ".dotfile.md").write_text("skip", encoding="utf-8")

    listed = [path.relative_to(manager.knowledge_path).as_posix() for path in manager.list_files()]
    assert listed == ["public/doc.md"]


def test_list_files_respects_include_and_exclude_patterns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pattern filters should include only requested files and allow explicit exclusions."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(
            tmp_path / "knowledge",
            include_patterns=["content/post/*/index.md"],
            exclude_patterns=["content/post/draft-*/index.md"],
        ),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    (manager.knowledge_path / "content" / "post" / "hello" / "index.md").parent.mkdir(parents=True, exist_ok=True)
    (manager.knowledge_path / "content" / "post" / "hello" / "index.md").write_text("ok", encoding="utf-8")

    (manager.knowledge_path / "content" / "post" / "hello" / "body.md").write_text("skip", encoding="utf-8")
    (manager.knowledge_path / "content" / "post" / "nested" / "slug" / "index.md").parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    (manager.knowledge_path / "content" / "post" / "nested" / "slug" / "index.md").write_text("skip", encoding="utf-8")
    (manager.knowledge_path / "foo" / "content" / "post" / "hello" / "index.md").parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    (manager.knowledge_path / "foo" / "content" / "post" / "hello" / "index.md").write_text("skip", encoding="utf-8")
    (manager.knowledge_path / "content" / "post" / "draft-post" / "index.md").parent.mkdir(parents=True, exist_ok=True)
    (manager.knowledge_path / "content" / "post" / "draft-post" / "index.md").write_text("skip", encoding="utf-8")

    listed = [path.relative_to(manager.knowledge_path).as_posix() for path in manager.list_files()]
    assert listed == ["content/post/hello/index.md"]


@pytest.mark.asyncio
async def test_start_watcher_starts_git_sync_even_when_file_watch_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git polling should run even when filesystem watch is disabled."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    async def fake_git_sync_loop() -> None:
        await manager._git_sync_stop_event.wait()

    monkeypatch.setattr(manager, "_git_sync_loop", fake_git_sync_loop)

    await manager.start_watcher()
    assert manager._watch_task is None
    assert manager._git_sync_task is not None

    await manager.stop_watcher()
    assert manager._git_sync_task is None
