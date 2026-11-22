"""Tests for a run inside a run, and for a script's `tool()` calls that start agents or runs.

Level: engine harness (`WorkflowStack`) with the real broker, the real job runtime and scripted agents.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.approval.tool_approval import ToolApprovalDecision
from mindroom.config.models import ModelPricing
from mindroom.custom_tools.attachment_helpers import resolve_context_thread_id
from mindroom.delegation.state import SubagentPersona
from mindroom.script_runs.models import ScriptRunState
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.engine import WorkflowLimits
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.fakes.scripted_model import Await, answer, calls, tool_call, usage
from tests.workflow_helpers import NodeFailed, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.workflow_runs.models import NodeReceipt, WorkflowNode, WorkflowRun
    from tests.fakes.scripted_model import ScriptedModel

_PROMPT = {"prompt": "Run the nested workflow."}
_SOURCE = "def main(args):\n    return 'nested'\n"
_START = tool_call("start_workflow", "c1", source=_SOURCE, args={"n": 1})
_RUNNING, _COMPLETED, _CANCELLED = WorkflowNodeState.RUNNING, WorkflowNodeState.COMPLETED, WorkflowNodeState.CANCELLED


async def _opened(stack: WorkflowStack, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    monkeypatch.setattr(engine_module, "_RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "_RECHECK_SECONDS", 0.05)
    # As the job coordinator rules in production: a restart keeps the jobs of agents to continue them.
    stack.continues = lambda job: job.kind == "delegation"
    await stack.open_jobs()
    stack.script_agents(monkeypatch)
    yield stack
    await stack.close()


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with the real job runtime and scripted agents on a priced model; no tool asks for an approval."""
    options = {"other_agents": ("helper",), "delegate_to": ("helper",), "pricing": ModelPricing(input=1, output=2)}
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=(), **options) as stack:
        async for opened in _opened(stack, monkeypatch):
            yield opened


