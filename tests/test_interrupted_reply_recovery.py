"""A replayed turn whose stopped attempt already streamed answers again in place, knowing what that attempt did."""

from __future__ import annotations

import contextlib
import html
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.constants import (
    AI_RUN_METADATA_KEY,
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
    _set_gateway_method,
    _team_orchestrator,
    bind_runtime_paths,
)
from tests.bot_helpers import _stream_outcome
from tests.conftest import unwrap_extracted_collaborator
from tests.identity_helpers import fixture_entity_matrix_id
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_response_runner_focused import _admit_approval_source, _preparation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.delivery_gateway import StreamingDeliveryRequest
    from mindroom.final_delivery import StreamTransportOutcome
    from mindroom.history.turn_recorder import TurnRecorder
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
    assert reply.visible_text == PARTIAL
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
        STREAM_STATUS_CANCELLED,
        STREAM_STATUS_COMPLETED,
        STREAM_STATUS_ERROR,
        STREAM_STATUS_INTERRUPTED,
    ],
)
def test_only_in_progress_streams_are_unfinished(status: str | None) -> None:
    """Terminal and non-stream messages show no stopped work."""
    assert unfinished_streamed_reply(PARTIAL, _content(status)) is None


def test_a_reply_still_waiting_on_its_approval_is_unfinished() -> None:
    """A restart can stop an approved run before its first edit, so the reply still shows the pause."""
    reply = unfinished_streamed_reply(PARTIAL, _content(STREAM_STATUS_APPROVAL_PENDING))

    assert reply is not None
    assert reply.visible_text == PARTIAL
    assert reply.tool_trace == TRACE


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


@dataclass(frozen=True)
class _ModelCall:
    """One model call of the replayed turn, with the prompt the model saw."""

    context: ResponseTurnContext
    model_prompt: str
    streamed: bool

    @property
    def account(self) -> str | None:
        """Return the stopped attempt's account saved in the prompt, unescaped."""
        prompt = html.unescape(self.model_prompt)
        opening = '<item key="interrupted_attempt" cache_policy="volatile">\n'
        if opening not in prompt:
            return None
        return prompt.split(opening, 1)[1].split("\n</item>", 1)[0]


async def _replay(
    bot: AgentBot,
    request: ResponseRequest,
    visible: ResolvedVisibleMessage | Exception | None,
) -> tuple[list[_ModelCall], AsyncMock]:
    """Run the replayed turn with the model and the Matrix read replaced at their seams; presence says offline."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    calls: list[_ModelCall] = []

    async def fake_ai_response(*args: object, **kwargs: object) -> str:
        calls.append(_ModelCall(cast("ResponseTurnContext", args[0]), cast("str", kwargs["model_prompt"]), False))
        return "The complete report."

    async def fake_stream(*args: object, **kwargs: object) -> AsyncIterator[str]:
        calls.append(_ModelCall(cast("ResponseTurnContext", args[0]), cast("str", kwargs["model_prompt"]), True))
        yield "The complete report."

    fetch = AsyncMock(side_effect=visible) if isinstance(visible, Exception) else AsyncMock(return_value=visible)
    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=fetch),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(side_effect=fake_ai_response)),
        patch("mindroom.response_runner.stream_agent_response", new=fake_stream),
        _hooks_prepare(runner),
    ):
        await runner.generate_response(request)
    return calls, fetch


async def _final_answer(bot: AgentBot) -> dict[str, Any]:
    final = await bot.journal_principal().load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert final is not None
    assert final.edits_event_id == REPLY_ID
    return cast("dict[str, Any]", final.payload["m.new_content"])


@pytest.mark.asyncio
@pytest.mark.parametrize("adopted_placeholder", [True, False], ids=["replayed_turn", "edit_regeneration"])
async def test_replay_continues_below_the_stopped_attempt_and_saves_its_account(
    tmp_path: Path,
    adopted_placeholder: bool,
) -> None:
    """The stopped text and calls stay above the continuation, which streams even to an offline requester."""
    bot = _bot(tmp_path)
    request = replace(await _crashed_turn(bot), existing_event_is_placeholder=adopted_placeholder)

    (call,), _fetch = await _replay(bot, request, _streamed())

    assert call.streamed
    assert call.model_prompt.startswith("CRASHTEST write the report\n\n<mindroom_message_context>")
    account = call.account
    assert account is not None
    assert account.startswith("Your reply to the current message was interrupted by a restart before it finished.")
    assert "Half of the report\n\n(turn stopped before completion; 1 tool call(s) had finished; " in account
    assert 'The `counter` tool finished with input preview "{}" and output preview "1".' in account
    assert 'The `report` tool was still running with input preview "{\\"pages\\": 3}"' in account
    # The account is saved with the turn, not passed once beside it.
    assert call.context.transient_enrichment_items == (HOOK_CONTEXT,)
    answer = await _final_answer(bot)
    assert answer["body"] == f"{PARTIAL}\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}\n\nThe complete report."
    assert tool_trace_from_content(answer) == list(TRACE)
    sent = [call.kwargs["content"] for call in bot.client.room_send.await_args_list]
    in_progress = [
        content.get("m.new_content", content)
        for content in sent
        if content.get("m.new_content", content).get(STREAM_STATUS_KEY) == STREAM_STATUS_STREAMING
    ]
    assert in_progress
    assert all(content["body"].startswith(PARTIAL) for content in in_progress)
    assert all(tool_trace_from_content(content) == list(TRACE) for content in in_progress)
    assert not await bot.journal_principal().is_pending("$source")


@pytest.mark.asyncio
async def test_a_failure_before_the_continuation_streams_keeps_the_stopped_reply(tmp_path: Path) -> None:
    """A run that fails before it streams leaves the stopped attempt's text in place rather than redacting it."""
    bot = _bot(tmp_path)
    request = await _crashed_turn(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)

    def failing_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        message = "model unavailable"
        raise RuntimeError(message)

    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=_streamed())),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.stream_agent_response", new=failing_stream),
        _hooks_prepare(runner),
        contextlib.suppress(RuntimeError),
    ):
        await runner.generate_response(request)

    bot.client.room_redact.assert_not_awaited()
    final = await bot.journal_principal().load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert final is None


