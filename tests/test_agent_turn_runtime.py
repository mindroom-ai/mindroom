"""Agent turns on the owned loop, through ``ai.stream_agent_response`` and ``ai.ai_response``.

The seam is ``run_agent`` fed by a scripted provider behind a real ``create_agent`` build: what a reply streams,
what it stores, which tool-call policy runs around every call, and how it ends when it is paused, cancelled,
failed, empty, or silent. The last tests drive the same turns through ``ResponseRunner``, the lifecycle owner, under
which the turn driver writes the interrupted-turn record after the loop stored its own terminal record.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.tools import Toolkit
from structlog.testing import capture_logs

from mindroom import agents, ai, ai_runtime, loop_hooks, response_turn
from mindroom.agent_loop.output_limit import CUT_OFF_TOOL_CALL_RESULT
from mindroom.ai import ai_response, stream_agent_response
from mindroom.ai_runtime import queued_message_signal_context
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.budgets.pricing import format_cost_usd
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, ModelConfig, ModelPricing
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.dispatch_source import ScheduledHistoryBudget
from mindroom.entity_resolution import entity_identity_registry
from mindroom.final_delivery import AwaitingApproval, Cancelled, Completed, FinalDeliveryOutcome
from mindroom.history.interrupted_replay import is_interrupted_replay_run
from mindroom.history.run_visibility import (
    QUEUED_MESSAGE_NOTICE_MARKER_KEY,
    is_queued_notice_message,
    queued_notice_marker,
)
from mindroom.history.storage import new_scope_session
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import CompactionOutcome, HistoryScope
from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    MessageEnvelope,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.llm.errors import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelSafeguardRefusal, RateLimited
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message, system_text
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    NativeCompactionReplay,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import MessageMetrics
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.native_compaction import replay_basis
from mindroom.participation import ParticipationGate
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import ResponsePausedForApproval, TurnSinks
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.skill_learning.capture import SkillReviewCapture
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.usage.storage import COMPACTED_REQUESTS_METADATA_KEY
from tests.access_schema_support import with_current_room_member_access
from tests.approval_continuation_helpers import RECEIPT_CALLS, RECEIPT_TEXT
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.bot_helpers import _hook_plugin, make_test_agent_bot, unique_room_send_responses
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_model_providers,
    install_runtime_journal_support,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    make_turn_context,
    make_visible_message,
    message_origin,
    patch_response_runner_module,
    seed_session,
    test_runtime_paths,
    unwrap_extracted_collaborator,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call, usage
from tests.fakes.summary_model import summary_model
from tests.history_helpers import RecordingCompactionLifecycle
from tests.response_attempt_helpers import install_direct_response_admission
from tests.scripted_agent_helpers import (
    ProcessCrash,
    crash_after_first_terminal_store,
    registered_toolkit,
    streamed_text,
)
from tests.usage_helpers import assert_counted_usage, counted_total, counted_usage, usage_rows

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from agno.run import RunContext as AgnoRunContext

    from mindroom.constants import RuntimePaths
    from mindroom.post_response_effects import ResponseOutcome
    from mindroom.response_runner import ResponseRunner
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

ROOM_ID = "!room:localhost"
THREAD_ID = "$thread:localhost"
REQUESTER_ID = "@user:localhost"
EVENT_ID = "$current:localhost"
SESSION_ID = f"{ROOM_ID}:{THREAD_ID}"
PROBE_TOOL = "agent_turn_probe"
EXTERNAL_PROBE_TOOL = "agent_turn_external_probe"


class _ProbeToolkit(Toolkit):
    """Two functions the tests watch: one records its calls, one writes the shared session state."""

    calls: ClassVar[list[str]] = []

    def __init__(self) -> None:
        super().__init__(name="probe", tools=[self.touch, self.remember])

    async def touch(self, label: str) -> str:
        """Record one call.

        Args:
            label: What to record.

        """
        type(self).calls.append(label)
        return f"touched {label}"

    async def remember(self, label: str, run_context: AgnoRunContext | None = None) -> str:
        """Keep a label in the run's shared session state.

        Args:
            label: What to keep.
            run_context: The run, supplied by the loop.

        """
        assert run_context is not None
        run_context.session_state["remembered"] = label
        return "remembered"


class _ExternalProbeToolkit(_ProbeToolkit):
    """The probe whose ``touch`` runs outside the loop, as a delegation's ``run_subagent`` does."""

    def __init__(self) -> None:
        Toolkit.__init__(self, name="probe", tools=[self.touch], external_execution_required_tools=["touch"])


@pytest.fixture
def probe() -> Iterator[type[_ProbeToolkit]]:
    """Register the probe toolkits for the test and restore the registries afterwards."""
    _ProbeToolkit.calls.clear()
    with registered_toolkit(PROBE_TOOL, _ProbeToolkit), registered_toolkit(EXTERNAL_PROBE_TOOL, _ExternalProbeToolkit):
        yield _ProbeToolkit


@dataclass
class _Reply:
    """One agent reply's config, scripted model, recorder, and store."""

    config: Config
    runtime_paths: RuntimePaths
    model: ScriptedModel
    recorder: TurnRecorder
    storage: ConversationStore
    identity: ToolExecutionIdentity
    runtime: ToolRuntimeContext


def _reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: Sequence[Sequence[ScriptStep]],
    *,
    tools: Sequence[str] = (PROBE_TOOL,),
    approval_rules: Sequence[ApprovalRuleConfig] = (),
    plugins: Sequence[object] = (),
    max_tool_calls_per_turn: int | None = None,
    context_window: int | None = None,
) -> _Reply:
    """Build the reply of a one-agent config whose model is the scripted provider.

    A ``context_window`` turns on compaction with a ``summary`` model, which the test supplies.
    """
    agent: dict[str, Any] = {"display_name": "Assistant", "role": "Answer questions.", "tools": list(tools)}
    if max_tool_calls_per_turn is not None:
        agent["max_tool_calls_per_turn"] = max_tool_calls_per_turn
    authored: dict[str, Any] = {
        "defaults": {"tools": []},
        "agents": {"assistant": agent},
        "memory": {"backend": "none"},
        "models": {"default": {"provider": "openai", "id": "test-model", "extra_kwargs": {"api_key": "test-key"}}},
        "timezone": "UTC",
    }
    if context_window is not None:
        authored["models"]["default"]["context_window"] = context_window
        authored["models"]["summary"] = {"provider": "openai", "id": "summary-model", "context_window": 100_000}
        authored["defaults"]["compaction"] = {"model": "summary", "reserve_tokens": 500}
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate(authored), runtime_paths)
    if approval_rules:
        config.tool_approval = ToolApprovalConfig(rules=list(approval_rules))
    model = ScriptedModel([list(turn) for turn in turns])
    install_model_providers(monkeypatch, {"default": model})
    if plugins:
        monkeypatch.setattr(agents, "_load_agent_plugins", lambda *_args, **_kwargs: list(plugins))
    runtime = make_test_tool_runtime_context(
        agent_name="assistant",
        target=MessageTarget(
            room_id=ROOM_ID,
            source_thread_id=THREAD_ID,
            resolved_thread_id=THREAD_ID,
            reply_to_event_id=EVENT_ID,
            session_id=SESSION_ID,
        ),
        requester_id=REQUESTER_ID,
        client=SimpleNamespace(user_id="@mindroom_assistant:localhost"),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        correlation_id="correlation-1",
        membership_turn_id=EVENT_ID,
    )
    identity = build_execution_identity_from_runtime_context(runtime)
    storage = create_session_storage("assistant", config, runtime_paths, execution_identity=identity)
    return _Reply(config, runtime_paths, model, TurnRecorder(user_message="Hello"), storage, identity, runtime)


