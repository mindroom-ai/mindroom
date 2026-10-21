"""Chroma collection lifecycle for one knowledge base.

Naming, opening, probing, deleting and reclaiming the collections a knowledge
base owns. None of that depends on the refresh advancing the base: a base's
collections are a function of where it stores vectors and what those vectors
are called, so this deliberately holds no per-refresh state and takes what it
needs as arguments.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from chromadb.errors import InternalError, NotFoundError

from mindroom.knowledge.chroma_client import KnowledgeCollection
from mindroom.knowledge.collection_lifetime import collection_lifetime_lock
from mindroom.knowledge.indexing_config import storage_key_for_base
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from chromadb.api.client import Client
    from chromadb.api.models.Collection import Collection
    from chromadb.api.types import Metadata

logger = get_logger(__name__)

_COLLECTION_PREFIX = "mindroom_knowledge"

#: Source identity every indexed chunk carries, so a collection doubles as an
#: index from source path to the vectors and signature that path produced.
SOURCE_PATH_KEY = "source_path"
SOURCE_MTIME_NS_KEY = "source_mtime_ns"
SOURCE_SIZE_KEY = "source_size"
SOURCE_DIGEST_KEY = "source_digest"

#: Completed candidate entries whose vectors are confirmed in one Chroma query.
#: Only a starting point: the query splits itself when the store refuses it,
#: because the real limit is matched rows, which this cannot know up front.
VECTOR_VERIFY_BATCH = 128
#: Source paths whose vectors are dropped in one Chroma delete. Independent of
#: the verify batch: a delete binds no variable per matched row, so this bounds
#: only how much work one call does.
_VECTOR_DELETE_BATCH = 128
#: The distance every knowledge collection is created with; collections written before MindRoom owned this store
#: carry the same setting, so they stay readable as they are.
_COLLECTION_METADATA: Final = {"hnsw:space": "cosine"}


@runtime_checkable
class _NamedCollection(Protocol):
    """Collection object shape returned by Chroma clients."""

    name: str


@dataclass(frozen=True)
class CollectionSpace:
    """One knowledge base's private storage directory and the names it owns.

    ``default_collection`` is *derived*, never accepted. Cleanup treats it as
    proof of ownership -- a collection is deletable only if it matches that
    name or carries its candidate prefix -- so a space that could be handed the
    name would be able to authorize deleting a collection this base does not
    own. Computing it from ``base_id`` and the resolved source path keeps the
    answer to "is this mine?" a function of identity rather than an assertion.
    """

    base_id: str
    knowledge_path: Path
    storage_path: Path

    @property
    def default_collection(self) -> str:
        """Return the published collection name this base's identity owns."""
        return _collection_name_for_base(self.base_id, self.knowledge_path)


@dataclass(frozen=True)
class KnowledgeChunk:
    """One chunk a knowledge file reader produced, before it is embedded and stored."""

    text: str
    #: The source file's basename, which search results report as the document name.
    name: str | None
    #: What the reader and chunker recorded, such as ``chunk``, ``chunk_size``, ``page``, or ``row_number``.
    metadata: Mapping[str, Any]


def _collection_name_for_base(base_id: str, knowledge_path: Path) -> str:
    """Return the published collection name one base's resolved source path owns."""
    return f"{_COLLECTION_PREFIX}_{storage_key_for_base(base_id, knowledge_path)}"


def candidate_collection_name(space: CollectionSpace) -> str:
    """Return a fresh candidate name under the prefix this base provably owns."""
    return f"{space.default_collection}_candidate_{uuid.uuid4().hex[:16]}"


def open_collection(space: CollectionSpace, collection_name: str) -> KnowledgeCollection:
    """Return a handle on one collection in this base's storage directory; nothing is opened yet."""
    return KnowledgeCollection(path=str(space.storage_path), name=collection_name)


