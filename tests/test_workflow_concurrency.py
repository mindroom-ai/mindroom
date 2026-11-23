"""Tests for how many calls of a workflow run work at one time: the run's `max_concurrent`.

Level: engine harness (`WorkflowStack`) with the real broker, the real job runtime and scripted agents.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.engine import WorkflowLimits
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.fakes.scripted_model import Await, answer
from tests.workflow_helpers import NodeFailed, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.workflow_runs.models import NodeReceipt

_COUNT = tool_args("counter", "count")
_QUEUED, _RUNNING, _WAITING = WorkflowNodeState.QUEUED, WorkflowNodeState.RUNNING, WorkflowNodeState.WAITING
_COMPLETED, _CANCELLED = WorkflowNodeState.COMPLETED, WorkflowNodeState.CANCELLED


async def _opened(stack: WorkflowStack, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    await stack.open_jobs()
    stack.script_agents(monkeypatch)
    yield stack
    await stack.close()


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with the real job runtime and scripted agents, where no tool asks for an approval.

    A test starts its run with the limit it needs.
    """
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        async for opened in _opened(stack, monkeypatch):
            yield opened


@pytest_asyncio.fixture
async def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The same harness, whose operator asks for an approval before `append` runs."""
    with workflow_stack(tmp_path, extra_tools=("workflow",)) as stack:
        async for opened in _opened(stack, monkeypatch):
            yield opened


def _states(stack: WorkflowStack) -> list[WorkflowNodeState]:
    return [node.state for node in stack.store.nodes(stack.run_id)]


def _append(line: str) -> dict[str, object]:
    return tool_args("counter", "append", line=line)


async def _settled(stack: WorkflowStack, receipt: NodeReceipt) -> NodeReceipt:
    while receipt.state not in ("completed", "failed", "cancelled", "indeterminate"):
        receipt = await stack.engine.node_receipt(stack.attempt, receipt.id, wait=5)
    return receipt


@pytest.mark.asyncio
async def test_engine_runs_at_most_max_concurrent_nodes(stack: WorkflowStack) -> None:
    """Six agents of a run with `max_concurrent=2` work two at a time: the others wait `queued`, and all finish."""
    await stack.start(max_concurrent=2)
    working, peak, gate = 0, 0, asyncio.Event()

    async def work() -> None:
        nonlocal working, peak
        working += 1
        peak = max(peak, working)
        await gate.wait()
        await asyncio.sleep(0.01)
        working -= 1

    stack.child_script(*[[Await(work), *answer("done")] for _ in range(6)])
    receipts = [await stack.submit(f"/0:p{n}@0", "agent", {"prompt": f"task {n}"}) for n in range(6)]

    await stack.until(lambda: working == 2)
    assert [receipt.state for receipt in receipts] == ["running", "running", *["queued"] * 4]
    assert _states(stack) == [_RUNNING, _RUNNING, *[_QUEUED] * 4]
    # Only the working agents have a job: a queued call has started nothing.
    assert len([job for job in stack.jobs.retained_jobs() if job.kind == "delegation"]) == 2
    gate.set()

    assert [(await _settled(stack, receipt)).result for receipt in receipts] == ["done"] * 6
    assert (peak, _states(stack)) == (2, [_COMPLETED] * 6)


@pytest.mark.asyncio
async def test_waiting_node_frees_its_slot_and_takes_one_again(gated: WorkflowStack) -> None:
    """A call that waits for a person holds no place, so the next call works; once it goes on it holds one again."""
    stack = gated
    await stack.start(max_concurrent=1)
    stack.resolver.hold_approvals = asyncio.Event()
    written = asyncio.Event()
    stack.counting_tool.after_effect = written.wait

    first = await stack.submit("@0", "tool", _append("a"))
    await stack.until(lambda: _states(stack) == [_WAITING])
    # The only place is free while the first call waits for its approval.
    assert await stack.result("@1", "tool", _COUNT) == 1

    stack.resolver.hold_approvals.set()
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    third = await stack.submit("@2", "tool", _COUNT)
    assert (third.state, _states(stack)) == ("queued", [_RUNNING, _COMPLETED, _QUEUED])
    written.set()

    assert ((await _settled(stack, first)).result, (await _settled(stack, third)).result) == ("appended", 3)


@pytest.mark.asyncio
async def test_approved_tool_waits_for_a_free_slot_before_running(gated: WorkflowStack) -> None:
    """A tool whose approval arrives while another call holds the only slot queues until that slot is free."""
    stack = gated
    await stack.start(max_concurrent=1)
    approver = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    stack.resolver.hold_approvals = approver

    async def hold_second() -> None:
        second_started.set()
        await release_second.wait()

    stack.child_script([Await(hold_second), *answer("ok")])
    first = await stack.submit("@0", "tool", _append("a"))
    await stack.until(lambda: _states(stack) == [_WAITING])
    second = await stack.submit("@1", "agent", {"prompt": "other work"})
    await asyncio.wait_for(second_started.wait(), 8)

    try:
        approver.set()
        await stack.until(lambda: _states(stack) == [_QUEUED, _RUNNING])
        await asyncio.sleep(0.05)
        assert (stack.counting_tool.lines, _states(stack)) == ([], [_QUEUED, _RUNNING])
    finally:
        release_second.set()

    assert ((await _settled(stack, second)).result, (await _settled(stack, first)).result) == ("ok", "appended")
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_tool_calls_of_an_attempt_work_side_by_side_whenever_they_were_claimed(stack: WorkflowStack) -> None:
    """The broker holds no call of a workflow attempt back: one that was claimed while another ended waits for none.

    The reproducer of the second full review (Opus B1): the attempt's lock was dropped while a call's claim was on
    its thread, and that call then made the lock of a plain script, which runs one call at a time.
    """
    await stack.start(max_concurrent=4)
    gates = {str(n): asyncio.Event() for n in range(4)}
    working: set[str] = set()
    peak: list[int] = []

    async def effect() -> None:
        line = stack.counting_tool.lines[-1]
        working.add(line)
        peak.append(len(working))
        await gates[line].wait()
        working.discard(line)

    stack.counting_tool.after_effect = effect
    receipts = [await stack.submit("@0", "tool", _append("0"))]
    await stack.until(lambda: "0" in working)
    # The claim of call 1 is held on its thread while call 0 ends, which leaves the attempt without a call in flight.
    claiming, claim = threading.Event(), threading.Event()
    original = stack.script_store.claim_call

    def held(**call: object) -> object:
        claiming.set()
        claim.wait(10)
        return original(**call)

    stack.script_store.claim_call = held  # type: ignore[method-assign]
    receipts.append(await stack.submit("@1", "tool", _append("1")))
    await stack.until(claiming.is_set)
    stack.script_store.claim_call = original  # type: ignore[method-assign]
    gates["0"].set()
    await stack.until(lambda: not any(key[0] == stack.attempt.run_id for key in stack.broker._tasks))
    claim.set()
    await stack.until(lambda: "1" in working)

    receipts += [await stack.submit(f"@{n}", "tool", _append(str(n))) for n in (2, 3)]
    await stack.until(lambda: working == {"1", "2", "3"})
    for gate in gates.values():
        gate.set()

    assert [(await _settled(stack, receipt)).state for receipt in receipts] == ["completed"] * 4
    assert max(peak) == 3


@pytest.mark.asyncio
async def test_agent_that_starts_in_the_place_a_tool_call_leaves_is_not_inside_that_call(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queued agent's job starts from the ending tool call's task, and must not take that call for its own.

    The reproducer of the second full review (Opus M1): a subagent of such an agent nested its run under the tool.
    """
    seen: list[object] = []

    async def launch(*_args: object, **_kwargs: object) -> object:
        seen.append(engine_module.calling_node())
        msg = "Seen."
        raise engine_module.AgentNodeError(msg)

    monkeypatch.setattr(engine_module, "launch_child", launch)
    await stack.start(max_concurrent=1)
    gate = asyncio.Event()
    stack.counting_tool.after_effect = gate.wait
    await stack.submit("@0", "tool", _append("x"))
    await stack.until(lambda: stack.counting_tool.calls == 1)
    assert (await stack.submit("@1", "agent", {"prompt": "hi"})).state == "queued"

    gate.set()
    await stack.until(lambda: seen)

    assert seen == [None]