@pytest.mark.asyncio
async def test_a_terminal_reply_is_answered_as_before(tmp_path: Path) -> None:
    """A reply that already reached a terminal state hides no stopped work."""
    bot = _bot(tmp_path)
    visible = _streamed(f"Done.\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}", status=STREAM_STATUS_ERROR)

    (call,), _fetch = await _replay(bot, await _crashed_turn(bot), visible)

    assert call.account is None
    assert not call.streamed
    assert (await _final_answer(bot))["body"] == "The complete report."
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

    (call,), _fetch = await _replay(bot, await _crashed_turn(bot), read)

    assert call.account is not None
    assert "what that attempt did is unknown" in call.account
    assert (await _final_answer(bot))["body"] == "The complete report."
    assert not await bot.journal_principal().is_pending("$source")


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_event_id", [None, REPLY_ID], ids=["fresh_reply", "adopted_unrecovered_reply"])
async def test_only_a_recovered_reply_is_read_for_a_stopped_attempt(
    tmp_path: Path,
    existing_event_id: str | None,
) -> None:
    """Only the recovered flag opens the gate: a reply this attempt sends itself, or one adopted without recovery, is not read."""
    bot = _bot(tmp_path)
    request = replace(
        await _crashed_turn(bot),
        existing_event_id=existing_event_id,
        existing_event_is_placeholder=False,
        existing_event_is_recovered=False,
    )

    (call,), fetch = await _replay(bot, request, _streamed())

    fetch.assert_not_awaited()
    assert call.account is None


@pytest.mark.asyncio
async def test_a_stopped_team_reply_continues_with_its_account_minus_display_chrome(tmp_path: Path) -> None:
    """The team leader gets the account without the team chrome, and the team stream continues below the stopped reply."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_team_matrix_message(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="ultimate")
    messages: list[str] = []

    async def fake_team_stream(**kwargs: object) -> AsyncIterator[str]:
        messages.append(cast("str", kwargs["message"]))
        # Run metadata the turn recorded but never published to the live collector.
        cast("TurnRecorder", kwargs["turn_recorder"]).set_run_metadata({AI_RUN_METADATA_KEY: {"version": 1}})
        yield "Team answer"

    visible = _streamed(
        f"🤝 **Team Response** (General, Helper):\n\n{PARTIAL}\n\n\n*No team consensus - showing individual responses only*",
    )
    with (
        patch("mindroom.response_runner.fetch_latest_visible_message", new=AsyncMock(return_value=visible)),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.team_response_stream", new=fake_team_stream),
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
        delivered: list[StreamingDeliveryRequest] = []

        async def deliver(request: StreamingDeliveryRequest) -> StreamTransportOutcome:
            delivered.append(request)
            body = "".join([str(chunk) async for chunk in request.response_stream])
            return _stream_outcome(REPLY_ID, body)

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = deliver
        finalize = _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(wraps=coordinator.deps.delivery_gateway.finalize_streamed_response),
        )
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

    (message,) = messages
    account = html.unescape(message)
    assert "\n\nHalf of the report\n\n(turn stopped before completion" in account
    assert "Team Response" not in account
    assert "consensus" not in account
    ((stream,),) = [delivered]
    assert stream.resumed == unfinished_streamed_reply(visible.body, visible.content)
    finalized = finalize.await_args.args[0]
    assert finalized.extra_content[AI_RUN_METADATA_KEY] == {"version": 1}
