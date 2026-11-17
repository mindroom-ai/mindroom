"""Rerank arms: score the shipped recall's candidates with a reranker, then keep the hits at or above a threshold.

`select_reranked` selects through live recall's own `retrieve()` and selection (at most 5 hits, 800 o200k tokens, a
minimum reranker score). A turn whose reranker fails keeps the shipped hybrid selection, as live recall does.

Quality and latency are measured apart. Quality (`rerank_turns`) reads cached scores and selects from every score a
call returned, however long it took. Latency (`measure_latency`) times uncached calls of held-out turns one at a time
against the live provider, and counts as a fallback each call that failed or passed the deadline live recall waits.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from mindroom.config.judgment import OpenAIDecisionsJudgmentConfig, TypeSafeJudgmentConfig
from mindroom.config.knowledge import KnowledgeRerankerConfig
from mindroom.judgment import rerank as judgment_rerank
from mindroom.knowledge.rerank import _create_reranker
from mindroom.memory._file_backend import _RECALL_SELECTION, _retrieve_memories
from mindroom.model_defaults import OPENAI_DECISIONS_MODEL
from mindroom.token_counting.families import load_o200k

from .rerank_cache import checked_scores

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.config.judgment import ProbabilityJudgmentConfig
    from mindroom.constants import RuntimePaths
    from mindroom.judgment.answers import JudgmentResult
    from mindroom.judgment.state import JudgmentRequest
    from mindroom.knowledge.rerank import Reranker
    from mindroom.memory._shared import MemoryResult

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
    #: question. Only for the estimate a turn falls back to when the provider's billed input is unknown.
    request_overhead_tokens: int = 0
    candidate_overhead_tokens: int = 0
    #: Builds the reranker as live recall configures it, for latency mode; None means `create` already does.
    create_live: Callable[[RuntimePaths], Reranker] | None = None


def _cross_encoder(model: str) -> Callable[[RuntimePaths], Reranker]:
    # sentence-transformers' `CrossEncoder.predict` applies a sigmoid to a one-label model such as bge-reranker-v2-m3,
    # so these scores are already in [0, 1].
    config = KnowledgeRerankerConfig(provider="sentence_transformers", model=model)
    return lambda runtime_paths: _create_reranker(config, runtime_paths)


class _BilledJudgment:
    """The production judgment reranker, keeping the input tokens its provider billed for the last call.

    Calls must not overlap, as `rerank_turns` makes them one turn at a time.
    """

    def __init__(self, settings: ProbabilityJudgmentConfig, runtime_paths: RuntimePaths, *, gate: bool) -> None:
        reranker = judgment_rerank.create_judgment_reranker(settings, runtime_paths)
        if reranker is None:
            msg = f"The {settings.provider} reranker has no API key"
            raise RuntimeError(msg)
        batch = replace(reranker.batch, evaluate=self._billed(reranker.batch.evaluate))
        self._reranker = replace(reranker, batch=batch, gate=gate)
        self.last_input_tokens: int | None = None

    def _billed(
        self,
        evaluate: Callable[[JudgmentRequest], Awaitable[JudgmentResult[tuple[float | None, ...]]]],
    ) -> Callable[[JudgmentRequest], Awaitable[JudgmentResult[tuple[float | None, ...]]]]:
        async def billed(request: JudgmentRequest) -> JudgmentResult[tuple[float | None, ...]]:
            result = await evaluate(request)
            self.last_input_tokens = result.input_tokens
            return result

        return billed

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        """Score `documents` as the production reranker does."""
        self.last_input_tokens = None
        return await self._reranker.ascore(query, documents)


def _rubric_version(*, gate: bool) -> str:
    """Return a short hash of the questions a judgment request asks, so a changed rubric scores afresh."""
    questions = [judgment_rerank._candidate_question(0, "{passage}"), *([judgment_rerank._GATE] if gate else [])]
    return hashlib.sha256(repr(questions).encode()).hexdigest()[:12]


def _judgment(
    provider: Literal["openai_decisions", "typesafe"],
    *,
    gate: bool,
    usd_per_million_input_tokens: float,
) -> RerankerSpec:
    """Return a judgment arm's spec: quality calls wait up to `CALL_CAP_S`, latency calls time out as live ones."""

    def settings(**overrides: float) -> ProbabilityJudgmentConfig:
        if provider == "typesafe":
            return TypeSafeJudgmentConfig(provider="typesafe", **overrides)
        return OpenAIDecisionsJudgmentConfig(provider="openai_decisions", **overrides)

    return RerankerSpec(
        provider=provider,
        model="jev-1.13.0" if provider == "typesafe" else OPENAI_DECISIONS_MODEL,
        rubric_version=_rubric_version(gate=gate),
        usd_per_million_input_tokens=usd_per_million_input_tokens,
        create=lambda paths: _BilledJudgment(settings(timeout_seconds=CALL_CAP_S), paths, gate=gate),
        request_overhead_tokens=_JUDGMENT_REQUEST_OVERHEAD_TOKENS,
        candidate_overhead_tokens=_JUDGMENT_CANDIDATE_OVERHEAD_TOKENS,
        create_live=lambda paths: _BilledJudgment(settings(), paths, gate=gate),
    )


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
    "openai_decisions": _judgment("openai_decisions", gate=False, usd_per_million_input_tokens=0.10),
    "typesafe": _judgment("typesafe", gate=False, usd_per_million_input_tokens=0.042),
    "openai_decisions-gate": _judgment("openai_decisions", gate=True, usd_per_million_input_tokens=0.10),
    "typesafe-gate": _judgment("typesafe", gate=True, usd_per_million_input_tokens=0.042),
}


