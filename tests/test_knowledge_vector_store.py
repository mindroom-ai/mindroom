"""The owned knowledge vector store: one SQLite file per collection, searched in process."""

from __future__ import annotations

import math
import sqlite3
from typing import TYPE_CHECKING

import pytest

from mindroom.knowledge.vector_store import (
    KnowledgeCollection,
    StoredRow,
    _CollectionNotFoundError,
    list_collection_names,
)

if TYPE_CHECKING:
    from pathlib import Path


def _row(row_id: str, source_path: str, document: str, embedding: list[float], **metadata: object) -> StoredRow:
    return StoredRow(
        id=row_id,
        document=document,
        metadata={"source_path": source_path, "name": source_path.rsplit("/", 1)[-1], **metadata},
        embedding=embedding,
    )


@pytest.fixture
def collection(tmp_path: Path) -> KnowledgeCollection:
    """A fresh, empty collection in the test's storage directory."""
    created = KnowledgeCollection(path=tmp_path, name="mindroom_knowledge_docs")
    created.reset()
    return created


def test_reset_creates_an_empty_collection_file(collection: KnowledgeCollection, tmp_path: Path) -> None:
    """Reset creates the collection's file with no rows."""
    assert collection.exists()
    assert collection.file == tmp_path / "mindroom_knowledge_docs.sqlite3"
    assert not collection.has_rows()
    assert list_collection_names(tmp_path) == ("mindroom_knowledge_docs",)


def test_reset_drops_existing_rows(collection: KnowledgeCollection) -> None:
    """Resetting an existing collection empties it."""
    collection.write_source_rows("a.md", [_row("1", "a.md", "alpha", [1.0, 0.0])])
    collection.reset()
    assert not collection.has_rows()


def test_write_source_rows_replaces_only_that_files_rows(collection: KnowledgeCollection) -> None:
    """Writing a file's rows replaces that file's rows and leaves other files alone."""
    collection.write_source_rows(
        "a.md",
        [_row("a1", "a.md", "old a", [1.0, 0.0]), _row("a2", "a.md", "old a2", [1.0, 0.0])],
    )
    collection.write_source_rows("b.md", [_row("b1", "b.md", "b", [0.0, 1.0])])

    collection.write_source_rows("a.md", [_row("a3", "a.md", "new a", [1.0, 0.0])])

    hits = collection.search([1.0, 0.0], limit=10)
    assert sorted(hit.content for hit in hits) == ["b", "new a"]


def test_delete_source_paths_and_paths_with_vectors(collection: KnowledgeCollection) -> None:
    """Path deletes and probes work across more paths than one statement binds."""
    paths = [f"dir/{index}.md" for index in range(1200)]
    for index, path in enumerate(paths):
        collection.write_source_rows(path, [_row(str(index), path, f"doc {index}", [1.0, float(index)])])

    assert collection.paths_with_vectors([*paths, "missing.md"]) == set(paths)
    collection.delete_source_paths(paths[:1100])
    assert collection.paths_with_vectors(paths) == set(paths[1100:])
    assert collection.paths_with_vectors([]) == set()


