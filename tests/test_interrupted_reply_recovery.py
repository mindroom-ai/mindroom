"""A replayed turn whose stopped attempt already streamed answers again in place, knowing what that attempt did."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import nio
import pytest

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
from mindroom.hooks import EnrichmentItem
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.message_target import MessageTarget
from mindroom.response_sources import ResponseAttempt, ResponseSources
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE, TEAM_PROGRESS_PLACEHOLDER, unfinished_streamed_reply
from mindroom.tool_system.events import (
    ToolTraceEntry,
    build_tool_trace_content,
    earlier_tool_trace_content,
    tool_trace_from_content,
)
from mindroom.turn_record import TurnRecord
from tests.ai_user_id_helpers import (
    _build_response_runner,
    _config_with_team_matrix_message,
    _install_inert_post_response_effects,
    _make_bot,
    _response_request,
    _runtime_paths,
    _team_orchestrator,
    bind_runtime_paths,
)
from tests.conftest import unwrap_extracted_collaborator
from tests.identity_helpers import fixture_entity_matrix_id
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_response_runner_focused import _admit_approval_source, _preparation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.response_runner import ResponseRequest, ResponseRunner
    from mindroom.response_turn import ResponseTurnContext

ROOM_ID = "!room:localhost"
REPLY_ID = "$reply"
PARTIAL = "🔧 `counter` [1]\n\nHalf of the report"
HOOK_CONTEXT = EnrichmentItem(key="hook", text="From a hook.", persist=False)
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


def test_tool_calls_carried_from_earlier_attempts_come_first() -> None:
    """A reply regenerated after a stop carries the stopped attempts' calls, so its own stop keeps them."""
    content = {**_content(STREAM_STATUS_STREAMING, TRACE[1:]), **earlier_tool_trace_content(TRACE[:1])}

    reply = unfinished_streamed_reply("Thinking...", content)

    assert reply is not None
    assert reply.tool_trace == TRACE


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
) -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage.synthetic(
        event_id=REPLY_ID,
        sender="@mindroom_general:localhost",
        body=body,
        timestamp=2,
        thread_id="$thread",
        content={"body": body, **_content(status, trace)},
    )


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
    request = _plain_request(target, source_event_id="$source")
    return replace(
        request,
        prompt="CRASHTEST write the report",
        payload_preparation=_preparation(target, request.response_envelope),
        sources=sources,
        existing_event_id=REPLY_ID,
        existing_event_is_placeholder=True,
        existing_event_is_recovered=True,
        matrix_run_metadata=bot._turn_store.build_run_metadata(record),
    )


def _hooks_prepare(runner: ResponseRunner) -> AbstractContextManager[AsyncMock]:
    """Prepare the payload as a live turn does, which replaces the transient items with the hooks' own."""
    return patch.object(
        runner.deps.request_preparer,
        "prepare",
        new=AsyncMock(
            side_effect=lambda request: replace(
                request,
                payload_preparation=None,
                transient_enrichment_items=(HOOK_CONTEXT,),
            ),
        ),
    )


