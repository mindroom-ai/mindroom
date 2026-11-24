"""A run's cost limit: stopping at the limit, resuming with a higher limit, and the large-run notice."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.config.models import ModelPricing
from mindroom.matrix.event_types import WORKFLOW_QUESTION_CONTENT_KEY, WORKFLOW_RUN_CONTENT_KEY
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.models import ScriptRunState
from mindroom.script_runs.reasons import PLUGIN_TOOLS_CHANGED
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.limits import WorkflowLimits, run_usage
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.fakes.scripted_model import Await, answer, usage
from tests.workflow_helpers import CallFailed, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.usage import MessageMetrics

_LIMIT = {"max_cost_usd": 1.0, "max_concurrent": 2}


def _usd(amount: float) -> MessageMetrics:
    """Return the usage of one request that costs *amount*: the model costs 1 USD per million input tokens."""
    return usage(round(amount * 1_000_000), 0)


def _turn(amount: float = 0.6) -> list[Any]:
    return answer("done", _usd(amount))


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with a room, the real job runtime and scripted agents on a priced model; no run is started."""
    options = {"extra_tools": ("workflow",), "require_approval": (), "pricing": ModelPricing(input=1, output=2)}
    with workflow_stack(tmp_path, other_agents=("helper",), delegate_to=("helper",), **options) as stack:
        monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
        monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
        monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)
        stack.show()
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        yield stack
        await stack.close()


async def _agent(stack: WorkflowStack, index: int) -> str:
    """Make the agent call at `@index` and return its state as the call was answered."""
    return (await stack.submit(f"@{index}", "agent", {"prompt": f"Look into part {index}."})).state


def _states(stack: WorkflowStack) -> dict[str, str]:
    return {node.key: node.state.value for node in stack.store.nodes(stack.run_id)}


def _questions(stack: WorkflowStack) -> list[dict[str, Any]]:
    assert stack.room is not None
    return stack.room.carried(WORKFLOW_QUESTION_CONTENT_KEY)


def _cards(stack: WorkflowStack) -> list[dict[str, Any]]:
    assert stack.room is not None
    return stack.room.carried(WORKFLOW_RUN_CONTENT_KEY)


async def _settled(stack: WorkflowStack, *keys: str) -> None:
    done = (WorkflowNodeState.COMPLETED, WorkflowNodeState.FAILED, WorkflowNodeState.CANCELLED)
    await stack.until(lambda: all(stack.store.get_node(stack.run_id, key).state in done for key in keys))


async def _over_the_limit(stack: WorkflowStack, later: asyncio.Event | None = None) -> asyncio.Event:
    """Start four agents of a run with a limit of 1 USD: one spends 1.20, one still runs, two wait for a place.

    The two that wait spend nothing before *later* is set, when it is given.
    """
    await stack.start(**_LIMIT)
    gate = asyncio.Event()
    held = [] if later is None else [Await(later.wait)]
    model = stack.child_script(_turn(1.2), [Await(gate.wait), *_turn()], [*held, *_turn()], [*held, *_turn()])
    assert [await _agent(stack, index) for index in range(4)] == ["running", "running", "queued", "queued"]
    await stack.until(lambda: "completed" in _states(stack).values() and len(model.requests) >= 2)
    assert len(model.requests) == 2
    return gate


