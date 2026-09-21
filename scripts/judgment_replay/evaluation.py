"""Fixture replay, conservative spend accounting, blind labels and quality summaries."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from mindroom.judgment.client import InvalidJudgmentResponseError, decode_response
from mindroom.judgment.state import PINNED_MODEL

if TYPE_CHECKING:
    from collections.abc import Sequence

_MAX_INPUT_TOKENS = 64_000
_LABELS = {"finish_correct", "wrap_up_needed", "insufficient_evidence"}


@dataclass
class Budget:
    """Reserve the provider's whole context ceiling before each paid attempt.

    Missing usage keeps the reservation charged. Known usage replaces it with
    actual input-token cost. A retry would require another explicit reservation.
    """

    max_requests: int
    max_cost_usd: Decimal
    input_per_million: Decimal
    requests: int = field(default=0, init=False)
    charged_usd: Decimal = field(default=Decimal(0), init=False)
    actual_usd: Decimal = field(default=Decimal(0), init=False)
    unknown_bills: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Validate explicit caps before accepting a budget."""
        if type(self.max_requests) is not int or self.max_requests < 1:
            msg = "request cap must be a positive integer"
            raise ValueError(msg)
        if any(not value.is_finite() or value <= 0 for value in (self.max_cost_usd, self.input_per_million)):
            msg = "cost cap and tariff must be finite and positive"
            raise ValueError(msg)

    def reserve(self) -> bool:
        """Refuse before request dispatch when either explicit cap is exhausted."""
        ceiling = self.input_per_million * _MAX_INPUT_TOKENS / 1_000_000
        if self.requests >= self.max_requests or self.charged_usd + ceiling > self.max_cost_usd:
            return False
        self.requests += 1
        self.unknown_bills += 1
        self.charged_usd += ceiling
        return True

    def settle(self, input_tokens: int) -> None:
        """Replace one pending reservation with its actual provider bill."""
        if type(input_tokens) is not int or not 0 <= input_tokens <= _MAX_INPUT_TOKENS or self.unknown_bills < 1:
            msg = "invalid or unreserved token bill"
            raise ValueError(msg)
        actual = self.input_per_million * input_tokens / 1_000_000
        self.charged_usd += actual - self.input_per_million * _MAX_INPUT_TOKENS / 1_000_000
        self.actual_usd += actual
        self.unknown_bills -= 1


def _by_case(rows: Sequence[dict]) -> dict[str, dict]:
    result = {}
    for row in rows:
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in result:
            msg = "missing or duplicate case ID"
            raise ValueError(msg)
        result[case_id] = row
    return result


def evaluate_recorded(cases: Sequence[dict], fixtures: Sequence[dict], *, budget: Budget) -> list[dict]:
    """Validate saved wire bytes through the same strict decoder, with zero I/O."""
    by_case = _by_case(fixtures)
    _by_case(cases)
    if set(by_case) - {case["case_id"] for case in cases}:
        msg = "fixture contains unknown cases"
        raise ValueError(msg)
    rows = []
    for case in cases:
        fixture = by_case.get(case["case_id"])
        row = {
            "case_id": case["case_id"],
            "request_hash": case.get("request_hash"),
            "probability_finish": None,
            "confidence": None,
            "failure": None,
            "input_tokens": None,
            "output_tokens": None,
            "latency_ms": None,
            "mode": "recorded",
            "network_requests": 0,
            "synthetic": case.get("synthetic") is True,
        }
        if fixture is None:
            row["failure"] = "missing_fixture"
        elif not case.get("request_hash") or fixture.get("request_hash") != case["request_hash"]:
            row["failure"] = "fixture_request_mismatch"
        elif not budget.reserve():
            row["failure"] = "budget_exhausted"
        else:
            row.update(_decode_fixture(fixture, budget))
        rows.append(row)
    return rows