@pytest.mark.asyncio
async def test_cancel_starts_no_queued_node(stack: WorkflowStack) -> None:
    """A run that is stopped starts none of the calls that waited for a place: they end cancelled, having run nothing."""
    await stack.start(max_concurrent=1)
    stack.counting_tool.after_effect = asyncio.Event().wait
    await stack.submit("@0", "tool", _append("a"))
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    queued = [await stack.submit(f"@{n}", "tool", _append(f"never {n}")) for n in (1, 2)]
    assert [receipt.state for receipt in queued] == ["queued", "queued"]

    await stack.engine.cancel(stack.run_id, reason="Stopped.")

    assert (stack.counting_tool.calls, stack.counting_tool.lines) == (1, ["a"])
    assert _states(stack)[1:] == [_CANCELLED, _CANCELLED]
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.CANCELLED
    # An ended run keeps no count of working calls behind.
    assert stack.engine._places == {}


@pytest.mark.asyncio
async def test_closed_engine_starts_no_queued_call(stack: WorkflowStack) -> None:
    """A primary that shuts down stops its working calls and starts none in the places they leave."""
    await stack.start(max_concurrent=1)
    stack.counting_tool.after_effect = asyncio.Event().wait
    await stack.submit("@0", "tool", _append("a"))
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    assert (await stack.submit("@1", "tool", _append("b"))).state == "queued"

    await stack.engine.close()
    for _ in range(10):
        await asyncio.sleep(0.01)

    assert (stack.counting_tool.lines, _states(stack)[1]) == (["a"], _QUEUED)


