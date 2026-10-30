"""One knowledge collection stored as one SQLite file: chunk rows, their vectors, a keyword index, and its own state.

The file also holds which source files its rows claim (``files``), which ones failed (``failures``), and its settings
and lifecycle (``meta``), each changed in the same transaction as the rows it describes, so the claims are exact
whenever the file is readable. A new file appears whole or not at all.

Publication only changes which file the base's publication names, so readers never wait on a writer. A file is written
again only once it is no longer published and its readers have drained: a refresh may take a retired collection over
as its next candidate. Loaded vector matrices are therefore keyed by the file's write generation, not only its path.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
import threading
from collections import OrderedDict
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.knowledge.index_metadata import utc_now
from mindroom.knowledge.read_protocol import (
    DISTANCE_KEY,
    SOURCE_DIGEST_KEY,
    SOURCE_MTIME_NS_KEY,
    SOURCE_PATH_KEY,
    SOURCE_SIZE_KEY,
    SearchHit,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    import numpy as np

    from mindroom.knowledge.corpus_signature import FileSignature

#: The file's table layout. A layout change migrates existing files in place (``legacy_index_state``); a change to what
#: the vectors mean bumps ``indexing_config._VECTOR_STORE_FORMAT`` instead, which rebuilds every semantic index.
SCHEMA_VERSION = 2
_FILE_SUFFIX = ".sqlite3"
#: The file Chroma kept in a base's storage directory before MindRoom owned this store; never a collection.
_CHROMA_FILE = "chroma.sqlite3"
#: Source paths bound in one statement, well under every SQLite build's variable limit.
_PATH_BATCH = 500
#: Chunks each ranking contributes to a hybrid search before fusion.
_FUSION_CANDIDATES = 50
#: Reciprocal rank fusion's rank offset; 60 is the value the method was published with.
_RRF_K = 60
#: Query words matched in the keyword index, so a pasted document cannot make one search scan everything.
_MAX_KEYWORD_TERMS = 32
_WORD = re.compile(r"\w+")
#: What a collection name may be: one plain file-name component, the same characters Chroma allowed.
_COLLECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: Vector matrices kept loaded across searches, bounded by their total size apart from the largest one.
_MATRIX_CACHE_BYTES = 512 * 1024 * 1024
#: Bytes per read while reading a collection file ahead of its matrix load.
_READ_AHEAD_CHUNK_BYTES = 8 * 1024 * 1024
#: Rows normalized at a time while a matrix loads.
_NORMALIZE_ROWS = 1024
#: The keyword index's tokenizer, also used to normalize query words the same way.
_TOKENIZER = "porter unicode61 remove_diacritics 2"
# Stored row keys that serve indexing and say nothing about the chunk; ``content_id``, ``content_hash``, and the empty
# ``linked_to`` are the keys the rows Agno wrote carry.
_INDEXING_KEYS = frozenset(
    {SOURCE_MTIME_NS_KEY, SOURCE_SIZE_KEY, SOURCE_DIGEST_KEY, "content_id", "content_hash", "linked_to"},
)
_SCHEMA = f"""
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
    tokenize='{_TOKENIZER}'
);
CREATE VIRTUAL TABLE chunks_vocab USING fts5vocab(chunks_fts, row);
-- Changes with every row written or deleted, from a random start, so a cached matrix never outlives the rows it holds
-- even where file timestamps are too coarse to tell two writes apart.
CREATE TABLE generation (value INTEGER NOT NULL);
INSERT INTO generation (value) VALUES (random());
CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, document) VALUES (new.rowid, new.document);
    UPDATE generation SET value = value + 1;
END;
CREATE TRIGGER chunks_fts_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, document) VALUES ('delete', old.rowid, old.document);
    UPDATE generation SET value = value + 1;
