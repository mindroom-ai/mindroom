"""Response attempt lifecycle helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom import response_attempt as response_attempt_module
from mindroom.cancellation import (
    SYNC_RESTART_CANCEL_MSG,
    USER_STOP_CANCEL_MSG,
    current_task_is_process_shutdown,
    request_task_cancel,
)
from mindroom.config.main import Config
from mindroom.message_target import MessageTarget
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner, SpanAttempt


@dataclass
class _Span:
    """Records what one attempt tells the reply span that owns it."""

    registered: list[asyncio.Task[None]] = field(default_factory=list)
    stop_buttons: list[str] = field(default_factory=list)

    def attempt(self) -> SpanAttempt:
        return SpanAttempt(register=self.registered.append, add_stop_button=self._add_stop_button)

    async def _add_stop_button(self, message_id: str) -> None:
        self.stop_buttons.append(message_id)


def _runner(*, show_stop_button: bool = False) -> tuple[ResponseAttemptRunner, _Span]:
    return (
        ResponseAttemptRunner(
            ResponseAttemptDeps(
                client=MagicMock(user_id="@mindroom_agent:localhost"),
                logger=MagicMock(),
                show_stop_button=lambda: show_stop_button,
                config=Config(),
            ),
        ),
        _Span(),
    )


@pytest.mark.asyncio
async def test_response_attempt_tracks_the_adopted_placeholder_as_the_visible_task() -> None:
    """The attempt runs against the placeholder the turn already put in the room.

    It has no delivery gateway at all, which is the point: the durable outbox
    row is the only thing allowed to make a placeholder visible, so this runner
    cannot add a second one even by mistake.
    """
    target = MessageTarget.resolve("!room:localhost", "$thread", "$reply")
    runner, span = _runner()
    seen_message_ids: list[str | None] = []
    attempt_tasks: list[asyncio.Task[None] | None] = []

    async def response_function(message_id: str | None) -> None:
        seen_message_ids.append(message_id)
        attempt_tasks.append(asyncio.current_task())

    message_id = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=response_function,
            span=span.attempt(),
            existing_event_id="$thinking",
        ),
    )

    assert message_id == "$thinking"
    assert seen_message_ids == ["$thinking"]
    assert span.registered == attempt_tasks
    assert span.stop_buttons == []


@pytest.mark.asyncio
async def test_response_attempt_without_visible_message_registers_its_task_without_a_stop_button() -> None:
    """A reply span can stop an attempt that has no visible message yet, which has no event to carry a Stop button."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply", room_mode=True)
    runner, span = _runner(show_stop_button=True)
    seen_message_ids: list[str | None] = []
    attempt_tasks: list[asyncio.Task[None] | None] = []

    async def response_function(message_id: str | None) -> None:
        seen_message_ids.append(message_id)
        attempt_tasks.append(asyncio.current_task())

    message_id = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=response_function,
            span=span.attempt(),
        ),
    )

    assert message_id is None
    assert seen_message_ids == [None]
    assert span.registered == attempt_tasks
    assert span.stop_buttons == []


@pytest.mark.asyncio
async def test_response_attempt_adds_the_reply_stop_button_for_online_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Online users get the reply span's Stop button on the attempt's message."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$reply")
    runner, span = _runner(show_stop_button=True)
    is_user_online = AsyncMock(return_value=True)
    monkeypatch.setattr(response_attempt_module, "is_user_online", is_user_online)

    async def response_function(_message_id: str | None) -> None:
        return None

    message_id = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=response_function,
            span=span.attempt(),
            existing_event_id="$thinking",
            user_id="@user:localhost",
        ),
    )

    assert message_id == "$thinking"
    is_user_online.assert_awaited_once_with(
        runner.deps.client,
        "@user:localhost",
        room_id="!room:localhost",
    )
    assert span.stop_buttons == ["$thinking"]
    runner.deps.logger.info.assert_any_call(
        "Stop button decision",
        message_id="$thinking",
        user_online=True,
        show_button=True,
    )
    runner.deps.logger.info.assert_any_call("Adding stop button", message_id="$thinking")


