"""Rerank arms: score the shipped recall's candidates with a reranker, then keep the hits at or above a threshold.

`select_reranked` stands in for the shared `retrieve()` with the recall's selection (at most 5 hits, 800 o200k tokens,
a minimum reranker score), so the arm can switch to it once it exists. A turn whose reranker fails keeps the shipped
hybrid selection, as live recall will.

Quality and latency are measured apart. Quality (`rerank_turns`) reads cached scores and selects from every score a
call returned, however long it took. Latency (`measure_latency`) times uncached calls of held-out turns one at a time
against the live provider, and counts as a fallback each call that failed or passed the deadline live recall waits.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.config.knowledge import KnowledgeRerankerConfig
from mindroom.knowledge.rerank import create_reranker
from mindroom.memory._prompting import select_recall_hits
from mindroom.memory._shared import MemoryResult
from mindroom.model_defaults import OPENAI_DECISIONS_MODEL
from mindroom.token_counting.families import load_o200k

from .rerank_cache import checked_scores

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.rerank import Reranker

    from .rerank_cache import CachedReranker
    from .turns import Turn

#: Live recall falls back to the hybrid selection when its reranker takes longer than this.
DEADLINE_S = 2.0
#: Latency mode stops waiting for one call after this, records it as a timeout and moves on.
CALL_CAP_S = 30.0
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankerSpec:
    """A reranker an arm names: what its scores are cached under, its list price, and how to build it."""

    provider: str
    model: str
    #: Changes whenever the request or the score scale changes, so earlier cached scores are not reused.
    rubric_version: str
    usd_per_million_input_tokens: float
    create: Callable[[RuntimePaths], Reranker]
    #: Input tokens a priced request adds besides the query and candidate texts: its framing, and each candidate's
    #: question. Estimates until the provider's billed input is measured.
    request_overhead_tokens: int = 0
    candidate_overhead_tokens: int = 0


def _cross_encoder(model: str) -> Callable[[RuntimePaths], Reranker]:
    # sentence-transformers' `CrossEncoder.predict` applies a sigmoid to a one-label model such as bge-reranker-v2-m3,
    # so these scores are already in [0, 1].
    config = KnowledgeRerankerConfig(provider="sentence_transformers", model=model)
    return lambda runtime_paths: create_reranker(config, runtime_paths)


def _needs_judgment_reranker(runtime_paths: RuntimePaths) -> Reranker:
    _ = runtime_paths
    msg = "This reranker needs the judgment reranker (unified-retrieval PR 4), which does not exist yet"
    raise NotImplementedError(msg)


# The plan's estimate of a judgment request: about 60 rubric tokens per candidate, plus framing around them.
_JUDGMENT_REQUEST_OVERHEAD_TOKENS = 100
_JUDGMENT_CANDIDATE_OVERHEAD_TOKENS = 60
RERANKERS: dict[str, RerankerSpec] = {
    "bge-reranker-v2-m3": RerankerSpec(
        provider="sentence_transformers",
        model="BAAI/bge-reranker-v2-m3",
        rubric_version="predict-sigmoid",
        usd_per_million_input_tokens=0.0,
        create=_cross_encoder("BAAI/bge-reranker-v2-m3"),
    ),
    "openai_decisions": RerankerSpec(
        provider="openai_decisions",
        model=OPENAI_DECISIONS_MODEL,
        rubric_version="",
        usd_per_million_input_tokens=0.10,
        create=_needs_judgment_reranker,
        request_overhead_tokens=_JUDGMENT_REQUEST_OVERHEAD_TOKENS,
        candidate_overhead_tokens=_JUDGMENT_CANDIDATE_OVERHEAD_TOKENS,
    ),
    "typesafe": RerankerSpec(
        provider="typesafe",
        model="jev-1.13.0",
        rubric_version="",
        usd_per_million_input_tokens=0.042,
        create=_needs_judgment_reranker,
        request_overhead_tokens=_JUDGMENT_REQUEST_OVERHEAD_TOKENS,
        candidate_overhead_tokens=_JUDGMENT_CANDIDATE_OVERHEAD_TOKENS,
    ),
}


@dataclass(frozen=True)
class TurnRerank:
    """One turn's reranker scores (None when the call failed) and its estimated list price."""

    scores: list[float] | None
    usd: float


@dataclass(frozen=True)
class LatencyCall:
    """One uncached reranker call: its seconds (`CALL_CAP_S` for a timeout) and how it ended."""

    seconds: float
    #: `ok` returned valid scores, `error` raised or returned invalid ones, `timeout` hit `CALL_CAP_S`.
    outcome: Literal["ok", "error", "timeout"]

    @property
    def fallback(self) -> bool:
        """Whether live recall would have kept the hybrid selection: the call failed or passed the deadline."""
        return self.outcome != "ok" or self.seconds > DEADLINE_S

    @property
    def added_seconds(self) -> float:
        """The wait the call adds to a live turn, which stops waiting at the deadline."""
        return min(self.seconds, DEADLINE_S)


