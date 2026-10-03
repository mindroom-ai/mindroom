"""A replayed turn whose earlier attempt already streamed is settled as interrupted, not run again."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.agent_storage import get_agent_session, get_team_session
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY, STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING
from mindroom.event_journal import DeliveryStage
from mindroom.history.types import HistoryScope
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.response_sources import ResponseAttempt, ResponseSources
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE
from mindroom.tool_system.events import ToolTraceEntry, build_tool_trace_content
from mindroom.turn_record import TurnRecord
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_approval_interruption_recovery import _recovery_runtime
from tests.test_response_runner_focused import _admit_approval_source

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.response_runner import ResponseRequest, ResponseRunner

ROOM_ID = "!room:localhost"
REPLY_ID = "$waiting"
PARTIAL = "🔧 `counter` [1]\n\nHalf of the report"
TRACE = (ToolTraceEntry(type="tool_call_completed", tool_name="counter", args_preview="{}", result_preview="1"),)


class _GenerationStartedError(Exception):
    """The replay went on to prepare a fresh model run."""


def _streamed(body: str = PARTIAL, *, status: str = STREAM_STATUS_STREAMING) -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage.synthetic(
        event_id=REPLY_ID,
        sender="@mindroom_general:localhost",
        body=body,
        timestamp=2,
        thread_id="$thread",
        content={
            "body": body,
            STREAM_STATUS_KEY: status,
            **(build_tool_trace_content(TRACE) or {}),
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread", "m.in_reply_to": {"event_id": "$source"}},
        },
    )


async def _crashed_turn(bot: AgentBot, *, thread_id: str | None = "$thread") -> ResponseRequest:
    """Leave the durable state a dead process leaves: a pending source and an adopted streamed reply."""
    store = bot.journal_principal()
    target = _target(thread_id=thread_id, reply_to_event_id="$source")
    await _admit_approval_source(store)
    sources = ResponseSources(("$source",), ("$source",))
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id=thread_id,
        payload={"body": "Thinking...", STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
        response_attempt=ResponseAttempt("general", sources),
    )
    await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        event_id=REPLY_ID,
        delivered_projections=(),
    )
    record = TurnRecord.create(
        ("$source",),
        completed=False,
        response_owner="general",
        response_event_id=REPLY_ID,
        requester_id="@user:localhost",
        conversation_target=target,
        history_scope=HistoryScope(kind="agent", scope_id="general"),
    )
    await bot._turn_store.record_pending_turn(record)
    bot.client.room_send.return_value = nio.RoomSendResponse(event_id="$note", room_id=ROOM_ID)
    return replace(
        _plain_request(target, source_event_id="$source"),
        prompt="CRASHTEST write the report",
        sources=sources,
        existing_event_id=REPLY_ID,
        existing_event_is_placeholder=True,
        matrix_run_metadata=bot._turn_store.build_run_metadata(record),
    )


async def _replay(bot: AgentBot, request: ResponseRequest, visible: ResolvedVisibleMessage) -> ResponseRunner:
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=visible)),
        patch.object(runner, "_prepare_admitted_locked_turn", new=AsyncMock(side_effect=_GenerationStartedError)),
    ):
        await runner.generate_response(request)
    return runner


@pytest.mark.asyncio
async def test_replay_settles_streamed_reply_as_restart_interrupted(tmp_path: Path) -> None:
    """The visible note, the model's replay record and the source settlement all come from Matrix."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)

    runner = await _replay(bot, request, _streamed())

    store = bot.journal_principal()
    assert not await store.is_pending("$source")
    final = await store.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert final is not None
    assert final.acknowledged_event_id == "$note"
    assert final.edits_event_id == REPLY_ID
    note = final.payload["m.new_content"]
    assert note["body"] == f"{PARTIAL}\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}"
    assert note[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
    assert note["io.mindroom.tool_trace"] == build_tool_trace_content(TRACE)["io.mindroom.tool_trace"]
    assert bot._turn_store.get_turn_record("$source").completed

    target = request.response_envelope.target
    storage = runner.deps.state_writer.create_storage(
        runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost"),
    )
    try:
        session = get_agent_session(storage, target.session_id)
    finally:
        storage.close()
    assert session is not None
    (run,) = session.runs
    assert run.metadata["mindroom_replay_state"] == "interrupted"
    assert run.metadata["matrix_response_event_id"] == REPLY_ID
    user, assistant = run.messages
    assert "CRASHTEST write the report" in user.content
    assert assistant.content.startswith(
        "Half of the report\n\n(turn stopped before completion; 1 tool call(s) had finished)",
    )
    assert 'The `counter` tool finished with input preview "{}" and output preview "1".' in assistant.content

    assert bot.pending_sync_restart_retry_room_ids == {ROOM_ID}
    async with bot.response_recovery_scope(ROOM_ID, REPLY_ID) as permitted:
        assert permitted


@pytest.mark.asyncio
async def test_settled_interruption_resumes_through_restart_recovery(tmp_path: Path) -> None:
    """The settled reply is resumed exactly once by the same router relay an orderly restart uses."""
    bot = _bot(tmp_path)
    await _replay(bot, await _crashed_turn(bot), _streamed())
    note = f"{PARTIAL}\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}"

    with _recovery_runtime(bot, tmp_path, note) as (fleet, replacement, client):
        fleet._capture_replacement_recovery_rooms({"general": bot})
        await fleet._recover_pending_replacement_rooms(fleet.config)
        assert client.room_send.await_count == 1
        relay = client.room_send.await_args.kwargs["content"]
        assert relay["m.relates_to"]["m.in_reply_to"]["event_id"] == REPLY_ID
        await fleet._recover_stale_streams_after_restart([replacement], fleet.config, None, set())
        assert client.room_send.await_count == 1


@pytest.mark.asyncio
async def test_settled_interruption_keeps_only_the_note_when_auto_resume_is_off(tmp_path: Path) -> None:
    """``auto_resume_after_restart: false`` suppresses the relay, as it does after an orderly restart."""
    bot = _bot(tmp_path)
    await _replay(bot, await _crashed_turn(bot), _streamed())
    note = f"{PARTIAL}\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}"

    with _recovery_runtime(bot, tmp_path, note, policy="disabled") as (fleet, _replacement, client):
        fleet._capture_replacement_recovery_rooms({"general": bot})
        await fleet._recover_pending_replacement_rooms(fleet.config)
        client.room_send.assert_not_awaited()
    final = await bot.journal_principal().load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert final is not None
    assert final.payload["m.new_content"]["body"] == note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "visible",
    [
        _streamed("Thinking...", status=STREAM_STATUS_PENDING),
        _streamed(f"Done.\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}", status=STREAM_STATUS_ERROR),
        None,
    ],
    ids=["placeholder_only", "already_terminal", "unreadable"],
)
async def test_replay_without_unfinished_visible_work_runs_the_turn_again(
    tmp_path: Path,
    visible: ResolvedVisibleMessage | None,
) -> None:
    """Without visible work to keep, the turn replays from the start as before."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)

    with pytest.raises(_GenerationStartedError):
        await _replay(bot, request, visible)

    store = bot.journal_principal()
    assert await store.is_pending("$source")
    assert await store.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is None
    assert not bot.pending_sync_restart_retry_room_ids


@pytest.mark.asyncio
async def test_regenerating_an_existing_answer_is_never_settled_as_interrupted(tmp_path: Path) -> None:
    """Only an adopted placeholder can hold a dead attempt's stream; an edited answer re-drives."""
    bot = _bot(tmp_path)
    request = replace(await _crashed_turn(bot), existing_event_is_placeholder=False)

    with pytest.raises(_GenerationStartedError):
        await _replay(bot, request, _streamed())

    assert await bot.journal_principal().is_pending("$source")


