"""Rerank arms: score the shipped recall's candidates with a reranker, then keep the hits at or above a threshold.

`select_reranked` stands in for the shared `retrieve()` with the recall's selection (at most 5 hits, 800 o200k tokens,
a minimum reranker score), so the arm can switch to it once it exists. A turn whose reranker fails keeps the shipped
hybrid selection, as live recall will.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.config.knowledge import KnowledgeRerankerConfig
from mindroom.knowledge.rerank import create_reranker
from mindroom.memory._prompting import select_recall_hits
from mindroom.memory._shared import MemoryResult
from mindroom.model_defaults import OPENAI_DECISIONS_MODEL
from mindroom.token_counting.families import load_o200k

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.rerank import Reranker

    from .rerank_cache import CachedReranker
    from .turns import Turn

#: Live recall falls back to the hybrid selection when its reranker takes longer than this.
DEADLINE_S = 2.0
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


def _cross_encoder(model: str) -> Callable[[RuntimePaths], Reranker]:
    # sentence-transformers' `CrossEncoder.predict` applies a sigmoid to a one-label model such as bge-reranker-v2-m3,
    # so these scores are already in [0, 1].
    config = KnowledgeRerankerConfig(provider="sentence_transformers", model=model)
    return lambda runtime_paths: create_reranker(config, runtime_paths)


def _needs_judgment_reranker(runtime_paths: RuntimePaths) -> Reranker:
    _ = runtime_paths
    msg = "This reranker needs the judgment reranker (unified-retrieval PR 4), which does not exist yet"
    raise NotImplementedError(msg)


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
    ),
    "typesafe": RerankerSpec(
        provider="typesafe",
        model="jev-1.13.0",
        rubric_version="",
        usd_per_million_input_tokens=0.042,
        create=_needs_judgment_reranker,
    ),
}


@dataclass(frozen=True)
class TurnRerank:
    """One turn's reranker call: its scores (None when it failed), its latency when known, and its list price."""

    scores: list[float] | None
    seconds: float | None
    usd: float

    @property
    def fallback(self) -> bool:
        """Whether live recall would have kept the hybrid selection: the call failed or missed the deadline."""
        return self.scores is None or (self.seconds is not None and self.seconds > DEADLINE_S)


async def rerank_turns(
    turns: Sequence[Turn],
    candidates: Sequence[Sequence[MemoryResult] | None],
    reranker: CachedReranker,
    spec: RerankerSpec,
) -> list[TurnRerank | None]:
    """Score each turn's candidates, one turn at a time as live recall would; None for a turn with none to score."""
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
        tokens = 0
        if spec.usd_per_million_input_tokens:
            tokens = sum(len(load_o200k().encode_ordinary(text)) for text in (turn.query, *texts))
        reranked.append(
            TurnRerank(
                scores=scores,
                seconds=reranker.latency(turn.query, texts),
                usd=tokens * spec.usd_per_million_input_tokens / 1_000_000,
            ),
        )
    return reranked


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
