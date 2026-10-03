"""A replayed turn whose stopped attempt already streamed answers again in place, knowing what that attempt did."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import nio
import pytest
from agno.session.summary import SessionSummary

from mindroom.agent_storage import get_agent_session, get_team_session
from mindroom.constants import (
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.event_journal import DeliveryStage
from mindroom.history.storage import archive_compaction_chunk, reconcile_compaction_state
from mindroom.history.types import HistoryScope
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.response_sources import ResponseAttempt, ResponseSources
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE, TEAM_PROGRESS_PLACEHOLDER, unfinished_streamed_reply
from mindroom.tool_system.events import ToolTraceEntry, build_tool_trace_content
from mindroom.turn_record import TurnRecord
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_response_runner_focused import _admit_approval_source

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput

    from mindroom.bot import AgentBot
    from mindroom.response_runner import ResponseRequest
    from mindroom.response_turn import ResponseTurnContext

ROOM_ID = "!room:localhost"
REPLY_ID = "$reply"
PARTIAL = "🔧 `counter` [1]\n\nHalf of the report"
TRACE = (
    ToolTraceEntry(type="tool_call_completed", tool_name="counter", args_preview="{}", result_preview="1"),
    ToolTraceEntry(type="tool_call_started", tool_name="report", args_preview='{"pages": 3}'),
)


def _content(status: str | None, trace: tuple[ToolTraceEntry, ...] = TRACE) -> dict[str, object]:
    content: dict[str, object] = dict(build_tool_trace_content(trace) or {})
    if status is not None:
        content[STREAM_STATUS_KEY] = status
    return content


@pytest.mark.parametrize("status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
def test_unfinished_streamed_reply_keeps_text_and_trace(status: str) -> None:
    """Visible prose loses its display-only tool markers; the structured trace is kept whole."""
    reply = unfinished_streamed_reply(PARTIAL, _content(status))

    assert reply is not None
    assert reply.partial_text == "Half of the report"
    assert reply.tool_trace == TRACE


def test_a_tool_trace_without_text_is_still_unfinished_work() -> None:
    """A tool that ran before any prose is visible work the next attempt must know about."""
    reply = unfinished_streamed_reply("🔧 `counter` [1]", _content(STREAM_STATUS_STREAMING, TRACE[:1]))

    assert reply is not None
    assert reply.partial_text == ""
    assert reply.tool_trace == TRACE[:1]


@pytest.mark.parametrize("body", ["Thinking...", TEAM_PROGRESS_PLACEHOLDER, "   "])
@pytest.mark.parametrize("status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
def test_a_bare_placeholder_left_nothing(body: str, status: str) -> None:
    """Nothing visible ran behind a placeholder, so there is nothing to carry forward."""
    assert unfinished_streamed_reply(body, _content(status, ())) is None


@pytest.mark.parametrize("body", ["Thinking...", TEAM_PROGRESS_PLACEHOLDER])
def test_a_trace_beside_placeholder_text_is_still_carried(body: str) -> None:
    """Placeholder text says nothing, but a tool trace beside it still ran."""
    reply = unfinished_streamed_reply(body, _content(STREAM_STATUS_STREAMING))

    assert reply is not None
    assert reply.partial_text == ""
    assert reply.tool_trace == TRACE


@pytest.mark.parametrize(
    "status",
    [
        None,
        STREAM_STATUS_APPROVAL_PENDING,
        STREAM_STATUS_CANCELLED,
        STREAM_STATUS_COMPLETED,
        STREAM_STATUS_ERROR,
        STREAM_STATUS_INTERRUPTED,
    ],
)
def test_only_in_progress_streams_are_unfinished(status: str | None) -> None:
    """Terminal, approval-owned and non-stream messages already have their own owners."""
    assert unfinished_streamed_reply(PARTIAL, _content(status)) is None


def _streamed(
    body: str = PARTIAL,
    *,
    status: str | None = STREAM_STATUS_STREAMING,
    trace: tuple[ToolTraceEntry, ...] = TRACE,
    latest_edit: str = "$edit-a",
) -> ResolvedVisibleMessage:
    message = ResolvedVisibleMessage.synthetic(
        event_id=REPLY_ID,
        sender="@mindroom_general:localhost",
        body=body,
        timestamp=2,
        thread_id="$thread",
        content={"body": body, **_content(status, trace)},
    )
    message.latest_event_id = latest_edit
    return message


async def _crashed_turn(bot: AgentBot) -> ResponseRequest:
    """Leave the durable state a stopped process leaves: a pending source and an adopted streamed reply."""
    store = bot.journal_principal()
    target = _target(thread_id="$thread", reply_to_event_id="$source")
    await _admit_approval_source(store)
    sources = ResponseSources(("$source",), ("$source",))
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
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
    bot.client.room_send.return_value = nio.RoomSendResponse(event_id="$answer-edit", room_id=ROOM_ID)
    return replace(
        _plain_request(target, source_event_id="$source"),
        prompt="CRASHTEST write the report",
        sources=sources,
        existing_event_id=REPLY_ID,
        existing_event_is_placeholder=True,
        existing_event_is_recovered=True,
        matrix_run_metadata=bot._turn_store.build_run_metadata(record),
    )


async def _replay(
    bot: AgentBot,
    request: ResponseRequest,
    visible: ResolvedVisibleMessage | Exception | None,
) -> tuple[list[tuple[ResponseTurnContext, list[str]]], AsyncMock]:
    """Run the replayed turn, observing each model call's context and the history it could read."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    calls: list[tuple[ResponseTurnContext, list[str]]] = []

    async def fake_ai_response(*args: object, **_kwargs: object) -> str:
        calls.append((cast("ResponseTurnContext", args[0]), _recorded_attempts(bot, request)))
        return "The complete report."

    fetch = AsyncMock(side_effect=visible) if isinstance(visible, Exception) else AsyncMock(return_value=visible)
    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=fetch),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(side_effect=fake_ai_response)),
    ):
        await runner.generate_response(request)
    return calls, fetch


