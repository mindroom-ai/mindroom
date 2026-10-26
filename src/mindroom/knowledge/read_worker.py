"""Wait idle for one native Chroma read, execute it, then release all native memory by exiting."""

from __future__ import annotations

import math
import os
import signal
import sys
from contextlib import closing
from typing import TYPE_CHECKING

from mindroom.knowledge.chroma_client import KnowledgeCollection
from mindroom.knowledge.collection_lifetime import read_collection
from mindroom.knowledge.read_protocol import (
    DISTANCE_KEY,
    MAX_FRAME_BYTES,
    SOURCE_DIGEST_KEY,
    SOURCE_MTIME_NS_KEY,
    SOURCE_SIZE_KEY,
    ReadRequest,
    ReadResult,
    SearchHit,
    request_adapter,
    result_adapter,
)
from mindroom.logging_config import get_logger, setup_subprocess_logging

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chromadb.api.models.Collection import Collection

# The parent runs this module with `python -m`, so `__name__` is `__main__`.
logger = get_logger("mindroom.knowledge.read_worker")

# Stored row keys that serve indexing and say nothing about the chunk: the source signature, and the ``content_id``,
# ``content_hash``, and empty ``linked_to`` every row carries (``knowledge.collections``).
_INDEXING_KEYS = frozenset(
    {SOURCE_MTIME_NS_KEY, SOURCE_SIZE_KEY, SOURCE_DIGEST_KEY, "content_id", "content_hash", "linked_to"},
)


def _query_collection(collection: Collection, embedding: Sequence[float], *, limit: int) -> list[SearchHit]:
    """Return the ``limit`` nearest chunks, nearest first, each with its source, its reader keys, and its distance.

    A chunk stored twice under one name appears once. The row keys that serve indexing (``_INDEXING_KEYS``) stay out
    of the hit.
    """
    result = collection.query(
        query_embeddings=[list(embedding)],
        n_results=limit,
        include=["metadatas", "documents", "distances"],
    )
    hits: list[SearchHit] = []
    seen: set[tuple[str | None, str]] = set()
    for document, metadata, distance in zip(
        (result["documents"] or [[]])[0],
        (result["metadatas"] or [[]])[0],
        (result["distances"] or [[]])[0],
        strict=True,
    ):
        stored = dict(metadata or {})
        name = stored.pop("name", None)
        meta_data = {key: value for key, value in stored.items() if key not in _INDEXING_KEYS}
        meta_data[DISTANCE_KEY] = distance
        hit = SearchHit(
            content=document or "",
            name=None if name is None else str(name),
            meta_data=meta_data,
        )
        if (hit.name, hit.content) not in seen:
            seen.add((hit.name, hit.content))
            hits.append(hit)
    hits.sort(key=lambda hit: hit.meta_data[DISTANCE_KEY])
    return hits[:limit]


def _read(request: ReadRequest) -> ReadResult:
    with (
        read_collection(request.path, request.collection, request.published_settings) as collection_name,
        closing(KnowledgeCollection(path=request.path, name=collection_name)) as collection,
    ):
        # ``get_collection`` raises for a vanished collection instead of recreating it.
        return ReadResult(hits=_query_collection(collection.collection(), request.embedding, limit=request.limit))


def _main() -> None:
    """Idle until stdin delivers a deadline line and one JSON request, then write one JSON result."""
    setup_subprocess_logging()
    # Redirect the descriptor too: native code and preconfigured log handlers
    # may retain the original stdout object, bypassing redirect_stdout.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    with output:
        deadline = sys.stdin.buffer.readline()
        if not deadline:
            return  # The parent exited.
        timeout = float(deadline)
        if not math.isfinite(timeout) or timeout <= 0:
            message = "Knowledge reader deadline must be finite and positive"
            raise ValueError(message)
        # The kernel terminates even GIL-blocked native code after the parent dies.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        request_payload = sys.stdin.buffer.read(MAX_FRAME_BYTES + 1)
        try:
            if len(request_payload) > MAX_FRAME_BYTES:
                message = "Knowledge read request exceeds transport size limit"
                raise ValueError(message)  # noqa: TRY301
            result = _read(request_adapter.validate_json(request_payload))
            payload = result_adapter.dump_json(result)
            if len(payload) > MAX_FRAME_BYTES:
                message = "Knowledge read result exceeds transport size limit"
                raise ValueError(message)  # noqa: TRY301
        except Exception as exc:
            logger.exception("Knowledge read failed")
            payload = result_adapter.dump_json(ReadResult(error_type=type(exc).__name__))
        output.write(payload)


if __name__ == "__main__":
    _main()
    # The result is delivered and exit releases all native memory, so skip the
    # interpreter teardown (about 0.1 s with Chroma loaded) that the parent waits for.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
