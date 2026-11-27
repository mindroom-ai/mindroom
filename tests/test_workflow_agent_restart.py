"""Tests for the agents of a workflow run across a restart of MindRoom, and for calls that wait for an approval.

Level: engine harness (`WorkflowStack`) with the real job runtime and real delegation; each agent's model is scripted.
A restart is the harness's `restart()`: the job runtime keeps the agents' jobs, and the coordinator's pass continues them.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.approval import manager as approval_manager
from mindroom.delegation import continuation
from mindroom.delegation.background import delegation_child
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs import nodes as nodes_module
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from mindroom.workflow_runs.structured import schema_prompt
from tests.fakes.scripted_model import Await, answer, calls, tool_call, usage
from tests.tool_job_helpers import JobApprovalCards, wait_for_status
from tests.workflow_helpers import NodeFailed, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_jobs.runtime import BackgroundJob
    from mindroom.workflow_runs.models import WorkflowNode

_PROMPT: dict[str, object] = {"prompt": "Do the long task"}
_APPEND = tool_args("counter", "append", line="a")


async def _opened(stack: WorkflowStack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    stack.continues = lambda job: job.kind == "delegation"
    await stack.open_jobs()
    stack.script_agents(monkeypatch)
    await stack.start()


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with the real job runtime, scripted agents and one started run; no tool asks for approval."""
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        await _opened(stack, monkeypatch)
        yield stack
        await stack.close()