@pytest.mark.asyncio
async def test_cost_limit_lets_running_calls_finish_starts_no_new_node_and_ends_interrupted(
    stack: WorkflowStack,
) -> None:
    """At the limit no queued or new call starts, running ones finish, and the run ends interrupted with the reason."""
    gate = await _over_the_limit(stack)
    model = stack.models["watcher"]

    # One agent still runs: the run keeps going and queued or newly submitted calls do not start.
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING
    assert await _agent(stack, 4) == "queued"
    assert len(model.requests) == 2

    gate.set()
    await _settled(stack, "@0", "@1")
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.INTERRUPTED)

    run = stack.store.get_run(stack.run_id)
    reason = "Stopped at the cost limit ($1.80 of $1.00)."
    assert (run.state, run.error) == (WorkflowRunState.INTERRUPTED, reason)
    assert await stack.engine.delivery(run) == reason
    assert _states(stack) == {
        "@0": "completed",
        "@1": "completed",
        "@2": "cancelled",
        "@3": "cancelled",
        "@4": "cancelled",
    }
    for key in ("@2", "@3", "@4"):
        node = stack.store.get_node(stack.run_id, key)
        assert node is not None
        assert node.handle is None
        assert "job_id" not in node.links
    assert len(model.requests) == 2
    assert _questions(stack) == []
    assert run_usage(stack.store, stack.run_id)["cost_usd"] == pytest.approx(1.8)

    await stack.views.show(stack.run_id)
    assert _cards(stack)[-1]["status"] == "interrupted"
    assert _cards(stack)[-1]["error"] == reason


@pytest.mark.asyncio
async def test_call_whose_start_was_under_way_when_the_limit_was_reached_does_not_start(
    stack: WorkflowStack,
) -> None:
    """A queued call that got its place just before the limit was reached starts nothing and ends the run if idle."""
    gate = await _over_the_limit(stack)
    places = stack.engine.live.places[stack.run_id]
    started = False

    async def start() -> None:
        nonlocal started
        started = True

    places.working.add("@2")
    await stack.engine.live.start_queued(stack.run_id, "@2", start)

    assert (started, "@2" in places.working, len(stack.models["watcher"].requests)) == (False, False, 2)
    gate.set()
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.INTERRUPTED)


@pytest.mark.asyncio
async def test_resume_with_higher_limit_continues_and_reuses_completed_nodes(stack: WorkflowStack) -> None:
    """Resuming without a higher limit is refused; resuming with a higher limit reuses saved nodes and runs the rest."""
    gate = await _over_the_limit(stack)
    gate.set()
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.INTERRUPTED)
    assert len(stack.models["watcher"].requests) == 2

    # Without raising max_cost_usd (or with a limit not above spent), resume is refused.
    with pytest.raises(CallFailed, match=r"max_cost_usd above \$1\.80"):
        await stack.call("resume_workflow", run_id=stack.run_id, call_id="resume-same")
    with pytest.raises(CallFailed, match=r"max_cost_usd above \$1\.80"):
        await stack.call("resume_workflow", run_id=stack.run_id, max_cost_usd=1.8, call_id="resume-equal")
    with pytest.raises(CallFailed, match="max_cost_usd must be a positive amount in USD"):
        await stack.call("resume_workflow", run_id=stack.run_id, max_cost_usd=0, call_id="resume-zero")
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.INTERRUPTED

    # Resuming with a higher limit reuses completed nodes without re-running them, then stops at the new limit.
    handle = json.loads(await stack.call("resume_workflow", run_id=stack.run_id, max_cost_usd=2.5, call_id="resume-up"))
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)
    assert stack.store.get_run(stack.run_id).max_cost_usd == 2.5

    assert (await stack.submit("@0", "agent", {"prompt": "Look into part 0."})).cached is True
    assert (await stack.submit("@1", "agent", {"prompt": "Look into part 1."})).cached is True
    assert len(stack.models["watcher"].requests) == 2

    # @2 spends 0.60 -> 2.40 < 2.50, so it finishes and the run stays running.
    assert await stack.result("@2", "agent", {"prompt": "Look into part 2."}) == "done"
    assert len(stack.models["watcher"].requests) == 3
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING

    # @3 spends 0.60 -> 3.00 >= 2.50, so the run stops interrupted at the new limit.
    assert await stack.result("@3", "agent", {"prompt": "Look into part 3."}) == "done"
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.INTERRUPTED)
    run = stack.store.get_run(stack.run_id)
    assert run.error == "Stopped at the cost limit ($3.00 of $2.50)."
    assert handle["run_id"] == stack.run_id