END;
"""  # noqa: S608 - fixed DDL; the only substitution is the constant tokenizer
#: The collection's own state; one statement each, so a caller's transaction holds them.
_STATE_SCHEMA = (
    # The file version each claimed source path's rows embed; an empty file is claimed with no rows.
    "CREATE TABLE files (path TEXT PRIMARY KEY, stamp INTEGER NOT NULL, size INTEGER NOT NULL, digest TEXT NOT NULL)"
    " WITHOUT ROWID",
    "CREATE TABLE failures (path TEXT PRIMARY KEY) WITHOUT ROWID",
    "CREATE TABLE meta (settings TEXT NOT NULL, stage TEXT NOT NULL, rebuild INTEGER NOT NULL, revision TEXT,"
    " total_files INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
)
_META_COLUMNS = "settings, stage, rebuild, revision, total_files, created_at, updated_at"


class _CollectionNotFoundError(LookupError):
    """The collection's file does not exist."""


class _KnowledgeIndexTooLargeError(ValueError):
    """The collection's vectors exceed what its search may load into memory."""


@dataclass(frozen=True)
class StoredRow:
    """One chunk as stored: its row id, text, metadata, and vector."""

    id: str
    document: str
    metadata: Mapping[str, Any]
    #: The vector, or its little-endian float32 bytes as stored.
    embedding: Sequence[float] | bytes


