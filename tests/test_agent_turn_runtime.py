"""Agent turns on the owned loop, through ``ai.stream_agent_response`` and ``ai.ai_response``.

The seam is ``run_agent`` fed by a scripted provider behind a real ``create_agent`` build: what a reply streams,
what it stores, which tool-call policy runs around every call, and how it ends when it is paused, cancelled,
failed, empty, or silent. The last tests drive the same turns through ``ResponseRunner``, the lifecycle owner that
writes the interrupted-turn record after the loop stored its own terminal record.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from agno.tools import Toolkit
from structlog.testing import capture_logs

from mindroom import agents, ai, ai_runtime, loop_hooks, response_runner
from mindroom.agent_loop.output_limit import CUT_OFF_TOOL_CALL_RESULT
from mindroom.ai import ai_response, stream_agent_response
from mindroom.ai_runtime import queued_message_signal_context
from mindroom.approval.receipt import approval_receipt_context
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.dispatch_source import ScheduledHistoryBudget
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
from mindroom.llm.events import ContentDelta, RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, TextDelta, ToolCallReady, TurnCompleted, UsageReported
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.participation import ParticipationGate
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.skill_learning.capture import SkillReviewCapture
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.metadata import TOOL_METADATA, TOOL_REGISTRY, ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.usage.storage import COMPACTED_REQUESTS_METADATA_KEY, quote_identifier
from tests.access_schema_support import with_current_room_member_access
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.bot_helpers import make_test_agent_bot, unique_room_send_responses
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    make_turn_context,
    message_origin,
    seed_session,
    test_runtime_paths,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call, usage
from tests.fakes.summary_model import summary_model
from tests.history_helpers import RecordingCompactionLifecycle
from tests.reply_span_helpers import response_span
from tests.response_attempt_helpers import install_direct_response_admission

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from agno.run import RunContext as AgnoRunContext

    from mindroom.constants import RuntimePaths
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
    """Register the probe toolkit for the test and restore the registries afterwards."""
    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()

    @register_tool_with_metadata(
        name=PROBE_TOOL,
        file_access=ToolFileAccess.NONE,
        display_name="Agent Turn Probe",
        description="Records the calls an agent turn makes.",
        category=ToolCategory.DEVELOPMENT,
    )
    def probe_factory() -> type[_ProbeToolkit]:
        return _ProbeToolkit

    @register_tool_with_metadata(
        name=EXTERNAL_PROBE_TOOL,
        file_access=ToolFileAccess.NONE,
        display_name="Agent Turn External Probe",
        description="Pauses an agent turn for execution outside the loop.",
        category=ToolCategory.DEVELOPMENT,
    )
    def external_probe_factory() -> type[_ExternalProbeToolkit]:
        return _ExternalProbeToolkit

    _ProbeToolkit.calls.clear()
    try:
        yield _ProbeToolkit
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)


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
    capabilities: Capabilities | None = None,
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
    model = ScriptedModel([list(turn) for turn in turns], capabilities=capabilities or Capabilities())
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: model)
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
    del reply
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
        **overrides,
    )


async def _stream(
    reply: _Reply,
    *,
    prompt: str = "Hello",
    run_id: str = "run-1",
    with_recorder: bool = True,
    **kwargs: Any,  # noqa: ANN401
) -> list[Any]:
    """Stream one reply as the response runner does and return its chunks."""
    ctx = _ctx(
        reply,
        run_id=run_id,
        **{
            k: kwargs.pop(k)
            for k in ("participation", "skill_review_capture", "scheduled_history_budget", "matrix_run_metadata")
            if k in kwargs
        },
    )
    with tool_runtime_context(reply.runtime):
        return [
            chunk
            async for chunk in stream_agent_response(
                ctx,
                prompt=prompt,
                runtime_paths=reply.runtime_paths,
                config=reply.config,
                execution_identity=reply.identity,
                turn_recorder=reply.recorder if with_recorder else None,
                supports_native_tool_approval=True,
                **kwargs,
            )
        ]


async def _respond(reply: _Reply, *, prompt: str = "Hello", run_id: str = "run-1", **kwargs: Any) -> str:  # noqa: ANN401
    """Run one blocking reply and return its text."""
    ctx = _ctx(reply, run_id=run_id, **{k: kwargs.pop(k) for k in ("participation",) if k in kwargs})
    with tool_runtime_context(reply.runtime):
        return await ai_response(
            ctx,
            prompt=prompt,
            runtime_paths=reply.runtime_paths,
            config=reply.config,
            execution_identity=reply.identity,
            turn_recorder=reply.recorder,
            supports_native_tool_approval=True,
            **kwargs,
        )


def _text(chunks: Sequence[Any]) -> str:
    return "".join(chunk.text for chunk in chunks if isinstance(chunk, ContentDelta))


def _stored(reply: _Reply, run_id: str = "run-1") -> RunRecord:
    record = reply.storage.get_run(run_id)
    assert isinstance(record, RunRecord), record
    return record


def _usage_rows(storage: ConversationStore, run_id: str) -> list[dict[str, Any]]:
    """Return the usage ledger rows of one run."""
    with storage.transaction(write=False) as connection:
        rows = connection.execute(
            f"SELECT usage_data FROM {quote_identifier(storage.names.usage)} WHERE run_id = ?",  # noqa: S608
            (run_id,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


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


def _plugin(name: str, callbacks: Sequence[object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        discovered_automations=(),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}", settings={}),
        plugin_order=0,
    )


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
    assert _text(chunks) == "The probe answered."
    assert len(reply.model.requests) == 2
    assert reply.model.requests[1].messages[-1].role == "tool"
    assert reply.model.requests[1].messages[-1].content == "touched a"
    assert all(request.tool_choice is None and request.stream for request in reply.model.requests)
    record = _stored(reply)
    assert record.status is RunStatus.completed
    assert record.content == "The probe answered."
    assert [tool.tool_name for tool in record.tools or []] == ["touch"]
    assert record.metrics is not None
    assert (record.metrics.input_tokens, record.metrics.output_tokens) == (20, 4)
    assert reply.recorder.outcome == "completed"
    assert reply.recorder.assistant_text == "The probe answered."
    assert [tool.tool_name for tool in reply.recorder.completed_tools] == ["touch"]
    assert reply.storage.load_session(SESSION_ID, "agent") is not None


@pytest.mark.asyncio
async def test_a_blocking_reply_sends_non_streaming_requests_and_returns_the_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """The blocking path runs the same loop with ``stream=False`` requests and the tool trace in the text."""
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("touch", "call_1", label="a")), answer("Done.")])

    text = await _respond(reply)

    assert text.startswith("Done.")
    assert "touch" in text
    assert probe.calls == ["a"]
    assert all(request.stream is False for request in reply.model.requests)
    assert _stored(reply).status is RunStatus.completed


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_an_approval_gated_call_pauses_the_run_before_it_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    streaming: bool,
) -> None:
    """A ``requires_confirmation`` tool never runs before approval: the run is stored paused on the call."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="dangerous"))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )

    run = _stream(reply) if streaming else _respond(reply)
    with pytest.raises(ResponsePausedForApproval) as raised:
        await run

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
@pytest.mark.parametrize("streaming", [True, False])
async def test_a_pause_with_no_approval_to_ask_for_fails_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    streaming: bool,
) -> None:
    """A run paused on an externally executed call that is not a delegation has nothing to resume it, so the reply fails.

    At this seam the ``PAUSED`` row stays as the loop wrote it and the failure lands on the turn recorder as an
    error; the lifecycle owner then records the failure over the row (``test_the_runner_records_the_failure_...``).
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="outside"))],
        tools=(EXTERNAL_PROBE_TOOL,),
    )

    run = _stream(reply) if streaming else _respond(reply)
    with pytest.raises(RuntimeError, match="without an approval to ask for"):
        await run

    assert probe.calls == []
    record = _stored(reply)
    assert record.status is RunStatus.paused
    assert [requirement.needs_external_execution for requirement in record.requirements or []] == [True]
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
        plugins=[_plugin("policy", [block_b, observe])],
    )

    with recording_tool_calls(_Recorder()):
        chunks = await _stream(reply)

    assert probe.calls == ["a"]
    assert _text(chunks) == "Both handled."
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
async def test_a_stop_mid_stream_stores_the_cancelled_run_once_before_the_interrupted_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    with_recorder: bool,
) -> None:
    """The usage a cut-off request reported is counted once, on the cancelled run stored before the turn's record.

    A reply with a turn recorder leaves the interrupted record to its lifecycle owner; one without writes it itself,
    over the cancelled run the loop stored first.
    """
    del probe
    reply = _reply(
        tmp_path,
        monkeypatch,
        [[TextDelta("Partial "), UsageReported(usage(10, 2), final=False), Await(_cancel_current_task)]],
    )
    statuses_when_interrupted_record_written: list[RunStatus | None] = []
    original = ai.persist_interrupted_replay

    def spy(**kwargs: Any) -> None:  # noqa: ANN401
        stored = reply.storage.get_run("run-1")
        statuses_when_interrupted_record_written.append(stored.status if stored is not None else None)
        original(**kwargs)

    monkeypatch.setattr(ai, "persist_interrupted_replay", spy)

    task = asyncio.create_task(_stream(reply, with_recorder=with_recorder))
    outcome = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError)

    record = _stored(reply)
    if with_recorder:
        assert statuses_when_interrupted_record_written == []
        assert record.status is RunStatus.cancelled
        # No request completed, so the run holds no content; the recorder keeps the streamed text for its record.
        assert record.content is None
        assert reply.recorder.outcome == "interrupted"
        assert reply.recorder.original_status is RunStatus.cancelled
        assert reply.recorder.assistant_text == "Partial "
    else:
        assert statuses_when_interrupted_record_written == [RunStatus.cancelled]
        assert record.metadata is not None
        assert record.metadata["mindroom_original_status"] == "cancelled"
        assert "Partial" in str(record.content)
    [row] = _usage_rows(reply.storage, "run-1")
    assert row["metrics"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_a_provider_error_stores_an_error_record_and_streams_the_friendly_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A failed request ends the reply with the user-facing error text; the stored run says why it failed."""
    del probe
    reply = _reply(tmp_path, monkeypatch, [[_raising(RateLimited("slow down", provider="openai"))]])

    chunks = await _stream(reply)

    [text] = [chunk for chunk in chunks if isinstance(chunk, str)]
    assert "Rate limited" in text
    record = _stored(reply)
    assert record.status is RunStatus.error
    assert record.content == "slow down"


