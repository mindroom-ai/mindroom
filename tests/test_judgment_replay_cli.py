"""Offline CLI runs in fresh processes with networking denied."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mindroom.judgment.state import JudgmentMessage, QueuedJudgmentInput, build_queued_judgment_request
from scripts.judgment_replay import commands
from scripts.judgment_replay.corpus import digest, extract_corpus


def test_offline_guard_denies_socket_and_subprocess() -> None:
    """The guard applies below HTTP clients and rejects process escape."""
    program = """
from scripts.judgment_replay.network import deny_network
import socket
import subprocess
deny_network()
for operation in (lambda: socket.socket(), lambda: subprocess.run(['true'])):
    try:
        operation()
    except PermissionError:
        continue
    raise AssertionError('offline guard bypassed')
"""
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()


def test_cli_label_report_and_evaluation_refusal(tmp_path: Path) -> None:
    """Real command dispatch needs neither credentials nor a network connection."""
    source = tmp_path / "source"
    (source / "logs").mkdir(parents=True)
    (source / "logs/mindroom_one.log").write_text(
        json.dumps(
            {
                "event": "queued_message_notice_injected",
                "response_turn_id": "synthetic",
                "timestamp": "2026-09-01T00:00:00Z",
                "room_id": "room",
                "thread_id": "thread",
            },
        ),
    )
    evidence = tmp_path / "evidence"
    extract_corpus(source, evidence)
    prefix = [sys.executable, "-m", "scripts.judgment_replay"]
    for command in (
        [
            "label",
            "--evidence",
            str(evidence),
            "--output",
            str(tmp_path / "labels.jsonl"),
            "--labeler",
            "human-a",
            "--pass",
            "1",
        ],
        ["report", "--evidence", str(evidence), "--output", str(tmp_path / "report.md")],
    ):
        result = subprocess.run([*prefix, *command], capture_output=True, check=False)
        assert result.returncode == 0, result.stderr.decode()
    template = json.loads((tmp_path / "labels.jsonl").read_text())
    assert template["label"] is None
    report = (tmp_path / "report.md").read_text()
    assert "no-go" in report.lower()
    assert "Surface A" in report
    result = subprocess.run(
        [
            *prefix,
            "evaluate",
            "--evidence",
            str(evidence),
            "--output",
            str(tmp_path / "predictions.jsonl"),
            "--max-requests",
            "1",
            "--max-cost-usd",
            "1",
            "--input-price-per-million",
            "0.042",
            "--price-date",
            "2026-09-20",
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert b"--allow-network" in result.stderr
    assert not (tmp_path / "predictions.jsonl").exists()


def test_cli_replays_explicit_synthetic_fixture_without_network(tmp_path: Path) -> None:
    """Recorded replay computes the leaf hash and stays visibly synthetic from input through output."""
    source = tmp_path / "source"
    (source / "logs").mkdir(parents=True)
    (source / "logs/mindroom_one.log").write_text(
        json.dumps(
            {
                "event": "queued_message_notice_injected",
                "response_turn_id": "synthetic",
                "timestamp": "2026-09-01T00:00:00Z",
                "room_id": "room",
                "thread_id": "thread",
            },
        ),
    )
    evidence = tmp_path / "evidence"
    extract_corpus(source, evidence)
    case = json.loads((evidence / "cases.jsonl").read_text())
    input_value = {
        "active": {"sender": "alice", "text": "Install the package."},
        "queued": [{"sender": "alice", "text": "Thanks."}],
        "tool_names": ["read_file"],
        "context": [],
    }
    request = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(**input_value["active"]),
            queued=tuple(JudgmentMessage(**message) for message in input_value["queued"]),
            tool_names=tuple(input_value["tool_names"]),
        ),
    )
    inputs = tmp_path / "synthetic-inputs.jsonl"
    inputs.write_text(json.dumps({"case_id": case["case_id"], "synthetic": True, "input": input_value}) + "\n")
    fixtures = tmp_path / "recorded.jsonl"
    fixtures.write_text(
        json.dumps(
            {
                "case_id": case["case_id"],
                "request_hash": request.request_hash,
                "response_json": json.dumps(
                    {
                        "model": "jev-1.13.0",
                        "answers": {
                            "queued_message_effect": {
                                "type": "choice",
                                "choice": "finish",
                                "probabilities": {"finish": 0.8, "wrap_up": 0.2},
                                "confidence": 0.731234,
                            },
                        },
                        "usage": {"input_tokens": 321, "output_tokens": 7},
                    },
                ),
                "latency_ms": 12.5,
            },
        )
        + "\n",
    )
    output = tmp_path / "predictions.jsonl"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.judgment_replay",
            "evaluate",
            "--evidence",
            str(evidence),
            "--output",
            str(output),
            "--recorded-responses",
            str(fixtures),
            "--inputs",
            str(inputs),
            "--max-requests",
            "1",
            "--max-cost-usd",
            "1",
            "--input-price-per-million",
            "0.042",
            "--price-date",
            "2026-09-20",
        ],
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode()
    prediction = json.loads(output.read_text())
    assert prediction["failure"] is None
    assert prediction["synthetic"] is True
    assert prediction["network_requests"] == 0
    assert prediction["confidence"] == 0.731234
    assert prediction["input_tokens"] == 321
    assert prediction["output_tokens"] == 7
    summary = json.loads(output.with_suffix(".summary.json").read_text())
    assert summary["mode"] == "recorded"
    assert summary["attempts"] == 1
    assert summary["unknown_bills"] == 0
    assert summary["cost_usd"] == "0.000013482"
    assert summary["known_input_usage_cost_usd"] == "0.000013482"
    assert summary["unknown_usage_reserved_cost_usd"] == "0E-9"
    assert summary["fixture_replay_cost_is_not_actual_spend"] is True
    assert summary["synthetic_predictions"] == 1


def test_cli_live_paths_refuse_missing_hash_and_unverified_corpus(tmp_path: Path) -> None:
    """Every live route stops before credentials or transport unless reviewed complete evidence exists."""
    source = tmp_path / "source"
    (source / "logs").mkdir(parents=True)
    (source / "logs/mindroom_one.log").write_text(
        json.dumps(
            {
                "event": "queued_message_notice_injected",
                "response_turn_id": "synthetic",
                "timestamp": "2026-09-01T00:00:00Z",
                "room_id": "room",
                "thread_id": "thread",
            },
        ),
    )
    evidence = tmp_path / "evidence"
    extract_corpus(source, evidence)
    case = json.loads((evidence / "cases.jsonl").read_text())
    inputs = tmp_path / "inputs.jsonl"
    inputs.write_text(json.dumps({"case_id": case["case_id"], "synthetic": True, "input": {}}) + "\n")
    reviewed_hash = hashlib.sha256(inputs.read_bytes()).hexdigest()
    linked_inputs = tmp_path / "linked-inputs.jsonl"
    linked_inputs.symlink_to(inputs)
    existing_output = tmp_path / "existing.jsonl"
    existing_output.write_text("keep")
    prefix = [
        sys.executable,
        "-m",
        "scripts.judgment_replay",
        "evaluate",
        "--evidence",
        str(evidence),
        "--max-requests",
        "1",
        "--max-cost-usd",
        "1",
        "--input-price-per-million",
        "0.042",
        "--price-date",
        "2026-09-20",
        "--allow-network",
    ]

    missing_manifest = subprocess.run(
        [*prefix, "--output", str(tmp_path / "missing.jsonl")],
        capture_output=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "TYPESAFE_API_KEY"},
    )
    missing_hash = subprocess.run(
        [
            *prefix,
            "--output",
            str(tmp_path / "wrong.jsonl"),
            "--inputs",
            str(inputs),
            "--reviewed-input-sha256",
            "0" * 64,
        ],
        capture_output=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "TYPESAFE_API_KEY"},
    )
    unverified = subprocess.run(
        [
            *prefix,
            "--output",
            str(tmp_path / "unverified.jsonl"),
            "--inputs",
            str(inputs),
            "--reviewed-input-sha256",
            reviewed_hash,
        ],
        capture_output=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "TYPESAFE_API_KEY"},
    )
    symlinked = subprocess.run(
        [
            *prefix,
            "--output",
            str(tmp_path / "symlinked.jsonl"),
            "--inputs",
            str(linked_inputs),
            "--reviewed-input-sha256",
            reviewed_hash,
        ],
        capture_output=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "TYPESAFE_API_KEY"},
    )
    existing = subprocess.run(
        [*prefix, "--output", str(existing_output)],
        capture_output=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "TYPESAFE_API_KEY"},
    )

    assert [result.returncode for result in (missing_manifest, missing_hash, unverified, symlinked, existing)] == [
        2,
        2,
        2,
        2,
        2,
    ]
    assert not any((tmp_path / name).exists() for name in ("missing.jsonl", "wrong.jsonl", "unverified.jsonl"))
    assert existing_output.read_text() == "keep"


def test_reviewed_bytes_are_parsed_without_reopening_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path replacement after hashing cannot change the bytes selected for live egress."""
    original = b'{"case_id":"reviewed"}\n'
    replacement = b'{"case_id":"unreviewed"}\n'
    inputs = tmp_path / "inputs.jsonl"
    inputs.write_bytes(original)
    args = argparse.Namespace(
        inputs=inputs,
        allow_network=True,
        reviewed_input_sha256=digest(original),
        recorded_responses=None,
    )

    def replace_path_after_hash(raw: bytes) -> str:
        inputs.write_bytes(replacement)
        return digest(raw)

    monkeypatch.setattr(commands, "digest", replace_path_after_hash)

    selected = commands._inputs(
        args,
        [{"case_id": "reviewed", "tier": "verified", "pending_membership_known": True}],
    )

    assert selected == [{"case_id": "reviewed"}]
    assert inputs.read_bytes() == replacement