@pytest.mark.asyncio
async def test_queued_agent_whose_node_ended_meanwhile_starts_nothing(stack: WorkflowStack) -> None:
    """The place of a queued call is checked against its saved node: one that has an end by then launches no agent."""
    await stack.start(max_concurrent=1)
    gate = asyncio.Event()
    stack.child_script([Await(gate.wait), *answer("done")], answer("never"))
    first = await stack.submit("@0", "agent", {"prompt": "one"})
    await stack.until(lambda: len(stack.models["watcher"].requests) == 1)
    queued = await stack.submit("@1", "agent", {"prompt": "two"})
    assert queued.state == "queued"
    # As a run's end would leave it, had it come between the place becoming free and the call taking it.
    end = {"state": _CANCELLED, "error": {"kind": "cancelled", "message": "Stopped."}}
    assert stack.store.settle_node(stack.run_id, "@1", 1, stack.store.get_run(stack.run_id).epoch, **end)
    gate.set()

    assert (await _settled(stack, first)).result == "done"
    await stack.until(lambda: stack.engine._places[stack.run_id].working == set())
    assert (len(stack.models["watcher"].requests), _states(stack)[1]) == (1, _CANCELLED)
    assert len(stack.jobs.retained_jobs()) == 1


@pytest.mark.asyncio
async def test_queued_tool_call_whose_node_ended_meanwhile_runs_nothing(stack: WorkflowStack) -> None:
    """The same check for a tool call: a queued node that has an end when its place comes calls no tool."""
    await stack.start(max_concurrent=1)
    written = asyncio.Event()
    stack.counting_tool.after_effect = written.wait
    first = await stack.submit("@0", "tool", _append("a"))
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    assert (await stack.submit("@1", "tool", _append("never"))).state == "queued"
    end = {"state": _CANCELLED, "error": {"kind": "cancelled", "message": "Stopped."}}
    assert stack.store.settle_node(stack.run_id, "@1", 1, stack.store.get_run(stack.run_id).epoch, **end)
    written.set()

    assert (await _settled(stack, first)).result == "appended"
    await stack.until(lambda: stack.engine._places[stack.run_id].working == set())
    assert (stack.counting_tool.lines, _states(stack)[1]) == (["a"], _CANCELLED)


