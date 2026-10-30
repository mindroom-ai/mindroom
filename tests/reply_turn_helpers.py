"""Build a response runner with the reply-turn owners it shares, wired as the bot wires them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_cli.cli_approval_waits import CliWaiters
from mindroom.approval.continuation_runner import ApprovalContinuationRunner
from mindroom.reply_turn.inbox import InboxResponses
from mindroom.reply_turn.locked_turn import LockedTurns
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.response_runner import ResponseRunner

if TYPE_CHECKING:
    from mindroom.reply_turn.locked_turn import ReplyTurnDeps


def runner_for(deps: ReplyTurnDeps) -> ResponseRunner:
    """Return a response runner over ``deps``, with its own locked turns, approval runner, inbox, and locks."""
    cli_waiters = CliWaiters()
    lifecycle_coordinator = ResponseLifecycleCoordinator()
    locked = LockedTurns(deps)
    return ResponseRunner(
        locked=locked,
        approvals=ApprovalContinuationRunner(
            locked=locked,
            lifecycle_coordinator=lifecycle_coordinator,
            cli_waiters=cli_waiters,
        ),
        inbox=InboxResponses(
            logger=deps.logger,
            replies=deps.replies,
            retry_approval_sources=deps.retry_approval_sources,
            runtime=deps.runtime,
        ),
        lifecycle_coordinator=lifecycle_coordinator,
        cli_waiters=cli_waiters,
    )
