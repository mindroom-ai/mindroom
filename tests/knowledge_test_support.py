"""Shared helpers for knowledge-index tests."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

import mindroom.knowledge.registry as knowledge_registry
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.knowledge.read_protocol import SOURCE_DIGEST_KEY, SOURCE_MTIME_NS_KEY, SOURCE_PATH_KEY, SOURCE_SIZE_KEY
from mindroom.knowledge.vector_store import CollectionMeta, StoredRow
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from mindroom.config.knowledge import KnowledgeGitConfig
    from mindroom.knowledge.vector_store import KnowledgeCollection


def stored_rows(collection: KnowledgeCollection) -> list[dict[str, Any]]:
    """Return a collection's rows in insertion order as ``{"id", "content", "metadata"}`` dicts."""
    with closing(sqlite3.connect(collection.file)) as connection:
        return [
            {"id": row_id, "content": document, "metadata": json.loads(metadata)}
            for row_id, document, metadata in connection.execute(
                "SELECT id, document, metadata FROM chunks ORDER BY rowid",
            )
        ]


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


@pytest.fixture
def patch_vector_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Index and search with a fake embedder; vectors go to the real SQLite store under each test's storage root."""
    monkeypatch.setattr(
        "mindroom.knowledge.manager.create_configured_embedder",
        lambda *_args, **_kwargs: _FakeEmbedder(),
    )
    monkeypatch.setattr(
        "mindroom.knowledge.registry.create_configured_embedder",
        lambda *_args, **_kwargs: _FakeEmbedder(),
    )
    knowledge_registry._published_indexes.clear()
    yield
    knowledge_registry._published_indexes.clear()


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