@pytest_asyncio.fixture
async def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The same harness, whose operator asks before an agent or a run is started, and whose owner may delegate."""
    asks = ("run_subagent", "start_workflow")
    options = {"other_agents": ("helper",), "delegate_to": ("helper",), "require_approval": asks}
    with workflow_stack(tmp_path, extra_tools=("workflow",), **options) as stack:
        async for opened in _opened(stack, monkeypatch):
            yield opened


async def _nested(stack: WorkflowStack, parent: str) -> WorkflowRun:
    """Wait until the run *parent* has one run inside it that runs, and return that run."""
    await stack.until(lambda: [run.state for run in stack.store.child_runs(parent)] == [WorkflowRunState.RUNNING])
    return stack.store.child_runs(parent)[0]


async def _report(stack: WorkflowStack, run_id: str, result: object) -> None:
    """Let the script of the run *run_id* return *result*, and leave the stack on the run it was on."""
    parent, stack.run_id = stack.run_id, run_id
    try:
        await stack.finish_attempt(result=result)
    finally:
        stack.run_id = parent


async def _settled(stack: WorkflowStack, receipt: NodeReceipt) -> NodeReceipt:
    while receipt.state not in ("completed", "failed", "cancelled", "indeterminate"):
        receipt = await stack.engine.node_receipt(stack.attempt, receipt.id, wait=5)
    return receipt


def _group(stack: WorkflowStack, child: WorkflowRun) -> WorkflowNode:
    """Return the node that shows the nested run *child* under the call that started it."""
    node = stack.store.get_node(str(child.parent_run_id), f"{child.parent_node_key}/{child.run_id}")
    assert node is not None
    return node


@pytest.mark.asyncio
async def test_agent_starts_a_nested_run_inline_and_gets_its_report(stack: WorkflowStack) -> None:
    """An agent's `start_workflow` waits for a run of its own inside the agent's run, and returns that run's report.

    The nested run has its own limit, so a parent that works on one call at a time does not wait for itself, and it
    is no top-level run, so the requester's limit of those does not refuse it.
    """
    for _ in range(7):
        stack.reserve()
    await stack.start(max_concurrent=1)
    parent = stack.run_id
    model = stack.child_script(calls(_START), answer("done"))

    receipt = await stack.submit("@0", "agent", _PROMPT)
    child = await _nested(stack, parent)

    assert (child.parent_run_id, child.parent_node_key, child.depth, child.max_concurrent) == (parent, "@0", 1, 16)
    assert str(child.origin_job_id).startswith("nested:")
    group = _group(stack, child)
    assert (group.kind, group.group_key, group.state, group.links) == (
        "group",
        "@0",
        _RUNNING,
        {"child_run_id": child.run_id},
    )
    assert group.handle is not None
    assert (group.handle.kind, group.handle.ref) == ("run", child.run_id)
    await _report(stack, child.run_id, "the nested report")
    assert (await _settled(stack, receipt)).result == "done"
    assert "the nested report" in str(model.requests[1].messages[-1].content)
    assert _group(stack, child).state is _COMPLETED


_OWN_RUN = "A call of a run cannot cancel that run or one it is inside. Return from main() or raise to end the script."


@pytest.mark.asyncio
async def test_a_run_s_own_call_cannot_cancel_it_or_a_run_around_it(stack: WorkflowStack) -> None:
    """The end of a run waits for its calls to stop, so a call that asked for it would wait for itself.

    Refused for the script's own `tool()` call, for the script of a run inside, and for an agent of the run.
    """
    await stack.start()
    parent = stack.run_id
    with pytest.raises(NodeFailed) as refused:
        await asyncio.wait_for(stack.result("@0", "tool", tool_args("workflow", "cancel_workflow", run_id=parent)), 5)
    assert (refused.value.kind, str(refused.value)) == ("failed", _OWN_RUN)

    await stack.submit("@1", "tool", tool_args("workflow", "start_workflow", source=_SOURCE))
    child = await _nested(stack, parent)
    stack.run_id = child.run_id
    with pytest.raises(NodeFailed) as refused:
        await asyncio.wait_for(stack.result("@0", "tool", tool_args("workflow", "cancel_workflow", run_id=parent)), 5)
    assert str(refused.value) == _OWN_RUN

    stack.run_id = parent
    model = stack.child_script(calls(tool_call("cancel_workflow", "c1", run_id=parent)), answer("done"))
    assert await asyncio.wait_for(stack.result("@2", "agent", _PROMPT), 10) == "done"
    assert _OWN_RUN in str(model.requests[1].messages[-1].content)
    assert [stack.store.get_run(run_id).state for run_id in (parent, child.run_id)] == [WorkflowRunState.RUNNING] * 2
    # A run inside may still be cancelled from the run around it.
    stopped = await stack.result("@3", "tool", tool_args("workflow", "cancel_workflow", run_id=child.run_id))
    assert (child.run_id in str(stopped), stack.store.get_run(child.run_id).state) == (True, WorkflowRunState.CANCELLED)


@pytest.mark.asyncio
async def test_stop_of_the_parent_cancels_the_nested_run(stack: WorkflowStack) -> None:
    """A run inside a run does not outlive it: the stop of the parent ends the nested run and its group."""
    await stack.start()
    parent = stack.run_id
    stack.child_script(calls(_START), answer("never"))
    await stack.submit("@0", "agent", _PROMPT)
    child = await _nested(stack, parent)

    await stack.engine.cancel(parent, reason="Stopped.")

    await stack.until(lambda: stack.store.get_run(child.run_id).state is WorkflowRunState.CANCELLED)
    assert _group(stack, child).state is _CANCELLED


@pytest.mark.asyncio
async def test_stop_of_the_parent_cancels_a_nested_run_nobody_waits_for(stack: WorkflowStack) -> None:
    """After a restart no call waits for the nested run yet: the stop of the parent still reaches it."""
    await stack.start()
    parent = stack.run_id
    stack.child_script(calls(_START), answer("never"))
    await stack.submit("@0", "agent", _PROMPT)
    child = await _nested(stack, parent)
    await stack.restart()
    assert set(await stack.recover()) == {parent, child.run_id}
    assert not stack.engine.attended(child.owner)

    await stack.engine.cancel(parent, reason="Stopped.")

    assert stack.store.get_run(child.run_id).state is WorkflowRunState.CANCELLED


@pytest.mark.asyncio
async def test_nested_run_cost_is_in_the_parent_total_once(stack: WorkflowStack) -> None:
    """What the agents of a nested run cost is in the parent's sums once, and in no agent's own cost."""
    await stack.start()
    parent = stack.run_id
    ten_cents = usage(100_000, 0)
    stack.child_script(calls(_START, metrics=ten_cents), answer("inside", usage(300_000, 0)), answer("done", ten_cents))
    receipt = await stack.submit("@0", "agent", _PROMPT)
    child = await _nested(stack, parent)
    stack.run_id = child.run_id
    assert await stack.result("@0", "agent", {"prompt": "Work inside."}) == "inside"
    stack.run_id = parent
    await _report(stack, child.run_id, "the nested report")
    assert (await _settled(stack, receipt)).result == "done"

    inner, outer = stack.engine.run_usage(child.run_id), stack.engine.run_usage(parent)

    assert (inner["cost_usd"], outer["cost_usd"]) == (pytest.approx(0.30), pytest.approx(0.50))
    assert (outer["attempt_cost_usd"], outer["input_tokens"]) == (pytest.approx(0.50), 500_000)
    assert stack.store.usage_totals(parent)["cost_usd"] == pytest.approx(0.20)
    # The nested run is part of the attempt that started it: the next attempt of the outer run has spent nothing.
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(parent)
    resumed = stack.engine.run_usage(parent)
    assert (resumed["cost_usd"], resumed["attempt_cost_usd"]) == (pytest.approx(0.50), 0.0)


@pytest.mark.asyncio
async def test_repeated_call_of_the_same_agent_finds_the_same_nested_run(stack: WorkflowStack) -> None:
    """The nested run is found by what started it and not by a call's ID: a repeated call starts no second run.

    An agent that a restart cut short repeats its call under a new ID. It waits for the run while that still runs,
    and gets the stored report once it has ended.
    """
    await stack.start()
    parent = stack.run_id
    gate = asyncio.Event()
    again = tool_call("start_workflow", "c2", source=_SOURCE, args={"n": 1})
    model = stack.child_script(calls(_START), calls(again), [Await(gate.wait), *answer("done")])
    receipt = await stack.submit("@0", "agent", _PROMPT)
    child = await _nested(stack, parent)

    await _report(stack, child.run_id, "the nested report")
    await stack.until(lambda: len(model.requests) == 3)

    assert [run.run_id for run in stack.store.child_runs(parent)] == [child.run_id]
    assert "the nested report" in str(model.requests[2].messages[-1].content)
    gate.set()
    assert (await _settled(stack, receipt)).result == "done"


@pytest.mark.asyncio
async def test_other_arguments_and_a_rerun_of_the_call_start_runs_of_their_own(stack: WorkflowStack) -> None:
    """The nested run's key holds the arguments and the generation of the call: either one changed is another run."""
    await stack.start()
    parent = stack.run_id
    other = tool_call("start_workflow", "c2", source=_SOURCE, args={"n": 2})
    stack.child_script(calls(_START), calls(other), answer("done"), calls(_START), answer("again"))

    async def report_the_next(total: int) -> None:
        running = WorkflowRunState.RUNNING
        await stack.until(
            lambda: (
                [len(runs := stack.store.child_runs(parent)), [run.state for run in runs].count(running)] == [total, 1]
            ),
        )
        [child] = [run for run in stack.store.child_runs(parent) if run.state is running]
        await _report(stack, child.run_id, f"report {total}")

    receipt = await stack.submit("@0", "agent", _PROMPT)
    await report_the_next(1)
    await report_the_next(2)
    assert (await _settled(stack, receipt)).result == "done"
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(parent, rerun=["n1"])
    receipt = await stack.submit("@0", "agent", _PROMPT)
    await report_the_next(3)

    assert (await _settled(stack, receipt)).result == "again"
    assert len({run.origin_job_id for run in stack.store.child_runs(parent)}) == 3


