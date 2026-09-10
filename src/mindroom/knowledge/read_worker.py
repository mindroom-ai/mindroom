"""Execute one native Chroma read, then release all native memory by exiting."""

from __future__ import annotations

import os
import sys
import traceback
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

from agno.knowledge.embedder.base import Embedder

from mindroom.knowledge.chroma_client import ChromaDb
from mindroom.knowledge.read_protocol import (
    MAX_FRAME_BYTES,
    ReadDocument,
    ReadRequest,
    ReadResult,
    request_adapter,
    result_adapter,
)
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection


class _PublishedChromaDb(ChromaDb):
    """Keep Agno's search semantics while forbidding implicit collection creation."""

    @override
    def _collections_to_query(self, user_id: str | None) -> list[Collection]:
        del user_id
        return [self.client.get_collection(name=self.collection_name)]


@dataclass
class _QueryEmbedder(Embedder):
    vector: list[float] = field(default_factory=list)

    def get_embedding(self, text: str) -> list[float]:
        del text
        return self.vector


def _read(request: ReadRequest) -> ReadResult:
    with closing(
        _PublishedChromaDb(
            collection=request.collection,
            path=request.path,
            persistent_client=True,
            embedder=_QueryEmbedder(vector=request.embedding or []),
        ),
    ) as vector_db:
        if request.query is None:
            return ReadResult(exists=vector_db.exists())
        if request.embedding is None:
            message = "A search requires a query embedding"
            raise ValueError(message)
        documents = vector_db.search(query=request.query, limit=request.limit, filters=request.filters)
        return ReadResult(exists=True, documents=[ReadDocument.from_document(document) for document in documents])


def _main() -> None:
    """Read one JSON request from stdin and return one JSON result on stdout."""
    # Redirect the descriptor too: native code and preconfigured log handlers
    # may retain the original stdout object, bypassing redirect_stdout.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    request_payload = sys.stdin.buffer.read(MAX_FRAME_BYTES + 1)
    with output:
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
            sys.stderr.write(redact_sensitive_text(traceback.format_exc()))
            payload = result_adapter.dump_json(ReadResult(error_type=type(exc).__name__))
        output.write(payload)


if __name__ == "__main__":
    _main()
