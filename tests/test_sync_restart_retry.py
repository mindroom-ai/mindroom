"""Retry guard for edit regenerations of an interrupted replay."""

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

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY
from mindroom.history.types import HistoryScope
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest
from mindroom.sync_restart_retry import interrupted_source_needs_retry
from mindroom.turn_record import TurnRecord
from tests.conftest import delivered_matrix_event, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target

if TYPE_CHECKING:
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
        # The placeholder went out, and the reply's records edit the dispatch error into it.
        assert bot.client.room_send.await_count == 2
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
    # A sync-restart retry is an edit regeneration of the answer its turn names.
    request = replace(
        _plain_request(target, source_event_id="$source"),
        existing_event_id="$existing",
        prepared_edit_record=TurnRecord.create(["$source"], response_event_id="$existing"),
        sync_restart_retry_source_event_id="$source",
    )

    edit_message = AsyncMock(return_value=delivered_matrix_event("$edit"))
    with (
        patch.object(runner.deps.state_writer, "create_storage", return_value=storage),
        patch("mindroom.delivery_gateway.send_message_outcome", new=edit_message),
    ):
        response = await runner.generate_team_response_helper(
            request,
            team_agents=[bot.matrix_id],
            team_mode="coordinate",
            resolution_reason="No team available",
        )

    assert response == ("$existing" if interrupted else None)
    assert edit_message.await_count == int(interrupted)