def _decode_fixture(fixture: dict, budget: Budget) -> dict:
    # Fixtures retain response bytes as text so duplicate keys remain testable.
    response = fixture.get("response_json")
    if not isinstance(response, str):
        return {"failure": "invalid_fixture"}
    try:
        parsed = decode_response(response.encode(), expected_model=PINNED_MODEL)
        budget.settle(parsed.usage.input_tokens)
    except (InvalidJudgmentResponseError, ValueError):
        return {"failure": "invalid_response"}
    latency = fixture.get("latency_ms")
    if isinstance(latency, bool) or not isinstance(latency, int | float) or not math.isfinite(latency) or latency < 0:
        return {"failure": "invalid_fixture_latency"}
    return {
        "failure": None,
        "probability_finish": dict(parsed.answer.probabilities)["finish"],
        "confidence": parsed.answer.confidence,
        "choice": parsed.answer.choice,
        "model": parsed.model,
        "input_tokens": parsed.usage.input_tokens,
        "output_tokens": parsed.usage.output_tokens,
        "latency_ms": latency,
    }


def validate_labels(cases: Sequence[dict], labels: Sequence[dict]) -> dict:
    """Require two distinct human identities; retain disagreements and unknowns."""
    known = _by_case(cases)
    grouped: dict[str, dict[int, dict]] = {}
    for label in labels:
        case_id = label.get("case_id")
        pass_number = label.get("pass")
        if (
            case_id not in known
            or label.get("label") not in _LABELS
            or type(pass_number) is not int
            or pass_number not in {1, 2}
            or not isinstance(label.get("labeler"), str)
            or not label["labeler"].strip()
            or label.get("rubric") != "queued-v1"
            or type(label.get("severe")) is not bool
        ):
            msg = "invalid annotation identity, rubric, pass or label"
            raise ValueError(msg)
        passes = grouped.setdefault(case_id, {})
        if pass_number in passes:
            msg = "duplicate annotation pass"
            raise ValueError(msg)
        passes[pass_number] = label
    agreed = {}
    severe = []
    disagreement = []
    for case_id, passes in grouped.items():
        if len(passes) != 2:
            continue
        if passes[1]["labeler"].strip() == passes[2]["labeler"].strip():
            msg = "two independent labelers required"
            raise ValueError(msg)
        if passes[1]["label"] != passes[2]["label"]:
            disagreement.append(case_id)
        else:
            agreed[case_id] = passes[1]["label"]
        if any(row["severe"] for row in passes.values()):
            severe.append(case_id)
    return {
        "agreed": agreed,
        "severe": severe,
        "disagreement": disagreement,
        "two_pass_cases": sum(len(passes) == 2 for passes in grouped.values()),
        "single_pass_cases": sum(len(passes) == 1 for passes in grouped.values()),
    }


