"""Shared fakes for knowledge-index tests."""

from __future__ import annotations

from itertools import count
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
from agno.knowledge.document.base import Document
from agno.knowledge.embedder.base import Embedder

import mindroom.knowledge.refresh_locks as knowledge_refresh_locks
import mindroom.knowledge.registry as knowledge_registry
import mindroom.knowledge.utils as knowledge_utils
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.config.knowledge import KnowledgeGitConfig


def validate_where_operands(where: dict[str, Any] | None) -> None:
    """Reject the ``where`` shapes ChromaDB refuses before it reaches the store.

    Chroma validates an empty ``$in`` operand and raises, so a fake that only
    filtered records would answer "nothing matched" for a query production
    cannot issue at all -- and would answer it most convincingly on an empty
    collection, where no record is ever tested. Checking the operand rather
    than the records is what makes that case visible.
    """
    if not where:
        return
    for condition in where.values():
        if isinstance(condition, dict) and "$in" in condition and not condition["$in"]:
            msg = "empty $in operand: ChromaDB rejects this before querying the store"
            raise AssertionError(msg)


def metadata_matches(metadata: dict[str, Any], key: str, condition: object) -> bool:
    """Mirror the subset of ChromaDB ``where`` matching the indexer relies on.

    Only the operators MindRoom actually issues are supported; anything else
    raises so a new query shape cannot silently pass against the fake.
    """
    if isinstance(condition, dict):
        if "$in" in condition:
            return metadata.get(key) in condition["$in"]
        if "$eq" in condition:
            return metadata.get(key) == condition["$eq"]
        msg = f"unsupported where condition: {condition!r}"
        raise AssertionError(msg)
    return metadata.get(key) == condition


def chroma_get_result(
    *,
    ids: list[str],
    metadatas: list[dict[str, Any]],
    include: Sequence[str],
    documents: list[str] | None = None,
    embeddings: list[list[float]] | None = None,
) -> dict[str, Any]:
    """Shape a Chroma ``get`` result, honoring ``include`` the way Chroma does.

    Chroma omits whatever was not requested: ``include=[]`` returns
    ``metadatas: None`` while still returning ids. That asymmetry is the whole
    reason an existence probe must read ids, so a fake that returned metadatas
    regardless would let a probe reading them pass in tests while reporting
    "no vectors" for every file in production.

    ``include`` is required rather than defaulted. Every production caller
    passes it explicitly, and guessing a default here would model a shape
    nothing exercises. Asking for a field the caller did not supply raises
    rather than returning ``None``, so a fake that cannot serve a query shape
    says so instead of looking like an empty store.
    """
    available: dict[str, list[Any] | None] = {
        "metadatas": metadatas,
        "documents": documents,
        "embeddings": embeddings,
    }
    unsupported = set(include) - set(available)
    if unsupported:
        msg = f"unsupported include fields: {sorted(unsupported)}"
        raise AssertionError(msg)
    absent = [name for name in include if available[name] is None]
    if absent:
        msg = f"include asked for fields this fake was not given: {sorted(absent)}"
        raise AssertionError(msg)
    return {"ids": ids, **{name: (values if name in include else None) for name, values in available.items()}}


_vector_row_ids = count()


class _Collection:
    def __init__(self, name: str) -> None:
        self._name = name

    def get(
        self,
        *,
        include: Sequence[str],
        limit: int | None = None,
        offset: int = 0,
        where: dict[str, object] | None = None,
    ) -> dict[str, object]:
        with _VectorDb.lock:
            selected_all = list(_VectorDb.collections.get(self._name, []))
        if where:
            key, condition = next(iter(where.items()))
            selected_all = [item for item in selected_all if metadata_matches(item["metadata"], key, condition)]
        selected = selected_all[offset:] if limit is None else selected_all[offset : offset + limit]
        return chroma_get_result(
            ids=[str(item["id"]) for item in selected],
            metadatas=[dict(item["metadata"]) for item in selected],
            documents=[str(item["content"]) for item in selected],
            embeddings=[list(cast("list[float]", item["embedding"])) for item in selected],
            include=include,
        )

    def add(
        self,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, object]],
    ) -> None:
        if not ids:
            # Chroma rejects an empty write rather than treating it as a no-op.
            message = "Expected Embeddings to be non-empty list or numpy array, got [] in add."
            raise ValueError(message)
        with _VectorDb.lock:
            _VectorDb.collections.setdefault(self._name, []).extend(
                {
                    "id": identifier,
                    "content": document,
                    "embedding": list(embedding),
                    "metadata": dict(metadata),
                }
                for identifier, embedding, document, metadata in zip(
                    ids,
                    embeddings,
                    documents,
                    metadatas,
                    strict=True,
                )
            )

    def delete(self, *, where: dict[str, object]) -> None:
        key, condition = next(iter(where.items()))
        with _VectorDb.lock:
            _VectorDb.collections[self._name] = [
                item
                for item in _VectorDb.collections.get(self._name, [])
                if not metadata_matches(item["metadata"], key, condition)
            ]


class _Client:
    def get_collection(self, name: str) -> _Collection:
        return _Collection(name)

    def list_collections(self) -> list[str]:
        with _VectorDb.lock:
            return sorted(_VectorDb.collections)


