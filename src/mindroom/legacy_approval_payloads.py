"""Interpret approval fields that predate the current continuation payload."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.event_journal.approval_continuations import ApprovalContinuation


def resolve_legacy_visibility(*, show_tool_calls: bool, is_frozen: bool, current_policy: bool | None) -> bool:
    """Freeze an older continuation against the policy available at its first claim."""
    if is_frozen:
        return show_tool_calls
    if current_policy is None:
        msg = "Legacy approval continuation visibility must be resolved before claim"
        raise RuntimeError(msg)
    return current_policy


def restore_legacy_approval_origin(continuation: ApprovalContinuation) -> TurnOrigin:
    """Rebuild the origin snapshot absent from continuations written by older runtimes."""
    if continuation.origin is not None:
        return continuation.origin
    transport_sender_id = continuation.transport_sender_id or continuation.requester_id
    relayed = transport_sender_id != continuation.requester_id
    return TurnOrigin(
        transport_sender_id=transport_sender_id,
        requester_id=continuation.requester_id,
        sender_entity_name=ROUTER_AGENT_NAME if relayed else None,
        requester_entity_name=None,
        sender_kind=SenderKind.MANAGED_ENTITY if relayed else SenderKind.USER,
        requester_kind=SenderKind.USER,
        intent=TurnIntent.ROUTER_HANDOFF if relayed else TurnIntent.USER_MESSAGE,
        source_kind=continuation.source_kind,
        trust=TurnTrust.TRUSTED_INTERNAL if relayed else TurnTrust.EXTERNAL,
    )


def legacy_approval_card_id(content: Mapping[str, object]) -> str | None:
    """Return a usable current ID or the old tool-call fallback."""
    approval_id = content.get("approval_id")
    if isinstance(approval_id, str) and approval_id:
        return approval_id
    tool_call_id = content.get("tool_call_id")
    return tool_call_id if isinstance(tool_call_id, str) and tool_call_id else None