def _ctx(reply: _Reply, *, run_id: str = "run-1", **overrides: Any) -> ai.ResponseTurnContext:  # noqa: ANN401
    """Return the reply's turn as the response runner builds it, its other inputs set by ``overrides``."""
    return replace(
        make_turn_context(
            "assistant",
            session_id=SESSION_ID,
            run_id=run_id,
            correlation_id="correlation-1",
            reply_to_event_id=EVENT_ID,
            room_id=ROOM_ID,
            thread_id=THREAD_ID,
            requester_id=REQUESTER_ID,
        ),
        execution_identity=reply.identity,
        supports_native_tool_approval=True,
        **overrides,
    )


async def _stream(
    reply: _Reply,
    *,
    prompt: str = "Hello",
    run_id: str = "run-1",
    with_recorder: bool = True,
    **turn_inputs: Any,  # noqa: ANN401
) -> list[Any]:
    """Stream one reply as the response runner does and return its chunks."""
    with tool_runtime_context(reply.runtime):
        return [
            chunk
            async for chunk in stream_agent_response(
                _ctx(reply, run_id=run_id, prompt=prompt, **turn_inputs),
                reply.runtime_paths,
                reply.config,
                sinks=TurnSinks(turn_recorder=reply.recorder if with_recorder else None),
            )
        ]


async def _respond(reply: _Reply, *, prompt: str = "Hello", run_id: str = "run-1", **turn_inputs: Any) -> str:  # noqa: ANN401
    """Run one reply collected into its text, as a reply delivered without streaming is."""
    with tool_runtime_context(reply.runtime):
        result = await ai_response(
            _ctx(reply, run_id=run_id, prompt=prompt, **turn_inputs),
            reply.runtime_paths,
            reply.config,
            sinks=TurnSinks(turn_recorder=reply.recorder),
        )
    return result.text


def _stored(reply: _Reply, run_id: str = "run-1") -> RunRecord:
    record = reply.storage.get_run(run_id)
    assert isinstance(record, RunRecord), record
    return record


def _assert_counted_usage(reply: _Reply, expected: Sequence[tuple[int, int]]) -> None:
    """Assert the reply's session counted the ``expected`` usage snapshots everywhere usage is read."""
    assert_counted_usage(
        reply.storage,
        SESSION_ID,
        expected,
        config=reply.config,
        runtime_paths=reply.runtime_paths,
        model=reply.model.identity,
        requester_id=REQUESTER_ID,
    )


def _contents(messages: Sequence[Message]) -> list[tuple[str, object]]:
    return [(message.role, message.content) for message in messages]


def _raising(error: BaseException) -> Await:
    async def raise_it() -> None:
        raise error

    return Await(raise_it)


async def _cancel_current_task() -> None:
    task = asyncio.current_task()
    assert task is not None
    task.cancel()
    await asyncio.sleep(0)


def _cut_off(call: dict[str, Any]) -> list[ScriptStep]:
    """A response the output limit cut off while it requested ``call``."""
    final = usage(5, 1)
    return [
        ToolCallReady(call),
        UsageReported(final),
        TurnCompleted(message=Message(role="assistant", tool_calls=[call], metrics=final), stop_reason="output_limit"),
    ]


class _QueuedState:
    def __init__(self, *, pending: bool) -> None:
        self.pending = pending

    def has_pending_human_messages(self) -> bool:
        return self.pending

    def pending_message_snapshot(self) -> tuple[object, ...]:
        return ("$pending",) if self.pending else ()


@pytest.mark.asyncio
async def test_a_streamed_reply_runs_its_tool_calls_and_stores_one_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """Tool events and text deltas stream in order, the run is stored once completed, and the recorder settles."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="a")), answer("The probe answered.", usage(20, 4))],
    )

    chunks = await _stream(reply)

    assert probe.calls == ["a"]
    assert [type(chunk).__name__ for chunk in chunks[:2]] == ["ToolStarted", "ToolCompleted"]
    assert isinstance(chunks[-1], RunFinished)
    assert "".join(streamed_text(chunks)) == "The probe answered."
    assert len(reply.model.requests) == 2
    assert reply.model.requests[1].messages[-1].role == "tool"
    assert reply.model.requests[1].messages[-1].content == "touched a"
    assert all(request.tool_choice is None for request in reply.model.requests)
    record = _stored(reply)
    assert record.status is RunStatus.completed
    assert record.content == "The probe answered."
    assert [tool.tool_name for tool in record.tools or []] == ["touch"]
    assert record.metrics is not None
    assert (record.metrics.tokens.input_tokens, record.metrics.tokens.output_tokens) == (20, 4)
    assert reply.recorder.outcome == "completed"
    assert reply.recorder.assistant_text == "The probe answered."
    assert [tool.tool_name for tool in reply.recorder.completed_tools] == ["touch"]
    assert reply.storage.load_session(SESSION_ID) is not None


@pytest.mark.asyncio
async def test_a_collected_reply_returns_the_streamed_text_with_its_tool_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A reply collected into one text keeps the tool calls where they streamed, before the answer that followed."""
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("touch", "call_1", label="a")), answer("Done.")])

    text = await _respond(reply)

    assert text.strip() == "🔧 `touch` [1]\n\nDone."
    assert probe.calls == ["a"]
    assert _stored(reply).status is RunStatus.completed


@pytest.mark.asyncio
async def test_an_approval_gated_call_pauses_the_run_before_it_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A ``requires_confirmation`` tool never runs before approval: the run is stored paused on the call."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="dangerous"))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )

    with pytest.raises(ResponsePausedForApproval) as raised:
        await _respond(reply)

    assert probe.calls == []
    paused = raised.value.paused
    assert paused.run_id == "run-1"
    assert [(tool.tool_name, tool.requires_confirmation, tool.result) for tool in paused.tools] == [
        ("touch", True, None),
    ]
    assert paused.toolkit_owners[("assistant", "touch")] == PROBE_TOOL
    assert len(reply.model.requests) == 1
    record = _stored(reply)
    assert record.status is RunStatus.paused
    assert [requirement.needs_confirmation for requirement in record.requirements or []] == [True]
    assert reply.recorder.outcome == "suspended"