@pytest.mark.asyncio
async def test_an_error_record_never_repeats_an_earlier_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """The interrupted-turn record of a failed reply holds only this reply's own text, not replayed history."""
    del probe
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
    session = new_scope_session(session_id=SESSION_ID, scope_id="assistant", is_team=False)
    session.runs = [earlier]
    seed_session(reply.storage, session)

    text = await _respond(reply)

    assert "Rate limited" in text
    record = _stored(reply)
    assert record.status is RunStatus.error
    assert record.content == "slow down"
    assert all("green pixel" not in str(message.content) for message in record.messages if message.role == "assistant")
    # The turn's own record keeps no partial text either: the earlier answer was history, not this reply's.
    assert reply.recorder.outcome == "interrupted"
    assert reply.recorder.original_status is RunStatus.error
    assert reply.recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_an_empty_reply_is_discarded_and_retried_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A completed run with nothing to show is removed from the scope and the turn tries again under a new run id."""
    del probe
    reply = _reply(tmp_path, monkeypatch, [answer(""), answer("Second try.")])

    chunks = await _stream(reply)

    assert _text(chunks) == "Second try."
    assert len(reply.model.requests) == 2
    # The retry reuses the turn's run id, so the discarded empty run leaves nothing behind.
    [record] = reply.storage.list_runs(SESSION_ID)
    assert (record.run_id, record.content) == ("run-1", "Second try.")
    assert reply.recorder.outcome == "completed"


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
    assert _text(chunks) == "Noted."
    notice = reply.config.get_prompt("QUEUED_MESSAGE_NOTICE_TEXT")
    assert reply.model.requests[1].messages[-1].content == notice
    assert all(message.content != notice for message in reply.model.requests[0].messages)


def _notice_markers(messages: Sequence[Message]) -> list[bool | str | None]:
    return [queued_notice_marker(message) for message in messages if is_queued_notice_message(message)]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_a_reply_keeps_its_live_notice_until_the_response_boundary_finalizes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    streaming: bool,
) -> None:
    """The stored run carries the notice the model saw until the outer boundary marks it as persisted."""
    del probe
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("touch", "call_1", label="a")), answer("final answer")])

    with queued_message_signal_context(_QueuedState(pending=True)) as notice_context:
        result = _text(await _stream(reply)) if streaming else await _respond(reply)
        assert _notice_markers(_stored(reply).messages) == [True]
        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    assert result.startswith("final answer")
    assert _notice_markers(_stored(reply).messages) == ["persisted"]


@pytest.mark.asyncio
async def test_a_failed_reply_drops_its_live_notice_at_the_response_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """An error-only reply keeps no notice in its stored run once the boundary finalizes it."""
    del probe
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="a")), [_raising(RateLimited("slow down", provider="openai"))]],
    )

    with queued_message_signal_context(_QueuedState(pending=True)) as notice_context:
        await _respond(reply)
        stored = _stored(reply)
        assert stored.status == RunStatus.error
        assert _notice_markers(stored.messages) == [True]
        await ai_runtime.finalize_queued_notice_response_turn_async(notice_context)

    assert _notice_markers(_stored(reply).messages) == []


@pytest.mark.asyncio
async def test_a_reply_persists_a_notice_an_earlier_crashed_reply_left_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """Loading the conversation turns a prior reply's live notice into the persisted one before the request."""
    del probe
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
            updated_at=1,
        ),
    )

    assert await _respond(reply) == "final answer"

    assert _notice_markers(_stored(reply, "run-0").messages) == ["persisted"]
    assert [_notice_markers([message]) for message in reply.model.requests[0].messages if message.from_history] == [
        ["persisted"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_a_silent_participation_decision_ends_the_reply_before_any_model_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    streaming: bool,
) -> None:
    """A reply that decides to stay silent sends only the decision request and leaves no run behind."""
    del probe
    reply = _reply(tmp_path, monkeypatch, [answer('{"action":"stay_silent","reason":"Humans are talking."}')])
    gate = ParticipationGate()

    result = _text(await _stream(reply, participation=gate)) if streaming else await _respond(reply, participation=gate)

    assert result == ""
    assert gate.is_silent
    assert gate.is_declined
    [decision] = reply.model.requests
    assert decision.tool_choice == "none"
    assert decision.stream is False
    assert reply.storage.list_runs(SESSION_ID) == []
    assert reply.recorder.outcome == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_an_approving_participation_decision_reuses_the_reply_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    streaming: bool,
) -> None:
    """The decision request is the reply's exact messages plus the decision prompt, with tool selection disabled."""
    del probe
    reply = _reply(
        tmp_path,
        monkeypatch,
        [answer('{"action":"respond","reason":"A direct question."}'), answer("Useful answer")],
    )
    gate = ParticipationGate(instructions="Offer concise technical help.")

    result = _text(await _stream(reply, participation=gate)) if streaming else await _respond(reply, participation=gate)

    assert result == "Useful answer"
    assert gate.approved
    decision, answer_request = reply.model.requests
    assert _contents(decision.messages[:-1]) == _contents(answer_request.messages)
    assert decision.messages[-1].role == "user"
    assert "Offer concise technical help." in str(decision.messages[-1].content)
    assert decision.tools == answer_request.tools
    assert (decision.tool_choice, decision.stream) == ("none", False)
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
    assert _text(chunks) == "Working. " * 3
    assert _stored(reply).status is RunStatus.completed
    assert [
        (entry["entity"], entry["budget"], entry["model_requests"])
        for entry in logs
        if entry["event"] == "tool_call_limit_reached"
    ] == [("assistant", 1, 3)]


