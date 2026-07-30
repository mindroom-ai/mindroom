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

from mindroom.embedding_errors import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EmbedderRequestError,
    describe_embedder_error,
    embedder_batch_cardinality_mismatch,
    embedder_failure_is_transient,
    is_embedder_auth_failure_detail,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = get_logger(__name__)

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
    _batching_disabled: bool = field(default=False, init=False, repr=False)
    _observed_dimensions: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Mirror the wrapped embedder's dimensions so vector writes stay consistent."""
        self.dimensions = self.inner.dimensions
        self.batch_size = self.inner.batch_size

    def supports_batching(self) -> bool:
        """Return whether the wrapped embedder can still embed a batch in one request."""
        return not self._batching_disabled and isinstance(self.inner, _SupportsBatchEmbedding)

    def clear_cache(self) -> None:
        """Drop prefetched vectors once their batch has been written."""
        self._cache.clear()

    def uncached(self, texts: Iterable[str]) -> list[str]:
        """Return the distinct texts that still need embedding, in first-seen order."""
        return list(dict.fromkeys(text for text in texts if text not in self._cache))

    def _validated(self, embedding: list[float]) -> list[float]:
        """Reject a vector that is empty or inconsistent with the ones before it.

        Width is checked against the first vector this adapter actually saw,
        not against ``Embedder.dimensions``: that field is a declared default
        (agno ships 1536) which real providers routinely contradict, so
        trusting it would reject correct vectors. Consistency still catches the
        case that matters here, a provider changing width mid-run.
        """
        if not embedding:
            raise EmbedderRequestError(EMBEDDER_EMPTY_VECTOR_DETAIL)
        if self._observed_dimensions is None:
            self._observed_dimensions = len(embedding)
        elif len(embedding) != self._observed_dimensions:
            detail = f"embedder returned a {len(embedding)}-dimension vector, expected {self._observed_dimensions}"
            raise EmbedderRequestError(detail)
        return embedding

    def _embed_each_into_cache(self, pending: Sequence[str]) -> int:
        """Embed one text per request, preserving input order.

        Successes are cached as they land, so a failure part-way through never
        costs the items already embedded.

        Prefetching is an optimization, so a fault affecting one text is left
        uncached and the file that owns it fails through the normal per-file
        path, where it is retried and recorded as a failed file. Only a
        credential rejection is re-raised: that is provably global, and
        grinding one doomed request per remaining chunk would bury the cause.
        """
        cached = 0
        for text in pending:
            try:
                self._cache[text] = self._validated(self.inner.get_embedding(text))
            except Exception as exc:
                if is_embedder_auth_failure_detail(describe_embedder_error(exc)):
                    raise
                logger.debug("Leaving one chunk unembedded for the per-file path", exc_info=True)
                continue
            cached += 1
        return cached

    def embed_batch_into_cache(self, texts: Sequence[str]) -> int:
        """Embed one planned batch, falling back to per-item when batching is unusable.

        Batch support is a capability claim that only a real multi-input
        request can test: some OpenAI-compatible backends accept an array and
        answer with a single vector, or reject the array outright. Treating
        that as a fatal error stalls every refresh on its first batch, so a
        non-transient batch failure retires batching for the rest of this run
        and the same texts are embedded one at a time instead.

        The fallback hides nothing: authentication, authorization, invalid
        model, and dimension failures surface again from the per-item requests
        with their existing semantics. Transient failures are re-raised
        untouched so the caller's retry and backoff still apply.
        """
        pending = [text for text in dict.fromkeys(texts) if text not in self._cache]
        if not pending:
            return 0
        if not self.supports_batching() or len(pending) == 1:
            # A single-input request proves nothing about batch support, so it
            # is never allowed to retire batching.
            return self._embed_each_into_cache(pending)

        inner = self.inner
        if not isinstance(inner, _SupportsBatchEmbedding):  # pragma: no cover - guarded by supports_batching
            return self._embed_each_into_cache(pending)

        try:
            embeddings = inner.get_embeddings_batch(list(pending))
        except Exception as exc:
            if embedder_failure_is_transient(exc) or not embedder_batch_cardinality_mismatch(exc):
                # Transient faults keep their retry, and everything else
                # (credentials, permissions, bad model, malformed payload)
                # would fail exactly the same way one input at a time. Falling
                # back would only turn one clear failure into one failed
                # request per chunk.
                raise
            self._disable_batching(f"multi-input request failed: {describe_embedder_error(exc)}")
            return self._embed_each_into_cache(pending)

        if len(embeddings) != len(pending):
            # The partial result is discarded on purpose: with fewer vectors
            # than inputs there is no trustworthy way to tell which input each
            # one belongs to, and guessing by position would silently attach a
            # vector to the wrong chunk. Vectors cached by earlier successful
            # batches are untouched.
            self._disable_batching(
                f"multi-input request returned {len(embeddings)} vectors for {len(pending)} inputs",
            )
            return self._embed_each_into_cache(pending)

        for text, embedding in zip(pending, embeddings, strict=True):
            self._cache[text] = self._validated(embedding)
        return len(pending)

    def _disable_batching(self, reason: str) -> None:
        self._batching_disabled = True
        logger.warning("Knowledge embedder does not support batching; using one request per chunk", reason=reason)

    def get_embedding(self, text: str) -> list[float]:
        """Return a prefetched embedding, or delegate to the wrapped embedder."""
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        # Validated here too: this is the path Agno's writer actually uses, so
        # skipping it would let an unusable vector reach the collection.
        return self._validated(self.inner.get_embedding(text))

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Return a prefetched embedding without usage, or delegate for a miss.

        Prefetched hits report no usage payload: usage was already accounted
        for by the batch request that produced the vector, and reporting it
        again per chunk would double-count it.
        """
        cached = self._cache.get(text)
        if cached is not None:
            return cached, None
        embedding, usage = self.inner.get_embedding_and_usage(text)
        return self._validated(embedding), usage

    async def async_get_embedding(self, text: str) -> list[float]:
        """Async variant of ``get_embedding``."""
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        return self._validated(await self.inner.async_get_embedding(text))

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Async variant of ``get_embedding_and_usage``."""
        cached = self._cache.get(text)
        if cached is not None:
            return cached, None
        embedding, usage = await self.inner.async_get_embedding_and_usage(text)
        return self._validated(embedding), usage
