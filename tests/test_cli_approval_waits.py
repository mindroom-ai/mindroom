"""How a minimal-mode response's CLI approval scope reports its end to the paused reply's rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.agent_cli.cli_approval_waits import CliApprovalWaits, CliWaiters
from mindroom.final_delivery import AwaitingApproval, Cancelled, Completed, Owed, TurnProgress
from mindroom.reply_lifecycle import ApprovalFailureKind

if TYPE_CHECKING:
    from mindroom.approval.continuation_rules import CliExit
    from mindroom.event_journal.approval_store import ApprovalStore
    from mindroom.message_target import MessageTarget
    from mindroom.response_turn import PausedAttempt

pytestmark = pytest.mark.asyncio


async def _exit_after_an_approved_cli_call(progress: TurnProgress) -> CliExit:
    """Run a live scope whose CLI call paused and was approved in place, then end it with ``progress``."""
    exits: list[CliExit] = []

    async def record_exit(_source: str, trigger: CliExit) -> None:
        exits.append(trigger)

    waits = CliApprovalWaits(
        store=cast("ApprovalStore", MagicMock()),
        claim=AsyncMock(),
        advance=AsyncMock(),
        exit=record_exit,
        waiters=CliWaiters(),
    )
    waits.wait = AsyncMock(return_value=())  # type: ignore[method-assign]
    async with waits.scope(
        source_event_ids=("$source",),
        progress=progress,
        target=cast("MessageTarget", MagicMock()),
        publish=AsyncMock(),
        authorize=AsyncMock(return_value=True),
        settle_terminal=True,
    ) as pause:
        assert await pause(cast("PausedAttempt", MagicMock())) == ()
    [trigger] = exits
    return trigger


async def test_an_owed_answer_ends_the_scope_suspended() -> None:
    """A whole answer recorded but not yet acknowledged leaves its claimed continuation to dispatch.

    The continuation can only finish through an acknowledged or refused FINAL, so the scope reports the response
    suspended and dispatch finishes it once the outbox has delivered the answer.
    """
    trigger = await _exit_after_an_approved_cli_call(TurnProgress(ending=Owed()))

    assert trigger.suspended is True
    assert trigger.live is True


async def test_a_later_pause_ends_the_scope_suspended() -> None:
    """A response that paused again after its CLI call was approved leaves that pause's continuation waiting."""
    trigger = await _exit_after_an_approved_cli_call(TurnProgress(ending=AwaitingApproval()))

    assert trigger.suspended is True


async def test_a_completed_answer_ends_the_scope_unsuspended() -> None:
    """A delivered answer lets the rules finish the approval the response claimed."""
    trigger = await _exit_after_an_approved_cli_call(TurnProgress(ending=Completed()))

    assert trigger.suspended is False


async def test_a_stopped_response_fails_its_cli_call_as_stopped() -> None:
    """A Stop fails the paused call with the Stop's kind, never a reason code."""
    trigger = await _exit_after_an_approved_cli_call(TurnProgress(ending=Cancelled("user_stop")))

    assert trigger.suspended is False
    assert trigger.failure.kind is ApprovalFailureKind.USER_STOP
    assert trigger.failure.detail is None
