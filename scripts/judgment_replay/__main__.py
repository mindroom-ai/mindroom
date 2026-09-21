"""Command entry point: no network unless evaluate explicitly opts in."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.judgment_replay.commands import evaluate_command, label_command, report_command
from scripts.judgment_replay.corpus import extract_corpus
from scripts.judgment_replay.network import deny_network


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract", help="freeze local evidence and report coverage")
    extract.add_argument("--source", type=Path, required=True)
    extract.add_argument("--evidence", type=Path, required=True)
    extract.add_argument("--validation-after")
    extract.add_argument("--holdout-after")
    extract.add_argument("--source-revision", required=True)
    label = commands.add_parser("label", help="create blind templates or validate one human pass")
    label.add_argument("--evidence", type=Path, required=True)
    label.add_argument("--output", type=Path, required=True)
    label.add_argument("--labeler", required=True)
    label.add_argument("--pass", dest="label_pass", type=int, choices=(1, 2), required=True)
    label.add_argument("--annotations", type=Path)
    evaluate = commands.add_parser("evaluate", help="replay fixtures; live inference requires explicit authorization")
    evaluate.add_argument("--evidence", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--recorded-responses", type=Path)
    evaluate.add_argument("--allow-network", action="store_true")
    evaluate.add_argument("--inputs", type=Path)
    evaluate.add_argument("--reviewed-input-sha256")
    evaluate.add_argument("--max-requests", type=int, required=True)
    evaluate.add_argument("--max-cost-usd", required=True)
    evaluate.add_argument("--input-price-per-million", required=True)
    evaluate.add_argument("--price-date", required=True)
    report = commands.add_parser("report", help="report source coverage, uncertainty and no-go reasons")
    report.add_argument("--evidence", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--labels", type=Path, nargs="*", default=[])
    report.add_argument("--predictions", type=Path)
    report.add_argument("--thresholds", type=float, nargs="+", default=[0.6, 0.7, 0.8, 0.9, 0.95, 0.99])
    return parser


def main() -> None:
    """Run one explicit operation; never print source text or exception payloads."""
    parser = _parser()
    args = parser.parse_args()
    if args.command != "evaluate" or not args.allow_network:
        deny_network()
    if args.command == "evaluate" and not args.recorded_responses and not args.allow_network:
        parser.exit(2, "evaluate requires --recorded-responses or explicit --allow-network; network remains denied.\n")
    try:
        if args.command == "extract":
            summary = extract_corpus(
                args.source,
                args.evidence,
                validation_after=args.validation_after,
                holdout_after=args.holdout_after,
                source_revision=args.source_revision,
            )
        else:
            summary = {"label": label_command, "evaluate": evaluate_command, "report": report_command}[args.command](
                args,
            )
        print(json.dumps(summary, indent=2, sort_keys=True))
    except (ValueError, OSError) as error:
        parser.exit(2, f"Replay refused ({type(error).__name__}); inspect inputs locally.\n")


if __name__ == "__main__":
    main()
