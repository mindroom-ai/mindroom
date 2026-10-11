"""Participation requests preserve bounded context and refuse unsafe input."""

from __future__ import annotations

import hashlib
import json

import pytest

from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentMessage, build_judgment_request
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