@pytest.mark.asyncio
async def test_a_pause_with_no_approval_to_ask_for_fails_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A run paused on an externally executed call that is not a delegation has nothing to resume it, so the reply fails.

    The loop stores the ``PAUSED`` row; the turn driver records the failure on the turn recorder as an error and
    writes it over the row, so the row no longer waits for an approval that never comes. The reply says why.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="outside"))],
        tools=(EXTERNAL_PROBE_TOOL,),
    )
    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)

    text = await _respond(reply)

    # Below the call it had shown, the reply ends with the failure's own paragraph.
    assert text.split("\n\n")[-2:] == [
        "🔧 `touch` [1] ⏳",
        "[assistant] ⚠️ Error: agent run 'run-1' paused on ['touch'] without an approval to ask for",
    ]
    assert probe.calls == []
    [paused] = replaced
    assert paused.status is RunStatus.paused
    assert [requirement.needs_external_execution for requirement in paused.requirements or []] == [True]
    record = _stored(reply)
    assert is_interrupted_replay_run(record)
    assert not record.requirements
    assert reply.recorder.outcome == "interrupted"
    assert reply.recorder.original_status is RunStatus.error


@pytest.mark.asyncio
async def test_plugin_hooks_gate_and_observe_every_tool_call_and_the_call_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A before-call hook blocks a call the model requested, the after-call hook sees results, and calls are recorded."""
    seen: list[tuple[str, object, bool]] = []
    recorded: list[tuple[str, object]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def block_b(ctx: ToolBeforeCallContext) -> None:
        if ctx.arguments.get("label") == "b":
            ctx.decline("b is off limits")

    @hook(EVENT_TOOL_AFTER_CALL)
    async def observe(ctx: ToolAfterCallContext) -> None:
        seen.append((ctx.tool_name, ctx.result, ctx.blocked))

    class _Recorder:
        async def started(self, tool_name: str, args: Mapping[str, object]) -> str:
            recorded.append(("started", (tool_name, dict(args))))
            return tool_name

        async def finished(self, call_id: str, tool_name: str, args: Mapping[str, object], result: object) -> None:
            del tool_name, args
            recorded.append(("finished", (call_id, result)))

    reply = _reply(
        tmp_path,
        monkeypatch,
        [
            calls(tool_call("touch", "call_a", label="a"), tool_call("touch", "call_b", label="b")),
            answer("Both handled."),
        ],
        plugins=[_hook_plugin("policy", [block_b, observe])],
    )

    with recording_tool_calls(_Recorder()):
        chunks = await _stream(reply)

    assert probe.calls == ["a"]
    assert "".join(streamed_text(chunks)) == "Both handled."
    results = {
        message.tool_call_id: message.content for message in reply.model.requests[1].messages if message.role == "tool"
    }
    assert results["call_a"] == "touched a"
    assert "[TOOL CALL DECLINED]" in str(results["call_b"])
    assert "b is off limits" in str(results["call_b"])
    assert {(name, blocked) for name, _result, blocked in seen} == {("touch", False), ("touch", True)}
    assert [result for name, result, blocked in seen if not blocked] == ["touched a"]
    assert ("started", ("touch", {"label": "a"})) in recorded
    assert ("finished", ("touch", "touched a")) in recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("with_recorder", [True, False])
@pytest.mark.usefixtures("probe")
async def test_a_stop_mid_stream_stores_the_cancelled_run_once_before_the_interrupted_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_recorder: bool,
) -> None:
    """The usage a cut-off request reported is counted once, on the cancelled run stored before the turn's record.

    The turn driver writes the interrupted record once, over the cancelled run the loop stored first, whether or not
    its caller passes a recorder.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [[TextDelta("Partial "), UsageReported(usage(10, 2), final=False), Await(_cancel_current_task)]],
    )
    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)

    task = asyncio.create_task(_stream(reply, with_recorder=with_recorder))
    outcome = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError)

    record = _stored(reply)
    assert [(run.run_id, run.status) for run in replaced] == [("run-1", RunStatus.cancelled)]
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "cancelled"
    assert "Partial" in str(record.content)
    if with_recorder:
        assert reply.recorder.outcome == "interrupted"
        assert reply.recorder.original_status is RunStatus.cancelled
        assert reply.recorder.assistant_text == "Partial "
    [row] = usage_rows(reply.storage, "run-1")
    assert row["metrics"]["input_tokens"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.usefixtures("probe")
async def test_a_stopped_reply_records_the_text_and_tool_calls_it_had_shown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """A blocking reply stopped mid-run records what its run had produced, as a streamed one does."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [
            calls(tool_call("touch", "call_1", label="a")),
            [TextDelta("Partial "), Await(_cancel_current_task)],
        ],
    )

    task = asyncio.create_task(_stream(reply) if streaming else _respond(reply))
    [outcome] = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(outcome, asyncio.CancelledError)
    assert reply.recorder.outcome == "interrupted"
    assert reply.recorder.original_status is RunStatus.cancelled
    assert reply.recorder.assistant_text == "Partial "
    assert [(tool.tool_name, tool.type) for tool in reply.recorder.completed_tools] == [
        ("touch", "tool_call_completed"),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_provider_error_stores_an_error_record_and_streams_the_friendly_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed request ends the reply with the user-facing error text; the run the loop stored says why it failed.

    The turn's interrupted record then replaces that run, keeping that the turn ended in an error.
    """
    reply = _reply(tmp_path, monkeypatch, [[_raising(RateLimited("slow down", provider="openai"))]])
    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)

    chunks = await _stream(reply)

    [text] = streamed_text(chunks)
    assert "Rate limited" in text
    [failed] = replaced
    assert failed.status is RunStatus.error
    assert (failed.content, failed.error) == (None, "slow down")
    record = _stored(reply)
    assert is_interrupted_replay_run(record)
    assert (record.metadata or {})["mindroom_original_status"] == "error"


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_an_error_record_never_repeats_an_earlier_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interrupted-turn record of a failed reply holds only this reply's own text, not replayed history."""
    reply = _reply(tmp_path, monkeypatch, [[_raising(RateLimited("slow down", provider="openai"))]])
    earlier = RunRecord(
        run_id="run-0",
        session_id=SESSION_ID,
        agent_id="assistant",
        status=RunStatus.completed,
        content="The image is one green pixel.",
        messages=[
            Message(role="user", content="What is this?"),
            Message(role="assistant", content="The image is one green pixel."),
        ],
    )
    session = new_scope_session(session_id=SESSION_ID, scope=HistoryScope(kind="agent", scope_id="assistant"))
    session.runs = [earlier]
    seed_session(reply.storage, session)
    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)

    text = await _respond(reply)

    assert "Rate limited" in text
    [failed] = replaced
    assert failed.status is RunStatus.error
    assert (failed.content, failed.error) == (None, "slow down")
    assert all("green pixel" not in str(message.content) for message in failed.messages if message.role == "assistant")
    record = _stored(reply)
    assert all("green pixel" not in str(message.content) for message in record.messages if message.role == "assistant")
    # The turn's own record keeps no partial text either: the earlier answer was history, not this reply's.
    assert reply.recorder.outcome == "interrupted"
    assert reply.recorder.original_status is RunStatus.error
    assert reply.recorder.assistant_text == ""


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_an_error_record_never_takes_an_earlier_reply_in_the_thread_for_its_own_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that fails on its first request records no partial text, though its input carries an earlier reply.

    The thread's earlier reply reaches the run's input, and is stored with the run, as an assistant message no model
    of this run answered.
    """
    reply = _reply(tmp_path, monkeypatch, [[_raising(RateLimited("slow down", provider="openai"))]])
    agent_id = entity_identity_registry(reply.config, reply.runtime_paths).current_ids["assistant"].full_id
    thread_history = [
        make_visible_message(sender=REQUESTER_ID, body="What is this?", event_id="$question:localhost"),
        make_visible_message(sender=agent_id, body="The image is one green pixel.", event_id="$answer:localhost"),
    ]

    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)

    text = await _respond(reply, thread_history=thread_history)

    assert "Rate limited" in text
    [failed] = replaced
    stored_replies = [message for message in failed.messages if message.role == "assistant"]
    assert ["green pixel" in str(message.content) for message in stored_replies] == [True]
    assert reply.recorder.outcome == "interrupted"
    assert reply.recorder.assistant_text == ""


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_an_empty_reply_is_left_out_of_history_and_retried_once_under_a_run_id_of_its_own(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed run with nothing to show is never stored as a turn, and the turn tries again under a new run id.

    The empty run's usage stays under the turn's run id. The retry's run is the turn's final run, which the turn's
    record, its run metadata, and the attempt-start sink a delegated child publishes its run through all name.
    """
    reply = _reply(tmp_path, monkeypatch, [answer("", usage(100, 5)), answer("Second try.", usage(120, 9))])
    run_metadata: dict[str, Any] = {}
    started: list[str] = []
    sinks = TurnSinks(
        turn_recorder=reply.recorder,
        run_metadata_collector=run_metadata,
        on_attempt_start=lambda run_id, _model_name: started.append(run_id),
    )

    with tool_runtime_context(reply.runtime):
        chunks = [
            chunk async for chunk in stream_agent_response(_ctx(reply), reply.runtime_paths, reply.config, sinks=sinks)
        ]

    assert "".join(streamed_text(chunks)) == "Second try."
    assert len(reply.model.requests) == 2
    [record] = reply.storage.list_runs(SESSION_ID)
    assert record.content == "Second try."
    assert started == ["run-1", record.run_id]
    assert (reply.recorder.outcome, reply.recorder.run_id) == ("completed", record.run_id)
    assert run_metadata[AI_RUN_METADATA_KEY]["run_id"] == record.run_id
    assert [row["metrics"]["input_tokens"] for row in usage_rows(reply.storage, "run-1")] == [100]
    assert [row["metrics"]["input_tokens"] for row in usage_rows(reply.storage, record.run_id)] == [120]