def _delete_if_present(client: Client, collection_name: str) -> None:
    """Delete one collection, treating an already-absent one as deleted and raising any other failure."""
    try:
        client.delete_collection(name=collection_name)
    except NotFoundError:
        return


def reset_collection(collection: KnowledgeCollection) -> None:
    """Empty one collection by dropping and recreating it; a failed drop raises instead of keeping old rows."""
    _delete_if_present(collection.client, collection.name)
    collection.client.create_collection(name=collection.name, metadata=_COLLECTION_METADATA)


def _collection_has_source_path(collection: Collection, relative_path: str) -> bool:
    """Return whether one source path has any vector, at one row of cost.

    Chroma binds one SQL variable per *returned* row, so an unbounded probe on
    a heavily chunked file exceeds SQLite's ceiling and fails outright. One row
    is all existence needs, and ``limit=1`` is what keeps this answerable at
    any file size. Do not widen it.
    """
    result = collection.get(where={SOURCE_PATH_KEY: relative_path}, limit=1, include=[])
    return bool(result.get("ids"))


def _collection_paths_with_vectors(collection: Collection, relative_paths: Sequence[str]) -> set[str]:
    """Return which of `relative_paths` have at least one vector in the collection.

    Chroma binds one SQL variable per *matched row*, not one per queried path,
    so a fixed batch of paths does not bound the query at all: whether it fits
    under SQLite's ceiling depends on how many chunks those particular files
    produced. That makes any batch size chosen up front a gamble. Small files
    leave a batch of 128 far below the ceiling, a handful of large ones puts
    the same batch over it, and once over, every verification query for that
    base fails identically and the candidate is stranded for good.

    So the batch is not guessed, it is *adapted*: ask for the whole thing, and
    on refusal halve it and ask again. Each split halves the matched rows too,
    so it converges, and a store that can answer the batch pays nothing.

    A single path is the floor, where splitting can no longer help, so it is
    asked for a single row instead, which stays under any ceiling however many
    chunks the file has. That floor is what makes the recursion total, and it
    is also where a failure that was never about query size finally surfaces.

    No paths is answerable without asking: Chroma rejects an empty ``$in``
    operand outright, so the empty case has to be a base case rather than a
    query. Splitting never reaches it -- every half of a batch of two or more
    is non-empty -- but a caller can.
    """
    if not relative_paths:
        return set()

    if len(relative_paths) == 1:
        relative_path = relative_paths[0]
        return {relative_path} if _collection_has_source_path(collection, relative_path) else set()

    try:
        result = collection.get(where={SOURCE_PATH_KEY: {"$in": list(relative_paths)}}, include=["metadatas"])
    except NotFoundError:
        # Splitting answers "the store refused this query for its size". A
        # collection that is gone is not that, and stays gone however small the
        # query gets, so descending would only cost log2(batch) + 1 doomed
        # queries before raising exactly the same error from the first leaf.
        raise
    except InternalError as error:
        if "too many SQL variables" not in str(error):
            raise
        # Splitting stays correct but multiplies the queries, and how far it
        # degrades depends on chunk counts nothing here can see. Without a
        # trace, a base whose files outgrew the ceiling just gets quietly
        # slower -- the same invisibility that let the unsplit query strand
        # candidates undiagnosed.
        logger.debug("Split a refused knowledge vector verification query", paths=len(relative_paths))
        midpoint = len(relative_paths) // 2
        return _collection_paths_with_vectors(collection, relative_paths[:midpoint]) | _collection_paths_with_vectors(
            collection,
            relative_paths[midpoint:],
        )

    found: set[str] = set()
    for metadata in result.get("metadatas") or []:
        source_path = metadata.get(SOURCE_PATH_KEY)
        if isinstance(source_path, str):
            found.add(source_path)
    return found


