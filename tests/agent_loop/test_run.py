"""Agent runs end in exactly one stored record, and approval resumes run exactly the approved calls.

The resume tests continue paused runs Agno stored (``run-paused-approval``, the ``team-paused`` member, and
``run-paused-delegation`` of the storage goldens) through ``resume_agent``, as a run paused before the upgrade resumes
after it, and assert the resumed records as typed values.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

import pytest

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, NO_HOOKS, LoopHooks
from mindroom.agent_loop.run import resume_agent, run_agent
from mindroom.agent_loop.spec import AgentSpec, RunContext, UnstoredSession
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.event_journal.approval_calls import ApprovalCall, approval_arguments_digest
from mindroom.history.request_messages import build_resume_request
from mindroom.llm.errors import RateLimited
from mindroom.llm.events import RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity, TextDelta, UsageReported
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.tool_function import ToolGate
from tests.agent_loop.helpers import add, boom, collect, context, echo, external, gated, spec, toolkit
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.run_record_helpers import PRODUCT, approved_call, denied_call, result, resumed, settled, waiting_call
from tests.session_storage.goldens import loaded_sessions

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.run import RunContext as AgnoRunContext

    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolkitClose


def _prompt() -> list[Message]:
    return [Message(role="user", content="hi")]


class _Store:
    """A persist callback that remembers every record it was handed, as a snapshot."""

    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    async def __call__(self, record: RunRecord) -> None:
        await asyncio.sleep(0)
        self.saved.append(record.to_dict())


# --- fresh runs ------------------------------------------------------------------------------------------------------


class _FailingFirstStore(_Store):
    """A persist callback whose first store fails, as a full disk fails it, and whose later stores succeed."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def __call__(self, record: RunRecord) -> None:
        if not self.failed:
            self.failed = True
            msg = "disk full"
            raise OSError(msg)
        await super().__call__(record)


@pytest.mark.parametrize("carried", [None, 5.0])
@pytest.mark.asyncio
async def test_a_run_whose_terminal_store_fails_counts_its_time_once(
    monkeypatch: pytest.MonkeyPatch,
    carried: float | None,
) -> None:
    """A run whose completed record fails to store is stored as failed, its duration still counting its leg once."""
    readings = iter([100.0])
    monkeypatch.setattr("mindroom.agent_loop.run.time", SimpleNamespace(monotonic=lambda: next(readings, 103.0)))
    store = _FailingFirstStore()
    paused = _paused_record(_gated_add()) if carried is not None else None
    if paused is not None:
        paused.metrics = RunMetrics(duration=carried)
        events = resume_agent(
            spec(ScriptedModel([answer("Three.")]), gated(add)),
            paused,
            build_resume_request(paused=paused.messages, summary=None, history=[]),
            approved=[_approval("c1", "add", {"a": 1, "b": 2})],
            denied={},
            external_results={},
            context=lambda run_id: context(run_id=run_id, session_id="session-1"),
            hooks=NO_HOOKS,
            persist=store,
            request_template=EMPTY_REQUEST,
        )
    else:
        events = run_agent(
            spec(ScriptedModel([answer("Done.")])),
            _prompt(),
            ctx=context(),
            persist=store,
            request_template=EMPTY_REQUEST,
        )

    with pytest.raises(OSError, match="disk full"):
        await collect(events)

    [stored] = store.saved
    assert stored["status"] == "ERROR"
    assert stored["metrics"]["duration"] == (carried or 0.0) + 3.0


@pytest.mark.asyncio
async def test_a_completed_run_is_stored_once_before_it_is_announced() -> None:
    store = _Store()
    model = ScriptedModel(
        [calls(tool_call("add", "c1", a=2, b=3), metrics=usage(10, 2)), answer("Five.", usage(20, 4))],
    )

    events, record = await collect(
        run_agent(
            spec(model, toolkit(add)),
            _prompt(),
            ctx=context(),
            persist=store,
            request_template=EMPTY_REQUEST,
            metadata={"room_id": "!r"},
        ),
    )

    assert isinstance(events[-1], RunFinished)
    assert len(store.saved) == 1
    assert store.saved[0] == record.to_dict()
    stored = store.saved[0]
    assert (stored["status"], stored["content"], stored["agent_id"], stored["agent_name"]) == (
        "COMPLETED",
        "Five.",
        "code",
        "Code",
    )
    assert (stored["model"], stored["model_provider"], stored["user_id"], stored["session_id"]) == (
        "scripted-model",
        "Synthetic",
        "@alice:example.org",
        "session-1",
    )
    assert stored["metadata"] == {"room_id": "!r"}
    assert [message["role"] for message in stored["messages"]] == ["system", "user", "assistant", "tool", "assistant"]
    assert stored["metrics"]["total_tokens"] == 36
    assert stored["metrics"]["duration"] > 0
    assert [tool["tool_call_id"] for tool in stored["tools"]] == ["c1"]
    assert "requirements" not in stored


def remember(note: str, run_context: AgnoRunContext) -> str:
    """Keep a note in the run's session state.

    Args:
        note: What to keep.
        run_context: The run's context, which the loop injects.

    """
    assert run_context.session_state is not None
    run_context.session_state["note"] = note
    return "kept"


def recall(run_context: AgnoRunContext) -> str:
    """Return the note the run's session state keeps.

    Args:
        run_context: The run's context, which the loop injects.

    """
    assert run_context.session_state is not None
    return str(run_context.session_state.get("note"))


