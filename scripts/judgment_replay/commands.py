"""File-backed annotation, fixture evaluation and aggregate report commands."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from mindroom.judgment.client import SystemOneClient
from mindroom.judgment.state import (
    PINNED_MODEL,
    JudgmentMessage,
    JudgmentRequest,
    QueuedJudgmentInput,
    build_queued_judgment_request,
)
from scripts.judgment_replay.corpus import (
    digest,
    private_directory,
    read_records,
    records_from_bytes,
    refuse_symlinks,
    verify_manifest,
    write_private,
)
from scripts.judgment_replay.evaluation import Budget, evaluate_recorded, summarize, validate_labels

if TYPE_CHECKING:
    import argparse
    from pathlib import Path


def load_rows(path: Path) -> list[dict]:
    """Reject incomplete annotation or fixture files rather than quietly scoring a prefix."""
    errors = []
    rows = [row for _, row in read_records(path, errors=errors)]
    if errors:
        msg = "malformed input artifact"
        raise ValueError(msg)
    return rows


def label_command(args: argparse.Namespace) -> dict:
    """Make blind templates or validate an independently supplied annotation pass."""
    verify_manifest(args.evidence)
    cases = load_rows(args.evidence / "cases.jsonl")
    if args.annotations:
        labels = load_rows(args.annotations)
        validate_labels(cases, labels)
        if any(row["labeler"] != args.labeler or row["pass"] != args.label_pass for row in labels):
            msg = "annotation provenance disagrees with declared pass"
            raise ValueError(msg)
    else:
        labels = [
            {
                "case_id": case["case_id"],
                "label": None,
                "labeler": args.labeler,
                "pass": args.label_pass,
                "rubric": "queued-v1",
                "severe": False,
                "evidence_tier": case["tier"],
                "split": case["split"],
                "sources": case["sources"],
            }
            for case in cases
        ]
    write_private(args.output, labels, lines=True)
    return {"annotation_rows": len(labels), "template": not bool(args.annotations)}


def _inputs(args: argparse.Namespace, cases: list[dict]) -> list[dict]:
    if not args.inputs:
        return cases
    refuse_symlinks(args.inputs)
    raw = args.inputs.read_bytes()
    if args.allow_network and digest(raw) != args.reviewed_input_sha256:
        msg = "reviewed input hash required for live egress"
        raise ValueError(msg)
    inputs = records_from_bytes(raw)
    by_id = {case["case_id"]: case for case in cases}
    selected = []
    for item in inputs:
        if item.get("case_id") not in by_id:
            msg = "unknown input case"
            raise ValueError(msg)
        source = by_id[item["case_id"]]
        if args.recorded_responses and item.get("synthetic") is True:
            request = _request_for_input(item)
            supplied_hash = item.get("request_hash")
            if supplied_hash is not None and supplied_hash != request.request_hash:
                msg = "synthetic request hash mismatch"
                raise ValueError(msg)
            selected.append(
                {
                    **item,
                    "pending_membership_known": False,
                    "request_hash": request.request_hash,
                    "synthetic": True,
                    "tier": "synthetic",
                },
            )
            continue
        if item.get("synthetic") is True:
            msg = "synthetic inputs are fixture-only"
            raise ValueError(msg)
        if source["tier"] != "verified" or not source["pending_membership_known"]:
            msg = "historical pending membership is unobservable; evaluation refused"
            raise ValueError(msg)
        selected.append(item)
    return selected


def _request_for_input(case: dict) -> JudgmentRequest:
    value = case.get("input")
    if not isinstance(value, dict):
        msg = "judgment input must be an object"
        raise ValueError(msg)  # noqa: TRY004 - command refusal catches ValueError without exposing input details
    try:
        request = build_queued_judgment_request(
            QueuedJudgmentInput(
                active=JudgmentMessage(**value["active"]),
                queued=tuple(JudgmentMessage(**message) for message in value["queued"]),
                tool_names=tuple(value.get("tool_names", [])),
                context=tuple(JudgmentMessage(**message) for message in value.get("context", [])),
            ),
        )
    except (KeyError, TypeError) as exc:
        msg = "invalid judgment input shape"
        raise ValueError(msg) from exc
    if not request.complete:
        msg = "judgment input is incomplete"
        raise ValueError(msg)
    return request


async def _evaluate_live(cases: list[dict], *, budget: Budget) -> list[dict]:
    """Only reached after explicit CLI authorization and reviewed manifest checks."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        msg = "missing inference credential"
        raise ValueError(msg)
    rows = []
    client = SystemOneClient(api_key=key, model=PINNED_MODEL)
    for case in cases:
        if case.get("tier") != "verified" or not case.get("pending_membership_known") or "input" not in case:
            msg = "only verified complete reviewed inputs can leave the host"
            raise ValueError(msg)
        request = _request_for_input(case)
        if request.request_hash != case.get("request_hash"):
            msg = "input completeness or reviewed request hash mismatch"
            raise ValueError(msg)
        if not budget.reserve():
            rows.append({"case_id": case["case_id"], "failure": "budget_exhausted", "probability_finish": None})
            continue
        result = await client.judge(request, owner=case["case_id"], allow_network=True)
        if result.input_tokens is not None:
            budget.settle(result.input_tokens)
        rows.append(
            {
                "case_id": case["case_id"],
                "request_hash": request.request_hash,
                "model": result.model_id,
                "failure": result.failure,
                "probability_finish": dict(result.answer.probabilities)["finish"] if result.answer else None,
                "confidence": result.answer.confidence if result.answer else None,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": result.latency_ms,
                "mode": "live",
            },
        )
    return rows