@pytest.mark.asyncio
async def test_a_queued_message_is_announced_after_a_tool_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """When a newer human message waits, the request after a tool batch carries the queued-message notice."""
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("touch", "call_1", label="a")), answer("Noted.")])

    with queued_message_signal_context(_QueuedState(pending=True)):
        chunks = await _stream(reply)

    assert probe.calls == ["a"]
    assert "".join(streamed_text(chunks)) == "Noted."
    notice = reply.config.get_prompt("QUEUED_MESSAGE_NOTICE_TEXT")
    assert reply.model.requests[1].messages[-1].content == notice
    assert all(message.content != notice for message in reply.model.requests[0].messages)


def _notice_markers(messages: Sequence[Message]) -> list[bool | str | None]:
    return [queued_notice_marker(message) for message in messages if is_queued_notice_message(message)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_reply_keeps_its_live_notice_until_the_response_boundary_finalizes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored run carries the notice the model saw until the outer boundary marks it as persisted."""
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("touch", "call_1", label="a")), answer("final answer")])

    with queued_message_signal_context(_QueuedState(pending=True)) as notice_context:
        result = "".join(streamed_text(await _stream(reply)))
        assert _notice_markers(_stored(reply).messages) == [True]
        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    assert result.startswith("final answer")
    assert _notice_markers(_stored(reply).messages) == ["persisted"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_failed_reply_drops_its_live_notice_at_the_response_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error-only reply keeps no notice in its stored run once the boundary finalizes it."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="a")), [_raising(RateLimited("slow down", provider="openai"))]],
    )

    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)

    with queued_message_signal_context(_QueuedState(pending=True)) as notice_context:
        await _respond(reply)
        [failed] = replaced
        assert failed.status == RunStatus.error
        assert _notice_markers(failed.messages) == [True]
        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    assert _notice_markers(_stored(reply).messages) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_reply_persists_a_notice_an_earlier_crashed_reply_left_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading the conversation turns a prior reply's live notice into the persisted one before the request."""
    reply = _reply(tmp_path, monkeypatch, [answer("final answer")])
    crash_left = Message(
        role="user",
        content="A newer message is waiting.",
        provider_data={
            QUEUED_MESSAGE_NOTICE_MARKER_KEY: True,
            "mindroom_queued_message_notice_response_turn_id": "prior",
        },
    )
    seed_session(
        reply.storage,
        ConversationSession(
            session_id=SESSION_ID,
            kind="agent",
            owner_id="assistant",
            runs=[RunRecord(run_id="run-0", agent_id="assistant", status=RunStatus.completed, messages=[crash_left])],
            created_at=1,
        ),
    )

    assert await _respond(reply) == "final answer"

    assert _notice_markers(_stored(reply, "run-0").messages) == ["persisted"]
    assert [_notice_markers([message]) for message in reply.model.requests[0].messages if message.from_history] == [
        ["persisted"],
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_only_a_reply_whose_attempt_started_hands_its_session_to_queued_notice_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that fails while preparing its first attempt ran nothing in the agent session, so it registers none."""
    reply = _reply(tmp_path, monkeypatch, [answer("Done.")])

    with queued_message_signal_context(None) as notices:
        assert await _respond(reply) == "Done."
    assert list(notices.storage_targets) == [("assistant", SESSION_ID, "agent")]

    async def history_unavailable(*_args: object, **_kwargs: object) -> None:
        msg = "history unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(ai, "_prepare_agent_run_context", history_unavailable)
    with queued_message_signal_context(None) as notices:
        assert await _respond(reply, run_id="run-2") == "[assistant] ⚠️ Error: history unavailable"
    assert notices.storage_targets == {}


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_silent_participation_decision_ends_the_reply_before_any_model_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that decides to stay silent sends only the decision request and leaves no run behind."""
    reply = _reply(tmp_path, monkeypatch, [answer('{"action":"stay_silent","reason":"Humans are talking."}')])
    gate = ParticipationGate()

    result = await _respond(reply, participation=gate)

    assert result == ""
    assert gate.is_silent
    assert gate.is_declined
    [decision] = reply.model.requests
    assert decision.tool_choice == "none"
    assert reply.storage.list_runs(SESSION_ID) == []
    assert reply.recorder.outcome == "skipped"


_SILENT_DECISION = '{"action":"stay_silent","reason":"Humans are talking."}'


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
@pytest.mark.parametrize("ending", ["empty", "declined"])
async def test_a_crash_after_a_reply_that_is_no_turn_leaves_nothing_the_next_prompt_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ending: str,
) -> None:
    """An empty reply, or one its participation check declined, is never stored as a turn, so no crash leaves one."""
    first = answer("") if ending == "empty" else answer(_SILENT_DECISION)
    reply = _reply(tmp_path, monkeypatch, [first, answer("Next answer.")])
    crash_after_first_terminal_store(monkeypatch)

    with pytest.raises(ProcessCrash):
        await _respond(reply, participation=ParticipationGate() if ending == "declined" else None)
    reply.recorder = TurnRecorder(user_message="Next question")
    result = await _respond(reply, prompt="Next question", run_id="run-2")

    assert result == "Next answer."
    assert [message for message in reply.model.requests[-1].messages if message.from_history] == []
    assert [run.run_id for run in reply.storage.list_runs(SESSION_ID)] == ["run-2"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
@pytest.mark.parametrize("ending", ["empty", "declined"])
@pytest.mark.parametrize("run_id", ["run-1", None])
async def test_a_reply_that_is_no_turn_still_counts_toward_usage_and_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ending: str,
    run_id: str | None,
) -> None:
    """Its tokens stay in the usage ledger, the usage report's session totals, and the requester's monthly spend."""
    if ending == "empty":
        script = [answer("", usage(100, 5)), answer("Recovered.", usage(120, 9))]
        expected_snapshots, stored = [(100, 5), (120, 9)], ["Recovered."]
    else:
        script = [answer(_SILENT_DECISION, usage(80, 12))]
        expected_snapshots, stored = [(80, 12)], []
    reply = _reply(tmp_path, monkeypatch, script)

    # With a turn run id or without one, an empty attempt's retry runs under a run id of its own.
    await _respond(reply, run_id=run_id, participation=ParticipationGate() if ending == "declined" else None)

    assert [run.content for run in reply.storage.list_runs(SESSION_ID)] == stored
    _assert_counted_usage(reply, expected_snapshots)


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_an_approving_participation_decision_reuses_the_reply_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision request is the reply's exact messages plus the decision prompt, with tool selection disabled."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [answer('{"action":"respond","reason":"A direct question."}'), answer("Useful answer")],
    )
    gate = ParticipationGate(instructions="Offer concise technical help.")

    result = await _respond(reply, participation=gate)

    assert result == "Useful answer"
    assert gate.approved
    decision, answer_request = reply.model.requests
    assert _contents(decision.messages[:-1]) == _contents(answer_request.messages)
    assert decision.messages[-1].role == "user"
    assert "Offer concise technical help." in str(decision.messages[-1].content)
    assert decision.tools == answer_request.tools
    assert decision.tool_choice == "none"
    assert answer_request.tool_choice is None
    record = _stored(reply)
    assert record.content == "Useful answer"
    # The decision prompt never enters the run's own history.
    assert all("respond or stay_silent" not in str(message.content) for message in record.messages)
    assert record.metrics is not None