@pytest.mark.asyncio
async def test_an_approval_receipt_is_projected_into_the_first_request_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """The trusted receipt reaches the model once, on the system message, and never enters the stored run."""
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("touch", "call_1", label="a")), answer("ok")])

    with approval_receipt_context("Trusted approval receipt."):
        await _stream(reply)

    assert probe.calls == ["a"]
    first, second = reply.model.requests
    assert str(first.messages[0].content).endswith("Trusted approval receipt.")
    assert first.messages[0].provider_data == {"mindroom_approval_receipt": True}
    assert "Trusted approval receipt." not in str(second.messages[0].content)
    assert all("Trusted approval receipt." not in str(message.content) for message in _stored(reply).messages)


@pytest.mark.asyncio
async def test_the_hooks_of_a_reply_are_installed_in_their_documented_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """Participation decides first, compaction rewrites next, the receipt projects last; the other stages are wired."""
    del probe
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
    assert hooks.answer_tool_calls is None
    assert hooks.rewrite_tool_calls is None
    assert len(hooks.tool_middleware) == 1
    assert len(hooks.after_tool_batch) == 1
    assert len(hooks.on_resume_entry) == 1
    assert hooks.on_loop_end is not None
    assert captured["request_template"].native_compaction is None
    assert captured["request_template"].stream is True