@pytest.mark.asyncio
async def test_outer_cancellation_is_forwarded_to_attempt_task() -> None:
    """Cancelling the awaiting chain must cancel the attempt task with the same provenance."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$reply")
    runner, span = _runner()
    inner_started = asyncio.Event()
    inner_cancel_args: list[tuple[object, ...]] = []
    cancellation_reasons: list[str] = []

    async def response_function(_message_id: str | None) -> None:
        inner_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            inner_cancel_args.append(exc.args)
            raise

    outer = asyncio.create_task(
        runner.run(
            ResponseAttemptRequest(
                target=target,
                response_function=response_function,
                span=span.attempt(),
                existing_event_id="$existing",
                on_cancelled=cancellation_reasons.append,
            ),
        ),
    )
    await inner_started.wait()
    outer.cancel(msg=SYNC_RESTART_CANCEL_MSG)
    assert await outer == "$existing"

    assert cancellation_reasons == ["sync_restart_cancelled"]
    assert inner_cancel_args == [(SYNC_RESTART_CANCEL_MSG,)]


@pytest.mark.asyncio
async def test_process_shutdown_keeps_outer_attempt_owned_until_child_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orderly shutdown cannot finish the owned runner while its response child is live."""
    monkeypatch.setattr(response_attempt_module, "_FORWARDED_CANCEL_WAIT_SECONDS", 0.01)
    target = MessageTarget.resolve("!room:localhost", "$thread", "$reply")
    runner, span = _runner()
    child_started = asyncio.Event()
    child_cancelled = asyncio.Event()
    release_child = asyncio.Event()
    child_tasks: list[asyncio.Task[None]] = []
    child_shutdown_markers: list[bool] = []

    async def response_function(_message_id: str | None) -> None:
        child = asyncio.current_task()
        assert child is not None
        child_tasks.append(child)
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_shutdown_markers.append(current_task_is_process_shutdown())
            child_cancelled.set()
            await release_child.wait()
            raise

    outer = asyncio.create_task(
        runner.run(
            ResponseAttemptRequest(
                target=target,
                response_function=response_function,
                span=span.attempt(),
                existing_event_id="$existing",
            ),
        ),
    )
    await child_started.wait()
    request_task_cancel(outer, process_shutdown=True)

    try:
        await asyncio.wait_for(child_cancelled.wait(), timeout=1.0)
        await asyncio.sleep(0.02)
        assert child_shutdown_markers == [True]
        assert not outer.done()
        assert span.registered == child_tasks
    finally:
        release_child.set()
        results = await asyncio.gather(outer, *child_tasks, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    assert outer.cancelled()
    runner.deps.logger.warning.assert_called_once()
    assert runner.deps.logger.warning.call_args.kwargs["exc_info"] is False


@pytest.mark.asyncio
async def test_process_shutdown_upgrade_retains_generic_unwind_child() -> None:
    """A second cancellation upgrades child ownership and still propagates interruption."""
    runner, span = _runner()
    started = asyncio.Event()
    unwinding = asyncio.Event()
    upgraded = asyncio.Event()
    release = asyncio.Event()
    flags: list[bool] = []

    async def response_function(_message_id: str | None) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            flags.append(current_task_is_process_shutdown())
            unwinding.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    flags.append(current_task_is_process_shutdown())
                    upgraded.set()
            raise

    outer = asyncio.create_task(
        runner.run(
            ResponseAttemptRequest(
                target=MessageTarget.resolve("!room:localhost", "$thread", "$reply"),
                existing_event_id="$existing",
                response_function=response_function,
                span=span.attempt(),
            ),
        ),
    )
    await started.wait()
    request_task_cancel(outer, cancel_source="sync_restart")
    await unwinding.wait()
    request_task_cancel(outer, process_shutdown=True)
    try:
        await asyncio.wait_for(upgraded.wait(), timeout=0.1)
        assert flags == [False, True]
        assert not outer.done()
    finally:
        release.set()
        results = await asyncio.gather(outer, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_process_shutdown_during_stop_button_setup_still_owns_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during presence setup must not orphan the already-started response."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$reply")
    runner, span = _runner(show_stop_button=True)
    child_started = asyncio.Event()
    child_cancelled = asyncio.Event()
    presence_started = asyncio.Event()
    release_child = asyncio.Event()
    child_tasks: list[asyncio.Task[None]] = []

    async def response_function(_message_id: str | None) -> None:
        child = asyncio.current_task()
        assert child is not None
        child_tasks.append(child)
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert current_task_is_process_shutdown()
            child_cancelled.set()
            await release_child.wait()
            raise

    async def wait_for_presence(*_args: object, **_kwargs: object) -> bool:
        presence_started.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(response_attempt_module, "is_user_online", wait_for_presence)
    outer = asyncio.create_task(
        runner.run(
            ResponseAttemptRequest(
                target=target,
                response_function=response_function,
                span=span.attempt(),
                existing_event_id="$existing",
                user_id="@user:localhost",
            ),
        ),
    )
    await child_started.wait()
    await presence_started.wait()
    request_task_cancel(outer, process_shutdown=True)

    try:
        await asyncio.wait_for(child_cancelled.wait(), timeout=1.0)
        assert not outer.done()
        assert span.registered == child_tasks
    finally:
        for child in child_tasks:
            if not child.done():
                child.cancel()
        release_child.set()
        await asyncio.gather(outer, *child_tasks, return_exceptions=True)

    assert span.stop_buttons == []


@pytest.mark.asyncio
async def test_attempt_task_error_during_forwarded_cancellation_is_logged() -> None:
    """An attempt task that errors while unwinding the forced cancel must be reported."""
    runner, _span = _runner()
    inner_started = asyncio.Event()

    async def misbehaving_attempt() -> None:
        inner_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            msg = "cleanup failed during cancellation"
            raise RuntimeError(msg) from None

    task = asyncio.create_task(misbehaving_attempt())
    await inner_started.wait()
    await runner._forward_cancel_to_attempt_task(task, asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG))

    error_calls = runner.deps.logger.error.call_args_list
    assert len(error_calls) == 1
    assert error_calls[0].args == ("Response attempt task failed while unwinding forwarded cancellation",)
    assert error_calls[0].kwargs["error"] == "cleanup failed during cancellation"


