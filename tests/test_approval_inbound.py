"""A client's approval response, sent as a custom event, reaches the approval API only with a sender and a decision."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.approval.inbound import handle_tool_approval_response_event
from mindroom.approval.tool_approval import ApprovalActionResult
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.logging_config import get_logger
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.runtime_fakes import ready_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.approval.tool_approval import MatrixApprovalAction


def _response(content: object, **source: object) -> nio.UnknownEvent:
    event = nio.UnknownEvent.from_dict(
        {
            "type": "io.mindroom.tool_approval_response",
            "sender": "@user:localhost",
            "event_id": "$response",
            "origin_server_ts": 1,
            "content": {},
        },
    )
    assert isinstance(event, nio.UnknownEvent)
    event.source = {**event.source, "content": content, **source}
    return event


async def _handle(tmp_path: Path, event: nio.UnknownEvent) -> MatrixApprovalAction | None:
    """Handle one response in a ready runtime; return the action the approval API received, if any."""
    paths = test_runtime_paths(tmp_path)
    runtime = BotRuntimeState(orchestrator=ready_runtime(bind_runtime_paths(Config(), paths), paths))
    with patch(
        "mindroom.approval.inbound.handle_matrix_approval_action",
        new=AsyncMock(return_value=ApprovalActionResult(consumed=True, card_event_id="$approval")),
    ) as handle_matrix_approval_action:
        await handle_tool_approval_response_event(
            nio.MatrixRoom("!test:localhost", "@mindroom_code:localhost"),
            event,
            runtime=runtime,
            runtime_paths=paths,
            logger=get_logger("tests.approval_inbound"),
        )
    return handle_matrix_approval_action.await_args.args[0] if handle_matrix_approval_action.await_args else None


_CARD = {"m.relates_to": {"m.in_reply_to": {"event_id": "$approval"}}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _response({"status": "approved", **_CARD}, sender=""),
        _response({"status": "approved", **_CARD}, sender=7),
        _response({"reason": "neither a status nor an action", **_CARD}),
        _response({"status": "maybe", **_CARD}),
        _response("not an object"),
        _response({"approval_id": "approval-1", "status": "approved"}),
    ],
    ids=["empty sender", "non-string sender", "no decision", "unknown status", "malformed content", "no card"],
)
async def test_approval_responses_without_a_sender_a_decision_or_a_card_are_ignored(
    tmp_path: Path,
    event: nio.UnknownEvent,
) -> None:
    """A response must name its sender, reply to a card, and carry a status or an action."""
    assert await _handle(tmp_path, event) is None


@pytest.mark.asyncio
async def test_an_approval_response_without_a_sender_field_is_ignored(tmp_path: Path) -> None:
    """A response whose envelope lacks a sender reaches nothing."""
    event = _response({"status": "approved", **_CARD})
    del event.source["sender"]

    assert await _handle(tmp_path, event) is None


@pytest.mark.asyncio
async def test_an_approval_response_resolves_its_card_with_its_decision(tmp_path: Path) -> None:
    """A response replying to a card hands its room, sender, card, status, and reason to the approval API."""
    action = await _handle(tmp_path, _response({"status": "denied", "reason": " not now ", **_CARD}))

    assert action is not None
    assert (action.room_id, action.sender_id, action.card_event_id) == (
        "!test:localhost",
        "@user:localhost",
        "$approval",
    )
    assert (action.status, action.reason, action.action) == ("denied", "not now", None)


@pytest.mark.asyncio
async def test_an_approval_response_revoking_an_auto_approval_names_its_grant(tmp_path: Path) -> None:
    """A revoke action reaches the approval API with the grant it revokes and no decision of its own."""
    action = await _handle(tmp_path, _response({"action": "revoke_auto_approval", "grant_id": "grant-1", **_CARD}))

    assert action is not None
    assert (action.action, action.grant_id, action.status) == ("revoke_auto_approval", "grant-1", None)
