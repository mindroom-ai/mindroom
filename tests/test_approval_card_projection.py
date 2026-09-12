"""Terminal approval wire contracts at the journal projection boundary."""

from __future__ import annotations

import copy
import json

from mindroom.event_journal.approval_card_state import (
    ApprovalDecisionMetadata,
    stored_resolution,
    terminal_content,
)


def _original() -> dict[str, object]:
    return {
        "msgtype": "io.mindroom.tool_approval",
        "body": "Approval required: write_file",
        "approval_id": "request",
        "tool_name": "write_file",
        "arguments": {"path": "notes.txt", "content": "preview"},
        "full_arguments": {"path": "notes.txt", "content": "the complete reviewed text"},
        "full_arguments_url": "mxc://example.org/arguments",
        "status": "pending",
        "thread_id": "$thread",
        "approval_scope": {"id": "scope", "tool_name": "write_file"},
        "continuation_id": "continuation",
        "continuation_generation": 2,
        "tool_call_id": "call",
        "auto_approve_options": [300, 600, 1800],
    }


def test_once_edit_preserves_original_identity_and_evidence_without_pending_review_data() -> None:
    """Terminal edits must keep references to the original exact arguments."""
    original = _original()
    frozen = copy.deepcopy(original)
    result = terminal_content(
        original,
        status="approved",
        reason="Reviewed the target file.",
        metadata=ApprovalDecisionMetadata(resolved_by="@alice:example.org", resolved_at="2026-09-12T12:00:00Z"),
    )
    assert result == {
        "msgtype": "io.mindroom.tool_approval",
        "body": "Approved: write_file",
        "approval_id": "request",
        "tool_name": "write_file",
        "arguments": {"path": "notes.txt", "content": "preview"},
        "full_arguments_url": "mxc://example.org/arguments",
        "status": "approved",
        "approvable": False,
        "thread_id": "$thread",
        "approval_scope": {"id": "scope", "tool_name": "write_file"},
        "continuation_id": "continuation",
        "continuation_generation": 2,
        "tool_call_id": "call",
        "resolved_by": "@alice:example.org",
        "resolved_at": "2026-09-12T12:00:00Z",
        "resolution_reason": "Reviewed the target file.",
        "approval_provenance": {"kind": "once"},
    }
    assert original == frozen


def test_automatic_original_keeps_full_arguments_and_immutable_grant_provenance() -> None:
    """Automatic receipts are original evidence, and cannot expose grant controls."""
    provenance = {
        "kind": "timed_grant",
        "grant_id": "grant",
        "granted_by": "@alice:example.org",
        "granted_at": "2026-09-12T12:00:00Z",
        "expires_at": "2026-09-12T12:10:00Z",
    }
    result = terminal_content(
        _original(),
        status="approved",
        reason=None,
        metadata=ApprovalDecisionMetadata(
            resolved_by="@alice:example.org",
            resolved_at="2026-09-12T12:01:00Z",
            provenance=provenance,
            auto_approval={"grant_id": "grant", "expires_at": "2026-09-12T12:10:00Z", "revoked_at": None},
        ),
        publication="receipt",
    )
    assert result["body"] == "Auto-approved: write_file"
    assert result["full_arguments"] == {"path": "notes.txt", "content": "the complete reviewed text"}
    assert result["approval_provenance"] == provenance
    assert "auto_approval" not in result
    assert "auto_approve_options" not in result
    assert result["approvable"] is False


def test_actual_journal_outcome_overrides_offered_actor_and_grant() -> None:
    """A deadline winner must never publish the rejected grant decision."""
    result = stored_resolution(
        {"payload_json": json.dumps(_original())},
        metadata=ApprovalDecisionMetadata(
            resolved_by="@alice:example.org",
            resolved_at="2026-09-12T12:00:00Z",
            provenance={"kind": "timed_grant", "grant_id": "rejected"},
            auto_approval={"grant_id": "rejected"},
        ),
        requested_status="approved",
        decision="expired",
        reason="Tool approval request timed out.",
        description="approval payload",
    )
    assert result["status"] == "expired"
    assert result["body"] == "Expired: write_file"
    assert result["resolved_by"] is None
    assert result["resolution_reason"] == "Tool approval request timed out."
    assert result["approval_scope"] == {"id": "scope", "tool_name": "write_file"}
    assert "approval_provenance" not in result
    assert "auto_approval" not in result