@pytest.mark.asyncio
async def test_the_request_cap_ends_a_runaway_tool_loop_and_logs_the_pinned_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """Past the configured budget, calls are refused and the run completes after budget + 2 requests."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", f"call_{index}", label=str(index)), content="Working. ") for index in range(3)],
        max_tool_calls_per_turn=1,
    )

    with capture_logs() as logs:
        chunks = await _stream(reply)

    assert probe.calls == ["0"]
    assert len(reply.model.requests) == 3
    assert "".join(streamed_text(chunks)) == "Working. " * 3
    assert _stored(reply).status is RunStatus.completed
    assert [
        (entry["entity"], entry["budget"], entry["model_requests"])
        for entry in logs
        if entry["event"] == "tool_call_limit_reached"
    ] == [("assistant", 1, 3)]


@pytest.mark.asyncio
async def test_a_turn_that_reads_the_approval_receipt_keeps_it_for_its_run_and_never_stores_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """The bound receipt follows the reading run's input in each of its requests and never enters a stored run.

    The rejected model switch ends the first attempt, so the turn's answer comes from a second attempt without it.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [
            calls(tool_call("touch", "call_1", label="a")),
            calls(tool_call("switch_thread_model", "call_2", model_name="missing", when="next-turn")),
            answer("ok"),
        ],
        tools=(PROBE_TOOL, "thread_model"),
    )

    with approval_receipt_context(RECEIPT_CALLS, run_id="run-1"):
        await _stream(reply, run_id="run-1")

    assert probe.calls == ["a"]
    first, second, retried = reply.model.requests
    assert (first.messages[-1].role, first.messages[-1].content) == ("user", RECEIPT_TEXT)
    assert RECEIPT_TEXT not in system_text(first.messages)
    assert second.messages[: len(first.messages)] == first.messages
    assert all(RECEIPT_TEXT not in str(message.content) for message in retried.messages)
    session = reply.storage.load_session(SESSION_ID)
    assert session is not None
    assert len(session.runs) == 2
    assert all(RECEIPT_TEXT not in str(message.content) for run in session.runs for message in run.messages)