@pytest_asyncio.fixture
async def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The same harness, whose operator asks for approval before `append` runs; `cards` holds the open cards."""
    with workflow_stack(tmp_path, extra_tools=("workflow",)) as stack:
        await _opened(stack, monkeypatch)
        cards = JobApprovalCards(asyncio.get_running_loop().create_future(), stack.jobs.journal)
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
        stack.cards = cards  # type: ignore[attr-defined]
        yield stack
        if not cards.decision.done():
            cards.decision.cancel()
        await stack.close()


def _node(stack: WorkflowStack, key: str = "@0") -> WorkflowNode:
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    return node


def _node_jobs(stack: WorkflowStack) -> list[BackgroundJob]:
    return [job for job in stack.jobs.retained_jobs() if job.kind == "delegation"]


def _sent(stack: WorkflowStack, request: int = -1) -> str:
    """Return what the owner's copy was sent as the user's message of one of its model requests."""
    messages = stack.models["watcher"].requests[request].messages
    return str([message.content for message in messages if message.role == "user"][-1])


async def _working(stack: WorkflowStack, *, after_append: bool) -> None:
    """Submit the agent call and leave its turn hanging in a model request, after one `append` or before any call."""
    hang = [Await(asyncio.Event().wait), *answer("never")]
    turns = [calls(tool_call("append", "c1", line="a"), metrics=usage(7, 2)), hang] if after_append else [hang]
    model = stack.child_script(*turns)
    await stack.submit("@0", "agent", _PROMPT)
    await stack.until(lambda: len(model.requests) == len(turns))


async def _restarted(stack: WorkflowStack, *turns: object, continued: bool = True) -> None:
    """Restart the primary, script the agent's next turns, and run the starting primary's passes.

    With *continued* the coordinator continues the agents' jobs; without, its bot is no longer in the room, so each
    job ends as the restart left it.
    """
    await stack.restart()
    stack.child_script(*turns)
    await stack.continue_agents(joined=continued)
    assert await stack.recover() == [stack.run_id]


@pytest.mark.asyncio
async def test_running_agent_is_continued_not_restarted_and_its_node_attaches(stack: WorkflowStack) -> None:
    """A restart cuts the turn short; its job goes on as the same subagent, and the replayed call waits for it."""
    await _working(stack, after_append=True)
    before = stack.node_job("@0")

    await _restarted(stack, answer("done"))

    assert await stack.result("@0", "agent", _PROMPT) == "done"
    node, [job] = _node(stack), _node_jobs(stack)
    assert (node.generation, node.attempt, stack.counting_tool.lines) == (1, 2, ["a"])
    assert job.job_id == before.job_id
    child = delegation_child(job)
    assert (child.subagent_id, child.workflow_run_id, child.workflow_node_key, child.workflow_generation) == (
        delegation_child(before).subagent_id,
        stack.run_id,
        "@0",
        1,
    )
    assert "<interrupted_task>\nDo the long task\n</interrupted_task>" in _sent(stack)


@pytest.mark.asyncio
async def test_restart_before_the_first_tool_call_continues_with_the_full_prompt_at_the_same_depth(
    stack: WorkflowStack,
) -> None:
    """The child's task is only its node's label: the continuation is sent the whole prompt, which the run keeps."""
    prompt = "Summarize. " + "x" * 5_000
    stack.child_script([Await(asyncio.Event().wait), *answer("never")])
    await stack.submit("@0", "agent", {"prompt": prompt})
    await stack.until(lambda: len(stack.models["watcher"].requests) == 1)

    await _restarted(stack, answer("done"))

    assert await stack.result("@0", "agent", {"prompt": prompt}) == "done"
    job = stack.node_job("@0")
    child = delegation_child(job)
    assert (len(child.task), child.depth, _node(stack).generation) == (80, 1, 1)
    assert prompt in _sent(stack)
    # The run keeps the prompt; the job, whose every snapshot repeats its adapter, gets a mark for it and no copy.
    assert "x" * 100 not in json.dumps(job.adapter)


@pytest.mark.asyncio
async def test_continuation_after_a_tool_call_is_sent_the_whole_prompt_however_long(stack: WorkflowStack) -> None:
    """What the call asks for may stand at the end of a long prompt: the continued agent is sent all of it."""
    prompt = "Append a line. " + "x" * 9_000 + " Then answer with the word tide."
    model = stack.child_script(
        calls(tool_call("append", "c1", line="a")),
        [Await(asyncio.Event().wait), *answer("never")],
    )
    await stack.submit("@0", "agent", {"prompt": prompt})
    await stack.until(lambda: len(model.requests) == 2)

    await _restarted(stack, calls(tool_call("append", "c2", line="b")), [Await(asyncio.Event().wait), *answer("never")])
    await stack.until(lambda: len(stack.models["watcher"].requests) == 2)

    assert f"<interrupted_task>\n{prompt}\n</interrupted_task>" in _sent(stack, 0)
    # The run keeps the prompt: the job, whose every snapshot repeats its adapter, keeps only what was put around it.
    assert "x" * 100 not in json.dumps(stack.node_job("@0").adapter)

    await _restarted(stack, answer("tide"))

    # A second restart puts the same prompt, whole, into what the agent is sent then.
    assert await stack.result("@0", "agent", {"prompt": prompt}) == "tide"
    assert (prompt in _sent(stack), stack.counting_tool.lines) == (True, ["a", "b"])


@pytest.mark.asyncio
async def test_continuation_usage_is_added_once(stack: WorkflowStack) -> None:
    """The node's usage is what the cut-short turn and its continuation used together, however often it is read."""
    await _working(stack, after_append=True)

    await _restarted(stack, answer("done", usage(5, 3)))

    assert await stack.result("@0", "agent", _PROMPT) == "done"
    await stack.until(lambda: stack.node_job("@0").consumed)
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)
    assert (await stack.submit("@0", "agent", _PROMPT)).cached
    totals = stack.store.usage_summary(stack.run_id).totals
    assert (totals["input_tokens"], totals["output_tokens"], totals["tool_calls"]) == (12, 5, 1)


@pytest.mark.asyncio
async def test_agent_that_cannot_be_continued_after_a_tool_call_is_indeterminate(stack: WorkflowStack) -> None:
    """A turn that had called a tool and cannot go on may have done anything: its call is not run again on its own."""
    await _working(stack, after_append=True)

    await _restarted(stack, continued=False)

    with pytest.raises(NodeFailed) as stopped:
        await stack.result("@0", "agent", _PROMPT)
    assert (stopped.value.kind, _node(stack).state) == ("indeterminate", WorkflowNodeState.INDETERMINATE)
    assert (stack.node_job("@0").status, stack.counting_tool.lines, len(_node_jobs(stack))) == ("interrupted", ["a"], 1)