@dataclass(frozen=True)
class CollectionMeta:
    """A collection's settings and lifecycle, stored beside its rows."""

    #: The indexing settings its vectors were built under, as ``IndexingSettings.to_metadata()``.
    settings: Mapping[str, str]
    #: ``complete`` once the publisher marks it so, just before publishing it; a refresh that takes it over makes it
    #: ``building`` again.
    stage: Literal["building", "complete"]
    #: A forced rebuild, which neither reuses nor keeps the publication it replaces; set by whichever build writes it.
    rebuild: bool
    #: The Git revision its claims describe, when known.
    revision: str | None
    #: Source files in the corpus it last reconciled against.
    total_files: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class KnowledgeCollection:
    """One named collection, the file ``<name>.sqlite3`` in a knowledge base's storage directory."""

    path: Path
    name: str

    def __post_init__(self) -> None:
        """Refuse a name that is not one plain file-name component, so a name read from metadata stays a file name."""
        if not _COLLECTION_NAME.fullmatch(self.name):
            message = f"Invalid knowledge collection name: {self.name!r}"
            raise ValueError(message)

    @property
    def file(self) -> Path:
        """Return the collection's database file."""
        return self.path / f"{self.name}{_FILE_SUFFIX}"

    def exists(self) -> bool:
        """Return whether the collection's file exists."""
        return self.file.is_file()

    def create(self, meta: CollectionMeta, *, pages: Iterable[Sequence[StoredRow]] = ()) -> None:
        """Replace the collection with a new one holding ``pages``; the new file appears whole or not at all.

        Rows copied from another store carry their file's signature in their metadata, which claims their path.
        """

        def fill(connection: sqlite3.Connection) -> None:
            for page in pages:
                _insert_rows(connection, page)
            install_state_tables(connection, meta)

        self._build(fill)

    def create_copy(self, source: KnowledgeCollection, meta: CollectionMeta) -> None:
        """Replace the collection with a compact copy of ``source`` and ``meta``; it appears whole or not at all."""

        def fill(connection: sqlite3.Connection) -> None:
            _write_meta(connection, meta)

        with closing(source._connect(read_only=True)) as reader:
            self._build(fill, vacuum_from=reader)

    def _build(
        self,
        fill: Callable[[sqlite3.Connection], None],
        *,
        vacuum_from: sqlite3.Connection | None = None,
    ) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        building = self.file.with_name(f"{self.file.name}.building")
        building.unlink(missing_ok=True)
        if vacuum_from is not None:
            vacuum_from.execute("VACUUM INTO ?", (str(building),))
        with closing(sqlite3.connect(building)) as connection:
            if vacuum_from is None:
                connection.executescript(_SCHEMA)
            # No reader sees the file before the rename, so one transaction is only for speed.
            connection.execute("BEGIN")
            fill(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        # The old file's journal must not outlive it, or SQLite would roll it back into the new file.
        self.file.with_name(f"{self.file.name}-journal").unlink(missing_ok=True)
        building.replace(self.file)

    def delete(self) -> None:
        """Delete the collection's file, treating an already-absent one as deleted."""
        self.file.with_name(f"{self.file.name}-journal").unlink(missing_ok=True)
        self.file.unlink(missing_ok=True)

    def meta(self) -> CollectionMeta:
        """Return the collection's settings and lifecycle."""
        with closing(self._connect()) as connection:
            return _read_meta(connection)

    def files(self) -> dict[str, FileSignature]:
        """Return the file version each claimed source path's rows embed."""
        with closing(self._connect()) as connection:
            return {
                path: (stamp, size, digest) for path, stamp, size, digest in connection.execute("SELECT * FROM files")
            }

    def failed_paths(self) -> set[str]:
        """Return the source paths whose last attempt failed."""
        with closing(self._connect()) as connection:
            return {path for (path,) in connection.execute("SELECT path FROM failures")}

    def claim_counts(self) -> tuple[int, int]:
        """Return how many source files the collection claims and how many failed."""
        with closing(self._connect()) as connection:
            claimed = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            failed = connection.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        return claimed, failed

    def free_fraction(self) -> float:
        """Return the share of the file's pages that deleted rows left free; SQLite reuses them but never shrinks."""
        with closing(self._connect()) as connection:
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            free = connection.execute("PRAGMA freelist_count").fetchone()[0]
        return free / pages if pages else 0.0

    def write_file(self, relative_path: str, signature: FileSignature, rows: Sequence[StoredRow]) -> None:
        """Replace one source file's rows with ``rows`` and claim it at ``signature``, in one transaction.

        Vectors must match the collection's width.
        """
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM chunks WHERE source_path = ?", (relative_path,))
            _insert_rows(connection, rows)
            connection.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)", (relative_path, *signature))
            connection.execute("DELETE FROM failures WHERE path = ?", (relative_path,))
            _touch(connection)

    def record_failure(self, relative_path: str) -> None:
        """Record that one source file failed to index: it keeps no rows and no claim."""
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM chunks WHERE source_path = ?", (relative_path,))
            connection.execute("DELETE FROM files WHERE path = ?", (relative_path,))
            connection.execute("INSERT OR IGNORE INTO failures VALUES (?)", (relative_path,))
            _touch(connection)

    def reconcile(
        self,
        *,
        drop: Sequence[str],
        restamp: Mapping[str, FileSignature],
        revision: str | None,
        total_files: int,
        rebuild: bool,
    ) -> None:
        """Start or continue a build against one listing, in one transaction.

        Drops the rows, claims and failures of ``drop``, adopts the new stamps of files whose content is unchanged,
        and records the revision and corpus size the claims now describe; the collection is a ``building`` one, a
        forced rebuild only when ``rebuild``, from here.
        """
        with closing(self._connect()) as connection, connection:
            for batch in _batches(drop):
                placeholders = _placeholders(batch)
                connection.execute(f"DELETE FROM chunks WHERE source_path IN ({placeholders})", batch)  # noqa: S608
                connection.execute(f"DELETE FROM files WHERE path IN ({placeholders})", batch)  # noqa: S608
                connection.execute(f"DELETE FROM failures WHERE path IN ({placeholders})", batch)  # noqa: S608
            connection.executemany(
                "UPDATE files SET stamp = ? WHERE path = ?",
                ((signature[0], path) for path, signature in restamp.items()),
            )
            connection.execute(
                "UPDATE meta SET stage = 'building', rebuild = ?, revision = ?, total_files = ?",
                (int(rebuild), revision, total_files),
            )
            _touch(connection)

    def mark_complete(self) -> None:
        """Mark the build complete, which the publisher does just before publishing it."""
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE meta SET stage = 'complete'")
            _touch(connection)

    def search(
        self,
        embedding: Sequence[float],
        *,
        limit: int,
        query: str | None = None,
        max_matrix_bytes: int | None = None,
    ) -> list[SearchHit]:
        """Return the ``limit`` best chunks, each document's repeats dropped.

        Chunks rank by cosine distance to ``embedding``; with ``query`` text, that ranking is fused with an FTS5 BM25
        ranking of the query's words by reciprocal rank fusion, so exact names and identifiers the embedding blurs
        still surface. Every hit reports its cosine distance either way. A collection whose vectors take more than
        ``max_matrix_bytes`` raises ``_KnowledgeIndexTooLargeError`` instead of loading.
        """
        import numpy as np  # noqa: PLC0415 - only searching needs numpy, which installs on first use.

        with closing(self._connect(read_only=True)) as connection:
            index = _vector_index(connection, self.file, max_matrix_bytes)
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
                ranked = _reciprocal_rank_fusion(
                    ranked,
                    _keyword_rowids(connection, query, candidates, chunk_count=len(index.rowids)),
                )
            positions = np.searchsorted(index.rowids, ranked)
            return _hits(
                connection,
                [(rowid, 1.0 - float(scores[position])) for rowid, position in zip(ranked, positions, strict=True)],
                limit=limit,
            )

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        """Open the existing collection file after checking its schema version.

        Only published files, which are never mid-transaction, open read-only. Anything else opens read-write, so
        SQLite can roll back the journal a refresh killed mid-write left behind instead of refusing to read.
        """
        if not self.file.is_file():
            message = f"Knowledge collection {self.name!r} does not exist"
            raise _CollectionNotFoundError(message)
        connection = sqlite3.connect(
            f"{self.file.resolve().as_uri()}?mode={'ro' if read_only else 'rw'}",
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
    if version != SCHEMA_VERSION:
        message = f"Knowledge collection {name!r} has schema version {version}, expected {SCHEMA_VERSION}"
        raise sqlite3.DatabaseError(message)


def install_state_tables(connection: sqlite3.Connection, meta: CollectionMeta) -> None:
    """Add the state tables to a collection's rows in the caller's transaction, claiming each path its rows carry.

    Every path's rows were written in one transaction, so they carry one file version, which they claim. Rows without
    a well-formed signature claim nothing and are dropped, so no row is ever unclaimed.
    """
    for statement in _STATE_SCHEMA:
        connection.execute(statement)
    stamp, size, digest = (f"$.{SOURCE_MTIME_NS_KEY}", f"$.{SOURCE_SIZE_KEY}", f"$.{SOURCE_DIGEST_KEY}")
    connection.execute(
        "INSERT INTO files SELECT source_path, json_extract(metadata, ?), json_extract(metadata, ?),"
        " json_extract(metadata, ?) FROM chunks"
        " WHERE json_type(metadata, ?) = 'integer' AND json_type(metadata, ?) = 'integer'"
        " AND json_extract(metadata, ?) >= 0 AND json_type(metadata, ?) = 'text' AND json_extract(metadata, ?) != ''"
        " GROUP BY source_path",
        (stamp, size, digest, stamp, size, size, digest, digest),
    )
    connection.execute("DELETE FROM chunks WHERE source_path NOT IN (SELECT path FROM files)")
    _write_meta(connection, meta)


def _write_meta(connection: sqlite3.Connection, meta: CollectionMeta) -> None:
    connection.execute("DELETE FROM meta")
    connection.execute(
        f"INSERT INTO meta ({_META_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",  # noqa: S608 - fixed column list
        (
            json.dumps(dict(meta.settings), sort_keys=True),
            meta.stage,
            int(meta.rebuild),
            meta.revision,
            meta.total_files,
            meta.created_at,
            meta.updated_at,
        ),
    )


def _read_meta(connection: sqlite3.Connection) -> CollectionMeta:
    settings, stage, rebuild, revision, total_files, created_at, updated_at = connection.execute(
        f"SELECT {_META_COLUMNS} FROM meta",  # noqa: S608 - fixed column list
    ).fetchone()
    return CollectionMeta(
        settings=json.loads(settings),
        stage=cast("Literal['building', 'complete']", stage),
        rebuild=bool(rebuild),
        revision=revision,
        total_files=total_files,
        created_at=created_at,
        updated_at=updated_at,
    )


def _touch(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE meta SET updated_at = ?", (utc_now(),))


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
            if entry.name.endswith(_FILE_SUFFIX) and entry.name != _CHROMA_FILE and entry.is_file()
        ),
    )