@pytest.mark.asyncio
async def test_a_later_attempt_of_a_turn_on_a_new_scope_replays_the_run_an_earlier_attempt_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """The scope's first session stays with the turn, so the attempt after a model switch replays the first one's run.

    The scope has no stored session when the turn starts; the second attempt's request carries both calls of the first.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [
            calls(tool_call("touch", "call_1", label="a")),
            calls(tool_call("switch_thread_model", "call_2", model_name="missing", when="next-turn")),
            answer("ok"),
        ],
        tools=(PROBE_TOOL, "thread_model"),
    )

    await _stream(reply)

    assert probe.calls == ["a"]
    *_, last = reply.model.requests
    assert [message.tool_call_id for message in last.messages if message.role == "tool"] == ["call_1", "call_2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_tool_calls_per_turn", "runs"),
    [(1, 1), (None, 2)],
    ids=["refused_by_the_call_limit", "ran_and_failed"],
)
async def test_only_a_model_switch_that_ran_continues_the_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    max_tool_calls_per_turn: int | None,
    runs: int,
) -> None:
    """A switch the call limit refused never ran, so the model's next answer ends the turn without a second attempt.

    A switch that ran and failed stops the run unanswered, so the turn continues with a second attempt.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [
            calls(tool_call("touch", "call_1", label="a")),
            calls(tool_call("switch_thread_model", "call_2", model_name="missing", when="next-turn")),
            answer("Done."),
            answer("Started over."),
        ],
        tools=(PROBE_TOOL, "thread_model"),
        max_tool_calls_per_turn=max_tool_calls_per_turn,
    )

    chunks = await _stream(reply)

    assert probe.calls == ["a"]
    assert len(reply.model.requests) == 3
    assert "".join(streamed_text(chunks)).endswith("Done.")
    session = reply.storage.load_session(SESSION_ID)
    assert session is not None
    assert len(session.runs) == runs
    # The refused switch is recorded as a failed call; the one that ran answered with the tool's own error payload.
    assert [(tool.tool_name, tool.is_error) for tool in session.runs[0].tools or ()] == [
        ("touch", False),
        ("switch_thread_model", max_tool_calls_per_turn is not None),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
@pytest.mark.parametrize("collected", [False, True], ids=["streamed", "collected"])
@pytest.mark.parametrize("ending", ["empty_retry", "model_switch"])
async def test_a_reply_publishes_the_usage_and_cost_of_every_attempt_its_turn_ran(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ending: str,
    collected: bool,
) -> None:
    """The reply's usage and cost sum its attempts, an empty one and its retry or a model switch and its continuation.

    They are what the usage ledger counts, each attempt priced at its own model, and the reply's first token is the
    first attempt's. The model, the run, the status, and the context are those of the attempt that answered.
    """
    switch = tool_call("switch_thread_model", "call-switch", model_name="large", when="after-toolcall")
    first_usage = usage(100, 5, time_to_first_token=0.5)
    first = answer("", first_usage) if ending == "empty_retry" else calls(switch, metrics=first_usage)
    final = answer("Answered.", usage(120, 9, time_to_first_token=0.25))
    reply = _reply(
        tmp_path,
        monkeypatch,
        [first, final] if ending == "empty_retry" else [first],
        tools=(PROBE_TOOL, "thread_model"),
    )
    with_current_room_member_access(reply.config)
    large = ScriptedModel(
        [final],
        identity=ModelIdentity(id="large-model", provider="Synthetic", provider_name="Synthetic"),
    )
    install_model_providers(monkeypatch, {"large": large})
    reply.config.models["default"].pricing = ModelPricing(input=2, output=10)
    reply.config.models["default"].context_window = 1_000
    reply.config.models["large"] = ModelConfig(
        provider="openai",
        id="large-model",
        context_window=2_000,
        pricing=ModelPricing(input=3, output=15),
    )
    sinks = TurnSinks(turn_recorder=reply.recorder)

    with tool_runtime_context(reply.runtime):
        if collected:
            result = await ai_response(_ctx(reply), reply.runtime_paths, reply.config, sinks=sinks)
            published = result.run_metadata[AI_RUN_METADATA_KEY]
        else:
            run_metadata: dict[str, Any] = {}
            stream = stream_agent_response(
                _ctx(reply),
                reply.runtime_paths,
                reply.config,
                sinks=replace(sinks, run_metadata_collector=run_metadata),
            )
            assert "".join(streamed_text([chunk async for chunk in stream])) == "Answered."
            published = run_metadata[AI_RUN_METADATA_KEY]

    assert sorted(counted_usage(reply.storage, SESSION_ID)) == [(100, 5), (120, 9)]
    usage_published = published["usage"]
    assert (usage_published["input_tokens"], usage_published["output_tokens"]) == counted_total(
        reply.storage,
        SESSION_ID,
    )
    assert (usage_published["total_tokens"], usage_published["time_to_first_token"]) == (234, "0.5")
    # Each attempt is priced at its model's prices: 100/5 and 120/9 tokens at 2/10 or 3/15 dollars per million.
    cost = 100 * 2 + 5 * 10 + (120 * 2 + 9 * 10 if ending == "empty_retry" else 120 * 3 + 9 * 15)
    assert published["cost"] == {"usd": format_cost_usd(cost / 1_000_000)}
    final_model, window = ("default", 1_000) if ending == "empty_retry" else ("large", 2_000)
    assert published["model"]["config"] == final_model
    assert published["context"] == {"input_tokens": 120, "window_tokens": window}
    assert (published["run_id"], published["status"]) == (reply.recorder.run_id, "completed")
    assert reply.recorder.run_id != "run-1"


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_reply_publishes_the_usage_of_each_of_three_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model switch whose continuation came back empty and was retried publishes all three attempts' usage."""
    switch = tool_call("switch_thread_model", "call-switch", model_name="missing", when="next-turn")
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(switch, metrics=usage(100, 5)), answer("", usage(40, 2)), answer("Answered.", usage(120, 9))],
        tools=(PROBE_TOOL, "thread_model"),
    )
    run_metadata: dict[str, Any] = {}

    with tool_runtime_context(reply.runtime):
        stream = stream_agent_response(
            _ctx(reply),
            reply.runtime_paths,
            reply.config,
            sinks=TurnSinks(turn_recorder=reply.recorder, run_metadata_collector=run_metadata),
        )
        assert "".join(streamed_text([chunk async for chunk in stream])).endswith("Answered.")

    assert len(reply.model.requests) == 3
    assert sorted(counted_usage(reply.storage, SESSION_ID)) == [(40, 2), (100, 5), (120, 9)]
    published = run_metadata[AI_RUN_METADATA_KEY]["usage"]
    assert (published["input_tokens"], published["output_tokens"]) == counted_total(reply.storage, SESSION_ID)


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_reply_whose_continuation_failed_publishes_the_usage_of_the_attempt_before_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed attempt ends the turn, and the reply's usage still holds the switch's attempt, as the ledger does."""
    switch = tool_call("switch_thread_model", "call-switch", model_name="missing", when="next-turn")
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(switch, metrics=usage(100, 5)), [_raising(RateLimited("slow down", provider="openai"))]],
        tools=(PROBE_TOOL, "thread_model"),
    )

    with tool_runtime_context(reply.runtime):
        result = await ai_response(_ctx(reply), reply.runtime_paths, reply.config, sinks=TurnSinks())

    assert result.failed is not None
    published = result.run_metadata[AI_RUN_METADATA_KEY]
    assert published["status"] == "error"
    assert counted_total(reply.storage, SESSION_ID) == (100, 5)
    assert (published["usage"]["input_tokens"], published["usage"]["output_tokens"]) == (100, 5)


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_the_hooks_of_a_reply_are_installed_in_their_documented_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a job actor, participation decides first, compaction rewrites next, and the receipt hook comes last.

    The rest of the hooks are wired. A reply with a job actor projects its job definitions before participation.
    """
    reply = _reply(tmp_path, monkeypatch, [answer('{"action":"respond","reason":"Yes."}'), answer("ok")])
    captured: dict[str, Any] = {}
    original = ai.run_agent

    def capturing(spec: Any, run_input: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        captured.update(kwargs)
        return original(spec, run_input, **kwargs)

    monkeypatch.setattr(ai, "run_agent", capturing)

    await _stream(reply, participation=ParticipationGate(), skill_review_capture=SkillReviewCapture())

    hooks = captured["hooks"]
    assert [function.__qualname__.split(".")[0] for function in hooks.before_request] == [
        "participation_hook",
        "mid_turn_compaction_hook",
        "approval_receipt_hook",
    ]
    assert hooks.answer_tool_calls is not None
    assert hooks.rewrite_tool_calls is None
    # This agent offers no scheduler, so its calls run without the live-function binding step.
    assert [middleware.__name__ for middleware in hooks.tool_middleware] == ["middleware"]
    assert len(hooks.after_tool_batch) == 1
    assert len(hooks.on_resume_entry) == 1
    assert hooks.on_loop_end is not None
    assert captured["request_template"].native_compaction is None


@dataclass(frozen=True)
class _NativeRoute:
    """A route that compacts natively at the threshold it is given."""

    def native_compaction_settings(
        self,
        *,
        threshold: int | None,
        history_generation: str,
        allow_authored: bool,
    ) -> NativeCompactionReplay | None:
        del history_generation, allow_authored
        return None if threshold is None else NativeCompactionReplay(route="route-a", threshold=threshold)


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_reply_on_a_natively_compacting_route_sends_the_native_history_its_preparation_chose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply's request compacts on the provider at the configured threshold and replays its history explicitly."""
    reply = _reply(tmp_path, monkeypatch, [answer("ok")])
    reply.model.capabilities = Capabilities(native_compaction=True)
    reply.model.native_history_route = _NativeRoute()
    reply.config.models["default"].context_window = 200_000
    reply.config.defaults.compaction = CompactionConfig(threshold_tokens=30_000)

    await _stream(reply)

    [request] = reply.model.requests
    assert request.native_compaction == NativeCompactionReplay(
        route="route-a",
        threshold=30_000,
        basis=replay_basis(None, []),
    )
    assert request.portable_replay is True


@pytest.mark.asyncio
@pytest.mark.parametrize("replays_history", [True, False])
@pytest.mark.usefixtures("probe")
async def test_mid_turn_compaction_folds_into_the_scope_only_when_the_turn_replays_its_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replays_history: bool,
) -> None:
    """A reply that replays the scope's history binds its folds to the scope; one without replay compacts run-locally.

    A scheduled turn whose history budget disabled replay deliberately carries no stored history, so a fold must not
    append a scope summary row or inject the scope's summary into its request.
    """
    reply = _reply(tmp_path, monkeypatch, [answer("ok")])
    bound: dict[str, Any] = {}
    original = loop_hooks.mid_turn_compaction_hook

    def capturing(**kwargs: Any) -> Any:  # noqa: ANN401
        bound.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(loop_hooks, "mid_turn_compaction_hook", capturing)
    budget = None if replays_history else ScheduledHistoryBudget(limit=0, source_event_id=EVENT_ID)

    chunks = await _stream(reply, scheduled_history_budget=budget)

    assert "".join(streamed_text(chunks)) == "ok"
    storage = bound["storage"]
    assert storage.store is not None
    assert storage.session is not None
    assert storage.scope == HistoryScope(kind="agent", scope_id="assistant")
    plan = bound["replay_plan"]
    assert (plan is not None and plan.add_history_to_context) is replays_history


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_reply_posts_its_mid_turn_compaction_notices_and_seeds_no_earlier_fold_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fold during the reply reaches its notices and its turn recorder through the owned summarizer.

    The run's metadata keeps what the turn seeded but none of the folded usage of the run it follows.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        # The first response reports a request far over the 4,000-token window, so the second request compacts.
        [calls(tool_call("touch", "call_1", label="a"), metrics=usage(1_000_000, 1)), answer("done")],
        context_window=4000,
    )
    summarizer = summary_model("SUMMARY")
    install_model_providers(monkeypatch, {"summary": summarizer})
    lifecycle = RecordingCompactionLifecycle()
    seeded = {COMPACTED_REQUESTS_METADATA_KEY: [{"input_tokens": 10}], "room_topic": "kept"}

    chunks = await _stream(reply, compaction_lifecycle=lifecycle, matrix_run_metadata=seeded)

    assert "".join(streamed_text(chunks)) == "done"
    assert len(summarizer.requests) == 1
    assert any(isinstance(event, CompactionOutcome) for event in lifecycle.events)
    # An interrupted record of this run would leave out the calls the fold summarized.
    assert "call_1" in reply.recorder.folded_tool_call_ids
    metadata = _stored(reply).metadata
    assert metadata is not None
    assert metadata["room_topic"] == "kept"
    assert [request["metrics"]["input_tokens"] for request in metadata[COMPACTED_REQUESTS_METADATA_KEY]] == [1_000_000]