@pytest.mark.asyncio
async def test_the_calls_of_a_run_no_store_keeps_share_its_session_s_state() -> None:
    session = UnstoredSession("session-1")
    model = ScriptedModel(
        [calls(tool_call("remember", "c1", note="blue")), calls(tool_call("recall", "c2")), answer("Blue.")],
    )

    _, record = await collect(
        run_agent(
            spec(model, toolkit(remember, recall)),
            _prompt(),
            ctx=RunContext(run_id="run-1", session=session),
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )

    assert [tool.result for tool in record.tools or ()] == ["kept", "blue"]
    assert session.session_state == {"note": "blue"}
    assert record.session_id == "session-1"


@pytest.mark.asyncio
async def test_stored_messages_leave_out_history_copies_and_transient_context() -> None:
    model = ScriptedModel([answer("ok")])
    history = Message(role="user", content="earlier", from_history=True)
    transient = Message(role="user", content="context", add_to_agent_memory=False)

    _, record = await collect(
        run_agent(
            spec(model),
            [history, *_prompt(), transient],
            ctx=context(),
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )

    assert [message.content for message in record.messages] == ["You are Code.", "hi", "ok"]
    assert model.requests[0].messages[1] is history
    assert model.requests[0].messages[-1] is transient


@pytest.mark.asyncio
async def test_a_paused_run_whose_model_said_nothing_is_stored_paused_without_content() -> None:
    store = _Store()
    model = ScriptedModel([calls(tool_call("add", "c1", a=6, b=7), metrics=usage(1, 1))])

    _, record = await collect(
        run_agent(spec(model, gated(add)), _prompt(), ctx=context(), persist=store, request_template=EMPTY_REQUEST),
    )

    assert record.status is RunStatus.paused
    # The pause is the record's requirements; no notice stands in for text the model did not write.
    assert record.content is None
    assert "content" not in store.saved[0]
    assert record.requirements is not None
    assert record.requirements[0].needs_confirmation
    assert store.saved[0]["status"] == "PAUSED"
    assert [message["role"] for message in store.saved[0]["messages"]] == ["system", "user", "assistant"]


@pytest.mark.asyncio
async def test_a_paused_run_keeps_the_text_the_model_streamed() -> None:
    model = ScriptedModel(
        [calls(tool_call("add", "c1", a=6, b=7), content="I need approval first.", metrics=usage(1, 1))],
    )

    _, record = await collect(
        run_agent(spec(model, gated(add)), _prompt(), ctx=context(), persist=None, request_template=EMPTY_REQUEST),
    )

    assert record.content == "I need approval first."


@pytest.mark.asyncio
async def test_a_silent_external_pause_has_no_content() -> None:
    model = ScriptedModel([calls(tool_call("echo", "c1", text="task"), metrics=usage(1, 1))])

    _, record = await collect(
        run_agent(spec(model, external(echo)), _prompt(), ctx=context(), persist=None, request_template=EMPTY_REQUEST),
    )

    assert (record.status, record.content) == (RunStatus.paused, None)


@pytest.mark.asyncio
async def test_a_provider_error_stores_an_error_record_with_the_error_text_and_reraises() -> None:
    store = _Store()

    async def throttle() -> None:
        msg = "Rate limited"
        raise RateLimited(msg, provider="Synthetic")

    model = ScriptedModel([[UsageReported(usage(5, 0), final=False), Await(throttle)]])

    with pytest.raises(RateLimited, match="Rate limited"):
        async for _ in run_agent(spec(model), _prompt(), ctx=context(), persist=store, request_template=EMPTY_REQUEST):
            pass

    [stored] = store.saved
    assert (stored["status"], stored.get("content"), stored["error"]) == ("ERROR", None, "Rate limited")
    assert [message["role"] for message in stored["messages"]] == ["system", "user", "assistant"]
    assert stored["messages"][-1]["metrics"] == {"input_tokens": 5, "total_tokens": 5}
    assert stored["metrics"]["input_tokens"] == 5
    assert "tools" not in stored


@pytest.mark.asyncio
async def test_a_failed_run_ends_its_stream_with_its_stored_record_then_raises() -> None:
    """A failed run's record ends its stream as a finished run's does: stored first, then the failure is raised."""
    log: list[str] = []

    async def persist(record: RunRecord) -> None:
        log.append(f"stored {record.status.value}")

    async def throttle() -> None:
        msg = "Rate limited"
        raise RateLimited(msg, provider="Synthetic")

    events = run_agent(
        spec(ScriptedModel([[Await(throttle)]])),
        _prompt(),
        ctx=context(),
        persist=persist,
        request_template=EMPTY_REQUEST,
    )
    finished: list[RunRecord] = []

    async def consume() -> None:
        async for event in events:
            assert isinstance(event, RunFinished)
            log.append("finished")
            assert isinstance(event.record, RunRecord)
            finished.append(event.record)

    with pytest.raises(RateLimited, match="Rate limited"):
        await consume()

    assert log == ["stored ERROR", "finished"]
    [record] = finished
    assert (record.status, record.error) == (RunStatus.error, "Rate limited")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "text"),
    [(RuntimeError("store unavailable"), "store unavailable"), (TimeoutError(), "TimeoutError")],
    ids=["with_text", "without_text"],
)
async def test_a_failed_run_records_why_it_failed(error: Exception, text: str) -> None:
    """A run that raises stores the exception's text, or its type name when it has none, as its error, not its content."""
    store = _Store()

    async def fail() -> None:
        raise error

    model = ScriptedModel([[Await(fail)]])

    with pytest.raises(type(error)):
        async for _ in run_agent(spec(model), _prompt(), ctx=context(), persist=store, request_template=EMPTY_REQUEST):
            pass

    [stored] = store.saved
    assert (stored["status"], stored.get("content"), stored["error"]) == ("ERROR", None, text)
    # A loaded record hands over why the run failed, as the live one did.
    assert RunRecord.from_dict(stored).handover_content == text