@pytest.mark.asyncio
async def test_further_turn_of_an_agent_stops_at_the_limit_without_starting_correction(stack: WorkflowStack) -> None:
    """A schema correction turn is new spending: at the limit it does not start and the run ends interrupted."""
    await stack.start(max_cost_usd=0.5, max_concurrent=1)
    good = '```json\n{"ok": true}\n```'
    model = stack.child_script(answer("no json here", _usd(0.6)), answer(good, _usd(0.1)))
    ask = {"prompt": "Does it hold?", "schema": {"type": "object", "required": ["ok"]}}

    await stack.submit("@0", "agent", ask)
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.INTERRUPTED)

    assert len(model.requests) == 1
    assert _states(stack)["@0"] == "cancelled"
    assert stack.store.get_run(stack.run_id).error == "Stopped at the cost limit ($0.60 of $0.50)."
    assert run_usage(stack.store, stack.run_id)["cost_usd"] == pytest.approx(0.6)

    await stack.resume(stack.run_id, max_cost_usd=1.0)
    assert await stack.result("@0", "agent", ask) == {"ok": True}
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_further_turn_that_a_call_finds_due_stops_at_the_limit(stack: WorkflowStack) -> None:
    """Reconciling a finished turn after a restart starts no correction turn over the limit and ends interrupted."""
    await stack.start(max_cost_usd=0.5, max_concurrent=1)
    gate = asyncio.Event()
    wrong = [Await(gate.wait), *answer("no json here", _usd(0.6))]
    model = stack.child_script(wrong, answer('```json\n{"ok": true}\n```', _usd(0.1)))
    ask = {"prompt": "Does it hold?", "schema": {"type": "object", "required": ["ok"]}}
    await stack.submit("@0", "agent", ask)
    await stack.until(lambda: len(model.requests) == 1)
    for waiting in stack.engine.live.tasks.pop(stack.run_id):
        waiting.cancel()
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)
    assert await stack.maintain() == [stack.run_id]
    job_id = stack.store.get_node(stack.run_id, "@0").handle.ref
    gate.set()
    jobs = stack.jobs.retained_jobs
    await stack.until(lambda: all(job.status in TERMINAL_STATUSES for job in jobs() if job.job_id == job_id))

    receipt = await stack.submit("@0", "agent", ask)
    assert receipt.state == "queued"
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.INTERRUPTED)
    assert len(model.requests) == 1
    assert stack.store.get_run(stack.run_id).error == "Stopped at the cost limit ($0.60 of $0.50)."


@pytest.mark.asyncio
async def test_budget_counts_rerun_turns_and_cached_nodes_do_not_move_it(stack: WorkflowStack) -> None:
    """Reusing a saved call spends nothing; rerunning it adds the new turn's cost and stops at the limit."""
    await stack.start(**_LIMIT)
    stack.child_script(_turn(), _turn())
    assert await stack.result("@0", "agent", {"prompt": "Look into it."}) == "done"
    await stack.end_attempt(ScriptRunState.FAILED)

    await stack.resume(stack.run_id)
    assert (await stack.submit("@0", "agent", {"prompt": "Look into it."})).cached
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING
    await stack.end_attempt(ScriptRunState.FAILED)

    await stack.resume(stack.run_id, rerun=["n1"])
    assert await stack.result("@0", "agent", {"prompt": "Look into it."}) == "done"
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.INTERRUPTED)
    assert stack.store.get_run(stack.run_id).error == "Stopped at the cost limit ($1.20 of $1.00)."


@pytest.mark.asyncio
async def test_without_a_limit_nothing_stops(stack: WorkflowStack) -> None:
    """A run that names no limit spends what its agents cost and keeps running."""
    await stack.start(max_concurrent=2)
    stack.child_script(_turn(1.2), _turn(), _turn())

    for index in range(3):
        assert await stack.result(f"@{index}", "agent", {"prompt": f"Look into part {index}."}) == "done"

    assert _questions(stack) == []
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_cost_limit_under_a_cent_formats_amounts(stack: WorkflowStack) -> None:
    """Amounts under a cent keep up to six decimal places instead of rounding to `$0.00`."""
    await stack.start(max_cost_usd=0.000001, max_concurrent=1)
    stack.child_script(_turn(0.00042))
    assert await stack.result("@0", "agent", {"prompt": "Look into part 0."}) == "done"
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.INTERRUPTED)

    assert stack.store.get_run(stack.run_id).error == "Stopped at the cost limit ($0.00042 of $0.000001)."


