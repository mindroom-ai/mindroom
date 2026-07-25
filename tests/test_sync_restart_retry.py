"""One-shot retry of responses cancelled by sync-restart recovery."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from structlog.testing import capture_logs

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.history.types import HistoryScope
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest, ResponseRunner
from mindroom.sync_restart_retry import (
    _MAX_ATTEMPTED_KEYS,
    SyncRestartRetryQueue,
    interrupted_source_needs_retry,
)
from tests.conftest import delivered_matrix_event, request_envelope, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


def _stored_run(
    scope: HistoryScope,
    run_id: str,
    *,
    source_event_id: str | None = "$source",
    interrupted: bool = False,
) -> RunOutput | TeamRunOutput:
    metadata = {} if source_event_id is None else {MATRIX_EVENT_ID_METADATA_KEY: source_event_id}
    if interrupted:
        metadata["mindroom_replay_state"] = "interrupted"
    run_kwargs = {
        "run_id": run_id,
        "status": RunStatus.completed,
        "content": "answer",
        "metadata": metadata,
    }
    if scope.kind == "team":
        return TeamRunOutput(team_id=scope.scope_id, **run_kwargs)
    return RunOutput(agent_id=scope.scope_id, **run_kwargs)


def test_retry_history_uses_latest_matching_visible_run() -> None:
    """Only latest model-visible run for same source and scope decides retry eligibility."""
    scope = HistoryScope(kind="agent", scope_id="general")
    interrupted = _stored_run(scope, "interrupted", interrupted=True)

    assert interrupted_source_needs_retry([interrupted], scope=scope, source_event_id="$source") is True
    assert interrupted_source_needs_retry([], scope=scope, source_event_id="$source") is False
    for later in (_stored_run(scope, "completed"), _stored_run(scope, "failed-replay", interrupted=True)):
        assert interrupted_source_needs_retry([interrupted, later], scope=scope, source_event_id="$source") is False
    unrelated_runs = [
        interrupted,
        _stored_run(scope, "other-source", source_event_id="$other"),
        _stored_run(HistoryScope(kind="team", scope_id="team"), "other-scope"),
    ]
    assert interrupted_source_needs_retry(unrelated_runs, scope=scope, source_event_id="$source") is True
    ambiguous_runs = [interrupted, _stored_run(scope, "ambiguous", source_event_id=None)]
    assert interrupted_source_needs_retry(ambiguous_runs, scope=scope, source_event_id="$source") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("is_team", [False, True])
@pytest.mark.parametrize("history_case", ["current", "superseded", "missing", "degraded", "error"])
async def test_locked_retry_guard_precedes_payload_and_fails_closed(
    tmp_path: Path,
    *,
    is_team: bool,
    history_case: str,
) -> None:
    """Agent and team retries check history after lock and before payload work."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(reply_to_event_id="$source")
    execution_identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost")
    history_scope = (
        runner.deps.state_writer.team_history_scope([bot.matrix_id], requester_user_id=execution_identity.requester_id)
        if is_team
        else runner.deps.state_writer.history_scope()
    )
    runs = [_stored_run(history_scope, "interrupted", interrupted=True)]
    if history_case == "superseded":
        runs.append(_stored_run(history_scope, "completed"))
    session = (
        TeamSession(session_id=target.session_id, team_id=history_scope.scope_id, runs=runs)
        if is_team
        else AgentSession(session_id=target.session_id, agent_id=history_scope.scope_id, runs=runs)
    )
    storage = MagicMock()
    storage.get_session.return_value = {"missing": None, "degraded": object()}.get(history_case, session)
    if history_case == "error":
        storage.get_session.side_effect = RuntimeError("history unavailable")

    events: list[str] = []

    def create_storage(*_args: object, **_kwargs: object) -> MagicMock:
        events.append("history")
        return storage

    async def prepare_payload(_request: ResponseRequest) -> ResponseRequest:
        events.append("prepare")
        message = "payload preparation reached"
        raise RuntimeError(message)

    request = replace(
        _plain_request(target, source_event_id="$source"),
        payload_preparation=MagicMock(),
        sync_restart_retry_source_event_id="$source",
        on_lifecycle_lock_acquired=lambda: events.append("lock"),
    )
    with (
        patch.object(runner.deps.state_writer, "create_storage", side_effect=create_storage),
        patch.object(runner.deps.request_preparer, "prepare", new=AsyncMock(side_effect=prepare_payload)),
    ):
        response = (
            runner.generate_team_response_helper(request, team_agents=[bot.matrix_id], team_mode="coordinate")
            if is_team
            else runner.generate_response(request)
        )
        lifecycle = runner._lifecycle_coordinator
        lock = lifecycle._response_lifecycle_lock(target)
        queued_signal = lifecycle._get_or_create_queued_signal(target)
        await lock.acquire()
        queued_signal.begin_response_turn()
        task = asyncio.create_task(response)
        try:
            await asyncio.sleep(0)
            assert queued_signal.pending_human_messages == 0
        finally:
            lock.release()
            queued_signal.finish_response_turn()
        if history_case == "current":
            with pytest.raises(PostLockRequestPreparationError):
                await task
        else:
            assert await task is None

    assert events == (["history", "lock", "history", "prepare"] if history_case == "current" else ["history"])
    if history_case == "current":
        bot.client.room_send.assert_awaited_once()
    else:
        bot.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_team_resolution_fallback_obeys_locked_retry_guard(tmp_path: Path, *, interrupted: bool) -> None:
    """Only a still-interrupted team edit retry may deliver its availability reason."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(reply_to_event_id="$source")
    execution_identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost")
    history_scope = runner.deps.state_writer.team_history_scope(
        [bot.matrix_id],
        requester_user_id=execution_identity.requester_id,
    )
    storage = MagicMock()
    storage.get_session.return_value = TeamSession(
        session_id=target.session_id,
        team_id=history_scope.scope_id,
        runs=[_stored_run(history_scope, "run", interrupted=interrupted)],
    )
    request = replace(
        _plain_request(target, source_event_id="$source"),
        existing_event_id="$existing",
        sync_restart_retry_source_event_id="$source",
    )

    edit_message = AsyncMock(return_value=delivered_matrix_event("$edit"))
    with (
        patch.object(runner.deps.state_writer, "create_storage", return_value=storage),
        patch("mindroom.delivery_gateway.edit_message_result", new=edit_message),
    ):
        response = await runner.generate_team_response_helper(
            request,
            team_agents=[bot.matrix_id],
            team_mode="coordinate",
            resolution_reason="No team available",
        )

    assert response == ("$existing" if interrupted else None)
    assert edit_message.await_count == int(interrupted)


@pytest.mark.asyncio
async def test_team_resolution_fallback_sync_restart_registers_retry(tmp_path: Path) -> None:
    """Cancellation while editing a fallback reason must retain the edit for retry."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(reply_to_event_id="$source")
    execution_identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost")
    history_scope = runner.deps.state_writer.team_history_scope(
        [bot.matrix_id],
        requester_user_id=execution_identity.requester_id,
    )
    storage = MagicMock()
    storage.get_session.return_value = TeamSession(
        session_id=target.session_id,
        team_id=history_scope.scope_id,
        runs=[_stored_run(history_scope, "run", interrupted=True)],
    )
    retries: list[str] = []
    request = replace(
        _plain_request(target, source_event_id="$source"),
        existing_event_id="$existing",
        sync_restart_retry_source_event_id="$source",
        on_sync_restart_cancelled=lambda: retries.append("retry"),
    )
    edit_message = AsyncMock(
        side_effect=[asyncio.CancelledError("sync_restart"), delivered_matrix_event("$cancelled")],
    )

    with (
        patch.object(runner.deps.state_writer, "create_storage", return_value=storage),
        patch("mindroom.delivery_gateway.edit_message_result", new=edit_message),
    ):
        response = await runner.generate_team_response_helper(
            request,
            team_agents=[bot.matrix_id],
            team_mode="coordinate",
            resolution_reason="No team available",
        )

    assert response == "$existing"
    assert retries == ["retry"]
    assert edit_message.await_count == 1