@pytest.mark.asyncio
async def test_a_failed_run_stores_its_failure_text_only_with_credentials_redacted() -> None:
    store = _Store()

    async def fail() -> None:
        msg = "provider down: api_key=unit-test-secret"
        raise RuntimeError(msg)

    model = ScriptedModel([[Await(fail)]])

    with pytest.raises(RuntimeError):
        async for _ in run_agent(spec(model), _prompt(), ctx=context(), persist=store, request_template=EMPTY_REQUEST):
            pass

    [stored] = store.saved
    assert stored["error"] == "provider down: api_key=***redacted***"
    assert "unit-test-secret" not in json.dumps(stored)


@pytest.mark.asyncio
async def test_an_error_after_text_keeps_the_text_as_content() -> None:
    store = _Store()

    async def fail() -> None:
        msg = "boom"
        raise RateLimited(msg, provider="Synthetic")

    model = ScriptedModel(
        [calls(tool_call("echo", "c1", text="x"), content="Working.", metrics=usage(1, 1)), [Await(fail)]],
    )

    with pytest.raises(RateLimited):
        async for _ in run_agent(
            spec(model, toolkit(echo)),
            _prompt(),
            ctx=context(),
            persist=store,
            request_template=EMPTY_REQUEST,
        ):
            pass

    assert (store.saved[0]["status"], store.saved[0]["content"], store.saved[0]["error"]) == (
        "ERROR",
        "Working.",
        "boom",
    )


