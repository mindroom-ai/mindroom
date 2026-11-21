"""Tests for agent nodes: one `agent()` call of a workflow script as one delegation job.

Level: engine harness (`WorkflowStack`) with the real job runtime and real delegation; each agent's model is scripted.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.approval import manager as approval_manager
from mindroom.delegation.background import delegation_child
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.llm.errors import ProviderError
from mindroom.orchestration.tool_job_runtime import _follow_up_candidates
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.models import ScriptRunState
from mindroom.script_runs.reasons import PLUGIN_TOOLS_CHANGED
from mindroom.tool_jobs import runtime as tool_job_runtime
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
from tests.workflow_helpers import NodeFailed, WorkflowStack, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_jobs.runtime import BackgroundJob, JobGrant
    from mindroom.workflow_runs.models import WorkflowNode

_PROMPT: dict[str, object] = {"prompt": "Say hi"}


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with the real job runtime, scripted agents and one started run; no tool asks for approval."""
    monkeypatch.setattr(engine_module, "_RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "_RECHECK_SECONDS", 0.05)
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
    assert (job.adapter["workflow"], job.adapter["node"], job.adapter["generation"]) == (stack.run_id, "@0", 1)
    assert (child.workflow_run_id, child.workflow_node_key, child.workflow_generation) == (stack.run_id, "@0", 1)
    assert (child.task, child.depth, child.child_agent_name) == ("Say hi", 1, "watcher")
    assert node.links == {"job_id": job.job_id, "delegation_id": child.delegation_id, "subagent_id": child.subagent_id}
    totals = stack.store.usage_totals(stack.run_id)
    assert (totals["input_tokens"], totals["output_tokens"]) == (11, 3)


@pytest_asyncio.fixture
async def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The same harness, whose operator asks for approval before `append` runs; `cards` holds the open cards."""
    monkeypatch.setattr(engine_module, "_RECHECK_SECONDS", 0.05)
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
async def test_job_that_was_started_and_is_gone_leaves_its_call_indeterminate(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that started and that the job runtime no longer holds may have done anything: it is not run again."""
    _hanging(stack)
    await stack.submit("@0", "agent", _PROMPT)
    assert _node(stack).handle == DispatchHandle("job", stack.node_job("@0").job_id, dispatched=True)
    await _replayed(stack)
    monkeypatch.setattr(stack.jobs, "retained_jobs", lambda: ())

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

    assert await stack.result("@0", "agent", {"prompt": "new"}) == "new answer"
    gate.set()
    await wait_for_status(stack.jobs, old, "cancelled")

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
@pytest.mark.parametrize(("work", "kind"), [(0.2, "completed"), (2.6, "timeout")])
async def test_timeout_counts_the_work_before_and_after_an_approval_and_not_the_wait(
    gated: WorkflowStack,
    work: float,
    kind: str,
) -> None:
    """The seconds are the turn's working time as a whole: a card does not hand out the full time again."""
    cards: JobApprovalCards = gated.cards  # type: ignore[attr-defined]
    busy = Await(partial(asyncio.sleep, work))
    gated.child_script([busy, *calls(tool_call("append", "c1", line="a"))], [busy, *answer("sent")])
    # Each stretch of work fits in the four seconds with room for the turn's own start; two long ones do not.
    receipt = await gated.submit("@0", "agent", _PROMPT, {"timeout": 4.0})
    await asyncio.wait_for(cards.posted.wait(), 10)
    await asyncio.sleep(4.5)
    cards.decision.set_result(BackgroundApprovalDecision("approved", None))

    while receipt.state in ("running", "waiting"):
        receipt = await gated.engine.node_receipt(gated.attempt, receipt.id, wait=10)

    assert (receipt.error["kind"] if receipt.error else receipt.state) == kind


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
    prompt = "Summarize this. " + "x" * 200_000
    model = stack.child_script(answer("short"))

    assert await stack.result("@0", "agent", {"prompt": prompt}) == "short"

    child = delegation_child(stack.node_job("@0"))
    assert (child.task, len(child.task)) == (prompt[:80], 80)
    sent = [str(message.content) for message in model.requests[0].messages if message.role == "user"]
    assert prompt in sent[-1]


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
        assert (refused.value.kind, _node(stack).result) == ("denied", None)


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
async def test_denied_card_denies_the_call_and_the_child_goes_on(gated: WorkflowStack) -> None:
    """A denial is the agent's to deal with, as for every subagent: the call does not run, and the turn ends with its answer."""
    cards: JobApprovalCards = gated.cards  # type: ignore[attr-defined]
    gated.child_script(calls(tool_call("append", "c1", line="a")), answer("not sent"))
    cards.decision.set_result(BackgroundApprovalDecision("denied", "Not now"))

    assert await gated.result("@0", "agent", _PROMPT) == "not sent"

    assert (gated.counting_tool.lines, _node(gated).state) == ([], WorkflowNodeState.COMPLETED)