@pytest.mark.asyncio
async def test_timed_out_attempt_task_failure_is_logged_when_it_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A straggler outliving the forwarded-cancel wait must still report its eventual failure."""
    monkeypatch.setattr(response_attempt_module, "_FORWARDED_CANCEL_WAIT_SECONDS", 0.01)
    runner, _span = _runner()
    inner_started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_attempt() -> None:
        inner_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Resist the forwarded cancel past the wait timeout, then fail.
            await release.wait()
            msg = "late cleanup failure"
            raise RuntimeError(msg) from None

    task = asyncio.create_task(stubborn_attempt())
    await inner_started.wait()
    await runner._forward_cancel_to_attempt_task(task, asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG))

    runner.deps.logger.warning.assert_called_once()
    runner.deps.logger.error.assert_not_called()

    release.set()
    with pytest.raises(RuntimeError, match="late cleanup failure"):
        await task
    await asyncio.sleep(0)  # Let the done callback run.

    error_calls = runner.deps.logger.error.call_args_list
    assert len(error_calls) == 1
    assert error_calls[0].args == ("Response attempt task failed while unwinding forwarded cancellation",)
    assert error_calls[0].kwargs["error"] == "late cleanup failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_args", "expected_reason", "log_method", "log_message"),
    [
        ((USER_STOP_CANCEL_MSG,), "cancelled_by_user", "info", "Response cancelled by user"),
        ((SYNC_RESTART_CANCEL_MSG,), "sync_restart_cancelled", "info", "Response interrupted by sync restart"),
        ((), "interrupted", "warning", "Response interrupted — traceback for diagnosis"),
    ],
)
async def test_response_attempt_cancellation_records_reason_and_logs_provenance(
    cancel_args: tuple[str, ...],
    expected_reason: str,
    log_method: str,
    log_message: str,
) -> None:
    """Cancelled attempts should classify and log their cancellation provenance."""
    target = MessageTarget.resolve("!room:localhost", "$thread", "$reply")
    runner, span = _runner()
    cancellation_reasons: list[str] = []

    async def response_function(_message_id: str | None) -> None:
        raise asyncio.CancelledError(*cancel_args)

    message_id = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=response_function,
            span=span.attempt(),
            existing_event_id="$existing",
            on_cancelled=cancellation_reasons.append,
        ),
    )

    assert message_id == "$existing"
    assert cancellation_reasons == [expected_reason]
    getattr(runner.deps.logger, log_method).assert_called_once()
    log_call = getattr(runner.deps.logger, log_method).call_args
    assert log_call.args[0] == log_message
    assert log_call.kwargs["message_id"] == "$existing"