def _attempt_context(context: ResponseTurnContext) -> list[str]:
    return [item.text for item in context.transient_enrichment_items if item.key == "interrupted_attempt"]


def _recorded_attempts(bot: AgentBot, request: ResponseRequest) -> list[str]:
    """Return the assistant text of every stopped-attempt record in the turn's agent history."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = request.response_envelope.target
    storage = runner.deps.state_writer.create_storage(
        runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost"),
    )
    try:
        session = get_agent_session(storage, target.session_id)
    finally:
        storage.close()
    runs = [] if session is None else session.runs or []
    return [
        cast("str", run.content)
        for run in runs
        # The faked model never records completion, so its own turn ends in a failed record.
        if isinstance(run.metadata, dict) and run.metadata.get("mindroom_original_status") == "cancelled"
    ]


@pytest.mark.asyncio
async def test_replay_answers_again_in_place_knowing_what_the_stopped_attempt_did(tmp_path: Path) -> None:
    """The new attempt sees the earlier text and finished tools, then replaces the reply like any answer."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)

    ((context, history),), _fetch = await _replay(bot, request, _streamed())

    (instruction,) = _attempt_context(context)
    assert instruction.startswith("Your previous attempt at replying to the current message was interrupted")
    # The account rides in the instruction too, so no history window can drop it.
    assert 'The `counter` tool finished with input preview "{}" and output preview "1".' in instruction
    (attempt,) = history
    assert attempt.startswith("Half of the report\n\n(turn stopped before completion; 1 tool call(s) had finished; ")
    assert 'The `counter` tool finished with input preview "{}" and output preview "1".' in attempt
    assert 'The `report` tool was still running with input preview "{\\"pages\\": 3}"' in attempt
    store = bot.journal_principal()
    final = await store.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert final is not None
    assert final.edits_event_id == REPLY_ID
    answer = cast("dict[str, Any]", final.payload["m.new_content"])
    assert answer["body"] == "The complete report."
    assert RESTART_INTERRUPTED_RESPONSE_NOTE not in answer["body"]
    assert not await store.is_pending("$source")
    assert not bot.pending_sync_restart_retry_room_ids


