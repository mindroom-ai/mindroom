"""Tests for KnowledgeManager internals."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from agno.knowledge.chunking.fixed import FixedSizeChunking
from agno.knowledge.document.base import Document
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentPrivateKnowledgeConfig
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.knowledge.chunking import SafeFixedSizeChunking
from mindroom.knowledge.manager import (
    _FAILED_SIGNATURE_RETRY_NS,
    _MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES,
    KnowledgeManager,
    _create_embedder,
)
from mindroom.knowledge.shared_managers import (
    _get_shared_knowledge_manager,
    _shared_knowledge_manager_init_lock,
    _shared_knowledge_managers,
    ensure_agent_knowledge_managers,
    ensure_shared_knowledge_manager,
    get_published_shared_knowledge_manager,
    get_shared_knowledge_manager_for_config,
    initialize_shared_knowledge_managers,
    shutdown_shared_knowledge_managers,
)
from mindroom.knowledge.utils import KnowledgeAvailability, _get_knowledge_for_base
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    _private_instance_state_root_path,
    resolve_worker_key,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Callable


class _DummyVectorDb:
    def delete(self) -> bool:
        return True

    def create(self) -> None:
        return None

    def exists(self) -> bool:
        return True


class _DummyCollection:
    def count(self) -> int:
        if _DummyChromaDb.raise_on_count:
            msg = "count() should not be called in this test"
            raise AssertionError(msg)
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
    raise_on_count: ClassVar[bool] = False

    def __init__(self, **_: object) -> None:
        self.collection_name = "mindroom_knowledge"
        self.client = _DummyClient()

    def delete(self) -> bool:
        return True

    def create(self) -> None:
        return None

    def exists(self) -> bool:
        return True


class _ShadowCollection:
    def __init__(self, name: str) -> None:
        self._name = name

    def get(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        include: list[str] | None = None,
        where: dict[str, object] | None = None,
    ) -> dict[str, object]:
        _ = include
        selected_all = list(_ShadowChromaDb.collections.get(self._name, []))
        if where:
            key, value = next(iter(where.items()))
            selected_all = [item for item in selected_all if item["metadata"].get(key) == value]
        selected = selected_all[offset:] if limit is None else selected_all[offset : offset + limit]
        ids = [str(index) for index in range(offset, offset + len(selected))]
        return {"ids": ids, "metadatas": [dict(item["metadata"]) for item in selected]}


class _ShadowClient:
    def get_collection(self, name: str) -> _ShadowCollection:
        return _ShadowCollection(name)


class _ShadowKnowledge:
    def __init__(self, vector_db: _DummyVectorDb) -> None:
        self.vector_db = vector_db

    async def ainsert(
        self,
        *,
        path: str,
        metadata: dict[str, object],
        upsert: bool,
        reader: object | None = None,
    ) -> None:
        _ = (upsert, reader)
        _ShadowChromaDb.collections.setdefault(self.vector_db.collection_name, []).append(
            {
                "content": Path(path).read_text(encoding="utf-8"),
                "metadata": dict(metadata),
            },
        )

    def remove_vectors_by_metadata(self, metadata: dict[str, object]) -> bool:
        collection_name = self.vector_db.collection_name
        existing = _ShadowChromaDb.collections.get(collection_name, [])
        filtered = [
            item for item in existing if not all(item["metadata"].get(key) == value for key, value in metadata.items())
        ]
        _ShadowChromaDb.collections[collection_name] = filtered
        return len(filtered) != len(existing)

    def search(
        self,
        query: str,
        max_results: int | None = None,
        filters: dict[str, object] | list[object] | None = None,
        search_type: str | None = None,
    ) -> list[Document]:
        _ = (query, filters, search_type)
        limit = 5 if max_results is None else max_results
        return [
            Document(content=item["content"], meta_data=dict(item["metadata"]))
            for item in _ShadowChromaDb.collections.get(self.vector_db.collection_name, [])[:limit]
        ]


class _ShadowChromaDb:
    collections: ClassVar[dict[str, list[dict[str, object]]]] = {}

    def __init__(self, *, collection: str, **_: object) -> None:
        self.collection_name = collection
        self.client = _ShadowClient()
        self.collections.setdefault(collection, [])

    def delete(self) -> bool:
        self.collections.pop(self.collection_name, None)
        return True

    def create(self) -> None:
        self.collections[self.collection_name] = []

    def exists(self) -> bool:
        return self.collection_name in self.collections


def _mind_private_agent(
    *,
    watch: bool,
    template_dir: str,
    knowledge_path: str = "memory",
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
                path=knowledge_path,
                watch=watch,
                git=git,
            ),
        ),
    )


def _make_config(
    path: Path,
    *,
    embedder_dimensions: int | None = None,
    include_extensions: list[str] | None = None,
    exclude_extensions: list[str] | None = None,
) -> Config:
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
            "research": KnowledgeBaseConfig(
                path=str(path),
                watch=False,
                include_extensions=include_extensions,
                exclude_extensions=exclude_extensions or [],
            ),
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
    repo_url: str = "https://github.com/example/knowledge.git",
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    lfs: bool = False,
    startup_behavior: str = "blocking",
    sync_timeout_seconds: int = 3600,
) -> Config:
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(
                path=str(path),
                watch=False,
                git=KnowledgeGitConfig(
                    repo_url=repo_url,
                    branch="main",
                    poll_interval_seconds=30,
                    lfs=lfs,
                    startup_behavior=startup_behavior,
                    sync_timeout_seconds=sync_timeout_seconds,
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


@pytest.mark.asyncio
async def test_knowledge_manager_treats_missing_dotted_path_as_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing dotted path should still work once it becomes a directory."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    knowledge_path = tmp_path / "docs.v1"
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(knowledge_path), watch=False),
        },
    )
    manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    assert manager.knowledge_path == knowledge_path.resolve()
    assert manager.knowledge_path.is_dir() is True

    file_path = manager.knowledge_path / "guide.md"
    file_path.write_text("versioned docs", encoding="utf-8")

    assert manager.list_files() == [file_path.resolve()]

    indexed = await manager.index_file(file_path, upsert=True)

    assert indexed is True
    knowledge = manager.get_knowledge()
    assert isinstance(knowledge, _DummyKnowledge)
    metadata = knowledge.insert_calls[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["source_path"] == "guide.md"


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
    assert isinstance(chunking_strategy, SafeFixedSizeChunking)
    assert getattr(chunking_strategy, "chunk_size", None) == 640
    assert getattr(chunking_strategy, "overlap", None) == 32


def test_safe_fixed_size_chunking_avoids_micro_chunk_explosion() -> None:
    """Whitespace backtracking should not degrade into one-character progress."""
    document = Document(
        id="doc-1",
        name="doc",
        content=("a " + "x" * 30 + " ") * 4,
        meta_data={},
    )

    original_chunks = FixedSizeChunking(chunk_size=20, overlap=5).chunk(document)
    safe_chunks = SafeFixedSizeChunking(chunk_size=20, overlap=5).chunk(document)

    assert any(len(chunk.content) <= 5 for chunk in original_chunks)
    assert len(safe_chunks) < len(original_chunks)
    assert all(len(chunk.content) >= 10 for chunk in safe_chunks[:-1])


def test_knowledge_base_chunk_overlap_must_be_smaller_than_chunk_size() -> None:
    """KnowledgeBaseConfig should reject overlap >= size."""
    with pytest.raises(ValidationError, match="chunk_overlap must be smaller than chunk_size"):
        KnowledgeBaseConfig(path="./docs", chunk_size=500, chunk_overlap=500)


def test_get_status_includes_git_sync_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status should expose Git sync metadata for git-backed knowledge bases."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge", startup_behavior="background", lfs=True),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )
    manager._git_last_error = "sync failed"

    status = manager.get_status()

    assert status["git"]["lfs"] is True
    assert status["git"]["startup_behavior"] == "background"
    assert status["git"]["last_error"] == "sync failed"
    assert status["git"]["repo_present"] is False


def test_get_shared_knowledge_manager_for_config_misses_when_git_runtime_settings_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared manager cache lookups should miss when startup/runtime git settings drift."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config_a = _make_git_config(
        tmp_path / "knowledge",
        startup_behavior="blocking",
        sync_timeout_seconds=3600,
    )
    config_b = _make_git_config(
        tmp_path / "knowledge",
        startup_behavior="background",
        sync_timeout_seconds=900,
    )
    manager = KnowledgeManager(
        base_id="research",
        config=config_a,
        runtime_paths=runtime_paths_for(config_a),
    )

    resolved = get_shared_knowledge_manager_for_config(
        "research",
        config=config_b,
        runtime_paths=runtime_paths_for(config_b),
        candidate_manager=manager,
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_get_published_shared_knowledge_manager_returns_in_under_10ms_with_init_lock_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published shared-manager lookups should not wait for the init lock."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_config(tmp_path / "docs")
    managers = await initialize_shared_knowledge_managers(config, runtime_paths_for(config), reindex_on_create=False)

    try:
        lock = _shared_knowledge_manager_init_lock("research")
        await lock.acquire()
        start = time.perf_counter()
        manager = get_published_shared_knowledge_manager("research")
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert manager is managers["research"]
        assert elapsed_ms < 10
    finally:
        if lock.locked():
            lock.release()
        await shutdown_shared_knowledge_managers()


def test_get_published_shared_knowledge_manager_returns_none_before_init() -> None:
    """Published shared-manager lookups should miss before any shared manager is initialized."""
    assert get_published_shared_knowledge_manager("missing") is None


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
async def test_load_indexed_files_does_not_use_collection_count(dummy_manager: KnowledgeManager) -> None:
    """Loading indexed files should page via get() without relying on collection.count()."""
    _DummyChromaDb.metadatas = [
        {"source_path": "docs/a.txt"},
        {"source_path": "notes/b.md"},
    ]
    _DummyChromaDb.raise_on_count = True

    try:
        indexed_count = await dummy_manager.load_indexed_files()
    finally:
        _DummyChromaDb.raise_on_count = False

    assert indexed_count == 2


@pytest.mark.asyncio
async def test_reindex_all_uses_bounded_file_concurrency(dummy_manager: KnowledgeManager) -> None:
    """Full reindex should process multiple files concurrently up to the configured bound."""
    for index in range(3):
        (dummy_manager.knowledge_path / f"doc-{index}.md").write_text(f"doc {index}", encoding="utf-8")

    active = 0
    max_active = 0

    async def _fake_index(
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        _ = (knowledge, indexed_files, indexed_signatures)
        nonlocal active, max_active
        assert upsert is True
        assert resolved_path.is_file()
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return True

    dummy_manager._index_file_locked = _fake_index

    indexed_count = await dummy_manager.reindex_all()

    assert indexed_count == 3
    assert max_active == min(3, _MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES)


@pytest.mark.asyncio
async def test_search_during_reindex_returns_results_against_old_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live reads should stay on the previously published snapshot until swap completion."""
    _ShadowChromaDb.collections = {}
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _ShadowChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _ShadowKnowledge)

    docs_path = tmp_path / "knowledge"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "doc-a.md").write_text("old snapshot", encoding="utf-8")
    config = _make_config(docs_path)
    manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    await manager.reindex_all()

    (docs_path / "doc-a.md").write_text("new snapshot", encoding="utf-8")
    (docs_path / "doc-b.md").write_text("new sibling", encoding="utf-8")

    started_shadow_build = asyncio.Event()
    release_shadow_build = asyncio.Event()
    original_index_file_locked = manager._index_file_locked

    async def _block_shadow_build(
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        if knowledge is not None and knowledge is not manager.get_knowledge() and not started_shadow_build.is_set():
            started_shadow_build.set()
            await release_shadow_build.wait()
        return await original_index_file_locked(
            resolved_path,
            upsert=upsert,
            knowledge=knowledge,
            indexed_files=indexed_files,
            indexed_signatures=indexed_signatures,
        )

    monkeypatch.setattr(manager, "_index_file_locked", _block_shadow_build)

    reindex_task = asyncio.create_task(manager.reindex_all())
    await started_shadow_build.wait()

    live_results = manager.get_knowledge().search("snapshot", max_results=10)
    assert [document.content for document in live_results] == ["old snapshot"]

    release_shadow_build.set()
    indexed_count = await reindex_task

    assert indexed_count == 2
    live_results = manager.get_knowledge().search("snapshot", max_results=10)
    assert {document.content for document in live_results} == {"new snapshot", "new sibling"}


@pytest.mark.asyncio
async def test_reindex_all_failure_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed full rebuild should keep serving the last published snapshot."""
    _ShadowChromaDb.collections = {}
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _ShadowChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _ShadowKnowledge)

    docs_path = tmp_path / "knowledge"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "doc-a.md").write_text("stable snapshot", encoding="utf-8")
    config = _make_config(docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths,
    )
    await manager.reindex_all()
    live_collection_name = manager._current_collection_name()

    (docs_path / "doc-a.md").write_text("broken refresh", encoding="utf-8")
    original_index_file_locked = manager._index_file_locked
    failure_message = "shadow rebuild failed"

    async def _fail_shadow_build(
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        if knowledge is not None and knowledge is not manager.get_knowledge():
            await original_index_file_locked(
                resolved_path,
                upsert=upsert,
                knowledge=knowledge,
                indexed_files=indexed_files,
                indexed_signatures=indexed_signatures,
            )
            raise RuntimeError(failure_message)
        return await original_index_file_locked(
            resolved_path,
            upsert=upsert,
            knowledge=knowledge,
            indexed_files=indexed_files,
            indexed_signatures=indexed_signatures,
        )

    monkeypatch.setattr(manager, "_index_file_locked", _fail_shadow_build)

    with pytest.raises(RuntimeError, match=failure_message):
        await manager.reindex_all()

    live_results = manager.get_knowledge().search("snapshot", max_results=10)
    assert [document.content for document in live_results] == ["stable snapshot"]

    payload = json.loads(manager._indexing_settings_path.read_text(encoding="utf-8"))
    assert payload["status"] == "complete"
    assert payload["collection"] == live_collection_name
    assert payload["availability"] == "refresh_failed"

    availability: list[KnowledgeAvailability] = []
    knowledge = _get_knowledge_for_base(
        "research",
        config=config,
        runtime_paths=runtime_paths,
        shared_manager_lookup=lambda _base_id: manager,
        on_availability=availability.append,
    )
    assert knowledge is manager.get_knowledge()
    assert availability == [KnowledgeAvailability.REFRESH_FAILED]


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
async def test_initialize_shared_knowledge_managers_resumes_partial_index_when_checkpoint_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup should resume a partial legacy index instead of wiping it when no checkpoint exists."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "a.md").write_text("A\n", encoding="utf-8")
    (docs_path / "b.md").write_text("B\n", encoding="utf-8")
    _DummyChromaDb.metadatas = [{"source_path": "a.md"}]

    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage")
    config = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"research": KnowledgeBaseConfig(path=str(docs_path), watch=False)},
        ),
        runtime_paths,
    )
    reset_collection = MagicMock(side_effect=AssertionError("partial index resume must not reset the collection"))
    monkeypatch.setattr(KnowledgeManager, "_reset_collection", reset_collection)

    try:
        managers = await initialize_shared_knowledge_managers(config, runtime_paths, reindex_on_create=False)
        manager = managers["research"]

        assert manager._indexed_files == {"a.md", "b.md"}
        assert {metadata["source_path"] for metadata in _DummyChromaDb.metadatas if isinstance(metadata, dict)} == {
            "a.md",
            "b.md",
        }
        payload = json.loads(manager._indexing_settings_path.read_text(encoding="utf-8"))
        assert payload["settings"] == list(manager._indexing_settings)
        assert payload["status"] == "complete"
        assert payload["collection"] == manager._current_collection_name()
        assert payload["availability"] == "ready"
        assert isinstance(payload["last_published_at"], str)
        reset_collection.assert_not_called()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_full_reindexes_when_checkpoint_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt checkpoint with existing vectors must force a safe full rebuild."""
    _DummyChromaDb.metadatas = [{"source_path": "a.md"}]
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "a.md").write_text("A\n", encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage")
    config = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"research": KnowledgeBaseConfig(path=str(docs_path), watch=False)},
        ),
        runtime_paths,
    )
    seed_manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths,
    )
    seed_manager._indexing_settings_path.write_text("{not-json", encoding="utf-8")

    initialize = AsyncMock()
    sync_indexed_files = AsyncMock(return_value={"loaded_count": 1, "indexed_count": 0, "removed_count": 0})
    monkeypatch.setattr(KnowledgeManager, "initialize", initialize)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)

    try:
        await initialize_shared_knowledge_managers(config, runtime_paths, reindex_on_create=False)

        initialize.assert_awaited_once()
        sync_indexed_files.assert_not_awaited()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_maintains_registry(
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

    managers = await initialize_shared_knowledge_managers(config, runtime_paths, reindex_on_create=False)
    assert set(managers) == {"research", "legal"}
    assert _get_shared_knowledge_manager("research") is managers["research"]

    updated_config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "research"), watch=False),
        },
    )

    managers = await initialize_shared_knowledge_managers(updated_config, runtime_paths, reindex_on_create=False)
    assert set(managers) == {"research"}
    assert _get_shared_knowledge_manager("legal") is None

    await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_full_reindex_on_settings_change(
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

    managers = await initialize_shared_knowledge_managers(config, runtime_paths, reindex_on_create=False)
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

    managers = await initialize_shared_knowledge_managers(updated_config, runtime_paths, reindex_on_create=False)
    new_manager = managers["research"]
    assert new_manager is not original_manager
    initialize.assert_awaited_once()
    sync_indexed_files.assert_not_awaited()

    await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_non_index_setting_change_reuses_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-index settings should reuse the published shared manager without on-access sync."""
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

    managers = await initialize_shared_knowledge_managers(config, runtime_paths, reindex_on_create=False)
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

    managers = await initialize_shared_knowledge_managers(updated_config, runtime_paths, reindex_on_create=False)
    new_manager = managers["research"]
    assert new_manager is original_manager
    initialize.assert_not_awaited()
    sync_indexed_files.assert_not_awaited()

    await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_full_reindex_on_git_lfs_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching Git LFS mode must rebuild the index because file contents can change."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "research", lfs=False)
    runtime_paths = runtime_paths_for(config)

    initial_sync_git_repository = AsyncMock(return_value={"updated": False, "changed_count": 0, "removed_count": 0})
    initial_sync_indexed_files = AsyncMock(return_value={"loaded_count": 0, "indexed_count": 0, "removed_count": 0})
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", initial_sync_git_repository)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", initial_sync_indexed_files)

    managers = await initialize_shared_knowledge_managers(config, runtime_paths, reindex_on_create=False)
    original_manager = managers["research"]

    updated_config = _make_git_config(tmp_path / "research", lfs=True)

    initialize = AsyncMock()
    sync_indexed_files = AsyncMock(return_value={"loaded_count": 0, "indexed_count": 0, "removed_count": 0})
    monkeypatch.setattr(KnowledgeManager, "initialize", initialize)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)

    managers = await initialize_shared_knowledge_managers(updated_config, runtime_paths, reindex_on_create=False)
    new_manager = managers["research"]

    assert new_manager is not original_manager
    initialize.assert_awaited_once()
    sync_indexed_files.assert_not_awaited()

    await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_private_knowledge_managers_copy_template_and_isolate_private_instance_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Private knowledge should copy the configured template into canonical requester-scoped roots."""
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
        alice_managers = await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=alice_identity,
        )
        bob_managers = await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=bob_identity,
        )

        assert _get_shared_knowledge_manager(private_base_id) is None

        alice_manager = alice_managers[private_base_id]
        bob_manager = bob_managers[private_base_id]

        assert alice_manager is not None
        assert bob_manager is not None
        assert alice_manager is not bob_manager

        alice_worker_key = resolve_worker_key("user", alice_identity)
        bob_worker_key = resolve_worker_key("user", bob_identity)
        assert alice_worker_key is not None
        assert bob_worker_key is not None

        alice_workspace = (
            _private_instance_state_root_path(
                tmp_path,
                worker_key=alice_worker_key,
                agent_name="mind",
            )
            / "mind_data"
        )
        bob_workspace = (
            _private_instance_state_root_path(
                tmp_path,
                worker_key=bob_worker_key,
                agent_name="mind",
            )
            / "mind_data"
        )
        assert alice_manager.knowledge_path == (alice_workspace / "memory").resolve()
        assert bob_manager.knowledge_path == (bob_workspace / "memory").resolve()
        assert (alice_workspace / "SOUL.md").exists()
        assert (alice_workspace / "MEMORY.md").exists()
        assert (bob_workspace / "SOUL.md").exists()
        assert (bob_workspace / "MEMORY.md").exists()
    finally:
        await shutdown_shared_knowledge_managers()


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
        managers = await initialize_shared_knowledge_managers(config, runtime_paths_for(config))
        manager = managers["docs"]

        assert docs_path.parent.is_dir()
        assert manager.knowledge_path == docs_path.resolve()
        assert docs_path.is_dir()
        assert manager.list_files() == []

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
        await shutdown_shared_knowledge_managers()


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
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
@pytest.mark.parametrize("watch", [True, False])
async def test_worker_scoped_git_private_knowledge_refreshes_on_access_without_background_watchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
    watch: bool,
) -> None:
    """Worker-scoped Git knowledge should refresh on access instead of starting git polling tasks."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": _mind_private_agent(
                watch=watch,
                template_dir=str(template_dir),
                knowledge_path="kb_repo",
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
    sync_indexed_files = AsyncMock(return_value={"loaded_count": 0, "indexed_count": 0, "removed_count": 0})
    start_watcher = AsyncMock()
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)
    monkeypatch.setattr(KnowledgeManager, "start_watcher", start_watcher)

    try:
        await ensure_agent_knowledge_managers("mind", config, runtime_paths_for(config), execution_identity=identity)
        await ensure_agent_knowledge_managers("mind", config, runtime_paths_for(config), execution_identity=identity)

        assert sync_git_repository.await_count == 2
        sync_git_repository.assert_has_awaits(
            [
                call(index_changes=False),
                call(index_changes=False),
            ],
            any_order=False,
        )
        assert sync_indexed_files.await_count == 2
        start_watcher.assert_not_awaited()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_worker_scoped_git_private_knowledge_ignores_background_startup_without_watchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Request-scoped Git knowledge must refresh on access because no background loop is started."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": _mind_private_agent(
                watch=False,
                template_dir=str(template_dir),
                knowledge_path="kb_repo",
                git=KnowledgeGitConfig(
                    repo_url="https://github.com/example/memory.git",
                    branch="main",
                    poll_interval_seconds=30,
                    startup_behavior="background",
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
    sync_indexed_files = AsyncMock(return_value={"loaded_count": 0, "indexed_count": 0, "removed_count": 0})
    prepare_background_git_startup = AsyncMock()
    start_watcher = AsyncMock()
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)
    monkeypatch.setattr(KnowledgeManager, "prepare_background_git_startup", prepare_background_git_startup)
    monkeypatch.setattr(KnowledgeManager, "start_watcher", start_watcher)

    try:
        await ensure_agent_knowledge_managers("mind", config, runtime_paths_for(config), execution_identity=identity)
    finally:
        await shutdown_shared_knowledge_managers()

    sync_git_repository.assert_awaited_once_with(index_changes=False)
    sync_indexed_files.assert_awaited_once_with()
    prepare_background_git_startup.assert_not_awaited()
    start_watcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_scoped_git_private_knowledge_full_reindex_still_syncs_before_reindex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Request-scoped background Git bases must still refresh before a full reindex."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": _mind_private_agent(
                watch=False,
                template_dir=str(template_dir),
                knowledge_path="kb_repo",
                git=KnowledgeGitConfig(
                    repo_url="https://github.com/example/memory.git",
                    branch="main",
                    poll_interval_seconds=30,
                    startup_behavior="background",
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
    reindex_all = AsyncMock(return_value=0)
    prepare_background_git_startup = AsyncMock()
    start_watcher = AsyncMock()
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", reindex_all)
    monkeypatch.setattr(KnowledgeManager, "prepare_background_git_startup", prepare_background_git_startup)
    monkeypatch.setattr(KnowledgeManager, "start_watcher", start_watcher)

    try:
        await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=identity,
            reindex_on_create=True,
        )
    finally:
        await shutdown_shared_knowledge_managers()

    sync_git_repository.assert_awaited_once_with(index_changes=False)
    reindex_all.assert_awaited_once_with()
    prepare_background_git_startup.assert_not_awaited()
    start_watcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialize_shared_git_knowledge_starts_background_sync_when_watch_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared Git knowledge should still start background sync when file watch is disabled."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge")
    runtime_paths = runtime_paths_for(config)
    start_watcher = AsyncMock()
    sync_git_repository = AsyncMock(return_value={"updated": False, "changed_count": 0, "removed_count": 0})
    sync_indexed_files = AsyncMock(return_value={"loaded_count": 0, "indexed_count": 0, "removed_count": 0})
    monkeypatch.setattr(KnowledgeManager, "start_watcher", start_watcher)
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)

    try:
        await initialize_shared_knowledge_managers(
            config,
            runtime_paths,
            start_watchers=True,
            reindex_on_create=False,
        )

        start_watcher.assert_awaited_once()
        sync_git_repository.assert_awaited_once_with(index_changes=False)
        sync_indexed_files.assert_awaited_once()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_private_request_knowledge_managers_are_not_registered_globally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Request-scoped private knowledge should stay out of the shared registry."""
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

        resolved_manager = _get_shared_knowledge_manager(private_base_id)
        resolved_for_config = get_shared_knowledge_manager_for_config(
            private_base_id,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

        assert resolved_manager is None
        assert resolved_for_config is None
        assert scoped_manager is managers[private_base_id]
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_get_knowledge_for_base_reuses_published_shared_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared-base lookups should use the published shared manager."""
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
        managers = await initialize_shared_knowledge_managers(config, runtime_paths_for(config))
        manager = managers["docs"]
        knowledge = _get_knowledge_for_base("docs", config=config, runtime_paths=runtime_paths_for(config))

        assert knowledge is manager.get_knowledge()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_get_knowledge_for_base_serves_last_good_snapshot_on_config_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared lookups should keep serving the published snapshot during config drift."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir(parents=True, exist_ok=True)
    docs_b.mkdir(parents=True, exist_ok=True)
    (docs_a / "guide.md").write_text("Shared docs A.\n", encoding="utf-8")
    (docs_b / "guide.md").write_text("Shared docs B.\n", encoding="utf-8")

    config_a = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_a), watch=False)},
        ),
        _runtime_paths(tmp_path / "config-a.yaml", tmp_path / "storage-a"),
    )
    config_b = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_b), watch=False)},
        ),
        _runtime_paths(tmp_path / "config-b.yaml", tmp_path / "storage-b"),
    )

    try:
        managers = await initialize_shared_knowledge_managers(config_a, runtime_paths_for(config_a))
        availability: list[KnowledgeAvailability] = []

        knowledge = _get_knowledge_for_base(
            "docs",
            config=config_b,
            runtime_paths=runtime_paths_for(config_b),
            on_availability=availability.append,
        )

        assert knowledge is managers["docs"].get_knowledge()
        assert availability == [KnowledgeAvailability.CONFIG_MISMATCH]
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_get_knowledge_for_base_treats_stale_lookup_as_cache_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale shared-manager candidate should fall through to the current shared manager."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir(parents=True, exist_ok=True)
    docs_b.mkdir(parents=True, exist_ok=True)
    (docs_a / "guide.md").write_text("Shared docs A.\n", encoding="utf-8")
    (docs_b / "guide.md").write_text("Shared docs B.\n", encoding="utf-8")

    config_a = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_a), watch=False)},
        ),
        _runtime_paths(tmp_path / "config-a.yaml", tmp_path / "storage-a"),
    )
    config_b = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_b), watch=False)},
        ),
        _runtime_paths(tmp_path / "config-b.yaml", tmp_path / "storage-b"),
    )
    stale_manager = KnowledgeManager(
        base_id="docs",
        config=config_a,
        runtime_paths=runtime_paths_for(config_a),
        storage_path=runtime_paths_for(config_a).storage_root,
        knowledge_path=docs_a.resolve(),
    )

    try:
        managers = await initialize_shared_knowledge_managers(config_b, runtime_paths_for(config_b))

        knowledge = _get_knowledge_for_base(
            "docs",
            config=config_b,
            runtime_paths=runtime_paths_for(config_b),
            shared_manager_lookup=lambda base_id: stale_manager if base_id == "docs" else None,
        )

        assert knowledge is managers["docs"].get_knowledge()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_refreshes_runtime_paths_on_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reused managers should adopt the latest runtime paths on config/runtime refresh."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "guide.md").write_text("Shared docs.\n", encoding="utf-8")
    config_a = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), watch=False)},
        ),
        _runtime_paths(tmp_path / "cfg-a" / "config.yaml", tmp_path / "storage"),
    )
    config_b = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), watch=False)},
        ),
        _runtime_paths(tmp_path / "cfg-b" / "config.yaml", tmp_path / "storage"),
    )

    try:
        managers_a = await initialize_shared_knowledge_managers(
            config_a,
            runtime_paths_for(config_a),
            reindex_on_create=False,
        )
        managers_b = await initialize_shared_knowledge_managers(
            config_b,
            runtime_paths_for(config_b),
            reindex_on_create=False,
        )

        assert managers_a["docs"] is managers_b["docs"]
        assert managers_b["docs"].runtime_paths.env_path == runtime_paths_for(config_b).env_path
        assert managers_b["docs"].runtime_paths.config_path == runtime_paths_for(config_b).config_path
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_full_reindex_on_cold_settings_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold shared manager recreation should still full-reindex after index-affecting drift."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_path = tmp_path / "research"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "guide.md").write_text("Shared docs.\n", encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage")
    config_a = Config(
        agents={},
        models={},
        knowledge_bases={"research": KnowledgeBaseConfig(path=str(docs_path), watch=False)},
    )
    config_b = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(
                path=str(docs_path),
                watch=False,
                chunk_size=1234,
            ),
        },
    )

    try:
        await initialize_shared_knowledge_managers(config_a, runtime_paths, reindex_on_create=False)
        await shutdown_shared_knowledge_managers()

        initialize = AsyncMock()
        sync_indexed_files = AsyncMock(return_value={"loaded_count": 0, "indexed_count": 0, "removed_count": 0})
        monkeypatch.setattr(KnowledgeManager, "initialize", initialize)
        monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)

        await initialize_shared_knowledge_managers(config_b, runtime_paths, reindex_on_create=False)

        initialize.assert_awaited_once()
        sync_indexed_files.assert_not_awaited()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_interrupted_reindex_restart_resumes_partial_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restarted process should continue from partial reindex progress instead of wiping it."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)
    monkeypatch.setattr("mindroom.knowledge.manager._MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES", 1)

    def _clear_dummy_collection(_vector_db: _DummyChromaDb) -> None:
        _DummyChromaDb.metadatas.clear()

    monkeypatch.setattr(_DummyChromaDb, "delete", _clear_dummy_collection)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "a.md").write_text("A\n", encoding="utf-8")
    (docs_path / "b.md").write_text("B\n", encoding="utf-8")
    config = _make_config(docs_path)
    runtime_paths = runtime_paths_for(config)

    interrupted_manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths,
    )
    original_index_file_locked = interrupted_manager._index_file_locked
    indexed_paths: list[str] = []

    async def _interrupt_after_first_file(
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        indexed = await original_index_file_locked(
            resolved_path,
            upsert=upsert,
            knowledge=knowledge,
            indexed_files=indexed_files,
            indexed_signatures=indexed_signatures,
        )
        indexed_paths.append(interrupted_manager._relative_path(resolved_path))
        if len(indexed_paths) == 1:
            message = "simulated crash"
            raise RuntimeError(message)
        return indexed

    monkeypatch.setattr(interrupted_manager, "_index_file_locked", _interrupt_after_first_file)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await interrupted_manager.reindex_all()

    interrupted_payload = json.loads(interrupted_manager._indexing_settings_path.read_text(encoding="utf-8"))
    assert interrupted_payload["settings"] == list(interrupted_manager._indexing_settings)
    assert interrupted_payload["status"] == "indexing"
    assert interrupted_payload["collection"] == interrupted_manager._current_collection_name()
    assert interrupted_payload["availability"] == "initializing"
    assert {metadata["source_path"] for metadata in _DummyChromaDb.metadatas if isinstance(metadata, dict)} == {
        indexed_paths[0],
    }

    reset_collection = MagicMock(side_effect=AssertionError("restart resume must not reset the collection"))
    monkeypatch.setattr(KnowledgeManager, "_reset_collection", reset_collection)

    try:
        managers = await initialize_shared_knowledge_managers(config, runtime_paths, reindex_on_create=False)
        resumed_manager = managers["research"]

        assert resumed_manager._indexed_files == {"a.md", "b.md"}
        assert {metadata["source_path"] for metadata in _DummyChromaDb.metadatas if isinstance(metadata, dict)} == {
            "a.md",
            "b.md",
        }
        resumed_payload = json.loads(resumed_manager._indexing_settings_path.read_text(encoding="utf-8"))
        assert resumed_payload["settings"] == list(resumed_manager._indexing_settings)
        assert resumed_payload["status"] == "complete"
        assert resumed_payload["collection"] == resumed_manager._current_collection_name()
        assert resumed_payload["availability"] == "ready"
        assert isinstance(resumed_payload["last_published_at"], str)
        reset_collection.assert_not_called()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_refreshes_shared_managers_on_reuse_without_watchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared managers should stay pure-read on reuse when callers intentionally disable watchers."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "guide.md").write_text("Shared docs.\n", encoding="utf-8")
    config = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), watch=True)},
        ),
        _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    try:
        managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths_for(config),
            start_watchers=False,
            reindex_on_create=False,
        )
        sync_mock = AsyncMock(return_value={"added": 0, "updated": 0, "removed": 0})
        monkeypatch.setattr(managers["docs"], "sync_indexed_files", sync_mock)

        await initialize_shared_knowledge_managers(
            config,
            runtime_paths_for(config),
            start_watchers=False,
            reindex_on_create=False,
        )

        sync_mock.assert_not_awaited()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_preserves_existing_watchers_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later non-owner callers should not tear down an active shared-manager watcher by default."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    async def _watch_loop_until_stopped(manager: KnowledgeManager) -> None:
        await manager._watch_stop_event.wait()

    monkeypatch.setattr(KnowledgeManager, "_watch_loop", _watch_loop_until_stopped)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "guide.md").write_text("Shared docs.\n", encoding="utf-8")
    config = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), watch=True)},
        ),
        _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    try:
        managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths_for(config),
            start_watchers=True,
            reindex_on_create=False,
        )
        manager = managers["docs"]
        original_stop_watcher = manager.stop_watcher
        stop_watcher = AsyncMock(side_effect=original_stop_watcher)
        monkeypatch.setattr(manager, "stop_watcher", stop_watcher)

        reused_managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths_for(config),
            start_watchers=False,
            reindex_on_create=False,
        )

        assert reused_managers["docs"] is manager
        stop_watcher.assert_not_awaited()
        assert manager._watch_task is not None
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_runtime_owner_can_disable_existing_watchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime owner should still be able to reconcile shared managers down to on-access mode."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    async def _watch_loop_until_stopped(manager: KnowledgeManager) -> None:
        await manager._watch_stop_event.wait()

    monkeypatch.setattr(KnowledgeManager, "_watch_loop", _watch_loop_until_stopped)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "guide.md").write_text("Shared docs.\n", encoding="utf-8")
    config = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), watch=True)},
        ),
        _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    try:
        managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths_for(config),
            start_watchers=True,
            reindex_on_create=False,
            reconcile_existing_runtime=True,
        )
        manager = managers["docs"]
        original_stop_watcher = manager.stop_watcher
        stop_watcher = AsyncMock(side_effect=original_stop_watcher)
        monkeypatch.setattr(manager, "stop_watcher", stop_watcher)

        reused_managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths_for(config),
            start_watchers=False,
            reindex_on_create=False,
            reconcile_existing_runtime=True,
        )

        assert reused_managers["docs"] is manager
        stop_watcher.assert_awaited_once_with()
        assert manager._watch_task is None
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_refreshes_when_previous_watcher_task_is_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finished watcher tasks should not trigger on-access refresh for later non-owner callers."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    async def _watch_loop_returns_immediately(_manager: KnowledgeManager) -> None:
        return None

    monkeypatch.setattr(KnowledgeManager, "_watch_loop", _watch_loop_returns_immediately)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "guide.md").write_text("Shared docs.\n", encoding="utf-8")
    config = bind_runtime_paths(
        Config(
            agents={},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), watch=True)},
        ),
        _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    try:
        managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths_for(config),
            start_watchers=True,
            reindex_on_create=False,
            reconcile_existing_runtime=True,
        )
        manager = managers["docs"]
        await asyncio.sleep(0)
        assert manager._watch_task is not None
        assert manager._watch_task.done() is True

        sync_indexed_files = AsyncMock(return_value={"loaded_count": 1, "indexed_count": 0, "removed_count": 0})
        monkeypatch.setattr(manager, "sync_indexed_files", sync_indexed_files)

        reused_managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths_for(config),
            start_watchers=False,
            reindex_on_create=False,
        )

        assert reused_managers["docs"] is manager
        sync_indexed_files.assert_not_awaited()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_removes_stale_shared_manager_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared-manager initialization should discard superseded keys after path changes."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir(parents=True, exist_ok=True)
    docs_b.mkdir(parents=True, exist_ok=True)
    (docs_a / "guide.md").write_text("Docs A.\n", encoding="utf-8")
    (docs_b / "guide.md").write_text("Docs B.\n", encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path)
    config_a = bind_runtime_paths(
        Config(
            agents={"researcher": AgentConfig(display_name="Researcher", knowledge_bases=["docs"])},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_a), watch=False)},
        ),
        runtime_paths,
    )
    config_b = bind_runtime_paths(
        Config(
            agents={"researcher": AgentConfig(display_name="Researcher", knowledge_bases=["docs"])},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_b), watch=False)},
        ),
        runtime_paths,
    )

    try:
        first = await initialize_shared_knowledge_managers(config_a, runtime_paths_for(config_a))
        second = await initialize_shared_knowledge_managers(config_b, runtime_paths_for(config_b))

        assert (
            get_shared_knowledge_manager_for_config(
                "docs",
                config=config_a,
                runtime_paths=runtime_paths_for(config_a),
            )
            is None
        )
        assert (
            get_shared_knowledge_manager_for_config(
                "docs",
                config=config_b,
                runtime_paths=runtime_paths_for(config_b),
            )
            is second["docs"]
        )
        assert first["docs"] is not second["docs"]
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_request_scoped_knowledge_manager_initialization_serializes_per_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Concurrent request-scoped ensures should serialize creation for the same binding."""
    template_dir = build_private_template_dir()
    config = bind_runtime_paths(
        Config(
            agents={"mind": _mind_private_agent(watch=False, template_dir=str(template_dir))},
            models={},
        ),
        _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    active = 0
    max_active = 0
    calls = 0

    async def fake_create(**_: object) -> KnowledgeManager:
        nonlocal active, max_active, calls
        calls += 1
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return MagicMock(spec=KnowledgeManager)

    monkeypatch.setattr("mindroom.knowledge.shared_managers._create_knowledge_manager_for_target", fake_create)

    await asyncio.gather(
        ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=identity,
        ),
        ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=identity,
        ),
    )

    assert calls == 2
    assert max_active == 1


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_replaces_stale_shared_key_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent shared-manager replacement should not leave the stale shared manager alive."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir(parents=True, exist_ok=True)
    docs_b.mkdir(parents=True, exist_ok=True)
    (docs_a / "guide.md").write_text("Docs A.\n", encoding="utf-8")
    (docs_b / "guide.md").write_text("Docs B.\n", encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path)
    config_a = bind_runtime_paths(
        Config(
            agents={"researcher": AgentConfig(display_name="Researcher", knowledge_bases=["docs"])},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_a), watch=False)},
        ),
        runtime_paths,
    )
    config_b = bind_runtime_paths(
        Config(
            agents={"researcher": AgentConfig(display_name="Researcher", knowledge_bases=["docs"])},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_b), watch=False)},
        ),
        runtime_paths,
    )
    old_started = asyncio.Event()
    release_old = asyncio.Event()

    async def fake_initialize(manager: KnowledgeManager) -> None:
        if manager.knowledge_path == docs_a.resolve():
            old_started.set()
            await release_old.wait()

    try:
        with patch.object(KnowledgeManager, "initialize", new=fake_initialize):
            old_task = asyncio.create_task(
                initialize_shared_knowledge_managers(
                    config_a,
                    runtime_paths_for(config_a),
                    reindex_on_create=True,
                ),
            )
            await old_started.wait()
            new_task = asyncio.create_task(
                initialize_shared_knowledge_managers(
                    config_b,
                    runtime_paths_for(config_b),
                    reindex_on_create=True,
                ),
            )
            await asyncio.sleep(0)
            release_old.set()
            old_result, new_result = await asyncio.gather(old_task, new_task)

        new_manager = new_result["docs"]
        assert old_result["docs"] is not new_manager
        assert _shared_knowledge_managers == {"docs": new_manager}
        assert (
            get_shared_knowledge_manager_for_config(
                "docs",
                config=config_a,
                runtime_paths=runtime_paths_for(config_a),
            )
            is None
        )
        assert (
            get_shared_knowledge_manager_for_config(
                "docs",
                config=config_b,
                runtime_paths=runtime_paths_for(config_b),
            )
            is new_manager
        )
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_ensure_agent_knowledge_managers_skips_shared_bases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request-path ensures should ignore shared knowledge bases."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    docs_path = tmp_path / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "guide.md").write_text("Shared docs.\n", encoding="utf-8")
    config = bind_runtime_paths(
        Config(
            agents={"researcher": AgentConfig(display_name="Researcher", knowledge_bases=["docs"])},
            models={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), watch=False)},
        ),
        _runtime_paths(tmp_path / "config.yaml", tmp_path),
    )

    try:
        managers = await ensure_agent_knowledge_managers("researcher", config, runtime_paths_for(config))

        assert managers == {}
        assert _get_shared_knowledge_manager("docs") is None
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_recreated_request_knowledge_managers_full_reindex_on_settings_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Fresh request-scoped managers should still full-reindex after index-affecting drift."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage")
    config_a = bind_runtime_paths(
        Config(
            agents={"mind": _mind_private_agent(watch=False, template_dir=str(template_dir))},
            models={},
        ),
        runtime_paths,
    )
    config_b = bind_runtime_paths(
        Config(
            agents={
                "mind": AgentConfig(
                    display_name="Mind",
                    memory_backend="file",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        template_dir=str(template_dir),
                        context_files=["SOUL.md"],
                        knowledge=AgentPrivateKnowledgeConfig(
                            path="memory",
                            watch=False,
                            chunk_size=1234,
                        ),
                    ),
                ),
            },
            models={},
        ),
        runtime_paths,
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )

    try:
        await ensure_agent_knowledge_managers(
            "mind",
            config_a,
            runtime_paths_for(config_a),
            execution_identity=identity,
            reindex_on_create=False,
        )

        initialize = AsyncMock()
        sync_indexed_files = AsyncMock(return_value={"loaded_count": 0, "indexed_count": 0, "removed_count": 0})
        monkeypatch.setattr(KnowledgeManager, "initialize", initialize)
        monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", sync_indexed_files)

        await ensure_agent_knowledge_managers(
            "mind",
            config_b,
            runtime_paths_for(config_b),
            execution_identity=identity,
            reindex_on_create=False,
        )

        initialize.assert_awaited_once()
        sync_indexed_files.assert_not_awaited()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_private_request_knowledge_managers_are_created_fresh_per_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Each private ensure should build a fresh request-owned manager."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "mind": AgentConfig(
                    display_name="Mind",
                    private=AgentPrivateConfig(
                        per="user_agent",
                        root="mind_data",
                        template_dir=str(template_dir),
                        context_files=["SOUL.md"],
                        knowledge=AgentPrivateKnowledgeConfig(path="memory", watch=False),
                    ),
                ),
            },
            models={},
        ),
        runtime_paths,
    )
    private_base_id = config.get_agent_private_knowledge_base_id("mind")
    assert private_base_id is not None
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )

    try:
        first = await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=identity,
        )
        second = await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=identity,
        )

        assert first[private_base_id] is not second[private_base_id]
        assert _get_shared_knowledge_manager(private_base_id) is None
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_request_bound_private_manager_stays_usable_after_later_private_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """A request map should keep working after later private requests build other managers."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)
    template_dir = build_private_template_dir()
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                memory_backend="file",
                private=AgentPrivateConfig(
                    per="user_agent",
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
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
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

        await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=bob_identity,
        )
        assert (
            _get_knowledge_for_base(
                private_base_id,
                config=config,
                runtime_paths=runtime_paths_for(config),
                request_knowledge_managers=alice_managers,
            )
            is not None
        )

        assert (
            _get_knowledge_for_base(
                private_base_id,
                config=config,
                runtime_paths=runtime_paths_for(config),
                request_knowledge_managers={},
            )
            is None
        )
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_degraded_request_scoped_knowledge_does_not_fall_back_to_cached_private_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Degraded requests should not silently pick cached private knowledge back up."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    template_dir = build_private_template_dir()
    config = bind_runtime_paths(
        Config(
            agents={
                "mind": _mind_private_agent(
                    watch=False,
                    template_dir=str(template_dir),
                ),
            },
            models={},
        ),
        _runtime_paths(tmp_path / "config.yaml", tmp_path),
    )
    private_base_id = config.get_agent_private_knowledge_base_id("mind")
    assert private_base_id is not None
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )

    try:
        await ensure_agent_knowledge_managers(
            "mind",
            config,
            runtime_paths_for(config),
            execution_identity=alice_identity,
        )
        assert (
            _get_knowledge_for_base(
                private_base_id,
                config=config,
                runtime_paths=runtime_paths_for(config),
                request_knowledge_managers={},
            )
            is None
        )
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_degraded_request_scoped_knowledge_preserves_shared_manager_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty request-scoped binding should not suppress shared knowledge."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    shared_root = tmp_path / "shared-docs"
    shared_root.mkdir()
    (shared_root / "guide.md").write_text("Shared docs.\n", encoding="utf-8")
    config = _make_config(shared_root)

    try:
        managers = await initialize_shared_knowledge_managers(config, runtime_paths_for(config))

        assert (
            _get_knowledge_for_base(
                "research",
                config=config,
                runtime_paths=runtime_paths_for(config),
                request_knowledge_managers={},
                shared_manager_lookup=lambda base_id: managers.get(base_id),
            )
            is managers["research"].get_knowledge()
        )
    finally:
        await shutdown_shared_knowledge_managers()


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
async def test_initialize_git_backed_base_syncs_before_single_full_reindex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed initialization should refresh the checkout once, then report completed initial sync."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )
    sync_git_repository = AsyncMock(return_value={"updated": True, "changed_count": 10, "removed_count": 0})
    reindex_all = AsyncMock(return_value=10)
    monkeypatch.setattr(manager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(manager, "reindex_all", reindex_all)

    await manager.initialize()

    sync_git_repository.assert_awaited_once_with(index_changes=False)
    reindex_all.assert_awaited_once_with()
    assert manager.get_status()["git"]["initial_sync_complete"] is True


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_background_git_startup_defers_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background git startup should avoid blocking sync and start the background loop."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge", startup_behavior="background")
    runtime_paths = runtime_paths_for(config)

    prepare_background_git_startup = AsyncMock(
        return_value={
            "startup_mode": "resume",
            "loaded_count": 0,
            "indexed_count": 0,
            "removed_count": 0,
            "git_deferred": True,
        },
    )
    start_git_sync = AsyncMock(return_value=None)
    sync_git_repository = AsyncMock(return_value={"updated": False, "changed_count": 0, "removed_count": 0})
    initialize = AsyncMock(return_value=None)

    monkeypatch.setattr(KnowledgeManager, "prepare_background_git_startup", prepare_background_git_startup)
    monkeypatch.setattr(KnowledgeManager, "_start_git_sync", start_git_sync)
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(KnowledgeManager, "initialize", initialize)

    try:
        await initialize_shared_knowledge_managers(
            config,
            runtime_paths,
            start_watchers=False,
            reindex_on_create=False,
        )
    finally:
        await shutdown_shared_knowledge_managers()

    prepare_background_git_startup.assert_awaited_once_with("resume")
    start_git_sync.assert_awaited_once()
    sync_git_repository.assert_not_awaited()
    initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_preserves_background_git_sync_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later non-owner callers should not tear down active shared-manager git sync by default."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge", startup_behavior="background")
    runtime_paths = runtime_paths_for(config)

    prepare_background_git_startup = AsyncMock(
        return_value={
            "startup_mode": "resume",
            "loaded_count": 0,
            "indexed_count": 0,
            "removed_count": 0,
            "git_deferred": True,
        },
    )
    start_git_sync_calls = 0

    async def _track_start_git_sync(manager: KnowledgeManager) -> None:
        nonlocal start_git_sync_calls
        start_git_sync_calls += 1
        if manager._git_sync_task is None:
            manager._git_sync_stop_event = asyncio.Event()
            manager._git_sync_task = asyncio.create_task(manager._git_sync_stop_event.wait())

    sync_events: list[str] = []

    async def _track_sync_repository() -> dict[str, int | bool]:
        sync_events.append("sync")
        return {"updated": False, "changed_count": 0, "removed_count": 0}

    sync_git_repository = AsyncMock(side_effect=_track_sync_repository)

    monkeypatch.setattr(KnowledgeManager, "prepare_background_git_startup", prepare_background_git_startup)
    monkeypatch.setattr(KnowledgeManager, "_start_git_sync", _track_start_git_sync)
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", sync_git_repository)

    try:
        managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths,
            start_watchers=False,
            reindex_on_create=False,
        )
        manager = managers["research"]
        original_stop_git_sync = manager._stop_git_sync
        stop_git_sync = AsyncMock(side_effect=original_stop_git_sync)
        monkeypatch.setattr(manager, "_stop_git_sync", stop_git_sync)

        blocking_config = _make_git_config(tmp_path / "knowledge", startup_behavior="blocking")
        blocking_managers = await initialize_shared_knowledge_managers(
            blocking_config,
            runtime_paths_for(blocking_config),
            start_watchers=False,
            reindex_on_create=False,
        )

        assert blocking_managers["research"] is manager
        stop_git_sync.assert_not_awaited()
        sync_git_repository.assert_not_awaited()
    finally:
        await shutdown_shared_knowledge_managers()

    prepare_background_git_startup.assert_awaited_once_with("resume")
    assert start_git_sync_calls == 1


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_full_reindex_preserves_background_git_sync_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full-reindex replacement should keep the prior shared-manager git-sync runtime by default."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge", startup_behavior="background")
    runtime_paths = runtime_paths_for(config)

    prepare_background_git_startup = AsyncMock(
        return_value={
            "startup_mode": "resume",
            "loaded_count": 0,
            "indexed_count": 0,
            "removed_count": 0,
            "git_deferred": True,
        },
    )
    initialize = AsyncMock(return_value=None)
    watcher_starts: list[int] = []
    git_sync_starts: list[int] = []

    async def _track_start_watcher(manager: KnowledgeManager) -> None:
        watcher_starts.append(id(manager))
        if manager._watch_task is None:
            manager._watch_stop_event = asyncio.Event()
            manager._watch_task = asyncio.create_task(manager._watch_stop_event.wait())

    async def _track_start_git_sync(manager: KnowledgeManager) -> None:
        git_sync_starts.append(id(manager))
        if manager._git_sync_task is None:
            manager._git_sync_stop_event = asyncio.Event()
            manager._git_sync_task = asyncio.create_task(manager._git_sync_stop_event.wait())

    monkeypatch.setattr(KnowledgeManager, "prepare_background_git_startup", prepare_background_git_startup)
    monkeypatch.setattr(KnowledgeManager, "initialize", initialize)
    monkeypatch.setattr(KnowledgeManager, "start_watcher", _track_start_watcher)
    monkeypatch.setattr(KnowledgeManager, "_start_git_sync", _track_start_git_sync)

    try:
        managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths,
            start_watchers=False,
            reindex_on_create=False,
        )
        original_manager = managers["research"]

        updated_config = _make_git_config(tmp_path / "knowledge", lfs=True, startup_behavior="background")
        updated_managers = await initialize_shared_knowledge_managers(
            updated_config,
            runtime_paths_for(updated_config),
            start_watchers=True,
            reindex_on_create=False,
        )
        replacement_manager = updated_managers["research"]

        assert replacement_manager is not original_manager
        initialize.assert_awaited_once_with()
        assert replacement_manager._watch_task is None
        assert replacement_manager._git_sync_task is not None
        assert watcher_starts == []
        assert git_sync_starts == [id(original_manager), id(replacement_manager)]
    finally:
        await shutdown_shared_knowledge_managers()

    prepare_background_git_startup.assert_awaited_once_with("resume")


@pytest.mark.asyncio
async def test_explicit_reindex_replacement_restores_preserved_background_git_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit reindex should restore the prior shared git-sync runtime after stale-manager replacement."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge", startup_behavior="background")
    runtime_paths = runtime_paths_for(config)

    prepare_background_git_startup = AsyncMock(
        return_value={
            "startup_mode": "resume",
            "loaded_count": 0,
            "indexed_count": 0,
            "removed_count": 0,
            "git_deferred": True,
        },
    )
    sync_git_repository = AsyncMock(return_value={"updated": False, "changed_count": 0, "removed_count": 0})
    reindex_all = AsyncMock(return_value=4)
    git_sync_starts: list[int] = []

    async def _track_start_git_sync(manager: KnowledgeManager) -> None:
        git_sync_starts.append(id(manager))
        if manager._git_sync_task is None:
            manager._git_sync_stop_event = asyncio.Event()
            manager._git_sync_task = asyncio.create_task(manager._git_sync_stop_event.wait())

    monkeypatch.setattr(KnowledgeManager, "prepare_background_git_startup", prepare_background_git_startup)
    monkeypatch.setattr(KnowledgeManager, "_start_git_sync", _track_start_git_sync)

    try:
        managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths,
            start_watchers=False,
            reindex_on_create=False,
        )
        original_manager = managers["research"]

        updated_config = _make_git_config(tmp_path / "knowledge", lfs=True, startup_behavior="background")
        replacement_manager = await ensure_shared_knowledge_manager(
            "research",
            config=updated_config,
            runtime_paths=runtime_paths_for(updated_config),
            start_watchers=False,
            reindex_on_create=False,
            initialize_on_create=False,
        )

        assert replacement_manager is not None
        assert replacement_manager is not original_manager
        assert replacement_manager._git_sync_task is None

        monkeypatch.setattr(replacement_manager, "sync_git_repository", sync_git_repository)
        monkeypatch.setattr(replacement_manager, "reindex_all", reindex_all)

        result = await replacement_manager.finish_pending_background_git_startup(force_full_reindex=True)
        await replacement_manager.restore_deferred_shared_runtime()

        assert result["indexed_count"] == 4
        assert replacement_manager._git_sync_task is not None
        assert git_sync_starts == [id(original_manager), id(replacement_manager)]
    finally:
        await shutdown_shared_knowledge_managers()

    prepare_background_git_startup.assert_awaited_once_with("resume")
    sync_git_repository.assert_awaited_once_with(index_changes=False)
    reindex_all.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_runtime_owner_stops_background_git_sync_when_startup_becomes_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime owner should still be able to reconcile shared git sync down to blocking mode."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge", startup_behavior="background")
    runtime_paths = runtime_paths_for(config)

    prepare_background_git_startup = AsyncMock(
        return_value={
            "startup_mode": "resume",
            "loaded_count": 0,
            "indexed_count": 0,
            "removed_count": 0,
            "git_deferred": True,
        },
    )
    start_git_sync_calls = 0

    async def _track_start_git_sync(manager: KnowledgeManager) -> None:
        nonlocal start_git_sync_calls
        start_git_sync_calls += 1
        if manager._git_sync_task is None:
            manager._git_sync_stop_event = asyncio.Event()
            manager._git_sync_task = asyncio.create_task(manager._git_sync_stop_event.wait())

    sync_events: list[str] = []

    async def _track_sync_repository() -> dict[str, int | bool]:
        sync_events.append("sync")
        return {"updated": False, "changed_count": 0, "removed_count": 0}

    sync_git_repository = AsyncMock(side_effect=_track_sync_repository)

    monkeypatch.setattr(KnowledgeManager, "prepare_background_git_startup", prepare_background_git_startup)
    monkeypatch.setattr(KnowledgeManager, "_start_git_sync", _track_start_git_sync)
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", sync_git_repository)

    try:
        managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths,
            start_watchers=False,
            reindex_on_create=False,
            reconcile_existing_runtime=True,
        )
        manager = managers["research"]
        original_stop_git_sync = manager._stop_git_sync

        async def _track_stop_git_sync() -> None:
            sync_events.append("stop")
            await original_stop_git_sync()

        stop_git_sync = AsyncMock(side_effect=_track_stop_git_sync)
        monkeypatch.setattr(manager, "_stop_git_sync", stop_git_sync)

        blocking_config = _make_git_config(tmp_path / "knowledge", startup_behavior="blocking")
        blocking_managers = await initialize_shared_knowledge_managers(
            blocking_config,
            runtime_paths_for(blocking_config),
            start_watchers=False,
            reindex_on_create=False,
            reconcile_existing_runtime=True,
        )

        assert blocking_managers["research"] is manager
        stop_git_sync.assert_awaited_once_with()
        assert sync_events == ["stop"]
    finally:
        await shutdown_shared_knowledge_managers()

    prepare_background_git_startup.assert_awaited_once_with("resume")
    assert start_git_sync_calls == 1
    sync_git_repository.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_full_reindex_stays_blocking_even_when_background_startup_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared git managers should sync the checkout before a required blocking full reindex."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge", startup_behavior="background")
    runtime_paths = runtime_paths_for(config)

    prepare_background_git_startup = AsyncMock()
    start_git_sync = AsyncMock(return_value=None)
    sync_git_repository = AsyncMock(return_value={"updated": False, "changed_count": 0, "removed_count": 0})
    reindex_all = AsyncMock(return_value=0)
    monkeypatch.setattr(KnowledgeManager, "prepare_background_git_startup", prepare_background_git_startup)
    monkeypatch.setattr(KnowledgeManager, "_start_git_sync", start_git_sync)
    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", reindex_all)

    try:
        managers = await initialize_shared_knowledge_managers(
            config,
            runtime_paths,
            start_watchers=False,
            reindex_on_create=True,
        )
        manager = managers["research"]

        assert manager._git_background_startup_mode is None
        sync_git_repository.assert_awaited_once_with(index_changes=False)
        reindex_all.assert_awaited_once_with()
        prepare_background_git_startup.assert_not_awaited()
        start_git_sync.assert_awaited_once_with()
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_initialize_shared_knowledge_managers_resumes_partial_git_index_with_full_file_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed resume should refresh the checkout, then reconcile against the full file set."""
    _DummyChromaDb.metadatas = [{"source_path": "doc.md"}]
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge")
    runtime_paths = runtime_paths_for(config)
    seed_manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths,
    )
    seed_manager._save_persisted_indexing_state("indexing")

    git_calls: list[bool] = []
    sync_calls = 0
    initialize_calls = 0

    async def _fake_sync_git_repository(
        _manager: KnowledgeManager,
        *,
        index_changes: bool = True,
    ) -> dict[str, int | bool]:
        git_calls.append(index_changes)
        return {"updated": False, "changed_count": 0, "removed_count": 0}

    async def _fake_sync_indexed_files(_manager: KnowledgeManager) -> dict[str, int]:
        nonlocal sync_calls
        sync_calls += 1
        return {"loaded_count": 1, "indexed_count": 1, "removed_count": 0}

    async def _fake_initialize(_manager: KnowledgeManager) -> None:
        nonlocal initialize_calls
        initialize_calls += 1

    def _unexpected_reset_collection(_manager: KnowledgeManager) -> None:
        message = "git resume should not reset the collection"
        raise AssertionError(message)

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _fake_sync_git_repository)
    monkeypatch.setattr(KnowledgeManager, "sync_indexed_files", _fake_sync_indexed_files)
    monkeypatch.setattr(KnowledgeManager, "initialize", _fake_initialize)
    monkeypatch.setattr(KnowledgeManager, "_reset_collection", _unexpected_reset_collection)

    try:
        managers = await initialize_shared_knowledge_managers(config, runtime_paths, reindex_on_create=False)

        assert git_calls == [False]
        assert sync_calls == 1
        assert initialize_calls == 0
        assert managers["research"].get_status()["git"]["initial_sync_complete"] is True
    finally:
        await shutdown_shared_knowledge_managers()


