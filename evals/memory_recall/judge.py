"""Judge how relevant each recalled memory hit is to its turn, with an LLM and a SQLite cache of verdicts.

The replay has no assistant reply, so the judge rates relevance to the incoming message only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import httpx

# Bump whenever PROMPT changes, so cached verdicts from an older rubric are not reused.
PROMPT_VERSION = "v1"
PROMPT = """You audit an AI assistant's automatic memory recall. Before the assistant answered, the system searched its \
memory notes with the incoming message as query and injected the hits below into the prompt. Judge each hit for THIS turn.

For each hit give:
- "relevance": 2 = directly relevant: contains a fact, preference or context the assistant needs or that clearly \
improves this answer; 1 = same topic/background but not needed; 0 = unrelated to this turn.
- "why": at most 15 words.

Return ONLY JSON: {{"hits": [{{"relevance": 0|1|2, "why": "..."}}, ...]}} with exactly {n} entries in order.

=== INCOMING MESSAGE ===
{query}

{hits}
"""

_logger = logging.getLogger(__name__)

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_ATTEMPTS = 3
_RETRY_DELAY_S = 2.0
_TIMEOUT_S = 180
_MAX_QUERY_CHARS = 5000
_MAX_HIT_CHARS = 6000


@dataclass(frozen=True)
class Verdict:
    """The judge's rating of one hit: 0 unrelated, 1 same topic but not needed, 2 directly relevant."""

    relevance: int
    why: str


def cache_key(model: str, query: str, hit: str) -> str:
    """Return the cache key of judging `hit` for `query` with `model` under the current prompt."""
    return hashlib.sha256(json.dumps([model, PROMPT_VERSION, query, hit]).encode()).hexdigest()


class JudgeCache:
    """Verdicts stored in a SQLite file, keyed by `cache_key`."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS verdicts (key TEXT PRIMARY KEY, relevance INTEGER, why TEXT)")
        self._db.commit()

    def get(self, key: str) -> Verdict | None:
        """Return the cached verdict for `key`, or None."""
        row = self._db.execute("SELECT relevance, why FROM verdicts WHERE key = ?", (key,)).fetchone()
        return Verdict(row[0], row[1]) if row else None

    def put(self, key: str, verdict: Verdict) -> None:
        """Store `verdict` under `key`."""
        self._db.execute(
            "INSERT OR REPLACE INTO verdicts (key, relevance, why) VALUES (?, ?, ?)",
            (key, verdict.relevance, verdict.why),
        )
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


async def judge_hits(
    query: str,
    hits: Sequence[str],
    *,
    cache: JudgeCache,
    client: httpx.AsyncClient,
    model: str,
    api_key: str,
) -> list[Verdict | None]:
    """Return a verdict per hit, in order; None marks a hit the judge failed on.

    Cached hits are not re-judged; the others go to the judge in one request, retried on a bad response.
    """
    keys = [cache_key(model, query, hit) for hit in hits]
    verdicts = [cache.get(key) for key in keys]
    pending = [index for index, verdict in enumerate(verdicts) if verdict is None]
    if not pending:
        return verdicts
    fresh = await _request_verdicts(query, [hits[index] for index in pending], client, model, api_key)
    if fresh is None:
        return verdicts
    for index, verdict in zip(pending, fresh, strict=True):
        cache.put(keys[index], verdict)
        verdicts[index] = verdict
    return verdicts


async def _request_verdicts(
    query: str,
    hits: list[str],
    client: httpx.AsyncClient,
    model: str,
    api_key: str,
) -> list[Verdict] | None:
    """Ask the judge about `hits`; return its verdicts, or None when every attempt gave a bad response."""
    listing = "\n\n".join(f"=== HIT {number} ===\n{_clip(hit, _MAX_HIT_CHARS)}" for number, hit in enumerate(hits, 1))
    prompt = PROMPT.format(n=len(hits), query=_clip(query, _MAX_QUERY_CHARS), hits=listing)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 1500,
    }
    error = ""
    for attempt in range(_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_RETRY_DELAY_S * attempt)
        try:
            response = await client.post(
                _ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
                timeout=_TIMEOUT_S,
            )
            response.raise_for_status()
            verdicts = _parse(response.json()["choices"][0]["message"]["content"] or "")
        except Exception as exc:  # any failure (HTTP, transport, missing choices, bad JSON) is retried
            error = f"{type(exc).__name__}: {exc}"
            continue
        if len(verdicts) == len(hits):
            return verdicts
        error = f"{len(verdicts)} verdicts for {len(hits)} hits"
    _logger.warning("Judge failed after %d attempts: %.300s", _ATTEMPTS, error)
    return None


def _parse(content: str) -> list[Verdict]:
    """Return the verdicts in the judge's JSON reply; raise ValueError when it is malformed."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match is None:
        msg = "no JSON object in judge reply"
        raise ValueError(msg)
    entries: list[Any] = json.loads(match.group(0))["hits"]
    verdicts = []
    for entry in entries:
        relevance = entry["relevance"]
        if type(relevance) is not int or relevance not in (0, 1, 2):
            msg = f"relevance out of range: {relevance!r}"
            raise ValueError(msg)
        verdicts.append(Verdict(relevance, str(entry.get("why", ""))))
    return verdicts


def _clip(text: str, limit: int) -> str:
    """Return `text`, or its head and tail around a `[...]` marker when longer than `limit`."""
    return text if len(text) <= limit else text[: limit // 2] + "\n[...]\n" + text[-limit // 2 :]