def paths_with_vectors(collection: KnowledgeCollection, relative_paths: Sequence[str]) -> set[str]:
    """Return which of the given source paths actually have vectors in one collection."""
    if not relative_paths:
        # Answered before resolving a handle: asking about no paths has one
        # obvious answer, and resolving would raise if the collection is gone.
        return set()
    return _collection_paths_with_vectors(collection.collection(), relative_paths)


def delete_source_path_vectors(collection: KnowledgeCollection, relative_paths: Sequence[str]) -> None:
    """Delete vectors for many source paths in one vector-store round trip.

    Unlike the ``$in`` the verification query issues, this one needs no
    ceiling protection: a delete does not bind one SQL variable per matched
    row, so the batch size alone bounds it. Measured on ChromaDB 1.5.8,
    deleting 51,200 rows across 128 paths in one call succeeds, where the
    equivalent read fails with ``too many SQL variables``.
    """
    chroma_collection = collection.collection()
    for start in range(0, len(relative_paths), _VECTOR_DELETE_BATCH):
        batch = list(relative_paths[start : start + _VECTOR_DELETE_BATCH])
        chroma_collection.delete(where={SOURCE_PATH_KEY: {"$in": batch}})


def _source_content_hash(source_metadata: Mapping[str, Any], relative_path: str) -> str:
    """Return the hash every row of one file version carries, from the file's identity and signature."""
    return hashlib.sha256(":".join([json.dumps(source_metadata, sort_keys=True), relative_path]).encode()).hexdigest()


def _row_metadata(
    chunk: KnowledgeChunk,
    *,
    source_metadata: Mapping[str, Any],
    content_hash: str,
) -> Metadata:
    """Return one chunk's stored metadata, with the keys and values rows have always carried.

    Search results show the model every stored key except ``name``, so the
    set stays the one existing collections hold: the reader's own keys, the
    source signature, and ``linked_to``, ``content_hash``, and ``content_id``.
    """
    metadata: dict[str, Any] = {key: value for key, value in chunk.metadata.items() if value is not None}
    metadata.update(source_metadata)
    metadata["linked_to"] = ""
    if chunk.name is not None:
        metadata["name"] = chunk.name
    metadata["content_id"] = str(uuid.uuid5(uuid.NAMESPACE_DNS, content_hash))
    metadata["content_hash"] = content_hash
    return metadata


def write_source_rows(
    collection: KnowledgeCollection,
    *,
    relative_path: str,
    source_metadata: Mapping[str, Any],
    chunks: Sequence[KnowledgeChunk],
    vectors: Sequence[Sequence[float]],
) -> None:
    """Replace one source file's rows with its embedded chunks.

    The old rows are deleted only once every new vector exists, so a failed
    embedding never empties a file. Rows are written in pages of Chroma's
    largest batch, and row ids are fixed by the file version and the chunk's
    position.
    """
    if len(vectors) != len(chunks):
        message = f"Expected {len(chunks)} vectors for {relative_path}, got {len(vectors)}"
        raise ValueError(message)
    content_hash = _source_content_hash(source_metadata, relative_path)
    chroma_collection = collection.collection()
    chroma_collection.delete(where={SOURCE_PATH_KEY: relative_path})
    page_size = collection.client.get_max_batch_size()
    for start in range(0, len(chunks), page_size):
        page = range(start, min(start + page_size, len(chunks)))
        chroma_collection.upsert(
            ids=[hashlib.md5(f"{content_hash}_{index}".encode()).hexdigest() for index in page],  # noqa: S324
            embeddings=[list(vectors[index]) for index in page],
            documents=[chunks[index].text.replace("\x00", "\ufffd") for index in page],
            metadatas=[
                _row_metadata(chunks[index], source_metadata=source_metadata, content_hash=content_hash)
                for index in page
            ],
        )


async def delete_collection(space: CollectionSpace, collection_name: str) -> bool:
    """Delete one collection this base owns, reporting whether it is really gone."""
    try:
        await asyncio.to_thread(_delete_collection_sync, space, collection_name)
    except Exception:
        logger.warning(
            "Failed to delete knowledge collection",
            base_id=space.base_id,
            collection=collection_name,
            exc_info=True,
        )
        return False
    return True


