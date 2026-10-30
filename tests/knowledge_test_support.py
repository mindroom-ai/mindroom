"""Shared helpers for knowledge-index tests."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

import mindroom.knowledge.manager as knowledge_manager
import mindroom.knowledge.refresh_runner as knowledge_refresh_runner
import mindroom.knowledge.registry as knowledge_registry
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.knowledge.collections import open_collection
from mindroom.knowledge.index_metadata import load_publication, new_publication, save_publication
from mindroom.knowledge.read_protocol import SOURCE_DIGEST_KEY, SOURCE_MTIME_NS_KEY, SOURCE_PATH_KEY, SOURCE_SIZE_KEY
from mindroom.knowledge.refresh_runner import _refresh_knowledge_binding
from mindroom.knowledge.registry import published_index_storage_path
from mindroom.knowledge.vector_store import CollectionMeta, StoredRow
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from mindroom.config.knowledge import KnowledgeGitConfig
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.index_metadata import Publication
    from mindroom.knowledge.indexing_config import IndexingSettings
    from mindroom.knowledge.manager import KnowledgeManager
    from mindroom.knowledge.refresh_runner import _RefreshChildResult
    from mindroom.knowledge.registry import PublishedIndexKey
    from mindroom.knowledge.vector_store import KnowledgeCollection
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def stored_rows(collection: KnowledgeCollection) -> list[dict[str, Any]]:
    """Return a collection's rows in insertion order as ``{"id", "content", "metadata"}`` dicts."""
    with closing(sqlite3.connect(collection.file)) as connection:
        return [
            {"id": row_id, "content": document, "metadata": json.loads(metadata)}
            for row_id, document, metadata in connection.execute(
                "SELECT id, document, metadata FROM chunks ORDER BY rowid",
            )
        ]


def row_paths(collection: KnowledgeCollection) -> set[str]:
    """Return the source paths a collection holds rows for."""
    return {row["metadata"][SOURCE_PATH_KEY] for row in stored_rows(collection)}


def published_collection(manager: KnowledgeManager) -> KnowledgeCollection:
    """Return the collection the manager's base publishes."""
    publication = load_publication(manager._base_storage_path)
    assert publication is not None
    assert publication.collection is not None
    return open_collection(manager._collections, publication.collection)


def publish(
    key: PublishedIndexKey,
    *,
    collection: str | None,
    settings: IndexingSettings | None = None,
    indexed_count: int = 1,
    source_signature: str = "source-signature",
    published_revision: str | None = None,
) -> Publication:
    """Write a publication for ``key`` the way a refresh child does, and return it."""
    publication = new_publication(
        settings=key.indexing_settings if settings is None else settings,
        collection=collection,
        indexed_count=indexed_count,
        source_signature=source_signature,
        published_revision=published_revision,
    )
    save_publication(published_index_storage_path(key), publication)
    return publication


def collection_meta(settings: Mapping[str, str] | None = None, **changes: Any) -> CollectionMeta:  # noqa: ANN401
    """Return the state a test collection starts with: building, under ``settings``."""
    meta = CollectionMeta(
        settings=dict(settings or {}),
        stage="building",
        rebuild=False,
        revision=None,
        total_files=0,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    return replace(meta, **changes)


def seed_collection(
    collection: KnowledgeCollection,
    rows: Sequence[Mapping[str, Any]],
    *,
    settings: Mapping[str, str] | None = None,
) -> None:
    """Replace a collection with ``rows`` (``content``, ``metadata`` and optional ``id``), each with a one-dimension vector.

    Each source path is claimed at the signature its first row's metadata names, or ``(0, 0, "seeded")``.
    """
    collection.create(collection_meta(settings))
    by_path: dict[str, list[StoredRow]] = {}
    for index, row in enumerate(rows):
        metadata = dict(row.get("metadata") or {})
        metadata.setdefault(SOURCE_PATH_KEY, "seeded.md")
        by_path.setdefault(metadata[SOURCE_PATH_KEY], []).append(
            StoredRow(
                id=str(row.get("id", f"seeded-{index}")),
                document=str(row["content"]),
                metadata=metadata,
                embedding=[1.0],
            ),
        )
    for relative_path, path_rows in by_path.items():
        metadata = path_rows[0].metadata
        signature = (
            int(metadata.get(SOURCE_MTIME_NS_KEY, 0)),
            int(metadata.get(SOURCE_SIZE_KEY, 0)),
            str(metadata.get(SOURCE_DIGEST_KEY, "seeded")),
        )
        collection.write_file(relative_path, signature, path_rows)


class _FakeEmbedder:
    """Typed stand-in for the configured embedder; every chunk embeds to the same one-dimension vector."""

    sends_batches = False

    def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        _ = purpose
        return [[1.0] for _text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


def install_knowledge_embedder(monkeypatch: pytest.MonkeyPatch, build: Callable[..., object]) -> None:
    """Build every knowledge embedder with ``build``, and drop the read handles built before."""
    monkeypatch.setattr(knowledge_manager, "create_configured_embedder", build)
    monkeypatch.setattr(knowledge_registry, "create_configured_embedder", build)
    knowledge_registry._published_indexes.clear()


@pytest.fixture
def patch_vector_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Index and search with a fake embedder; vectors go to the real SQLite store under each test's storage root."""
    install_knowledge_embedder(monkeypatch, lambda *_args, **_kwargs: _FakeEmbedder())
    yield
    knowledge_registry._published_indexes.clear()


async def run_refresh_child_in_process(
    key: PublishedIndexKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    force_reindex: bool,
) -> _RefreshChildResult:
    """Run the refresh child's job in this process, reporting an exception as the child's entry point does."""
    try:
        return await _refresh_knowledge_binding(
            key.base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            force_reindex=force_reindex,
        )
    except Exception as exc:
        return knowledge_refresh_runner._failed_child_result(exc)


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
