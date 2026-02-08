"""Tests for KnowledgeManager internals."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config import Config, KnowledgeConfig
from mindroom.knowledge import KnowledgeManager

if TYPE_CHECKING:
    from pathlib import Path


class _DummyVectorDb:
    def delete(self) -> bool:
        return True

    def create(self) -> None:
        return None


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
    def __init__(self, **_: object) -> None:
        return None


def _make_config(path: Path) -> Config:
    return Config(
        agents={},
        models={},
        knowledge=KnowledgeConfig(enabled=True, path=str(path), watch=False),
    )


@pytest.fixture
def dummy_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KnowledgeManager:
    """Build a KnowledgeManager with lightweight fakes for vector operations."""
    monkeypatch.setattr("mindroom.knowledge.ChromaDb", _DummyChromaDb)
    monkeypatch.setattr("mindroom.knowledge.Knowledge", _DummyKnowledge)

    config = _make_config(tmp_path / "knowledge")
    return KnowledgeManager(config=config, storage_path=tmp_path / "storage")


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