@pytest.mark.asyncio
async def test_cancellation_mid_stream_stores_a_cancelled_record_once_before_the_caller_writes() -> None:
    streaming = asyncio.Event()
    hold = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        streaming.set()
        await hold.wait()

    model = ScriptedModel(
        [
            calls(tool_call("add", "c1", a=6, b=7), metrics=usage(1, 1)),
            [TextDelta("Par"), UsageReported(usage(50, 2), final=False), Await(wait_to_be_cancelled)],
        ],
    )
    writes: list[str] = []
    saved: list[dict[str, Any]] = []

    async def persist(record: RunRecord) -> None:
        # A slow store: the loop must finish it even though its waiter has been cancelled.
        await asyncio.sleep(0.01)
        saved.append(record.to_dict())
        writes.append("loop")

    async def consume() -> None:
        try:
            async for _ in run_agent(
                spec(model, toolkit(add)),
                _prompt(),
                ctx=context(),
                persist=persist,
                request_template=EMPTY_REQUEST,
            ):
                pass
        except asyncio.CancelledError:
            writes.append("caller")  # MindRoom's interrupted-turn record for the same run goes here.
            raise

    task = asyncio.create_task(consume())
    await asyncio.wait_for(streaming.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert writes == ["loop", "caller"]
    [stored] = saved
    assert stored["status"] == "CANCELLED"
    assert "content" not in stored  # Nothing completed; no boilerplate text replaces the partial stream.
    assert [message["role"] for message in stored["messages"]] == ["system", "user", "assistant", "tool", "assistant"]
    assert stored["messages"][-1]["metrics"] == {"input_tokens": 50, "output_tokens": 2, "total_tokens": 52}
    assert {key: stored["metrics"][key] for key in ("input_tokens", "output_tokens", "total_tokens")} == {
        "input_tokens": 51,
        "output_tokens": 3,
        "total_tokens": 54,
    }
    assert [(tool["tool_call_id"], tool["result"]) for tool in stored["tools"]] == [("c1", '{"sum": 13}')]


@pytest.mark.asyncio
async def test_closing_the_run_while_handling_an_event_stores_a_cancelled_record_before_aclose_returns() -> None:
    model = ScriptedModel([[UsageReported(usage(50, 2), final=False), TextDelta("Par"), Await(asyncio.Event().wait)]])
    store = _Store()

    stream = run_agent(spec(model), _prompt(), ctx=context(), persist=store, request_template=EMPTY_REQUEST)
    async for _ in stream:
        break  # A stop that lands while the consumer handles an event closes the run instead of cancelling it.
    assert store.saved == []
    await stream.aclose()

    [stored] = store.saved
    assert stored["status"] == "CANCELLED"
    assert "content" not in stored
    assert stored["messages"][-1]["metrics"] == {"input_tokens": 50, "output_tokens": 2, "total_tokens": 52}
    assert {key: stored["metrics"][key] for key in ("input_tokens", "output_tokens", "total_tokens")} == {
        "input_tokens": 50,
        "output_tokens": 2,
        "total_tokens": 52,
    }


@pytest.mark.asyncio
async def test_a_stop_while_the_paused_record_is_being_saved_leaves_a_cancelled_record() -> None:
    model = ScriptedModel([calls(tool_call("add", "c1", a=6, b=7), metrics=usage(1, 1))])
    saved: list[dict[str, Any]] = []
    task: asyncio.Task[None] | None = None

    async def persist(record: RunRecord) -> None:
        saved.append(record.to_dict())
        if record.status is RunStatus.paused:
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

    async def consume() -> None:
        async for _ in run_agent(
            spec(model, gated(add)),
            _prompt(),
            ctx=context(),
            persist=persist,
            request_template=EMPTY_REQUEST,
        ):
            pass

    task = asyncio.create_task(consume())
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [record["status"] for record in saved] == ["PAUSED", "CANCELLED"]
    cancelled = saved[-1]
    # Cancellation wins over the pause: the requirement leaves and the tool entry stops waiting.
    assert cancelled["requirements"] == []
    tool = ToolExecution.from_dict(cancelled["tools"][0])
    assert (tool.requires_confirmation, tool.confirmed) == (False, None)
    assert "content" not in cancelled


@pytest.mark.asyncio
async def test_a_failing_store_fails_a_completed_run_but_not_an_interrupted_one() -> None:
    async def broken(record: RunRecord) -> None:
        del record
        msg = "disk full"
        raise OSError(msg)

    with pytest.raises(OSError, match="disk full"):
        await collect(
            run_agent(
                spec(ScriptedModel([answer("ok")])),
                _prompt(),
                ctx=context(),
                persist=broken,
                request_template=EMPTY_REQUEST,
            ),
        )

    async def fail() -> None:
        msg = "boom"
        raise RateLimited(msg, provider="Synthetic")

    with pytest.raises(RateLimited):
        async for _ in run_agent(
            spec(ScriptedModel([[Await(fail)]])),
            _prompt(),
            ctx=context(),
            persist=broken,
            request_template=EMPTY_REQUEST,
        ):
            pass


@pytest.mark.asyncio
async def test_a_run_without_a_system_message_sends_only_its_input() -> None:
    model = ScriptedModel([answer("ok")])
    agent = replace(spec(model), system_messages=())

    _, record = await collect(run_agent(agent, _prompt(), ctx=context(), persist=None, request_template=EMPTY_REQUEST))

    assert [message.role for message in model.requests[0].messages] == ["user"]
    assert [message.role for message in record.messages] == ["user", "assistant"]


# --- resumes against the storage goldens ----------------------------------------------------------------------------


def _golden_run(store: str, session_id: str, run_id: str) -> dict[str, Any]:
    [run] = [run for run in loaded_sessions()[store][session_id]["runs"] if run["run_id"] == run_id]
    return run


def _gated_calculator() -> AdaptedToolkit:
    """MindRoom's calculator with ``multiply`` under the approval policy, as the capture's config gated it."""
    adapted = adapt_toolkit(CalculatorTools())
    return replace(
        adapted,
        functions=tuple(
            replace(function, gate=ToolGate.POLICY) if function.name == "multiply" else function
            for function in adapted.functions
        ),
    )


def _delegate() -> AdaptedToolkit:
    def run_subagent(agent_name: str, task: str) -> str:
        """Delegate a task to another agent.

        Args:
            agent_name: The agent to run.
            task: What it should do.

        """
        msg = f"{agent_name} must run outside the loop: {task}"
        raise AssertionError(msg)

    return external(run_subagent)


def _approval(call_id: str, name: str, arguments: dict[str, Any], *, agent: str = "code") -> ApprovalCall:
    return ApprovalCall(
        tool_call_id=call_id,
        tool_name=name,
        invoking_agent=agent,
        expires_at_ns=0,
        toolkit_name="calculator",
        arguments_digest=approval_arguments_digest(arguments),
    )


def _agent(
    model: ScriptedModel,
    toolkits: Sequence[AdaptedToolkit],
    *,
    agent_id: str = "code",
    name: str = "Code",
) -> AgentSpec:
    return AgentSpec(id=agent_id, name=name, model=model, system_messages=(), toolkits=tuple(toolkits))


def _model(model_id: str, *turns: list[Any]) -> ScriptedModel:
    return ScriptedModel(
        list(turns),
        identity=ModelIdentity(id=model_id, provider="Synthetic"),
    )


async def _resume(
    agent: AgentSpec,
    paused: RunRecord,
    *,
    approved: Sequence[ApprovalCall] = (),
    denied: dict[str, str | None] | None = None,
    external_results: dict[str, str] | None = None,
    hooks: LoopHooks = NO_HOOKS,
) -> tuple[list[Any], RunRecord, _Store]:
    store = _Store()
    events, record = await collect(
        resume_agent(
            agent,
            paused,
            build_resume_request(paused=paused.messages, summary=None, history=[]),
            approved=approved,
            denied=denied or {},
            external_results=external_results or {},
            context=lambda run_id: context(run_id=run_id, session_id=paused.session_id or "session"),
            hooks=hooks,
            persist=store,
            request_template=EMPTY_REQUEST,
        ),
    )
    return events, record, store


@pytest.mark.asyncio
async def test_an_approved_call_resumes_into_a_completed_run_with_its_result() -> None:
    paused = RunRecord.from_dict(_golden_run("agent_code", "!room:example.org:$conversation", "run-paused-approval"))
    before = paused.to_dict()
    answered = ("6 x 7 = 42.", usage(260, 9, time_to_first_token=0.25))
    model = _model("code-model", answer(*answered))

    events, record, store = await _resume(
        _agent(model, [_gated_calculator()]),
        paused,
        approved=[_approval("call-multiply", "multiply", {"a": 6, "b": 7})],
    )

    call, waiting = waiting_call(paused)
    assert settled(record) == settled(
        resumed(
            paused,
            decided=approved_call(waiting, PRODUCT),
            added=[result(call, PRODUCT)],
            answered=answered,
            confirmation=True,
        ),
    )
    assert record.messages[: len(paused.messages)] == paused.messages
    assert len(record.messages) == len(paused.messages) + 2
    assert store.saved == [record.to_dict()]
    assert paused.to_dict() == before
    assert [type(event).__name__ for event in events[:2]] == ["ToolStarted", "ToolCompleted"]
    assert isinstance(events[0], ToolStarted)
    assert events[0].call == replace(record.tools[0].call, result=None, is_error=None)  # type: ignore[index]
    # The resume request repeats the paused request, system message first.
    assert [message.role for message in model.requests[0].messages] == ["system", "user", "assistant", "tool"]
    assert model.requests[0].messages[-1].content == '{"operation": "multiplication", "result": 42.0}'


@pytest.mark.asyncio
async def test_a_denied_call_resumes_into_a_completed_run_that_answers_it_with_the_reason() -> None:
    paused = RunRecord.from_dict(_golden_run("agent_code", "!room:example.org:$conversation", "run-paused-approval"))
    answered = ("I did not multiply, because the call was declined.", usage(260, 9, time_to_first_token=0.25))
    model = _model("code-model", answer(*answered))
    reason = "Alice declined the multiplication."

    events, record, _ = await _resume(_agent(model, [_gated_calculator()]), paused, denied={"call-multiply": reason})

    call, waiting = waiting_call(paused)
    assert settled(record) == settled(
        resumed(
            paused,
            decided=denied_call(waiting, reason),
            added=[result(call, reason, is_error=True)],
            answered=answered,
            confirmation=False,
            confirmation_note=reason,
        ),
    )
    assert not any(isinstance(event, ToolStarted | ToolCompleted) for event in events)
    assert model.requests[0].messages[-1].content == "Alice declined the multiplication."


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, ""])
async def test_a_call_denied_without_a_reason_tells_the_model_it_was_not_approved(reason: str | None) -> None:
    paused = RunRecord.from_dict(_golden_run("agent_code", "!room:example.org:$conversation", "run-paused-approval"))
    model = _model("code-model", answer("I did not multiply.", usage(260, 9, time_to_first_token=0.25)))

    _, record, _ = await _resume(_agent(model, [_gated_calculator()]), paused, denied={"call-multiply": reason})

    assert model.requests[0].messages[-1].content == "Not approved by requester"
    assert record.tools[0].confirmation_note == "Not approved by requester"  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_team_member_s_approved_call_resumes_into_the_member_s_completed_run() -> None:
    paused = RunRecord.from_dict(
        _golden_run("team_squad", "!room:example.org:$squad", "00000000-0000-4000-8000-00000000005a"),
    )
    answered = ("42", usage(95, 1, time_to_first_token=0.25))
    model = _model("checker-model", answer(*answered))

    _, record, _ = await _resume(
        _agent(model, [_gated_calculator()], agent_id="checker", name="Checker"),
        paused,
        approved=[_approval("call-checker-multiply", "multiply", {"a": 6, "b": 7}, agent="checker")],
    )

    call, waiting = waiting_call(paused)
    assert settled(record) == settled(
        resumed(
            paused,
            decided=approved_call(waiting, PRODUCT),
            added=[result(call, PRODUCT)],
            answered=answered,
            confirmation=True,
        ),
    )
    assert record.parent_run_id == "team-paused"


