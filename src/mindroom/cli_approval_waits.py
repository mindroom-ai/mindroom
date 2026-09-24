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
from mindroom.event_journal import ApprovalDecision
from mindroom.orchestration.runtime import (
    cancel_failure_reason,
    classify_cancel_source,
    current_task_is_process_shutdown,
)
from mindroom.response_turn import ResponsePausedForApproval, apply_exact_approval_decisions
from mindroom.streaming import PROGRESS_PLACEHOLDER

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from agno.run.requirement import RunRequirement

    from mindroom.approval_response import ApprovalResponseCoordinator
    from mindroom.event_journal import PrincipalStore
    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.message_target import MessageTarget
    from mindroom.response_turn import PausedAttempt


class _ApprovalProgress(Protocol):
    failure_reason: str | None
    delivery_outcome: FinalDeliveryOutcome | None

    def note_task_cancelled(self, failure_reason: str) -> None: ...


@dataclass
class CliApprovalWaits:
    """Keep waiter registration, durable claims, and terminal cleanup in one owner."""

    store: PrincipalStore
    responses: ApprovalResponseCoordinator
    runtime_generation: str
    retry_sources: Callable[[str, tuple[str, ...]], None]
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
        show_tool_calls: bool,
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
            await self.responses.advance_pause(
                current,
                paused,
                target=target,
                pending_text=PROGRESS_PLACEHOLDER,
            )
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
                    current.failure_reason if current is not None else None
                ) or "CLI approval lost its source ownership"
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
                claimed = await self.store.claim_approval_continuation(
                    current.approval_id,
                    runtime_generation=self.runtime_generation,
                    legacy_show_tool_calls=show_tool_calls,
                )
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
        show_tool_calls: bool,
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
                        show_tool_calls=show_tool_calls,
                    )
                except ResponsePausedForApproval as error:
                    suspended = True
                    emit_cli_suspension(error)
                    raise

        try:
            yield pause
        except asyncio.CancelledError as error:
            progress.note_task_cancelled(cancel_failure_reason(classify_cancel_source(error)))
            raise
        finally:
            for event_id in source_event_ids:
                if self.waiters.get(event_id) is waiter:
                    self.waiters.pop(event_id)
            # Responses that never paused a CLI call, including every standard one, own no CLI state.
            if used:
                await self._settle(
                    source,
                    suspended=suspended
                    or (
                        progress.delivery_outcome is not None
                        and progress.delivery_outcome.terminal_status == "suspended"
                    ),
                    progress=progress,
                    settle_terminal=settle_terminal,
                )

    async def _settle(
        self,
        source: str,
        *,
        suspended: bool,
        progress: _ApprovalProgress,
        settle_terminal: bool,
    ) -> None:
        current = await self.store.approval_continuation_for_source(source)
        if suspended and current is not None and current.state == "ready":
            self.retry_sources(current.room_id, tuple(current.source_event_ids))
        if (
            settle_terminal
            and not suspended
            and current is not None
            and current.cli_call is not None
            and (current.state != "claimed" or current.runtime_generation == self.runtime_generation)
        ):
            if await self.responses.final_delivery(current) is not None:
                await self.store.finish_approval_continuation(current.approval_id)
            elif not current_task_is_process_shutdown():
                await self.responses.request_failure(
                    current,
                    progress.failure_reason or "CLI approval response ended before final delivery.",
                )
