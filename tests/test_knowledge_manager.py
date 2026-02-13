"""Tests for KnowledgeManager internals."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, call

import pytest

from mindroom.config import Config, KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.knowledge import (
    KnowledgeManager,
    get_knowledge_manager,
    initialize_knowledge_managers,
    shutdown_knowledge_managers,
)

if TYPE_CHECKING:
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
    ) -> dict[str, object]:
        _ = include
        if limit is None:
            selected = _DummyChromaDb.metadatas[offset:]
        else:
            selected = _DummyChromaDb.metadatas[offset : offset + limit]
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

    def insert(self, *, path: str, metadata: dict[str, object], upsert: bool) -> None:
        self.insert_calls.append({"path": path, "metadata": metadata, "upsert": upsert})

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


def _make_config(path: Path) -> Config:
    return Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(path), watch=False),
        },
    )


def _make_git_config(path: Path) -> Config:
    return Config(
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
                ),
            ),
        },
    )


@pytest.fixture
def dummy_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KnowledgeManager:
    """Build a KnowledgeManager with lightweight fakes for vector operations."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.Knowledge", _DummyKnowledge)

    config = _make_config(tmp_path / "knowledge")
    return KnowledgeManager(base_id="research", config=config, storage_path=tmp_path / "storage")


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
    assert knowledge.insert_calls[0]["metadata"] == {"source_path": "doc.txt"}
    assert knowledge.insert_calls[0]["upsert"] is True


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
async def test_initialize_knowledge_managers_maintains_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry should track configured bases and remove stale managers."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.Knowledge", _DummyKnowledge)

    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "research"), watch=False),
            "legal": KnowledgeBaseConfig(path=str(tmp_path / "legal"), watch=False),
        },
    )

    managers = await initialize_knowledge_managers(config, tmp_path / "storage", reindex_on_create=False)
    assert set(managers) == {"research", "legal"}
    assert get_knowledge_manager("research") is managers["research"]

    updated_config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(tmp_path / "research"), watch=False),
        },
    )

    managers = await initialize_knowledge_managers(updated_config, tmp_path / "storage", reindex_on_create=False)
    assert set(managers) == {"research"}
    assert get_knowledge_manager("legal") is None

    await shutdown_knowledge_managers()


@pytest.mark.asyncio
async def test_sync_git_repository_updates_index_for_changed_and_deleted_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git sync should remove deleted files and upsert changed files."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        storage_path=tmp_path / "storage",
    )

    monkeypatch.setattr(
        manager,
        "_sync_git_repository_once",
        lambda _git_config: ({"docs/new.md", "docs/updated.md"}, {"docs/deleted.md"}, True),
    )
    manager.index_file = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager.remove_file = AsyncMock(return_value=True)  # type: ignore[method-assign]

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


def test_list_files_skips_hidden_paths_when_git_skip_hidden_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden files and folders are ignored for git-backed knowledge bases by default."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        storage_path=tmp_path / "storage",
    )

    (manager.knowledge_path / "public").mkdir(parents=True, exist_ok=True)
    (manager.knowledge_path / "public" / "doc.md").write_text("ok", encoding="utf-8")
    (manager.knowledge_path / ".hidden").mkdir(parents=True, exist_ok=True)
    (manager.knowledge_path / ".hidden" / "secret.md").write_text("skip", encoding="utf-8")
    (manager.knowledge_path / "public" / ".dotfile.md").write_text("skip", encoding="utf-8")

    listed = [path.relative_to(manager.knowledge_path).as_posix() for path in manager.list_files()]
    assert listed == ["public/doc.md"]


@pytest.mark.asyncio
async def test_start_watcher_starts_git_sync_even_when_file_watch_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git polling should run even when filesystem watch is disabled."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        storage_path=tmp_path / "storage",
    )

    async def fake_git_sync_loop() -> None:
        await manager._git_sync_stop_event.wait()

    monkeypatch.setattr(manager, "_git_sync_loop", fake_git_sync_loop)

    await manager.start_watcher()
    assert manager._watch_task is None
    assert manager._git_sync_task is not None

    await manager.stop_watcher()
    assert manager._git_sync_task is None
