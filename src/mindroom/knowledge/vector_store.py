"""One knowledge collection stored as one SQLite file: chunk rows, their vectors, and a keyword index.

A refresh builds a separate candidate file and publication only changes which file the base's metadata names, so a
published file is never written again. Readers therefore never wait on a writer, and a vector matrix loaded from a
file stays valid for as long as that file exists.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
import threading
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.knowledge.read_protocol import SOURCE_PATH_KEY, SearchHit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    import numpy as np

#: Bump together with ``indexing_config._VECTOR_STORE_FORMAT``, which makes every semantic index rebuild.
_SCHEMA_VERSION = 1
_FILE_SUFFIX = ".sqlite3"
#: The file Chroma kept in a base's storage directory before MindRoom owned this store.
LEGACY_CHROMA_FILE = "chroma.sqlite3"
#: Source paths bound in one statement, well under every SQLite build's variable limit.
_PATH_BATCH = 500
#: Chunks each ranking contributes to a hybrid search before fusion.
_FUSION_CANDIDATES = 50
#: Reciprocal rank fusion's rank offset; 60 is the value the method was published with.
_RRF_K = 60
#: Query words matched in the keyword index, so a pasted document cannot make one search scan everything.
_MAX_KEYWORD_TERMS = 32
_WORD = re.compile(r"\w+")
#: Vector matrices kept loaded across searches, bounded by their total size.
_MATRIX_CACHE_BYTES = 512 * 1024 * 1024
_SCHEMA = """
CREATE TABLE chunks (
    rowid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    document TEXT NOT NULL,
    metadata TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX chunks_source_path ON chunks(source_path);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    document,
    content='chunks',
    content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, document) VALUES (new.rowid, new.document);
END;
CREATE TRIGGER chunks_fts_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, document) VALUES ('delete', old.rowid, old.document);
END;
"""


class _CollectionNotFoundError(LookupError):
    """The collection's file does not exist."""


@dataclass(frozen=True)
class StoredRow:
    """One chunk as stored: its row id, text, metadata, and vector."""

    id: str
    document: str
    metadata: Mapping[str, Any]
    embedding: Sequence[float]


