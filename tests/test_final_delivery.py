"""Contract tests for canonical terminal delivery types."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from mindroom.delivery_gateway import DeliveryResult
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.response_runner import _coerce_final_delivery_outcome
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
    assert outcome.visible_response_event_id == "$stream"


def test_canonical_cancelled_with_visible_response_preserves_visible_event_id_without_claiming_success() -> None:
    """Cancelled terminal states can preserve a visible event without claiming a cancellation note landed."""
    outcome = FinalDeliveryOutcome.cancelled_with_visible_response(
        final_visible_event_id="$existing",
        failure_reason="cancelled_by_user",
    )

    assert outcome.state == "cancelled_with_visible_response"
    assert outcome.terminal_status == "cancelled"
    assert outcome.final_visible_event_id == "$existing"
    assert outcome.final_visible_body is None
    assert outcome.visible_response_event_id == "$existing"


def test_response_identity_event_id_excludes_cancelled_visible_streams_but_preserves_terminal_failures() -> None:
    """Visible cancellation and delivered-response identity are separate downstream facts."""
    cancelled = FinalDeliveryOutcome.keep_prior_visible_stream_after_cancel(
        last_physical_stream_event_id="$stream",
        failure_reason="cancelled_by_user",
    )
    error = FinalDeliveryOutcome.keep_prior_visible_stream_after_error(
        last_physical_stream_event_id="$stream",
        failure_reason="boom",
    )

    assert cancelled.visible_response_event_id == "$stream"
    assert cancelled.response_identity_event_id is None
    assert error.visible_response_event_id == "$stream"
    assert error.response_identity_event_id == "$stream"


def test_coerce_final_delivery_outcome_does_not_promote_failed_visible_delivery_into_success() -> None:
    """Legacy fallback coercion must preserve terminal failure when a visible stream errored."""
    outcome = _coerce_final_delivery_outcome(
        DeliveryResult(
            event_id="$visible",
            response_text="partial",
            delivery_kind="sent",
            failure_reason="boom",
        ),
    )

    assert outcome.state != "final_visible_delivery"
    assert outcome.terminal_status == "error"
    assert outcome.visible_response_event_id == "$visible"


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