def test_copy_rows_keeps_selected_rows_verbatim(collection: KnowledgeCollection, tmp_path: Path) -> None:
    """Copying keeps the selected rows' text, metadata, and vectors."""
    collection.write_source_rows("a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0], source_size=5)])
    collection.write_source_rows("b.md", [_row("b1", "b.md", "beta", [0.0, 1.0], source_size=4)])
    candidate = KnowledgeCollection(path=tmp_path, name="mindroom_knowledge_docs_candidate_x")
    candidate.reset()

    candidate.copy_rows(collection, select=lambda metadata: metadata["source_path"] == "a.md", page_rows=1)

    [hit] = candidate.search([1.0, 0.0], limit=5)
    assert hit.content == "alpha"
    assert hit.meta_data["source_size"] == 5
    assert candidate.paths_with_vectors(["a.md", "b.md"]) == {"a.md"}


def test_search_ranks_by_cosine_distance_and_shapes_hits(collection: KnowledgeCollection) -> None:
    """Search ranks by cosine distance and shapes hits the way knowledge search always has."""
    collection.write_source_rows(
        "a.md",
        [
            _row("a1", "a.md", "east", [2.0, 0.0], content_id="cid", chunk=1),
            _row("a2", "a.md", "north", [0.0, 3.0], chunk=2),
            _row("a3", "a.md", "northeast", [1.0, 1.0], chunk=3),
        ],
    )

    hits = collection.search([1.0, 0.1], limit=2)

    assert [hit.content for hit in hits] == ["east", "northeast"]
    east = hits[0]
    assert east.name == "a.md"
    assert "name" not in east.meta_data
    assert "content_id" not in east.meta_data
    assert east.meta_data["chunk"] == 1
    expected = 1 - 1.0 / math.hypot(1.0, 0.1)
    assert east.meta_data["distances"] == pytest.approx(expected, abs=1e-6)
    assert east.meta_data["similarity_score"] == east.meta_data["distances"]


def test_search_drops_repeated_chunks_of_one_document(collection: KnowledgeCollection) -> None:
    """A chunk stored twice under one name appears once, and the limit still fills."""
    collection.write_source_rows(
        "a.md",
        [
            _row("a1", "a.md", "same", [1.0, 0.0]),
            _row("a2", "a.md", "same", [1.0, 0.0]),
            _row("a3", "a.md", "other", [0.9, 0.1]),
        ],
    )

    assert [hit.content for hit in collection.search([1.0, 0.0], limit=2)] == ["same", "other"]


def test_search_empty_collection_returns_nothing(collection: KnowledgeCollection) -> None:
    """An empty collection returns no hits."""
    assert collection.search([1.0, 0.0], limit=3) == []


def test_search_rejects_a_query_of_another_dimension(collection: KnowledgeCollection) -> None:
    """A query embedding of the wrong dimension is refused."""
    collection.write_source_rows("a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])
    with pytest.raises(ValueError, match="dimension"):
        collection.search([1.0, 0.0, 0.0], limit=1)


def test_search_sees_a_rebuilt_file(collection: KnowledgeCollection) -> None:
    """A rebuilt file is not served from a cached matrix of its previous version."""
    collection.write_source_rows("a.md", [_row("a1", "a.md", "before", [1.0, 0.0])])
    assert [hit.content for hit in collection.search([1.0, 0.0], limit=1)] == ["before"]

    collection.reset()
    collection.write_source_rows("a.md", [_row("a1", "a.md", "after", [1.0, 0.0])])

    assert [hit.content for hit in collection.search([1.0, 0.0], limit=1)] == ["after"]


def test_missing_collection_raises_not_found(tmp_path: Path) -> None:
    """Operations on a missing collection raise, and deleting it is a no-op."""
    missing = KnowledgeCollection(path=tmp_path, name="gone")
    assert not missing.exists()
    with pytest.raises(_CollectionNotFoundError):
        missing.search([1.0], limit=1)
    with pytest.raises(_CollectionNotFoundError):
        missing.write_source_rows("a.md", [])
    missing.delete()


def test_delete_removes_the_file(collection: KnowledgeCollection, tmp_path: Path) -> None:
    """Deleting a collection removes its file."""
    collection.delete()
    assert not collection.exists()
    assert list_collection_names(tmp_path) == ()


def test_corrupt_file_reports_sqlite_corruption(tmp_path: Path) -> None:
    """A file that is not a database reports SQLite's own corruption code."""
    broken = KnowledgeCollection(path=tmp_path, name="broken")
    broken.file.write_bytes(b"not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError) as raised:
        broken.has_rows()
    assert raised.value.sqlite_errorname == "SQLITE_NOTADB"


def test_list_collection_names_skips_other_files(tmp_path: Path) -> None:
    """Listing finds collection files only, skipping Chroma's legacy database."""
    (tmp_path / "chroma.sqlite3").write_bytes(b"")
    (tmp_path / "indexing_settings.json").write_text("{}")
    (tmp_path / "b.sqlite3").write_bytes(b"")
    (tmp_path / "a.sqlite3").write_bytes(b"")
    assert list_collection_names(tmp_path) == ("a", "b")
    assert list_collection_names(tmp_path / "absent") == ()