@pytest.mark.asyncio
async def test_start_workflow_saves_the_limit_and_refuses_one_that_is_no_amount(stack: WorkflowStack) -> None:
    """`max_cost_usd` is the run's, and anything but a positive number is refused before a run exists."""
    source = "def main(args):\n    return 'counted'\n"
    handle = json.loads(await stack.call("start_workflow", source=source, max_cost_usd=2.5))

    assert stack.store.get_run(handle["run_id"]).max_cost_usd == 2.5
    for wrong in (0, -1, "much", float("inf"), float("nan")):
        with pytest.raises(CallFailed, match="max_cost_usd must be a positive amount in USD"):
            await stack.call("start_workflow", source=source, max_cost_usd=wrong, call_id=f"wrong-{wrong}")
    assert len(stack.store.list_runs()) == 1


@pytest.mark.parametrize(("notice_usd", "large"), [(10, True), (0, False)])
@pytest.mark.asyncio
async def test_large_notice_fires_when_the_projection_passes_ten_dollars(
    stack: WorkflowStack,
    notice_usd: float,
    large: bool,
) -> None:
    """A run whose projected cost passes the operator's amount is marked large, once; 0 turns that test off."""
    stack.limits = WorkflowLimits(large_run_notice_usd=notice_usd)
    await stack.start(title="Audit", job_id="job-1", max_concurrent=1)
    stack.child_script(_turn(6.0), [Await(asyncio.Event().wait), *_turn()])
    assert await stack.result("@0", "agent", {"prompt": "Look into part 0."}) == "done"
    # The first agent's place may still be its own for a moment: the second starts then or right after.
    assert await _agent(stack, 1) in ("running", "queued")
    await stack.until(lambda: _states(stack)["@1"] == "running")

    # One turn of 6 USD ended and one agent is still to end: 6 spent, and 6 more projected.
    await stack.views.show(stack.run_id)
    await stack.views.show(stack.run_id)

    log = ["Large run: 2 agents, about $12.00 projected."] if large else []
    assert (stack.store.get_run(stack.run_id).large, stack.store.log(stack.run_id)) == (large, log)
    await stack.engine.cancel(stack.run_id, reason="Done.")


@pytest.mark.asyncio
async def test_nested_run_stops_at_ancestors_cost_limit_and_parent_stops_too(stack: WorkflowStack) -> None:
    """A nested run enforces its ancestors' cost limits: after exceeding parent's $0.10, no second inner agent starts."""
    await stack.start(max_cost_usd=0.10, max_concurrent=1)
    parent = stack.run_id
    model = stack.child_script(_turn(0.20), _turn(0.20))

    start_inner = tool_args("workflow", "start_workflow", source="def main(args): return 'inner'", max_concurrent=1)
    await stack.submit("@0", "tool", start_inner)
    # Queue a second call in the parent behind the nested run; it must never start once the parent limit is hit.
    assert await _agent(stack, 1) == "queued"
    await stack.until(lambda: [run.state for run in stack.store.child_runs(parent)] == [WorkflowRunState.RUNNING])
    [inner] = stack.store.child_runs(parent)
    assert inner.max_cost_usd is None

    stack.run_id = inner.run_id
    assert await _agent(stack, 0) == "running"
    assert await _agent(stack, 1) == "queued"
    await stack.until(lambda: stack.store.get_run(inner.run_id).state is WorkflowRunState.INTERRUPTED)

    reason = "Stopped at the cost limit ($0.20 of $0.10)."
    ended_inner = stack.store.get_run(inner.run_id)
    assert (ended_inner.state, ended_inner.error) == (WorkflowRunState.INTERRUPTED, reason)
    inner_second = stack.store.get_node(inner.run_id, "@1")
    assert inner_second is not None
    assert (inner_second.state, inner_second.handle) == (WorkflowNodeState.CANCELLED, None)

    stack.run_id = parent
    await stack.until(lambda: stack.store.get_run(parent).state is WorkflowRunState.INTERRUPTED)
    ended_parent = stack.store.get_run(parent)
    assert (ended_parent.state, ended_parent.error) == (WorkflowRunState.INTERRUPTED, reason)
    assert len(model.requests) == 1
    assert run_usage(stack.store, parent)["cost_usd"] == pytest.approx(0.20)
    queued_parent = stack.store.get_node(parent, "@1")
    assert queued_parent is not None
    assert (queued_parent.state, queued_parent.handle) == (WorkflowNodeState.CANCELLED, None)