@pytest.mark.asyncio
@pytest.mark.parametrize("replays_history", [True, False])
async def test_mid_turn_compaction_folds_into_the_scope_only_when_the_turn_replays_its_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    replays_history: bool,
) -> None:
    """A reply that replays the scope's history binds its folds to the scope; one without replay compacts run-locally.

    A scheduled turn whose history budget disabled replay deliberately carries no stored history, so a fold must not
    append a scope summary row or inject the scope's summary into its request.
    """
    del probe
    reply = _reply(tmp_path, monkeypatch, [answer("ok")])
    bound: dict[str, Any] = {}
    original = loop_hooks.mid_turn_compaction_hook

    def capturing(**kwargs: Any) -> Any:  # noqa: ANN401
        bound.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(loop_hooks, "mid_turn_compaction_hook", capturing)
    budget = None if replays_history else ScheduledHistoryBudget(limit=0, source_event_id=EVENT_ID)

    chunks = await _stream(reply, scheduled_history_budget=budget)

    assert _text(chunks) == "ok"
    assert bound["store"] is not None
    if replays_history:
        assert bound["session"] is not None
        assert bound["scope"] == HistoryScope(kind="agent", scope_id="assistant")
    else:
        assert bound["session"] is None
        assert bound["scope"] is None


@pytest.mark.asyncio
async def test_a_reply_posts_its_mid_turn_compaction_notices_and_seeds_no_earlier_fold_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A fold during the reply reaches its notices and its turn recorder through the owned summarizer.

    The run's metadata keeps what the turn seeded but none of the folded usage of the run it follows.
    """
    del probe
    reply = _reply(
        tmp_path,
        monkeypatch,
        # The first response reports a request far over the 4,000-token window, so the second request compacts.
        [calls(tool_call("touch", "call_1", label="a"), metrics=usage(1_000_000, 1)), answer("done")],
        context_window=4000,
    )
    summarizer = summary_model("SUMMARY")
    monkeypatch.setattr("mindroom.history.compaction.build_model_provider", lambda *_args: summarizer)
    lifecycle = RecordingCompactionLifecycle()
    seeded = {COMPACTED_REQUESTS_METADATA_KEY: [{"input_tokens": 10}], "room_topic": "kept"}

    chunks = await _stream(reply, compaction_lifecycle=lifecycle, matrix_run_metadata=seeded)

    assert _text(chunks) == "done"
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
    """On a Claude route, calls of a response the output limit cut off are answered, not run, and the model retries."""
    reply = _reply(
        tmp_path,
        monkeypatch,
        [_cut_off(tool_call("touch", "call_1", label="huge")), answer("Split into parts.")],
        capabilities=Capabilities(claude_family=True),
    )

    chunks = await _stream(reply)

    assert probe.calls == []
    assert _text(chunks) == "Split into parts."
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
        capabilities=Capabilities(claude_family=True),
    )

    chunks = await _stream(reply)

    assert probe.calls == []
    assert len(reply.model.requests) == 3
    assert [chunk for chunk in chunks if isinstance(chunk, str)]
    record = _stored(reply)
    assert record.status is RunStatus.error
    assert "output limit" in str(record.content)


@pytest.mark.asyncio
async def test_a_safeguard_refusal_is_stored_and_reported_as_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A refused response fails the run with the refusal text the runtime recognizes."""
    del probe
    reply = _reply(tmp_path, monkeypatch, [[_raising(ModelSafeguardRefusal(provider="anthropic"))]])

    chunks = await _stream(reply)

    assert [chunk for chunk in chunks if isinstance(chunk, str)]
    record = _stored(reply)
    assert record.status is RunStatus.error
    assert record.content == MODEL_SAFEGUARD_REFUSAL_MESSAGE