async def _replay(
    bot: AgentBot,
    request: ResponseRequest,
    visible: ResolvedVisibleMessage | Exception | None,
) -> tuple[list[ResponseTurnContext], AsyncMock]:
    """Run the replayed turn with the model and the Matrix read replaced at their seams."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    contexts: list[ResponseTurnContext] = []

    async def fake_ai_response(*args: object, **_kwargs: object) -> str:
        contexts.append(cast("ResponseTurnContext", args[0]))
        return "The complete report."

    fetch = AsyncMock(side_effect=visible) if isinstance(visible, Exception) else AsyncMock(return_value=visible)
    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=fetch),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(side_effect=fake_ai_response)),
        _hooks_prepare(runner),
    ):
        await runner.generate_response(request)
    return contexts, fetch


def _attempt_context(context: ResponseTurnContext) -> list[str]:
    return [item.text for item in context.transient_enrichment_items if item.key == "interrupted_attempt"]


@pytest.mark.asyncio
async def test_replay_answers_again_in_place_knowing_what_the_stopped_attempt_did(tmp_path: Path) -> None:
    """The new attempt sees the earlier text and finished tools, then replaces the reply like any answer."""
    bot = _bot(tmp_path)

    (context,), _fetch = await _replay(bot, await _crashed_turn(bot), _streamed())

    assert HOOK_CONTEXT in context.transient_enrichment_items
    (item,) = [item for item in context.transient_enrichment_items if item.key == "interrupted_attempt"]
    assert item.minimal_required
    assert not item.persist
    instruction = item.text
    assert instruction.startswith("Your previous attempt at replying to the current message was interrupted")
    assert "Half of the report\n\n(turn stopped before completion; 1 tool call(s) had finished; " in instruction
    assert 'The `counter` tool finished with input preview "{}" and output preview "1".' in instruction
    assert 'The `report` tool was still running with input preview "{\\"pages\\": 3}"' in instruction
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
    ("trace", "closing_line"),
    [
        (
            (
                *TRACE,
                ToolTraceEntry(type="tool_call_completed", tool_name="counter", args_preview="{}", result_preview="2"),
            ),
            "Already called for the current message, so do not repeat: `counter` (2 calls), `report` (1 call).",
        ),
        ((), None),
    ],
    ids=["calls_listed", "text_only"],
)
async def test_the_new_attempt_is_told_which_calls_not_to_repeat(
    tmp_path: Path,
    trace: tuple[ToolTraceEntry, ...],
    closing_line: str | None,
) -> None:
    """A closing line counts every listed call, because models follow it even when the message asks for the call again."""
    bot = _bot(tmp_path)

    (context,), _fetch = await _replay(bot, await _crashed_turn(bot), _streamed("Half of the report", trace=trace))

    (instruction,) = _attempt_context(context)
    if closing_line is None:
        assert "Already called" not in instruction
    else:
        assert instruction.endswith(closing_line)


@pytest.mark.asyncio
async def test_a_streamed_replay_carries_the_stopped_attempt_too(tmp_path: Path) -> None:
    """The streaming path receives the same context and still delivers through the adopted reply."""
    bot = _bot(tmp_path)
    request = replace(await _crashed_turn(bot), payload_preparation=None)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    contexts: list[ResponseTurnContext] = []

    async def fake_stream(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
        contexts.append(ctx)
        yield "The complete report."

    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=_streamed())),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.stream_agent_response", new=fake_stream),
    ):
        await runner.generate_response(request)

    ((instruction,),) = [_attempt_context(context) for context in contexts]
    assert "The `counter` tool finished" in instruction
    final = await bot.journal_principal().load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert final is not None
    assert final.edits_event_id == REPLY_ID
    answer = cast("dict[str, Any]", final.payload["m.new_content"])
    assert answer["body"] == "The complete report."
    # Every edit of the new attempt carries the stopped attempt's calls, so stopping it too would not lose them.
    assert tool_trace_from_content(answer) == list(TRACE)


@pytest.mark.asyncio
async def test_a_terminal_reply_is_answered_as_before(tmp_path: Path) -> None:
    """A reply that already reached a terminal state hides no stopped work."""
    bot = _bot(tmp_path)
    visible = _streamed(f"Done.\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}", status=STREAM_STATUS_ERROR)

    (context,), _fetch = await _replay(bot, await _crashed_turn(bot), visible)

    assert _attempt_context(context) == []
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

    (context,), _fetch = await _replay(bot, await _crashed_turn(bot), read)

    (instruction,) = _attempt_context(context)
    assert "what that attempt did is unknown" in instruction
    assert not await bot.journal_principal().is_pending("$source")


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_event_id", [None, REPLY_ID], ids=["fresh_reply", "adopted_unrecovered_reply"])
async def test_only_a_recovered_reply_is_read_for_a_stopped_attempt(
    tmp_path: Path,
    existing_event_id: str | None,
) -> None:
    """Only the recovered flag opens the gate: a reply this attempt sends itself, or one adopted without recovery (as an edit regeneration does), is not read."""
    bot = _bot(tmp_path)
    request = replace(
        await _crashed_turn(bot),
        existing_event_id=existing_event_id,
        existing_event_is_placeholder=False,
        existing_event_is_recovered=False,
    )

    (context,), fetch = await _replay(bot, request, _streamed())

    fetch.assert_not_awaited()
    assert _attempt_context(context) == []


@pytest.mark.asyncio
async def test_a_stopped_team_reply_reaches_the_team_turn_without_its_display_chrome(tmp_path: Path) -> None:
    """The team path carries the account too, minus the header and no-consensus note it was displayed with."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team_matrix_message(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    contexts: list[ResponseTurnContext] = []

    async def fake_team_response(*_args: object, **kwargs: object) -> str:
        contexts.append(cast("ResponseTurnContext", kwargs["ctx"]))
        return "Team answer"

    visible = _streamed(
        f"🤝 **Team Response** (General, Helper):\n\n{PARTIAL}\n\n\n*No team consensus - showing individual responses only*",
    )
    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=visible)),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response", new=AsyncMock(side_effect=fake_team_response)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=_team_orchestrator(config, runtime_paths),
        )
        _install_inert_post_response_effects(coordinator)
        await coordinator.generate_team_response_helper(
            replace(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                existing_event_id=REPLY_ID,
                existing_event_is_placeholder=True,
                existing_event_is_recovered=True,
            ),
            team_agents=[fixture_entity_matrix_id("general", "localhost", runtime_paths)],
            team_mode="coordinate",
        )

    ((instruction,),) = [_attempt_context(context) for context in contexts]
    assert "\n\nHalf of the report\n\n(turn stopped before completion" in instruction
    assert "Team Response" not in instruction
    assert "consensus" not in instruction