@dataclass(frozen=True)
class TurnRerank:
    """One turn's reranker scores (None when the call failed) and its list price, billed or estimated."""

    scores: list[float] | None
    usd: float
    #: Whether `usd` is the provider's billed input; otherwise it is the o200k estimate.
    billed: bool


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
    """Score each turn's candidates, from the cache where it can; None for a turn with none to score.

    A turn costs the input its provider billed for its whole request, when that is known; else the o200k estimate.
    """
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
        billed = reranker.billed_input_tokens(turn.query, texts)
        tokens = _estimated_tokens(spec, turn.query, texts) if billed is None else billed
        usd = tokens * spec.usd_per_million_input_tokens / 1_000_000
        reranked.append(TurnRerank(scores=scores, usd=usd, billed=billed is not None))
    return reranked


def _estimated_tokens(spec: RerankerSpec, query: str, texts: Sequence[str]) -> int:
    """Return the estimated input tokens of one request scoring `texts` for `query`."""
    tokens = sum(len(load_o200k().encode_ordinary(text)) for text in (query, *texts))
    return tokens + spec.request_overhead_tokens + spec.candidate_overhead_tokens * len(texts)


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


async def select_reranked(
    query: str,
    candidates: Sequence[MemoryResult],
    scores: Sequence[float] | None,
    *,
    threshold: float,
) -> list[MemoryResult]:
    """Return the candidates recall injects for `scores`, through live recall's own `retrieve()` selection.

    That is the best reranker score first (ties in hybrid order), at or above `threshold`, at most 5 hits within
    800 o200k tokens, each carrying its reranker score. Without scores (the reranker failed) this is the shipped
    hybrid selection.
    """
    # Counts exact o200k tokens: `retrieve` estimates them until the encoder has loaded in the background.
    load_o200k()
    if scores is None:
        return await _retrieve_memories(query, candidates, _RECALL_SELECTION)
    return await _retrieve_memories(
        query,
        candidates,
        replace(_RECALL_SELECTION, min_rerank_score=threshold),
        reranker=_Scored(scores),
    )


@dataclass(frozen=True)
class _Scored:
    """A reranker that answers with scores already fetched (from the cache)."""

    scores: Sequence[float]

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        _ = query, documents
        return list(self.scores)
