"""Response-owned CLI approval waits backed by native journal claims."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from mindroom.agent_cli.events import emit_cli_suspension
from mindroom.agent_cli.lifetime import current_cli_lifetime
from mindroom.approval.continuation_rules import CliExit
from mindroom.approval.failure import cancelled_failure
from mindroom.event_journal import ApprovalDecision, ApprovalFailure
from mindroom.final_delivery import (
    AwaitingApproval,
    Cancelled,
    Failed,
    FailureCause,
    Owed,
    Suppressed,
    TurnEnding,
    approval_failure,
)
from mindroom.orchestration.runtime import classify_cancel_source, current_task_is_process_shutdown
from mindroom.response_turn import ResponsePausedForApproval, apply_exact_approval_decisions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mindroom.cancellation import CancelSource
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.event_journal.approval_store import ApprovalStore
    from mindroom.llm.runs import RunRequirement
    from mindroom.message_target import MessageTarget
    from mindroom.response_turn import PausedAttempt


class _ApprovalProgress(Protocol):
    # How the response ended, once its settlement decided it.
    ending: TurnEnding | None


# What a response that ended without saying how leaves its paused CLI call to fail with.
_ENDED_BEFORE_FINAL = approval_failure(
    Failed(FailureCause.ERROR, "CLI approval response ended before final delivery."),
)


def _exit_failure(ending: TurnEnding | None, cancelled: CancelSource | None) -> ApprovalFailure:
    """Return how a response that ended without an answer fails its paused CLI call."""
    if isinstance(ending, Cancelled | Failed | Suppressed):
        return approval_failure(ending)
    if cancelled is not None:
        return cancelled_failure(cancelled)
    return _ENDED_BEFORE_FINAL


@dataclass
class CliApprovalWaits:
    """Keep waiter registration and durable claims in one owner, and report each scope's end."""

    store: ApprovalStore
    # Claims a ready continuation for the waiting response, with its reply's resume in place.
    claim: Callable[[ApprovalContinuation], Awaitable[ApprovalContinuation | None]]
    # Records a claimed continuation's next pause while the response waits in place.
    advance: Callable[[ApprovalContinuation, PausedAttempt, MessageTarget], Awaitable[object]]
    # Runs what the paused reply of a source does as a scope that paused it ends.
    exit: Callable[[str, CliExit], Awaitable[None]]
    waiters: dict[str, asyncio.Event] = field(default_factory=dict, init=False)

    def wake(self, source_event_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Wake response-local owners and return sources that have no live waiter."""
        unowned: list[str] = []
        for source_event_id in source_event_ids:
            waiter = self.waiters.get(source_event_id)
            if waiter is None:
                unowned.append(source_event_id)
            else:
                waiter.set()
        return tuple(unowned)

    async def wait(  # noqa: C901 - expiry fences both sides of awaited authorization
        self,
        paused: PausedAttempt,
        *,
        waiter: asyncio.Event,
        source: str,
        target: MessageTarget,
        publish: Callable[[PausedAttempt], Awaitable[object]],
        authorize: Callable[[], Awaitable[bool]],
    ) -> tuple[RunRequirement, ...]:
        """Publish with the native owner, then claim its exact approved generation."""
        lifetime = current_cli_lifetime()
        if lifetime is not None:
            paused = replace(paused, continuation_count=lifetime.continuation_count)
        current = await self.store.approval_continuation_for_source(source)
        if current is None:
            await publish(paused)
        elif current.state == "claimed" and current.cli_call is not None:
            await self.advance(current, paused, target)
        else:
            msg = "CLI approval source already has another owner"
            raise RuntimeError(msg)
        while True:
            # Registration precedes publication; clearing before the durable read
            # makes a decision on either side of that read observable.
            waiter.clear()
            current = await self.store.approval_continuation_for_source(source)
            if current is None or current.state not in {"waiting", "ready"}:
                # A failed publication records its cause; report that, not the lost ownership it implies.
                msg = (
                    "CLI approval lost its source ownership"
                    if current is None or current.failure is None
                    else current.failure.reason
                )
                raise RuntimeError(msg)
            lifetime = current_cli_lifetime()
            deadline = lifetime.grant_expires_at_ns if lifetime is not None else None
            if deadline is not None and time.time_ns() >= deadline:
                raise ResponsePausedForApproval(paused)
            if current.state == "ready":
                if not await authorize():
                    msg = "Current authorization no longer permits this CLI approval"
                    raise PermissionError(msg)
                if deadline is not None and time.time_ns() >= deadline:
                    raise ResponsePausedForApproval(paused)
                claimed = await self.claim(current)
                if claimed is None:
                    msg = "CLI approval lost its single execution claim"
                    raise RuntimeError(msg)
                return tuple(
                    apply_exact_approval_decisions(
                        deepcopy(paused.requirements),
                        decisions={
                            call.tool_call_id: call.decision is ApprovalDecision.APPROVED for call in claimed.calls
                        },
                        denial_reasons={call.tool_call_id: call.reason for call in claimed.calls},
                    ),
                )
            try:
                async with asyncio.timeout(None if deadline is None else max(0, (deadline - time.time_ns()) / 1e9)):
                    await waiter.wait()
            except TimeoutError:
                raise ResponsePausedForApproval(paused) from None

    @asynccontextmanager
    async def scope(
        self,
        *,
        source_event_ids: tuple[str, ...],
        progress: _ApprovalProgress,
        target: MessageTarget,
        publish: Callable[[PausedAttempt], Awaitable[object]],
        authorize: Callable[[], Awaitable[bool]],
        settle_terminal: bool,
    ) -> AsyncIterator[Callable[[PausedAttempt], Awaitable[tuple[RunRequirement, ...]]]]:
        """Own live and recovered approval waits; settle terminal state only for live responses.

        A recovered continuation's lifecycle owner settles its terminal state after
        final delivery and post-response effects, so recovery passes ``settle_terminal=False``.
        """
        waiter = asyncio.Event()
        lock = asyncio.Lock()
        source = source_event_ids[0]
        used = suspended = False
        cancelled: CancelSource | None = None

        async def pause(paused: PausedAttempt) -> tuple[RunRequirement, ...]:
            nonlocal used, suspended
            used = True
            async with lock:
                for event_id in source_event_ids:
                    self.waiters[event_id] = waiter
                try:
                    return await self.wait(
                        paused,
                        waiter=waiter,
                        source=source,
                        target=target,
                        publish=publish,
                        authorize=authorize,
                    )
                except ResponsePausedForApproval as error:
                    suspended = True
                    emit_cli_suspension(error)
                    raise

        try:
            yield pause
        except asyncio.CancelledError as error:
            cancelled = classify_cancel_source(error)
            raise
        finally:
            for event_id in source_event_ids:
                if self.waiters.get(event_id) is waiter:
                    self.waiters.pop(event_id)
            # Responses that never paused a CLI call, including every standard one, own no CLI state.
            if used:
                await self.exit(
                    source,
                    CliExit(
                        # A pause, or an answer the outbox still owes: the continuation finishes through its
                        # acknowledged FINAL, which dispatch reads.
                        suspended=suspended or isinstance(progress.ending, AwaitingApproval | Owed),
                        live=settle_terminal,
                        shutting_down=current_task_is_process_shutdown(),
                        failure=_exit_failure(progress.ending, cancelled),
                    ),
                )