@pytest.mark.asyncio
async def test_run_pending_background_git_startup_persists_completed_resume_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred resume startup should persist completion so restarts can switch to incremental mode."""
    _DummyChromaDb.metadatas = [{"source_path": "doc.md"}]
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge", startup_behavior="background")
    runtime_paths = runtime_paths_for(config)
    manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths,
    )
    sync_git_repository = AsyncMock(return_value={"updated": False, "changed_count": 0, "removed_count": 0})
    sync_indexed_files = AsyncMock(return_value={"loaded_count": 1, "indexed_count": 1, "removed_count": 0})
    monkeypatch.setattr(manager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(manager, "sync_indexed_files", sync_indexed_files)

    await manager.prepare_background_git_startup("resume")

    result = await manager._run_pending_background_git_startup()

    assert result == {
        "updated": False,
        "changed_count": 0,
        "removed_count": 0,
        "startup_mode": "resume",
        "loaded_count": 1,
        "indexed_count": 1,
    }
    assert manager._git_background_startup_mode is None
    persisted_state = manager._load_persisted_indexing_state()
    assert persisted_state is not None
    assert persisted_state.settings == manager._indexing_settings
    assert persisted_state.status == "complete"

    restarted_manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths,
    )
    assert restarted_manager._startup_index_mode() == "incremental"

    changed_config = _make_git_config(
        tmp_path / "knowledge",
        startup_behavior="background",
        include_patterns=["docs/**"],
    )
    changed_manager = KnowledgeManager(
        base_id="research",
        config=changed_config,
        runtime_paths=runtime_paths_for(changed_config),
    )
    assert changed_manager._startup_index_mode() == "full_reindex"


@pytest.mark.asyncio
async def test_finish_pending_background_git_startup_force_full_reindex_clears_pending_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced direct completion should clear deferred startup state after syncing and reindexing."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge", startup_behavior="background"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )
    sync_git_repository = AsyncMock(return_value={"updated": False, "changed_count": 0, "removed_count": 0})
    reindex_all = AsyncMock(return_value=3)
    monkeypatch.setattr(manager, "sync_git_repository", sync_git_repository)
    monkeypatch.setattr(manager, "reindex_all", reindex_all)

    await manager.prepare_background_git_startup("resume")

    result = await manager.finish_pending_background_git_startup(force_full_reindex=True)

    assert result == {
        "updated": False,
        "changed_count": 0,
        "removed_count": 0,
        "startup_mode": "full_reindex",
        "indexed_count": 3,
    }
    sync_git_repository.assert_awaited_once_with(index_changes=False)
    reindex_all.assert_awaited_once_with()
    assert manager._git_background_startup_mode is None


@pytest.mark.asyncio
async def test_reindex_explicitly_uses_git_startup_finisher_and_restores_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit manager reindex should delegate Git work and restore deferred shared runtime."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )
    finish_pending_background_git_startup = AsyncMock(
        return_value={"startup_mode": "full_reindex", "indexed_count": 7},
    )
    restore_deferred_shared_runtime = AsyncMock(return_value=None)
    monkeypatch.setattr(manager, "finish_pending_background_git_startup", finish_pending_background_git_startup)
    monkeypatch.setattr(manager, "restore_deferred_shared_runtime", restore_deferred_shared_runtime)

    indexed_count = await manager.reindex_explicitly()

    assert indexed_count == 7
    finish_pending_background_git_startup.assert_awaited_once_with(force_full_reindex=True)
    restore_deferred_shared_runtime.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_reindex_explicitly_restores_runtime_when_git_reindex_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit manager reindex should restore deferred shared runtime even when Git reindex fails."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )
    finish_pending_background_git_startup = AsyncMock(side_effect=RuntimeError("boom"))
    restore_deferred_shared_runtime = AsyncMock(return_value=None)
    monkeypatch.setattr(manager, "finish_pending_background_git_startup", finish_pending_background_git_startup)
    monkeypatch.setattr(manager, "restore_deferred_shared_runtime", restore_deferred_shared_runtime)

    with pytest.raises(RuntimeError, match="boom"):
        await manager.reindex_explicitly()

    finish_pending_background_git_startup.assert_awaited_once_with(force_full_reindex=True)
    restore_deferred_shared_runtime.assert_awaited_once_with()


def test_startup_index_mode_does_not_use_collection_count_for_existing_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup index mode should detect existing vectors via get(), not collection.count()."""
    _DummyChromaDb.metadatas = [{"source_path": "doc.md"}]
    _DummyChromaDb.raise_on_count = True
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )
    manager._save_persisted_indexing_state("indexing")

    try:
        assert manager._startup_index_mode() == "resume"
    finally:
        _DummyChromaDb.raise_on_count = False


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
async def test_sync_git_repository_once_skips_repeated_lfs_pull_for_already_hydrated_unchanged_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged LFS heads should hydrate once, then reuse the persisted hydration marker."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge", lfs=True)
    runtime_paths = _runtime_paths(tmp_path / "config.yaml", tmp_path / "storage")
    manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths,
    )

    git_calls: list[list[str]] = []

    async def _fake_ensure_git_repository(_git_config: object) -> bool:
        return False

    async def _fake_git_rev_parse(ref: str) -> str | None:
        if ref in {"HEAD", "origin/main"}:
            return "same"
        return None

    async def _fake_git_list_tracked_files() -> set[str]:
        return {"doc.md"}

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        return ""

    monkeypatch.setattr(manager, "_ensure_git_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(manager, "_git_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(manager, "_git_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    changed_files, removed_files, updated = await manager._sync_git_repository_once(manager._git_config())

    assert updated is False
    assert changed_files == set()
    assert removed_files == set()
    assert ["lfs", "pull", "origin", "main"] in git_calls

    hydrated_manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=runtime_paths,
    )
    repeated_git_calls: list[list[str]] = []

    async def _fake_run_git_second(args: list[str], **_: object) -> str:
        repeated_git_calls.append(args)
        return ""

    monkeypatch.setattr(hydrated_manager, "_ensure_git_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(hydrated_manager, "_git_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(hydrated_manager, "_git_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(hydrated_manager, "_run_git", _fake_run_git_second)

    changed_files, removed_files, updated = await hydrated_manager._sync_git_repository_once(
        hydrated_manager._git_config(),
    )

    assert updated is False
    assert changed_files == set()
    assert removed_files == set()
    assert ["lfs", "pull", "origin", "main"] not in repeated_git_calls


@pytest.mark.asyncio
async def test_sync_git_repository_once_reindexes_local_tracked_changes_when_head_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracked local dirty files should be reset and reindexed even when the remote head is unchanged."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    git_calls: list[list[str]] = []

    async def _fake_ensure_git_repository(_git_config: object) -> bool:
        return False

    async def _fake_git_rev_parse(ref: str) -> str | None:
        if ref in {"HEAD", "origin/main"}:
            return "same"
        return None

    list_tracked_files_results = iter([{"doc.md"}, {"doc.md"}])

    async def _fake_git_list_tracked_files() -> set[str]:
        return next(list_tracked_files_results)

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        if args == ["diff", "--name-only", "--no-renames", "HEAD"]:
            return "doc.md\n"
        return ""

    monkeypatch.setattr(manager, "_ensure_git_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(manager, "_git_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(manager, "_git_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    changed_files, removed_files, updated = await manager._sync_git_repository_once(manager._git_config())

    assert updated is True
    assert changed_files == {"doc.md"}
    assert removed_files == set()
    assert ["checkout", "main"] in git_calls
    assert ["reset", "--hard", "origin/main"] in git_calls


@pytest.mark.asyncio
async def test_sync_git_repository_once_restores_locally_deleted_tracked_files_when_head_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracked local deletions should be restored and reindexed even when the remote head is unchanged."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    git_calls: list[list[str]] = []

    async def _fake_ensure_git_repository(_git_config: object) -> bool:
        return False

    async def _fake_git_rev_parse(ref: str) -> str | None:
        if ref in {"HEAD", "origin/main"}:
            return "same"
        return None

    list_tracked_files_results = iter([{"doc.md"}, {"doc.md"}])

    async def _fake_git_list_tracked_files() -> set[str]:
        return next(list_tracked_files_results)

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        if args == ["diff", "--name-only", "--no-renames", "HEAD"]:
            return "doc.md\n"
        return ""

    monkeypatch.setattr(manager, "_ensure_git_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(manager, "_git_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(manager, "_git_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    changed_files, removed_files, updated = await manager._sync_git_repository_once(manager._git_config())

    assert updated is True
    assert changed_files == {"doc.md"}
    assert removed_files == set()
    assert ["checkout", "main"] in git_calls
    assert ["reset", "--hard", "origin/main"] in git_calls


@pytest.mark.asyncio
async def test_sync_git_repository_once_pulls_lfs_after_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LFS-enabled repos should explicitly pull LFS objects after resetting to the remote branch."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge", lfs=True),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    git_calls: list[list[str]] = []

    async def _fake_ensure_git_repository(_git_config: object) -> bool:
        return False

    async def _fake_git_rev_parse(ref: str) -> str | None:
        if ref == "HEAD":
            return "before"
        if ref == "origin/main":
            return "after"
        return None

    list_tracked_files_results = iter([{"doc.md"}, {"doc.md"}])

    async def _fake_git_list_tracked_files() -> set[str]:
        return next(list_tracked_files_results)

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        if args[:3] == ["diff", "--name-only", "--no-renames"]:
            return "doc.md\n"
        return ""

    monkeypatch.setattr(manager, "_ensure_git_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(manager, "_git_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(manager, "_git_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    changed_files, removed_files, updated = await manager._sync_git_repository_once(manager._git_config())

    assert updated is True
    assert changed_files == {"doc.md"}
    assert removed_files == set()
    assert ["lfs", "pull", "origin", "main"] in git_calls


@pytest.mark.asyncio
async def test_ensure_git_lfs_available_raises_clear_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing Git LFS should raise the runtime-image guidance instead of a raw git failure."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge", lfs=True),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    async def _fake_run_git(args: list[str], **_: object) -> str:
        if args == ["lfs", "version"]:
            msg = "git: 'lfs' is not a git command"
            raise RuntimeError(msg)
        return ""

    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    with pytest.raises(RuntimeError, match="Git LFS is required for this knowledge base"):
        await manager._ensure_git_lfs_available(cwd=manager.knowledge_path)


@pytest.mark.asyncio
async def test_ensure_git_lfs_repository_ready_installs_once_per_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repo-local LFS setup should only run once per manager checkout."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge", lfs=True),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    git_calls: list[list[str]] = []

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        return ""

    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    await manager._ensure_git_lfs_repository_ready(manager.knowledge_path)
    await manager._ensure_git_lfs_repository_ready(manager.knowledge_path)

    assert git_calls == [["lfs", "version"], ["lfs", "install", "--local"]]


@pytest.mark.asyncio
async def test_ensure_git_repository_clones_lfs_repo_with_skip_smudge_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial LFS clones should hydrate even if an old hydrated-head marker matches the cloned commit."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge", lfs=True),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    clone_envs: list[dict[str, str] | None] = []
    git_calls: list[list[str]] = []
    manager._git_lfs_hydrated_head_path.write_text("same", encoding="utf-8")

    async def _fake_run_git(
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = cwd
        git_calls.append(args)
        if args[0] == "clone":
            clone_envs.append(env)
        return ""

    monkeypatch.setattr(manager, "_run_git", _fake_run_git)
    monkeypatch.setattr(manager, "_git_rev_parse", AsyncMock(return_value="same"))

    cloned = await manager._ensure_git_repository(manager._git_config())

    assert cloned is True
    assert clone_envs == [{"GIT_LFS_SKIP_SMUDGE": "1"}]
    assert ["lfs", "pull", "origin", "main"] in git_calls


@pytest.mark.asyncio
async def test_sync_git_repository_does_not_record_indexing_failures_as_git_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indexing failures after git sync should not overwrite git status with a fake git error."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    async def _sync_once(_git_config: object) -> tuple[set[str], set[str], bool]:
        return {"docs/updated.md"}, set(), True

    monkeypatch.setattr(manager, "_sync_git_repository_once", _sync_once)
    monkeypatch.setattr(manager, "_git_rev_parse", AsyncMock(return_value="abc123"))
    manager.index_file = AsyncMock(side_effect=RuntimeError("index blew up"))

    with pytest.raises(RuntimeError, match="index blew up"):
        await manager.sync_git_repository()

    assert manager._git_last_error is None
    assert manager._git_last_successful_commit == "abc123"
    assert manager._git_last_successful_sync_at is not None
    assert manager._git_initial_sync_complete is False


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
async def test_run_git_timeout_kills_subprocess_and_raises_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timed out git commands should terminate the child process and raise a redacted runtime error."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge", sync_timeout_seconds=5),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    class _HangingProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.kill_called = False
            self.wait_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
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

    async def _fake_wait_for(awaitable: object, **kwargs: float) -> tuple[bytes, bytes]:
        _ = kwargs["timeout"]
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(manager, "_git_sync_timeout_seconds", lambda: 1.0)

    with pytest.raises(RuntimeError, match=r"Git command timed out after 1s: git fetch origin main"):
        await manager._run_git(["fetch", "origin", "main"])

    assert process.kill_called is True
    assert process.wait_called is True


@pytest.mark.asyncio
async def test_run_git_preserves_index_lock_and_does_not_retry(
    dummy_manager: KnowledgeManager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Git lock failures should surface immediately without deleting the lock file."""
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    lock_path = git_dir / "index.lock"
    lock_path.write_text("", encoding="utf-8")

    class _FailingProcess:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"",
                (
                    f"fatal: Unable to create '{lock_path}': File exists.\n"
                    "Another git process seems to be running in this repository."
                ).encode(),
            )

    recorded_cwds: list[str] = []

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> object:
        _ = args
        recorded_cwds.append(str(kwargs["cwd"]))
        return _FailingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match=r"index\.lock"):
        await dummy_manager._run_git(["checkout", "main"], cwd=repo_root)

    assert recorded_cwds == [str(repo_root)]
    assert lock_path.exists() is True


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


def test_text_only_default_file_filter_excludes_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-git knowledge bases should ignore binary-ish files by default."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_config(tmp_path / "knowledge"),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    (manager.knowledge_path / "guide.md").write_text("ok", encoding="utf-8")
    (manager.knowledge_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (manager.knowledge_path / "audio.mp3").write_bytes(b"ID3")
    (manager.knowledge_path / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (manager.knowledge_path / "paper.pdf").write_bytes(b"%PDF-1.7")

    listed = [path.relative_to(manager.knowledge_path).as_posix() for path in manager.list_files()]
    assert listed == ["guide.md"]


def test_user_override_can_re_enable_specific_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit include_extensions should allow a normally filtered suffix back in."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_config(tmp_path / "knowledge", include_extensions=[".pdf"]),
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )

    (manager.knowledge_path / "guide.md").write_text("skip", encoding="utf-8")
    (manager.knowledge_path / "paper.pdf").write_bytes(b"%PDF-1.7")

    listed = [path.relative_to(manager.knowledge_path).as_posix() for path in manager.list_files()]
    assert listed == ["paper.pdf"]


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


@pytest.mark.asyncio
async def test_git_sync_loop_rereads_poll_interval_after_settings_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running Git sync loops should pick up a new poll interval after config refresh."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_git_config(tmp_path / "knowledge")
    manager = KnowledgeManager(
        base_id="research",
        config=config,
        runtime_paths=_runtime_paths(tmp_path / "config.yaml", tmp_path / "storage"),
    )
    updated_config = _make_git_config(tmp_path / "knowledge")
    updated_config.knowledge_bases["research"].git.poll_interval_seconds = 5
    timeouts: list[float] = []

    async def _run_pending_background_git_startup() -> dict[str, object]:
        return {}

    async def _fake_wait_for(awaitable: object, **kwargs: float) -> None:
        timeouts.append(kwargs["timeout"])
        awaitable.close()
        if len(timeouts) == 1:
            manager._refresh_settings(
                updated_config,
                runtime_paths_for(updated_config),
                manager.storage_path,
                manager.knowledge_path,
            )
            raise TimeoutError
        manager._git_sync_stop_event.set()

    monkeypatch.setattr(manager, "_run_pending_background_git_startup", _run_pending_background_git_startup)
    monkeypatch.setattr("mindroom.knowledge.manager.asyncio.wait_for", _fake_wait_for)

    await manager._git_sync_loop()

    assert timeouts == [30.0, 5.0]
