"""How a minimal-mode response's CLI approval scope reports its end to the paused reply's rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.agent_cli.cli_approval_waits import CliApprovalWaits
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.response_runner import DeliveryProgress

if TYPE_CHECKING:
    from mindroom.approval.continuation_rules import CliExit
    from mindroom.event_journal.approval_store import ApprovalStore
    from mindroom.message_target import MessageTarget
    from mindroom.response_turn import PausedAttempt

pytestmark = pytest.mark.asyncio


async def _exit_after_an_approved_cli_call(progress: DeliveryProgress) -> CliExit:
    """Run a live scope whose CLI call paused and was approved in place, then end it with ``progress``."""
    exits: list[CliExit] = []

    async def record_exit(_source: str, trigger: CliExit) -> None:
        exits.append(trigger)

    waits = CliApprovalWaits(
        store=cast("ApprovalStore", MagicMock()),
        claim=AsyncMock(),
        advance=AsyncMock(),
        exit=record_exit,
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


@pytest.mark.xfail(strict=True, reason="bug: an owed answer reads as an approval suspension")
async def test_an_owed_answer_does_not_read_as_an_approval_suspension() -> None:
    """A whole answer recorded but not yet acknowledged ends the scope unsuspended, so its approval finishes.

    Read as suspended, the rules wait on a continuation this response already claimed, which then holds the
    conversation until a restart.
    """
    progress = DeliveryProgress()
    # The outcome the gateway reports for an answer whose FINAL is recorded but not yet in the room.
    progress.settle(FinalDeliveryOutcome(terminal_status="suspended", event_id=None, failure_reason="delivery_failed"))

    trigger = await _exit_after_an_approved_cli_call(progress)

    assert trigger.suspended is False
    assert trigger.live is True
