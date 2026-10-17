"""Participation requests preserve bounded context and refuse unsafe input."""

from __future__ import annotations

import hashlib
import json

import pytest

from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentMessage, build_judgment_request, fit_judgment_request
from mindroom.participation import PARTICIPATION_QUESTION


def test_participation_request_has_stable_hashes_and_complete_context() -> None:
    """Identical inputs produce identical wire bytes; conversation changes invalidate them."""
    messages = (JudgmentMessage("user", "How does it work?"), JudgmentMessage("assistant", "Like this."))
    request = build_judgment_request(PARTICIPATION_QUESTION, messages, instructions="Help when useful.")
    assert request == build_judgment_request(PARTICIPATION_QUESTION, messages, instructions="Help when useful.")
    assert request.complete
    assert request.body is not None
    payload = json.loads(request.body)
    assert payload["state"]["conversation"] == [
        {"role": "user", "text": "How does it work?"},
        {"role": "assistant", "text": "Like this."},
    ]
    assert payload["guidance"] == "Help when useful."
    assert request.request_hash == hashlib.sha256(request.body).hexdigest()
    assert request.state_bytes == len(request.body) <= MAX_REQUEST_BYTES
    changed = build_judgment_request(
        PARTICIPATION_QUESTION,
        (JudgmentMessage("user", "New question"),),
        instructions="Help when useful.",
    )
    assert changed.request_hash != request.request_hash


@pytest.mark.parametrize(
    ("messages", "instructions"),
    [
        ((), ""),
        ((JudgmentMessage("assistant", "No user input"),), ""),
        ((JudgmentMessage("user", " "),), ""),
        ((JudgmentMessage("user", "Question"), JudgmentMessage("system", "Private")), ""),
        ((JudgmentMessage("user", "Question"),) * 9, ""),
        ((JudgmentMessage("user", "x" * MAX_REQUEST_BYTES),), ""),
        ((JudgmentMessage("user", "é" * (MAX_REQUEST_BYTES // 2)),), ""),
        ((JudgmentMessage("user", "Question"),), "x" * MAX_REQUEST_BYTES),
        ((JudgmentMessage("user", "token=sk-secret"),), ""),
        ((JudgmentMessage("user", "Question"),), "token=sk-secret"),
        ((JudgmentMessage("user", "\ud800"),), ""),
        ((JudgmentMessage("user", "Question"),), "\ud800"),
    ],
)
def test_incomplete_participation_input_never_produces_wire_bytes(
    messages: tuple[JudgmentMessage, ...],
    instructions: str,
) -> None:
    """Unsupported, oversized, secret-bearing, or unencodable input must fall back locally."""
    request = build_judgment_request(PARTICIPATION_QUESTION, messages, instructions=instructions)
    assert not request.complete
    assert request.body is None
    assert request.incomplete_reason


def test_fit_judgment_request_drops_oldest() -> None:
    """Long threads reach the judge as their newest messages that fit, never as a refusal."""
    history = [JudgmentMessage("user", f"message {index} " + "x" * 1_900) for index in range(20)]
    request = fit_judgment_request(
        PARTICIPATION_QUESTION,
        history,
        JudgmentMessage("user", "Current request"),
        instructions="",
        max_context_messages=63,
    )
    assert request is not None
    assert request.complete
    assert request.body is not None
    conversation = json.loads(request.body)["state"]["conversation"]
    assert conversation[-1] == {"role": "user", "text": "Current request"}
    kept = [message["text"].split(" x")[0].split("x")[0].strip() for message in conversation[:-1]]
    assert 0 < len(kept) < 20
    assert kept == [f"message {index}" for index in range(20 - len(kept), 20)]