def _wilson(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    half = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def _threshold_point(scored: list[tuple[dict, str, dict]], threshold: float, severe: list[str]) -> dict:
    accepted = [
        (case, label)
        for case, label, prediction in scored
        if not prediction.get("failure")
        and prediction.get("probability_finish") is not None
        and prediction["probability_finish"] >= threshold
    ]
    unsafe = [case["case_id"] for case, label in accepted if label == "wrap_up_needed"]
    correct = len(accepted) - len(unsafe)
    positives = sum(label == "finish_correct" for _, label, _ in scored)
    accepted_threads = {case["thread_key"] for case, _ in accepted}
    unsafe_threads = {case["thread_key"] for case, label in accepted if label == "wrap_up_needed"}
    return {
        "threshold": threshold,
        "scored_turns": len(scored),
        "accepted_finish": len(accepted),
        "precision": correct / len(accepted) if accepted else None,
        "precision_ci95_wilson": _wilson(correct, len(accepted)),
        "recall": correct / positives if positives else None,
        "recall_ci95_wilson": _wilson(correct, positives),
        "unsafe_finish_case_ids": unsafe,
        "unsafe_finish_rate": len(unsafe) / len(accepted) if accepted else None,
        "unsafe_finish_ci95_wilson": _wilson(len(unsafe), len(accepted)),
        "accepted_threads": len(accepted_threads),
        "unsafe_threads": len(unsafe_threads),
        "unsafe_thread_ci95_wilson": _wilson(len(unsafe_threads), len(accepted_threads)),
        "severe_case_veto": bool(set(unsafe) & set(severe)),
        "confusion": {
            "true_finish": correct,
            "unsafe_finish": len(unsafe),
            "missed_finish": positives - correct,
            "correct_wrap_up": len(scored) - positives - len(unsafe),
        },
    }


def _eligible_scored_cases(
    cases: Sequence[dict],
    annotation: dict,
    predictions: dict[str, dict],
    split: str,
) -> tuple[list[tuple[dict, str, dict]], int, int, dict[str, int]]:
    """Retain every eligible denominator row, even when its real prediction is absent."""
    scored = []
    missing_prediction_count = 0
    synthetic_predictions_excluded = 0
    exclusion_counts = {
        "no_agreed_binary_label": 0,
        "split_mismatch": 0,
        "unverified_tier": 0,
    }
    for case in cases:
        if case.get("split") != split:
            exclusion_counts["split_mismatch"] += 1
            continue
        if case.get("tier") != "verified":
            exclusion_counts["unverified_tier"] += 1
            continue
        label = annotation["agreed"].get(case["case_id"])
        if label not in {"finish_correct", "wrap_up_needed"}:
            exclusion_counts["no_agreed_binary_label"] += 1
            continue
        prediction = predictions.get(case["case_id"])
        if prediction is None:
            missing_prediction_count += 1
            prediction = {"failure": "missing_prediction", "probability_finish": None}
        elif prediction.get("synthetic") is True:
            synthetic_predictions_excluded += 1
            prediction = {"failure": "synthetic_prediction", "probability_finish": None}
        scored.append((case, label, prediction))
    return scored, missing_prediction_count, synthetic_predictions_excluded, exclusion_counts


def summarize(
    cases: Sequence[dict],
    labels: Sequence[dict],
    predictions: Sequence[dict],
    *,
    thresholds: Sequence[float],
    split: str = "holdout",
) -> dict:
    """Report diagnostic curves on a single split, never silently choose a policy."""
    by_case = _by_case(predictions)
    known = _by_case(cases)
    if set(by_case) - set(known):
        msg = "predictions contain unknown cases"
        raise ValueError(msg)
    for row in predictions:
        probability = row.get("probability_finish")
        if probability is not None and (
            type(probability) not in {float, int} or not math.isfinite(probability) or not 0 <= probability <= 1
        ):
            msg = "invalid prediction probability"
            raise ValueError(msg)
    if any(type(value) not in {float, int} or not math.isfinite(value) or not 0.5 < value <= 1 for value in thresholds):
        msg = "thresholds must be finite and between 0.5 exclusive and 1 inclusive"
        raise ValueError(msg)
    annotation = validate_labels(cases, labels)
    scored, missing_prediction_count, synthetic_predictions_excluded, exclusion_counts = _eligible_scored_cases(
        cases,
        annotation,
        by_case,
        split,
    )
    return {
        "split": split,
        "case_count": len(cases),
        "prediction_count": len(predictions),
        "eligible_labeled_turns": len(scored),
        "missing_prediction_count": missing_prediction_count,
        "synthetic_predictions_excluded": synthetic_predictions_excluded,
        "exclusion_counts": exclusion_counts,
        "labels": annotation,
        "thresholds": [_threshold_point(scored, value, annotation["severe"]) for value in thresholds],
        "chosen_threshold": None,
        "baseline_classifier_cost_usd": 0,
        "gate": "no_go",
        "gate_reasons": ["risk_tolerance_not_set", "no_real_inference_authorized", "no_production_readiness_evidence"],
        "boundary_metrics": None,
        "unmeasured_experiments": [
            "minimal_vs_context",
            "whole_queue_vs_single",
            "adversarial_flip_rate",
            "alias_vs_pin",
            "ready_before_first_boundary",
            "suppression_age",
            "causal_cost_savings",
        ],
        "uncertainty": "Wilson intervals are diagnostic; turns within a thread are correlated. Thread intervals count any unsafe finish per thread.",
    }