@pytest.mark.asyncio
async def test_an_external_result_resumes_into_a_completed_run_that_answers_the_call_with_it() -> None:
    paused = RunRecord.from_dict(_golden_run("agent_code", "!room:example.org:$delegation", "run-paused-delegation"))
    answered = ("The helper found 42.", usage(330, 6, time_to_first_token=0.25))
    model = _model("code-model", answer(*answered))
    helper_result = "42\n\nSubagent ID: 0123"

    events, record, _ = await _resume(
        _agent(model, [_delegate()]),
        paused,
        external_results={"call-delegate": helper_result},
    )

    call, waiting = waiting_call(paused)
    # An externally executed call's result answers it as is, never as a failure.
    executed = replace(waiting, result=helper_result, external_execution_required=False)
    assert settled(record) == settled(
        resumed(
            paused,
            decided=executed,
            added=[result(call, helper_result, is_error=None)],
            answered=answered,
            external_execution_result=helper_result,
        ),
    )
    assert not any(isinstance(event, ToolStarted | ToolCompleted) for event in events)


@pytest.mark.asyncio
async def test_an_approved_call_resumes_with_the_string_arguments_it_paused_with() -> None:
    received: list[dict[str, object]] = []

    def note(
        text: str,
        scope: Literal["none", "all"] | None = None,
        title: str | None = None,
        pinned: bool = False,
    ) -> str:
        """Record a note.

        Args:
            text: The note.
            scope: Who sees it.
            title: An optional title.
            pinned: Whether it stays on top.

        """
        received.append({"text": text, "scope": scope, "title": title, "pinned": pinned})
        return "noted"

    sent = {"text": "None", "scope": "none", "title": "null", "pinned": "true"}
    stored = {"text": "None", "scope": "none", "title": None, "pinned": True}
    paused_model = ScriptedModel([calls(tool_call("note", "c1", **sent), metrics=usage(1, 1))])
    _, paused = await collect(
        run_agent(
            spec(paused_model, gated(note)),
            _prompt(),
            ctx=context(),
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )
    assert paused.status is RunStatus.paused
    assert paused.tools is not None
    assert paused.tools[0].tool_args == stored

    _, record, _ = await _resume(
        spec(ScriptedModel([answer("Noted.")]), gated(note)),
        paused,
        approved=[_approval("c1", "note", stored)],
    )

    assert record.status is RunStatus.completed
    assert received == [stored]


@pytest.mark.parametrize("second_decision", ["approve", "deny"])
@pytest.mark.asyncio
async def test_a_second_resume_answers_only_the_calls_it_decided(second_decision: str) -> None:
    executed: list[tuple[int, int]] = []

    def add(a: int, b: int) -> str:
        """Add two integers.

        Args:
            a: First number.
            b: Second number.

        """
        executed.append((a, b))
        return str(a + b)

    model = ScriptedModel(
        [
            calls(tool_call("add", "c1", a=1, b=2), metrics=usage(1, 1)),
            calls(tool_call("add", "c2", a=3, b=4), metrics=usage(1, 1)),
            answer("done", usage(1, 1)),
        ],
    )
    agent = spec(model, gated(add))
    _, first = await collect(
        run_agent(
            agent,
            [Message(role="user", content="go")],
            ctx=context(),
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )
    _, second, _ = await _resume(agent, first, approved=[_approval("c1", "add", {"a": 1, "b": 2})])
    assert second.status is RunStatus.paused
    assert [(r.tool_execution.tool_call_id, r.confirmation) for r in second.requirements or ()] == [
        ("c1", True),
        ("c2", None),
    ]

    if second_decision == "approve":
        events, third, _ = await _resume(agent, second, approved=[_approval("c2", "add", {"a": 3, "b": 4})])
    else:
        events, third, _ = await _resume(agent, second, denied={"c2": "no"})

    # The call the first resume approved ran once; this resume answered only c2, so the model sees each result once.
    assert executed == [(1, 2), (3, 4)] if second_decision == "approve" else [(1, 2)]
    assert third.status is RunStatus.completed
    tool_messages = [message for message in model.requests[-1].messages if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["c1", "c2"]
    assert tool_messages[0].content == "3"
    assert tool_messages[1].content == ("7" if second_decision == "approve" else "no")
    assert [event.call.tool_call_id for event in events if isinstance(event, ToolStarted | ToolCompleted)] == (
        ["c2", "c2"] if second_decision == "approve" else []
    )
    assert [(r.tool_execution.tool_call_id, r.confirmation) for r in third.requirements or ()] == [
        ("c1", True),
        ("c2", second_decision == "approve"),
    ]
    assert all(not tool.is_paused for tool in third.tools or ())


# --- resume guards ---------------------------------------------------------------------------------------------------


def _paused_record(*tools: ToolExecution, status: RunStatus = RunStatus.paused) -> RunRecord:
    from mindroom.llm.runs import RunRequirement  # noqa: PLC0415 - only these guards build records by hand.

    return RunRecord(
        run_id="run-p",
        session_id="session-1",
        status=status,
        agent_id="code",
        messages=[
            Message(role="user", content="go"),
            Message(role="assistant", tool_calls=[tool_call("add", "c1", a=1, b=2)]),
        ],
        tools=list(tools),
        requirements=[RunRequirement(tool) for tool in tools if tool.is_paused],
    )


def _gated_add(call_id: str = "c1", **arguments: int) -> ToolExecution:
    return ToolExecution(
        tool_call_id=call_id,
        tool_name="add",
        tool_args=arguments or {"a": 1, "b": 2},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )


@pytest.mark.parametrize(
    ("approved", "denied", "reason"),
    [
        ((), {}, "uncovered"),
        ((_approval("c1", "add", {"a": 1, "b": 2}), _approval("c1", "add", {"a": 1, "b": 2})), {}, "duplicate"),
        ((_approval("c1", "add", {"a": 1, "b": 2}),), {"c1": "no"}, "both"),
        ((_approval("c1", "add", {"a": 1, "b": 2}), _approval("c9", "add", {"a": 1, "b": 2})), {}, "unknown"),
        ((_approval("c1", "add", {"a": 1, "b": 3}),), {}, "digest"),
        ((_approval("c1", "echo", {"a": 1, "b": 2}),), {}, "name"),
    ],
)
@pytest.mark.asyncio
async def test_a_resume_refuses_decisions_that_do_not_match_the_paused_calls_exactly(
    approved: Sequence[ApprovalCall],
    denied: dict[str, str],
    reason: str,
) -> None:
    paused = _paused_record(_gated_add())
    before = paused.to_dict()
    model = ScriptedModel([answer("never")])

    with pytest.raises(ValueError, match="retry the request"):
        await _resume(spec(model, gated(add)), paused, approved=approved, denied=denied)

    assert paused.to_dict() == before, reason
    assert model.requests == []


@pytest.mark.asyncio
async def test_a_resume_refuses_a_planted_confirmed_call_that_no_approval_covers() -> None:
    planted = ToolExecution(
        tool_call_id="c2",
        tool_name="add",
        tool_args={"a": 9, "b": 9},
        requires_confirmation=True,
        confirmed=True,
    )
    paused = _paused_record(_gated_add(), planted)
    paused.requirements = paused.requirements[:1]  # type: ignore[index]

    with pytest.raises(ValueError, match="retry the request"):
        await _resume(
            spec(ScriptedModel([answer("never")]), gated(add)),
            paused,
            approved=[_approval("c1", "add", {"a": 1, "b": 2})],
        )


@pytest.mark.asyncio
async def test_a_resume_refuses_a_run_that_is_not_paused() -> None:
    with pytest.raises(ValueError, match="is COMPLETED"):
        await _resume(spec(ScriptedModel([]), gated(add)), _paused_record(status=RunStatus.completed))


@pytest.mark.asyncio
async def test_a_resume_refuses_an_approved_tool_the_agent_no_longer_offers() -> None:
    paused = _paused_record(_gated_add())

    with pytest.raises(ValueError, match="no longer offered"):
        await _resume(
            spec(ScriptedModel([answer("never")]), gated(echo)),
            paused,
            approved=[_approval("c1", "add", {"a": 1, "b": 2})],
        )


@pytest.mark.asyncio
async def test_resume_entry_hooks_run_after_the_decided_results_and_the_loop_continues_the_record() -> None:
    seen: list[list[str]] = []

    async def entry(messages: list[Message], results: Sequence[Message]) -> None:
        seen.append([message.role for message in messages])
        assert [result.tool_call_id for result in results] == ["c1"]

    paused = _paused_record(_gated_add())
    paused.content = "Checking first."
    model = ScriptedModel([calls(tool_call("add", "c2", a=3, b=4), metrics=usage(1, 1)), answer("Seven.", usage(2, 2))])

    events, record, _ = await _resume(
        spec(model, toolkit(add)),
        paused,
        approved=[_approval("c1", "add", {"a": 1, "b": 2})],
        hooks=LoopHooks(on_resume_entry=(entry,)),
    )

    assert seen == [["user", "assistant", "tool"]]
    # The loop's own text joins the paused content, a blank line apart.
    assert record.content == "Checking first.\n\nSeven."
    assert record.tools is not None
    assert [tool.tool_call_id for tool in record.tools] == ["c1", "c2"]
    assert [event.call.tool_call_id for event in events if isinstance(event, ToolCompleted)] == ["c1", "c2"]
    assert record.metrics is not None
    assert record.metrics.tokens.total_tokens == 6


@pytest.mark.parametrize(("ending", "status"), [("completed", "COMPLETED"), ("failed", "ERROR")])
@pytest.mark.asyncio
async def test_a_resume_adds_its_time_to_the_paused_runs_duration_as_it_adds_its_usage(
    ending: str,
    status: str,
) -> None:
    """A resumed run's counters count every leg of the run, so its duration does too, however the resume ends."""

    async def throttle() -> None:
        msg = "Rate limited"
        raise RateLimited(msg, provider="Synthetic")

    paused = _paused_record(_gated_add())
    paused.metrics = RunMetrics(tokens=TokenCounts(input_tokens=4, output_tokens=1, total_tokens=5), duration=5.0)
    turn = answer("Three.", usage(2, 2)) if ending == "completed" else [UsageReported(usage(2, 2)), Await(throttle)]
    store = _Store()
    resume = resume_agent(
        spec(ScriptedModel([turn]), gated(add)),
        paused,
        build_resume_request(paused=paused.messages, summary=None, history=[]),
        approved=[_approval("c1", "add", {"a": 1, "b": 2})],
        denied={},
        external_results={},
        context=lambda run_id: context(run_id=run_id, session_id="session-1"),
        hooks=NO_HOOKS,
        persist=store,
        request_template=EMPTY_REQUEST,
    )

    before = time.monotonic()
    if ending == "completed":
        await collect(resume)
    else:
        with pytest.raises(RateLimited):
            await collect(resume)
    resumed_for = time.monotonic() - before

    [stored] = store.saved
    assert stored["status"] == status
    assert stored["metrics"]["total_tokens"] == 9
    assert 5.0 < stored["metrics"]["duration"] <= 5.0 + resumed_for
    assert paused.metrics.duration == 5.0


def switch_thread_model(model_name: str) -> str:
    """Switch the thread's model for the rest of this response.

    Args:
        model_name: The configured model to switch to.

    """
    return f"switched to {model_name}"


@pytest.mark.parametrize("stopping", ["approved", "before-pause"])
@pytest.mark.asyncio
async def test_a_resumed_batch_whose_result_stops_the_run_ends_it_without_another_request(stopping: str) -> None:
    """An approved model switch for the current response ends the resume, as it ends a fresh batch.

    The old model gets no request and the resume hooks see nothing, so the caller continues with the new model. A
    result the batch produced before it paused stops the run the same way once its paused call is answered.
    """
    seen: list[str] = []

    async def entry(*_hook_arguments: object) -> None:
        seen.append("resume hook")

    switch = toolkit(switch_thread_model, name="thread_model", stop_after_call=True)
    switch_call = tool_call("switch_thread_model", "c-switch", model_name="opus")
    if stopping == "approved":
        tools = replace(switch, functions=tuple(replace(f, gate=ToolGate.POLICY) for f in switch.functions))
        waiting = ToolExecution(
            tool_call_id="c-switch",
            tool_name="switch_thread_model",
            tool_args={"model_name": "opus"},
            requires_confirmation=True,
            approval_type="mindroom_policy",
        )
        paused = _paused_record(waiting)
        paused.messages[1] = Message(role="assistant", tool_calls=[switch_call])
        approved = [_approval("c-switch", "switch_thread_model", {"model_name": "opus"})]
        agent = spec(ScriptedModel([answer("The old model answers.")]), tools)
    else:
        switched = ToolExecution(
            tool_call_id="c-switch",
            tool_name="switch_thread_model",
            tool_args={"model_name": "opus"},
            result="switched to opus",
            stop_after_call=True,
        )
        paused = _paused_record(switched, _gated_add())
        paused.messages[1] = Message(role="assistant", tool_calls=[switch_call, tool_call("add", "c1", a=1, b=2)])
        paused.messages.append(
            Message(
                role="tool",
                content="switched to opus",
                tool_call_id="c-switch",
                tool_name="switch_thread_model",
                stop_after_tool_call=True,
            ),
        )
        approved = [_approval("c1", "add", {"a": 1, "b": 2})]
        agent = spec(ScriptedModel([answer("The old model answers.")]), switch, gated(add))

    _, record, store = await _resume(agent, paused, approved=approved, hooks=LoopHooks(on_resume_entry=(entry,)))

    assert isinstance(agent.model, ScriptedModel)
    assert agent.model.requests == []
    assert seen == []
    assert record.status is RunStatus.completed
    assert [saved["status"] for saved in store.saved] == ["COMPLETED"]
    assert record.tools is not None
    assert all(tool.result is not None for tool in record.tools)


# --- toolkit connections ---------------------------------------------------------------------------------------------


def _connecting(log: list[str], adapted: AdaptedToolkit, *, owned: bool = True) -> AdaptedToolkit:
    """Return ``adapted`` as a toolkit that holds a connection for one run; ``owned`` False was connected already."""

    async def close() -> None:
        log.append(f"{adapted.name} close")

    async def connect() -> ToolkitClose | None:
        log.append(f"{adapted.name} connect")
        return close if owned else None

    return replace(adapted, connect=connect)


def _logged_request(log: list[str]) -> Await:
    async def record() -> None:
        log.append("request")

    return Await(record)


@pytest.mark.asyncio
async def test_a_run_connects_its_toolkits_first_and_closes_what_it_connected_after_reporting_its_end() -> None:
    log: list[str] = []
    model = ScriptedModel([[_logged_request(log), *answer("Done.")]])
    agent = spec(
        model,
        _connecting(log, toolkit(add, name="database")),
        _connecting(log, toolkit(echo, name="server"), owned=False),
        toolkit(boom, name="plain"),
    )

    reported: list[str] = []
    async for event in run_agent(agent, _prompt(), ctx=context(), persist=None, request_template=EMPTY_REQUEST):
        if isinstance(event, RunFinished):
            reported = list(log)

    # A toolkit connected before the run belongs to whoever connected it.
    assert reported == ["database connect", "server connect", "request"]
    assert log == [*reported, "database close"]


@pytest.mark.parametrize("consumer", ["drains", "closes"])
@pytest.mark.asyncio
async def test_a_run_reports_its_end_before_a_slow_close_finishes_and_the_close_still_finishes(consumer: str) -> None:
    """The consumer holds ``RunFinished`` while the close runs, whether it reads on or closes the stream there."""
    log: list[str] = []
    closing = asyncio.Event()
    release = asyncio.Event()

    async def close() -> None:
        log.append("close started")
        closing.set()
        await release.wait()
        log.append("closed")

    async def connect() -> ToolkitClose:
        return close

    store = _Store()
    agent = spec(ScriptedModel([answer("Done.")]), replace(toolkit(add, name="database"), connect=connect))
    stream = run_agent(agent, _prompt(), ctx=context(), persist=store, request_template=EMPTY_REQUEST)

    async def consume() -> None:
        async with closing_async_stream(stream):
            async for event in stream:
                if isinstance(event, RunFinished):
                    log.append("finished")
                    if consumer == "closes":
                        break

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(closing.wait(), timeout=5)
        assert log == ["finished", "close started"]
        assert not task.done()
    finally:
        release.set()
    await task

    assert log == ["finished", "close started", "closed"]
    # Closing the stream once it reported its end leaves the completed record as it was stored.
    assert [saved["status"] for saved in store.saved] == ["COMPLETED"]


@pytest.mark.parametrize("ending", ["error", "cancel"])
@pytest.mark.asyncio
async def test_a_run_that_fails_or_is_cancelled_closes_what_it_connected_after_storing_its_record(ending: str) -> None:
    log: list[str] = []
    waiting = asyncio.Event()

    async def end() -> None:
        log.append("request")
        if ending == "error":
            msg = "Rate limited"
            raise RateLimited(msg, provider="Synthetic")
        waiting.set()
        await asyncio.Event().wait()

    async def persist(record: RunRecord) -> None:
        log.append(f"stored {record.status.value}")

    model = ScriptedModel([[Await(end)]])
    agent = spec(model, _connecting(log, toolkit(add, name="database")))
    task = asyncio.create_task(
        collect(run_agent(agent, _prompt(), ctx=context(), persist=persist, request_template=EMPTY_REQUEST)),
    )
    if ending == "cancel":
        await asyncio.wait_for(waiting.wait(), timeout=5)
        task.cancel()
    with pytest.raises(RateLimited if ending == "error" else asyncio.CancelledError):
        await task

    status = "ERROR" if ending == "error" else "CANCELLED"
    assert log == ["database connect", "request", f"stored {status}", "database close"]


@pytest.mark.asyncio
async def test_a_resume_connects_before_the_approved_call_runs() -> None:
    log: list[str] = []

    def add(a: int, b: int) -> str:
        """Add two integers.

        Args:
            a: First number.
            b: Second number.

        """
        log.append("approved call")
        return json.dumps({"sum": a + b})

    model = ScriptedModel([[_logged_request(log), *answer("Three.")]])

    await _resume(
        spec(model, _connecting(log, gated(add, name="database"))),
        _paused_record(_gated_add()),
        approved=[_approval("c1", "add", {"a": 1, "b": 2})],
    )

    assert log == ["database connect", "approved call", "request", "database close"]


@pytest.mark.asyncio
async def test_a_resume_keeps_counting_the_calls_its_turn_already_made() -> None:
    """A resume continues its turn, so a call past the turn's limit is refused instead of pausing once more."""

    def add(a: int, b: int) -> str:
        """Add two integers.

        Args:
            a: First number.
            b: Second number.

        """
        return str(a + b)

    model = ScriptedModel(
        [
            calls(tool_call("add", "c1", a=1, b=2), metrics=usage(1, 1)),
            calls(tool_call("add", "c2", a=3, b=4), metrics=usage(1, 1)),
            answer("done", usage(1, 1)),
        ],
    )
    agent = spec(model, gated(add), max_tool_calls_per_turn=1)
    _, first = await collect(
        run_agent(
            agent,
            [Message(role="user", content="go")],
            ctx=context(),
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )
    _, second, _ = await _resume(agent, first, denied={"c1": "no"})

    assert second.status is RunStatus.completed
    tool_messages = [message for message in model.requests[-1].messages if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["c1", "c2"]
    assert "limit" in str(tool_messages[1].content).lower()


@pytest.mark.asyncio
async def test_a_resume_charges_the_limit_only_for_the_calls_that_counted_before_the_pause() -> None:
    """A call naming no tool never counts, so the stored run's refusal costs its resume nothing.

    With a limit of two, the unknown call and the gated call are both in the run when it pauses, but only the gated
    one counts: after the approval the next call is the second that counts and runs, and the one after it is refused.
    """
    model = ScriptedModel(
        [
            calls(tool_call("missing", "c1"), tool_call("add", "c2", a=1, b=2), metrics=usage(1, 1)),
            calls(tool_call("echo", "c3", text="second"), metrics=usage(1, 1)),
            calls(tool_call("echo", "c4", text="third"), metrics=usage(1, 1)),
            answer("done", usage(1, 1)),
        ],
    )
    agent = spec(model, gated(add), toolkit(echo), max_tool_calls_per_turn=2)
    _, first = await collect(
        run_agent(
            agent,
            [Message(role="user", content="go")],
            ctx=context(),
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )
    assert first.status is RunStatus.paused
    stored = RunRecord.from_dict(first.to_dict())
    assert ([tool.tool_call_id for tool in stored.tools or ()], stored.uncounted_tool_calls) == (["c1", "c2"], 1)

    _, second, _ = await _resume(agent, stored, approved=[_approval("c2", "add", {"a": 1, "b": 2})])

    assert second.status is RunStatus.completed
    answers = {
        message.tool_call_id: str(message.content) for message in model.requests[-1].messages if message.role == "tool"
    }
    assert answers["c3"] == "second"
    assert "limit" in answers["c4"].lower()