@pytest.mark.asyncio
async def test_a_streamed_replay_reads_the_record_and_instruction_too(tmp_path: Path) -> None:
    """The streaming path receives the same context and still delivers through the adopted reply."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    calls: list[tuple[ResponseTurnContext, list[str]]] = []

    async def fake_stream(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
        calls.append((ctx, _recorded_attempts(bot, request)))
        yield "The complete report."

    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=_streamed())),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.stream_agent_response", new=fake_stream),
    ):
        await runner.generate_response(request)

    ((context, history),) = calls
    assert len(_attempt_context(context)) == 1
    (attempt,) = history
    assert "The `counter` tool finished" in attempt
    final = await bot.journal_principal().load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert final is not None
    assert final.edits_event_id == REPLY_ID
    assert cast("dict[str, Any]", final.payload["m.new_content"])["body"] == "The complete report."


@pytest.mark.asyncio
async def test_a_terminal_reply_is_answered_as_before(tmp_path: Path) -> None:
    """A reply that already reached a terminal state hides no stopped work."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    visible = _streamed(f"Done.\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}", status=STREAM_STATUS_ERROR)

    ((context, _history),), _fetch = await _replay(bot, request, visible)

    assert _attempt_context(context) == []
    assert _recorded_attempts(bot, request) == []
    assert not await bot.journal_principal().is_pending("$source")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        None,
        nio.EncryptionError("missing session key"),
        nio.exceptions.RemoteProtocolError("relations page failed"),
        _streamed("Thinking...", status=STREAM_STATUS_PENDING, trace=()),
        _streamed("You selected: 1 Yes\n\nProcessing your response...", status=None, trace=()),
    ],
    ids=["unreadable", "undecryptable", "unlisted_edits", "nothing_shown", "selection_acknowledgement"],
)
async def test_a_stopped_attempt_with_unknown_work_still_warns_the_new_attempt(
    tmp_path: Path,
    read: ResolvedVisibleMessage | Exception | None,
) -> None:
    """Unknown is not nothing: the turn is answered, warned that side effects may already have happened."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)

    ((context, _history),), _fetch = await _replay(bot, request, read)

    (instruction,) = _attempt_context(context)
    assert "what that attempt did is unknown" in instruction
    assert _recorded_attempts(bot, request) == []
    assert not await bot.journal_principal().is_pending("$source")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("placeholder", "recovered"),
    [(False, True), (True, False)],
    ids=["edit_regeneration", "fresh_placeholder"],
)
async def test_only_a_recovered_placeholder_is_read_for_a_stopped_attempt(
    tmp_path: Path,
    placeholder: bool,
    recovered: bool,
) -> None:
    """An edited answer re-drives, and a placeholder this attempt just sent has no earlier attempt behind it."""
    bot = _bot(tmp_path)
    request = replace(
        await _crashed_turn(bot),
        existing_event_is_placeholder=placeholder,
        existing_event_is_recovered=recovered,
    )

    ((context, _history),), fetch = await _replay(bot, request, _streamed())

    fetch.assert_not_awaited()
    assert _attempt_context(context) == []


@pytest.mark.asyncio
async def test_every_stopped_attempt_folds_into_one_record(tmp_path: Path) -> None:
    """A second stop before the new attempt shows the old tools keeps them, in one latest run."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = request.response_envelope.target
    identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost")
    second = _streamed("A new start", trace=(), latest_edit="$edit-b")
    instructions: list[str] = []

    for visible in (_streamed(), _streamed(), second, second):
        with patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=visible)):
            answered = await runner._with_interrupted_attempt(
                request,
                resolved_target=target,
                history_scope=HistoryScope(kind="agent", scope_id="general"),
                execution_identity=identity,
            )
        (instruction,) = [
            item.text for item in answered.transient_enrichment_items if item.key == "interrupted_attempt"
        ]
        instructions.append(instruction)

    # Rereading a recorded attempt means the attempt after it left no edit, so its work is unknown.
    assert ["is unknown" in instruction for instruction in instructions] == [False, True, False, True]
    assert all("The `counter` tool finished" in instruction for instruction in instructions)
    (record,) = _recorded_attempts(bot, request)
    first_account, second_account = record.split("\n\nA new start")
    assert first_account.startswith("Half of the report")
    assert "The `counter` tool finished" in first_account
    assert second_account == "\n\n(turn stopped before completion)"


