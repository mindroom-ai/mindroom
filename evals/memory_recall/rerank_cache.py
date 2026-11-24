"""Reranker scores cached in SQLite, so a threshold sweep or a rerun never asks a provider twice for the same text."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.knowledge.rerank import Reranker


def checked_scores(scores: Sequence[float], count: int) -> list[float]:
    """Return `scores` when they are `count` finite numbers; raise ValueError otherwise."""
    if len(scores) != count or not all(math.isfinite(score) for score in scores):
        msg = f"Reranker returned {len(scores)} scores for {count} texts, or a non-finite score"
        raise ValueError(msg)
    return list(scores)


class RerankCache:
    """Reranker scores per key, in one SQLite file."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS scores (key TEXT PRIMARY KEY, score REAL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS billed (key TEXT PRIMARY KEY, input_tokens INTEGER)")
        self._db.commit()

    def score(self, key: str) -> float | None:
        """Return the cached score for `key`, or None."""
        row = self._db.execute("SELECT score FROM scores WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, scores: dict[str, float]) -> None:
        """Store `scores` in one transaction."""
        self._db.executemany("INSERT OR REPLACE INTO scores (key, score) VALUES (?, ?)", scores.items())
        self._db.commit()

    def billed(self, key: str) -> int | None:
        """Return the input tokens billed for the request `key`, or None."""
        row = self._db.execute("SELECT input_tokens FROM billed WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put_billed(self, key: str, input_tokens: int) -> None:
        """Store the input tokens billed for the request `key`."""
        self._db.execute("INSERT OR REPLACE INTO billed (key, input_tokens) VALUES (?, ?)", (key, input_tokens))
        self._db.commit()

    def close(self) -> None:
        """Close the SQLite file."""
        self._db.close()

    def __enter__(self) -> Self:
        """Return the cache, to close it on leaving the block."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the SQLite file."""
        self.close()


class CachedReranker:
    """A `Reranker` that sends a provider only the texts it has not scored for the query before.

    Keys are the sha256 of provider, model, rubric version, query and text, so a new rubric or model scores afresh.
    A wrong count or a non-finite score raises and is not cached. Latency is not measured here, since a cached or
    partly cached call says nothing about it: `rerank_arms.measure_latency` times uncached calls.

    When a call sent every text of its request and the inner reranker reports `last_input_tokens`, the provider's
    billed input is kept under the whole request, for `billed_input_tokens`.
    """

    def __init__(self, inner: Reranker, cache: RerankCache, *, provider: str, model: str, rubric_version: str) -> None:
        self._inner = inner
        self._cache = cache
        self._identity = [provider, model, rubric_version]

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        """Return one score per document, asking the provider only for the uncached ones."""
        keys = [self._key(query, text) for text in documents]
        scores = [self._cache.score(key) for key in keys]
        pending = [index for index, score in enumerate(scores) if score is None]
        if not pending:
            return [score for score in scores if score is not None]
        fresh = checked_scores(await self._inner.ascore(query, [documents[index] for index in pending]), len(pending))
        self._cache.put({keys[index]: score for index, score in zip(pending, fresh, strict=True)})
        billed = getattr(self._inner, "last_input_tokens", None)
        if billed is not None and len(pending) == len(documents):
            self._cache.put_billed(self._request_key(query, documents), billed)
        for index, score in zip(pending, fresh, strict=True):
            scores[index] = score
        return [score for score in scores if score is not None]

    def billed_input_tokens(self, query: str, documents: Sequence[str]) -> int | None:
        """Return the input tokens billed for one whole request scoring `documents` for `query`, or None."""
        return self._cache.billed(self._request_key(query, documents))

    def _key(self, query: str, text: str) -> str:
        return hashlib.sha256(json.dumps([*self._identity, query, text]).encode()).hexdigest()

    def _request_key(self, query: str, documents: Sequence[str]) -> str:
        return hashlib.sha256(json.dumps([*self._identity, query, list(documents)]).encode()).hexdigest()
