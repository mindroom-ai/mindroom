"""Contract tests for canonical terminal delivery types."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.tool_system.events import ToolTraceEntry


def test_canonical_keep_prior_visible_stream_after_cancel_keeps_physical_visibility_separate_from_final_success() -> (
    None
):
    """A kept-stream terminal state must not look like successful final delivery."""
    outcome = FinalDeliveryOutcome.keep_prior_visible_stream_after_cancel(
        last_physical_stream_event_id="$stream",
        failure_reason="terminal_cancelled_after_visible_stream",
    )

    assert outcome.state == "kept_prior_visible_stream_after_cancel"
    assert outcome.terminal_status == "cancelled"
    assert outcome.final_visible_event_id is None
    assert outcome.last_physical_stream_event_id == "$stream"
    assert outcome.has_any_visible_response is True
    assert outcome.has_final_visible_delivery is False


def test_contract_final_visible_delivery_freezes_mutable_snapshots() -> None:
    """Canonical outcomes must freeze mutable metadata snapshots."""
    trace_entry = ToolTraceEntry(type="tool_call_completed", tool_name="shell", result_preview="pwd")
    outcome = FinalDeliveryOutcome.final_visible_delivery(
        final_visible_event_id="$final",
        final_visible_body="Done",
        tool_trace=[trace_entry],
        extra_content={"nested": ["value"]},
        option_map={"1": "one"},
        options_list=[{"label": "One", "value": "one"}],
    )

    assert outcome.tool_trace == (trace_entry,)
    assert isinstance(outcome.extra_content, MappingProxyType)
    assert outcome.extra_content["nested"] == ("value",)
    assert isinstance(outcome.option_map, MappingProxyType)
    assert outcome.options_list == (MappingProxyType({"label": "One", "value": "one"}),)

    with pytest.raises(TypeError):
        outcome.extra_content["another"] = "value"
    with pytest.raises(TypeError):
        outcome.option_map["2"] = "two"


def test_canonical_suppressed_redacted_requires_prior_visible_stream() -> None:
    """Suppression cleanup states require a prior visible stream event."""
    with pytest.raises(ValueError, match="last_physical_stream_event_id"):
        FinalDeliveryOutcome.suppressed_redacted(last_physical_stream_event_id=None)


def test_canonical_illegal_state_matrix_is_rejected() -> None:
    """Direct construction must reject illegal state and payload combinations."""
    with pytest.raises(ValueError, match="cancelled_without_visible_response"):
        FinalDeliveryOutcome(
            state="cancelled_without_visible_response",
            terminal_status="cancelled",
            final_visible_event_id="$final",
            last_physical_stream_event_id=None,
            final_visible_body="unexpected",
        )


def test_contract_stream_transport_outcome_distinguishes_placeholder_only_visibility() -> None:
    """Placeholder-only terminal visibility must not count as visible body content."""
    outcome = StreamTransportOutcome(
        last_physical_stream_event_id="$placeholder",
        terminal_operation="edit",
        terminal_result="succeeded",
        terminal_status="completed",
        rendered_body="Thinking...",
        visible_body_state="placeholder_only",
    )

    assert outcome.has_any_physical_stream_event is True
    assert outcome.has_rendered_visible_body is False


def test_contract_stream_transport_outcome_rejects_body_when_visibility_is_none() -> None:
    """Transport facts cannot claim no visibility while carrying a rendered body."""
    with pytest.raises(ValueError, match="visible_body_state"):
        StreamTransportOutcome(
            last_physical_stream_event_id=None,
            terminal_operation="none",
            terminal_result="not_attempted",
            terminal_status="completed",
            rendered_body="unexpected",
            visible_body_state="none",
        )