@pytest.mark.asyncio
async def test_attempt_that_failed_starts_no_queued_call_and_keeps_no_places(stack: WorkflowStack) -> None:
    """A script that failed while calls waited for a place ends its attempt: none of them starts after it."""
    await stack.start(max_concurrent=1)
    stack.counting_tool.after_effect = asyncio.Event().wait
    await stack.submit("@0", "tool", _append("a"))
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    assert (await stack.submit("@1", "tool", _append("never"))).state == "queued"

    await stack.finish_attempt(error="KeyError: 'x'")

    assert (stack.counting_tool.lines, stack.engine._places) == (["a"], {})
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.FAILED


@pytest.mark.asyncio
async def test_queued_call_of_an_attempt_that_ended_never_ran_and_runs_in_the_next(stack: WorkflowStack) -> None:
    """A call that still waited for a place when its attempt ended is safe to run: the next attempt runs it once."""
    await stack.start(max_concurrent=1)
    stack.counting_tool.after_effect = asyncio.Event().wait
    await stack.submit("@0", "tool", _append("a"))
    await stack.until(lambda: stack.counting_tool.lines == ["a"])
    assert (await stack.submit("@1", "tool", _append("b"))).state == "queued"

    await stack.restart(kill=True)
    stack.counting_tool.after_effect = None
    assert await stack.recover() == [stack.run_id]
    left = stack.store.get_node(stack.run_id, "@1")
    assert left is not None
    assert (left.state, (left.error or {}).get("kind")) == (WorkflowNodeState.FAILED, "interrupted")

    with pytest.raises(NodeFailed) as first:
        await stack.result("@0", "tool", _append("a"))
    assert first.value.kind == "indeterminate"
    assert await stack.result("@1", "tool", _append("b")) == "appended"
    node = stack.store.get_node(stack.run_id, "@1")
    assert node is not None
    assert (node.generation, stack.counting_tool.lines) == (2, ["a", "b"])


@pytest.mark.asyncio
async def test_queued_agent_that_may_not_start_is_refused_when_its_turn_comes(stack: WorkflowStack) -> None:
    """A call that waited for a place is checked when it starts: one that may not run fails as `refused` then."""
    await stack.start(max_concurrent=1)
    gate = asyncio.Event()
    stack.child_script([Await(gate.wait), *answer("done")], answer("after"))
    first = await stack.submit("@0", "agent", {"prompt": "one"})
    await stack.until(lambda: len(stack.models["watcher"].requests) == 1)
    refused = await stack.submit("@1", "agent", {"prompt": "two", "model": "no-such-model"})
    last = await stack.submit("@2", "agent", {"prompt": "three"})
    assert (refused.state, last.state) == ("queued", "queued")
    gate.set()

    assert (await _settled(stack, first)).result == "done"
    ended = await _settled(stack, refused)
    assert (ended.state, (ended.error or {}).get("kind")) == ("failed", "refused")
    # The place the refused call took for a moment went on to the next one.
    assert (await _settled(stack, last)).result == "after"


