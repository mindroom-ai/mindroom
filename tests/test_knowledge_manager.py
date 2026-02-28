"""Tests for KnowledgeManager internals."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, call

import pytest

from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
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
    monkeypatch.setattr("mindroom.knowledge.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.Knowledge", _DummyKnowledge)

    config = _make_config(tmp_path / "knowledge")
    return KnowledgeManager(base_id="research", config=config, storage_path=tmp_path / "storage")


def test_knowledge_base_relative_path_resolves_from_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Knowledge base relative paths should resolve from the config directory."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.Knowledge", _DummyKnowledge)
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


def test_list_files_respects_include_and_exclude_patterns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pattern filters should include only requested files and allow explicit exclusions."""
    _DummyChromaDb.metadatas = []
    monkeypatch.setattr("mindroom.knowledge.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.Knowledge", _DummyKnowledge)

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
