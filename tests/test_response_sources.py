"""Focused tests for explicit response source ownership."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.response import ToolExecution

from mindroom.constants import MATRIX_SOURCE_EVENT_IDS_METADATA_KEY
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.response_runner import _DeliveryProgress
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import PausedAttempt
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.turn_record import TurnRecord
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_response_runner_focused import _admit_approval_source, _ordered_pause

if TYPE_CHECKING:
    from pathlib import Path


def test_response_source_values_are_validated_and_immutable() -> None:
    """Malformed or mutable ownership identities cannot cross the response seam."""
    response_sources = ResponseSources(
        pending_event_ids=("$revision",),
        logical_source_event_ids=("$source",),
        discovery_event_ids=("$alias",),
        edit_receipt_order=7,
    )
    with pytest.raises(FrozenInstanceError):
        response_sources.edit_receipt_order = 8
    with pytest.raises(ValueError, match="pending_event_ids must not be empty"):
        ResponseSources(pending_event_ids=(), logical_source_event_ids=("$source",))
    with pytest.raises(ValueError, match="logical_source_event_ids must not be empty"):
        ResponseSources(pending_event_ids=("$revision",), logical_source_event_ids=())
    with pytest.raises(ValueError, match="duplicate"):
        ResponseSources(
            pending_event_ids=("$revision", "$revision"),
            logical_source_event_ids=("$source",),
        )
    with pytest.raises(ValueError, match="positive integer"):
        ResponseSources(
            pending_event_ids=("$revision",),
            logical_source_event_ids=("$source",),
            edit_receipt_order=0,
        )


@pytest.mark.asyncio
async def test_explicit_edit_sources_ignore_unrelated_model_metadata(tmp_path: Path) -> None:
    """Approval suspension persists only the pending edit selected by the producer."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    principal = runner.deps.approval_store
    await _admit_approval_source(principal, event_id="$edit")
    await _admit_approval_source(principal, event_id="$settled")
    await principal.settle_many(("$settled",))
    request = replace(
        _plain_request(_target(thread_id="$thread"), source_event_id="$edit"),
        sources=ResponseSources(
            pending_event_ids=("$edit",),
            logical_source_event_ids=("$source",),
            edit_receipt_order=7,
        ),
        prepared_edit_record=TurnRecord.create(
            ["$source"],
            source_event_revisions={"$source": (20, "$edit")},
            latest_edit_receipt_order=7,
        ),
        matrix_run_metadata={MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$settled"]},
    )
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-paused",
            tools=(ToolExecution(tool_call_id="call-1", tool_name="read_document", requires_confirmation=True),),
        ),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id=request.room_id,
        thread_id=request.thread_id,
        resolved_thread_id=request.response_envelope.target.resolved_thread_id,
        session_id=paused.session_id,
    )

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$waiting")),
        patch("mindroom.response_runner.uuid4", return_value=MagicMock(hex="approval-explicit-edit")),
        patch("mindroom.approval_response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval_response.evaluate_tool_approval", new=AsyncMock(return_value=(True, 60.0))),
    ):
        await runner._suspend_for_approval(
            paused,
            request=request,
            target=request.response_envelope.target,
            progress=_DeliveryProgress(),
            execution_identity=identity,
            entity_kind="agent",
            history_scope=runner.deps.state_writer.history_scope(),
            show_tool_calls=True,
        )

    continuation = await principal.approval_continuation("approval-explicit-edit")
    assert continuation is not None
    assert continuation.source_event_ids == ("$edit",)
    assert continuation.sources == request.sources
    resumed = runner._approval_response_request(
        continuation,
        target=request.response_envelope.target,
    )
    assert resumed.sources == request.sources
    assert await principal.approval_continuation_for_source("$settled") is None
    assert await principal.is_pending("$edit")
    assert not await principal.is_pending("$settled")
