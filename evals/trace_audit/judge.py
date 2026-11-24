"""Blinded independent applicability judge with seeded candidate ordering and 0600 JSON cache."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from evals.trace_audit.baseline import CostAccumulator
from mindroom.agent_loop.helper import HelperResult, complete_prompt
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.providers import registry as provider_registry
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from mindroom.budgets.pricing import PricedModel
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

PROMPT_VERSION = "v1"
type ApplicabilityScore = Literal["direct", "partial", "none", "harmful"]
type CandidateArm = Literal["proposed_patch", "original_or_noop", "placebo_unrelated_skill"]
type BlindLabel = Literal["A", "B", "C"]
type BlindedJudgeCompleteFn = Callable[[str, Sequence[str]], Awaitable[HelperResult[BlindedJudgeResponse]]]

CANDIDATE_ARMS: tuple[CandidateArm, ...] = ("proposed_patch", "original_or_noop", "placebo_unrelated_skill")
_BLIND_LABELS: tuple[BlindLabel, ...] = ("A", "B", "C")
_VALID_SCORES: tuple[ApplicabilityScore, ...] = ("direct", "partial", "none", "harmful")
_MAX_TEXT_CHARS = 4_000
_MAX_RATIONALE_CHARS = 160

DEFAULT_NOOP_GUIDANCE = "No procedural skill guidance is provided for this task (empty baseline)."
DEFAULT_PLACEBO_SKILL = (
    "---\nname: meeting-notes-style\ndescription: Formatting conventions for meeting notes\n---\n"
    "1. Format dates using ISO-8601 (YYYY-MM-DD) with explicit UTC offsets.\n"
    "2. Group action items by owner at the bottom of the meeting summary.\n"
)
_JUDGE_SYSTEM_PROMPT = (
    "You are an independent, blinded evaluator judging whether candidate skill documents would have prevented "
    "a held-out AI agent failure.\nScore each candidate (A, B, C) independently:\n"
    "- direct: Directly addresses the failure's root cause with a clear procedural rule that prevents or fixes it.\n"
    "- partial: Related topic or background, but misses the key check needed to prevent this failure.\n"
    "- none: Unrelated, empty/no-op, or offers no actionable guidance for this failure.\n"
    "- harmful: Gives misleading, dangerous, or counterproductive instructions that would worsen the failure.\n"
    "Return scores for all three labels A, B, and C, with a concise rationale (<= 160 chars each)."
)


class BlindedCandidateScore(BaseModel):
    """One candidate's blinded applicability verdict."""

    label: BlindLabel
    score: ApplicabilityScore
    rationale: str = Field(default="", description="Concise <= 160 char justification.")


class BlindedJudgeResponse(BaseModel):
    """Structured JSON response containing verdicts for Candidates A, B, and C."""

    scores: list[BlindedCandidateScore]


@dataclass(frozen=True, slots=True)
class JudgeCase:
    """One held-out teachable failure paired with its three unblinded candidate arms."""

    item_id: str
    session_id: str
    cluster_key: str
    matched_skill: str
    failure_text: str
    proposed_patch: str
    original_or_noop: str
    placebo_unrelated_skill: str


def write_private_json(path: Path, payload: object) -> None:
    """Atomically write JSON with strict ``0600`` file permissions."""
    write_json_file_durable(path, payload, indent=2, sort_keys=True, trailing_newline=True)
    path.chmod(0o600)


def deterministic_arm_order(seed: int, item_id: str) -> tuple[CandidateArm, ...]:
    """Return a deterministic, seeded permutation of the three candidate arms for ``item_id``."""
    digest = hashlib.sha256(f"{seed}:{item_id}".encode()).digest()
    arms = list(CANDIDATE_ARMS)
    random.Random(int.from_bytes(digest[:8], "big")).shuffle(arms)  # noqa: S311
    return tuple(arms)


def _clip_redacted(text: str, limit: int) -> str:
    return redact_sensitive_text(text.strip(), max_length=limit)[:limit]


