"""Shared fakes for knowledge-index tests."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
from chromadb.errors import NotFoundError

import mindroom.knowledge.read_proxy as knowledge_read_proxy
import mindroom.knowledge.refresh_locks as knowledge_refresh_locks
import mindroom.knowledge.registry as knowledge_registry
import mindroom.knowledge.utils as knowledge_utils
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.knowledge.read_protocol import SearchHit
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.config.knowledge import KnowledgeGitConfig
    from mindroom.embeddings.base import Embedder
    from mindroom.knowledge.indexing_config import IndexingSettings


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


class _Collection:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

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

    def upsert(
        self,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, object]],
    ) -> None:
        with _VectorDb.lock:
            rows = _VectorDb.collections.setdefault(self._name, [])
            replaced = set(ids)
            rows[:] = [row for row in rows if row["id"] not in replaced]
        self.add(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)

    def delete(self, *, where: dict[str, object]) -> None:
        validate_where_operands(where)
        key, condition = next(iter(where.items()))
        with _VectorDb.lock:
            _VectorDb.collections[self._name] = [
                item
                for item in _VectorDb.collections.get(self._name, [])
                if not metadata_matches(item["metadata"], key, condition)
            ]


class _Client:
    """In-memory stand-in for the persistent Chroma client one knowledge base opens."""

    def get_collection(self, name: str) -> _Collection:
        with _VectorDb.lock:
            if name not in _VectorDb.collections:
                message = f"Collection {name!r} does not exist"
                raise NotFoundError(message)
        return _Collection(name)

    def create_collection(self, name: str, metadata: dict[str, object] | None = None) -> _Collection:
        _ = metadata
        with _VectorDb.lock:
            _VectorDb.collections.setdefault(name, [])
        return _Collection(name)

    def delete_collection(self, name: str) -> None:
        with _VectorDb.lock:
            if name not in _VectorDb.collections:
                message = f"Collection {name!r} does not exist"
                raise NotFoundError(message)
            del _VectorDb.collections[name]

    def list_collections(self) -> list[_Collection]:
        with _VectorDb.lock:
            return [_Collection(name) for name in sorted(_VectorDb.collections)]

    def get_max_batch_size(self) -> int:
        return 5461

    def close(self) -> None:
        """The in-memory fake has no external resources to release."""


class _VectorDb:
    """The in-memory store every fake collection reads and writes, and a published index reader over it."""

    collections: ClassVar[dict[str, list[dict[str, object]]]] = {}
    lock: ClassVar[Lock] = Lock()

    def __init__(self, *, collection: str, **_: object) -> None:
        self.collection_name = collection

    def exists(self) -> bool:
        with self.lock:
            return self.collection_name in self.collections

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        _ = query
        with self.lock:
            items = list(self.collections.get(self.collection_name, []))
        return [SearchHit(content=str(item["content"]), meta_data=dict(item["metadata"])) for item in items[:limit]]


class _ReadProxy(_VectorDb):
    def __init__(
        self,
        *,
        collection_name: str,
        path: str,
        embedder: Embedder,
        published_settings: IndexingSettings,
    ) -> None:
        self.published_settings = published_settings
        super().__init__(collection=collection_name, path=path, embedder=embedder)


class _FakeEmbedder:
    """Typed stand-in for the configured embedder; every chunk embeds to the same one-dimension vector."""

    sends_batches = False

    def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        _ = purpose
        return [[1.0] for _text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


@pytest.fixture
def patch_vector_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use an in-memory vector store for published knowledge index tests."""
    _VectorDb.collections = {}
    monkeypatch.setattr("mindroom.knowledge.chroma_client._persistent_client", lambda _path: _Client())
    monkeypatch.setattr(
        "mindroom.knowledge.manager.create_configured_embedder",
        lambda *_args, **_kwargs: _FakeEmbedder(),
    )
    monkeypatch.setattr(knowledge_read_proxy, "ChromaReadProxy", _ReadProxy)
    monkeypatch.setattr(
        knowledge_read_proxy,
        "collection_exists",
        lambda _path, collection: _VectorDb(collection=collection).exists(),
    )
    monkeypatch.setattr(
        "mindroom.knowledge.registry.create_configured_embedder",
        lambda *_args, **_kwargs: _FakeEmbedder(),
    )
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
    poll_intervals: dict[str, int] | None = None,
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
                    poll_interval_seconds=(poll_intervals or {}).get(base_id),
                    mode=(modes or {}).get(base_id, "semantic"),
                )
                for base_id, path in bases.items()
            },
        ),
        runtime_paths,
    )
