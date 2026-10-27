"""The owned knowledge vector store: one SQLite file per collection, searched in process."""

from __future__ import annotations

import math
import sqlite3
import subprocess
import sys
import time
import tracemalloc
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import NoReturn

import numpy as np
import pytest

import mindroom.knowledge.vector_store as vector_store_module
from mindroom.knowledge.vector_store import (
    KnowledgeCollection,
    StoredRow,
    _CollectionNotFoundError,
    _KnowledgeIndexTooLargeError,
    list_collection_names,
)


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


def test_hybrid_search_surfaces_an_exact_keyword_match_the_embedding_misses(collection: KnowledgeCollection) -> None:
    """A chunk naming the query's identifier rises to the top even when its vector is far from the query's."""
    collection.write_source_rows(
        "a.md",
        [
            _row("a1", "a.md", "general setup advice", [1.0, 0.0]),
            _row("a2", "a.md", "more general advice", [0.99, 0.1]),
            _row("a3", "a.md", "Set MINDROOM_STORAGE_PATH to move the data directory", [0.0, 1.0]),
        ],
    )

    dense = collection.search([1.0, 0.0], limit=3)
    hybrid = collection.search([1.0, 0.0], limit=3, query="where does MINDROOM_STORAGE_PATH point")

    assert dense[-1].content.startswith("Set MINDROOM_STORAGE_PATH")
    assert hybrid[0].content.startswith("Set MINDROOM_STORAGE_PATH")
    assert hybrid[0].meta_data["distances"] == pytest.approx(1.0, abs=1e-6)


def test_hybrid_search_stems_words_and_ignores_punctuation(collection: KnowledgeCollection) -> None:
    """Keyword matching stems English words, and a query without words falls back to the vector ranking."""
    collection.write_source_rows(
        "a.md",
        [_row("a1", "a.md", "unrelated", [1.0, 0.0]), _row("a2", "a.md", "Ada is the lead", [0.0, 1.0])],
    )

    assert collection.search([1.0, 0.0], limit=1, query="who leads?")[0].content == "Ada is the lead"
    assert collection.search([1.0, 0.0], limit=1, query="?!")[0].content == "unrelated"


def test_a_write_killed_mid_transaction_rolls_back_on_the_next_open(collection: KnowledgeCollection) -> None:
    """A refresh killed mid-write leaves a hot journal; the next probe rolls it back and keeps the committed rows."""
    collection.write_source_rows("a.md", [_row("a1", "a.md", "kept", [1.0, 0.0])])
    crash = (
        "import os, sqlite3, sys\n"
        "connection = sqlite3.connect(sys.argv[1])\n"
        "connection.execute('BEGIN IMMEDIATE')\n"
        "connection.executemany('INSERT INTO chunks(id, source_path, document, metadata, embedding) "
        "VALUES (?, ?, ?, ?, ?)', [(str(i), 'b.md', 'x' * 1000, '{}', bytes(8)) for i in range(3000)])\n"
        "connection.execute(\"DELETE FROM chunks WHERE id = 'a1'\")\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", crash, str(collection.file)], check=True)
    assert collection.file.with_name(f"{collection.file.name}-journal").exists()

    assert collection.has_rows()
    assert collection.paths_with_vectors(["a.md", "b.md"]) == {"a.md"}
    assert [hit.content for hit in collection.search([1.0, 0.0], limit=5)] == ["kept"]