def _request(on_sync_restart_cancelled: Callable[[], None] | None = None) -> ResponseRequest:
    return ResponseRequest(
        thread_history=[],
        prompt="Hello",
        response_envelope=request_envelope(),
        on_sync_restart_cancelled=on_sync_restart_cancelled,
    )


def _cancelled_outcome(*, failure_reason: str, visible: bool = True) -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome(
        terminal_status="cancelled",
        event_id="$interrupted_note" if visible else None,
        is_visible_response=visible,
        failure_reason=failure_reason,
    )


def _notify(
    runner: ResponseRunner,
    request: ResponseRequest,
    outcome: FinalDeliveryOutcome,
) -> None:
    runner._notify_sync_restart_cancelled(request, outcome)


def test_notify_fires_for_marked_handled_sync_restart_cancellation() -> None:
    """A sync-restart cancellation that left a visible note must report itself."""
    calls: list[str] = []
    _notify(
        ResponseRunner(deps=MagicMock()),
        _request(on_sync_restart_cancelled=lambda: calls.append("retry")),
        _cancelled_outcome(failure_reason="sync_restart_cancelled"),
    )
    assert calls == ["retry"]


def test_notify_ignores_user_stop_and_unmarked_turns() -> None:
    """User stops and turns without a visible note must not request a retry."""
    calls: list[str] = []
    runner = ResponseRunner(deps=MagicMock())
    request = _request(on_sync_restart_cancelled=lambda: calls.append("retry"))

    _notify(runner, request, _cancelled_outcome(failure_reason="cancelled_by_user"))
    _notify(runner, request, _cancelled_outcome(failure_reason="sync_restart_cancelled", visible=False))

    assert calls == []


