"""The retrieval stage every search shares: rerank its candidates when a reranker is set, then keep the leading ones."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, replace
from itertools import accumulate
from typing import TYPE_CHECKING

from mindroom.knowledge.read_protocol import DISTANCE_KEY
from mindroom.logging_config import get_logger
from mindroom.token_counting.counts import count
from mindroom.token_counting.families import o200k_counter

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.knowledge.read_protocol import SearchHit
    from mindroom.knowledge.rerank import Reranker

logger = get_logger(__name__)

# Absorbs float rounding, so a hit exactly ``hybrid_score_gap`` below the top hit is kept.
_SCORE_GAP_TOLERANCE = 1e-9
_O200K_COUNTER = o200k_counter()


@dataclass(frozen=True)
class Candidate[T]:
    """One search result on its way through retrieval."""

    item: T
    #: What a reranker reads and the token budget counts.
    text: str
    #: The hybrid score going in, the reranker's score coming out of a reranked retrieval.
    score: float


@dataclass(frozen=True)
class Selection:
    """Which leading candidates a retrieval keeps; the first candidate that fails a rule ends the selection."""

    limit: int
    #: The o200k tokens the kept texts may take in total.
    token_budget: int | None = None
    #: Without reranking, keep candidates whose hybrid score is within this of the best one's.
    hybrid_score_gap: float | None = None
    #: After reranking, keep candidates the reranker scored at or above this.
    min_rerank_score: float | None = None


def hybrid_scores(hits: Sequence[SearchHit]) -> list[float]:
    """Return a cosine-scale score per hit that never rises down the hybrid rank order.

    Hits arrive in hybrid rank order, which word matches can lift above closer vectors. Each score is the best cosine
    similarity at or below its rank, so scores stay on the cosine scale and sorting by them keeps the hybrid order.
    """
    return list(accumulate((1.0 - hit.meta_data[DISTANCE_KEY] for hit in reversed(hits)), max))[::-1]


def o200k_tokens(text: str) -> int:
    """Return the o200k tokens of ``text``: exact once the encoder has loaded, a quarter per character before.

    Never waits for the tokenizer, which loads in the background.
    """
    return count(_O200K_COUNTER, text)


async def retrieve[T](
    query: str,
    candidates: Sequence[Candidate[T]],
    selection: Selection,
    *,
    reranker: Reranker | None = None,
    deadline_seconds: float | None = None,
) -> list[Candidate[T]]:
    """Return the leading candidates ``selection`` keeps, in the reranker's order when one is set.

    A reranker that raises, passes ``deadline_seconds``, or does not return one score per candidate, each finite or
    ``-inf`` (its mark for a candidate it could not judge, which ranks last and passes no threshold), leaves the
    candidates in their given order, selected by their hybrid scores.
    """
    if reranker is None or not candidates:
        return _select(candidates, selection, reranked=False)
    try:
        scores = await asyncio.wait_for(reranker.ascore(query, [c.text for c in candidates]), deadline_seconds)
        if len(scores) != len(candidates) or any(math.isnan(score) or score == math.inf for score in scores):
            msg = f"reranker returned {len(scores)} scores for {len(candidates)} candidates, or NaN or +inf"
            raise ValueError(msg)  # noqa: TRY301 - one fallback for every way reranking fails
    except Exception:
        logger.warning("Reranking failed; keeping search order", exc_info=True)
        return _select(candidates, selection, reranked=False)
    rescored = [replace(candidate, score=score) for candidate, score in zip(candidates, scores, strict=True)]
    return _select(sorted(rescored, key=lambda candidate: -candidate.score), selection, reranked=True)


def _select[T](ranked: Sequence[Candidate[T]], selection: Selection, *, reranked: bool) -> list[Candidate[T]]:
    """Return the leading ``ranked`` candidates that pass every rule of ``selection``, up to the first that fails."""
    kept: list[Candidate[T]] = []
    tokens = 0
    for candidate in ranked[: max(selection.limit, 0)]:
        if reranked:
            threshold = selection.min_rerank_score
            if threshold is not None and (candidate.score < threshold or candidate.score == -math.inf):
                break
        elif (
            selection.hybrid_score_gap is not None
            and ranked[0].score - candidate.score > selection.hybrid_score_gap + _SCORE_GAP_TOLERANCE
        ):
            break
        if selection.token_budget is not None:
            tokens += o200k_tokens(candidate.text)
            if tokens > selection.token_budget:
                break
        kept.append(candidate)
    return kept
