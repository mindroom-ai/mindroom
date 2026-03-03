"""Tests for KnowledgeManager internals."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, call

import pytest
from pydantic import ValidationError

from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.knowledge.manager import (
    _FAILED_SIGNATURE_RETRY_NS,
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


def _make_config(path: Path) -> Config:
    return Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(path), watch=False),
        },
    )


def _make_git_config(
    path: Path,
    *,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> Config:
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
                    include_patterns=include_patterns or [],
                    exclude_patterns=exclude_patterns or [],
                ),
            ),
        },
    )


@pytest.fixture
def dummy_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KnowledgeManager:
    """Build a KnowledgeManager with lightweight fakes for vector operations."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    config = _make_config(tmp_path / "knowledge")
    return KnowledgeManager(base_id="research", config=config, storage_path=tmp_path / "storage")


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
    monkeypatch.setattr("mindroom.constants.CONFIG_PATH", config_dir / "config.yaml")

    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path="knowledge", watch=False),
        },
    )
    manager = KnowledgeManager(base_id="research", config=config, storage_path=tmp_path / "storage")

    assert manager.knowledge_path == (config_dir / "knowledge").resolve()


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
    manager = KnowledgeManager(base_id="research", config=config, storage_path=tmp_path / "storage")
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
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _DummyKnowledge)

    manager = KnowledgeManager(
        base_id="research",
        config=_make_git_config(tmp_path / "knowledge"),
        storage_path=tmp_path / "storage",
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
        storage_path=tmp_path / "storage",
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
        storage_path=tmp_path / "storage",
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