def test_notify_uses_only_the_canonical_final_delivery_outcome() -> None:
    """Transient cancellation state must not retry a turn whose final outcome completed."""
    calls: list[str] = []
    _notify(
        ResponseRunner(deps=MagicMock()),
        _request(on_sync_restart_cancelled=lambda: calls.append("retry")),
        FinalDeliveryOutcome(
            terminal_status="completed",
            event_id="$response",
            is_visible_response=True,
            failure_reason=None,
        ),
    )
    assert calls == []


@pytest.mark.asyncio
async def test_queue_runs_each_retry_exactly_once() -> None:
    """Flushing must run queued retries once and refuse re-registration."""
    queue = SyncRestartRetryQueue()
    runs: list[str] = []

    async def retry() -> None:
        runs.append("ran")

    assert queue.register("$event", retry, room_id="!room:localhost") is True
    assert queue.register("$event", retry, room_id="!room:localhost") is False
    assert queue.has_pending
    assert queue.pending_room_ids == {"!room:localhost"}

    await queue.flush()
    assert runs == ["ran"]
    assert not queue.has_pending
    assert not queue.pending_room_ids

    # Already-attempted keys never requeue, so a second stall cannot loop.
    assert queue.register("$event", retry, room_id="!room:localhost") is False
    await queue.flush()
    assert runs == ["ran"]


@pytest.mark.asyncio
async def test_concurrent_flushes_share_one_serial_retry_pass() -> None:
    """Overlapping healthy sync callbacks must not run the same retry twice."""
    queue = SyncRestartRetryQueue()
    started = asyncio.Event()
    release = asyncio.Event()
    runs = 0

    async def retry() -> None:
        nonlocal runs
        runs += 1
        started.set()
        await release.wait()

    assert queue.register("$event", retry, room_id="!room:localhost") is True
    first = asyncio.create_task(queue.flush())
    await started.wait()
    second = asyncio.create_task(queue.flush())
    await asyncio.sleep(0)
    assert not second.done()

    release.set()
    await asyncio.gather(first, second)

    assert runs == 1
    assert not queue.has_pending


@pytest.mark.asyncio
async def test_queue_isolates_individual_retry_failures() -> None:
    """One failing retry must not block the others."""
    queue = SyncRestartRetryQueue()
    runs: list[str] = []

    async def failing() -> None:
        msg = "deliberate test error"
        raise RuntimeError(msg)

    async def succeeding() -> None:
        runs.append("ok")

    queue.register("$a", failing, room_id="!a:localhost")
    queue.register("$b", succeeding, room_id="!b:localhost")
    await queue.flush()
    assert runs == ["ok"]


@pytest.mark.asyncio
async def test_queue_flushes_in_registration_order() -> None:
    """Retries must run FIFO so older interrupted turns answer first."""
    queue = SyncRestartRetryQueue()
    runs: list[str] = []

    def make_retry(key: str) -> Callable[[], Awaitable[None]]:
        async def retry() -> None:
            runs.append(key)

        return retry

    for key in ("$first", "$second", "$third"):
        queue.register(key, make_retry(key), room_id="!room:localhost")

    await queue.flush()
    assert runs == ["$first", "$second", "$third"]


@pytest.mark.asyncio
async def test_cancelled_flush_logs_in_flight_key_and_keeps_rest_pending() -> None:
    """Cancelling a flush mid-retry must log the lost key, propagate, and keep later retries queued."""
    queue = SyncRestartRetryQueue()
    started = asyncio.Event()

    async def hanging() -> None:
        started.set()
        await asyncio.Event().wait()

    async def later() -> None:
        pass

    queue.register("$in_flight", hanging, room_id="!in-flight:localhost")
    queue.register("$later", later, room_id="!later:localhost")
    flush_task = asyncio.create_task(queue.flush())
    await started.wait()

    flush_task.cancel()
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await flush_task

    assert [entry["source_event_id"] for entry in logs if entry["event"] == "sync_restart_retry_cancelled"] == [
        "$in_flight",
    ]
    # The interrupted key was already promoted to attempted and never requeues.
    assert queue.register("$in_flight", hanging, room_id="!in-flight:localhost") is False
    # The untouched retry survives for the next healthy sync response.
    assert queue.has_pending