class _VectorDb:
    collections: ClassVar[dict[str, list[dict[str, object]]]] = {}
    lock: ClassVar[Lock] = Lock()

    def __init__(self, *, collection: str, **_: object) -> None:
        self.collection_name = collection
        self.client = _Client()

    def delete(self) -> bool:
        with self.lock:
            self.collections.pop(self.collection_name, None)
        return True

    def create(self) -> None:
        with self.lock:
            self.collections[self.collection_name] = []

    def exists(self) -> bool:
        with self.lock:
            return self.collection_name in self.collections

    def search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, object] | list[object] | None = None,
    ) -> list[Document]:
        _ = (query, filters)
        with self.lock:
            items = list(self.collections.get(self.collection_name, []))
        return [Document(content=str(item["content"]), meta_data=dict(item["metadata"])) for item in items[:limit]]

    async def async_search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, object] | list[object] | None = None,
    ) -> list[Document]:
        return self.search(query=query, limit=limit, filters=filters)


class _Knowledge:
    def __init__(self, vector_db: _VectorDb | None = None) -> None:
        self.vector_db = vector_db

    def insert(
        self,
        *,
        path: str,
        metadata: dict[str, object],
        upsert: bool,
        reader: object | None = None,
    ) -> None:
        _ = (upsert, reader)
        with _VectorDb.lock:
            _VectorDb.collections.setdefault(self.vector_db.collection_name, []).append(
                {
                    "id": f"row-{next(_vector_row_ids)}",
                    "content": Path(path).read_text(encoding="utf-8"),
                    "embedding": [1.0],
                    "metadata": dict(metadata),
                },
            )

    async def ainsert(
        self,
        *,
        path: str,
        metadata: dict[str, object],
        upsert: bool,
        reader: object | None = None,
    ) -> None:
        # Match the real Knowledge surface: ainsert delegates to insert.
        self.insert(path=path, metadata=metadata, upsert=upsert, reader=reader)

    def remove_vectors_by_metadata(self, metadata: dict[str, object]) -> bool:
        with _VectorDb.lock:
            items = _VectorDb.collections.get(self.vector_db.collection_name, [])
            filtered = [
                item for item in items if not all(item["metadata"].get(key) == value for key, value in metadata.items())
            ]
            _VectorDb.collections[self.vector_db.collection_name] = filtered
        return len(filtered) != len(items)

    def search(self, query: str, max_results: int | None = None) -> list[Document]:
        return self.vector_db.search(query=query, limit=max_results or 5)


class _FakeEmbedder(Embedder):
    """Typed stand-in for the configured embedder; never issues requests."""

    def get_embedding(self, text: str) -> list[float]:
        _ = text
        msg = "fake embedder must not be asked for vectors"
        raise AssertionError(msg)

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, object] | None]:
        _ = text
        msg = "fake embedder must not be asked for vectors"
        raise AssertionError(msg)


@pytest.fixture
def patch_vector_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use an in-memory vector store for published knowledge index tests."""
    _VectorDb.collections = {}
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _VectorDb)
    monkeypatch.setattr("mindroom.knowledge.collections.ChromaDb", _VectorDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _Knowledge)
    monkeypatch.setattr("mindroom.knowledge.collections.Knowledge", _Knowledge)
    monkeypatch.setattr(
        "mindroom.knowledge.manager.create_configured_embedder",
        lambda *_args, **_kwargs: _FakeEmbedder(),
    )
    monkeypatch.setattr("mindroom.knowledge.indexing_config.ChromaDb", _VectorDb)
    monkeypatch.setattr("mindroom.knowledge.registry.ChromaDb", _VectorDb)
    monkeypatch.setattr("mindroom.knowledge.registry.StrictSearchKnowledge", _Knowledge)
    monkeypatch.setattr("mindroom.knowledge.registry.create_configured_embedder", lambda *_args, **_kwargs: object())
    knowledge_registry._published_indexes.clear()
    knowledge_utils._refresh_scheduled_at.clear()
    knowledge_refresh_locks._refresh_locks.clear()
    knowledge_refresh_locks._active_refresh_counts.clear()
    yield
    knowledge_registry._published_indexes.clear()
    knowledge_utils._refresh_scheduled_at.clear()
    knowledge_refresh_locks._refresh_locks.clear()
    knowledge_refresh_locks._active_refresh_counts.clear()
    _VectorDb.collections = {}


def _config(
    tmp_path: Path,
    *,
    bases: dict[str, Path],
    agent_bases: list[str],
    git_configs: dict[str, KnowledgeGitConfig] | None = None,
    watch: bool = False,
    modes: dict[str, str] | None = None,
    memory: dict[str, object] | None = None,
) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=agent_bases)},
            models={},
            memory=memory or {},
            knowledge_bases={
                base_id: KnowledgeBaseConfig(
                    path=str(path),
                    watch=watch,
                    git=(git_configs or {}).get(base_id),
                    mode=(modes or {}).get(base_id, "semantic"),
                )
                for base_id, path in bases.items()
            },
        ),
        runtime_paths,
    )