@pytest.mark.asyncio
async def test_workflow_tool_calls_run_in_parallel(stack: WorkflowStack) -> None:
    """The tool calls of a run work at the same time, up to its limit; the broker holds them to one only for a script."""
    await stack.start(max_concurrent=3)
    working, peak, gate = 0, 0, asyncio.Event()

    async def effect() -> None:
        nonlocal working, peak
        working += 1
        peak = max(peak, working)
        await gate.wait()
        working -= 1

    stack.counting_tool.after_effect = effect
    receipts = [await stack.submit(f"@{n}", "tool", _append(str(n))) for n in range(5)]

    await stack.until(lambda: working == 3)
    assert _states(stack) == [_RUNNING] * 3 + [_QUEUED] * 2
    gate.set()

    assert [(await _settled(stack, receipt)).result for receipt in receipts] == ["appended"] * 5
    assert (peak, sorted(stack.counting_tool.lines)) == (3, ["0", "1", "2", "3", "4"])


@pytest.mark.asyncio
async def test_tool_rate_limit_delays_and_does_not_fail(stack: WorkflowStack, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call above the attempt's calls per minute waits until the minute has room for it, and then runs."""
    monkeypatch.setattr(engine_module, "_RATE_WAIT_SECONDS", 0.02)
    stack.limits = WorkflowLimits(max_tool_calls_per_minute=2)
    await stack.start()
    assert [await stack.result(f"@{n}", "tool", _COUNT) for n in range(2)] == [1, 2]

    third = await stack.submit("@2", "tool", _COUNT)
    await asyncio.sleep(0.1)
    assert (third.state, _states(stack)[2], stack.counting_tool.calls) == ("running", _RUNNING, 2)
    # A minute later, as the attempt's records tell it: the earlier calls are old enough.
    with closing(sqlite3.connect(stack.script_store.database_path)) as records:
        records.execute("UPDATE script_calls SET created_at = '2000-01-01T00:00:00+00:00'")
        records.commit()

    assert (await _settled(stack, third)).result == 3
    await stack.end_attempt(ScriptRunState.EXITED)


@pytest.mark.asyncio
async def test_queued_call_retrying_owner_readiness_does_not_start_on_stopping_run(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued agent retrying while its owner bot is not ready must not launch once cancel starts stopping the run."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.01)
    await stack.start(max_concurrent=2)
    holder_gate = asyncio.Event()
    finisher_gate = asyncio.Event()
    stack.child_script(
        [Await(holder_gate.wait), *answer("holder")],
        [Await(finisher_gate.wait), *answer("finisher")],
        answer("should never run"),
    )
    # Start two agents to fill max_concurrent=2, and queue the third.
    await stack.submit("@0", "agent", {"prompt": "hold"})
    await stack.submit("@1", "agent", {"prompt": "finish"})
    await stack.until(lambda: len(stack.models["watcher"].requests) == 2)
    queued = await stack.submit("@2", "agent", {"prompt": "queued"})
    assert queued.state == "queued"

    # Owner bot becomes temporarily unready just as @1 finishes so @2 enters _start_queued's retry loop.
    ready = False
    retries = 0
    default_access = stack.engine.owner_access

    def owner_access(run: object) -> object:
        nonlocal retries
        if not ready:
            retries += 1
            return None
        return default_access(run)  # type: ignore[arg-type]

    stack.engine.owner_access = owner_access  # type: ignore[assignment]
    finisher_gate.set()
    await stack.until(lambda: retries >= 2)

    # Hold cancel briefly after _stop_agents pops _places and marks _stopping, then restore owner readiness.
    original_cancel = stack.jobs.request_cancel

    async def slow_cancel(job_id: str) -> object:
        nonlocal ready
        ready = True
        await asyncio.sleep(0.08)
        holder_gate.set()
        return await original_cancel(job_id)

    monkeypatch.setattr(stack.jobs, "request_cancel", slow_cancel)
    await stack.engine.cancel(stack.run_id, reason="Stopped.")

    node = stack.store.get_node(stack.run_id, "@2")
    assert node is not None
    assert (node.state, node.handle, len(stack.models["watcher"].requests)) == (_CANCELLED, None, 2)