@pytest.mark.asyncio
async def test_agent_that_cannot_be_continued_and_left_no_record_is_indeterminate(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn whose run cannot be found is not proven to have done nothing: its call is not run again on its own."""
    await _working(stack, after_append=True)
    await _restarted(stack, continued=False)

    async def missing(*_args: object, **_options: object) -> None:
        return None

    monkeypatch.setattr(agent_nodes, "read_child_run", missing)

    with pytest.raises(NodeFailed) as stopped:
        await stack.result("@0", "agent", _PROMPT)
    assert (stopped.value.kind, _node(stack).generation, stack.counting_tool.lines) == ("indeterminate", 1, ["a"])


@pytest.mark.asyncio
async def test_agent_whose_record_is_missing_at_the_restart_is_not_sent_its_prompt_again(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn whose run cannot be found is not proven to have called no tool: nothing continues it or runs it again."""
    await _working(stack, after_append=True)

    async def missing(*_args: object, **_options: object) -> None:
        return None

    monkeypatch.setattr(continuation, "read_child_run", missing)
    monkeypatch.setattr(agent_nodes, "read_child_run", missing)
    await _restarted(stack, calls(tool_call("append", "c2", line="a")), answer("done"))

    with pytest.raises(NodeFailed) as stopped:
        await stack.result("@0", "agent", _PROMPT)
    assert (stopped.value.kind, _node(stack).generation, stack.counting_tool.lines) == ("indeterminate", 1, ["a"])


@pytest.mark.asyncio
async def test_agent_that_cannot_be_continued_before_any_tool_call_runs_again(stack: WorkflowStack) -> None:
    """A turn that had called no tool did nothing: the replay runs its call again, as a new job."""
    await _working(stack, after_append=False)
    first = stack.node_job("@0").job_id

    await _restarted(stack, answer("second try"), continued=False)

    assert await stack.result("@0", "agent", _PROMPT) == "second try"
    assert (_node(stack).generation, stack.node_job("@0").job_id != first) == (2, True)
    assert [job.status for job in _node_jobs(stack) if job.job_id == first] == ["interrupted"]


@pytest.mark.asyncio
async def test_agent_of_a_run_that_was_cancelled_while_jobs_were_off_is_not_continued(stack: WorkflowStack) -> None:
    """A run that ended could not stop a job it could not reach: the job ends when it would go on, and runs nothing.

    Nobody could read what the job's turn had done either, so its call is not one to run again unasked.
    """
    await _working(stack, after_append=False)
    job_id = stack.node_job("@0").job_id
    stack.has_jobs = False
    await stack.engine.cancel(stack.run_id, reason="Stopped.")
    stack.has_jobs = True
    assert (_node(stack).state, stack.node_job("@0").status) == (WorkflowNodeState.INDETERMINATE, "running")

    await stack.restart()
    model = stack.child_script(answer("must not be asked"))
    await stack.continue_agents()

    await wait_for_status(stack.jobs, job_id, "interrupted")
    assert (model.requests, stack.store.get_run(stack.run_id).state) == ([], WorkflowRunState.CANCELLED)


@pytest.mark.asyncio
async def test_agent_of_a_run_whose_job_was_stopped_is_not_continued(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop of the run's job wins over the restart: its agent is not continued before the run itself is cancelled."""
    monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)
    handle = json.loads(await stack.call("workflow", action="start", source="def main(args):\n    return None\n"))
    stack.run_id = handle["run_id"]
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)
    await _working(stack, after_append=False)
    job_id = stack.node_job("@0").job_id

    await stack.restart(kill=True)
    # Arranged, not played: a primary that died with only the Stop of the run's job saved leaves that job cancelled
    # at the next start, and the run and its call as they were until the run's own pass.
    entry = stack.jobs._entries[handle["job_id"]]
    entry.job = replace(entry.job, status="cancelled")
    assert _node(stack).state is WorkflowNodeState.RUNNING
    model = stack.child_script(calls(tool_call("append", "c1", line="after stop")), answer("must not be asked"))

    await stack.continue_agents()

    await wait_for_status(stack.jobs, job_id, "interrupted")
    assert (model.requests, stack.counting_tool.lines) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("after_append", [False, True])
