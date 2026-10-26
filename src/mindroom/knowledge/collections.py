"""Collection lifecycle for one knowledge base.

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
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.knowledge.collection_lifetime import collection_lifetime_lock
from mindroom.knowledge.indexing_config import storage_key_for_base
from mindroom.knowledge.legacy_chroma_storage import remove_legacy_chroma_storage
from mindroom.knowledge.read_protocol import SOURCE_PATH_KEY
from mindroom.knowledge.vector_store import (
    KnowledgeCollection,
    StoredRow,
    list_collection_names,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from mindroom.knowledge.candidate_checkpoint import FileSignature

logger = get_logger(__name__)


_COLLECTION_PREFIX = "mindroom_knowledge"

#: Source signature every indexed chunk carries beside its ``SOURCE_PATH_KEY``, so a collection doubles as an
#: index from source path to the vectors and signature that path produced.
SOURCE_MTIME_NS_KEY = "source_mtime_ns"
SOURCE_SIZE_KEY = "source_size"
SOURCE_DIGEST_KEY = "source_digest"

#: Completed candidate entries whose vectors are confirmed per thread hop, so verifying a huge candidate still yields
#: to the event loop and to cancellation.
VECTOR_VERIFY_BATCH = 500


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

    def owns(self, collection_name: str) -> bool:
        """Return whether a collection name carries this base's identity: its default name or candidate prefix."""
        return collection_name == self.default_collection or collection_name.startswith(
            f"{self.default_collection}_candidate_",
        )


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
    """Return a handle on one collection this base owns in its storage directory; nothing is opened yet.

    Names come from metadata a worker sharing the storage root can write, and even a directory swapped for a link
    then only exposes files named for this base, never another file a forged name would choose.
    """
    if not space.owns(collection_name):
        message = f"Knowledge collection {collection_name!r} does not belong to base {space.base_id!r}"
        raise ValueError(message)
    return KnowledgeCollection(path=space.storage_path, name=collection_name)


def _source_content_hash(source_metadata: Mapping[str, Any]) -> str:
    """Return the hash every row of one file version carries, from the file's identity and signature.

    The input is the sorted metadata JSON, a colon, and the path, which rows indexed by earlier releases hashed too.
    """
    return hashlib.sha256(
        ":".join([json.dumps(source_metadata, sort_keys=True), source_metadata[SOURCE_PATH_KEY]]).encode(),
    ).hexdigest()


def _row_metadata(
    chunk: KnowledgeChunk,
    *,
    source_metadata: Mapping[str, Any],
    content_hash: str,
) -> dict[str, Any]:
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
    signature: FileSignature,
    rows: Sequence[tuple[KnowledgeChunk, Sequence[float]]],
) -> None:
    """Replace one source file's rows with its chunks, each paired with its vector.

    The caller embeds every chunk before it writes, so a failed embedding
    never empties a file. Row ids are fixed by the file version and the
    chunk's position.
    """
    mtime_ns, size, digest = signature
    source_metadata = {
        SOURCE_PATH_KEY: relative_path,
        SOURCE_MTIME_NS_KEY: mtime_ns,
        SOURCE_SIZE_KEY: size,
        SOURCE_DIGEST_KEY: digest,
    }
    content_hash = _source_content_hash(source_metadata)
    collection.write_source_rows(
        relative_path,
        [
            StoredRow(
                id=hashlib.md5(f"{content_hash}_{index}".encode()).hexdigest(),  # noqa: S324
                document=chunk.text.replace("\x00", "\ufffd"),
                metadata=_row_metadata(chunk, source_metadata=source_metadata, content_hash=content_hash),
                embedding=vector,
            )
            for index, (chunk, vector) in enumerate(rows)
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
    """Delete one collection, treating an already-absent one as deleted."""
    with collection_lifetime_lock(space.storage_path, exclusive=True):
        open_collection(space, collection_name).delete()


def cleanup_superseded_collections(
    space: CollectionSpace,
    *,
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
        collection_names = list_collection_names(space.storage_path)
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
                open_collection(space, collection_name).delete()
            except Exception:
                logger.warning(
                    "Failed to clean superseded knowledge collection",
                    base_id=space.base_id,
                    collection=collection_name,
                    exc_info=True,
                )
        for leftover in space.storage_path.glob(f"{default_collection}*.sqlite3.building"):
            # A reset killed before its rename; no collection is ever read from these.
            leftover.unlink(missing_ok=True)
        if not candidates_only:
            # Only the sweep after a publication: until then the Chroma files are what a downgrade would read.
            remove_legacy_chroma_storage(space.storage_path, base_id=space.base_id)
        if unowned:
            logger.info(
                "Preserved knowledge collections with unprovable ownership",
                base_id=space.base_id,
                collections=sorted(unowned),
            )