async def _cut_short(
    stack: WorkflowStack,
    *made: dict[str, Any],
    kill: bool = False,
    before: tuple[dict[str, Any], ...] = (),
) -> tuple[str, WorkflowRun]:
    """Start a run whose agent makes the calls *made* in one turn, and stop MindRoom once a run inside it runs.

    The agent makes the calls *before* first, each in a turn of its own that ends.
    """
    await stack.start()
    parent = stack.run_id
    stack.child_script(*(calls(call) for call in before), calls(*made), answer("never"))
    await stack.submit("@0", "agent", _PROMPT)
    child = await _nested(stack, parent)
    await (stack.kill() if kill else stack.restart())
    return parent, child


def _sent(model: ScriptedModel) -> str:
    """Return what the agent was sent as the user's message of its latest model request."""
    return str([message.content for message in model.requests[-1].messages if message.role == "user"][-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("kill", [False, True])
async def test_restart_during_a_nested_run_continues_the_agent_with_that_runs_report(
    stack: WorkflowStack,
    kill: bool,
) -> None:
    """An agent that a restart cut short inside `start_workflow` goes on with the report of the run that call started.

    The agent is not asked to make the call again. The call was saved with its run when it reserved it, so the
    agent's job waits for that run, which went on by itself, and the agent's next turn is sent the report as the
    result of its call.
    """
    parent, child = await _cut_short(stack, _START, kill=kill)
    model = stack.child_script(answer("done"))
    await stack.continue_agents()
    assert parent in await stack.recover()
    # The run inside gets its next attempt from the starting primary, or from the agent's job that waits for it.
    running = [(WorkflowRunState.RUNNING, 2)]
    await stack.until(lambda: [(run.state, run.attempt) for run in stack.store.child_runs(parent)] == running)
    await asyncio.sleep(0.3)

    # The job waits for the run, and asks no model while it does.
    assert model.requests == []
    await _report(stack, child.run_id, "the nested report")
    stack.run_id = parent
    assert await stack.result("@0", "agent", _PROMPT) == "done"
    assert "<start_workflow_result>\nthe nested report\n</start_workflow_result>" in _sent(model)
    assert "still running" not in _sent(model)
    assert ([run.run_id for run in stack.store.child_runs(parent)], len(model.requests)) == ([child.run_id], 1)
    assert _group(stack, child).state is _COMPLETED


@pytest.mark.asyncio
async def test_restarts_while_the_continued_agent_waits_for_the_run_wait_for_it_again(stack: WorkflowStack) -> None:
    """The job that waits for the run inside is cut short like any job: continued again, it waits for the same run.

    Its turn had not begun, so it is sent the same prompt, and a wait is not what stops a job: the limit on
    restarts in a row does not end it.
    """
    parent, child = await _cut_short(stack, _START)
    model = stack.child_script(answer("done"))
    for attempt in (2, 3, 4, 5):
        await stack.continue_agents()
        assert parent in await stack.recover()
        await stack.until(lambda: stack.engine.attended(child.owner))
        running = [(WorkflowRunState.RUNNING, attempt)]
        await stack.until(lambda: [(run.state, run.attempt) for run in stack.store.child_runs(parent)] == running)  # noqa: B023
        stack.run_id = parent
        if attempt == 2:
            # As for an agent whose turn restarts had cut short twice already, the most that is continued.
            assert stack.node_job("@0").adapter["restart_continuations"] == 1
            await stack.jobs.save_adapter(stack.node_job("@0").job_id, restart_continuations=2)
        if attempt < 5:
            await stack.restart()
    assert model.requests == []
    # The restarts that cut the agent's turn short count toward the limit, and the ones that cut a wait short do not.
    assert stack.node_job("@0").adapter["restart_continuations"] == 2
    # Only a run inside the call the agent is of is waited for, whatever a prompt names.
    assert await stack.engine.nested_report(("wf-" + "0" * 32, "@0", 1), child.run_id) is None
    await _report(stack, child.run_id, "the nested report")

    stack.run_id = parent
    assert await stack.result("@0", "agent", _PROMPT) == "done"
    assert "<start_workflow_result>\nthe nested report\n</start_workflow_result>" in _sent(model)
    assert ([run.run_id for run in stack.store.child_runs(parent)], len(model.requests)) == ([child.run_id], 1)


@pytest.mark.asyncio
async def test_stop_of_the_continued_agent_that_waits_for_the_run_cancels_that_run(stack: WorkflowStack) -> None:
    """The job waits as the call that started the run did: when it is stopped, nobody is left to read the report."""
    parent, child = await _cut_short(stack, _START)
    model = stack.child_script(answer("never"))
    await stack.continue_agents()
    assert parent in await stack.recover()
    await stack.until(lambda: stack.engine.attended(child.owner))
    stack.run_id = parent

    await stack.jobs.cancel(stack.node_job("@0").job_id, owner=stack.context.execution_identity, depth=0)

    await stack.until(lambda: stack.store.get_run(child.run_id).state is WorkflowRunState.CANCELLED)
    assert model.requests == []


@pytest.mark.asyncio
async def test_nested_run_that_ended_before_its_agent_went_on_gives_the_agent_its_stored_report(
    stack: WorkflowStack,
) -> None:
    """A run inside a run that has ended by the time its agent is continued is not waited for: its report is there.

    The account of the agent's calls names the one it had finished, and not the call whose result it is given.
    """
    parent, child = await _cut_short(stack, _START, before=(tool_call("append", "c0", line="a"),))
    assert set(await stack.recover()) == {parent, child.run_id}
    await _report(stack, child.run_id, "the nested report")
    model = stack.child_script(answer("done"))

    await stack.continue_agents()

    stack.run_id = parent
    assert await stack.result("@0", "agent", _PROMPT) == "done"
    assert "<start_workflow_result>\nthe nested report\n</start_workflow_result>" in _sent(model)
    assert ("`append` tool" in _sent(model), "still running" in _sent(model)) == (True, False)
    assert [run.run_id for run in stack.store.child_runs(parent)] == [child.run_id]
    # The wait is over: the job no longer says that it waits for a run.
    assert (stack.node_job("@0").adapter.get("wait"), stack.counting_tool.lines) == (None, ["a"])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["changed file", "no binding", "another call"])
async def test_agent_whose_cut_short_calls_are_not_all_proven_starts_of_runs_is_not_continued(
    stack: WorkflowStack,
    case: str,
) -> None:
    """Without proof for every call that was cut short, nobody can say what the turn did: its call is indeterminate.

    The proof is the call saved with its run, for the script and the arguments the call still names. A script file
    that changed since is another script, a call that reserved no run has none, and a call of any other tool beside
    it may have done anything.
    """
    workspace = stack.context.agent_workspace_root(create=True)
    (workspace / "nested.py").write_text(_SOURCE, encoding="utf-8")
    start = tool_call("start_workflow", "c1", path="nested.py")
    stack.counting_tool.after_effect = asyncio.Event().wait
    made = (start, tool_call("append", "c2", line="a")) if case == "another call" else (start,)
    parent, child = await _cut_short(stack, *made)
    stack.counting_tool.after_effect = None
    if case == "changed file":
        (workspace / "nested.py").write_text(_SOURCE + "# edited\n", encoding="utf-8")
    if case == "no binding":
        with closing(sqlite3.connect(stack.store.database_path)) as records:
            records.execute("DELETE FROM workflow_calls")
            records.commit()
    model = stack.child_script(answer("never"))

    await stack.continue_agents()
    assert set(await stack.recover()) == {parent, child.run_id}

    stack.run_id = parent
    with pytest.raises(NodeFailed) as cut_short:
        await stack.result("@0", "agent", _PROMPT)
    assert (cut_short.value.kind, model.requests) == ("indeterminate", [])
    assert [run.run_id for run in stack.store.child_runs(parent)] == [child.run_id]


@pytest.mark.asyncio
async def test_agent_cut_short_inside_a_call_that_starts_no_run_goes_on_as_before(stack: WorkflowStack) -> None:
    """The rule above is for calls that start runs: an agent inside any other call is continued and told to check."""
    await stack.start()
    stack.counting_tool.after_effect = asyncio.Event().wait
    stack.child_script(calls(tool_call("append", "c1", line="a")), answer("never"))
    await stack.submit("@0", "agent", _PROMPT)
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    await stack.restart()
    stack.counting_tool.after_effect = None
    model = stack.child_script(answer("done"))

    await stack.continue_agents()
    assert await stack.recover() == [stack.run_id]

    assert await stack.result("@0", "agent", _PROMPT) == "done"
    assert "check before repeating it" in _sent(model)
    assert stack.counting_tool.lines == ["a"]


def _delegate(**arguments: object) -> dict[str, object]:
    return tool_args("delegate", "run_subagent", task="Help with this.", **arguments)


def _agent_jobs(stack: WorkflowStack) -> int:
    return len([job for job in stack.jobs.retained_jobs() if job.kind == "delegation"])


@pytest.mark.asyncio
async def test_script_tool_delegate_becomes_an_agent_node_with_a_job(stack: WorkflowStack) -> None:
    """A script's `tool("delegate", "run_subagent", ...)` runs as `agent()` does: an agent node, with a job of its own."""
    await stack.start()
    model = stack.child_script(answer("helped"))

    assert await stack.result("@0", "tool", _delegate(system_prompt="You help.")) == "helped"

    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    assert (node.kind, node.state, node.request["prompt"], node.request["system"]) == (
        "agent",
        _COMPLETED,
        "Help with this.",
        "You help.",
    )
    assert node.handle is not None
    assert (node.handle.kind, stack.node_job("@0").kind, len(model.requests)) == ("job", "delegation", 1)
    # The same call of a resumed script is the saved answer: no agent runs again.
    assert (await stack.submit("@0", "tool", _delegate(system_prompt="You help."))).result == "helped"
    assert (stack.counting_tool.calls, _agent_jobs(stack)) == (0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_denied_or_approval_required_script_tool_delegate_launches_nothing_until_approved(
    gated: WorkflowStack,
    approved: bool,
) -> None:
    """The operator's rule for `run_subagent` holds for a script's call of it: no agent starts before the decision.

    The call shows as waiting for an approval meanwhile, and a denial fails it with kind `denied`.
    """
    # With a place for one working call, which an earlier call used: the call that waited for its decision has none
    # before it starts.
    await gated.start(max_concurrent=1)
    assert await gated.result("@9", "tool", tool_args("counter", "text", size=1)) == "x"
    gated.resolver.hold_approvals = asyncio.Event()
    gated.resolver.decision = ToolApprovalDecision(approved=approved, reason=None if approved else "No.")
    gated.child_script(answer("helped"), agent="helper")

    receipt = await gated.submit("@0", "tool", _delegate(agent_name="helper"))
    await gated.until(lambda: gated.resolver.asks == ["delegate.run_subagent"])
    await gated.until(lambda: gated.store.get_run(gated.run_id).state is WorkflowRunState.WAITING)

    node = gated.store.get_node(gated.run_id, "@0")
    assert node is not None
    assert (node.kind, node.state, node.waiting_on, _agent_jobs(gated)) == (
        "agent",
        WorkflowNodeState.WAITING,
        "approval",
        0,
    )
    gated.resolver.hold_approvals.set()
    ended = await _settled(gated, receipt)
    if approved:
        assert (ended.result, _agent_jobs(gated)) == ("helped", 1)
    else:
        assert (ended.state, (ended.error or {}).get("kind"), _agent_jobs(gated)) == ("failed", "denied", 0)
        assert gated.models["helper"].requests == []


@pytest.mark.asyncio
async def test_script_tool_start_workflow_passes_the_same_gate(gated: WorkflowStack) -> None:
    """A script's `tool("workflow", "start_workflow", ...)` asks as the rule says, and then runs a run inside its run."""
    await gated.start(depth=1)
    parent = gated.run_id
    gated.resolver.hold_approvals = asyncio.Event()

    receipt = await gated.submit("@0", "tool", tool_args("workflow", "start_workflow", source=_SOURCE))
    await gated.until(lambda: gated.resolver.asks == ["workflow.start_workflow"])
    assert gated.store.child_runs(parent) == []
    gated.resolver.hold_approvals.set()
    child = await _nested(gated, parent)

    # A script is no agent: the run it starts is as deep as its own.
    assert (child.parent_run_id, child.parent_node_key, child.depth) == (parent, "@0", 1)
    assert _group(gated, child).group_key == "@0"
    await _report(gated, child.run_id, "the nested report")
    assert (await _settled(gated, receipt)).result == "the nested report"
    assert _group(gated, child).state is _COMPLETED


@pytest.mark.asyncio
async def test_script_tool_continue_subagent_is_refused_with_what_to_use(stack: WorkflowStack) -> None:
    """A session of a subagent is not a script's to continue through `tool()`: the call says so and runs nothing."""
    await stack.start()

    with pytest.raises(NodeFailed, match="continue_subagent") as refused:
        await stack.result("@0", "tool", tool_args("delegate", "continue_subagent", subagent_id="s1", task="More."))

    assert (refused.value.kind, _agent_jobs(stack)) == ("refused", 0)


@pytest.mark.asyncio
async def test_depth_limit_refuses_the_fourth_level(stack: WorkflowStack) -> None:
    """The agents of a run are one level below the agent that started it: a run at the deepest level starts none."""
    await stack.start(depth=3)
    model = stack.child_script(answer("never"))

    with pytest.raises(NodeFailed, match="the maximum delegation depth was reached") as refused:
        await stack.result("@0", "agent", _PROMPT)

    assert (refused.value.kind, model.requests, _agent_jobs(stack)) == ("refused", [], 0)


@pytest.mark.asyncio
async def test_nested_run_of_an_agent_is_one_level_deeper_than_its_parent(stack: WorkflowStack) -> None:
    """Depth is the real chain of agents: the run an agent at depth two starts is at depth two, and its agents at three."""
    await stack.start(depth=1)
    stack.child_script(calls(_START), answer("done"))
    await stack.submit("@0", "agent", _PROMPT)

    child = await _nested(stack, stack.run_id)

    assert child.depth == 2


@pytest.mark.asyncio
async def test_workflow_agent_message_lands_in_the_originating_thread(stack: WorkflowStack) -> None:
    """A workflow agent's tools act in the room and the thread its run was started in, where `matrix_message` sends.

    `matrix_message` takes its target from the call's context (`resolve_context_thread_id`), so the context the
    agent's tool call runs in is what decides where its message lands.
    """
    await stack.start()
    run = stack.store.get_run(stack.run_id)
    seen: list[tuple[str, str | None, str | None]] = []

    def where() -> None:
        context = get_tool_runtime_context()
        assert context is not None
        thread = resolve_context_thread_id(context, room_id=context.room_id, thread_id=None)
        seen.append((context.room_id, thread, context.requester_id))

    stack.counting_tool.before_effect = where
    stack.child_script(calls(tool_call("append", "c1", line="a")), answer("done"))

    assert await stack.result("@0", "agent", _PROMPT) == "done"

    assert seen == [(run.room_id, run.thread_root_event_id, run.owner_user_id)]
    assert (run.room_id, run.thread_root_event_id) == ("!room:example.test", "$thread:example.test")


@pytest.mark.asyncio
async def test_restart_during_a_scripts_nested_run_ends_its_call_indeterminate(stack: WorkflowStack) -> None:
    """A script's `start_workflow` call that a restart cut short may have done anything: it is not made again.

    The run it started goes on by itself, as the one run it is.
    """
    await stack.start()
    parent = stack.run_id
    start = tool_args("workflow", "start_workflow", source=_SOURCE)
    await stack.submit("@0", "tool", start)
    child = await _nested(stack, parent)

    await stack.restart()
    assert set(await stack.recover()) == {parent, child.run_id}

    stack.run_id = parent
    with pytest.raises(NodeFailed) as cut_short:
        await stack.result("@0", "tool", start)
    assert cut_short.value.kind == "indeterminate"
    inside = [(run.run_id, run.state) for run in stack.store.child_runs(parent)]
    assert inside == [(child.run_id, WorkflowRunState.RUNNING)]


@pytest.mark.asyncio
async def test_a_run_started_by_the_script_of_a_narrowed_run_has_no_more_tools_than_that_run(
    stack: WorkflowStack,
) -> None:
    """The broker's context has no persona, so the run inside takes the one its outer run was saved with."""
    persona = SubagentPersona("inline", "counter", "Count.", tools=("workflow", "counter.text"))
    await stack.start(persona=asdict(persona))
    parent = stack.run_id
    with pytest.raises(NodeFailed, match="not a tool of the agent"):
        await stack.result("@0", "tool", tool_args("counter", "append", line="outer"))

    await stack.submit("@1", "tool", tool_args("workflow", "start_workflow", source=_SOURCE))
    child = await _nested(stack, parent)
    stack.run_id = child.run_id

    assert child.persona is not None
    assert child.persona["tools"] == ["workflow", "counter.text"]
    with pytest.raises(NodeFailed, match="not a tool of the agent"):
        await stack.result("@0", "tool", tool_args("counter", "append", line="widened"))
    assert stack.counting_tool.lines == []


@pytest.mark.asyncio
async def test_script_tool_delegate_above_the_rate_limit_waits_and_then_runs(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate of a delegated call is a tool call of the script: above the calls per minute it waits, as they do."""
    monkeypatch.setattr(engine_module, "_RATE_WAIT_SECONDS", 0.02)
    stack.limits = WorkflowLimits(max_tool_calls_per_minute=1)
    await stack.start()
    assert await stack.result("@0", "tool", tool_args("counter", "text", size=1)) == "x"
    model = stack.child_script(answer("helped"))

    receipt = await stack.submit("@1", "tool", _delegate(system_prompt="You help."))
    await asyncio.sleep(0.1)
    assert (receipt.state, model.requests) == ("queued", [])
    # A minute later, as the attempt's records tell it: the earlier call is old enough.
    with closing(sqlite3.connect(stack.script_store.database_path)) as records:
        records.execute("UPDATE script_calls SET created_at = '2000-01-01T00:00:00+00:00'")
        records.commit()

    assert (await _settled(stack, receipt)).result == "helped"