@pytest.mark.asyncio
async def test_nested_run_with_lower_own_limit_stops_without_stopping_parent(stack: WorkflowStack) -> None:
    """A nested run with a tighter limit stops at its own limit while its under-budget parent keeps running."""
    await stack.start(max_cost_usd=1.0, max_concurrent=2)
    parent = stack.run_id
    model = stack.child_script(_turn(0.20), _turn(0.20))

    start_inner = tool_args("workflow", "start_workflow", source="def main(args): return 'inner'", max_cost_usd=0.10)
    await stack.submit("@0", "tool", start_inner)
    await stack.until(lambda: [run.state for run in stack.store.child_runs(parent)] == [WorkflowRunState.RUNNING])
    [inner] = stack.store.child_runs(parent)

    stack.run_id = inner.run_id
    assert await stack.result("@0", "agent", {"prompt": "First inner agent."}) == "done"
    await stack.until(lambda: stack.store.get_run(inner.run_id).state is WorkflowRunState.INTERRUPTED)
    assert stack.store.get_run(inner.run_id).error == "Stopped at the cost limit ($0.20 of $0.10)."

    stack.run_id = parent
    await _settled(stack, "@0")
    parent_launch = stack.store.get_node(parent, "@0")
    assert parent_launch is not None
    assert parent_launch.state is WorkflowNodeState.CANCELLED
    assert parent_launch.error is not None
    assert parent_launch.error["kind"] == "interrupted"
    assert stack.store.get_run(parent).state is WorkflowRunState.RUNNING
    assert await stack.result("@1", "agent", {"prompt": "Parent continues."}) == "done"
    assert len(model.requests) == 2
    assert stack.store.get_run(parent).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_raising_parent_limit_continues_stopped_nested_child(stack: WorkflowStack) -> None:
    """Raising a parent's cost limit continues the same stopped child and delivers its real report with 2 turns total."""
    inner_source = "def main(args):\n    return 'inner'\n"
    launch = tool_args("workflow", "start_workflow", source=inner_source)
    await stack.start(max_cost_usd=0.10, max_concurrent=1)
    parent = stack.run_id
    model = stack.child_script(answer("first", _usd(0.20)), answer("next", _usd(0.20)))

    await stack.submit("@0", "tool", launch)
    await stack.until(lambda: [run.state for run in stack.store.child_runs(parent)] == [WorkflowRunState.RUNNING])
    [child] = stack.store.child_runs(parent)

    stack.run_id = child.run_id
    assert await stack.result("@0", "agent", {"prompt": "First"}) == "first"
    await stack.until(lambda: stack.store.get_run(child.run_id).state is WorkflowRunState.INTERRUPTED)

    stack.run_id = parent
    await stack.until(lambda: stack.store.get_run(parent).state is WorkflowRunState.INTERRUPTED)
    stopped_node = stack.store.get_node(parent, "@0")
    assert stopped_node is not None
    assert stopped_node.state is not WorkflowNodeState.COMPLETED
    assert len(model.requests) == 1

    await stack.call("resume_workflow", run_id=parent, max_cost_usd=1.0, call_id="raise-parent-limit")
    await stack.until(lambda: stack.store.get_run(parent).attempt == 2)

    replayed = await stack.submit("@0", "tool", launch)
    assert replayed.cached is False
    await stack.until(
        lambda: (
            stack.store.get_run(child.run_id).state is WorkflowRunState.RUNNING
            and stack.store.get_run(child.run_id).attempt == 2
        ),
    )
    assert [run.run_id for run in stack.store.child_runs(parent)] == [child.run_id]
    assert run_usage(stack.store, parent)["attempt_cost_usd"] == pytest.approx(0.0)

    stack.run_id = child.run_id
    first_replayed = await stack.submit("@0", "agent", {"prompt": "First"})
    assert (first_replayed.cached, first_replayed.result) == (True, "first")
    second = await stack.result("@1", "agent", {"prompt": "Second"})
    assert second == "next"
    await stack.finish_attempt(result=f"{first_replayed.result}{second}")

    stack.run_id = parent
    await _settled(stack, "@0")
    parent_node = stack.store.get_node(parent, "@0")
    assert parent_node is not None
    assert (parent_node.state, parent_node.result) == (WorkflowNodeState.COMPLETED, "firstnext")
    await stack.finish_attempt(result=parent_node.result)
    assert stack.store.get_run(parent).result == "firstnext"
    assert len(model.requests) == 2
    reported = json.loads(await stack.call("get_workflow", run_id=parent))["usage"]
    assert (reported["attempt_cost_usd"], reported["cost_usd"]) == pytest.approx((0.20, 0.40))