@pytest.mark.asyncio
async def test_room_level_reply_is_settled_without_a_resume_request(tmp_path: Path) -> None:
    """Auto-resume only continues threads, so a room-level reply keeps just the note and record."""
    bot = _bot(tmp_path)

    await _replay(bot, await _crashed_turn(bot, thread_id=None), _streamed())

    assert not await bot.journal_principal().is_pending("$source")
    assert not bot.pending_sync_restart_retry_room_ids


@pytest.mark.asyncio
async def test_team_reply_is_recorded_in_its_team_history(tmp_path: Path) -> None:
    """A team's interrupted record lands in the team scope its next turn reads."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = request.response_envelope.target
    identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost")
    scope = HistoryScope(kind="team", scope_id="team_general_helper")

    with patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=_streamed())):
        assert await runner._settle_unfinished_streamed_reply(
            request,
            resolved_target=target,
            history_scope=scope,
            execution_identity=identity,
        )

    storage = runner.deps.state_writer.create_storage(identity, scope=scope)
    try:
        session = get_team_session(storage, target.session_id)
    finally:
        storage.close()
    assert session is not None
    (run,) = session.runs
    assert run.team_id == "team_general_helper"
    assert run.metadata["mindroom_replay_state"] == "interrupted"
    assert not await bot.journal_principal().is_pending("$source")


@pytest.mark.asyncio
async def test_failed_replay_record_leaves_the_turn_pending(tmp_path: Path) -> None:
    """Without its replay record the note would let tools run twice, so the turn stays owed."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)

    with (
        patch.object(runner, "_persist_interrupted_turn", side_effect=RuntimeError("database is locked")),
        pytest.raises(RuntimeError, match="database is locked"),
    ):
        await _replay(bot, request, _streamed())

    store = bot.journal_principal()
    assert await store.is_pending("$source")
    assert await store.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is None
    assert not bot.pending_sync_restart_retry_room_ids