@dataclass(frozen=True)
class KnowledgeCollection:
    """One named collection, the file ``<name>.sqlite3`` in a knowledge base's storage directory."""

    path: Path
    name: str

    @property
    def file(self) -> Path:
        """Return the collection's database file."""
        return self.path / f"{self.name}{_FILE_SUFFIX}"

    def exists(self) -> bool:
        """Return whether the collection's file exists."""
        return self.file.is_file()

    def reset(self) -> None:
        """Replace the collection with an empty one; the new file appears whole or not at all."""
        self.path.mkdir(parents=True, exist_ok=True)
        building = self.file.with_name(f"{self.file.name}.building")
        building.unlink(missing_ok=True)
        with closing(sqlite3.connect(building)) as connection:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        # The old file's journal must not outlive it, or SQLite would roll it back into the new file.
        self.file.with_name(f"{self.file.name}-journal").unlink(missing_ok=True)
        building.replace(self.file)

    def delete(self) -> None:
        """Delete the collection's file, treating an already-absent one as deleted."""
        self.file.with_name(f"{self.file.name}-journal").unlink(missing_ok=True)
        self.file.unlink(missing_ok=True)

    def write_source_rows(self, relative_path: str, rows: Sequence[StoredRow]) -> None:
        """Replace one source file's rows with ``rows`` in one transaction."""
        with closing(self._connect(write=True)) as connection, connection:
            connection.execute("DELETE FROM chunks WHERE source_path = ?", (relative_path,))
            _insert_rows(connection, rows)

    def copy_rows(
        self,
        source: KnowledgeCollection,
        *,
        select: Callable[[Mapping[str, Any]], bool],
        page_rows: int = 500,
    ) -> None:
        """Copy the rows of ``source`` whose metadata ``select`` accepts, verbatim, in pages of ``page_rows``."""
        with closing(source._connect(write=False)) as reader, closing(self._connect(write=True)) as writer:
            last_rowid = 0
            while page := reader.execute(
                "SELECT rowid, id, source_path, document, metadata, embedding FROM chunks "
                "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (last_rowid, page_rows),
            ).fetchall():
                last_rowid = page[-1][0]
                kept = [row[1:] for row in page if select(json.loads(row[4]))]
                with writer:
                    writer.executemany(
                        "INSERT INTO chunks(id, source_path, document, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
                        kept,
                    )

    def delete_source_paths(self, relative_paths: Sequence[str]) -> None:
        """Delete every row of the given source paths."""
        with closing(self._connect(write=True)) as connection, connection:
            for batch in _batches(relative_paths):
                connection.execute(f"DELETE FROM chunks WHERE source_path IN ({_placeholders(batch)})", batch)  # noqa: S608

    def paths_with_vectors(self, relative_paths: Sequence[str]) -> set[str]:
        """Return which of the given source paths have at least one row."""
        if not relative_paths:
            return set()
        found: set[str] = set()
        with closing(self._connect(write=False)) as connection:
            for batch in _batches(relative_paths):
                found.update(
                    row[0]
                    for row in connection.execute(
                        f"SELECT DISTINCT source_path FROM chunks WHERE source_path IN ({_placeholders(batch)})",  # noqa: S608
                        batch,
                    )
                )
        return found

    def has_rows(self) -> bool:
        """Return whether the collection holds any row."""
        with closing(self._connect(write=False)) as connection:
            return connection.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None

    def search(self, embedding: Sequence[float], *, limit: int, query: str | None = None) -> list[SearchHit]:
        """Return the ``limit`` best chunks, each document's repeats dropped.

        Chunks rank by cosine distance to ``embedding``; with ``query`` text, that ranking is fused with an FTS5 BM25
        ranking of the query's words by reciprocal rank fusion, so exact names and identifiers the embedding blurs
        still surface. Every hit reports its cosine distance either way.
        """
        import numpy as np  # noqa: PLC0415 - only searching needs numpy, which installs on first use.

        with closing(self._connect(write=False)) as connection:
            index = _vector_index(connection, self.file)
            if not len(index.rowids):
                return []
            query_vector = np.asarray(embedding, dtype=np.float32)
            if query_vector.shape != (index.matrix.shape[1],):
                message = (
                    f"Query embedding dimension {query_vector.size} does not match the index dimension "
                    f"{index.matrix.shape[1]}"
                )
                raise ValueError(message)
            query_norm = float(np.linalg.norm(query_vector))
            if query_norm:
                query_vector /= query_norm
            scores = index.matrix @ query_vector
            candidates = min(len(scores), max(limit * 2, _FUSION_CANDIDATES))
            ranked = [int(index.rowids[position]) for position in _top_indices(scores, candidates)]
            if query is not None:
                ranked = _reciprocal_rank_fusion(ranked, _keyword_rowids(connection, query, candidates))
            positions = np.searchsorted(index.rowids, ranked)
            return _hits(
                connection,
                [(rowid, 1.0 - float(scores[position])) for rowid, position in zip(ranked, positions, strict=True)],
                limit=limit,
            )

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        """Open the existing collection file, read-write or read-only, after checking its schema version."""
        if not self.file.is_file():
            message = f"Knowledge collection {self.name!r} does not exist"
            raise _CollectionNotFoundError(message)
        connection = sqlite3.connect(
            f"{self.file.resolve().as_uri()}?mode={'rw' if write else 'ro'}",
            uri=True,
            timeout=30.0,
        )
        try:
            _check_schema_version(connection, self.name)
        except BaseException:
            connection.close()
            raise
        return connection


def _check_schema_version(connection: sqlite3.Connection, name: str) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != _SCHEMA_VERSION:
        message = f"Knowledge collection {name!r} has schema version {version}, expected {_SCHEMA_VERSION}"
        raise sqlite3.DatabaseError(message)


def list_collection_names(storage_path: Path) -> tuple[str, ...]:
    """Return the names of the collections stored in one directory, sorted."""
    try:
        entries = tuple(storage_path.iterdir())
    except FileNotFoundError:
        return ()
    return tuple(
        sorted(
            entry.name.removesuffix(_FILE_SUFFIX)
            for entry in entries
            if entry.name.endswith(_FILE_SUFFIX) and entry.name != LEGACY_CHROMA_FILE and entry.is_file()
        ),
    )


def _insert_rows(connection: sqlite3.Connection, rows: Iterable[StoredRow]) -> None:
    connection.executemany(
        "INSERT INTO chunks(id, source_path, document, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
        (
            (
                row.id,
                row.metadata[SOURCE_PATH_KEY],
                row.document,
                json.dumps(dict(row.metadata), sort_keys=True, ensure_ascii=False),
                struct.pack(f"<{len(row.embedding)}f", *row.embedding),
            )
            for row in rows
        ),
    )


def _batches(values: Sequence[str]) -> Iterator[list[str]]:
    for start in range(0, len(values), _PATH_BATCH):
        yield list(values[start : start + _PATH_BATCH])


def _placeholders(values: Sequence[object]) -> str:
    return ", ".join("?" * len(values))


def _top_indices(scores: np.ndarray, count: int) -> list[int]:
    """Return the positions of the ``count`` highest scores, highest first, ties in position order."""
    import numpy as np  # noqa: PLC0415

    if count <= 0:
        return []
    candidates = np.argpartition(-scores, count - 1)[:count] if count < len(scores) else np.arange(len(scores))
    return [int(position) for position in candidates[np.lexsort((candidates, -scores[candidates]))]]