@pytest.mark.asyncio
async def test_delegation_submitted_after_cost_limit_does_not_request_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the cost limit is already reached while another call drains, gated delegation asks for no approval."""
    options = {
        "extra_tools": ("workflow",),
        "other_agents": ("helper",),
        "delegate_to": ("helper",),
        "require_approval": ("run_subagent",),
        "pricing": ModelPricing(input=1, output=2),
    }
    with workflow_stack(tmp_path, **options) as gated:
        monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
        monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
        await gated.open_jobs()
        gated.script_agents(monkeypatch)
        await gated.start(max_concurrent=2, max_cost_usd=0.10)
        hold, decision = asyncio.Event(), asyncio.Event()
        gated.counting_tool.after_effect = hold.wait
        gated.resolver.hold_approvals = decision
        try:
            await gated.submit("@0", "tool", tool_args("counter", "append", line="pending"))
            await gated.until(lambda: bool(gated.counting_tool.lines))
            model = gated.child_script(answer("first", _usd(0.20)))
            assert await gated.result("@1", "agent", {"prompt": "First"}) == "first"
            assert run_usage(gated.store, gated.run_id)["cost_usd"] == pytest.approx(0.20)

            delegate = tool_args("delegate", "run_subagent", agent_name="helper", task="Do more work.")
            receipt = await gated.submit("@2", "tool", delegate)
            assert receipt.state == "queued"
            await asyncio.sleep(0.15)
            assert gated.resolver.asks == []
            assert len(model.requests) == 1

            hold.set()
            await gated.until(lambda: gated.store.get_run(gated.run_id).state is WorkflowRunState.INTERRUPTED)
            assert gated.resolver.asks == []
            assert gated.store.get_run(gated.run_id).error == "Stopped at the cost limit ($0.20 of $0.10)."
        finally:
            decision.set()
            hold.set()
            await gated.close()


@pytest.mark.asyncio
async def test_edited_resume_replacing_interrupted_nested_launch_does_not_repeat_write(stack: WorkflowStack) -> None:
    """Replacing an interrupted nested launch at @0 with another tool keeps that tool's completion across resumes."""
    launch = tool_args("workflow", "start_workflow", source="def main(args):\n    return 'inner'\n")
    append = tool_args("counter", "append", line="done")
    await stack.start(max_cost_usd=0.10, max_concurrent=1)
    parent = stack.run_id
    stack.child_script(answer("first", _usd(0.20)))

    await stack.submit("@0", "tool", launch)
    await stack.until(lambda: [run.state for run in stack.store.child_runs(parent)] == [WorkflowRunState.RUNNING])
    [child] = stack.store.child_runs(parent)

    stack.run_id = child.run_id
    assert await stack.result("@0", "agent", {"prompt": "First"}) == "first"
    await stack.until(lambda: stack.store.get_run(child.run_id).state is WorkflowRunState.INTERRUPTED)

    stack.run_id = parent
    await stack.until(lambda: stack.store.get_run(parent).state is WorkflowRunState.INTERRUPTED)

    edited = "def main(args):\n    return 'edited'\n"
    await stack.call("resume_workflow", run_id=parent, source=edited, max_cost_usd=1.0, call_id="edit-to-append")
    await stack.until(lambda: stack.store.get_run(parent).attempt == 2)

    assert await stack.result("@0", "tool", append) == "appended"
    assert stack.counting_tool.lines == ["done"]
    saved = stack.store.get_node(parent, "@0")
    assert saved is not None
    assert (saved.state, saved.result) == (WorkflowNodeState.COMPLETED, "appended")
    second_job = stack.store.get_run(parent).job_id
    assert second_job is not None
    await stack.finish_attempt(result="done")
    await stack.jobs.wait(second_job, owner=stack.context.execution_identity, depth=0)

    await stack.call("resume_workflow", run_id=parent, call_id="resume-again")
    await stack.until(lambda: stack.store.get_run(parent).attempt == 3)

    replayed = await stack.submit("@0", "tool", append)
    assert (replayed.state, replayed.cached, replayed.result) == ("completed", True, "appended")
    assert stack.counting_tool.lines == ["done"]