@pytest.mark.asyncio
async def test_session_state_a_tool_changes_is_saved_with_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """The shared ``session_state`` a tool writes lands on the stored session."""
    del probe
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("remember", "call_1", label="blue")), answer("Kept.")])

    await _stream(reply)

    session = reply.storage.load_session(SESSION_ID, "agent")
    assert session is not None
    assert session.session_state == {"remembered": "blue"}


@pytest.mark.asyncio
async def test_the_skill_review_capture_keeps_the_final_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """A response that counts toward skill learning leaves its final request, ending in the model's answer."""
    del probe
    reply = _reply(tmp_path, monkeypatch, [calls(tool_call("touch", "call_1", label="a")), answer("Final answer.")])
    capture = SkillReviewCapture()

    await _stream(reply, skill_review_capture=capture)

    assert capture.latest is not None
    assert (capture.latest.run_id, capture.latest.model_name) == ("run-1", "default")
    assert capture.latest.messages[-1].role == "assistant"
    assert capture.latest.messages[-1].content == "Final answer."
    assert {"remember", "touch"} <= {tool.name for tool in capture.latest.tools}


def _runner(reply: _Reply, tmp_path: Path) -> ResponseRunner:
    """Build the agent's bot on the reply's config, with a mocked Matrix client, and return its response runner."""
    with_current_room_member_access(reply.config)
    agent_user = AgentMatrixUser(
        agent_name="assistant",
        password=TEST_PASSWORD,
        display_name="Assistant",
        user_id="@mindroom_assistant:localhost",
    )
    bot = make_test_agent_bot(agent_user, tmp_path, reply.config, reply.runtime_paths, rooms=[ROOM_ID])
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
) -> tuple[Any, list[str]]:
    """Run one reply through the runner's turn, in its reply span, and return the outcome and the attempt run ids."""
    run_ids: list[str] = []
    respond = runner._process_and_respond_streaming if streaming else runner._process_and_respond
    async with response_span(runner, request):
        outcome = await respond(request, attempt_run_id_collector=run_ids)
    return outcome, run_ids


