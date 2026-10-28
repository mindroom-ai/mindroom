"""Own detached inbox responses until they finish, or until a shutdown drain proves their recovery."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.orchestration.runtime import request_task_cancel
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_shutdown_diagnostics import (
    ResponseShutdownPhase,
    ResponseShutdownPhaseTrace,
    context_with_response_shutdown_trace,
)
from mindroom.runtime_shutdown import GENERIC_SHUTDOWN, ORDERLY_SHUTDOWN, ResponseShutdownTimeoutError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    import structlog

    from mindroom.reply_scope import ReplyRuntime
    from mindroom.runtime_shutdown import RuntimeShutdownIntent

_PROCESS_SHUTDOWN_CANCEL_RETRY_SECONDS = 0.01


async def _cancel_pending_responses(
    pending: set[asyncio.Task[None]],
    *,
    timeout_seconds: float,
    shutdown_intent: RuntimeShutdownIntent,
) -> set[asyncio.Task[None]]:
    """Cancel pending responses without extending the caller's cleanup window."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_seconds)
    while pending:
        for task in pending:
            request_task_cancel(
                task,
                cancel_source=shutdown_intent.cancel_source,
                process_shutdown=shutdown_intent.hands_off_unfinished_work,
            )
        remaining_seconds = max(0.0, deadline - loop.time())
        window_expired = remaining_seconds == 0.0
        wait_seconds = remaining_seconds
        if shutdown_intent.stop_reason == "shutdown":
            wait_seconds = min(wait_seconds, _PROCESS_SHUTDOWN_CANCEL_RETRY_SECONDS)
        _done, pending = await asyncio.wait(pending, timeout=wait_seconds)
        if window_expired or shutdown_intent.stop_reason != "shutdown":
            break
    return pending


@dataclass
class _InboxResponseOwnership:
    """Own a response through terminal cleanup and consumption of its recovery proof."""

    recovery_proof_ready: Callable[[], bool | Awaitable[bool]]
    on_failure: Callable[[], None] | None
    shutdown_phase_trace: ResponseShutdownPhaseTrace
    source_event_ids: frozenset[str]
    room_id: str
    drain_intent: RuntimeShutdownIntent | None = None
    proof_task: asyncio.Task[bool] | None = None