@pytest.mark.asyncio
async def test_resuming_nested_launch_with_boolean_instead_of_int_starts_new_child(stack: WorkflowStack) -> None:
    """Changing nested launch args from `1` to `True` starts a new child instead of reusing the integer-arg child."""
    inner_source = "def main(args):\n    return 'inner'\n"
    launch_int = tool_args("workflow", "start_workflow", source=inner_source, args={"enabled": 1})
    launch_bool = tool_args("workflow", "start_workflow", source=inner_source, args={"enabled": True})
    await stack.start(max_cost_usd=0.10, max_concurrent=1)
    parent = stack.run_id
    stack.child_script(answer("first", _usd(0.20)))

    await stack.submit("@0", "tool", launch_int)
    await stack.until(lambda: [run.state for run in stack.store.child_runs(parent)] == [WorkflowRunState.RUNNING])
    [old_child] = stack.store.child_runs(parent)

    stack.run_id = old_child.run_id
    assert await stack.result("@0", "agent", {"prompt": "First"}) == "first"
    await stack.until(lambda: stack.store.get_run(old_child.run_id).state is WorkflowRunState.INTERRUPTED)

    stack.run_id = parent
    await stack.until(lambda: stack.store.get_run(parent).state is WorkflowRunState.INTERRUPTED)

    edited = "def main(args):\n    return 'bool'\n"
    await stack.call("resume_workflow", run_id=parent, source=edited, max_cost_usd=1.0, call_id="edit-bool-arg")
    await stack.until(lambda: stack.store.get_run(parent).attempt == 2)

    await stack.submit("@0", "tool", launch_bool)
    await stack.until(lambda: any(run.state is WorkflowRunState.RUNNING for run in stack.store.child_runs(parent)))
    [active] = [run for run in stack.store.child_runs(parent) if run.state is WorkflowRunState.RUNNING]
    assert active.run_id != old_child.run_id

    stack.run_id = active.run_id
    info = await stack.engine.calls.run_info(stack.attempt)
    assert info["args"] == {"enabled": True}
    assert info["args"]["enabled"] is True
    await stack.finish_attempt(result="checked args")

    stack.run_id = parent
    await _settled(stack, "@0")
    parent_node = stack.store.get_node(parent, "@0")
    assert parent_node is not None
    assert (parent_node.state, parent_node.result) == (WorkflowNodeState.COMPLETED, "checked args")