@dataclass(frozen=True)
class LatencySample:
    """One arm's timed calls, and why its measurement stopped early, if it did."""

    calls: list[LatencyCall]
    failure: str | None


async def rerank_turns(
    turns: Sequence[Turn],
    candidates: Sequence[Sequence[MemoryResult] | None],
    reranker: CachedReranker,
    spec: RerankerSpec,
) -> list[TurnRerank | None]:
    """Score each turn's candidates, from the cache where it can; None for a turn with none to score."""
    reranked: list[TurnRerank | None] = []
    for turn, turn_candidates in zip(turns, candidates, strict=True):
        if not turn_candidates:
            reranked.append(None)
            continue
        texts = [candidate.get("memory", "") for candidate in turn_candidates]
        try:
            scores = await reranker.ascore(turn.query, texts)
        except Exception as exc:  # any failure: live recall keeps the hybrid selection
            _logger.warning("Reranking a turn failed; it keeps the hybrid selection: %.300s", exc)
            scores = None
        reranked.append(TurnRerank(scores=scores, usd=_usd(spec, turn.query, texts)))
    return reranked


def _usd(spec: RerankerSpec, query: str, texts: Sequence[str]) -> float:
    """Return the estimated list price of one request scoring `texts` for `query`."""
    if not spec.usd_per_million_input_tokens:
        return 0.0
    tokens = sum(len(load_o200k().encode_ordinary(text)) for text in (query, *texts))
    tokens += spec.request_overhead_tokens + spec.candidate_overhead_tokens * len(texts)
    return tokens * spec.usd_per_million_input_tokens / 1_000_000


async def measure_latency(
    reranker: Reranker,
    turns: Sequence[Turn],
    candidates: Sequence[Sequence[MemoryResult] | None],
    *,
    count: int,
) -> LatencySample:
    """Time the uncached reranker call of the first `count` given turns that have candidates, one turn at a time.

    One untimed warm-up call first loads what a long-running process keeps loaded, such as a local model; a warm-up
    that reaches `CALL_CAP_S` ends the measurement with a failure. A call is not cancelled at the deadline, so a slow
    call does not leave work behind to delay the next one; it counts as a fallback instead. A call still running at
    `CALL_CAP_S` is cancelled and recorded as a timeout. A local model's thread cannot be stopped, so the next call
    may wait behind it.
    """
    requests = [
        (turn.query, [candidate.get("memory", "") for candidate in turn_candidates])
        for turn, turn_candidates in zip(turns, candidates, strict=True)
        if turn_candidates
    ][:count]
    if not requests:
        return LatencySample(calls=[], failure=None)
    try:
        async with asyncio.timeout(CALL_CAP_S):
            await reranker.ascore(*requests[0])
    except TimeoutError:
        return LatencySample(calls=[], failure=f"warm-up call did not return within {CALL_CAP_S:g} s")
    except Exception as exc:  # the warm-up only loads; the timed call of this turn reports its outcome
        _logger.warning("Warm-up rerank call failed: %.300s", exc)
    calls: list[LatencyCall] = []
    for query, texts in requests:
        start = time.perf_counter()
        try:
            async with asyncio.timeout(CALL_CAP_S):
                scores = await reranker.ascore(query, texts)
            checked_scores(scores, len(texts))
        except TimeoutError:
            _logger.warning("Timed rerank call did not return within %g s", CALL_CAP_S)
            calls.append(LatencyCall(seconds=CALL_CAP_S, outcome="timeout"))
            continue
        except Exception as exc:  # any failure: live recall keeps the hybrid selection
            _logger.warning("Timed rerank call failed: %.300s", exc)
            outcome = "error"
        else:
            outcome = "ok"
        calls.append(LatencyCall(seconds=time.perf_counter() - start, outcome=outcome))
    return LatencySample(calls=calls, failure=None)


def select_reranked(
    candidates: Sequence[MemoryResult],
    scores: Sequence[float] | None,
    *,
    threshold: float,
) -> list[MemoryResult]:
    """Return the candidates recall injects: best reranker score first, at or above `threshold`, within the budget.

    Ties keep the hybrid order. Each kept hit carries its reranker score. Without scores (the reranker failed) this
    is the shipped hybrid selection.
    """
    # Counts exact o200k tokens: `select_recall_hits` estimates them until the encoder has loaded in the background.
    load_o200k()
    if scores is None:
        return select_recall_hits(candidates)
    ranked = sorted(zip(scores, candidates, strict=True), key=lambda pair: -pair[0])
    kept: list[MemoryResult] = []
    for score, candidate in ranked:
        if score < threshold:
            break
        result = MemoryResult(**candidate)
        result["score"] = score
        kept.append(result)
    # Hit count and token budget as the shipped recall's; no score gap, since the threshold replaces it.
    return select_recall_hits(kept, score_gap=math.inf)
