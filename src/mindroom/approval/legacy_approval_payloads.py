"""Interpret approval card fields that predate the current card payload."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# LEGACY_COMPAT: Approval cards identifying calls only through tool_call_id.
# Legacy format: Approval cards with only the defensive tool_call_id alias usable as identity.
# Last legacy release: Unversioned external input; replacement: v2026.5.22 introduced both native ID fields.
# Handling: Prefer a valid approval_id and otherwise accept a valid tool_call_id from sparse external cards.
# Coverage: tests/test_tool_approval.py::test_pending_approval_from_sparse_card_uses_tool_call_id_as_approval_id.


def legacy_approval_card_id(content: Mapping[str, object]) -> str | None:
    """Return a usable current ID or the defensive tool-call alias."""
    approval_id = content.get("approval_id")
    if isinstance(approval_id, str) and approval_id:
        return approval_id
    tool_call_id = content.get("tool_call_id")
    return tool_call_id if isinstance(tool_call_id, str) and tool_call_id else None