@dataclass
class InboxResponses:
    """Own one entity's detached inbox responses through terminal cleanup and their shutdown recovery proofs."""

    logger: structlog.stdlib.BoundLogger
    # This bot instance's owner of durable reply records.
    replies: ReplyRuntime
    retry_approval_sources: Callable[[str, tuple[str, ...]], None]
    _inbox_response_tasks: dict[asyncio.Task[None], _InboxResponseOwnership] = field(default_factory=dict, init=False)
    _incomplete_inbox_responses_recoverable: bool = field(default=True, init=False)
    _process_shutdown_started: bool = field(default=False, init=False)

    def track_inbox_response(
        self,
        response: Coroutine[Any, Any, None],
        *,
        name: str,
        room_id: str,
        recovery_proof_ready: Callable[[], bool | Awaitable[bool]],
        on_failure: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
        source_event_ids: tuple[str, ...] = (),
    ) -> asyncio.Task[None]:
        """Own one detached inbox response until it completes or a drain settles it.

        Its sources go back to the journal when it finishes, unless a deferred
        claim of them waits for what blocks it, which retries them then.
        """
        if self._process_shutdown_started:
            response.close()
            raise ResponseAdmissionRefusedError
        shutdown_phase_trace = ResponseShutdownPhaseTrace()
        task = asyncio.create_task(
            response,
            name=name,
            context=context_with_response_shutdown_trace(shutdown_phase_trace),
        )
        self._inbox_response_tasks[task] = _InboxResponseOwnership(
            recovery_proof_ready=recovery_proof_ready,
            on_failure=on_failure,
            shutdown_phase_trace=shutdown_phase_trace,
            source_event_ids=frozenset(source_event_ids),
            room_id=room_id,
        )
        if on_terminal is not None:
            task.add_done_callback(lambda _finished: on_terminal())
        task.add_done_callback(self._finish_inbox_response_task)
        return task

    def has_live_inbox_response(self, source_event_id: str) -> bool:
        """Return whether a managed response task still owns one journal source."""
        return any(
            not task.done() and source_event_id in ownership.source_event_ids
            for task, ownership in self._inbox_response_tasks.items()
        )

    @property
    def pending_inbox_response_count(self) -> int:
        """Return an event-loop-local snapshot of runner-owned unsettled responses."""
        return sum(
            not task.done() or ownership.drain_intent is not None
            for task, ownership in self._inbox_response_tasks.items()
        )

    @property
    def pending_response_phase_counts(self) -> dict[str, int]:
        """Aggregate one phase per response and its retained proof."""
        counts: dict[str, int] = {}
        for task, ownership in self._inbox_response_tasks.items():
            if task.done() and ownership.drain_intent is None:
                continue
            phase = ResponseShutdownPhase.RECOVERY_PROOF.value if task.done() else ownership.shutdown_phase_trace.phase
            counts[phase] = counts.get(phase, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def incomplete_inbox_responses_recoverable(self) -> bool:
        """Return whether every timed-out response has finished cleanup with recovery proof."""
        return self._incomplete_inbox_responses_recoverable

    @property
    def process_shutdown_started(self) -> bool:
        """Return whether orderly process shutdown has closed response admission."""
        return self._process_shutdown_started

    def reopen_admission(self) -> None:
        """Admit responses again for a fresh sync-loop generation."""
        self._process_shutdown_started = False

    def _finish_inbox_response_task(self, task: asyncio.Task[None]) -> None:
        ownership = self._inbox_response_tasks.get(task)
        if ownership is not None and ownership.drain_intent is None:
            self._inbox_response_tasks.pop(task)
        if (
            ownership is not None
            and ownership.source_event_ids
            # A deferred claim is retried by what blocks it once that resolves, not here at once in a loop.
            and not self.replies.waits_to_claim(ownership.source_event_ids)
        ):
            self.retry_approval_sources(ownership.room_id, tuple(ownership.source_event_ids))
        if task.cancelled():
            return
        error = task.exception()
        if isinstance(error, ResponseAdmissionRefusedError):
            # The Matrix callback awaiting this pre-lock task surfaces the
            # refusal into sync-checkpoint failure accounting.
            return
        if error is not None:
            if ownership is not None and ownership.on_failure is not None:
                ownership.on_failure()
            self.logger.error(
                "inbox_response_task_failed",
                task_name=task.get_name(),
                exception_type=error.__class__.__name__,
                error=str(error),
            )

    def begin_process_shutdown(self) -> None:
        """Signal owned responses before other orderly-shutdown drains spend the budget."""
        self._process_shutdown_started = True
        for task, ownership in tuple(self._inbox_response_tasks.items()):
            if ownership.drain_intent == ORDERLY_SHUTDOWN:
                continue
            if task.done() and ownership.drain_intent is None:
                continue
            ownership.drain_intent = ORDERLY_SHUTDOWN
            if task.done():
                self._start_terminal_process_recovery_proof(task)
            else:
                task.add_done_callback(self._start_terminal_process_recovery_proof)
                request_task_cancel(task, process_shutdown=True)

    async def _evaluate_recovery_proof(
        self,
        ownership: _InboxResponseOwnership,
    ) -> bool:
        """Evaluate one exact durable handoff, following a pending commit."""
        while True:
            ready = ownership.recovery_proof_ready()
            if inspect.isawaitable(ready):
                ready = await ready
            if ready or ownership.drain_intent != ORDERLY_SHUTDOWN:
                return bool(ready)
            await asyncio.sleep(_PROCESS_SHUTDOWN_CANCEL_RETRY_SECONDS)

    def _ensure_recovery_proof_task(
        self,
        response_task: asyncio.Task[None],
        ownership: _InboxResponseOwnership,
    ) -> asyncio.Task[bool]:
        """Return the single proof owner for one terminal response."""
        proof_task = ownership.proof_task
        retry_failed_check = (
            proof_task is not None
            and proof_task.done()
            and not proof_task.cancelled()
            and proof_task.exception() is None
            and not proof_task.result()
            and ownership.drain_intent == ORDERLY_SHUTDOWN
        )
        if proof_task is None or proof_task.cancelled() or retry_failed_check:
            proof_task = asyncio.create_task(
                self._evaluate_recovery_proof(ownership),
                name=f"response_recovery_proof:{response_task.get_name()}",
            )
            ownership.proof_task = proof_task
            proof_task.add_done_callback(
                lambda finished: self._finish_recovery_proof_task(
                    ownership,
                    finished,
                ),
            )
        return proof_task

    def _start_terminal_process_recovery_proof(
        self,
        response_task: asyncio.Task[None],
    ) -> None:
        """Use the remaining cleanup window once one response is terminal."""
        ownership = self._inbox_response_tasks.get(response_task)
        if ownership is None or ownership.drain_intent != ORDERLY_SHUTDOWN:
            return
        if not response_task.cancelled() and response_task.exception() is None:
            return
        self._ensure_recovery_proof_task(
            response_task,
            ownership,
        )

    async def _recovery_proofs_are_ready(
        self,
        ownerships: dict[asyncio.Task[None], _InboxResponseOwnership],
        *,
        deadline: float | None = None,
    ) -> bool:
        """Prove terminal tasks transferred ownership within the drain deadline."""
        if any(not task.done() for task in ownerships):
            return False
        proof_targets = {task for task in ownerships if task.cancelled() or task.exception() is not None}
        if not proof_targets:
            return True

        while True:
            proof_tasks = {
                self._ensure_recovery_proof_task(
                    response_task,
                    ownerships[response_task],
                )
                for response_task in proof_targets
            }
            try:
                remaining_seconds = None if deadline is None else max(0.0, deadline - asyncio.get_running_loop().time())
                _done, pending = await asyncio.wait(proof_tasks, timeout=remaining_seconds)
                if pending:
                    msg = "response recovery proof exceeded bounded cleanup"
                    raise ResponseShutdownTimeoutError(msg)
                # The preceding bounded phase may have cancelled a proof while
                # its journal read was already committed to an offload worker.
                # Reusing that still-running task is required for ownership,
                # but its eventual CancelledError is not a new proof result.
                # Only after it is terminal may this phase launch a replacement.
                if any(task.cancelled() for task in proof_tasks):
                    continue
                return all(task.result() for task in proof_tasks)
            finally:
                pending = {task for task in proof_tasks if not task.done()}
                for task in pending:
                    task.cancel()

    def _finish_recovery_proof_task(
        self,
        ownership: _InboxResponseOwnership,
        proof_task: asyncio.Task[bool],
    ) -> None:
        """Retain a proof result for cleanup retry, or forget a cancelled read."""
        if proof_task.cancelled():
            if ownership.proof_task is proof_task:
                ownership.proof_task = None
            return
        proof_task.exception()

    def _release_response_ownership(self, tasks: set[asyncio.Task[None]]) -> None:
        """Release terminal response records after their proof results are consumed."""
        for task in tasks:
            self._inbox_response_tasks.pop(task, None)

    async def finish_process_shutdown_recovery(
        self,
        *,
        timeout_seconds: float,
    ) -> bool:
        """Finish retained response ownership inside an explicit final budget."""
        return await self.drain_inbox_responses(
            cancel_after_seconds=max(0.0, timeout_seconds) / 2,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )

    async def drain_inbox_responses(
        self,
        *,
        cancel_after_seconds: float | None = None,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> bool:
        """Settle detached inbox responses: graceful drains await, bounded grace cancels.

        Generic bounded cancellation returns False because the interrupted work
        has no durable restart contract. Orderly process cancellation returns
        True only when every task is terminal and its recovery callback proves
        journal or outbox ownership. The grace period uses two
        ``cancel_after_seconds`` windows: one waiting for completion and one
        letting cancelled tasks run cleanup. Orderly process shutdown repeats
        its cancellation signal inside the same second window so finite
        cancellation-resistant cleanup cannot strand ownership. A task that
        remains live after that cleanup window fails the shutdown boundary;
        callers must not release resources it can still use.
        """
        ownerships = {
            task: ownership
            for task, ownership in self._inbox_response_tasks.items()
            if not task.done() or ownership.drain_intent is not None
        }
        if not ownerships:
            return True
        loop = asyncio.get_running_loop()
        deadline = None if cancel_after_seconds is None else loop.time() + 2 * max(0.0, cancel_after_seconds)
        if cancel_after_seconds is None:
            await asyncio.gather(*ownerships, return_exceptions=True)
            pending: set[asyncio.Task[None]] = set()
        else:
            _done, pending = await asyncio.wait(ownerships, timeout=max(0.0, cancel_after_seconds))
        cancelled = bool(pending)
        for task in pending:
            # Retain the same record before cancellation can run its done callback.
            if ownerships[task].drain_intent is None:
                ownerships[task].drain_intent = GENERIC_SHUTDOWN
        if pending:
            assert deadline is not None
            pending = await _cancel_pending_responses(
                pending,
                timeout_seconds=max(0.0, deadline - loop.time()),
                shutdown_intent=shutdown_intent,
            )
        proof_targets = {
            task: ownership for task, ownership in ownerships.items() if ownership.drain_intent is not None
        }
        process_shutdown = any(ownership.drain_intent == ORDERLY_SHUTDOWN for ownership in ownerships.values())
        recoverable = await self._recovery_proofs_are_ready(
            proof_targets,
            deadline=deadline,
        )
        self._incomplete_inbox_responses_recoverable &= recoverable
        if pending:
            msg = f"{len(pending)} response tasks did not stop within bounded cleanup"
            raise ResponseShutdownTimeoutError(msg)
        self._release_response_ownership(set(proof_targets))
        return recoverable if process_shutdown else not cancelled

    async def wait_for_source_owned_inbox_responses(self) -> None:
        """Wait for detached responses that still own durable journal sources."""
        tasks = [
            task
            for task, ownership in self._inbox_response_tasks.items()
            if not task.done() and ownership.source_event_ids
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
