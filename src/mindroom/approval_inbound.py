"""Inbound Matrix approval handling — parse responses and resolve approvals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.authorization import is_authorized_sender
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.visible_body import strip_matrix_rich_reply_fallback
from mindroom.tool_approval import (
    MatrixApprovalAction,
    handle_matrix_approval_action,
    is_process_active_approval_card,
)

if TYPE_CHECKING:
    import nio
    import structlog

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.runtime_protocols import OrchestratorRuntime

__all__ = [
    "ApprovalResponsePayload",
    "handle_tool_approval_action",
    "maybe_handle_tool_approval_reply",
    "parse_approval_response_event",
]


@dataclass(frozen=True, slots=True)
class ApprovalResponsePayload:
    """Decoded fields from one custom Matrix approval response event."""

    card_event_id: str | None
    approval_id: str | None
    status: Literal["approved", "denied"] | None
    reason: str | None


def parse_approval_response_event(event: nio.UnknownEvent) -> ApprovalResponsePayload:
    """Parse one custom approval response event into a structured payload."""
    content = event.source.get("content", {})
    if not isinstance(content, dict):
        return ApprovalResponsePayload(card_event_id=None, approval_id=None, status=None, reason=None)

    card_event_id = EventInfo.from_event(event.source).reply_to_event_id
    raw_approval_id = content.get("approval_id")
    approval_id = raw_approval_id if isinstance(raw_approval_id, str) and raw_approval_id else None

    raw_status = content.get("status")
    status: Literal["approved", "denied"] | None = None
    if raw_status in {"approved", "denied"}:
        status = raw_status

    raw_reason = content.get("reason")
    if not isinstance(raw_reason, str) or not raw_reason.strip():
        raw_reason = content.get("denial_reason")
    reason = raw_reason.strip() if isinstance(raw_reason, str) and raw_reason.strip() else None
    return ApprovalResponsePayload(
        card_event_id=card_event_id,
        approval_id=approval_id,
        status=status,
        reason=reason,
    )


async def handle_tool_approval_action(
    *,
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    orchestrator: OrchestratorRuntime | None,
    logger: structlog.stdlib.BoundLogger,
    approval_event_id: str | None,
    status: Literal["approved", "denied"],
    reason: str | None,
    approval_id: str | None = None,
) -> bool:
    """Resolve one approval action only when the sender still has access."""
    if approval_event_id is None and approval_id is None:
        return False
    if not is_authorized_sender(
        sender_id,
        config,
        room.room_id,
        runtime_paths,
        room_alias=room.canonical_alias,
    ):
        logger.debug("ignoring_tool_approval_action_from_unauthorized_sender", user_id=sender_id)
        return False
    result = await handle_matrix_approval_action(
        MatrixApprovalAction(
            room_id=room.room_id,
            sender_id=sender_id,
            card_event_id=approval_event_id,
            approval_id=approval_id,
            status=status,
            reason=reason,
        ),
    )
    notice_event_id = approval_event_id or result.card_event_id
    if notice_event_id is not None and result.error_reason is not None and orchestrator is not None:
        await orchestrator.send_approval_notice(
            room_id=room.room_id,
            approval_event_id=notice_event_id,
            thread_id=result.thread_id,
            reason=result.error_reason,
        )
    return result.consumed


async def maybe_handle_tool_approval_reply(
    *,
    room: nio.MatrixRoom,
    event: nio.RoomMessageText,
    config: Config,
    runtime_paths: RuntimePaths,
    orchestrator: OrchestratorRuntime | None,
    logger: structlog.stdlib.BoundLogger,
) -> bool:
    """Consume reply-to-approval messages as denial actions."""
    reply_to_event_id = EventInfo.from_event(event.source).reply_to_event_id
    if reply_to_event_id is None:
        return False
    if not is_process_active_approval_card(reply_to_event_id):
        return False
    return await handle_tool_approval_action(
        room=room,
        sender_id=event.sender,
        config=config,
        runtime_paths=runtime_paths,
        orchestrator=orchestrator,
        logger=logger,
        approval_event_id=reply_to_event_id,
        status="denied",
        reason=strip_matrix_rich_reply_fallback(event.body),
    )
