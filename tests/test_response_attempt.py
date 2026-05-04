"""Response attempt lifecycle helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom import response_attempt as response_attempt_module
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG, USER_STOP_CANCEL_MSG
from mindroom.constants import STREAM_STATUS_KEY, STREAM_STATUS_PENDING
from mindroom.message_target import MessageTarget
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner


@dataclass
class _TrackedMessage:
    reaction_event_id: str | None = None


class _StopManager:
    def __init__(self) -> None:
        self.tracked_messages: dict[str, _TrackedMessage] = {}
        self.set_current_calls: list[tuple[str, MessageTarget, object, str | None]] = []
        self.added_buttons: list[str] = []
        self.cleared_messages: list[tuple[str, bool]] = []
        self.add_stop_button_kwargs: list[dict[str, object]] = []
        self.clear_message_kwargs: list[dict[str, object]] = []

    def set_current(
        self,
        message_id: str,
        target: MessageTarget,
        task: object,
        _reaction_event_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self.tracked_messages[message_id] = _TrackedMessage()
        self.set_current_calls.append((message_id, target, task, run_id))

    async def add_stop_button(
        self,
        _client: object,
        message_id: str,
        **_kwargs: object,
    ) -> str:
        self.added_buttons.append(message_id)
        self.add_stop_button_kwargs.append(_kwargs)
        self.tracked_messages[message_id].reaction_event_id = "$reaction"
        return "$reaction"

    def clear_message(
        self,
        message_id: str,
        _client: object,
        *,
        remove_button: bool,
        **_kwargs: object,
    ) -> None:
        self.cleared_messages.append((message_id, remove_button))
        self.clear_message_kwargs.append(_kwargs)


class _DeliveryGateway:
    def __init__(self, event_id: str | None = "$thinking") -> None:
        self.event_id = event_id
        self.sent_requests: list[Any] = []

    async def send_text(self, request: object) -> str | None:
        self.sent_requests.append(request)
        return self.event_id


def _runner(
    *,
    delivery_gateway: _DeliveryGateway | None = None,
    stop_manager: _StopManager | None = None,
    show_stop_button: bool = False,
) -> tuple[ResponseAttemptRunner, _DeliveryGateway, _StopManager]:
    resolved_delivery_gateway = delivery_gateway or _DeliveryGateway()
    resolved_stop_manager = stop_manager or _StopManager()
    return (
        ResponseAttemptRunner(
            ResponseAttemptDeps(
                client=MagicMock(user_id="@mindroom_agent:localhost"),
                delivery_gateway=resolved_delivery_gateway,
                stop_manager=resolved_stop_manager,
                logger=MagicMock(),
                show_stop_button=lambda: show_stop_button,
                ignore_unverified_devices=lambda: False,
                notify_outbound_event=MagicMock(),
                notify_outbound_redaction=MagicMock(),
            ),
        ),
        resolved_delivery_gateway,
        resolved_stop_manager,
    )


@pytest.mark.asyncio
async def test_response_attempt_sends_pending_placeholder_and_tracks_visible_task() -> None:
    """Thinking messages should be sent as tracked pending placeholders."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$reply")
    runner, delivery_gateway, stop_manager = _runner()
    seen_message_ids: list[str | None] = []

    async def response_function(message_id: str | None) -> None:
        seen_message_ids.append(message_id)

    message_id = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=response_function,
            thinking_message="Thinking...",
            run_id="run-1",
        ),
    )

    assert message_id == "$thinking"
    assert seen_message_ids == ["$thinking"]
    assert delivery_gateway.sent_requests[0].target == target
    assert delivery_gateway.sent_requests[0].response_text == "Thinking..."
    assert delivery_gateway.sent_requests[0].extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}
    assert stop_manager.set_current_calls[0][0] == "$thinking"
    assert stop_manager.set_current_calls[0][1] == target
    assert stop_manager.set_current_calls[0][3] == "run-1"
    assert stop_manager.cleared_messages == [("$thinking", False)]


@pytest.mark.asyncio
async def test_response_attempt_uses_pending_tracking_key_without_visible_message() -> None:
    """Responses without a visible message should still be tracked until cleanup."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply", room_mode=True)
    runner, delivery_gateway, stop_manager = _runner(delivery_gateway=_DeliveryGateway(event_id=None))
    seen_message_ids: list[str | None] = []

    async def response_function(message_id: str | None) -> None:
        seen_message_ids.append(message_id)

    message_id = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=response_function,
        ),
    )

    assert message_id is None
    assert seen_message_ids == [None]
    assert delivery_gateway.sent_requests == []
    tracked_id = stop_manager.set_current_calls[0][0]
    assert tracked_id.startswith("__pending_response__:")
    assert stop_manager.cleared_messages == [(tracked_id, False)]


@pytest.mark.asyncio
async def test_response_attempt_adds_stop_button_for_online_user_and_removes_it_on_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Online users get a stop reaction that is removed during cleanup."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$reply")
    runner, _delivery_gateway, stop_manager = _runner(show_stop_button=True)
    is_user_online = AsyncMock(return_value=True)
    monkeypatch.setattr(response_attempt_module, "is_user_online", is_user_online)

    async def response_function(_message_id: str | None) -> None:
        return None

    message_id = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=response_function,
            thinking_message="Thinking...",
            user_id="@user:localhost",
        ),
    )

    assert message_id == "$thinking"
    is_user_online.assert_awaited_once_with(
        runner.deps.client,
        "@user:localhost",
        room_id="!room:localhost",
    )
    assert stop_manager.added_buttons == ["$thinking"]
    assert stop_manager.add_stop_button_kwargs == [
        {
            "ignore_unverified_devices": False,
            "notify_outbound_event": runner.deps.notify_outbound_event,
        },
    ]
    assert stop_manager.cleared_messages == [("$thinking", True)]
    assert stop_manager.clear_message_kwargs == [
        {"notify_outbound_redaction": runner.deps.notify_outbound_redaction},
    ]
    runner.deps.logger.info.assert_any_call(
        "Stop button decision",
        message_id="$thinking",
        user_online=True,
        show_button=True,
    )
    runner.deps.logger.info.assert_any_call("Adding stop button", message_id="$thinking")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_args", "expected_reason", "log_method", "log_message"),
    [
        ((USER_STOP_CANCEL_MSG,), "cancelled_by_user", "info", "Response cancelled by user"),
        ((SYNC_RESTART_CANCEL_MSG,), "sync_restart_cancelled", "info", "Response interrupted by sync restart"),
        ((), "interrupted", "warning", "Response interrupted — traceback for diagnosis"),
    ],
)
async def test_response_attempt_cancellation_records_reason_logs_provenance_and_clears_tracking(
    cancel_args: tuple[str, ...],
    expected_reason: str,
    log_method: str,
    log_message: str,
) -> None:
    """Cancelled attempts should classify provenance and always clear tracking."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$reply")
    runner, delivery_gateway, stop_manager = _runner()
    cancellation_reasons: list[str] = []

    async def response_function(_message_id: str | None) -> None:
        raise asyncio.CancelledError(*cancel_args)

    message_id = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=response_function,
            existing_event_id="$existing",
            on_cancelled=cancellation_reasons.append,
        ),
    )

    assert message_id == "$existing"
    assert delivery_gateway.sent_requests == []
    assert cancellation_reasons == [expected_reason]
    assert stop_manager.cleared_messages == [("$existing", False)]
    getattr(runner.deps.logger, log_method).assert_called_once()
    log_call = getattr(runner.deps.logger, log_method).call_args
    assert log_call.args[0] == log_message
    assert log_call.kwargs["message_id"] == "$existing"
