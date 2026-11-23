"""Tests for agent nodes: one `agent()` call of a workflow script as one delegation job.

Level: engine harness (`WorkflowStack`) with the real job runtime and real delegation; each agent's model is scripted.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.approval import manager as approval_manager
from mindroom.delegation.background import delegation_child
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.llm.errors import ProviderError
from mindroom.orchestration import workflow_runtime
from mindroom.orchestration.tool_job_runtime import _follow_up_candidates
from mindroom.orchestration.workflow_runtime import WorkflowRuntimeLifecycle
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.models import ScriptRunState
from mindroom.script_runs.reasons import PLUGIN_TOOLS_CHANGED
from mindroom.tool_jobs import runtime as tool_job_runtime
from mindroom.tool_jobs.client_cancel import conversation_jobs
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.publication import _jobs_state_content
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.models import DispatchHandle, WorkflowNodeState, WorkflowRunState
from mindroom.workflow_runs.store import SupersededAttemptError
from tests.delegation_helpers import offered_tools
from tests.fakes.scripted_model import Await, answer, calls, tool_call, usage
from tests.scripted_agent_helpers import raising
from tests.tool_job_helpers import JobApprovalCards, wait_for_status
from tests.workflow_helpers import NodeFailed, WorkflowStack, set_run, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_jobs.runtime import BackgroundJob, JobGrant
    from mindroom.workflow_runs.models import WorkflowNode

_PROMPT: dict[str, object] = {"prompt": "Say hi"}


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with the real job runtime, scripted agents and one started run; no tool asks for approval."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()
        yield stack
        await stack.close()


@pytest.mark.asyncio
async def test_agent_node_runs_a_child_job_and_saves_the_raw_answer_and_usage(stack: WorkflowStack) -> None:
    """The node's result is what the child wrote, with no receipt after it; its job is the run's, at the owner's depth."""
    stack.child_script(answer("hi", usage(11, 3)))

    assert await stack.result("@0", "agent", {"prompt": "Say hi"}) == "hi"

    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    # The answer is saved first and the job acknowledged after it.
    await stack.until(lambda: stack.node_job("@0").consumed)
    job, child = stack.node_job("@0"), delegation_child(stack.node_job("@0"))
    assert (node.state, node.generation, node.label) == (WorkflowNodeState.COMPLETED, 1, "Say hi")
    assert (job.kind, job.depth, job.status, job.consumed) == ("delegation", 0, "completed", True)
    assert (child.workflow_run_id, child.workflow_node_key, child.workflow_generation) == (stack.run_id, "@0", 1)
    assert (child.task, child.depth, child.child_agent_name) == ("Say hi", 1, "watcher")
    ids = {"job_id": job.job_id, "delegation_id": child.delegation_id, "subagent_id": child.subagent_id}
    # With the agent and the model the call resolved to, which its arguments need not name.
    assert node.links == ids | {"agent": "watcher", "model": "default"}
    listed = json.loads(await stack.call("get_workflow", run_id=stack.run_id, nodes="all"))["nodes"]
    assert [shown["links"] for shown in listed] == [node.links]
    totals = stack.store.usage_totals(stack.run_id)
    assert (totals["input_tokens"], totals["output_tokens"]) == (11, 3)


@pytest_asyncio.fixture
async def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The same harness, whose operator asks for approval before `append` runs; `cards` holds the open cards."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    with workflow_stack(tmp_path, extra_tools=("workflow",)) as stack:
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        cards = JobApprovalCards(asyncio.get_running_loop().create_future(), stack.jobs.journal)
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
        stack.cards = cards  # type: ignore[attr-defined]
        await stack.start()
        yield stack
        if not cards.decision.done():
            cards.decision.cancel()
        await stack.close()


def _node(stack: WorkflowStack, key: str = "@0") -> WorkflowNode:
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    return node


def _hanging(stack: WorkflowStack, text: str = "late", agent: str = "watcher") -> asyncio.Event:
    """Script a turn that waits until the returned event is set, then answers *text*."""
    gate = asyncio.Event()
    stack.child_script([Await(gate.wait), *answer(text)], agent=agent)
    return gate


async def _replayed(stack: WorkflowStack) -> None:
    """Lose the attempt to an interruption of its process and let the pass start the next one."""
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)
    assert await stack.maintain() == [stack.run_id]


def _node_jobs(stack: WorkflowStack) -> list[BackgroundJob]:
    return [job for job in stack.jobs.retained_jobs() if job.kind == "delegation"]


@pytest.mark.asyncio
async def test_failed_node_reruns_on_resume_as_a_new_job(stack: WorkflowStack) -> None:
    """A turn that failed is asked again by the next attempt, as the node's next generation with a job of its own."""
    stack.child_script([raising(ProviderError("The provider is down.", provider="Synthetic"))])
    with pytest.raises(NodeFailed, match="The provider is down") as failed:
        await stack.result("@0", "agent", _PROMPT)
    first = stack.node_job("@0")
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)
    stack.child_script(answer("ok"))

    assert await stack.result("@0", "agent", _PROMPT) == "ok"

    assert (failed.value.kind, first.status) == ("failed", "failed")
    assert (_node(stack).generation, stack.node_job("@0").job_id != first.job_id) == (2, True)
    assert len(_node_jobs(stack)) == 2


@pytest.mark.asyncio
async def test_saved_answer_is_reused_and_no_agent_runs_again(stack: WorkflowStack) -> None:
    """A resume returns the saved answer of a completed turn, marked cached: no model is asked, and nothing is paid twice."""
    model = stack.child_script(answer("hi"))
    assert await stack.result("@0", "agent", _PROMPT) == "hi"
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)

    receipt = await stack.submit("@0", "agent", _PROMPT, {"timeout": 5})

    assert (receipt.cached, receipt.result, len(model.requests), len(_node_jobs(stack))) == (True, "hi", 1, 1)


@pytest.mark.asyncio
async def test_redelivered_request_attaches_and_starts_one_child(stack: WorkflowStack) -> None:
    """The same call sent twice by one attempt is one node and one job, also while the turn still runs."""
    gate = _hanging(stack, "once")
    first = await stack.submit("@0", "agent", _PROMPT)

    again = await stack.submit("@0", "agent", _PROMPT)
    gate.set()

    assert (first.id, first.state) == (again.id, "running")
    assert await stack.result("@0", "agent", _PROMPT) == "once"
    assert len(_node_jobs(stack)) == 1


@pytest.mark.asyncio
async def test_next_attempt_waits_for_the_job_that_still_runs_and_starts_none(stack: WorkflowStack) -> None:
    """An agent outlives the attempt that asked for it: the replay finds its node live and takes the same answer."""
    gate = _hanging(stack, "survived")
    await stack.submit("@0", "agent", _PROMPT)
    await _replayed(stack)

    attached = await stack.submit("@0", "agent", _PROMPT)
    gate.set()

    assert (attached.state, attached.cached) == ("running", False)
    assert await stack.result("@0", "agent", _PROMPT) == "survived"
    assert (_node(stack).generation, _node(stack).attempt, len(_node_jobs(stack))) == (1, 2, 1)