@pytest.mark.asyncio
async def test_a_cut_off_response_answers_its_calls_without_running_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """On any route, calls of a response the output limit cut off are answered, not run, and the model retries."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [_cut_off(tool_call("touch", "call_1", label="huge")), answer("Split into parts.")],
    )

    chunks = await _stream(reply)

    assert probe.calls == []
    assert "".join(streamed_text(chunks)) == "Split into parts."
    answered = [message for message in reply.model.requests[1].messages if message.role == "tool"]
    assert [(message.tool_call_id, message.content, message.tool_call_error) for message in answered] == [
        ("call_1", CUT_OFF_TOOL_CALL_RESULT, True),
    ]


@pytest.mark.asyncio
async def test_a_third_cut_off_response_fails_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A model that keeps hitting its output limit ends the reply with an error instead of looping."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [_cut_off(tool_call("touch", f"call_{index}", label="huge")) for index in range(3)],
    )
    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)

    chunks = await _stream(reply)

    assert probe.calls == []
    assert len(reply.model.requests) == 3
    assert streamed_text(chunks)
    [failed] = replaced
    assert failed.status is RunStatus.error
    assert "output limit" in str(failed.error)


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_a_safeguard_refusal_is_stored_and_reported_as_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused response fails the run with the refusal text the runtime recognizes."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [[_raising(ModelSafeguardRefusal(provider="anthropic", usage=MessageMetrics()))]],
    )
    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)

    chunks = await _stream(reply)

    assert streamed_text(chunks)
    [failed] = replaced
    assert failed.status is RunStatus.error
    assert (failed.content, failed.error) == (None, MODEL_SAFEGUARD_REFUSAL_MESSAGE)


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_session_state_a_tool_changes_is_saved_with_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared ``session_state`` a tool writes lands on the stored session."""
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("remember", "call_1", label="blue")), answer("Kept.")])

    await _stream(reply)

    session = reply.storage.load_session(SESSION_ID)
    assert session is not None
    assert session.session_state == {"remembered": "blue"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("probe")
async def test_the_skill_review_capture_keeps_the_final_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response that counts toward skill learning leaves its final request, ending in the model's answer."""
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("touch", "call_1", label="a")), answer("Final answer.")])
    capture = SkillReviewCapture()

    await _stream(reply, skill_review_capture=capture)

    assert capture.latest is not None
    assert (capture.latest.run_id, capture.latest.model_name) == ("run-1", "default")
    final = capture.latest.request.messages[-1]
    assert (final.role, final.content) == ("assistant", "Final answer.")
    assert {"remember", "touch"} <= {tool.name for tool in capture.latest.request.tools}


def _runner(reply: _Reply) -> ResponseRunner:
    """Build the agent's bot on the reply's config, with a mocked Matrix client, and return its response runner."""
    with_current_room_member_access(reply.config)
    agent_user = AgentMatrixUser(
        agent_name="assistant",
        password=TEST_PASSWORD,
        display_name="Assistant",
        user_id="@mindroom_assistant:localhost",
    )
    bot = make_test_agent_bot(agent_user, reply.config, reply.runtime_paths, rooms=[ROOM_ID])
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id="@mindroom_assistant:localhost")
    unique_room_send_responses(bot.client)
    install_runtime_journal_support(bot)
    return bot._response_runner


