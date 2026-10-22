"""Agent runs end in exactly one stored record, and approval resumes run exactly the approved calls.

The resume tests replay C4's paused rows (``run-paused-approval``, the ``team-paused`` member, and
``run-paused-delegation``) through ``resume_agent`` and compare the result with the rows Agno stored after the
same resume (``run-approved``, ``run-rejected``, the ``team-approved`` member, ``run-delegation-approved``).
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agent_loop.model_loop import NO_HOOKS, LoopHooks
from mindroom.agent_loop.run import REJECTED_TEXT, paused_content, resume_agent, run_agent
from mindroom.agent_loop.spec import AgentSpec
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.event_journal.approval_continuations import ApprovalCall, approval_arguments_digest
from mindroom.history.request_messages import build_resume_request
from mindroom.llm.errors import RateLimited
from mindroom.llm.events import RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity, TextDelta, UsageReported
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.context_bound_streams import closing_async_stream
from tests.agent_loop.helpers import add, boom, collect, context, echo, external, gated, spec, toolkit
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.llm.agno_dict_helpers import expected_run_dict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolkitClose

LOADED_SESSIONS = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "storage" / "loaded_sessions.json"


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


@pytest.mark.asyncio
async def test_a_completed_run_is_stored_once_before_it_is_announced() -> None:
    store = _Store()
    model = ScriptedModel(
        [calls(tool_call("add", "c1", a=2, b=3), metrics=usage(10, 2)), answer("Five.", usage(20, 4))],
    )

    events, record = await collect(run_agent(spec(model, toolkit(add)), _prompt(), ctx=context(), persist=store))

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


@pytest.mark.asyncio
async def test_stored_messages_leave_out_history_copies_and_transient_context() -> None:
    model = ScriptedModel([answer("ok")])
    history = Message(role="user", content="earlier", from_history=True)
    transient = Message(role="user", content="context", add_to_agent_memory=False)

    _, record = await collect(run_agent(spec(model), [history, *_prompt(), transient], ctx=context()))

    assert [message.content for message in record.messages] == ["You are Code.", "hi", "ok"]
    assert model.requests[0].messages[1] is history
    assert model.requests[0].messages[-1] is transient


@pytest.mark.asyncio
async def test_a_paused_run_is_stored_paused_with_agno_text_when_the_model_said_nothing() -> None:
    store = _Store()
    model = ScriptedModel([calls(tool_call("add", "c1", a=6, b=7), metrics=usage(1, 1))])

    _, record = await collect(run_agent(spec(model, gated(add)), _prompt(), ctx=context(), persist=store))

    assert record.status is RunStatus.paused
    assert record.content == "I have tools to execute, but I need confirmation."
    assert record.requirements is not None
    assert record.requirements[0].needs_confirmation
    assert store.saved[0]["status"] == "PAUSED"
    assert [message["role"] for message in store.saved[0]["messages"]] == ["system", "user", "assistant"]


@pytest.mark.asyncio
async def test_a_paused_run_keeps_the_text_the_model_streamed() -> None:
    model = ScriptedModel(
        [calls(tool_call("add", "c1", a=6, b=7), content="I need approval first.", metrics=usage(1, 1))],
    )

    _, record = await collect(run_agent(spec(model, gated(add)), _prompt(), ctx=context()))

    assert record.content == "I need approval first."


@pytest.mark.asyncio
async def test_a_silent_external_pause_has_empty_content() -> None:
    model = ScriptedModel([calls(tool_call("echo", "c1", text="task"), metrics=usage(1, 1))])

    _, record = await collect(run_agent(spec(model, external(echo)), _prompt(), ctx=context()))

    assert (record.status, record.content) == (RunStatus.paused, "")


def test_paused_content_follows_agno_for_every_pause_kind() -> None:
    confirm = ToolExecution(requires_confirmation=True)
    external_loud = ToolExecution(external_execution_required=True)
    silent = ToolExecution(external_execution_required=True, external_execution_silent=True)
    both = ToolExecution(requires_confirmation=True, external_execution_required=True)

    assert paused_content([confirm]) == "I have tools to execute, but I need confirmation."
    assert paused_content([external_loud]) == "I have tools to execute, but it needs external execution."
    assert paused_content([both]) == "I have tools to execute, but I need confirmation or external execution."
    assert paused_content([silent]) == ""
    assert paused_content([confirm, silent]) == "I have tools to execute, but I need confirmation."
    assert paused_content([ToolExecution(requires_confirmation=True, confirmed=True)]) == ""


@pytest.mark.asyncio
async def test_a_provider_error_stores_an_error_record_with_the_error_text_and_reraises() -> None:
    store = _Store()

    async def throttle() -> None:
        msg = "Rate limited"
        raise RateLimited(msg, provider="Synthetic")

    model = ScriptedModel([[UsageReported(usage(5, 0), final=False), Await(throttle)]])

    with pytest.raises(RateLimited, match="Rate limited"):
        async for _ in run_agent(spec(model), _prompt(), ctx=context(), persist=store):
            pass

    [stored] = store.saved
    assert (stored["status"], stored["content"]) == ("ERROR", "Rate limited")
    assert [message["role"] for message in stored["messages"]] == ["system", "user", "assistant"]
    assert stored["messages"][-1]["metrics"] == {"input_tokens": 5, "total_tokens": 5}
    assert stored["metrics"]["input_tokens"] == 5
    assert "tools" not in stored


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
        async for _ in run_agent(spec(model, toolkit(echo)), _prompt(), ctx=context(), persist=store):
            pass

    assert (store.saved[0]["status"], store.saved[0]["content"]) == ("ERROR", "Working.")


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
            async for _ in run_agent(spec(model, toolkit(add)), _prompt(), ctx=context(), persist=persist):
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

    stream = run_agent(spec(model), _prompt(), ctx=context(), persist=store)
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
        async for _ in run_agent(spec(model, gated(add)), _prompt(), ctx=context(), persist=persist):
            pass

    task = asyncio.create_task(consume())
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [record["status"] for record in saved] == ["PAUSED", "CANCELLED"]
    cancelled = saved[-1]
    # Cancellation wins over the pause: the requirement leaves and the tool entry stops waiting.
    assert cancelled["requirements"] == []
    assert (cancelled["tools"][0]["requires_confirmation"], cancelled["tools"][0]["confirmed"]) == (False, None)
    assert cancelled["content"] == "I have tools to execute, but I need confirmation."


@pytest.mark.asyncio
async def test_a_failing_store_fails_a_completed_run_but_not_an_interrupted_one() -> None:
    async def broken(record: RunRecord) -> None:
        del record
        msg = "disk full"
        raise OSError(msg)

    with pytest.raises(OSError, match="disk full"):
        await collect(run_agent(spec(ScriptedModel([answer("ok")])), _prompt(), ctx=context(), persist=broken))

    async def fail() -> None:
        msg = "boom"
        raise RateLimited(msg, provider="Synthetic")

    with pytest.raises(RateLimited):
        async for _ in run_agent(spec(ScriptedModel([[Await(fail)]])), _prompt(), ctx=context(), persist=broken):
            pass


@pytest.mark.asyncio
async def test_a_run_without_a_system_message_sends_only_its_input() -> None:
    model = ScriptedModel([answer("ok")])
    agent = replace(spec(model), system_message=None)

    _, record = await collect(run_agent(agent, _prompt(), ctx=context()))

    assert [message.role for message in model.requests[0].messages] == ["user"]
    assert [message.role for message in record.messages] == ["user", "assistant"]


# --- resumes against the C4 goldens ---------------------------------------------------------------------------------


def _golden_run(store: str, session_id: str, run_id: str) -> dict[str, Any]:
    sessions = json.loads(LOADED_SESSIONS.read_text(encoding="utf-8"))[store]
    [run] = [run for run in sessions[session_id]["runs"] if run["run_id"] == run_id]
    return run


def _gated_calculator() -> AdaptedToolkit:
    """MindRoom's calculator with ``multiply`` under the approval policy, as the capture's config gated it."""
    adapted = adapt_toolkit(CalculatorTools())
    return replace(
        adapted,
        functions=tuple(
            replace(function, requires_confirmation=True, approval_type="mindroom_policy")
            if function.name == "multiply"
            else function
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
    return AgentSpec(id=agent_id, name=name, model=model, system_message=None, toolkits=tuple(toolkits))


def _model(model_id: str, *turns: list[Any]) -> ScriptedModel:
    return ScriptedModel(
        list(turns),
        identity=ModelIdentity(id=model_id, provider="Synthetic", provider_name="Synthetic"),
    )


def _comparable(run: dict[str, Any], *, paused: RunRecord) -> dict[str, Any]:
    """Return a run dict without the identity and wall-clock values that differ between two sessions.

    Only the messages the resume added are compared: the stored paused messages are the sessions' own prompts,
    which the capture worded differently, and the completed row no longer holds the system message.
    """
    run = expected_run_dict(run)
    result = {
        key: value
        for key, value in run.items()
        if key not in {"run_id", "session_id", "parent_run_id", "metadata", "created_at"}
    }
    stored = [message for message in run["messages"] if message["role"] != "system"]
    skipped = sum(message.role != "system" for message in paused.messages)
    result["messages"] = [_comparable_message(message) for message in stored[skipped:]]
    result["tools"] = [_comparable_tool(tool) for tool in run.get("tools", [])]
    result["requirements"] = [_comparable_requirement(item) for item in run.get("requirements", [])]
    result["metrics"] = {key: value for key, value in run["metrics"].items() if key != "duration"}
    return result


def _comparable_message(message: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in message.items() if key not in {"id", "created_at"}}
    if isinstance(result.get("content"), str):
        # Each capture session prompted with its own Matrix event id.
        result["content"] = re.sub(r'event_id="[^"]+"', 'event_id="<event>"', result["content"])
    if "tool_call_id" in result:
        result["tool_call_id"] = "<call>"
    if "tool_calls" in result:
        result["tool_calls"] = [{**call, "id": "<call>"} for call in result["tool_calls"]]
    return result


def _comparable_tool(tool: dict[str, Any]) -> dict[str, Any]:
    # Timing is wall clock, and Agno recorded none for a resumed call; the owned loop times every call it runs.
    return {
        **{key: value for key, value in tool.items() if key not in {"created_at", "metrics"}},
        "tool_call_id": "<call>",
    }


def _comparable_requirement(item: dict[str, Any]) -> dict[str, Any]:
    # Agno's team resume handed the member the team's requirement, which names the member; the owned resume takes
    # decisions, not requirement objects, so a member row keeps its own requirement and the team run names the member.
    dropped = {"id", "created_at", "member_agent_id", "member_agent_name", "member_run_id"}
    result = {key: value for key, value in item.items() if key not in dropped}
    result["tool_execution"] = _comparable_tool(result["tool_execution"])
    return result


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
            ctx=context(run_id=paused.run_id, session_id=paused.session_id or "session"),
            hooks=hooks,
            persist=store,
        ),
    )
    return events, record, store


@pytest.mark.asyncio
async def test_an_approved_call_resumes_into_agno_s_approved_row() -> None:
    paused = RunRecord.from_dict(_golden_run("agent_code", "!room:example.org:$conversation", "run-paused-approval"))
    before = paused.to_dict()
    model = _model("code-model", answer("6 x 7 = 42.", usage(260, 9, time_to_first_token=0.25)))

    events, record, store = await _resume(
        _agent(model, [_gated_calculator()]),
        paused,
        approved=[_approval("call-multiply", "multiply", {"a": 6, "b": 7})],
    )

    expected = _golden_run("agent_code", "!room:example.org:$approved", "run-approved")
    assert _comparable(record.to_dict(), paused=paused) == _comparable(expected, paused=paused)
    assert record.messages[: len(paused.messages)] == paused.messages
    assert len(record.messages) == len(paused.messages) + 2
    assert store.saved == [record.to_dict()]
    assert paused.to_dict() == before
    assert [type(event).__name__ for event in events[:2]] == ["ToolStarted", "ToolCompleted"]
    assert isinstance(events[0], ToolStarted)
    assert events[0].tool is record.tools[0]  # type: ignore[index]
    # The resume request repeats the paused request, system message first.
    assert [message.role for message in model.requests[0].messages] == ["system", "user", "assistant", "tool"]
    assert model.requests[0].messages[-1].content == '{"operation": "multiplication", "result": 42.0}'


@pytest.mark.asyncio
async def test_a_denied_call_resumes_into_agno_s_rejected_row() -> None:
    paused = RunRecord.from_dict(_golden_run("agent_code", "!room:example.org:$conversation", "run-paused-approval"))
    model = _model(
        "code-model",
        answer("I did not multiply, because the call was declined.", usage(260, 9, time_to_first_token=0.25)),
    )

    events, record, _ = await _resume(
        _agent(model, [_gated_calculator()]),
        paused,
        denied={"call-multiply": "Alice declined the multiplication."},
    )

    expected = _golden_run("agent_code", "!room:example.org:$rejected", "run-rejected")
    assert _comparable(record.to_dict(), paused=paused) == _comparable(expected, paused=paused)
    assert not any(isinstance(event, ToolStarted | ToolCompleted) for event in events)
    assert model.requests[0].messages[-1].content == "Alice declined the multiplication."


@pytest.mark.asyncio
async def test_a_team_member_s_approved_call_resumes_into_the_member_s_completed_row() -> None:
    paused = RunRecord.from_dict(
        _golden_run("team_squad", "!room:example.org:$squad", "00000000-0000-4000-8000-00000000005a"),
    )
    model = _model("checker-model", answer("42", usage(95, 1, time_to_first_token=0.25)))

    _, record, _ = await _resume(
        _agent(model, [_gated_calculator()], agent_id="checker", name="Checker"),
        paused,
        approved=[_approval("call-checker-multiply", "multiply", {"a": 6, "b": 7}, agent="checker")],
    )

    expected = _golden_run("team_squad", "!room:example.org:$squad-approved", "00000000-0000-4000-8000-000000000063")
    assert _comparable(record.to_dict(), paused=paused) == _comparable(expected, paused=paused)
    assert record.parent_run_id == "team-paused"


@pytest.mark.asyncio
async def test_an_external_result_resumes_into_agno_s_delegation_approved_row() -> None:
    paused = RunRecord.from_dict(_golden_run("agent_code", "!room:example.org:$delegation", "run-paused-delegation"))
    expected = _golden_run("agent_code", "!room:example.org:$delegation-approved", "run-delegation-approved")
    model = _model("code-model", answer("The helper found 42.", usage(330, 6, time_to_first_token=0.25)))

    events, record, _ = await _resume(
        _agent(model, [_delegate()]),
        paused,
        external_results={"call-delegate": expected["tools"][0]["result"]},
    )

    assert _comparable(record.to_dict(), paused=paused) == _comparable(expected, paused=paused)
    assert not any(isinstance(event, ToolStarted | ToolCompleted) for event in events)


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
    _, first = await collect(run_agent(agent, [Message(role="user", content="go")], ctx=context()))
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
    assert [event.tool.tool_call_id for event in events if isinstance(event, ToolStarted | ToolCompleted)] == (
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
        ((_approval("c1", "add", {"a": 1, "b": 2}),), {"c1": None}, "both"),
        ((_approval("c1", "add", {"a": 1, "b": 2}), _approval("c9", "add", {"a": 1, "b": 2})), {}, "unknown"),
        ((_approval("c1", "add", {"a": 1, "b": 3}),), {}, "digest"),
        ((_approval("c1", "echo", {"a": 1, "b": 2}),), {}, "name"),
    ],
)
@pytest.mark.asyncio
async def test_a_resume_refuses_decisions_that_do_not_match_the_paused_calls_exactly(
    approved: Sequence[ApprovalCall],
    denied: dict[str, str | None],
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
async def test_a_denial_without_a_note_answers_with_agno_s_default_text() -> None:
    paused = _paused_record(_gated_add())
    model = ScriptedModel([answer("ok")])

    _, record, _ = await _resume(spec(model, gated(add)), paused, denied={"c1": None})

    tool_message = model.requests[0].messages[-1]
    assert (tool_message.content, tool_message.tool_call_error) == (REJECTED_TEXT, True)
    assert record.tools is not None
    assert record.tools[0].confirmation_note == REJECTED_TEXT
    assert record.requirements is not None
    assert record.requirements[0].confirmation is False


@pytest.mark.asyncio
async def test_resume_entry_hooks_run_after_the_decided_results_and_the_loop_continues_the_record() -> None:
    seen: list[list[str]] = []

    async def entry(messages: list[Message]) -> None:
        seen.append([message.role for message in messages])

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
    # The loop's own text replaces the paused content, as Agno's continuation did.
    assert record.content == "Seven."
    assert record.tools is not None
    assert [tool.tool_call_id for tool in record.tools] == ["c1", "c2"]
    assert [event.tool.tool_call_id for event in events if isinstance(event, ToolCompleted)] == ["c1", "c2"]
    assert record.metrics is not None
    assert record.metrics.total_tokens == 6


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
    async for event in run_agent(agent, _prompt(), ctx=context()):
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
    stream = run_agent(agent, _prompt(), ctx=context(), persist=store)

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
    task = asyncio.create_task(collect(run_agent(agent, _prompt(), ctx=context(), persist=persist)))
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