@pytest.mark.asyncio
async def test_config_apply_refusal_requeues_retry_and_keeps_rest_pending() -> None:
    """A refused retry ran nothing, so it must stay queued instead of burning its one attempt."""
    queue = SyncRestartRetryQueue()
    runs: list[str] = []

    gate_closed = True

    async def refused() -> None:
        runs.append("$refused")
        if gate_closed:
            raise ResponseAdmissionRefusedError

    async def later() -> None:
        runs.append("$later")

    queue.register("$refused", refused, room_id="!room:localhost")
    queue.register("$later", later, room_id="!later:localhost")

    with capture_logs() as logs:
        await queue.flush()

    # The pass stops: the gate stays closed for the whole apply, so continuing
    # would only burn every remaining retry against the same closed gate.
    assert runs == ["$refused"]
    assert [
        entry["source_event_id"] for entry in logs if entry["event"] == "sync_restart_retry_deferred_by_config_apply"
    ] == [
        "$refused",
    ]
    # Nothing was attempted, so both keys survive for the next flush.
    assert queue.has_pending
    assert queue.pending_room_ids == frozenset({"!room:localhost", "!later:localhost"})
    # Re-registering is still refused because the key is queued, not because it
    # was burned; the queued callback is the one that runs.
    assert queue.register("$refused", refused, room_id="!room:localhost") is False

    # Once the apply finishes the gate reopens, so the next flush drains both in
    # the original FIFO order.
    gate_closed = False
    runs.clear()
    await queue.flush()
    assert runs == ["$refused", "$later"]
    assert not queue.has_pending


@pytest.mark.asyncio
async def test_config_apply_refusal_at_attempted_bound_keeps_every_key_burned() -> None:
    """A refusal must not evict an older attempted key and let it run a second time.

    _mark_attempted evicts the oldest key once it reaches its bound, and that
    eviction cannot be undone. Promoting a key only after its retry actually ran
    is what keeps the "retried at most once" contract at the bound.
    """
    queue = SyncRestartRetryQueue()

    async def noop() -> None:
        return

    # Fill the attempted ledger exactly to its bound by running real retries.
    for index in range(_MAX_ATTEMPTED_KEYS):
        key = f"$attempted-{index}"
        assert queue.register(key, noop, room_id="!room:localhost") is True
    await queue.flush()

    oldest_key = "$attempted-0"
    assert queue.register(oldest_key, noop, room_id="!room:localhost") is False

    async def refused() -> None:
        raise ResponseAdmissionRefusedError

    queue.register("$refused", refused, room_id="!room:localhost")
    await queue.flush()

    # The refusal promoted nothing, so no older key was evicted to make room and
    # the oldest attempted key is still burned.
    assert queue.register(oldest_key, noop, room_id="!room:localhost") is False
    assert queue.has_pending


@pytest.mark.asyncio
async def test_watchdog_cancelled_response_is_redispatched_once_and_answers() -> None:
    """The dispatch/retry seam answers on the retry and never retries twice."""
    queue = SyncRestartRetryQueue()
    runner = ResponseRunner(deps=MagicMock())
    answers: list[str] = []
    attempts = 0

    async def execute_action() -> None:
        nonlocal attempts
        attempts += 1

        def register_retry() -> None:
            queue.register("$source", execute_action, room_id="!room:localhost")

        if attempts == 1:
            # First attempt: cancelled mid-generation by stall recovery.
            _notify(
                runner,
                _request(on_sync_restart_cancelled=register_retry),
                _cancelled_outcome(failure_reason="sync_restart_cancelled"),
            )
        else:
            answers.append("pong")

    await execute_action()
    assert queue.has_pending
    assert answers == []

    await queue.flush()  # The sync loop reported a healthy response again.
    assert answers == ["pong"]
    assert attempts == 2

    await queue.flush()
    assert attempts == 2


@pytest.mark.asyncio
async def test_user_stopped_response_is_not_retried() -> None:
    """A user stop must leave the retry queue empty."""
    queue = SyncRestartRetryQueue()
    runner = ResponseRunner(deps=MagicMock())

    def register_retry() -> None:
        queue.register("$source", MagicMock(), room_id="!room:localhost")

    _notify(
        runner,
        _request(on_sync_restart_cancelled=register_retry),
        _cancelled_outcome(failure_reason="cancelled_by_user"),
    )

    assert not queue.has_pending
