"""The owned knowledge vector store: one SQLite file per collection, its own state beside its rows, searched in process."""

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
from typing import TYPE_CHECKING, NoReturn

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
from tests.knowledge_test_support import collection_meta, stored_rows

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.knowledge.corpus_signature import FileSignature

_SIGNATURE = (1, 2, "digest")


def _row(row_id: str, source_path: str, document: str, embedding: list[float], **metadata: object) -> StoredRow:
    return StoredRow(
        id=row_id,
        document=document,
        metadata={"source_path": source_path, "name": source_path.rsplit("/", 1)[-1], **metadata},
        embedding=embedding,
    )


def _write(
    collection: KnowledgeCollection,
    relative_path: str,
    rows: Sequence[StoredRow],
    signature: FileSignature = _SIGNATURE,
) -> None:
    collection.write_file(relative_path, signature, rows)


def _row_paths(collection: KnowledgeCollection) -> set[str]:
    return {row["metadata"]["source_path"] for row in stored_rows(collection)}


@pytest.fixture
def collection(tmp_path: Path) -> KnowledgeCollection:
    """A fresh, empty collection in the test's storage directory."""
    created = KnowledgeCollection(path=tmp_path, name="mindroom_knowledge_docs")
    created.create(collection_meta())
    return created


def test_create_makes_an_empty_collection_file(collection: KnowledgeCollection, tmp_path: Path) -> None:
    """Creating a collection makes its file with no rows, no claims, and the state it was created with."""
    assert collection.exists()
    assert collection.file == tmp_path / "mindroom_knowledge_docs.sqlite3"
    assert stored_rows(collection) == []
    assert collection.files() == {}
    assert collection.claim_counts() == (0, 0)
    assert collection.meta() == collection_meta()
    assert list_collection_names(tmp_path) == ("mindroom_knowledge_docs",)


def test_create_replaces_existing_rows_claims_and_state(collection: KnowledgeCollection) -> None:
    """Creating an existing collection again empties it and records only the new state."""
    _write(collection, "a.md", [_row("1", "a.md", "alpha", [1.0, 0.0])])
    collection.record_failure("b.md")
    replacement = collection_meta({"mode": "semantic"}, rebuild=True)

    collection.create(replacement)

    assert stored_rows(collection) == []
    assert collection.claim_counts() == (0, 0)
    assert collection.meta() == replacement