@pytest.mark.asyncio
async def test_job_that_ended_between_two_attempts_is_copied_and_not_run_again(stack: WorkflowStack) -> None:
    """The answer of a turn that ended while no attempt waited for it is read from its job, and the job acknowledged."""
    gate = _hanging(stack, "meanwhile")
    await stack.submit("@0", "agent", _PROMPT)
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)
    await stack.engine.settle(stack.store.get_run(stack.run_id), replay=True)
    gate.set()
    await wait_for_status(stack.jobs, stack.node_job("@0").job_id, "completed")
    assert (_node(stack).state, stack.node_job("@0").consumed) == (WorkflowNodeState.RUNNING, False)
    assert await stack.maintain() == [stack.run_id]

    receipt = await stack.submit("@0", "agent", _PROMPT)

    assert (receipt.state, receipt.result, receipt.cached) == ("completed", "meanwhile", True)
    assert (stack.node_job("@0").consumed, len(_node_jobs(stack))) == (True, 1)


@pytest.mark.asyncio
async def test_job_that_was_never_started_ends_its_call_as_safe_to_run_again(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The node names its job before the job starts: when the start then fails, nothing ran, and the call says so."""
    start = stack.jobs.start

    async def failing_start(*_args: object, **_options: object) -> None:
        msg = "The journal is gone."
        raise OSError(msg)

    monkeypatch.setattr(stack.jobs, "start", failing_start)
    with pytest.raises(OSError, match="The journal is gone"):
        await stack.submit("@0", "agent", _PROMPT)
    monkeypatch.setattr(stack.jobs, "start", start)

    with pytest.raises(NodeFailed) as stopped:
        await stack.result("@0", "agent", _PROMPT)

    node = _node(stack)
    assert (stopped.value.kind, node.state, node.handle and node.handle.dispatched) == (
        "interrupted",
        WorkflowNodeState.FAILED,
        False,
    )
    assert _node_jobs(stack) == []
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)
    stack.child_script(answer("second try"))
    assert await stack.result("@0", "agent", _PROMPT) == "second try"


@pytest.mark.asyncio
async def test_request_cancelled_while_its_agent_starts_leaves_a_call_that_is_waited_for(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script's request can be cut off after the job started: the job is the node's, and its answer still arrives."""
    start = stack.jobs.start

    async def started_then_cancelled(job_id: str, **options: Any) -> None:  # noqa: ANN401 - the runtime's own options.
        _, claim = await start(job_id, **options)
        await stack.jobs.release_wait(job_id, claim)
        raise asyncio.CancelledError

    monkeypatch.setattr(stack.jobs, "start", started_then_cancelled)
    stack.child_script(answer("still answered"))
    with pytest.raises(asyncio.CancelledError):
        await stack.submit("@0", "agent", _PROMPT)
    monkeypatch.setattr(stack.jobs, "start", start)

    assert await stack.result("@0", "agent", _PROMPT) == "still answered"
    assert len(_node_jobs(stack)) == 1


@pytest.mark.asyncio
async def test_outcome_that_the_node_could_not_take_is_given_back_to_its_job(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait whose run moved on saves nothing and keeps nothing: the job's outcome is the next reader's."""

    def moved_on(*_args: object, **_options: object) -> bool:
        msg = "The run moved on."
        raise SupersededAttemptError(msg)

    monkeypatch.setattr(stack.store, "settle_node", moved_on)
    stack.child_script(answer("for the next reader"))
    await stack.submit("@0", "agent", _PROMPT)
    job_id = stack.node_job("@0").job_id
    await wait_for_status(stack.jobs, job_id, "completed")
    await stack.until(lambda: not stack.engine._tasks.get(stack.run_id))

    waited = await stack.jobs.wait(job_id, owner=stack.context.execution_identity, depth=0)

    assert (waited.claim is not None, waited.job.consumed) == (True, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["fails", "finds nothing"])
async def test_agent_whose_record_cannot_be_read_after_it_wrote_is_not_run_again(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    read: str,
) -> None:
    """Without the record of the turn nobody knows what it did: the call is indeterminate.

    A read that failed took nothing from the job, whose outcome stays the next reader's; a record that is not there
    is the job's whole outcome, which the node has taken.
    """

    async def unreadable(*_args: object, **_options: object) -> None:
        if read == "fails":
            msg = "The session store is locked."
            raise OSError(msg)

    monkeypatch.setattr(agent_nodes, "read_child_run", unreadable)
    stack.child_script(calls(tool_call("append", "c1", line="a")), answer("appended it"))

    with pytest.raises(NodeFailed) as unknown:
        await stack.result("@0", "agent", _PROMPT)

    assert (unknown.value.kind, _node(stack).state, _node(stack).result) == (
        "indeterminate",
        WorkflowNodeState.INDETERMINATE,
        None,
    )
    await stack.until(lambda: not stack.engine._tasks.get(stack.run_id))
    waited = await stack.jobs.wait(stack.node_job("@0").job_id, owner=stack.context.execution_identity, depth=0)
    assert (waited.claim is not None, waited.job.consumed) == (True, read == "finds nothing")
    await stack.jobs.release_wait(waited.job.job_id, waited.claim)
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)
    again = await stack.submit("@0", "agent", _PROMPT)
    assert (again.state, _node(stack).generation, stack.counting_tool.lines) == ("failed", 1, ["a"])


@pytest.mark.asyncio
async def test_job_that_was_started_and_is_gone_leaves_its_call_indeterminate(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that started and that the job runtime no longer holds may have done anything: it is not run again."""
    _hanging(stack)
    await stack.submit("@0", "agent", _PROMPT)
    assert _node(stack).handle == DispatchHandle("job", stack.node_job("@0").job_id, dispatched=True)
    await _replayed(stack)
    monkeypatch.setattr(stack.jobs, "retained_job", lambda _job_id: None)

    receipt = await stack.submit("@0", "agent", _PROMPT)

    assert (receipt.state, receipt.error and receipt.error["kind"]) == ("failed", "indeterminate")
    assert (_node(stack).state, _node(stack).generation) == (WorkflowNodeState.INDETERMINATE, 1)


@pytest.mark.asyncio
async def test_edited_prompt_at_the_same_key_starts_a_new_job_and_stops_the_old_one(stack: WorkflowStack) -> None:
    """A call that changed replaces its node: the job of the old generation is cancelled, and its late end changes nothing."""
    gate = _hanging(stack, "old answer")
    await stack.submit("@0", "agent", {"prompt": "old"})
    old = stack.node_job("@0").job_id
    await _replayed(stack)
    stack.child_script(answer("new answer"))

    # Nothing waits for the old job under the run's lock: the call is asked again, as the script's SDK does.
    with pytest.raises(ScriptRuntimeUnavailableError, match="is stopping"):
        await stack.submit("@0", "agent", {"prompt": "new"})
    assert not stack.engine._condition(stack.run_id).locked()
    await wait_for_status(stack.jobs, old, "cancelled")
    assert await stack.result("@0", "agent", {"prompt": "new"}) == "new answer"
    gate.set()

    node = _node(stack)
    assert (node.generation, node.result, stack.node_job("@0").job_id != old) == (2, "new answer", True)


@pytest.mark.asyncio
async def test_cancelled_run_stops_its_agents_and_a_resume_runs_them_again(stack: WorkflowStack) -> None:
    """A run that ended has no live work: its agent's job is cancelled with it, and the node with the job."""
    _hanging(stack)
    await stack.submit("@0", "agent", _PROMPT)
    job_id = stack.node_job("@0").job_id

    await stack.engine.cancel(stack.run_id, reason="Stopped.")
    await wait_for_status(stack.jobs, job_id, "cancelled")

    node = _node(stack)
    assert (node.state, node.error and node.error["kind"]) == (WorkflowNodeState.CANCELLED, "cancelled")
    await stack.resume(stack.run_id)
    stack.child_script(answer("again"))
    assert await stack.result("@0", "agent", _PROMPT) == "again"
    assert _node(stack).generation == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped_in", ["its next request", "the call itself"])
async def test_a_stopped_agent_that_made_a_call_is_not_run_again_without_rerun(
    stack: WorkflowStack,
    stopped_in: str,
) -> None:
    """A Stop waits for the agent's job to end and reads what its turn did: a turn that called a tool may have written.

    The agent appended its line, or was still inside the call that appends it, when the run was stopped. A resume
    raises for its node until `rerun` names it, as it does for a tool call whose end nobody saw.
    """
    wrote, gate = asyncio.Event(), asyncio.Event()

    async def held() -> None:
        wrote.set()
        await gate.wait()

    if stopped_in == "the call itself":
        stack.counting_tool.after_effect = held
    stack.child_script(calls(tool_call("append", "c1", line="a")), [Await(held), *answer("done")])
    await stack.submit("@0", "agent", _PROMPT)
    await asyncio.wait_for(wrote.wait(), 5)

    await stack.engine.cancel(stack.run_id, reason="Stopped.")

    node = _node(stack)
    assert (node.state, node.error and node.error["kind"]) == (WorkflowNodeState.INDETERMINATE, "indeterminate")
    assert stack.node_job("@0").status == "cancelled"
    stack.counting_tool.after_effect = None
    await stack.resume(stack.run_id)
    stack.child_script(calls(tool_call("append", "c2", line="a")), answer("again"))
    with pytest.raises(NodeFailed) as refused:
        await stack.result("@0", "agent", _PROMPT)
    assert (refused.value.kind, stack.counting_tool.lines, _node(stack).generation) == ("indeterminate", ["a"], 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(("wrote", "lines"), [(True, ["sent"]), (False, ["sent"])])
async def test_agent_that_failed_after_it_called_a_tool_is_not_run_again_without_rerun(
    stack: WorkflowStack,
    wrote: bool,
    lines: list[str],
) -> None:
    """A provider error after the agent's write leaves the write made: only an agent that had called no tool reruns.

    The reproducer of the second full review (Astra B5).
    """
    lost = [raising(ProviderError("The request failed.", provider="Synthetic"))]
    stack.child_script(*([calls(tool_call("append", "c1", line="sent"))] if wrote else []), lost)
    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", _PROMPT)
    assert failed.value.kind == ("indeterminate" if wrote else "failed")

    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)
    stack.child_script(calls(tool_call("append", "c2", line="sent")), answer("Recorded."))
    with suppress(NodeFailed):
        await stack.result("@0", "agent", _PROMPT)

    assert (stack.counting_tool.lines, _node(stack).generation) == (lines, 1 if wrote else 2)


@pytest.mark.asyncio
async def test_agent_that_timed_out_after_it_called_a_tool_is_not_run_again_without_rerun(stack: WorkflowStack) -> None:
    """Its time ran out after its write: the node is not `timeout`, which a resume would run again.

    The reproducer of the second full review (Opus M1b).
    """
    stack.child_script(calls(tool_call("append", "c1", line="a")), [Await(asyncio.Event().wait), *answer("done")])
    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", _PROMPT, {"timeout": 1.0})
    assert (failed.value.kind, stack.counting_tool.lines) == ("indeterminate", ["a"])

    await stack.engine.cancel(stack.run_id, reason="Stopped.")
    await stack.resume(stack.run_id)
    stack.child_script(calls(tool_call("append", "c2", line="a")), answer("again"))
    with pytest.raises(NodeFailed):
        await stack.result("@0", "agent", _PROMPT, {"timeout": 30.0})

    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_a_stop_does_not_wait_forever_for_an_agent_that_does_not_end(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run ends after a bounded wait, and the node of a turn nobody could read is not one to run again."""
    monkeypatch.setattr(engine_module, "_STOP_SECONDS", 0.1)
    _hanging(stack)
    await stack.submit("@0", "agent", _PROMPT)
    job_id = stack.node_job("@0").job_id
    asked: list[str] = []

    async def ignored(job_id: str) -> None:
        asked.append(job_id)

    monkeypatch.setattr(stack.jobs, "request_cancel", ignored)

    ended = await asyncio.wait_for(stack.engine.cancel(stack.run_id, reason="Stopped."), 5)

    # Asked before the wait, and once more when its node is read while it still works.
    assert (ended.state, asked, _node(stack).state) == (
        WorkflowRunState.CANCELLED,
        [job_id] * 2,
        WorkflowNodeState.INDETERMINATE,
    )


@pytest.mark.asyncio
async def test_a_stop_waits_for_all_its_agents_together_and_not_under_the_runs_lock(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One deadline for every agent of the run, not one each; meanwhile the run's lock is free and no call gets in."""
    monkeypatch.setattr(engine_module, "_STOP_SECONDS", 0.6)
    stack.child_script(*([Await(asyncio.Event().wait), *answer("late")] for _ in range(4)))
    for index in range(4):
        await stack.submit(f"@{index}", "agent", {"prompt": f"Task {index}"})
    asked: list[str] = []

    async def ignored(job_id: str) -> None:
        asked.append(job_id)

    monkeypatch.setattr(stack.jobs, "request_cancel", ignored)
    lock, clock = stack.engine._condition(stack.run_id), asyncio.get_running_loop().time
    began = clock()

    stop = asyncio.create_task(stack.engine.cancel(stack.run_id, reason="Stopped."))
    # Every agent is asked before any is waited for.
    async with asyncio.timeout(0.5):
        while len(asked) < 4:  # noqa: ASYNC110 - the fake has no event.
            await asyncio.sleep(0.01)
    with pytest.raises(SupersededAttemptError):
        await asyncio.wait_for(stack.submit("@4", "agent", _PROMPT), 0.3)
    assert (lock.locked(), stop.done()) == (False, False)
    ended = await asyncio.wait_for(stop, 5)

    # Four waits one after the other take 2.4 seconds.
    assert clock() - began < 1.8
    states = {node.state for node in stack.store.nodes(stack.run_id)}
    assert (ended.state, states, len(_node_jobs(stack))) == (
        WorkflowRunState.CANCELLED,
        {WorkflowNodeState.INDETERMINATE},
        4,
    )


@pytest.mark.asyncio
async def test_a_run_that_ends_starts_no_agent_in_the_place_its_stopped_agent_leaves(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script failed while two agents worked and one waited for a place, and one agent ends before the other.

    The place the first gives up, while the end of the run still waits for the second, starts no third agent.
    """
    monkeypatch.setattr(engine_module, "_STOP_SECONDS", 0.5)
    await stack.start(max_concurrent=2)
    stack.child_script(*([Await(asyncio.Event().wait), *answer("late")] for _ in range(2)))
    for key in ("@0", "@1"):
        await stack.submit(key, "agent", {"prompt": f"Task {key}"})
    assert (await stack.submit("@2", "agent", {"prompt": "Never"})).state == "queued"
    slow, cancel, jobs = stack.node_job("@1").job_id, stack.jobs.request_cancel, len(_node_jobs(stack))

    async def all_but_one(job_id: str) -> None:
        if job_id != slow:
            await cancel(job_id)

    monkeypatch.setattr(stack.jobs, "request_cancel", all_but_one)

    await stack.finish_attempt(error="KeyError: 'x'")

    states = [node.state for node in stack.store.nodes(stack.run_id)]
    assert (states, len(_node_jobs(stack)) - jobs, stack.store.get_run(stack.run_id).state) == (
        [WorkflowNodeState.CANCELLED, WorkflowNodeState.INDETERMINATE, WorkflowNodeState.CANCELLED],
        0,
        WorkflowRunState.FAILED,
    )


@pytest.mark.asyncio
async def test_the_pass_that_ends_a_stopped_jobs_run_neither_cuts_that_end_short_nor_holds_up_the_next_run(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The maintenance pass has less time than the end of a run may take, so that end is the engine's own task.

    The reproducer of the second full review (Sol Major 2, Opus Minor 1), through the real wrapper: the run the
    pass comes to first has a stopped job and an agent that does not end, and the other run only waits for its
    next attempt.
    """
    monkeypatch.setattr(engine_module, "_STOP_SECONDS", 2.5)
    second = stack.run_id
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)
    first = (await stack.start()).run_id
    _hanging(stack)
    await stack.submit("@0", "agent", _PROMPT)
    set_run(stack.store, first, job_id="a-stopped-job")
    assert [run.run_id for run in stack.store.list_runs(include_finished=False)] == [first, second]

    async def ignored(_job_id: str) -> None:
        return None

    monkeypatch.setattr(stack.jobs, "request_cancel", ignored)
    standing = lambda _paths, job_id: "stopped" if job_id == "a-stopped-job" else "ended"  # noqa: E731
    monkeypatch.setattr(workflow_runtime, "workflow_job_standing", standing)
    script_runtime = SimpleNamespace(
        pass_timeout_seconds=2.0,
        runtime_paths=stack.context.runtime_paths,
        retention_seconds=3600,
    )
    lifecycle = WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime)  # type: ignore[arg-type]

    await asyncio.wait_for(lifecycle._maintain(), 10)

    # The pass gave up after its half of 2 seconds, and the second run has its attempt by then.
    assert [stack.store.get_run(run_id).attempt for run_id in (first, second)] == [1, 2]
    await stack.until(lambda: stack.store.get_run(first).state is WorkflowRunState.CANCELLED)
    stack.run_id = first
    assert _node(stack).state is WorkflowNodeState.INDETERMINATE


@pytest.mark.asyncio
async def test_agent_whose_job_wrote_before_its_handle_was_stamped_is_not_run_again_by_a_stop_that_cannot_read_it(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job handle without the mark that it was handed over proves nothing: the job starts before the mark is saved.

    The reproducer of the third verification (Astra B3): the job is admitted, writes and completes, and MindRoom
    dies before the node's handle says `dispatched`. A Stop while no job runtime can read the job then ends the node.
    """
    real_start = stack.jobs.start

    async def admitted(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
        held = await real_start(*args, **kwargs)
        await asyncio.Event().wait()
        return held

    monkeypatch.setattr(stack.jobs, "start", admitted)
    request = {"prompt": "Record one line."}
    stack.child_script(calls(tool_call("append", "one", line="once")), answer("recorded"))
    submitting = asyncio.create_task(stack.submit("@0", "agent", request))
    await stack.until(lambda: stack.counting_tool.lines == ["once"])
    await stack.until(lambda: stack.node_job("@0").status == "completed")
    assert _node(stack).handle.dispatched is False
    await stack.kill()
    await asyncio.gather(submitting, return_exceptions=True)
    monkeypatch.setattr(stack.engine, "agents", lambda: None)

    await stack.engine.cancel(stack.run_id, reason="Stopped while jobs were off.")

    assert _node(stack).state is WorkflowNodeState.INDETERMINATE
    monkeypatch.undo()
    await stack.resume(stack.run_id)
    stack.child_script(calls(tool_call("append", "two", line="once")), answer("recorded"))
    with pytest.raises(NodeFailed):
        await stack.result("@0", "agent", request)
    assert stack.counting_tool.lines == ["once"]


@pytest.mark.asyncio
async def test_a_stop_of_several_agents_says_of_each_what_its_own_turn_did(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agents end together, and then each node is read: only the one whose turn called a tool is not run again."""
    wrote, gate = asyncio.Event(), asyncio.Event()

    async def held() -> None:
        wrote.set()
        await gate.wait()

    waits = ([Await(gate.wait), *answer("late")] for _ in range(2))
    stack.child_script(calls(tool_call("append", "c1", line="a")), [Await(held), *answer("done")], *waits)
    await stack.submit("@0", "agent", _PROMPT)
    await asyncio.wait_for(wrote.wait(), 5)
    for key in ("@1", "@2"):
        await stack.submit(key, "agent", {"prompt": f"Task {key}"})
    lock, cancel, locked = stack.engine._condition(stack.run_id), stack.jobs.request_cancel, []

    async def recorded(job_id: str) -> None:
        locked.append(lock.locked())
        await cancel(job_id)

    monkeypatch.setattr(stack.jobs, "request_cancel", recorded)

    await stack.engine.cancel(stack.run_id, reason="Stopped.")

    states = {node.key: node.state for node in stack.store.nodes(stack.run_id)}
    assert (states, locked, {job.status for job in _node_jobs(stack)}) == (
        {
            "@0": WorkflowNodeState.INDETERMINATE,
            "@1": WorkflowNodeState.CANCELLED,
            "@2": WorkflowNodeState.CANCELLED,
        },
        [False] * 3,
        {"cancelled"},
    )


@pytest.mark.asyncio
async def test_a_call_that_replaces_a_working_agent_that_made_a_call_is_not_run_unasked(stack: WorkflowStack) -> None:
    """An edit changed the call of an agent that still works: its job is stopped, and what it did decides the rest."""
    gate = asyncio.Event()
    stack.child_script(calls(tool_call("append", "c1", line="a")), [Await(gate.wait), *answer("done")])
    await stack.submit("@0", "agent", _PROMPT)
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    await _replayed(stack)

    stack.child_script(calls(tool_call("append", "c2", line="a")), answer("edited"))
    with pytest.raises(ScriptRuntimeUnavailableError):
        await stack.submit("@0", "agent", {"prompt": "An edited prompt."})
    await wait_for_status(stack.jobs, stack.node_job("@0").job_id, "cancelled")
    with pytest.raises(NodeFailed) as refused:
        await stack.result("@0", "agent", {"prompt": "An edited prompt."})

    assert (refused.value.kind, stack.counting_tool.lines, _node(stack).generation) == ("indeterminate", ["a"], 1)


@pytest.mark.asyncio
async def test_cancelling_one_node_job_fails_only_that_node(stack: WorkflowStack) -> None:
    """Stop on one agent ends that agent: its call raises `cancelled`, and the run goes on."""
    _hanging(stack)
    receipt = await stack.submit("@0", "agent", _PROMPT)
    await stack.jobs.cancel(stack.node_job("@0").job_id, owner=stack.context.execution_identity, depth=0)

    while receipt.state == "running":
        receipt = await stack.engine.node_receipt(stack.attempt, receipt.id, wait=5)

    assert (receipt.state, receipt.error and receipt.error["kind"]) == ("cancelled", "cancelled")
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING
    stack.child_script(answer("next"))
    assert await stack.result("@1", "agent", {"prompt": "Go on"}) == "next"


@pytest.mark.asyncio
async def test_timeout_cancels_the_child_and_waits_for_it_to_end(stack: WorkflowStack) -> None:
    """After its seconds of work the turn is stopped; the node fails as `timeout` once the job has ended."""
    _hanging(stack)

    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", _PROMPT, {"timeout": 0.05})

    assert (failed.value.kind, stack.node_job("@0").status) == ("timeout", "cancelled")


@pytest.mark.asyncio
@pytest.mark.parametrize(("work", "kind"), [(0.2, "completed"), (2.6, "indeterminate")])
async def test_timeout_counts_the_work_before_and_after_an_approval_and_not_the_wait(
    gated: WorkflowStack,
    work: float,
    kind: str,
) -> None:
    """The seconds are the turn's working time as a whole: a card does not hand out the full time again.

    The turn that runs out of time had made its approved call by then, so its node is not one to run again.
    """
    cards: JobApprovalCards = gated.cards  # type: ignore[attr-defined]
    busy = Await(partial(asyncio.sleep, work))
    gated.child_script([busy, *calls(tool_call("append", "c1", line="a"))], [busy, *answer("sent")])
    # Each stretch of work fits in the four seconds with room for the turn's own start; two long ones do not.
    receipt = await gated.submit("@0", "agent", _PROMPT, {"timeout": 4.0})
    await asyncio.wait_for(cards.posted.wait(), 10)
    await asyncio.sleep(4.5)
    # The node keeps what the turn has left while it waits, for an attempt that finds it waiting.
    handle = _node(gated).handle
    assert handle is not None
    assert (handle.work_until, 0 < (handle.work_left or 0) < 4.0 - work) == (None, True)
    cards.decision.set_result(BackgroundApprovalDecision("approved", None))

    while receipt.state in ("running", "waiting"):
        receipt = await gated.engine.node_receipt(gated.attempt, receipt.id, wait=10)

    assert (receipt.error["kind"] if receipt.error else receipt.state) == kind


@pytest.mark.asyncio
async def test_approval_wait_does_not_exhaust_schema_correction_working_time(gated: WorkflowStack) -> None:
    """Approval wait time on turn 1 is not deducted from the schema correction turn's working time (B2)."""
    cards: JobApprovalCards = gated.cards  # type: ignore[attr-defined]
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    gated.child_script(
        calls(tool_call("append", "c1", line="a")),
        answer("not json"),
        [Await(partial(asyncio.sleep, 0.2)), *answer('```json\n{"ok": true}\n```')],
    )
    receipt = await gated.submit("@0", "agent", _PROMPT | {"schema": schema}, {"timeout": 2.0})
    await asyncio.wait_for(cards.posted.wait(), 10)
    await asyncio.sleep(2.3)
    cards.decision.set_result(BackgroundApprovalDecision("approved", None))

    while receipt.state in ("running", "waiting"):
        receipt = await gated.engine.node_receipt(gated.attempt, receipt.id, wait=10)

    assert (receipt.state, receipt.result) == ("completed", {"ok": True})


@pytest.mark.asyncio
async def test_timeout_is_not_given_again_to_the_attempt_that_finds_the_agent_working(stack: WorkflowStack) -> None:
    """The next attempt waits for the same turn with the time that turn has left, not with all of it."""
    _hanging(stack)
    started = asyncio.get_running_loop().time()
    await stack.submit("@0", "agent", _PROMPT, {"timeout": 3.0})
    await asyncio.sleep(2.0)
    await _replayed(stack)

    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", _PROMPT, {"timeout": 3.0})

    assert (failed.value.kind, asyncio.get_running_loop().time() - started < 4.5) == ("timeout", True)


@pytest.mark.asyncio
async def test_timeout_stops_the_agent_also_while_no_attempt_waits_for_it(stack: WorkflowStack) -> None:
    """The working time is the turn's, not an attempt's: the turn is stopped on time between two attempts too.

    The attempt that comes then finds a call that was stopped, and runs it again, as after any timeout.
    """
    _hanging(stack)
    await stack.submit("@0", "agent", _PROMPT, {"timeout": 1.0})
    job_id = stack.node_job("@0").job_id

    await _replayed(stack)

    await asyncio.wait_for(wait_for_status(stack.jobs, job_id, "cancelled"), 5)
    stack.child_script(answer("second try"))
    answers: list[object] = []
    while not answers:
        # Asked again, as the script's SDK does, while the wait that stopped the turn gives its outcome back.
        with suppress(ScriptRuntimeUnavailableError):
            answers.append(await stack.result("@0", "agent", _PROMPT, {"timeout": 1.0}))
        await asyncio.sleep(0.05)
    assert (answers, _node(stack).generation) == (["second try"], 2)


@pytest.mark.asyncio
async def test_run_is_waiting_again_for_the_attempt_that_finds_its_timed_agent_waiting_for_a_person(
    gated: WorkflowStack,
) -> None:
    """The next attempt starts as running; the agent it attaches to still waits for the same card, and so does the run."""
    cards: JobApprovalCards = gated.cards  # type: ignore[attr-defined]
    gated.child_script(calls(tool_call("append", "c1", line="a")), answer("sent"))
    await gated.submit("@0", "agent", _PROMPT, {"timeout": 30.0})
    await gated.until(lambda: gated.store.get_run(gated.run_id).state is WorkflowRunState.WAITING)
    await _replayed(gated)
    assert gated.store.get_run(gated.run_id).state is WorkflowRunState.RUNNING

    await gated.submit("@0", "agent", _PROMPT, {"timeout": 30.0})

    assert (gated.store.get_run(gated.run_id).state, _node(gated).state) == (
        WorkflowRunState.WAITING,
        WorkflowNodeState.WAITING,
    )
    cards.decision.set_result(BackgroundApprovalDecision("approved", None))
    assert await gated.result("@0", "agent", _PROMPT, {"timeout": 30.0}) == "sent"


@pytest.mark.asyncio
async def test_node_jobs_are_in_no_list_no_room_state_and_no_follow_up(stack: WorkflowStack) -> None:
    """The run reports its agents: their jobs are no jobs of the owner's conversation, live or ended, read or not."""
    owner = stack.context.execution_identity
    key = ConversationKey(owner.recipient, str(owner.room_id), owner.resolved_thread_id, str(owner.requester_id), False)
    gate = _hanging(stack, "unread")
    await stack.submit("@0", "agent", _PROMPT)
    live = stack.jobs.retained_jobs()

    assert [job.status for job in live] == ["running"]
    assert _jobs_state_content(live) == {"jobs": [], "total": 0}
    assert await stack.jobs.list_jobs(owner=owner, depth=0) == []
    assert conversation_jobs(stack.jobs, room_id=str(owner.room_id), thread_id=None) == []
    # The engine stops waiting, as a primary that dies does, and the job ends with nobody to acknowledge it.
    await stack.engine.close()
    gate.set()
    await wait_for_status(stack.jobs, stack.node_job("@0").job_id, "completed")
    ended = stack.jobs.retained_jobs()

    assert [(job.status, job.consumed) for job in ended] == [("completed", False)]
    assert _follow_up_candidates(ended) == {}
    assert await stack.jobs.ended_jobs(key) == ()
    assert await stack.jobs.list_jobs(owner=owner, depth=0) == []


@pytest.mark.asyncio
async def test_job_of_a_call_is_deleted_after_its_retention_once_the_call_has_its_outcome(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node's job is kept while its call still waits for it, and goes the way of every old job after that."""
    monkeypatch.setattr(tool_job_runtime, "_ENDED_JOB_RETENTION", timedelta(0))
    gate = _hanging(stack, "kept until read")
    await stack.submit("@0", "agent", _PROMPT)
    job_id = stack.node_job("@0").job_id
    # The attempt ends, and the job after it: no attempt has read its answer into the node yet.
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)
    await stack.engine.settle(stack.store.get_run(stack.run_id), replay=True)
    gate.set()
    await wait_for_status(stack.jobs, job_id, "completed")

    await stack.coordinator._expire_ended_jobs()
    assert [job.job_id for job in _node_jobs(stack)] == [job_id]
    assert await stack.maintain() == [stack.run_id]
    assert (await stack.submit("@0", "agent", _PROMPT)).result == "kept until read"
    await stack.until(lambda: stack.node_job("@0").consumed)
    await stack.coordinator._expire_ended_jobs()

    assert _node_jobs(stack) == []


@pytest.mark.asyncio
async def test_large_answer_is_saved_whole_before_the_job_is_acknowledged(stack: WorkflowStack) -> None:
    """The node holds the whole answer, far beyond what a job keeps of its result."""
    text = "{" + ", ".join(f'"key{index}": "{"v" * 90}"' for index in range(2000)) + "}"
    stack.child_script(answer(text, chunk=50_000))

    assert await stack.result("@0", "agent", _PROMPT) == text

    await stack.until(lambda: stack.node_job("@0").consumed)
    assert len(text) > 200_000


@pytest.mark.asyncio
async def test_the_job_snapshot_holds_the_label_not_the_prompt(stack: WorkflowStack) -> None:
    """The child's task, which every snapshot of its job repeats, is the node's label; the model is sent the prompt."""
    prompt = "Summarize this. " + "word " * 40_000
    model = stack.child_script(answer("short"))

    assert await stack.result("@0", "agent", {"prompt": prompt}) == "short"

    child = delegation_child(stack.node_job("@0"))
    # Cut by the redactor, which says so in the label's last characters.
    assert (child.task, len(child.task)) == (f"{prompt[:65]}... [truncated]", 80)
    sent = [str(message.content) for message in model.requests[0].messages if message.role == "user"]
    assert prompt in sent[-1]


@pytest.mark.asyncio
async def test_label_of_a_call_without_a_name_is_the_first_line_of_its_prompt_without_its_secrets(
    stack: WorkflowStack,
) -> None:
    """The label goes where the prompt does not: into the job, the room and the run's view. It is one redacted line."""
    stack.child_script(answer("done"))

    await stack.result("@0", "agent", {"prompt": "\nDeploy with password=placeholder-not-a-secret now.\nThen report."})

    label = "Deploy with password=***redacted***"
    assert (_node(stack).label, delegation_child(stack.node_job("@0")).task) == (label, label)
    # A credential that the label's end would cut in two is masked whole, in a label the script gave as well.
    url = "https://user:placeholder-pass"
    cut = f"Fetch {'x' * (74 - len(url))}{url}@host.example/path"
    assert cut.index("@") == 80
    stack.child_script(answer("done"), answer("done"))
    await stack.result("@1", "agent", {"prompt": cut})
    await stack.result("@2", "agent", {"prompt": "Go"}, {"label": cut})
    assert [
        ("placeholder-pass" in node.label, len(node.label) <= 80) for node in (_node(stack, "@1"), _node(stack, "@2"))
    ] == [
        (False, True),
        (False, True),
    ]


@pytest.mark.asyncio
async def test_plain_copy_inherits_the_owners_tools(stack: WorkflowStack) -> None:
    """A copy of the owner is offered what the owner is: its own tools and the workflow toolkit, one level deeper."""
    model = stack.child_script(answer("done"))

    await stack.result("@0", "agent", _PROMPT)

    assert {"append", "run_shell_command", "start_workflow", "resume_workflow"} <= set(offered_tools(model)[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(("tools", "offered"), [([], []), (["counter"], ["append"])])
async def test_narrowed_copy_is_offered_the_tools_its_call_lists_and_no_other(
    stack: WorkflowStack,
    tools: list[str],
    offered: list[str],
) -> None:
    """`tools=[...]` with `system=` narrows the copy, and an empty list leaves it no tool at all."""
    model = stack.child_script(answer("done"))

    await stack.result("@0", "agent", _PROMPT | {"system": "Only summarize.", "tools": tools})

    persona = delegation_child(stack.node_job("@0")).persona
    assert persona is not None
    assert (list(persona.tools or ()), persona.tools is not None) == (tools, True)
    assert [name for name in offered_tools(model)[0] if name in ("append", "run_shell_command", "start_workflow")] == (
        offered
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "refusal"),
    [
        ({"model": "opus5"}, "Cannot delegate: Unknown model 'opus5'. Available models: default."),
        ({"agent": "stranger"}, "Cannot delegate to 'stranger': it is no longer an allowed target."),
        ({"agent": "nobody"}, "Cannot delegate to 'nobody': that agent is not allowed to reply to you."),
        ({"tools": ["shell"]}, "tools, skills and knowledge narrow a copy only together with system=."),
        ({"tools": []}, "tools, skills and knowledge narrow a copy only together with system=."),
        ({"knowledge": []}, "tools, skills and knowledge narrow a copy only together with system=."),
        ({"dedicated_worker": True}, "Cannot delegate: watcher may not run subagents in dedicated workers."),
    ],
)
async def test_turn_that_delegation_refuses_is_refused_and_starts_no_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, object],
    refusal: str,
) -> None:
    """The rules of `run_subagent` decide, with their own sentences, as kind `refused`; the options reach them."""
    with workflow_stack(tmp_path, extra_tools=("workflow",), other_agents=("stranger",)) as stack:
        # Its bot is up and knows its rooms, and the run's room is not one of them.
        stack.bot_rooms["stranger"] = ("!elsewhere:example.test",)
        stack._open()
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()

        with pytest.raises(NodeFailed, match=refusal.replace("(", r"\(")) as refused:
            await stack.result("@0", "agent", _PROMPT | options)

        assert (refused.value.kind, _node_jobs(stack), _node(stack).state) == ("refused", [], WorkflowNodeState.FAILED)
        again = await stack.submit("@0", "agent", _PROMPT | options)
        assert (again.state, _node(stack).generation) == ("failed", 1)
        await stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["helper", "roommate"])
async def test_another_agent_runs_as_configured_when_the_run_may_run_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    """`agent=` runs an agent the owner may delegate to, or one whose bot is in the run's room, with its own tools."""
    names = ("helper", "roommate")
    with workflow_stack(tmp_path, extra_tools=("workflow",), other_agents=names, delegate_to=("helper",)) as stack:
        stack.bot_rooms = {"roommate": (stack.context.room_id,)}
        stack._open()
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()
        model = stack.child_script(answer("from the other agent"), agent=target)

        assert await stack.result("@0", "agent", _PROMPT | {"agent": target}) == "from the other agent"

        child = delegation_child(stack.node_job("@0"))
        assert (child.child_agent_name, child.caller_agent_name, stack.node_job("@0").tool_name) == (
            target,
            "watcher",
            target,
        )
        assert "start_workflow" not in offered_tools(model)[0]
        await stack.close()


@pytest.mark.asyncio
async def test_room_agent_whose_bot_has_not_synced_is_asked_for_again_and_runs_once_it_has(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a restart the owner's bot may be ready before the target's: the call is not ready, and saves nothing."""
    with workflow_stack(tmp_path, extra_tools=("workflow",), other_agents=("roommate",)) as stack:
        stack.bot_rooms = {"roommate": (stack.context.room_id,)}
        stack.unsynced = {"roommate"}
        stack._open()
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()
        stack.child_script(answer("here now"), agent="roommate")

        with pytest.raises(ScriptRuntimeUnavailableError, match="'roommate'"):
            await stack.submit("@0", "agent", _PROMPT | {"agent": "roommate"})
        assert (stack.store.get_node(stack.run_id, "@0"), _node_jobs(stack)) == (None, [])
        stack.context.orchestrator.agent_bots["roommate"].first_sync_complete = True  # type: ignore[union-attr]

        assert await stack.result("@0", "agent", _PROMPT | {"agent": "roommate"}) == "here now"
        await stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", ["allowed", "denied"])
async def test_answer_of_a_job_that_cannot_be_read_yet_is_not_taken(stack: WorkflowStack, resolved: JobGrant) -> None:
    """While the requester's access to the job is unresolved, its ended turn is no result: the node waits for the answer."""
    gate = _hanging(stack, "unread")
    await stack.submit("@0", "agent", _PROMPT)
    stack.admission = "pending"
    gate.set()
    await wait_for_status(stack.jobs, stack.node_job("@0").job_id, "completed")
    await asyncio.sleep(0.3)
    assert (_node(stack).state, _node(stack).result) == (WorkflowNodeState.RUNNING, None)
    stack.admission = resolved

    if resolved == "allowed":
        assert await stack.result("@0", "agent", _PROMPT) == "unread"
    else:
        with pytest.raises(NodeFailed) as refused:
            await stack.result("@0", "agent", _PROMPT)
        # The turn ended, whatever it did: a read that is denied does not say that it did nothing.
        assert (refused.value.kind, _node(stack).result) == ("indeterminate", None)


@pytest.mark.asyncio
async def test_agent_whose_handle_never_got_its_mark_is_not_run_again_when_its_answer_may_not_be_read(
    stack: WorkflowStack,
) -> None:
    """A denied read of a job the runtime holds proves nothing, with or without the mark that it was handed over.

    The primary died right after the job was admitted, so the saved handle has no mark, and the job wrote.
    """
    gate = asyncio.Event()
    stack.child_script(calls(tool_call("append", "c1", line="a")), [Await(gate.wait), *answer("done")])
    await stack.submit("@0", "agent", _PROMPT)
    stack.admission = "pending"
    gate.set()
    await wait_for_status(stack.jobs, stack.node_job("@0").job_id, "completed")
    node, epoch = _node(stack), stack.store.get_run(stack.run_id).epoch
    unstamped = replace(node.handle, dispatched=False)
    assert stack.store.update_node(stack.run_id, "@0", node.generation, epoch, handle=unstamped)
    await stack.restart(kill=True)
    stack.admission = "denied"
    assert await stack.recover() == [stack.run_id]
    stack.child_script(calls(tool_call("append", "c2", line="a")), answer("again"))

    with pytest.raises(NodeFailed) as blocked:
        await stack.result("@0", "agent", _PROMPT)

    assert (blocked.value.kind, _node(stack).handle.dispatched, stack.counting_tool.lines) == (
        "indeterminate",
        False,
        ["a"],
    )
    stack.admission = "allowed"
    await _replayed(stack)
    with pytest.raises(NodeFailed) as still:
        await stack.result("@0", "agent", _PROMPT)
    assert (still.value.kind, _node(stack).generation, stack.counting_tool.lines) == ("indeterminate", 1, ["a"])


@pytest.mark.asyncio
async def test_agent_whose_answer_may_not_be_read_is_not_run_again_once_it_may(stack: WorkflowStack) -> None:
    """A denied read of an ended turn proves no failure: the call waits for a resume that names it, and writes once."""
    gate = asyncio.Event()
    stack.child_script(calls(tool_call("append", "c1", line="a")), [Await(gate.wait), *answer("done")])
    await stack.submit("@0", "agent", _PROMPT)
    stack.admission = "pending"
    gate.set()
    await wait_for_status(stack.jobs, stack.node_job("@0").job_id, "completed")
    stack.admission = "denied"
    with pytest.raises(NodeFailed):
        await stack.result("@0", "agent", _PROMPT)
    stack.admission = "allowed"
    stack.child_script(calls(tool_call("append", "c2", line="a")), answer("again"))

    await _replayed(stack)

    with pytest.raises(NodeFailed) as blocked:
        await stack.result("@0", "agent", _PROMPT)
    assert (blocked.value.kind, _node(stack).generation, stack.counting_tool.lines) == ("indeterminate", 1, ["a"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "args", "result"),
    [("agent", _PROMPT, "done"), ("tool", tool_args("counter", "append", line="a"), "appended")],
)
async def test_call_is_waited_for_also_when_the_look_at_its_run_failed_as_it_started(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    args: dict[str, object],
    result: str,
) -> None:
    """The call is the engine's from its insert: what fails after that fails the request, and the call still ends."""
    stack.child_script(answer("done"))
    states, failed = stack.store.node_states, []

    def fails_once(run_id: str) -> object:
        if not failed:
            failed.append(run_id)
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)
        return states(run_id)

    monkeypatch.setattr(stack.store, "node_states", fails_once)

    with pytest.raises(sqlite3.OperationalError):
        await stack.submit("@0", kind, args)

    assert await asyncio.wait_for(stack.result("@0", kind, args), 10) == result


@pytest.mark.asyncio
async def test_engine_that_closes_while_an_agent_starts_begins_no_wait_for_it(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed engine starts no task: the job is the next start's to continue, not settled from how the stop left it."""
    start, at_start = stack.jobs.start, asyncio.Event()

    async def started_then_held(job_id: str, **options: Any) -> None:  # noqa: ANN401 - the runtime's own options.
        _, claim = await start(job_id, **options)
        await stack.jobs.release_wait(job_id, claim)
        at_start.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(stack.jobs, "start", started_then_held)
    _hanging(stack)
    request = asyncio.create_task(stack.submit("@0", "agent", _PROMPT))
    await at_start.wait()

    await stack.engine.close()

    with pytest.raises(asyncio.CancelledError):
        await request
    assert not any(stack.engine._tasks.values())
    assert (_node(stack).state, stack.node_job("@0").status) == (WorkflowNodeState.RUNNING, "running")


async def _ended_between_attempts(stack: WorkflowStack) -> None:
    """Let the call's turn end while no attempt waits for it, have its owner collect the job, and unsettle the access."""
    gate = _hanging(stack)
    await stack.submit("@0", "agent", _PROMPT)
    await _replayed(stack)
    gate.set()
    job_id, owner = stack.node_job("@0").job_id, stack.context.execution_identity
    await wait_for_status(stack.jobs, job_id, "completed")
    collected = await stack.jobs.wait(job_id, owner=owner, depth=0)
    await stack.jobs.acknowledge_wait(job_id, collected.claim)
    assert stack.node_job("@0").consumed
    stack.admission = "pending"


@pytest.mark.asyncio
async def test_answer_that_was_collected_before_is_read_only_with_the_access_of_now(stack: WorkflowStack) -> None:
    """That somebody read the job once grants nothing: the replay is asked to come back until the access is resolved."""
    await _ended_between_attempts(stack)

    with pytest.raises(ScriptRuntimeUnavailableError):
        await stack.result("@0", "agent", _PROMPT)

    assert (_node(stack).state, _node(stack).result) == (WorkflowNodeState.RUNNING, None)
    stack.admission = "allowed"
    assert await stack.result("@0", "agent", _PROMPT) == "late"


@pytest.mark.asyncio
async def test_stop_of_a_run_whose_ended_agent_cannot_be_read_ends_it_without_the_answer(stack: WorkflowStack) -> None:
    """A Stop waits for nobody's access: the call it cannot read may have run, and the run ends at once."""
    await _ended_between_attempts(stack)
    with pytest.raises(ScriptRuntimeUnavailableError):
        await stack.result("@0", "agent", _PROMPT)

    await asyncio.wait_for(stack.engine.cancel(stack.run_id, reason="Stopped."), 5)

    node = _node(stack)
    assert (node.state, node.result) == (WorkflowNodeState.INDETERMINATE, None)
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.CANCELLED


@pytest.mark.asyncio
async def test_agents_of_a_run_that_a_subagent_started_are_one_level_below_it(stack: WorkflowStack) -> None:
    """The run keeps the depth of the agent that started it, so the limit on nested subagents counts its agents too."""
    stack.child_script(answer("deep"))
    await stack.engine.launch(stack.reserve(depth=1).run_id, stack.context)
    assert await stack.result("@0", "agent", _PROMPT) == "deep"
    job = stack.node_job("@0")
    assert (job.depth, delegation_child(job).depth) == (1, 2)

    await stack.engine.launch(stack.reserve(depth=3).run_id, stack.context)
    with pytest.raises(NodeFailed, match="the maximum delegation depth was reached") as refused:
        await stack.result("@0", "agent", _PROMPT)
    assert refused.value.kind == "refused"


@pytest.mark.asyncio
async def test_name_that_is_an_agent_logs_that_it_only_labels(stack: WorkflowStack) -> None:
    """`name="watcher"` runs a copy of the owner under that label, and the run says how to pick an agent."""
    stack.child_script(answer("a"), answer("b"))

    await stack.result("@0", "agent", _PROMPT | {"name": "watcher"}, {"name": "watcher"})
    await stack.result("@1", "agent", _PROMPT | {"name": "notes"}, {"name": "notes"})

    assert stack.store.log(stack.run_id) == [
        'name="watcher" only labels the node. Use agent="watcher" to run that agent.',
    ]
    assert (_node(stack).label, _node(stack, "@1").label) == ("watcher", "notes")


@pytest.mark.asyncio
async def test_agent_without_a_job_runtime_is_refused(tmp_path: Path) -> None:
    """Where background tool jobs are off, a run cannot run agents, and its script is told why."""
    with workflow_stack(tmp_path) as stack:
        await stack.start()

        with pytest.raises(NodeFailed, match=r"background_tool_jobs\.enabled") as refused:
            await stack.result("@0", "agent", _PROMPT)

        assert refused.value.kind == "refused"
        await stack.engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("tools", "kind"), [(False, "interrupted"), (True, "indeterminate")])
async def test_child_stopped_by_a_config_reload_is_not_saved_as_a_result(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    tools: bool,
    kind: str,
) -> None:
    """A turn that a reload stopped ends with a notice, which is no answer: the call is safe to run again only if it called no tool."""
    monkeypatch.setattr(agent_nodes, "reload_stopped_child_result", lambda _child, _record: "Stopped for a reload.")
    turns = [calls(tool_call("append", "c1", line="a")), answer("notice")] if tools else [answer("notice")]
    stack.child_script(*turns)

    with pytest.raises(NodeFailed) as stopped:
        await stack.result("@0", "agent", _PROMPT)

    states = {"interrupted": WorkflowNodeState.FAILED, "indeterminate": WorkflowNodeState.INDETERMINATE}
    assert (stopped.value.kind, _node(stack).state, _node(stack).result) == (kind, states[kind], None)
    if not tools:
        # What the script reads says what a script can do, not what a caller in a chat could.
        assert str(stopped.value) == "The agent was stopped before it started a tool call. A resume runs it again."
    assert stack.counting_tool.lines == (["a"] if tools else [])


@pytest.mark.asyncio
async def test_child_that_needs_approval_waits_and_resumes(gated: WorkflowStack) -> None:
    """While the agent's call waits for a person its node and its run are waiting; the approved call runs once."""
    cards: JobApprovalCards = gated.cards  # type: ignore[attr-defined]
    gated.child_script(calls(tool_call("append", "c1", line="a")), answer("sent"))
    receipt = await gated.submit("@0", "agent", _PROMPT)
    await asyncio.wait_for(cards.posted.wait(), 10)
    await gated.until(lambda: gated.store.get_run(gated.run_id).state is WorkflowRunState.WAITING)

    node = _node(gated)
    assert (node.state, node.waiting_on, gated.counting_tool.lines) == (WorkflowNodeState.WAITING, "approval", [])
    waiting = await gated.engine.node_receipt(gated.attempt, receipt.id, wait=0)
    assert waiting.state == "waiting"
    cards.decision.set_result(BackgroundApprovalDecision("approved", None))

    assert await gated.result("@0", "agent", _PROMPT) == "sent"
    assert (gated.counting_tool.lines, _node(gated).waiting_on) == (["a"], None)
    assert gated.store.get_run(gated.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_run_whose_agent_waits_for_a_person_is_running_while_a_tool_of_it_works(
    gated: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run waits only while nothing of it works: a tool that starts beside the waiting agent makes it running."""
    cards: JobApprovalCards = gated.cards  # type: ignore[attr-defined]
    gated.child_script(calls(tool_call("append", "c1", line="a")), answer("sent"))
    await gated.submit("@0", "agent", _PROMPT)
    await gated.until(lambda: gated.store.get_run(gated.run_id).state is WorkflowRunState.WAITING)
    release, broker = asyncio.Event(), type(gated.engine.broker)
    execute = broker.execute_for_workflow

    async def held(*args: Any, **options: Any) -> object:  # noqa: ANN401
        await release.wait()
        return await execute(*args, **options)

    monkeypatch.setattr(broker, "execute_for_workflow", held)

    # A call that asks nobody, so nothing but its start says that the run works again.
    await gated.submit("@1", "tool", tool_args("counter", "count"))

    assert gated.store.get_run(gated.run_id).state is WorkflowRunState.RUNNING
    release.set()
    await gated.until(lambda: gated.store.get_run(gated.run_id).state is WorkflowRunState.WAITING)
    cards.decision.set_result(BackgroundApprovalDecision("approved", None))
    assert await gated.result("@0", "agent", _PROMPT) == "sent"


@pytest.mark.asyncio
async def test_denied_card_denies_the_call_and_the_child_goes_on(gated: WorkflowStack) -> None:
    """A denial is the agent's to deal with, as for every subagent: the call does not run, and the turn ends with its answer."""
    cards: JobApprovalCards = gated.cards  # type: ignore[attr-defined]
    gated.child_script(calls(tool_call("append", "c1", line="a")), answer("not sent"))
    cards.decision.set_result(BackgroundApprovalDecision("denied", "Not now"))

    assert await gated.result("@0", "agent", _PROMPT) == "not sent"

    assert (gated.counting_tool.lines, _node(gated).state) == ([], WorkflowNodeState.COMPLETED)


@pytest.mark.asyncio
async def test_failed_agent_turn_does_not_write_output_path(stack: WorkflowStack) -> None:
    """A failed workflow agent turn does not write its output_path redirect; only a completed turn does."""

    async def fail() -> None:
        msg = "synthetic model error"
        raise RuntimeError(msg)

    stack.child_script([Await(fail)], answer("final report"))
    req: dict[str, object] = {"prompt": "Report.", "output_path": "trial-review.txt"}

    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", req)

    assert failed.value.kind == "failed"
    assert list(stack.tmp_path.rglob("trial-review.txt")) == []
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)

    assert await stack.result("@0", "agent", req) == "final report"
    written = list(stack.tmp_path.rglob("trial-review.txt"))
    assert len(written) == 1
    assert "final report" in written[0].read_text()