@pytest.mark.asyncio
async def test_compaction_archived_attempts_are_never_resurrected(tmp_path: Path) -> None:
    """Rereading an attempt compaction archived, even one folded into an older record, is not new."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = request.response_envelope.target
    identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost")
    scope = HistoryScope(kind="agent", scope_id="general")

    async def fold(visible: ResolvedVisibleMessage) -> str:
        with patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=visible)):
            answered = await runner._with_interrupted_attempt(
                request,
                resolved_target=target,
                history_scope=scope,
                execution_identity=identity,
            )
        (instruction,) = [
            item.text for item in answered.transient_enrichment_items if item.key == "interrupted_attempt"
        ]
        return instruction

    def live_runs() -> list[RunOutput | TeamRunOutput]:
        storage = runner.deps.state_writer.create_storage(identity)
        try:
            session = get_agent_session(storage, target.session_id)
            assert session is not None
            reconcile_compaction_state(storage, session, scope)
            return list(session.runs or [])
        finally:
            storage.close()

    second = _streamed("A second try", trace=(), latest_edit="$edit-b")
    await fold(_streamed())
    await fold(second)
    storage = runner.deps.state_writer.create_storage(identity)
    try:
        session = get_agent_session(storage, target.session_id)
        assert session is not None
        archive_compaction_chunk(
            storage=storage,
            session=session,
            scope=scope,
            summary=SessionSummary(summary="The counter tool ran once."),
            summary_model="test-model",
            archived_runs=list(session.runs or []),
        )
    finally:
        storage.close()

    reread = await fold(second)
    assert "is unknown" in reread
    assert "earlier interrupted attempts" not in reread
    assert live_runs() == []
    assert "is unknown" not in await fold(_streamed("A new start", trace=(), latest_edit="$edit-c"))
    (record,) = live_runs()
    assert cast("str", record.content).startswith("A new start")


@pytest.mark.asyncio
async def test_failed_attempt_record_leaves_the_turn_pending(tmp_path: Path) -> None:
    """Answering without the record could repeat finished tools, so the turn stays owed instead."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)

    with (
        patch.object(runner, "_persist_stopped_attempt", side_effect=RuntimeError("database is locked")),
        pytest.raises(RuntimeError, match="database is locked"),
    ):
        await _replay(bot, request, _streamed())

    store = bot.journal_principal()
    assert await store.is_pending("$source")
    assert await store.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is None


@pytest.mark.asyncio
async def test_a_team_attempt_is_recorded_in_its_team_history(tmp_path: Path) -> None:
    """A team scope's stopped attempt is recorded in that scope, without its display chrome."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = request.response_envelope.target
    identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost")
    scope = HistoryScope(kind="team", scope_id="team_general_helper")

    visible = _streamed(
        f"🤝 **Team Response** (General, Helper):\n\n{PARTIAL}\n\n\n*No team consensus - showing individual responses only*",
    )
    with patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=visible)):
        await runner._with_interrupted_attempt(
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
    assert cast("str", run.content).startswith("Half of the report\n\n(turn stopped before completion")
    assert "consensus" not in cast("str", run.content)
    assert "The `counter` tool finished" in cast("str", run.content)