async def test_replay_that_reaches_the_call_before_its_job_was_continued_waits_for_that_decision(
    stack: WorkflowStack,
    after_append: bool,
) -> None:
    """The replay attaches to no job that still waits to be continued: how that job goes on decides the call.

    So a job that then cannot be continued ends the call as it would have before the replay: a turn that called no
    tool runs again as the next agent, and one that had is indeterminate. The run does not fail for the order.
    """
    await _working(stack, after_append=after_append)
    await stack.restart()
    stack.child_script(answer("second try"))
    assert await stack.recover() == [stack.run_id]

    with pytest.raises(ScriptRuntimeUnavailableError, match="has not been continued yet"):
        await stack.submit("@0", "agent", _PROMPT)
    assert _node(stack).state is WorkflowNodeState.RUNNING
    await stack.continue_agents(joined=False)

    if after_append:
        with pytest.raises(NodeFailed) as stopped:
            await stack.result("@0", "agent", _PROMPT)
        assert (stopped.value.kind, _node(stack).generation) == ("indeterminate", 1)
    else:
        assert await stack.result("@0", "agent", _PROMPT) == "second try"
        assert _node(stack).generation == 2
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_expired_card_denies_the_call_and_the_child_goes_on(gated: WorkflowStack) -> None:
    """A card nobody answered is a denial of that call, as for every subagent; the engine sets no deadline of its own."""
    cards: JobApprovalCards = gated.cards  # type: ignore[attr-defined]
    gated.child_script(calls(tool_call("append", "c1", line="a")), answer("not sent"))
    cards.decision.set_result(BackgroundApprovalDecision("expired", "Nobody answered."))

    assert await gated.result("@0", "agent", _PROMPT) == "not sent"

    assert (gated.counting_tool.lines, _node(gated).state) == ([], WorkflowNodeState.COMPLETED)


@pytest.mark.asyncio
async def test_script_tool_approval_shows_the_node_and_the_run_waiting(gated: WorkflowStack) -> None:
    """While a script's own tool call waits for a person, its node and its run say so; the approved call runs once."""
    gated.resolver.hold_approvals = asyncio.Event()
    receipt = await gated.submit("@0", "tool", _APPEND)
    await gated.until(lambda: gated.store.get_run(gated.run_id).state is WorkflowRunState.WAITING)

    node = _node(gated)
    assert (node.state, node.waiting_on, gated.counting_tool.lines) == (WorkflowNodeState.WAITING, "approval", [])
    assert (await nodes_module.node_receipt(gated.engine, gated.attempt, receipt.id, wait=0)).state == "waiting"
    gated.resolver.hold_approvals.set()

    assert await gated.result("@0", "tool", _APPEND) == "appended"
    assert (gated.counting_tool.lines, _node(gated).waiting_on) == (["a"], None)
    assert gated.store.get_run(gated.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_script_tool_approval_after_restart_asks_again_and_runs_once(gated: WorkflowStack) -> None:
    """A call that waited for a person when the primary stopped never ran: the replay asks again, as a new call."""
    gated.resolver.hold_approvals = asyncio.Event()
    await gated.submit("@0", "tool", _APPEND)
    await gated.until(lambda: _node(gated).state is WorkflowNodeState.WAITING)

    await gated.restart(kill=True)
    assert await gated.recover() == [gated.run_id]

    assert await gated.result("@0", "tool", _APPEND) == "appended"
    assert (_node(gated).generation, gated.counting_tool.lines) == (2, ["a"])
    assert gated.resolver.asks == ["counter.append"]


@pytest.mark.asyncio
async def test_job_that_ended_while_mindroom_was_down_is_read_once_it_is_up(stack: WorkflowStack) -> None:
    """An agent whose answer was saved by its job, and not yet by its node, is not asked again after a restart."""
    gate = asyncio.Event()
    stack.child_script([Await(gate.wait), *answer("just in time")])
    await stack.submit("@0", "agent", _PROMPT)
    await stack.engine.close()
    gate.set()
    await wait_for_status(stack.jobs, stack.node_job("@0").job_id, "completed")

    await stack.restart()
    assert await stack.recover() == [stack.run_id]

    receipt = await stack.submit("@0", "agent", _PROMPT)
    assert (receipt.state, receipt.result, _node(stack).generation, len(_node_jobs(stack))) == (
        "completed",
        "just in time",
        1,
        1,
    )


@pytest.mark.asyncio
async def test_restart_before_first_tool_call_keeps_schema_instruction(stack: WorkflowStack) -> None:
    """A structured agent restarted before its first tool call still receives its schema instruction."""
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
    request: dict[str, object] = {"prompt": "Classify this item.", "schema": schema}
    stack.child_script([Await(asyncio.Event().wait), *answer("never")])
    await stack.submit("@0", "agent", request)
    await stack.until(lambda: len(stack.models["watcher"].requests) == 1)

    await _restarted(stack, answer('```json\n{"verdict": "ok"}\n```'))

    assert await stack.result("@0", "agent", request) == {"verdict": "ok"}
    assert schema_prompt("Classify this item.", schema) in _sent(stack)
    assert "verdict" not in json.dumps(stack.node_job("@0").adapter)
