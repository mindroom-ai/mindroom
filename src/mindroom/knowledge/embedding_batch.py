"""Batch the embedding requests a semantic knowledge refresh issues.

Agno embeds one chunk per provider request: ``ChromaDb._upsert`` calls
``Document.embed`` in a loop, and ``Document.embed`` calls
``Embedder.get_embedding_and_usage(text)``. A corpus of many small files is
therefore one HTTP round trip per file, which is the dominant cost of a large
build and far slower than the rate at which such corpora change.

Rather than reimplementing Agno's write path (and losing its id, metadata and
Chroma batching semantics), this module puts a narrow adapter at the MindRoom
boundary: ``BatchPrefetchEmbedder`` wraps the configured embedder and serves
already-embedded chunk texts from a short-lived cache. The indexer reads and
chunks a bounded batch of files first, embeds those chunk texts in as few
provider requests as the item and payload limits allow, and only then hands the
files to Agno, whose per-chunk embed calls become cache hits.

Cache misses fall through to the wrapped embedder unchanged, so behavior is
identical (only slower) for providers without batch support, for content that
changed between planning and insertion, and for query-time embedding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agno.knowledge.embedder.base import Embedder

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: Provider request limits. Both bounds matter: item count keeps a request from
#: exceeding per-request input limits, and payload size keeps a batch of large
#: chunks from producing a request the provider rejects outright.
DEFAULT_MAX_EMBEDDING_BATCH_ITEMS = 64
DEFAULT_MAX_EMBEDDING_BATCH_PAYLOAD_BYTES = 512_000


@runtime_checkable
class _SupportsBatchEmbedding(Protocol):
    """Embedder surface that can embed several texts in one provider request."""

    def get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding per input text, in input order."""
        ...


def plan_embedding_batches(
    texts: Sequence[str],
    *,
    max_items: int = DEFAULT_MAX_EMBEDDING_BATCH_ITEMS,
    max_payload_bytes: int = DEFAULT_MAX_EMBEDDING_BATCH_PAYLOAD_BYTES,
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
class BatchPrefetchEmbedder(Embedder):
    """Embedder that serves prefetched chunk embeddings from a bounded cache."""

    inner: Embedder = field(default_factory=Embedder)
    _cache: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)
    _provider_request_count: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        """Mirror the wrapped embedder's dimensions so vector writes stay consistent."""
        self.dimensions = self.inner.dimensions
        self.batch_size = self.inner.batch_size

    @property
    def provider_request_count(self) -> int:
        """Return how many provider requests this adapter has issued."""
        return self._provider_request_count

    def supports_batching(self) -> bool:
        """Return whether the wrapped embedder can embed a batch in one request."""
        return isinstance(self.inner, _SupportsBatchEmbedding)

    def clear_cache(self) -> None:
        """Drop prefetched vectors once their batch has been written."""
        self._cache.clear()

    def uncached(self, texts: Iterable[str]) -> list[str]:
        """Return the distinct texts that still need embedding, in first-seen order."""
        return list(dict.fromkeys(text for text in texts if text not in self._cache))

    def embed_batch_into_cache(self, texts: Sequence[str]) -> int:
        """Embed one planned batch in a single provider request and cache it.

        Raises whatever the wrapped embedder raises so the caller can classify
        the failure, retry a transient one, and fall back to per-text requests
        without re-embedding anything already cached.
        """
        pending = [text for text in dict.fromkeys(texts) if text not in self._cache]
        if not pending:
            return 0
        self._provider_request_count += 1
        if isinstance(self.inner, _SupportsBatchEmbedding):
            embeddings = self.inner.get_embeddings_batch(list(pending))
        else:
            embeddings = [self.inner.get_embedding(text) for text in pending]
        if len(embeddings) != len(pending):
            msg = f"embedder returned {len(embeddings)} embeddings for {len(pending)} inputs"
            raise ValueError(msg)
        for text, embedding in zip(pending, embeddings, strict=True):
            self._cache[text] = embedding
        return len(pending)

    def get_embedding(self, text: str) -> list[float]:
        """Return a prefetched embedding, or delegate to the wrapped embedder."""
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        self._provider_request_count += 1
        return self.inner.get_embedding(text)

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Return a prefetched embedding without usage, or delegate for a miss.

        Prefetched hits report no usage payload: usage was already accounted
        for by the batch request that produced the vector, and reporting it
        again per chunk would double-count it.
        """
        cached = self._cache.get(text)
        if cached is not None:
            return cached, None
        self._provider_request_count += 1
        return self.inner.get_embedding_and_usage(text)

    async def async_get_embedding(self, text: str) -> list[float]:
        """Async variant of ``get_embedding``."""
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        self._provider_request_count += 1
        return await self.inner.async_get_embedding(text)

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Async variant of ``get_embedding_and_usage``."""
        cached = self._cache.get(text)
        if cached is not None:
            return cached, None
        self._provider_request_count += 1
        return await self.inner.async_get_embedding_and_usage(text)
