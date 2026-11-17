"""Reranker scores cached in SQLite, so a threshold sweep or a rerun never asks a provider twice for the same text."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.knowledge.rerank import Reranker


class RerankCache:
    """Scores per key, and the seconds of each request a single uncached call scored, in one SQLite file."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS scores (key TEXT PRIMARY KEY, score REAL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS latencies (key TEXT PRIMARY KEY, seconds REAL)")
        self._db.commit()

    def score(self, key: str) -> float | None:
        """Return the cached score for `key`, or None."""
        row = self._db.execute("SELECT score FROM scores WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def latency(self, key: str) -> float | None:
        """Return the cached seconds of the request `key`, or None."""
        row = self._db.execute("SELECT seconds FROM latencies WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, scores: dict[str, float], *, latency: tuple[str, float] | None) -> None:
        """Store `scores`, and the `(request key, seconds)` of the call that scored them, in one transaction."""
        self._db.executemany("INSERT OR REPLACE INTO scores (key, score) VALUES (?, ?)", scores.items())
        if latency is not None:
            self._db.execute("INSERT OR REPLACE INTO latencies (key, seconds) VALUES (?, ?)", latency)
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
    A call that scored every text of its request records its seconds under that request, the latency a live recall
    of that turn would add. A wrong count or a non-finite score raises and is not cached.
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
        start = time.perf_counter()
        fresh = await self._inner.ascore(query, [documents[index] for index in pending])
        seconds = time.perf_counter() - start
        if len(fresh) != len(pending) or not all(math.isfinite(score) for score in fresh):
            msg = f"Reranker returned {len(fresh)} scores for {len(pending)} texts, or a non-finite score"
            raise ValueError(msg)
        latency = (self._key(query, *documents), seconds) if len(pending) == len(documents) else None
        self._cache.put({keys[index]: score for index, score in zip(pending, fresh, strict=True)}, latency=latency)
        for index, score in zip(pending, fresh, strict=True):
            scores[index] = score
        return [score for score in scores if score is not None]

    def latency(self, query: str, documents: Sequence[str]) -> float | None:
        """Return the seconds of the uncached call that scored exactly `documents` for `query`, or None."""
        return self._cache.latency(self._key(query, *documents))

    def _key(self, query: str, *texts: str) -> str:
        return hashlib.sha256(json.dumps([*self._identity, query, *texts]).encode()).hexdigest()