def _embedding_bytes(embedding: Sequence[float] | bytes) -> bytes:
    return embedding if isinstance(embedding, bytes) else struct.pack(f"<{len(embedding)}f", *embedding)


def _insert_rows(connection: sqlite3.Connection, rows: Sequence[StoredRow]) -> None:
    """Insert rows, refusing vectors whose width differs from each other or from the rows already stored."""
    stored = connection.execute("SELECT length(embedding) FROM chunks LIMIT 1").fetchone()
    blobs = [_embedding_bytes(row.embedding) for row in rows]
    widths = {len(blob) // 4 for blob in blobs} | ({stored[0] // 4} if stored else set())
    if len(widths) > 1:
        message = f"Embedding dimensions {sorted(widths)} do not match within one knowledge collection"
        raise ValueError(message)
    connection.executemany(
        "INSERT INTO chunks(id, source_path, document, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
        (
            (
                row.id,
                row.metadata[SOURCE_PATH_KEY],
                row.document,
                json.dumps(dict(row.metadata), sort_keys=True, ensure_ascii=False),
                blob,
            )
            for row, blob in zip(rows, blobs, strict=True)
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
    if count < len(scores):
        cutoff = np.partition(scores, len(scores) - count)[len(scores) - count]
        above = np.flatnonzero(scores > cutoff)
        candidates = np.concatenate([above, np.flatnonzero(scores == cutoff)[: count - len(above)]])
    else:
        candidates = np.arange(len(scores))
    return [int(position) for position in candidates[np.lexsort((candidates, -scores[candidates]))]]


def _keyword_rowids(connection: sqlite3.Connection, query: str, limit: int, *, chunk_count: int) -> list[int]:
    """Return the rowids of the ``limit`` chunks FTS5 ranks best by BM25 for any of the query's words.

    Words in more than half the chunks are dropped first: BM25 gives them almost no weight, yet scoring them would
    touch nearly every row. Equal scores order by source path, then chunk order: rowids alone would follow which file
    a refresh happened to write first.
    """
    words = list(dict.fromkeys(word.lower() for word in _WORD.findall(query)))[:_MAX_KEYWORD_TERMS]
    if words:
        terms = _index_terms(connection, words)
        all_terms = sorted(set().union(*terms))
        common = {
            term
            for term, documents in connection.execute(
                f"SELECT term, doc FROM chunks_vocab WHERE term IN ({_placeholders(all_terms)})",  # noqa: S608
                all_terms,
            )
            if documents * 2 > chunk_count
        }
        words = [word for word, word_terms in zip(words, terms, strict=True) if not word_terms <= common]
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


def _index_terms(connection: sqlite3.Connection, words: Sequence[str]) -> list[set[str]]:
    """Return the terms the keyword index's tokenizer makes of each word, such as ``configur`` for ``configurations``."""
    connection.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.query_words USING fts5(word, tokenize='{_TOKENIZER}')")
    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.query_terms USING fts5vocab(temp, query_words, instance)",
    )
    connection.execute("DELETE FROM temp.query_words")
    connection.executemany("INSERT INTO temp.query_words(rowid, word) VALUES (?, ?)", enumerate(words))
    terms: list[set[str]] = [set() for _word in words]
    for position, term in connection.execute("SELECT doc, term FROM temp.query_terms"):
        terms[position].add(term)
    return terms


def _reciprocal_rank_fusion(*rankings: Sequence[int]) -> list[int]:
    """Merge rankings by summed ``1 / (k + rank)``; ties keep the earlier ranking's order."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, rowid in enumerate(ranking, start=1):
            fused[rowid] = fused.get(rowid, 0.0) + 1.0 / (_RRF_K + rank)
    return sorted(fused, key=lambda rowid: -fused[rowid])


def _hits(connection: sqlite3.Connection, ranked: Sequence[tuple[int, float]], *, limit: int) -> list[SearchHit]:
    """Build hits for ranked ``(rowid, cosine distance)`` pairs, dropping a document's repeated chunks.

    A hit shows its source, its reader keys, and its distance; the row keys that serve indexing, the source signature
    and the content hash and id, and Agno's empty ``linked_to`` stay out of it.
    """
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
        stored = json.loads(metadata_json)
        name = stored.pop("name", None)
        meta_data = {key: value for key, value in stored.items() if key not in _INDEXING_KEYS}
        meta_data[DISTANCE_KEY] = distance
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


_index_cache: OrderedDict[tuple[str, int, int, int, int, int], _VectorIndex] = OrderedDict()
_index_cache_lock = threading.Lock()
#: Held while a matrix loads, so concurrent cold searches load it once instead of each allocating a copy.
_index_load_lock = threading.Lock()


def _file_version(file: Path) -> tuple[str, int, int, int, int] | None:
    """Return the file's identity and timestamps, or None once the file is gone."""
    try:
        status = file.stat()
    except FileNotFoundError:
        return None
    return (str(file), status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def _vector_index(connection: sqlite3.Connection, file: Path, max_matrix_bytes: int | None) -> _VectorIndex:
    """Return the file's vectors, loading them once per file version and keeping recent ones within the cache budget."""
    version = _file_version(file)
    if version is None:
        message = f"Knowledge collection file {file} disappeared"
        raise FileNotFoundError(message)
    key = (*version, connection.execute("SELECT value FROM generation").fetchone()[0])
    with _index_cache_lock:
        index = _index_cache.get(key)
        if index is not None:
            _index_cache.move_to_end(key)
            return index
    with _index_load_lock:
        with _index_cache_lock:
            index = _index_cache.get(key)
            if index is not None:
                return index
            # A base serves one collection at a time, so before the load, drop the matrices of its other collections
            # (the one a refresh retired stays on disk unchanged), of this file's older versions (a rewrite within one
            # timestamp tick may keep the stat key), and of deleted or rewritten files. A search that chose the old
            # collection just before a publication and loads it after the new one can swap them once more; it costs
            # one extra load per such search and settles as soon as it finishes.
            for stale in [
                cached
                for cached in _index_cache
                if Path(cached[0]).parent == file.parent or _file_version(Path(cached[0])) != cached[:-1]
            ]:
                del _index_cache[stale]
        # A file swept since the connection opened still loads through it, so reading ahead is only best effort. A
        # limited base loads in time without it, and one past its limit is refused without reading its file.
        if max_matrix_bytes is None:
            with suppress(OSError):
                _read_ahead(file)
        index = _load_vector_index(connection, max_matrix_bytes)
        with _index_cache_lock:
            _index_cache[key] = index
            # The largest index does not count against the budget, so one base too big for it is not reloaded on every
            # search, even when searches alternate between it and smaller ones.
            largest = max(_index_cache, key=lambda cached: _index_cache[cached].nbytes)
            cached_bytes = sum(cached.nbytes for cached in _index_cache.values()) - _index_cache[largest].nbytes
            for cached in list(_index_cache):
                if cached_bytes <= _MATRIX_CACHE_BYTES:
                    break
                if cached != largest:
                    cached_bytes -= _index_cache.pop(cached).nbytes
    return index


def _read_ahead(file: Path) -> None:
    """Read the collection file front to back, so the row-order matrix load that follows reads from the page cache.

    On a cold network disk a sequential read is several times faster than that load's own reads, which for a
    collection of a few GB can otherwise exceed a search's budget.
    """
    buffer = bytearray(_READ_AHEAD_CHUNK_BYTES)
    with file.open("rb", buffering=0) as handle:
        while handle.readinto(buffer):
            pass


def _load_vector_index(connection: sqlite3.Connection, max_matrix_bytes: int | None) -> _VectorIndex:
    import numpy as np  # noqa: PLC0415

    # One read transaction, so the count and the rows come from the same snapshot.
    connection.execute("BEGIN")
    try:
        count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        first = connection.execute("SELECT length(embedding) FROM chunks LIMIT 1").fetchone()
        dimension = 0 if first is None else first[0] // 4
        if max_matrix_bytes is not None and count * dimension * 4 > max_matrix_bytes:
            message = f"Knowledge collection holds {count} vectors, more than its {max_matrix_bytes}-byte search limit"
            raise _KnowledgeIndexTooLargeError(message)
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
    # In row blocks: a norm over the whole matrix allocates a temporary as large as the matrix itself.
    for start in range(0, count, _NORMALIZE_ROWS):
        block = matrix[start : start + _NORMALIZE_ROWS]
        norms = np.linalg.norm(block, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        block /= norms
    return _VectorIndex(rowids=rowids, matrix=matrix)