def _delete_collection_sync(space: CollectionSpace, collection_name: str) -> None:
    """Delete one collection, treating an already-absent one as deleted, then reclaim its storage."""
    collection = open_collection(space, collection_name)
    with collection_lifetime_lock(space.storage_path, exclusive=True), closing(collection):
        _delete_if_present(collection.client, collection_name)
        _reclaim_orphaned_segment_directories(space)


def _reclaim_orphaned_segment_directories(space: CollectionSpace) -> None:
    """Remove UUID-named Chroma directories that no live segment references."""
    database_path = space.storage_path / "chroma.sqlite3"
    if not database_path.is_file():
        return
    try:
        database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as connection:
            live_segment_ids = {row[0] for row in connection.execute("SELECT id FROM segments")}
        entries = tuple(space.storage_path.iterdir())
    except (OSError, sqlite3.Error):
        logger.warning(
            "Failed to inspect knowledge segment storage for cleanup",
            base_id=space.base_id,
            exc_info=True,
        )
        return

    reclaimed = 0
    for path in entries:
        try:
            is_segment_directory = not path.is_symlink() and path.is_dir() and str(uuid.UUID(path.name)) == path.name
        except (OSError, ValueError):
            continue
        if not is_segment_directory or path.name in live_segment_ids:
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            logger.warning(
                "Failed to reclaim orphaned knowledge segment directory",
                base_id=space.base_id,
                segment_id=path.name,
                exc_info=True,
            )
        else:
            reclaimed += 1
    if reclaimed:
        logger.info(
            "Reclaimed orphaned knowledge segment directories",
            base_id=space.base_id,
            segments=reclaimed,
        )


def cleanup_superseded_collections(
    space: CollectionSpace,
    *,
    client: Client,
    preserved: frozenset[str],
    candidates_only: bool = False,
) -> None:
    """Delete this base's superseded collections, preserving proven-live ones.

    Ownership is proven by name: both the default collection and the
    candidate prefix embed this base's identity and resolved source path,
    and both live in this base's own private storage directory. Anything
    else in that directory is left alone and reported rather than deleted,
    because nothing here can prove who owns it.
    """
    default_collection = space.default_collection
    candidate_prefix = f"{default_collection}_candidate_"

    try:
        collection_names = _listed_collection_names(client)
    except Exception:
        logger.warning(
            "Failed to list superseded knowledge collections for cleanup",
            base_id=space.base_id,
            exc_info=True,
        )
        return

    with collection_lifetime_lock(space.storage_path, exclusive=True):
        unowned: list[str] = []
        for collection_name in collection_names:
            if collection_name in preserved:
                continue
            is_candidate = collection_name.startswith(candidate_prefix)
            if not is_candidate and (candidates_only or collection_name != default_collection):
                # Reclaiming abandoned candidates must never race a legacy
                # published collection whose metadata predates this layout.
                if collection_name != default_collection:
                    unowned.append(collection_name)
                continue
            try:
                _delete_if_present(client, collection_name)
            except Exception:
                logger.warning(
                    "Failed to clean superseded knowledge collection",
                    base_id=space.base_id,
                    collection=collection_name,
                    exc_info=True,
                )
        _reclaim_orphaned_segment_directories(space)
        if unowned:
            logger.info(
                "Preserved knowledge collections with unprovable ownership",
                base_id=space.base_id,
                collections=sorted(unowned),
            )


def _listed_collection_names(client: Client) -> tuple[str, ...]:
    names: list[str] = []
    for collection in client.list_collections():
        if isinstance(collection, str):
            names.append(collection)
        elif isinstance(collection, _NamedCollection):
            names.append(collection.name)
    return tuple(dict.fromkeys(names))
