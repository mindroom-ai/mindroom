"""Run one visible response attempt with cancellation tracking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.cancellation import (
    current_task_is_process_shutdown,
    request_task_cancel,
    task_cancel_source_from_message,
)
from mindroom.logging_config import bound_log_context
from mindroom.matrix.presence import is_user_online
from mindroom.orchestration.runtime import cancel_failure_reason, classify_cancel_source, log_cancelled_response
from mindroom.streaming import StreamingLifecycleSuspensionError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    import nio
    import structlog

    from mindroom.config.main import Config
    from mindroom.message_target import MessageTarget

type _MatrixEventId = str

_FORWARDED_CANCEL_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class ResponseAttemptDeps:
    """Collaborators needed to run a visible response attempt."""

    client: nio.AsyncClient
    logger: structlog.stdlib.BoundLogger
    show_stop_button: Callable[[], bool]
    config: Config


@dataclass(frozen=True)
class SpanAttempt:
    """A reply span owns the attempt: the span registry cancels it and the reply records its Stop button."""

    # Told the attempt task as soon as it exists, so a Stop on the span cancels exactly it.
    register: Callable[[asyncio.Task[None]], None]
    # Shows the Stop button on the reply's event, best effort.
    add_stop_button: Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ResponseAttemptRequest:
    """Inputs for one cancellable response attempt."""

    target: MessageTarget
    response_function: Callable[[str | None], Coroutine[Any, Any, None]]
    # The reply span this attempt executes.
    span: SpanAttempt
    existing_event_id: str | None = None
    user_id: str | None = None
    on_cancelled: Callable[[str], None] | None = None


@dataclass(frozen=True)
class ResponseAttemptRunner:
    """Own the attempt task of one reply span: its registration, Stop button, and cleanup.

    Sending the turn's placeholder is deliberately not part of this: the
    durable ``INITIAL`` outbox row is the only thing allowed to put one in the
    room. A second sender here would send under a transaction ID nothing owns,
    so a placeholder the homeserver accepted without confirming would sit
    beside the one recovery replays -- two "Thinking..." messages for one turn,
    only one of which the answer ever edits.
    """

    deps: ResponseAttemptDeps

    async def _should_show_stop_button(self, request: ResponseAttemptRequest, message_id: str) -> bool:
        show_stop_button = self.deps.show_stop_button()
        if not show_stop_button or request.user_id is None:
            return show_stop_button
        user_is_online = await is_user_online(
            self.deps.client,
            request.user_id,
            room_id=request.target.room_id,
        )
        self.deps.logger.info(
            "Stop button decision",
            message_id=message_id,
            user_online=user_is_online,
            show_button=user_is_online,
        )
        return user_is_online

    async def _forward_cancel_to_attempt_task(self, task: asyncio.Task[None], exc: asyncio.CancelledError) -> None:
        """Cancel the attempt task when the awaiting chain was cancelled instead.

        Sync-restart recovery cancels the dispatch chain, not the attempt task it
        awaits; without forwarding, the generation keeps running as an orphan and
        races the cancelled-note delivery for the same visible message.
        """
        if task.done():
            return
        process_shutdown = current_task_is_process_shutdown()
        request_task_cancel(
            task,
            cancel_source=task_cancel_source_from_message(str(exc.args[0]) if exc.args else None),
            process_shutdown=process_shutdown,
        )
        if process_shutdown:
            await self._wait_for_process_shutdown_child(task)
            return
        try:
            done, pending = await asyncio.wait({task}, timeout=_FORWARDED_CANCEL_WAIT_SECONDS)
        except asyncio.CancelledError:
            if not current_task_is_process_shutdown():
                raise
            # A process stop can upgrade a generic cancellation already in flight.
            # Retag and retain the same child under the process-shutdown wait.
            request_task_cancel(task, process_shutdown=True)
            await self._wait_for_process_shutdown_child(task)
            return
        if pending:
            self.deps.logger.warning(
                "Response attempt task did not finish after forwarded cancellation",
                task_name=task.get_name(),
            )
            task.add_done_callback(self._log_attempt_unwind_failure)
        for finished in done:
            self._log_attempt_unwind_failure(finished)

    async def _wait_for_process_shutdown_child(self, task: asyncio.Task[None]) -> None:
        """Keep the awaiting owner alive until its process-tagged child finishes."""
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    request_task_cancel(task, process_shutdown=True)
            except Exception:
                break
        self._log_attempt_unwind_failure(task)

    def _log_attempt_unwind_failure(self, task: asyncio.Task[None]) -> None:
        """Consume one finished attempt task's outcome, reporting unwind failures.

        Without this, a task that errors while unwinding from the forced cancel
        surfaces only via asyncio's GC-time unretrieved-exception handler.
        """
        error = task.exception() if not task.cancelled() else None
        if error is not None:
            self.deps.logger.error(
                "Response attempt task failed while unwinding forwarded cancellation",
                task_name=task.get_name(),
                error=str(error),
            )

    async def run(self, request: ResponseAttemptRequest) -> _MatrixEventId | None:
        """Run one response coroutine as its reply span's attempt."""
        with bound_log_context(**request.target.log_context):
            message_id = request.existing_event_id
            task: asyncio.Task[None] = asyncio.create_task(request.response_function(message_id))
            request.span.register(task)
            try:
                if message_id is not None:
                    await self._add_stop_button(request, message_id)

                await asyncio.shield(task)
            except asyncio.CancelledError as caught_cancellation:
                cancellation = caught_cancellation
                if task.done() and task.cancelled():
                    try:
                        task.result()
                    except asyncio.CancelledError as child_cancellation:
                        cancellation = child_cancellation
                failure_reason = cancel_failure_reason(classify_cancel_source(cancellation))
                if request.on_cancelled is not None:
                    request.on_cancelled(failure_reason)
                await self._forward_cancel_to_attempt_task(task, cancellation)
                log_cancelled_response(
                    self.deps.logger,
                    exc=cancellation,
                    message_id=message_id or task.get_name(),
                    restart_message="Response interrupted by sync restart",
                    user_stop_message="Response cancelled by user",
                    interrupted_message="Response interrupted — traceback for diagnosis",
                )
                if current_task_is_process_shutdown():
                    raise
            except StreamingLifecycleSuspensionError:
                raise
            except Exception as error:
                self.deps.logger.exception("Error during response generation", error=str(error))
                raise

            return message_id

    async def _add_stop_button(self, request: ResponseAttemptRequest, message_id: str) -> None:
        """Show the Stop button on the reply's event when wanted; the reply records it and redacts it."""
        if not await self._should_show_stop_button(request, message_id):
            return
        self.deps.logger.info("Adding stop button", message_id=message_id)
        await request.span.add_stop_button(message_id)


__all__ = [
    "ResponseAttemptDeps",
    "ResponseAttemptRequest",
    "ResponseAttemptRunner",
    "SpanAttempt",
    "log_cancelled_response",
]
