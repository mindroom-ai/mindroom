"""Batch the document embedding requests a semantic knowledge refresh issues.

A refresh reads and chunks a bounded batch of files once, embeds the batch's
distinct chunk texts in as few provider requests as the item and payload
limits allow, and only then writes each file's rows. Embedding one request per
chunk instead makes a corpus of many small files one HTTP round trip per file,
which is the dominant cost of a large build and far slower than the rate at
which such corpora change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.embeddings.errors import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EmbedderRequestError,
    describe_embedder_error,
    embedder_batch_cardinality_mismatch,
    embedder_failure_is_transient,
    is_embedder_auth_failure_detail,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.embeddings.base import Embedder

logger = get_logger(__name__)

#: Provider request limits. Both bounds matter: item count keeps a request from
#: exceeding per-request input limits, and payload size keeps a batch of large
#: chunks from producing a request the provider rejects outright.
_MAX_EMBEDDING_BATCH_ITEMS = 64
_MAX_EMBEDDING_BATCH_PAYLOAD_BYTES = 512_000


def plan_embedding_batches(
    texts: Sequence[str],
    *,
    max_items: int = _MAX_EMBEDDING_BATCH_ITEMS,
    max_payload_bytes: int = _MAX_EMBEDDING_BATCH_PAYLOAD_BYTES,
) -> list[list[str]]:
    """Split texts into provider requests bounded by item count and payload size.

    A single text larger than ``max_payload_bytes`` still gets its own request:
    splitting it here would embed a fragment of a chunk.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for text in texts:
        text_bytes = len(text.encode("utf-8"))
        exceeds_limits = current and (
            len(current) >= max(max_items, 1) or current_bytes + text_bytes > max_payload_bytes
        )
        if exceeds_limits:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(text)
        current_bytes += text_bytes
    if current:
        batches.append(current)
    return batches


@dataclass
class RefreshEmbedder:
    """The embedder one knowledge refresh writes chunks with, and what the refresh has learned about it.

    Every vector is checked against the width of the first vector this
    refresh saw, not against a declared width, which real providers routinely
    contradict, so trusting it would reject correct vectors. Consistency still
    catches the case that matters here, a provider changing width mid-run.
    """

    embedder: Embedder
    _batching_disabled: bool = field(default=False, init=False, repr=False)
    _dimensions: int | None = field(default=None, init=False, repr=False)

    def batches(self) -> bool:
        """Return whether one request can still carry several chunk texts."""
        return not self._batching_disabled and self.embedder.sends_batches

    def embed_texts(self, texts: Sequence[str]) -> dict[str, list[float]]:
        """Embed distinct chunk texts as documents, in one request when batching is usable.

        Batch support is a capability claim that only a real multi-input
        request can test: some OpenAI-compatible backends accept an array and
        answer with a single vector, or reject the array outright. Treating
        that as a fatal error stalls every refresh on its first batch, so a
        cardinality mismatch retires batching for the rest of this refresh and
        the same texts are embedded one at a time instead.

        Everything else is re-raised: transient failures keep the caller's
        retry and backoff, and credentials, permissions, a bad model, or a
        malformed payload would fail exactly the same way one input at a time.

        On the per-item path, a text whose request fails for any reason but a
        credential rejection is left out of the result, so the file that owns
        it embeds it again through :meth:`embed_each`, where the failure is
        retried and recorded against that file. A rejected credential is
        re-raised because it is provably global, and grinding one doomed
        request per remaining chunk would bury the cause.
        """
        pending = list(dict.fromkeys(texts))
        if not pending:
            return {}
        if not self.batches() or len(pending) == 1:
            # A single-input request proves nothing about batch support, so it
            # is never allowed to retire batching.
            return self._embed_each_leaving_failures(pending)

        try:
            vectors = self.embedder.embed(pending, purpose="document")
        except Exception as exc:
            if embedder_failure_is_transient(exc) or not embedder_batch_cardinality_mismatch(exc):
                raise
            self._disable_batching(f"multi-input request failed: {describe_embedder_error(exc)}")
            return self._embed_each_leaving_failures(pending)

        if len(vectors) != len(pending):
            # The partial result is discarded on purpose: with fewer vectors
            # than inputs there is no trustworthy way to tell which input each
            # one belongs to, and guessing by position would silently attach a
            # vector to the wrong chunk.
            self._disable_batching(f"multi-input request returned {len(vectors)} vectors for {len(pending)} inputs")
            return self._embed_each_leaving_failures(pending)

        return {text: self._validated(vector) for text, vector in zip(pending, vectors, strict=True)}

    def embed_each(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed each chunk text as a document in its own request, raising the first failure."""
        return [self._validated(self.embedder.embed([text], purpose="document")[0]) for text in texts]

    def _embed_each_leaving_failures(self, texts: Sequence[str]) -> dict[str, list[float]]:
        """Embed one text per request, leaving out every text but a credential rejection fails."""
        vectors: dict[str, list[float]] = {}
        for text in texts:
            try:
                vectors[text] = self.embed_each([text])[0]
            except Exception as exc:
                if is_embedder_auth_failure_detail(describe_embedder_error(exc)):
                    raise
                logger.debug("Leaving one chunk for its file to embed", exc_info=True)
        return vectors

    def _validated(self, vector: list[float]) -> list[float]:
        """Reject a vector that is empty or inconsistent with the ones before it."""
        if not vector:
            raise EmbedderRequestError(EMBEDDER_EMPTY_VECTOR_DETAIL)
        if self._dimensions is None:
            self._dimensions = len(vector)
        elif len(vector) != self._dimensions:
            detail = f"embedder returned a {len(vector)}-dimension vector, expected {self._dimensions}"
            raise EmbedderRequestError(detail)
        return vector

    def _disable_batching(self, reason: str) -> None:
        self._batching_disabled = True
        logger.warning("Knowledge embedder does not support batching; using one request per chunk", reason=reason)
