"""Synthetic saved-response scoring and hard-budget tests."""

import json
from decimal import Decimal

import pytest

from scripts.judgment_replay.evaluation import Budget, evaluate_recorded, summarize, validate_labels


def _valid_response(*, confidence: float = 0.731234, input_tokens: int = 321) -> str:
    return json.dumps(
        {
            "model": "jev-1.13.0",
            "answers": {
                "queued_message_effect": {
                    "type": "choice",
                    "choice": "finish",
                    "probabilities": {"finish": 0.8, "wrap_up": 0.2},
                    "confidence": confidence,
                },
            },
            "usage": {"input_tokens": input_tokens, "output_tokens": 7},
        },
        separators=(",", ":"),
    )


def test_budget_reserves_unknown_bills_and_refuses_overspend() -> None:
    """An unbilled timeout still consumes its request and worst-case reservation."""
    budget = Budget(max_requests=2, max_cost_usd=Decimal("0.003"), input_per_million=Decimal("0.042"))
    assert budget.reserve()
    assert not budget.reserve()
    budget.settle(100)
    assert budget.reserve()
    assert not budget.reserve()
    assert budget.requests == 2


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0"])
def test_budget_requires_finite_positive_explicit_caps(amount: str) -> None:
    """Caps cannot disappear through invalid numeric inputs."""
    with pytest.raises(ValueError, match="finite and positive"):
        Budget(max_requests=1, max_cost_usd=Decimal(amount), input_per_million=Decimal("0.042"))


def test_recorded_invalid_response_falls_back_without_network() -> None:
    """Unmatched saved answers cannot be scored as valid predictions."""
    rows = evaluate_recorded(
        [{"case_id": "case", "request_hash": "expected"}],
        [{"case_id": "case", "request_hash": "different", "response": {}}],
        budget=Budget(max_requests=1, max_cost_usd=Decimal(1), input_per_million=Decimal("0.042")),
    )
    assert rows[0]["failure"] == "fixture_request_mismatch"
    assert rows[0]["probability_finish"] is None


def test_recorded_valid_response_retains_confidence_usage_and_actual_bill() -> None:
    """Saved API values must survive replay while cost uses billed input tokens rather than an estimate."""
    budget = Budget(max_requests=1, max_cost_usd=Decimal(1), input_per_million=Decimal("0.042"))

    rows = evaluate_recorded(
        [{"case_id": "case", "request_hash": "expected", "synthetic": True}],
        [
            {
                "case_id": "case",
                "request_hash": "expected",
                "response_json": _valid_response(),
                "latency_ms": 12.5,
            },
        ],
        budget=budget,
    )

    assert rows == [
        {
            "case_id": "case",
            "request_hash": "expected",
            "probability_finish": 0.8,
            "confidence": 0.731234,
            "failure": None,
            "input_tokens": 321,
            "output_tokens": 7,
            "latency_ms": 12.5,
            "mode": "recorded",
            "network_requests": 0,
            "synthetic": True,
            "choice": "finish",
            "model": "jev-1.13.0",
        },
    ]
    assert budget.requests == 1
    assert budget.unknown_bills == 0
    assert budget.actual_usd == Decimal("0.000013482")
    assert budget.charged_usd == budget.actual_usd


def test_malformed_saved_response_keeps_unknown_worst_case_reservation() -> None:
    """Missing provider usage must remain charged conservatively and must not trigger another attempt."""
    budget = Budget(max_requests=2, max_cost_usd=Decimal(1), input_per_million=Decimal("0.042"))

    rows = evaluate_recorded(
        [{"case_id": "case", "request_hash": "expected"}],
        [
            {
                "case_id": "case",
                "request_hash": "expected",
                "response_json": "not-json",
                "latency_ms": 1,
            },
        ],
        budget=budget,
    )

    assert rows[0]["failure"] == "invalid_response"
    assert budget.requests == 1
    assert budget.unknown_bills == 1
    assert budget.actual_usd == 0
    assert budget.charged_usd == Decimal("0.002688")


def test_recorded_request_cap_refuses_later_fixture_without_decoding_it() -> None:
    """A saved response beyond the explicit request cap remains a fallback prediction."""
    budget = Budget(max_requests=1, max_cost_usd=Decimal(1), input_per_million=Decimal("0.042"))
    cases = [
        {"case_id": "first", "request_hash": "first-hash"},
        {"case_id": "second", "request_hash": "second-hash"},
    ]
    fixtures = [
        {
            "case_id": case["case_id"],
            "request_hash": case["request_hash"],
            "response_json": _valid_response(),
            "latency_ms": 1,
        }
        for case in cases
    ]

    rows = evaluate_recorded(cases, fixtures, budget=budget)

    assert [row["failure"] for row in rows] == [None, "budget_exhausted"]
    assert budget.requests == 1


def test_independent_labels_required_and_unknown_cases_refused() -> None:
    """Independent human passes remain blind and attributable."""
    cases = [{"case_id": "one"}]
    labels = [
        {
            "case_id": "one",
            "label": "finish_correct",
            "labeler": "human-a",
            "pass": 1,
            "severe": False,
            "rubric": "queued-v1",
        },
    ]
    assert validate_labels(cases, labels)["agreed"] == {}
    labels.append({**labels[0], "pass": 2, "labeler": " human-a "})
    with pytest.raises(ValueError, match="independent"):
        validate_labels(cases, labels)
    labels[-1]["labeler"] = "human-b"
    assert validate_labels(cases, labels)["agreed"] == {"one": "finish_correct"}