def _load_cache(path: Path) -> dict[str, dict[str, dict[str, str]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _cached_scores(entry: object) -> dict[BlindLabel, ApplicabilityScore] | None:
    if not isinstance(entry, dict):
        return None
    parsed: dict[BlindLabel, ApplicabilityScore] = {}
    for label in _BLIND_LABELS:
        val = entry.get(label)
        score = val.get("score") if isinstance(val, dict) else None
        if score not in _VALID_SCORES:
            return None
        parsed[label] = score
    return parsed


def make_blinded_judge_caller(config: Config, runtime_paths: RuntimePaths, judge_model: str) -> BlindedJudgeCompleteFn:
    """Create a lazy, tool-free structured LLM completion caller for blinded evaluation."""
    provider: Any = None

    async def _complete(prompt: str, instructions: Sequence[str]) -> HelperResult[BlindedJudgeResponse]:
        nonlocal provider
        if provider is None:
            provider = provider_registry.build_model_provider(config, runtime_paths, judge_model)
        return await complete_prompt(provider, prompt, instructions=instructions, schema=BlindedJudgeResponse)

    return _complete


async def evaluate_blinded_cases(
    cases: Sequence[JudgeCase],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    judge_model: str,
    cache_path: Path,
    seed: int,
    prices: Mapping[tuple[str, str], PricedModel],
    complete_fn: BlindedJudgeCompleteFn | None = None,
) -> dict[str, Any]:
    """Score held-out teachable failures with blinded candidate ordering and a 0600 JSON cache."""
    arm_counts: dict[CandidateArm, dict[ApplicabilityScore, int]] = {
        a: dict.fromkeys(_VALID_SCORES, 0) for a in CANDIDATE_ARMS
    }
    cost_acc = CostAccumulator(prices)
    caller = complete_fn or make_blinded_judge_caller(config, runtime_paths, judge_model)
    cache = _load_cache(cache_path) if cases else {}
    cache_dirty = False
    cache_hits = cache_misses = judge_errors = 0
    items: list[dict[str, Any]] = []

    try:
        for case in cases:
            arm_order = deterministic_arm_order(seed, case.item_id)
            failure_text = _clip_redacted(case.failure_text, _MAX_TEXT_CHARS)
            ordered: list[tuple[BlindLabel, str]] = [
                (lbl, _clip_redacted(str(getattr(case, arm)), _MAX_TEXT_CHARS))
                for lbl, arm in zip(_BLIND_LABELS, arm_order, strict=True)
            ]
            key = hashlib.sha256(
                json.dumps(
                    [PROMPT_VERSION, judge_model, failure_text, [[lbl, b] for lbl, b in ordered]],
                    sort_keys=True,
                ).encode("utf-8"),
            ).hexdigest()
            if (cached := _cached_scores(cache.get(key))) is not None:
                cache_hits += 1
                label_scores, was_cached = cached, True
            else:
                cache_misses += 1
                was_cached = False
                prompt = "\n\n".join(
                    [
                        f"=== HELD-OUT FAILURE ===\n{failure_text}",
                        *(f"=== Candidate {lbl} ===\n{b}" for lbl, b in ordered),
                    ],
                )
                result = await caller(prompt, (_JUDGE_SYSTEM_PROMPT,))
                cost_acc.add_helper_usage(result.usage)
                content = result.content
                by_label = (
                    {e.label: e for e in content.scores}
                    if isinstance(content, BlindedJudgeResponse) and len(content.scores) == len(_BLIND_LABELS)
                    else {}
                )
                if set(by_label) != set(_BLIND_LABELS):
                    logger.warning("trace_audit_judge_invalid", item_id=case.item_id, model=judge_model)
                    judge_errors += 1
                    continue
                label_scores = {lbl: by_label[lbl].score for lbl in _BLIND_LABELS}
                cache[key] = {
                    lbl: {
                        "score": by_label[lbl].score,
                        "rationale": _clip_redacted(by_label[lbl].rationale, _MAX_RATIONALE_CHARS),
                    }
                    for lbl in _BLIND_LABELS
                }
                cache_dirty = True

            scores_by_arm = {arm: label_scores[lbl] for lbl, arm in zip(_BLIND_LABELS, arm_order, strict=True)}
            for arm, score in scores_by_arm.items():
                arm_counts[arm][score] += 1
            items.append(
                {
                    "item_id": case.item_id,
                    "session_id": case.session_id,
                    "cluster_key": case.cluster_key,
                    "matched_skill": case.matched_skill,
                    "arm_order": list(arm_order),
                    "scores": scores_by_arm,
                    "cached": was_cached,
                },
            )
    finally:
        if cache_dirty:
            write_private_json(cache_path, cache)

    total = len(items)
    return {
        "total_evaluated": total,
        "judge_errors": judge_errors,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "arm_counts": arm_counts,
        "direct_hit_rate": round(arm_counts["proposed_patch"]["direct"] / total, 6) if total else 0.0,
        "baseline_direct_hit_rate": round(arm_counts["original_or_noop"]["direct"] / total, 6) if total else 0.0,
        "placebo_hit_rate": round(arm_counts["placebo_unrelated_skill"]["direct"] / total, 6) if total else 0.0,
        "harmful_rate": round(arm_counts["proposed_patch"]["harmful"] / total, 6) if total else 0.0,
        "cost": cost_acc.to_dict(),
        "items": items,
    }
