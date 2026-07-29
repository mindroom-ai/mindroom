"""Chroma collection lifecycle for one knowledge base.

Naming, opening, probing, deleting and reclaiming the collections a knowledge
base owns. None of that depends on the refresh advancing the base: a base's
collections are a function of where it stores vectors and what those vectors
are called, so this deliberately holds no per-refresh state and takes what it
needs as arguments.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agno.vectordb.chroma import ChromaDb
from chromadb.errors import InternalError, NotFoundError

from mindroom.knowledge.indexing_config import storage_key_for_base
from mindroom.logging_config import get_logger
from mindroom.strict_knowledge import StrictInsertKnowledge as Knowledge

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from agno.knowledge.embedder.base import Embedder
    from agno.vectordb.base import VectorDb
    from chromadb.api.models.Collection import Collection

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


@runtime_checkable
class _CollectionListingClient(Protocol):
    """Vector client surface needed for best-effort collection cleanup."""

    def list_collections(self) -> list[object]:
        """Return collection names or collection objects."""
        ...


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

    ``embedder_factory`` stays a factory rather than an embedder so that a base
    which never opens a collection never constructs one.
    """

    base_id: str
    knowledge_path: Path
    storage_path: Path
    embedder_factory: Callable[[], Embedder]

    @property
    def default_collection(self) -> str:
        """Return the published collection name this base's identity owns."""
        return _collection_name_for_base(self.base_id, self.knowledge_path)


def _collection_name_for_base(base_id: str, knowledge_path: Path) -> str:
    """Return the published collection name one base's resolved source path owns."""
    return f"{_COLLECTION_PREFIX}_{storage_key_for_base(base_id, knowledge_path)}"


def candidate_collection_name(space: CollectionSpace) -> str:
    """Return a fresh candidate name under the prefix this base provably owns."""
    return f"{space.default_collection}_candidate_{uuid.uuid4().hex[:16]}"


def build_vector_db(
    space: CollectionSpace,
    collection_name: str,
    *,
    embedder: Embedder | None = None,
) -> ChromaDb:
    """Open one collection handle in this base's storage directory."""
    return ChromaDb(
        collection=collection_name,
        path=str(space.storage_path),
        persistent_client=True,
        embedder=embedder if embedder is not None else space.embedder_factory(),
    )


def build_knowledge(
    space: CollectionSpace,
    collection_name: str,
    *,
    embedder: Embedder | None = None,
) -> Knowledge:
    """Return the Agno knowledge surface backing one collection."""
    return Knowledge(vector_db=build_vector_db(space, collection_name, embedder=embedder))


def require_chroma_vector_db(knowledge: Knowledge) -> ChromaDb:
    """Narrow one knowledge surface to the Chroma database the candidate needs."""
    vector_db = knowledge.vector_db
    if not isinstance(vector_db, ChromaDb):
        msg = "Knowledge reindex candidate collection requires a ChromaDb vector database"
        raise TypeError(msg)
    return vector_db


def reset_vector_db(vector_db: ChromaDb) -> None:
    """Empty one collection by dropping and recreating it."""
    vector_db.delete()
    vector_db.create()


def collection_has_source_path(collection: Collection, relative_path: str) -> bool:
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
        return {relative_path} if collection_has_source_path(collection, relative_path) else set()

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


def paths_with_vectors(vector_db: ChromaDb, relative_paths: Sequence[str]) -> set[str]:
    """Return which of the given source paths actually have vectors in one collection."""
    if not relative_paths:
        # Answered before resolving a handle: asking about no paths has one
        # obvious answer, and resolving would raise if the collection is gone.
        return set()
    collection = vector_db.client.get_collection(name=vector_db.collection_name)
    return _collection_paths_with_vectors(collection, relative_paths)


def delete_source_path_vectors(vector_db: ChromaDb, relative_paths: Sequence[str]) -> None:
    """Delete vectors for many source paths in one vector-store round trip.

    Agno's ``delete_by_metadata`` wraps values in ``$eq`` and so can only
    take one path per call, which turns a large source update into one
    thread hop and one get+delete per file. The collection accepts ``$in``
    directly.

    Unlike the ``$in`` the verification query issues, this one needs no
    ceiling protection: a delete does not bind one SQL variable per matched
    row, so the batch size alone bounds it. Measured on ChromaDB 1.5.8,
    deleting 51,200 rows across 128 paths in one call succeeds, where the
    equivalent read fails with ``too many SQL variables``.
    """
    collection = vector_db.client.get_collection(name=vector_db.collection_name)
    for start in range(0, len(relative_paths), _VECTOR_DELETE_BATCH):
        batch = list(relative_paths[start : start + _VECTOR_DELETE_BATCH])
        collection.delete(where={SOURCE_PATH_KEY: {"$in": batch}})


async def delete_collection(space: CollectionSpace, collection_name: str) -> bool:
    """Delete one collection this base owns, reporting whether it is really gone.

    Agno's ``ChromaDb.delete`` swallows the provider error and returns
    ``False`` rather than raising, so catching exceptions alone would
    report every real failure as a success. A ``False`` result is also
    returned when the collection simply was not there, which is the
    outcome we want, so the two are told apart by probing existence.
    """
    try:
        deleted = await asyncio.to_thread(_delete_collection_sync, space, collection_name)
    except Exception:
        logger.warning(
            "Failed to delete knowledge collection",
            base_id=space.base_id,
            collection=collection_name,
            exc_info=True,
        )
        return False
    if deleted:
        return True
    logger.warning(
        "Knowledge collection still exists after deletion failed",
        base_id=space.base_id,
        collection=collection_name,
    )
    return False


def _delete_collection_sync(space: CollectionSpace, collection_name: str) -> bool:
    """Delete one collection, treating an already-absent one as success."""
    vector_db = build_vector_db(space, collection_name)
    if vector_db.delete():
        return True
    try:
        vector_db.client.get_collection(name=vector_db.collection_name)
    except NotFoundError:
        return True
    return False


def cleanup_superseded_collections(
    space: CollectionSpace,
    *,
    vector_db: VectorDb | None,
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
    if not isinstance(vector_db, ChromaDb):
        return
    client = vector_db.client
    if client is None or not isinstance(client, _CollectionListingClient):
        return

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
            build_vector_db(space, collection_name).delete()
        except Exception:
            logger.warning(
                "Failed to clean superseded knowledge collection",
                base_id=space.base_id,
                collection=collection_name,
                exc_info=True,
            )
    if unowned:
        logger.info(
            "Preserved knowledge collections with unprovable ownership",
            base_id=space.base_id,
            collections=sorted(unowned),
        )


def _listed_collection_names(client: _CollectionListingClient) -> tuple[str, ...]:
    names: list[str] = []
    for collection in client.list_collections():
        if isinstance(collection, str):
            names.append(collection)
        elif isinstance(collection, _NamedCollection):
            names.append(collection.name)
    return tuple(dict.fromkeys(names))
