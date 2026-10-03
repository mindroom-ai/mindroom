"""A replayed turn whose stopped attempt already streamed answers again in place, knowing what that attempt did."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.agent_storage import get_agent_session
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
    from pathlib import Path

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
    status: str = STREAM_STATUS_STREAMING,
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
        matrix_run_metadata=bot._turn_store.build_run_metadata(record),
    )


async def _replay(
    bot: AgentBot,
    request: ResponseRequest,
    visible: ResolvedVisibleMessage | None,
) -> tuple[list[ResponseTurnContext], AsyncMock]:
    """Run the replayed turn with the model and Matrix reads replaced at their seams."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    contexts: list[ResponseTurnContext] = []

    async def fake_ai_response(*args: object, **_kwargs: object) -> str:
        contexts.append(cast("ResponseTurnContext", args[0]))
        return "The complete report."

    fetch = AsyncMock(return_value=visible)
    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=fetch),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(side_effect=fake_ai_response)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        await runner.generate_response(request)
    return contexts, fetch


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

    (context,), _fetch = await _replay(bot, request, _streamed())

    (instruction,) = _attempt_context(context)
    assert instruction.startswith("A service restart stopped your previous attempt at replying to the current message")
    (attempt,) = _recorded_attempts(bot, request)
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
@pytest.mark.parametrize(
    "visible",
    [
        _streamed("Thinking...", status=STREAM_STATUS_PENDING),
        _streamed(f"Done.\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}", status=STREAM_STATUS_ERROR),
        None,
    ],
    ids=["placeholder_only", "already_terminal", "unreadable"],
)
async def test_replay_without_unfinished_visible_work_answers_as_before(
    tmp_path: Path,
    visible: ResolvedVisibleMessage | None,
) -> None:
    """Without visible work to carry forward the replay is an ordinary answer."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)

    (context,), _fetch = await _replay(bot, request, visible)

    assert _attempt_context(context) == []
    assert _recorded_attempts(bot, request) == []
    assert not await bot.journal_principal().is_pending("$source")


@pytest.mark.asyncio
async def test_regenerating_an_existing_answer_never_reads_a_stopped_attempt(tmp_path: Path) -> None:
    """Only an adopted placeholder can hold a stopped attempt's stream; an edited answer re-drives."""
    bot = _bot(tmp_path)
    request = replace(await _crashed_turn(bot), existing_event_is_placeholder=False)

    (context,), fetch = await _replay(bot, request, _streamed())

    fetch.assert_not_awaited()
    assert _attempt_context(context) == []


@pytest.mark.asyncio
async def test_each_stopped_attempt_keeps_its_own_record(tmp_path: Path) -> None:
    """A second stop before the new attempt shows the old tools must not erase their record."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = request.response_envelope.target
    identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id="@user:localhost")
    second = _streamed("A new start", trace=(), latest_edit="$edit-b")

    for visible in (_streamed(), _streamed(), second):
        with patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=visible)):
            await runner._with_interrupted_attempt(
                request,
                resolved_target=target,
                history_scope=HistoryScope(kind="agent", scope_id="general"),
                execution_identity=identity,
            )

    first_attempt, second_attempt = _recorded_attempts(bot, request)
    assert "The `counter` tool finished" in first_attempt
    assert second_attempt.startswith("A new start")


@pytest.mark.asyncio
async def test_failed_attempt_record_leaves_the_turn_pending(tmp_path: Path) -> None:
    """Answering without the record could repeat finished tools, so the turn stays owed instead."""
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