def test_meta_records_the_lifecycle_and_the_build_kind(collection: KnowledgeCollection) -> None:
    """Writes touch the update time, publishing marks it complete, and a reconcile starts building again.

    A reconcile also records whether the build that took the collection over is a forced rebuild.
    """
    collection.create(collection_meta({"mode": "semantic", "base_id": "docs"}, rebuild=True))
    created = collection.meta()
    _write(collection, "a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])

    collection.mark_complete()
    completed = collection.meta()
    collection.reconcile(drop=[], restamp={}, revision="rev", total_files=3, rebuild=False)
    reconciled = collection.meta()

    assert created.settings == {"mode": "semantic", "base_id": "docs"}
    assert completed.updated_at > created.updated_at
    assert (completed.stage, reconciled.stage) == ("complete", "building")
    assert (reconciled.revision, reconciled.total_files) == ("rev", 3)
    assert (created.rebuild, completed.rebuild, reconciled.rebuild) == (True, True, False)
    assert reconciled.created_at == created.created_at


def test_write_file_replaces_only_that_files_rows_and_claim(collection: KnowledgeCollection) -> None:
    """Writing a file's rows replaces that file's rows and claim, clears its failure, and leaves other files alone."""
    collection.record_failure("a.md")
    _write(
        collection,
        "a.md",
        [_row("a1", "a.md", "old a", [1.0, 0.0]), _row("a2", "a.md", "old a2", [1.0, 0.0])],
        signature=(1, 9, "old"),
    )
    _write(collection, "b.md", [_row("b1", "b.md", "b", [0.0, 1.0])])

    _write(collection, "a.md", [_row("a3", "a.md", "new a", [1.0, 0.0])], signature=(2, 5, "new"))

    hits = collection.search([1.0, 0.0], limit=10)
    assert sorted(hit.content for hit in hits) == ["b", "new a"]
    assert collection.files() == {"a.md": (2, 5, "new"), "b.md": _SIGNATURE}
    assert collection.claim_counts() == (2, 0)


def test_an_empty_file_is_claimed_with_no_rows(collection: KnowledgeCollection) -> None:
    """A file with no chunks is still claimed, so its next refresh does not read it again."""
    _write(collection, "a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])

    _write(collection, "a.md", [], signature=(3, 0, "empty"))

    assert stored_rows(collection) == []
    assert collection.files() == {"a.md": (3, 0, "empty")}


def test_a_recorded_failure_keeps_no_rows_and_no_claim(collection: KnowledgeCollection) -> None:
    """A failed file loses its rows and its claim, is counted once, and a later write clears it."""
    _write(collection, "a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])
    _write(collection, "b.md", [_row("b1", "b.md", "beta", [0.0, 1.0])])

    collection.record_failure("a.md")
    collection.record_failure("a.md")

    assert _row_paths(collection) == {"b.md"}
    assert collection.files() == {"b.md": _SIGNATURE}
    assert collection.claim_counts() == (1, 1)

    _write(collection, "a.md", [_row("a2", "a.md", "alpha again", [1.0, 0.0])])

    assert collection.claim_counts() == (2, 0)


def test_reconcile_drops_rows_claims_and_failures_together(collection: KnowledgeCollection) -> None:
    """Dropped paths lose their rows, claims, and failures across more paths than one statement binds."""
    paths = [f"dir/{index}.md" for index in range(1200)]
    for index, path in enumerate(paths):
        _write(collection, path, [_row(str(index), path, f"doc {index}", [1.0, float(index)])], (index, 1, f"d{index}"))
    collection.record_failure("failed.md")
    collection.record_failure("still-failing.md")
    collection.mark_complete()

    collection.reconcile(
        drop=[*paths[:1100], "failed.md", "never-claimed.md"],
        restamp={paths[1100]: (99, 1, "d1100")},
        revision="rev",
        total_files=101,
        rebuild=False,
    )

    assert collection.files() == {
        paths[1100]: (99, 1, "d1100"),
        **{path: (index, 1, f"d{index}") for index, path in enumerate(paths) if index > 1100},
    }
    assert _row_paths(collection) == set(paths[1100:])
    assert collection.claim_counts() == (100, 1)
    meta = collection.meta()
    assert (meta.stage, meta.revision, meta.total_files) == ("building", "rev", 101)


def test_a_failed_store_call_changes_neither_rows_nor_claims(
    collection: KnowledgeCollection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every mutation is one transaction: one that fails at its last statement leaves rows and claims as they were."""
    _write(collection, "a.md", [_row("a1", "a.md", "old", [1.0, 0.0])], signature=(1, 3, "old"))
    collection.record_failure("b.md")
    before = (stored_rows(collection), collection.files(), collection.claim_counts(), collection.meta())

    def fail(_connection: sqlite3.Connection) -> NoReturn:
        message = "disk I/O error"
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(vector_store_module, "_touch", fail)
    mutations = [
        lambda: _write(collection, "a.md", [_row("a2", "a.md", "new", [0.0, 1.0])], signature=(2, 3, "new")),
        lambda: _write(collection, "b.md", [_row("b1", "b.md", "beta", [0.0, 1.0])]),
        lambda: collection.record_failure("a.md"),
        lambda: collection.reconcile(drop=["a.md", "b.md"], restamp={}, revision="rev", total_files=0, rebuild=False),
        collection.mark_complete,
    ]
    for mutation in mutations:
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            mutation()
    monkeypatch.undo()

    assert (stored_rows(collection), collection.files(), collection.claim_counts(), collection.meta()) == before


def test_imported_rows_claim_only_paths_with_a_well_formed_signature(collection: KnowledgeCollection) -> None:
    """Rows copied from another store claim the signature they carry; rows without a usable one are dropped."""
    signed = {"source_mtime_ns": 5, "source_size": 7, "source_digest": "d"}
    pages = [
        [
            _row("g1", "good.md", "good one", [1.0, 0.0], **signed),
            _row("g2", "good.md", "good two", [1.0, 0.0], **signed),
        ],
        [
            _row("u1", "unsigned.md", "unsigned", [1.0, 0.0]),
            _row("e1", "empty-digest.md", "x", [1.0, 0.0], **{**signed, "source_digest": ""}),
            _row("n1", "number-digest.md", "x", [1.0, 0.0], **{**signed, "source_digest": 3}),
            _row("t1", "text-stamp.md", "x", [1.0, 0.0], **{**signed, "source_mtime_ns": "5"}),
            _row("b1", "bool-stamp.md", "x", [1.0, 0.0], **{**signed, "source_mtime_ns": True}),
            _row("f1", "fractional-size.md", "x", [1.0, 0.0], **{**signed, "source_size": 7.5}),
            _row("s1", "negative-size.md", "x", [1.0, 0.0], **{**signed, "source_size": -1}),
        ],
    ]

    collection.create(collection_meta(), pages=iter(pages))

    assert collection.files() == {"good.md": (5, 7, "d")}
    assert [row["content"] for row in stored_rows(collection)] == ["good one", "good two"]
    assert collection.claim_counts() == (1, 0)


def test_create_copy_is_a_compact_copy_with_the_sources_rows_and_claims(
    collection: KnowledgeCollection,
    tmp_path: Path,
) -> None:
    """A copy carries every row, claim, and keyword entry of its source, without the pages deleted rows left free."""
    for index in range(200):
        path = f"{index}.md"
        _write(collection, path, [_row(path, path, f"word{index} {'x' * 2000}", [1.0, float(index)])], (index, 1, path))
    collection.reconcile(
        drop=[f"{index}.md" for index in range(100)],
        restamp={},
        revision=None,
        total_files=100,
        rebuild=False,
    )
    assert collection.free_fraction() > 0.25
    source_meta = collection.meta()
    copy = KnowledgeCollection(path=tmp_path, name="mindroom_knowledge_docs_candidate_copy")
    meta = collection_meta({"mode": "semantic"}, revision="rev")

    copy.create_copy(collection, meta)

    assert copy.free_fraction() == 0.0
    assert copy.file.stat().st_size < collection.file.stat().st_size
    assert copy.files() == collection.files()
    assert stored_rows(copy) == stored_rows(collection)
    assert copy.meta() == meta
    assert copy.search([0.0, 1.0], limit=1, query="word150")[0].content.startswith("word150 ")
    assert collection.meta() == source_meta


def test_search_ranks_by_cosine_distance_and_shows_each_hit_s_source_reader_keys_and_distance(
    collection: KnowledgeCollection,
) -> None:
    """A hit shows its source, its reader keys, and one distance, without the row keys that serve indexing."""
    indexing = {
        "source_mtime_ns": 1,
        "source_size": 2,
        "source_digest": "digest",
        "content_id": "cid",
        "content_hash": "hash",
        "linked_to": "",
    }
    _write(
        collection,
        "a.md",
        [
            _row("a1", "a.md", "east", [2.0, 0.0], chunk=1, **indexing),
            _row("a2", "a.md", "north", [0.0, 3.0], chunk=2),
            _row("a3", "a.md", "northeast", [1.0, 1.0], chunk=3),
        ],
    )

    hits = collection.search([1.0, 0.1], limit=2)

    assert [hit.content for hit in hits] == ["east", "northeast"]
    east = hits[0]
    assert east.name == "a.md"
    expected = 1 - 1.0 / math.hypot(1.0, 0.1)
    assert east.meta_data == {"source_path": "a.md", "chunk": 1, "distance": pytest.approx(expected, abs=1e-6)}


def test_search_drops_repeated_chunks_of_one_document(collection: KnowledgeCollection) -> None:
    """A chunk stored twice under one name appears once, and the limit still fills."""
    _write(
        collection,
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
    _write(collection, "a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])
    with pytest.raises(ValueError, match="dimension"):
        collection.search([1.0, 0.0, 0.0], limit=1)


def test_search_sees_a_rebuilt_file(collection: KnowledgeCollection) -> None:
    """A rebuilt file is not served from a cached matrix of its previous version."""
    _write(collection, "a.md", [_row("a1", "a.md", "before", [1.0, 0.0])])
    assert [hit.content for hit in collection.search([1.0, 0.0], limit=1)] == ["before"]

    collection.create(collection_meta())
    _write(collection, "a.md", [_row("a1", "a.md", "after", [1.0, 0.0])])

    assert [hit.content for hit in collection.search([1.0, 0.0], limit=1)] == ["after"]


def test_missing_collection_raises_not_found(tmp_path: Path) -> None:
    """Every operation on a missing collection raises, copying from one makes nothing, and deleting it is a no-op."""
    missing = KnowledgeCollection(path=tmp_path, name="gone")
    target = KnowledgeCollection(path=tmp_path, name="target")
    assert not missing.exists()
    operations = [
        lambda: missing.search([1.0], limit=1),
        lambda: _write(missing, "a.md", []),
        lambda: missing.record_failure("a.md"),
        lambda: missing.reconcile(drop=[], restamp={}, revision=None, total_files=0, rebuild=False),
        missing.mark_complete,
        missing.meta,
        missing.files,
        missing.claim_counts,
        lambda: target.create_copy(missing, collection_meta()),
    ]
    for operation in operations:
        with pytest.raises(_CollectionNotFoundError):
            operation()
    missing.delete()
    assert list(tmp_path.iterdir()) == []


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
        broken.meta()
    assert raised.value.sqlite_errorname == "SQLITE_NOTADB"


def test_a_file_of_another_schema_version_does_not_open(tmp_path: Path) -> None:
    """A collection file in a layout this release does not write is refused, so no reader trusts its tables."""
    old = KnowledgeCollection(path=tmp_path, name="old")
    with closing(sqlite3.connect(old.file)) as connection:
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(sqlite3.DatabaseError, match="schema version 1, expected 2"):
        old.files()


def test_list_collection_names_skips_other_files(tmp_path: Path) -> None:
    """Listing finds collection files only, skipping Chroma's legacy database and unfinished builds."""
    (tmp_path / "chroma.sqlite3").write_bytes(b"")
    (tmp_path / "publication.json").write_text("{}")
    (tmp_path / "b.sqlite3").write_bytes(b"")
    (tmp_path / "a.sqlite3").write_bytes(b"")
    (tmp_path / "c.sqlite3.building").write_bytes(b"")
    assert list_collection_names(tmp_path) == ("a", "b")
    assert list_collection_names(tmp_path / "absent") == ()


def test_hybrid_search_surfaces_an_exact_keyword_match_the_embedding_misses(collection: KnowledgeCollection) -> None:
    """A chunk naming the query's identifier rises to the top even when its vector is far from the query's."""
    _write(
        collection,
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
    assert hybrid[0].meta_data["distance"] == pytest.approx(1.0, abs=1e-6)


def test_hybrid_search_stems_words_and_ignores_punctuation(collection: KnowledgeCollection) -> None:
    """Keyword matching stems English words, and a query without words falls back to the vector ranking."""
    _write(
        collection,
        "a.md",
        [_row("a1", "a.md", "unrelated", [1.0, 0.0]), _row("a2", "a.md", "Ada is the lead", [0.0, 1.0])],
    )

    assert collection.search([1.0, 0.0], limit=1, query="who leads?")[0].content == "Ada is the lead"
    assert collection.search([1.0, 0.0], limit=1, query="?!")[0].content == "unrelated"


def test_a_write_killed_mid_transaction_rolls_back_on_the_next_open(collection: KnowledgeCollection) -> None:
    """A refresh killed mid-write leaves a hot journal; the next open rolls it back, rows and claims alike."""
    _write(collection, "a.md", [_row("a1", "a.md", "kept", [1.0, 0.0])])
    crash = (
        "import os, sqlite3, sys\n"
        "connection = sqlite3.connect(sys.argv[1])\n"
        "connection.execute('BEGIN IMMEDIATE')\n"
        "connection.executemany('INSERT INTO chunks(id, source_path, document, metadata, embedding) "
        "VALUES (?, ?, ?, ?, ?)', [(str(i), 'b.md', 'x' * 1000, '{}', bytes(8)) for i in range(3000)])\n"
        "connection.execute(\"INSERT INTO files VALUES ('b.md', 1, 2, 'digest')\")\n"
        "connection.execute(\"DELETE FROM chunks WHERE id = 'a1'\")\n"
        "connection.execute(\"DELETE FROM files WHERE path = 'a.md'\")\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", crash, str(collection.file)], check=True)
    assert collection.file.with_name(f"{collection.file.name}-journal").exists()

    assert collection.files() == {"a.md": _SIGNATURE}
    assert _row_paths(collection) == {"a.md"}
    assert [hit.content for hit in collection.search([1.0, 0.0], limit=5)] == ["kept"]


def test_writes_reject_vectors_of_another_width(collection: KnowledgeCollection) -> None:
    """A collection holds one vector width, so an embedder that changed width fails the write instead of publishing."""
    _write(collection, "a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])

    with pytest.raises(ValueError, match="dimensions"):
        _write(collection, "b.md", [_row("b1", "b.md", "beta", [1.0, 0.0, 0.0])])
    with pytest.raises(ValueError, match="dimensions"):
        _write(collection, "c.md", [_row("c1", "c.md", "c", [1.0]), _row("c2", "c.md", "c", [1.0, 0.0])])
    _write(collection, "a.md", [_row("a2", "a.md", "alpha again", [0.0, 1.0])])

    assert _row_paths(collection) == {"a.md"}
    assert collection.files() == {"a.md": _SIGNATURE}


def test_concurrent_cold_searches_load_the_matrix_once(
    collection: KnowledgeCollection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Searches that all miss the cache share one matrix load instead of each allocating a copy."""
    _write(collection, "a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])
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
    _write(collection, "a.md", [_row(f"a{index}", "a.md", "alpha", [1.0, 0.0]) for index in range(3)])
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
    _write(collection, "a.md", [_row(str(index), "a.md", "doc", vectors[index].tolist()) for index in range(rows)])

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
    _write(
        collection,
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
    _write(
        collection,
        "a.md",
        [
            _row("a1", "a.md", "configurations for the café", [1.0, 0.0]),
            _row("a2", "a.md", "configurations for the café, running", [0.9, 0.1]),
            _row("a3", "a.md", "configurations in a cafe", [0.0, 1.0]),
        ],
    )
    with closing(sqlite3.connect(collection.file)) as connection:
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
        collection.create(collection_meta())
        _write(
            collection,
            "a.md",
            [_row(f"{collection.name}{index}", "a.md", f"doc {index}", [1.0, float(index)]) for index in range(count)],
        )
    monkeypatch.setattr(vector_store_module, "_MATRIX_CACHE_BYTES", 100)
    # Matrices other tests on this worker left cached would otherwise count against the budget.
    monkeypatch.setattr(vector_store_module, "_index_cache", OrderedDict())
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
        collection.create(collection_meta())
        _write(
            collection,
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
    old.create(collection_meta())
    _write(old, "a.md", [_row(f"o{index}", "a.md", "old", [1.0, float(index)]) for index in range(40)])
    old.search([1.0, 0.0], limit=1)
    old.delete()
    live = KnowledgeCollection(path=tmp_path, name="live")
    live.create(collection_meta())
    _write(live, "a.md", [_row("l1", "a.md", "live", [1.0, 0.0])])
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


@pytest.mark.parametrize("change", ["rewrite", "drop", "failure"])
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
    _write(collection, "a.md", [_row("a1", "a.md", "alpha", [1.0, 0.0])])
    _write(collection, "b.md", [_row("b1", "b.md", "beta", [0.0, 1.0])])
    assert [hit.content for hit in collection.search([1.0, 0.0], limit=2)] == ["alpha", "beta"]

    if change == "rewrite":
        _write(collection, "a.md", [_row("a2", "a.md", "alpha again", [1.0, 0.1])])
        expected = ["alpha again", "beta"]
    elif change == "drop":
        collection.reconcile(drop=["a.md"], restamp={}, revision=None, total_files=1, rebuild=False)
        expected = ["beta"]
    else:
        collection.record_failure("a.md")
        expected = ["beta"]

    assert [hit.content for hit in collection.search([1.0, 0.0], limit=2)] == expected
    assert len(vector_store_module._index_cache) == 1, "the outdated matrix stayed cached beside the new one"


def test_a_matrix_load_reads_its_file_ahead_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first search reads the collection file front to back before loading its matrix; cached searches do not."""
    monkeypatch.setattr(vector_store_module, "_index_cache", OrderedDict())
    collection = KnowledgeCollection(path=tmp_path, name="docs")
    collection.create(collection_meta())
    _write(collection, "a.md", [_row("a", "a.md", "alpha", [1.0, 0.0])])
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