def test_threshold_metrics_include_fallback_unknowns_and_severe_veto() -> None:
    """Unknown labels do not inflate quality, and failures keep wrap-up."""
    cases = [{"case_id": str(i), "thread_key": str(i), "split": "holdout", "tier": "verified"} for i in range(4)]
    labels = [
        {
            "case_id": str(i),
            "label": label,
            "pass": pass_number,
            "labeler": f"human-{pass_number}",
            "severe": i == 1,
            "rubric": "queued-v1",
        }
        for i, label in enumerate(["finish_correct", "wrap_up_needed", "finish_correct", "insufficient_evidence"])
        for pass_number in (1, 2)
    ]
    predictions = [
        {"case_id": "0", "probability_finish": 0.98, "failure": None},
        {"case_id": "1", "probability_finish": 0.99, "failure": None},
        {"case_id": "2", "probability_finish": None, "failure": "timeout"},
        {"case_id": "3", "probability_finish": 1.0, "failure": None},
    ]
    report = summarize(cases, labels, predictions, thresholds=[0.9])
    point = report["thresholds"][0]
    assert point["precision"] == 0.5
    assert point["recall"] == 0.5
    assert point["unsafe_finish_case_ids"] == ["1"]
    assert point["severe_case_veto"] is True
    assert point["precision_ci95_wilson"] == pytest.approx([0.0945312057, 0.9054687943])
    assert point["recall_ci95_wilson"] == pytest.approx([0.0945312057, 0.9054687943])
    assert point["unsafe_finish_ci95_wilson"] == pytest.approx([0.0945312057, 0.9054687943])
    assert point["scored_turns"] == 3
    assert report["labels"]["two_pass_cases"] == 4
    assert report["labels"]["single_pass_cases"] == 0
    assert report["baseline_classifier_cost_usd"] == 0
    assert report["gate"] == "no_go"
    assert report["chosen_threshold"] is None


def test_partial_evidence_and_duplicate_predictions_cannot_score() -> None:
    """Unsupported historical reconstruction never passes through a high probability."""
    cases = [{"case_id": "one", "tier": "partial", "split": "holdout", "thread_key": "thread"}]
    labels = [
        {
            "case_id": "one",
            "label": "finish_correct",
            "pass": p,
            "labeler": f"human-{p}",
            "severe": False,
            "rubric": "queued-v1",
        }
        for p in (1, 2)
    ]
    prediction = {"case_id": "one", "probability_finish": 1.0, "failure": None}
    assert summarize(cases, labels, [prediction], thresholds=[0.9])["thresholds"][0]["scored_turns"] == 0
    with pytest.raises(ValueError, match="duplicate"):
        summarize(cases, labels, [prediction, prediction], thresholds=[0.9])


def test_synthetic_fixture_prediction_never_scores_as_real_evidence() -> None:
    """A fixture sharing a verified case ID becomes a conservative missing real prediction."""
    cases = [{"case_id": "one", "tier": "verified", "split": "holdout", "thread_key": "thread"}]
    labels = [
        {
            "case_id": "one",
            "label": "finish_correct",
            "pass": label_pass,
            "labeler": f"human-{label_pass}",
            "severe": False,
            "rubric": "queued-v1",
        }
        for label_pass in (1, 2)
    ]
    prediction = {
        "case_id": "one",
        "probability_finish": 1.0,
        "failure": None,
        "synthetic": True,
    }

    report = summarize(cases, labels, [prediction], thresholds=[0.9])

    point = report["thresholds"][0]
    assert point["scored_turns"] == 1
    assert point["recall"] == 0
    assert point["confusion"]["missed_finish"] == 1
    assert report["synthetic_predictions_excluded"] == 1
    assert report["chosen_threshold"] is None


def test_missing_prediction_counts_as_conservative_fallback_in_recall() -> None:
    """Absent predictions must not disappear from the eligible positive denominator."""
    cases = [
        {"case_id": case_id, "tier": "verified", "split": "holdout", "thread_key": case_id}
        for case_id in ("predicted", "missing")
    ]
    labels = [
        {
            "case_id": case["case_id"],
            "label": "finish_correct",
            "pass": label_pass,
            "labeler": f"human-{label_pass}",
            "severe": False,
            "rubric": "queued-v1",
        }
        for case in cases
        for label_pass in (1, 2)
    ]
    predictions = [{"case_id": "predicted", "probability_finish": 0.99, "failure": None}]

    report = summarize(cases, labels, predictions, thresholds=[0.9])
    point = report["thresholds"][0]

    assert point["scored_turns"] == 2
    assert point["recall"] == 0.5
    assert point["confusion"]["missed_finish"] == 1
    assert report["eligible_labeled_turns"] == 2
    assert report["missing_prediction_count"] == 1
    assert report["exclusion_counts"] == {
        "no_agreed_binary_label": 0,
        "split_mismatch": 0,
        "unverified_tier": 0,
    }
