"""The queued-message judgment state stays complete, minimal, and deterministic."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import mindroom.judgment.state as state_module
from mindroom.judgment.state import (
    MAX_REQUEST_BYTES,
    PINNED_MODEL,
    QUEUED_MESSAGE_QUESTION,
    JudgmentMessage,
    QueuedJudgmentInput,
    build_queued_judgment_request,
)

if TYPE_CHECKING:
    import pytest


def test_queued_request_uses_fixed_choice_contract_and_sender_aliases() -> None:
    """A wire-shape regression must not expose identities or change the evaluated rubric."""
    request = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(sender="@alice:example.org", text="Install the package."),
            queued=(
                JudgmentMessage(sender="@alice:example.org", text="Thanks."),
                JudgmentMessage(sender="@bob:example.org", text="Separate question."),
            ),
            tool_names=("read_file", "run_shell"),
            context=(JudgmentMessage(sender="@bob:example.org", text="Earlier context."),),
        ),
    )

    assert request.complete is True
    assert request.incomplete_reason is None
    assert request.body is not None
    assert len(request.body) == request.state_bytes
    assert json.loads(request.body) == {
        "model": PINNED_MODEL,
        "questions": {
            QUEUED_MESSAGE_QUESTION.question_id: {
                "criteria": dict(QUEUED_MESSAGE_QUESTION.criteria),
                "instructions": QUEUED_MESSAGE_QUESTION.instructions,
                "type": "choice",
            },
        },
        "state": {
            "active_input": {"sender": "human_1", "text": "Install the package."},
            "completed_tool_names": ["read_file", "run_shell"],
            "context": [{"sender": "human_2", "text": "Earlier context."}],
            "queued_messages": [
                {"sender": "human_1", "text": "Thanks."},
                {"sender": "human_2", "text": "Separate question."},
            ],
        },
    }
    assert b"alice" not in request.body
    assert b"bob" not in request.body


def test_request_hashes_are_stable_and_cover_state_and_question() -> None:
    """Unstable serialization or question changes must not reuse persisted fixture identities."""
    value = QueuedJudgmentInput(
        active=JudgmentMessage(sender="alice", text="Do the work."),
        queued=(JudgmentMessage(sender="alice", text="Keep going."),),
    )

    first = build_queued_judgment_request(value)
    second = build_queued_judgment_request(value)
    changed = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=value.active,
            queued=(JudgmentMessage(sender="alice", text="Stop."),),
        ),
    )

    assert first == second
    assert len(first.request_hash) == 64
    assert len(first.state_hash) == 64
    assert first.request_hash != changed.request_hash
    assert first.state_hash != changed.state_hash


def test_optional_context_is_dropped_oldest_first_to_fit_the_request_ceiling() -> None:
    """An oversized optional history must not truncate the active input or pending queue."""
    context = tuple(JudgmentMessage(sender=f"person-{index}", text=f"{index}:" + "x" * 1_800) for index in range(12))
    request = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(sender="alice", text="Complete active input."),
            queued=(JudgmentMessage(sender="bob", text="Complete queued input."),),
            context=context,
        ),
    )

    assert request.complete is True
    assert request.body is not None
    assert len(request.body) <= MAX_REQUEST_BYTES
    state = json.loads(request.body)["state"]
    assert state["active_input"]["text"] == "Complete active input."
    assert state["queued_messages"] == [{"sender": "human_2", "text": "Complete queued input."}]
    retained = [int(item["text"].split(":", maxsplit=1)[0]) for item in state["context"]]
    assert retained
    assert retained == list(range(retained[0], 12))
    assert retained[0] > 0


def test_essential_text_that_cannot_fit_returns_incomplete_without_a_body() -> None:
    """The builder must never send a truncated active task and claim it is complete."""
    request = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(sender="alice", text="x" * MAX_REQUEST_BYTES),
            queued=(JudgmentMessage(sender="alice", text="Thanks."),),
        ),
    )

    assert request.complete is False
    assert request.incomplete_reason == "essential_input_too_large"
    assert request.body is None


def test_oversized_secret_is_rejected_before_redaction() -> None:
    """Ingress bounds must win before a huge credential-shaped value reaches redaction or hashing."""
    request = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(sender="alice", text="Do it."),
            queued=(JudgmentMessage(sender="alice", text="api_key=" + "x" * MAX_REQUEST_BYTES),),
        ),
    )

    assert request.complete is False
    assert request.incomplete_reason == "essential_input_too_large"
    assert request.body is None


def test_redacted_essential_text_returns_incomplete_without_a_body() -> None:
    """A removed credential may carry the intent, so redaction must not authorize finish."""
    request = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(sender="alice", text="Install the integration."),
            queued=(JudgmentMessage(sender="alice", text="api_key=sk-secret-value"),),
        ),
    )

    assert request.complete is False
    assert request.incomplete_reason == "essential_input_redacted"
    assert request.body is None


def test_missing_active_or_queued_input_is_incomplete() -> None:
    """A missing side of the comparison must not become a finish-capable request."""
    missing_active = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(sender="alice", text="  "),
            queued=(JudgmentMessage(sender="alice", text="Thanks."),),
        ),
    )
    missing_queue = build_queued_judgment_request(
        QueuedJudgmentInput(active=JudgmentMessage(sender="alice", text="Do it."), queued=()),
    )

    assert (missing_active.complete, missing_active.incomplete_reason, missing_active.body) == (
        False,
        "missing_essential_input",
        None,
    )
    assert (missing_queue.complete, missing_queue.incomplete_reason, missing_queue.body) == (
        False,
        "missing_essential_input",
        None,
    )


def test_tool_names_are_complete_or_the_request_is_incomplete() -> None:
    """The builder must not silently drop progress metadata to squeeze under the ceiling."""
    request = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(sender="alice", text="Do it."),
            queued=(JudgmentMessage(sender="alice", text="Thanks."),),
            tool_names=tuple(f"tool_{index}_" + "x" * 500 for index in range(40)),
        ),
    )

    assert request.complete is False
    assert request.incomplete_reason == "essential_input_too_large"
    assert request.body is None


def test_aggregate_queue_is_bounded_before_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Individually small messages cannot cause unbounded preflight work."""

    def refuse(_text: str) -> str:
        msg = "aggregate oversize must be rejected before redaction"
        raise AssertionError(msg)

    monkeypatch.setattr(state_module, "redact_sensitive_text", refuse)
    value = QueuedJudgmentInput(
        active=JudgmentMessage("human", "Task"),
        queued=tuple(JudgmentMessage("human", "x" * 15_900) for _ in range(100)),
    )
    assert not build_queued_judgment_request(value).complete