def _keyword_rowids(connection: sqlite3.Connection, query: str, limit: int) -> list[int]:
    """Return the rowids of the ``limit`` chunks FTS5 ranks best by BM25 for any of the query's words.

    Equal scores order by source path, then chunk order: rowids alone would follow which file a refresh happened to
    write first.
    """
    words = list(dict.fromkeys(word.lower() for word in _WORD.findall(query)))[:_MAX_KEYWORD_TERMS]
    if not words:
        return []
    expression = " OR ".join(f'"{word}"' for word in words)
    return [
        rowid
        for (rowid,) in connection.execute(
            "SELECT chunks.rowid FROM chunks_fts JOIN chunks ON chunks.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY chunks_fts.rank, chunks.source_path, chunks.rowid LIMIT ?",
            (expression, limit),
        )
    ]


def _reciprocal_rank_fusion(*rankings: Sequence[int]) -> list[int]:
    """Merge rankings by summed ``1 / (k + rank)``; ties keep the earlier ranking's order."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, rowid in enumerate(ranking, start=1):
            fused[rowid] = fused.get(rowid, 0.0) + 1.0 / (_RRF_K + rank)
    return sorted(fused, key=lambda rowid: -fused[rowid])


def _hits(connection: sqlite3.Connection, ranked: Sequence[tuple[int, float]], *, limit: int) -> list[SearchHit]:
    """Build hits for ranked ``(rowid, cosine distance)`` pairs, dropping a document's repeated chunks."""
    rows = {
        rowid: (document, metadata)
        for rowid, document, metadata in connection.execute(
            f"SELECT rowid, document, metadata FROM chunks WHERE rowid IN ({_placeholders(ranked)})",  # noqa: S608
            [rowid for rowid, _distance in ranked],
        )
    }
    hits: list[SearchHit] = []
    seen: set[tuple[str | None, str]] = set()
    for rowid, distance in ranked:
        document, metadata_json = rows[rowid]
        meta_data = json.loads(metadata_json)
        name = meta_data.pop("name", None)
        meta_data.pop("content_id", None)
        # TODO(simplify): Hits show the model the stored source signature, Agno's empty `linked_to`, `content_hash`, and
        # the cosine distance twice, once as a `similarity_score` that falls as matches improve. Show the source, the
        # chunk, and one distance. Models would read shorter hits; the knowledge goldens pin these keys.
        meta_data["distances"] = distance
        meta_data["similarity_score"] = distance
        hit = SearchHit(content=document, name=None if name is None else str(name), meta_data=meta_data)
        if (hit.name, hit.content) in seen:
            continue
        seen.add((hit.name, hit.content))
        hits.append(hit)
        if len(hits) == limit:
            break
    return hits


@dataclass(frozen=True)
class _VectorIndex:
    """One file's row ids and unit-length float32 vectors, in matching order."""

    rowids: np.ndarray
    matrix: np.ndarray

    @property
    def nbytes(self) -> int:
        return int(self.rowids.nbytes + self.matrix.nbytes)


_index_cache: OrderedDict[tuple[str, int, int, int, int], _VectorIndex] = OrderedDict()
_index_cache_lock = threading.Lock()


def _vector_index(connection: sqlite3.Connection, file: Path) -> _VectorIndex:
    """Return the file's vectors, loading them once per file version and keeping recent ones within the cache budget."""
    status = file.stat()
    key = (str(file), status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
    with _index_cache_lock:
        index = _index_cache.get(key)
        if index is not None:
            _index_cache.move_to_end(key)
            return index
    index = _load_vector_index(connection)
    if index.nbytes <= _MATRIX_CACHE_BYTES:
        with _index_cache_lock:
            _index_cache[key] = index
            cached_bytes = sum(cached.nbytes for cached in _index_cache.values())
            while cached_bytes > _MATRIX_CACHE_BYTES:
                _, evicted = _index_cache.popitem(last=False)
                cached_bytes -= evicted.nbytes
    return index


def _load_vector_index(connection: sqlite3.Connection) -> _VectorIndex:
    import numpy as np  # noqa: PLC0415

    # One read transaction, so the count and the rows come from the same snapshot.
    connection.execute("BEGIN")
    try:
        count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        first = connection.execute("SELECT length(embedding) FROM chunks LIMIT 1").fetchone()
        dimension = 0 if first is None else first[0] // 4
        rowids = np.empty(count, dtype=np.int64)
        matrix = np.empty((count, dimension), dtype=np.float32)
        rows = connection.execute("SELECT rowid, embedding FROM chunks ORDER BY rowid")
        for position, (rowid, blob) in enumerate(rows):
            if len(blob) != dimension * 4:
                message = "Knowledge collection holds embeddings of different dimensions"
                raise ValueError(message)
            rowids[position] = rowid
            matrix[position] = np.frombuffer(blob, dtype="<f4")
    finally:
        connection.rollback()
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix /= norms
    return _VectorIndex(rowids=rowids, matrix=matrix)