def _request(prompt: str = "Hello") -> ResponseRequest:
    """Return one admitted reply request for the thread the ``ai`` seam tests use."""
    target = MessageTarget.resolve(room_id=ROOM_ID, thread_id=THREAD_ID, reply_to_event_id=EVENT_ID)
    return ResponseRequest(
        thread_history=[],
        prompt=prompt,
        user_id=REQUESTER_ID,
        response_envelope=MessageEnvelope(
            source_event_id=EVENT_ID,
            target=target,
            body=prompt,
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="assistant",
            origin=message_origin(sender_id=REQUESTER_ID, requester_id=REQUESTER_ID, source_kind="message"),
        ),
        sources=ResponseSources(pending_event_ids=(EVENT_ID,), logical_source_event_ids=(EVENT_ID,)),
    )


async def _respond_through_runner(
    runner: ResponseRunner,
    request: ResponseRequest,
    *,
    streaming: bool,
) -> tuple[FinalDeliveryOutcome, ResponseOutcome]:
    """Run one reply through the runner's locked turn and return how its reply ended and what its turn reported."""
    ended: list[tuple[FinalDeliveryOutcome, ResponseOutcome]] = []

    async def record_ending(delivery: FinalDeliveryOutcome, outcome: ResponseOutcome, *_args: object) -> None:
        ended.append((delivery, outcome))

    with patch_response_runner_module(
        should_use_streaming=AsyncMock(return_value=streaming),
        apply_post_response_effects=AsyncMock(side_effect=record_ending),
    ):
        await runner.generate_response(request)
    [result] = ended
    return result


def _runs_under_interrupted_records(monkeypatch: pytest.MonkeyPatch, storage: ConversationStore) -> list[RunRecord]:
    """Record the run the loop stored that each interrupted-turn record the turn driver writes replaces."""
    runs: list[RunRecord] = []
    original = response_turn.persist_interrupted_replay_snapshot

    def spy(**kwargs: Any) -> None:  # noqa: ANN401
        stored = storage.get_run(kwargs["run_id"])
        assert stored is not None, "the loop stores the run before the turn's interrupted record replaces it"
        runs.append(stored)
        original(**kwargs)

    monkeypatch.setattr(response_turn, "persist_interrupted_replay_snapshot", spy)
    return runs


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.usefixtures("probe")
async def test_the_runner_delivers_an_empty_attempt_s_retry_as_the_turn_s_final_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Through ``ResponseRunner``, whose turns name their runs, the reply and the turn's outcome name the retry's run.

    Both attempts' usage counts: the retry's run does not replace the empty one's usage.
    """
    reply = _reply(tmp_path, monkeypatch, [answer("", usage(100, 5)), answer("Recovered.", usage(120, 9))])
    runner = _runner(reply)

    delivery, outcome = await _respond_through_runner(runner, _request(), streaming=streaming)

    assert (delivery.ending, delivery.final_visible_body, outcome.run_succeeded) == (Completed(), "Recovered.", True)
    [record] = reply.storage.list_runs(SESSION_ID)
    assert record.content == "Recovered."
    assert delivery.response_run_id == outcome.response_run_id == record.run_id
    _assert_counted_usage(reply, [(100, 5), (120, 9)])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.usefixtures("probe")
async def test_the_runner_writes_the_interrupted_record_after_the_loop_stored_the_cancelled_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Through ``ResponseRunner``, a stop mid-stream stores the ``CANCELLED`` run before the interrupted record.

    The turn driver writes the interrupted-turn record over the cancelled run the loop stored, and the cut-off
    request's usage counts once.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [[TextDelta("Partial "), UsageReported(usage(10, 2), final=False), Await(_cancel_current_task)]],
    )
    runner = _runner(reply)
    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)

    task = asyncio.create_task(_respond_through_runner(runner, _request(), streaming=streaming))
    [result] = await asyncio.gather(task, return_exceptions=True)

    assert not isinstance(result, BaseException), result
    delivery, outcome = result
    assert delivery.ending == Cancelled("interrupted")
    assert outcome.run_succeeded is False
    run_id = outcome.response_run_id
    assert run_id is not None
    assert [(run.run_id, run.status) for run in replaced] == [(run_id, RunStatus.cancelled)]
    record = reply.storage.get_run(run_id)
    assert record is not None
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "cancelled"
    [row] = usage_rows(reply.storage, run_id)
    assert row["metrics"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_the_runner_leaves_a_paused_run_as_the_loop_stored_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """Through ``ResponseRunner``, an approval pause reaches the suspension handler with its ``PAUSED`` row intact.

    The turn driver writes no interrupted-turn record over a suspended turn.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="dangerous"), metrics=usage(7, 1))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    runner = unwrap_extracted_collaborator(_runner(reply))
    replaced = _runs_under_interrupted_records(monkeypatch, reply.storage)
    suspend = AsyncMock(
        return_value=FinalDeliveryOutcome(ending=AwaitingApproval(), event_id=None, is_visible_response=False),
    )

    with (
        patch.object(runner.approvals, "suspend_for_approval", new=suspend),
        patch_response_runner_module(should_use_streaming=AsyncMock(return_value=False)),
    ):
        await runner.generate_response(_request())

    assert probe.calls == []
    assert replaced == []
    paused = suspend.await_args.args[0]
    record = reply.storage.get_run(paused.run_id)
    assert record is not None
    assert record.status is RunStatus.paused
    assert [requirement.needs_confirmation for requirement in record.requirements or []] == [True]
    [row] = usage_rows(reply.storage, paused.run_id)
    assert row["metrics"]["input_tokens"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_the_runner_records_the_failure_over_a_pause_with_no_approval_to_ask_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    streaming: bool,
) -> None:
    """Through ``ResponseRunner``, a pause nothing can resume fails the reply, and the failure lands over the ``PAUSED`` row.

    The loop stores the pause; the lifecycle owner then records the failure over the row as its interrupted-turn
    record, so it no longer waits for an approval that never comes. The reply says why, streamed or delivered whole.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="outside"), metrics=usage(7, 1))],
        tools=(EXTERNAL_PROBE_TOOL,),
    )
    runner = _runner(reply)
    presence = MagicMock(spec=nio.PresenceGetResponse)
    presence.presence = "online" if streaming else "offline"
    presence.last_active_ago = 0
    runner.deps.runtime.client.get_presence = AsyncMock(return_value=presence)

    await runner.generate_response(_request())

    body = runner.deps.runtime.client.room_send.await_args.kwargs["content"]["body"]
    assert body.split("\n\n")[-1].startswith("[assistant] ⚠️ Error: agent run ")
    assert body.endswith(" paused on ['touch'] without an approval to ask for")
    assert probe.calls == []
    session = reply.storage.load_session(SESSION_ID)
    assert session is not None
    [record] = session.runs
    assert record.status is RunStatus.completed
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "error"
    assert record.metadata["mindroom_replay_state"] == "interrupted"
    assert not record.requirements