def test_writes_reject_vectors_of_another_width(collection: KnowledgeCollection) -> None:
    """A collection holds one vector width, so an embedder that changed width fails the write instead of publishing."""
    collection.write_source_rows("a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])

    with pytest.raises(ValueError, match="dimensions"):
        collection.write_source_rows("b.md", [_row("b1", "b.md", "beta", [1.0, 0.0, 0.0])])
    with pytest.raises(ValueError, match="dimensions"):
        collection.write_source_rows("c.md", [_row("c1", "c.md", "c", [1.0]), _row("c2", "c.md", "c", [1.0, 0.0])])
    collection.write_source_rows("a.md", [_row("a2", "a.md", "alpha again", [0.0, 1.0])])
    assert collection.paths_with_vectors(["a.md", "b.md", "c.md"]) == {"a.md"}


def test_concurrent_cold_searches_load_the_matrix_once(
    collection: KnowledgeCollection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Searches that all miss the cache share one matrix load instead of each allocating a copy."""
    collection.write_source_rows("a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])
    loads: list[int] = []
    real_load = vector_store_module._load_vector_index

    def slow_load(connection: sqlite3.Connection, max_matrix_bytes: int | None) -> object:
        loads.append(1)
        time.sleep(0.2)
        return real_load(connection, max_matrix_bytes)

    monkeypatch.setattr(vector_store_module, "_load_vector_index", slow_load)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _index: collection.search([1.0, 0.0], limit=1), range(8)))

    assert loads == [1]
    assert all(result[0].content == "alpha" for result in results)


def _refuse_allocation(*_args: object, **_kwargs: object) -> NoReturn:
    message = "a matrix past the search's limit was allocated"
    raise AssertionError(message)


def test_a_search_refuses_vectors_past_its_limit_before_loading_them(
    collection: KnowledgeCollection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collection whose matrix exceeds the search's byte limit raises before allocating, reading ahead, or caching it."""
    monkeypatch.setattr(vector_store_module, "_index_cache", OrderedDict())
    reads: list[Path] = []
    monkeypatch.setattr(vector_store_module, "_read_ahead", reads.append)
    collection.write_source_rows("a.md", [_row(f"a{index}", "a.md", "alpha", [1.0, 0.0]) for index in range(3)])
    matrix_bytes = 3 * 2 * 4

    with monkeypatch.context() as allocation:
        allocation.setattr(np, "empty", _refuse_allocation)
        with pytest.raises(_KnowledgeIndexTooLargeError):
            collection.search([1.0, 0.0], limit=1, max_matrix_bytes=matrix_bytes - 1)
    assert not vector_store_module._index_cache

    assert collection.search([1.0, 0.0], limit=1, max_matrix_bytes=matrix_bytes)[0].content == "alpha"
    assert not reads, "a limited search read its file ahead"


def test_loading_a_matrix_allocates_no_second_matrix(collection: KnowledgeCollection) -> None:
    """Normalizing a loaded matrix needs one scratch block, not a temporary as large as the matrix."""
    rows = 8 * vector_store_module._NORMALIZE_ROWS
    vectors = np.random.default_rng(0).standard_normal((rows, 64), dtype=np.float32)
    collection.add_rows([_row(str(index), f"{index}.md", "doc", vectors[index].tolist()) for index in range(rows)])

    with closing(sqlite3.connect(collection.file)) as connection:
        tracemalloc.start()
        try:
            index = vector_store_module._load_vector_index(connection, None)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert peak < 1.5 * index.matrix.nbytes
    np.testing.assert_allclose(np.linalg.norm(index.matrix, axis=1), 1.0, rtol=1e-5)


@pytest.mark.parametrize("name", ["", "/etc/elsewhere", "../outside", "a/b", ".hidden", "with space"])
def test_collection_names_must_be_one_plain_component(tmp_path: Path, name: str) -> None:
    """A name read from worker-writable metadata can never become a path outside the base's directory."""
    with pytest.raises(ValueError, match="Invalid knowledge collection name"):
        KnowledgeCollection(path=tmp_path, name=name)


def test_top_k_keeps_the_earliest_positions_among_ties_at_the_cutoff() -> None:
    """Equal scores at the cutoff are taken in position order, so tied chunks rank the same way every time."""
    scores = np.array([0.5, 0.9, 0.5, 0.5, 0.9, 0.5], dtype=np.float32)

    assert vector_store_module._top_indices(scores, 3) == [1, 4, 0]
    assert vector_store_module._top_indices(scores, 6) == [1, 4, 0, 2, 3, 5]


def test_keyword_ranking_skips_words_most_chunks_contain(collection: KnowledgeCollection) -> None:
    """A word in more than half the chunks carries no keyword weight, so it cannot reorder the vector ranking."""
    collection.write_source_rows(
        "a.md",
        [
            _row("a1", "a.md", "the first note", [1.0, 0.0]),
            _row("a2", "a.md", "the the the second note", [0.9, 0.1]),
            _row("a3", "a.md", "the zebra", [0.0, 1.0]),
        ],
    )

    assert [hit.content for hit in collection.search([1.0, 0.0], limit=3, query="the")] == [
        "the first note",
        "the the the second note",
        "the zebra",
    ]
    assert collection.search([1.0, 0.0], limit=1, query="the zebra")[0].content == "the zebra"


def test_keyword_ranking_skips_inflections_of_words_most_chunks_contain(collection: KnowledgeCollection) -> None:
    """Common-word skipping sees the words as the keyword index stores them, stemmed and without accents."""
    collection.write_source_rows(
        "a.md",
        [
            _row("a1", "a.md", "configurations for the café", [1.0, 0.0]),
            _row("a2", "a.md", "configurations for the café, running", [0.9, 0.1]),
            _row("a3", "a.md", "configurations in a cafe", [0.0, 1.0]),
        ],
    )
    connection = sqlite3.connect(collection.file)

    assert vector_store_module._keyword_rowids(connection, "configuration cafes", 10, chunk_count=3) == []
    assert vector_store_module._keyword_rowids(connection, "configuration runs", 10, chunk_count=3) == [2]


def test_the_largest_index_stays_cached_when_searches_alternate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base larger than the cache budget is not reloaded when searches alternate between it and a smaller one."""
    large = KnowledgeCollection(path=tmp_path / "large_base", name="large")
    small = KnowledgeCollection(path=tmp_path / "small_base", name="small")
    for collection, count in ((large, 40), (small, 2)):
        collection.reset()
        collection.write_source_rows(
            "a.md",
            [_row(f"{collection.name}{index}", "a.md", f"doc {index}", [1.0, float(index)]) for index in range(count)],
        )
    monkeypatch.setattr(vector_store_module, "_MATRIX_CACHE_BYTES", 100)
    loads: list[str] = []
    real_load = vector_store_module._load_vector_index
    monkeypatch.setattr(
        vector_store_module,
        "_load_vector_index",
        lambda connection, max_matrix_bytes: loads.append("load") or real_load(connection, max_matrix_bytes),
    )

    for _round in range(3):
        large.search([1.0, 0.0], limit=1)
        small.search([1.0, 0.0], limit=1)

    assert len(loads) == 2


def test_the_published_matrix_stays_cached_beside_the_collection_it_retired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collection a refresh retired stays on disk unchanged, but its matrix must not stay cached and evict the new one.

    Both are over the budget and the same size, so whichever counts as the largest is the only one kept.
    """
    monkeypatch.setattr(vector_store_module, "_MATRIX_CACHE_BYTES", 1)
    monkeypatch.setattr(vector_store_module, "_index_cache", OrderedDict())
    retired = KnowledgeCollection(path=tmp_path, name="retired")
    published = KnowledgeCollection(path=tmp_path, name="published")
    for collection in (retired, published):
        collection.reset()
        collection.write_source_rows(
            "a.md",
            [_row(f"{collection.name}{index}", "a.md", collection.name, [1.0, float(index)]) for index in range(40)],
        )
    retired.search([1.0, 0.0], limit=1)
    loads: list[str] = []
    real_load = vector_store_module._load_vector_index
    monkeypatch.setattr(
        vector_store_module,
        "_load_vector_index",
        lambda connection, max_matrix_bytes: loads.append("load") or real_load(connection, max_matrix_bytes),
    )

    for _search in range(3):
        assert published.search([1.0, 0.0], limit=1)[0].content == "published"

    assert loads == ["load"]
    assert [Path(key[0]).name for key in vector_store_module._index_cache] == ["published.sqlite3"]


def test_a_republished_file_replaces_its_stale_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrices of deleted or rewritten files leave the cache, so a stale large one cannot crowd out the live index."""
    monkeypatch.setattr(vector_store_module, "_MATRIX_CACHE_BYTES", 1)
    monkeypatch.setattr(vector_store_module, "_index_cache", OrderedDict())
    # Another base's file, so only its deletion, not the directory rule, can drop its matrix.
    old = KnowledgeCollection(path=tmp_path / "deleted_base", name="old")
    old.reset()
    old.write_source_rows("a.md", [_row(f"o{index}", "a.md", "old", [1.0, float(index)]) for index in range(40)])
    old.search([1.0, 0.0], limit=1)
    old.delete()
    live = KnowledgeCollection(path=tmp_path, name="live")
    live.reset()
    live.write_source_rows("a.md", [_row("l1", "a.md", "live", [1.0, 0.0])])
    loads: list[str] = []
    real_load = vector_store_module._load_vector_index
    monkeypatch.setattr(
        vector_store_module,
        "_load_vector_index",
        lambda connection, max_matrix_bytes: loads.append("load") or real_load(connection, max_matrix_bytes),
    )

    for _search in range(3):
        assert live.search([1.0, 0.0], limit=1)[0].content == "live"

    assert loads == ["load"]
    assert [Path(key[0]).name for key in vector_store_module._index_cache] == ["live.sqlite3"]


@pytest.mark.parametrize("change", ["rewrite", "delete"])
def test_a_file_written_again_within_one_timestamp_tick_is_searched_afresh(
    collection: KnowledgeCollection,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A retired collection taken over and rewritten in place never serves the matrix cached before the rewrite.

    The file's identity and timestamps are pinned, as on a filesystem whose timestamps are too coarse to tell the two
    versions apart, so only the file's write generation can.
    """
    monkeypatch.setattr(vector_store_module, "_index_cache", OrderedDict())
    monkeypatch.setattr(
        vector_store_module,
        "_file_version",
        lambda file: (str(file), 0, 0, 0, 0) if file.exists() else None,
    )
    collection.write_source_rows("a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])
    collection.write_source_rows("b.md", [_row("b1", "b.md", "beta", [0.0, 1.0])])
    assert [hit.content for hit in collection.search([1.0, 0.0], limit=2)] == ["alpha", "beta"]

    if change == "rewrite":
        collection.write_source_rows("a.md", [_row("a2", "a.md", "alpha again", [1.0, 0.1])])
        expected = ["alpha again", "beta"]
    else:
        collection.delete_source_paths(["a.md"])
        expected = ["beta"]

    assert [hit.content for hit in collection.search([1.0, 0.0], limit=2)] == expected
    assert len(vector_store_module._index_cache) == 1, "the outdated matrix stayed cached beside the new one"


def test_a_matrix_load_reads_its_file_ahead_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first search reads the collection file front to back before loading its matrix; cached searches do not."""
    monkeypatch.setattr(vector_store_module, "_index_cache", OrderedDict())
    collection = KnowledgeCollection(path=tmp_path, name="docs")
    collection.reset()
    collection.write_source_rows("a.md", [_row("a", "a.md", "alpha", [1.0, 0.0])])
    calls: list[str] = []
    real_read_ahead = vector_store_module._read_ahead
    real_load = vector_store_module._load_vector_index
    monkeypatch.setattr(
        vector_store_module,
        "_read_ahead",
        lambda file: calls.append(f"read {file.name}") or real_read_ahead(file),
    )
    monkeypatch.setattr(
        vector_store_module,
        "_load_vector_index",
        lambda connection, max_matrix_bytes: calls.append("load") or real_load(connection, max_matrix_bytes),
    )

    for _search in range(2):
        assert collection.search([1.0, 0.0], limit=1)[0].content == "alpha"

    assert calls == ["read docs.sqlite3", "load"]
