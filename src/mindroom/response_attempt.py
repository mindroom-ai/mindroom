"""Run one visible response attempt with cancellation tracking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import STREAM_STATUS_KEY, STREAM_STATUS_PENDING
from mindroom.delivery_gateway import SendTextRequest
from mindroom.logging_config import bound_log_context
from mindroom.matrix.presence import is_user_online
from mindroom.orchestration.runtime import cancel_failure_reason, classify_cancel_source

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    import nio
    import structlog

    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.message_target import MessageTarget
    from mindroom.stop import StopManager
    from mindroom.timing import DispatchPipelineTiming

type MatrixEventId = str


@dataclass(frozen=True)
class ResponseAttemptDeps:
    """Collaborators needed to run a visible response attempt."""

    client: nio.AsyncClient
    delivery_gateway: DeliveryGateway
    stop_manager: StopManager
    logger: structlog.stdlib.BoundLogger
    show_stop_button: Callable[[], bool]
    notify_outbound_event: Callable[[str, dict[str, object]], None]
    notify_outbound_redaction: Callable[[str, str], None]


@dataclass(frozen=True)
class ResponseAttemptRequest:
    """Inputs for one cancellable response attempt."""

    target: MessageTarget
    response_function: Callable[[str | None], Coroutine[Any, Any, None]]
    thinking_message: str | None = None
    existing_event_id: str | None = None
    user_id: str | None = None
    run_id: str | None = None
    pipeline_timing: DispatchPipelineTiming | None = None
    on_cancelled: Callable[[str], None] | None = None


def log_cancelled_response(
    logger: structlog.stdlib.BoundLogger,
    *,
    exc: asyncio.CancelledError,
    message_id: str | None,
    restart_message: str,
    user_stop_message: str,
    interrupted_message: str,
) -> None:
    """Log one CancelledError with the right provenance label."""
    cancel_source = classify_cancel_source(exc)
    if cancel_source == "sync_restart":
        logger.info(restart_message, message_id=message_id)
    elif cancel_source == "user_stop":
        logger.info(user_stop_message, message_id=message_id)
    else:
        logger.warning(
            interrupted_message,
            message_id=message_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


@dataclass(frozen=True)
class ResponseAttemptRunner:
    """Own placeholder delivery, stop tracking, and attempt task cleanup."""

    deps: ResponseAttemptDeps

    async def _send_thinking_message(self, request: ResponseAttemptRequest) -> MatrixEventId | None:
        message_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=request.target,
                response_text=request.thinking_message or "",
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
            ),
        )
        if message_id is not None and request.pipeline_timing is not None:
            request.pipeline_timing.mark("placeholder_sent")
            request.pipeline_timing.mark_first_visible_reply("placeholder")
        return message_id

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

    async def run(self, request: ResponseAttemptRequest) -> MatrixEventId | None:
        """Run one response coroutine under visible message tracking."""
        if request.thinking_message is not None and request.existing_event_id is not None:
            msg = "thinking_message and existing_event_id are mutually exclusive"
            raise ValueError(msg)

        with bound_log_context(**request.target.log_context):
            initial_message_id = None
            if request.thinking_message is not None:
                initial_message_id = await self._send_thinking_message(request)

            message_id = request.existing_event_id or initial_message_id
            task: asyncio.Task[None] = asyncio.create_task(request.response_function(message_id))
            tracked_message_id = message_id or f"__pending_response__:{id(task)}"
            show_stop_button = False

            self.deps.stop_manager.set_current(
                tracked_message_id,
                request.target,
                task,
                None,
                run_id=request.run_id,
            )

            if message_id is not None:
                show_stop_button = await self._should_show_stop_button(request, message_id)
                if show_stop_button:
                    self.deps.logger.info("Adding stop button", message_id=message_id)
                    await self.deps.stop_manager.add_stop_button(
                        self.deps.client,
                        message_id,
                        notify_outbound_event=self.deps.notify_outbound_event,
                    )

            try:
                await task
            except asyncio.CancelledError as exc:
                failure_reason = cancel_failure_reason(classify_cancel_source(exc))
                if request.on_cancelled is not None:
                    request.on_cancelled(failure_reason)
                log_cancelled_response(
                    self.deps.logger,
                    exc=exc,
                    message_id=message_id or tracked_message_id,
                    restart_message="Response interrupted by sync restart",
                    user_stop_message="Response cancelled by user",
                    interrupted_message="Response interrupted — traceback for diagnosis",
                )
            except Exception as error:
                self.deps.logger.exception("Error during response generation", error=str(error))
                raise
            finally:
                tracked = self.deps.stop_manager.tracked_messages.get(tracked_message_id)
                button_already_removed = tracked is None or tracked.reaction_event_id is None
                self.deps.stop_manager.clear_message(
                    tracked_message_id,
                    self.deps.client,
                    remove_button=show_stop_button and not button_already_removed,
                    notify_outbound_redaction=self.deps.notify_outbound_redaction,
                )

            return message_id


__all__ = [
    "ResponseAttemptDeps",
    "ResponseAttemptRequest",
    "ResponseAttemptRunner",
    "log_cancelled_response",
]