def _interrupted_record_spy(monkeypatch: pytest.MonkeyPatch, storage: ConversationStore) -> list[RunStatus | None]:
    """Record the stored status of the run each time the runner writes its interrupted-turn record."""
    statuses: list[RunStatus | None] = []
    original = response_runner.persist_interrupted_replay_snapshot

    def spy(**kwargs: Any) -> None:  # noqa: ANN401
        stored = storage.get_run(kwargs["run_id"])
        statuses.append(stored.status if stored is not None else None)
        original(**kwargs)

    monkeypatch.setattr(response_runner, "persist_interrupted_replay_snapshot", spy)
    return statuses


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_the_runner_writes_the_interrupted_record_after_the_loop_stored_the_cancelled_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
    streaming: bool,
) -> None:
    """Through ``ResponseRunner``, a stop mid-stream stores the ``CANCELLED`` run before the interrupted record.

    The lifecycle owner writes the interrupted-turn record over the cancelled run the loop stored, and the cut-off
    request's usage counts once.
    """
    del probe
    reply = _reply(
        tmp_path,
        monkeypatch,
        [[TextDelta("Partial "), UsageReported(usage(10, 2), final=False), Await(_cancel_current_task)]],
    )
    runner = _runner(reply, tmp_path)
    statuses_when_interrupted_record_written = _interrupted_record_spy(monkeypatch, reply.storage)

    task = asyncio.create_task(_respond_through_runner(runner, _request(), streaming=streaming))
    [result] = await asyncio.gather(task, return_exceptions=True)

    assert not isinstance(result, BaseException), result
    outcome, run_ids = result
    assert outcome.delivery.terminal_status == "cancelled"
    assert outcome.run_succeeded is False
    [run_id] = run_ids
    assert statuses_when_interrupted_record_written == [RunStatus.cancelled]
    record = reply.storage.get_run(run_id)
    assert record is not None
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "cancelled"
    [row] = _usage_rows(reply.storage, run_id)
    assert row["metrics"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_the_runner_leaves_a_paused_run_as_the_loop_stored_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[_ProbeToolkit],
) -> None:
    """Through ``ResponseRunner``, an approval pause propagates with its ``PAUSED`` row intact.

    The lifecycle owner writes no interrupted-turn record over a suspended turn.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="dangerous"), metrics=usage(7, 1))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    runner = _runner(reply, tmp_path)
    statuses_when_interrupted_record_written = _interrupted_record_spy(monkeypatch, reply.storage)

    with pytest.raises(ResponsePausedForApproval) as raised:
        await _respond_through_runner(runner, _request(), streaming=False)

    assert probe.calls == []
    assert statuses_when_interrupted_record_written == []
    paused = raised.value.paused
    record = reply.storage.get_run(paused.run_id)
    assert record is not None
    assert record.status is RunStatus.paused
    assert [requirement.needs_confirmation for requirement in record.requirements or []] == [True]
    [row] = _usage_rows(reply.storage, paused.run_id)
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
    record, so it no longer waits for an approval that never comes. The blocking driver also raises the failure
    through the runner.
    """
    reply = _reply(
        tmp_path,
        monkeypatch,
        [calls(tool_call("touch", "call_1", label="outside"), metrics=usage(7, 1))],
        tools=(EXTERNAL_PROBE_TOOL,),
    )
    runner = _runner(reply, tmp_path)
    presence = MagicMock(spec=nio.PresenceGetResponse)
    presence.presence = "online" if streaming else "offline"
    presence.last_active_ago = 0
    runner.deps.runtime.client.get_presence = AsyncMock(return_value=presence)

    if streaming:
        await runner.generate_response(_request())
    else:
        with pytest.raises(RuntimeError, match="without an approval to ask for"):
            await runner.generate_response(_request())

    assert probe.calls == []
    session = reply.storage.load_session(SESSION_ID, "agent")
    assert session is not None
    [record] = session.runs
    assert record.status is RunStatus.completed
    assert record.metadata is not None
    assert record.metadata["mindroom_original_status"] == "error"
    assert record.metadata["mindroom_replay_state"] == "interrupted"
    assert not record.requirements