def evaluate_command(args: argparse.Namespace) -> dict:
    """Require recorded fixtures or explicit network permission and reviewed inputs."""
    if not args.recorded_responses and not args.allow_network:
        msg = "evaluate requires --recorded-responses or explicit --allow-network"
        raise ValueError(msg)
    if args.recorded_responses and args.allow_network:
        msg = "recorded fixtures and live network are mutually exclusive"
        raise ValueError(msg)
    if date.fromisoformat(args.price_date) > datetime.now(UTC).date():
        msg = "price date cannot be in the future"
        raise ValueError(msg)
    private_directory(args.output.parent)
    for destination in (args.output, args.output.with_suffix(".summary.json")):
        refuse_symlinks(destination)
        if destination.exists():
            msg = "evaluation output already exists"
            raise ValueError(msg)
    verify_manifest(args.evidence)
    budget = Budget(args.max_requests, Decimal(args.max_cost_usd), Decimal(args.input_price_per_million))
    cases = _inputs(args, load_rows(args.evidence / "cases.jsonl"))
    if args.recorded_responses:
        predictions = evaluate_recorded(cases, load_rows(args.recorded_responses), budget=budget)
    else:
        if not args.inputs or not args.reviewed_input_sha256:
            msg = "live evaluation requires reviewed input manifest"
            raise ValueError(msg)
        predictions = asyncio.run(_evaluate_live(cases, budget=budget))
    write_private(args.output, predictions, lines=True)
    summary = {
        "mode": "recorded" if args.recorded_responses else "live",
        "attempts": budget.requests,
        "cost_usd": str(budget.actual_usd),
        "reserved_cost_usd": str(budget.charged_usd),
        "unknown_bills": budget.unknown_bills,
        "known_input_usage_cost_usd": str(budget.actual_usd),
        "unknown_usage_reserved_cost_usd": str(budget.charged_usd - budget.actual_usd),
        "price_per_million_input_tokens": str(budget.input_per_million),
        "price_date": args.price_date,
        "price_source": "https://docs.typesafe.ai/models",
        "model": PINNED_MODEL,
        "fixture_replay_cost_is_not_actual_spend": bool(args.recorded_responses),
        "synthetic_predictions": sum(row.get("synthetic") is True for row in predictions),
        "prediction_sha256": digest(args.output.read_bytes()),
    }
    write_private(args.output.with_suffix(".summary.json"), summary)
    return summary


def report_command(args: argparse.Namespace) -> dict:
    """Write aggregate evidence and no-go findings, without exporting source bodies."""
    verification = verify_manifest(args.evidence)
    cases = load_rows(args.evidence / "cases.jsonl")
    labels = [row for path in args.labels for row in load_rows(path)]
    predictions = load_rows(args.predictions) if args.predictions else []
    coverage = json.loads((args.evidence / "coverage.json").read_text())
    reports = {
        split: summarize(cases, labels, predictions, thresholds=args.thresholds, split=split)
        for split in ("development", "validation", "holdout")
    }
    lines = [
        "# Judgment replay report",
        "",
        "Gate: **no-go** for production wiring.",
        "",
        "No quality threshold or tolerated harmful-continuation risk has been selected.",
        "Offline replay cannot prove task completion, obedience to notices, or causal savings.",
        "",
        f"Manifest SHA-256: `{verification['manifest_sha256']}`.",
        f"Exact notice events: {coverage['notice_events']}; distinct response turns: {coverage['notice_turns']}.",
        f"Verified pending sets: {coverage['verified_pending_sets']}; eligible boundary count: unobservable.",
        "Provider observations may repeat one notice; they are not independent injection boundaries.",
        "",
        "| Source | Matched turns |",
        "|---|---:|",
        *[f"| {kind} | {count} |" for kind, count in coverage["coverage"].items()],
        "",
        "Surface A: no observed evaluation cohort; candidates do not prove historical eligibility.",
        f"Observed participation-instruction candidates: {coverage['surface_a']['observed_candidates']}.",
        "The 306-versus-51 export-window comparison is a heuristic warning, not a false-positive rate.",
        "",
        "Baseline classifier cost: $0.",
        "No measured TypeSafe bill, latency, selected-threshold precision/recall, or readiness claim follows from synthetic fixtures.",
        "Missing measurements: boundary timing, whole-queue/context ablations, adversarial quality, alias comparison and suppression age.",
        "",
        "## Coverage and reproducible exclusions",
        "",
        "```json",
        json.dumps(coverage, indent=2, sort_keys=True),
        "```",
        "",
        "## Split-specific diagnostic metrics",
        "",
        "Thresholds below are diagnostic sweep points, not chosen policy defaults.",
        "Unsafe examples use case hashes; private evidence supplies their source references.",
        "",
        "```json",
        json.dumps(reports, indent=2, sort_keys=True),
        "```",
        "",
    ]
    # Markdown still uses private exclusive file creation.
    private_directory(args.output.parent)
    refuse_symlinks(args.output)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as handle:
        handle.write("\n".join(lines))
    return {"gate": "no_go", "notice_turns": len(cases), "labels": len(labels), "predictions": len(predictions)}
