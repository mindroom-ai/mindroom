"""Tests for resuming a workflow run: replay of saved calls, `rerun`, changed calls, groups, values and working time.

Level: engine harness (`WorkflowStack`): the real stores, broker and engine; the test plays the script.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.script_runs.models import ScriptRunState
from mindroom.script_runs.reasons import PLUGIN_TOOLS_CHANGED, RUNTIME_RESTARTED
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.engine import SupersededAttemptError, WorkflowLimits
from mindroom.workflow_runs.models import NodeRequest, WorkflowNodeState, WorkflowRunState
from mindroom.workflow_runs.store import WorkflowRunStoreError
from tests.workflow_helpers import HeldWrite, NodeFailed, WorkflowStack, set_run, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.workflow_runs.models import NodeReceipt, WorkflowNode

_A = tool_args("counter", "append", line="a")
_B = tool_args("counter", "append", line="b")
_COUNT = tool_args("counter", "count")
_TEXT = tool_args("counter", "text", size=1)
_EVERY_TIME = {"cached": False}
_FOREACH: dict[str, object] = {"op": "foreach"}


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """The engine harness with one started run."""
    with workflow_stack(tmp_path) as stack:
        await stack.start()
        yield stack
        await stack.engine.close()


async def _resume(stack: WorkflowStack, state: ScriptRunState = ScriptRunState.FAILED, **options: object) -> None:
    """End the attempt, if it still runs, and start the next one as a person's resume does."""
    if stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING:
        await stack.end_attempt(state)
    await stack.engine.resume(stack.context, stack.run_id, **options)  # type: ignore[arg-type]


def _node(stack: WorkflowStack, key: str) -> WorkflowNode:
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    return node


async def _group(stack: WorkflowStack, key: str, members: list[str]) -> NodeReceipt:
    """Submit a `foreach` at *key* whose items open the lanes *members*, in that order."""
    return await stack.submit(key, "group", _FOREACH, {"members": members})


@pytest.mark.asyncio
async def test_resume_reuses_saved_results_and_runs_only_what_failed(stack: WorkflowStack) -> None:
    """A finished write is not made again; the call that failed runs as its next generation."""
    assert await stack.result("@0", "tool", _A) == "appended"
    with pytest.raises(NodeFailed):
        await stack.result("@1", "tool", tool_args("counter", "fail_once"))

    await _resume(stack)

    first = await stack.submit("@0", "tool", _A)
    assert (first.cached, first.state, first.result) == (True, "completed", "appended")
    assert stack.counting_tool.lines == ["a"]
    assert await stack.result("@1", "tool", tool_args("counter", "fail_once")) == "ok"
    assert (_node(stack, "@0").generation, _node(stack, "@1").generation) == (1, 2)
    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, run.error, run.finished_at) == (WorkflowRunState.RUNNING, 2, None, None)


@pytest.mark.asyncio
async def test_indeterminate_call_is_not_issued_again_until_rerun_names_it(stack: WorkflowStack) -> None:
    """A call that may have run blocks its key on every attempt, until a resume names it."""
    node = await stack.leave_indeterminate("@0", _A)

    await _resume(stack)
    with pytest.raises(NodeFailed) as error:
        await stack.result("@0", "tool", _A)

    assert error.value.kind == "indeterminate"
    assert error.value.receipt.state == "failed"
    assert f'rerun=["n{node.seq}"]' in str(error.value)
    assert stack.counting_tool.calls == 1
    assert _node(stack, "@0").attempt == 2

    await _resume(stack, rerun=[f"n{node.seq}"])
    assert await stack.result("@0", "tool", _A) == "appended"
    assert stack.counting_tool.calls == 2
    assert _node(stack, "@0").generation == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [False, True])
async def test_changed_upstream_check_does_not_resend_an_ambiguous_write(stack: WorkflowStack, edited: bool) -> None:
    """Neither a dirty lane nor a new digest runs a call whose end is unknown."""
    first = await stack.result("@0", "tool", _COUNT, _EVERY_TIME)
    await stack.leave_indeterminate("@1", _A)

    await _resume(stack)
    assert await stack.result("@0", "tool", _COUNT, _EVERY_TIME) != first
    with pytest.raises(NodeFailed) as error:
        await stack.result("@1", "tool", _B if edited else _A)

    assert error.value.kind == "indeterminate"
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_unsettled_node_of_a_dead_attempt_is_read_from_its_handle_before_anything_else(
    stack: WorkflowStack,
) -> None:
    """A node no pass settled is reconciled at its next request: the saved receipt is copied, the tool is not called."""
    assert await stack.result("@0", "tool", _A) == "appended"
    await _resume(stack)
    # As a primary leaves it that stopped after the receipt and before the node: still running, under the old attempt.
    unsettled = replace(_node(stack, "@0"), state=WorkflowNodeState.RUNNING, result=None, generation=2)
    stack.store.put_node(unsettled, stack.store.get_run(stack.run_id).epoch)

    receipt = await stack.submit("@0", "tool", _A)

    assert (receipt.state, receipt.result, receipt.cached) == ("completed", "appended", True)
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_cached_false_runs_again_and_an_equal_result_keeps_the_lane_clean(stack: WorkflowStack) -> None:
    """A check that runs on every resume dirties what follows it only when it answers something else."""
    await stack.result("@0", "tool", _TEXT, _EVERY_TIME)
    await stack.result("@1", "tool", _A)

    await _resume(stack)
    await stack.result("@0", "tool", _TEXT, _EVERY_TIME)
    after_equal = await stack.submit("@1", "tool", _A)

    assert (_node(stack, "@0").generation, after_equal.cached, stack.counting_tool.lines) == (2, True, ["a"])


@pytest.mark.asyncio
async def test_cached_false_with_another_result_reruns_what_follows(stack: WorkflowStack) -> None:
    """The check answered something else, so the call after it may see something else: it runs again."""
    await stack.result("@0", "tool", _COUNT, _EVERY_TIME)
    await stack.result("@1", "tool", _A)

    await _resume(stack)
    await stack.result("@0", "tool", _COUNT, _EVERY_TIME)
    assert await stack.result("@1", "tool", _A) == "appended"

    assert (_node(stack, "@1").generation, stack.counting_tool.lines) == (2, ["a", "a"])


@pytest.mark.asyncio
async def test_cached_false_with_an_equal_result_over_the_inline_limit_keeps_the_lane_clean(
    stack: WorkflowStack,
) -> None:
    """A result that lives in a file is compared whole, not by the part of it its row keeps."""
    large = tool_args("counter", "text", size=8 * 1024 * 1024 + 1)
    await stack.result("@0", "tool", large, _EVERY_TIME)
    await stack.result("@1", "tool", _A)

    await _resume(stack)
    await stack.result("@0", "tool", large, _EVERY_TIME)
    after_equal = await stack.submit("@1", "tool", _A)

    assert _node(stack, "@0").result_file is not None
    assert (_node(stack, "@0").generation, after_equal.cached, stack.counting_tool.lines) == (2, True, ["a"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("first", "second"), [(1, True), (1, 1.0), ({"n": 0}, {"n": False})])
async def test_cached_false_with_the_same_value_of_another_json_type_reruns_what_follows(
    stack: WorkflowStack,
    first: object,
    second: object,
) -> None:
    """Python calls 1, 1.0 and True equal; the script may not, so the saved JSON decides."""
    assert first == second
    stack.counting_tool.answers = [first, second]
    answer = tool_args("counter", "answer")
    await stack.result("@0", "tool", answer, _EVERY_TIME)
    await stack.result("@1", "tool", _A)

    await _resume(stack)
    await stack.result("@0", "tool", answer, _EVERY_TIME)
    assert await stack.result("@1", "tool", _A) == "appended"

    assert stack.counting_tool.lines == ["a", "a"]


@pytest.mark.asyncio
async def test_changed_call_reruns_itself_and_the_calls_after_it_in_its_lane(stack: WorkflowStack) -> None:
    """Calls before the first changed call are reused; that call and the ones after it run again."""
    for key in ("@0", "@1", "@2"):
        await stack.result(key, "tool", _A)

    await _resume(stack)
    receipts = [
        await stack.submit("@0", "tool", _A),
        await stack.submit("@1", "tool", _B),
        await stack.submit("@2", "tool", _A),
    ]
    await stack.until(lambda: len(stack.counting_tool.lines) == 5)

    assert [receipt.cached for receipt in receipts] == [True, False, False]
    assert sorted(stack.counting_tool.lines) == ["a", "a", "a", "a", "b"]
    assert [_node(stack, key).generation for key in ("@0", "@1", "@2")] == [1, 2, 2]


@pytest.mark.asyncio
async def test_changed_call_without_a_script_edit_logs_the_notice(stack: WorkflowStack) -> None:
    """A call that changed although the script did not probably holds the time or a random value."""
    await stack.result("@0", "tool", _A)
    seq = _node(stack, "@0").seq

    await _resume(stack)
    await stack.result("@0", "tool", _B)

    assert stack.store.log(stack.run_id) == [
        f"Call n{seq} changed between attempts without a script edit. It probably contains the time or a random "
        "value; use now() or random() from mindroom.workflow.",
    ]


@pytest.mark.asyncio
async def test_changed_call_after_a_script_edit_logs_nothing(stack: WorkflowStack) -> None:
    """An edited script explains a changed call, and the run keeps the digest of the script it ran before."""
    before = stack.store.get_run(stack.run_id).source_digest
    await stack.result("@0", "tool", _A)

    await _resume(stack, source="def main(args):\n    return 2\n")
    await stack.result("@0", "tool", _B)

    run = stack.store.get_run(stack.run_id)
    assert stack.store.log(stack.run_id) == []
    assert run.previous_source_digest == before
    assert run.source_digest == hashlib.sha256(run.source.encode()).hexdigest() != before
    assert stack.scripts.launched[-1].source_digest == run.source_digest


@pytest.mark.asyncio
async def test_old_attempt_is_refused_after_resume(stack: WorkflowStack) -> None:
    """Only the run's current attempt is served, so a leftover process of the earlier one cannot act."""
    old = stack.attempt
    await stack.end_attempt(ScriptRunState.FAILED)

    await stack.engine.reopen(stack.run_id, stack.context)
    with pytest.raises(SupersededAttemptError):
        # The run is ready for its next attempt and names none: the old one gets no call in.
        await stack.engine.submit_node(old, NodeRequest("@0", "tool", _A, {}))
    await stack.engine.launch(stack.run_id, stack.context)

    with pytest.raises(SupersededAttemptError):
        await stack.engine.submit_node(old, NodeRequest("@0", "tool", _A, {}))
    assert stack.counting_tool.calls == 0


@pytest.mark.asyncio
async def test_now_and_random_replay_their_values(stack: WorkflowStack) -> None:
    """The gateway makes the value once; a resume returns the saved one."""
    now = await stack.result("@0", "value", {"op": "now"})
    number = await stack.result("@1", "value", {"op": "random"})
    assert isinstance(now, str)
    assert isinstance(number, float)
    assert datetime.fromisoformat(now).tzinfo is not None
    assert 0 <= number < 1

    await _resume(stack)

    assert await stack.result("@0", "value", {"op": "now"}) == now
    assert await stack.result("@1", "value", {"op": "random"}) == number
    with pytest.raises(WorkflowRunStoreError, match="value"):
        await stack.submit("@2", "value", {"op": "dice"})


@pytest.mark.asyncio
async def test_changed_item_reruns_the_check_after_the_join(stack: WorkflowStack) -> None:
    """One item of a fan-out changed: its sibling is reused, and the call after the fan-out runs again."""
    await _group(stack, "@0", ["a", "b"])
    await stack.result("/0:a@0", "tool", _A)
    await stack.result("/0:b@0", "tool", _B)
    await stack.result("@1", "tool", _TEXT)
    calls = stack.counting_tool.calls

    await _resume(stack)
    group = await _group(stack, "@0", ["a", "b"])
    sibling = await stack.submit("/0:b@0", "tool", _B)
    await stack.result("/0:a@0", "tool", tool_args("counter", "append", line="c"))
    check = await stack.submit("@1", "tool", _TEXT)

    assert (group.cached, sibling.cached, check.cached) == (True, True, False)
    await stack.until(lambda: stack.counting_tool.calls == calls + 2)
    assert _node(stack, "@1").generation == 2


@pytest.mark.asyncio
async def test_membership_only_change_reruns_the_check_after_the_join(stack: WorkflowStack) -> None:
    """An item was removed and nothing else ran: the call after the fan-out still runs again."""
    await _group(stack, "@0", ["a", "b"])
    await stack.result("/0:a@0", "tool", _A)
    await stack.result("/0:b@0", "tool", _B)
    await stack.result("@1", "tool", _TEXT)

    await _resume(stack)
    await _group(stack, "@0", ["a"])
    kept = await stack.submit("/0:a@0", "tool", _A)
    check = await stack.submit("@1", "tool", _TEXT)

    assert (kept.cached, check.cached) == (True, False)
    assert _node(stack, "@0").members == ("a",)


@pytest.mark.asyncio
async def test_reordered_and_grown_foreach_reuses_finished_items(stack: WorkflowStack) -> None:
    """Lanes are keyed by item, so moving items and adding one runs only the new one."""
    await _group(stack, "@0", ["a", "b"])
    await stack.result("/0:a@0", "tool", _A)
    await stack.result("/0:b@0", "tool", _B)

    await _resume(stack)
    await _group(stack, "@0", ["c", "b", "a"])
    reused = [await stack.submit("/0:b@0", "tool", _B), await stack.submit("/0:a@0", "tool", _A)]
    await stack.result("/0:c@0", "tool", tool_args("counter", "append", line="c"))

    assert [receipt.cached for receipt in reused] == [True, True]
    assert stack.counting_tool.lines == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_removed_item_is_marked_stale_when_the_attempt_completes(stack: WorkflowStack) -> None:
    """A call the completed attempt did not reach leaves the run's current view, and comes back when reached again."""
    await _group(stack, "@0", ["a", "b"])
    await stack.result("/0:a@0", "tool", _A)
    await stack.result("/0:b@0", "tool", _B)

    await _resume(stack)
    await _group(stack, "@0", ["a"])
    await stack.submit("/0:a@0", "tool", _A)
    await stack.finish_attempt(result="done")

    assert [node.key for node in stack.engine.status(stack.run_id)[1]] == ["@0", "/0:a@0"]
    assert _node(stack, "/0:b@0").stale

    await _resume(stack)
    await _group(stack, "@0", ["a", "b"])
    back = await stack.submit("/0:b@0", "tool", _B)

    assert (back.cached, _node(stack, "/0:b@0").stale, stack.counting_tool.lines) == (True, False, ["a", "b"])


@pytest.mark.asyncio
async def test_resume_of_a_run_that_still_runs_is_refused(stack: WorkflowStack) -> None:
    """A run has one live attempt at a time."""
    with pytest.raises(WorkflowRunStoreError) as refusal:
        await stack.engine.resume(stack.context, stack.run_id)

    assert str(refusal.value) == f"Run {stack.run_id} is still running. Cancel it first."
    assert len(stack.scripts.launched) == 1


@pytest.mark.asyncio
async def test_resume_refuses_an_unknown_call_in_rerun_by_name(stack: WorkflowStack) -> None:
    """A wrong ID starts nothing and leaves the run as it ended."""
    await stack.result("@0", "tool", _A)
    await stack.end_attempt(ScriptRunState.FAILED)

    with pytest.raises(WorkflowRunStoreError) as refusal:
        await stack.engine.resume(stack.context, stack.run_id, rerun=["n1", "n7"])

    assert str(refusal.value) == f"Run {stack.run_id} has no call n7."
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.FAILED
    assert len(stack.scripts.launched) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ended", "reason"),
    [(ScriptRunState.EXITED, None), (ScriptRunState.INTERRUPTED, RUNTIME_RESTARTED)],
    ids=["exited", "interrupted"],
)
async def test_look_at_the_old_attempt_after_a_resume_took_the_run_leaves_the_resume(
    stack: WorkflowStack,
    ended: ScriptRunState,
    reason: str | None,
) -> None:
    """A pass that read the run before it ended and was resumed judges the old attempt, which the run names no more.

    It neither ends the run as the old attempt ended nor starts the attempt that the resume is about to start.
    """
    seen = stack.store.get_run(stack.run_id)
    await stack.end_attempt(ended, reason)
    await stack.engine.reopen(stack.run_id, stack.context)

    assert not await stack.engine._reconcile_run(seen, stack.engine.owner_access)

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, len(stack.scripts.launched)) == (WorkflowRunState.STARTING, None, 1)


@pytest.mark.asyncio
async def test_two_resumes_at_once_start_one_attempt(stack: WorkflowStack) -> None:
    """The second resume finds the run starting and is refused; one process starts."""
    await stack.end_attempt(ScriptRunState.FAILED)

    outcomes = await asyncio.gather(
        stack.engine.resume(stack.context, stack.run_id),
        stack.engine.resume(stack.context, stack.run_id),
        return_exceptions=True,
    )

    assert sorted(type(outcome).__name__ for outcome in outcomes) == ["WorkflowRun", "WorkflowRunStoreError"]
    assert (stack.store.get_run(stack.run_id).attempt, len(stack.scripts.launched)) == (2, 2)


@pytest.mark.asyncio
async def test_engine_that_stops_during_a_resume_leaves_the_run_starting(stack: WorkflowStack) -> None:
    """A resume cut short by a stopping MindRoom stays accepted, as a start cut short does: the next primary starts it."""
    await stack.end_attempt(ScriptRunState.FAILED)
    stack.scripts.hold_launch = asyncio.Event()
    resuming = asyncio.create_task(stack.engine.resume(stack.context, stack.run_id))
    await stack.until(lambda: len(stack.scripts.launched) == 2)

    await stack.engine.close()

    run = await resuming
    assert (run.state, run.error, run.attempt) == (WorkflowRunState.STARTING, None, 1)


@pytest.mark.asyncio
async def test_resume_whose_script_cannot_start_fails_the_run_with_the_reason(stack: WorkflowStack) -> None:
    """A person's resume that is refused says why, as a refused start does."""
    await stack.end_attempt(ScriptRunState.FAILED)
    stack.scripts.refuse_launch = RuntimeError("No worker is available.")

    run = await stack.engine.resume(stack.context, stack.run_id)

    assert (run.state, run.error, run.attempt) == (WorkflowRunState.FAILED, "No worker is available.", 1)


@pytest_asyncio.fixture
async def timed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with a run that may work for one hour, and an engine that charges working time every 10 ms."""
    monkeypatch.setattr(engine_module, "_CHARGE_SECONDS", 0.01)
    with workflow_stack(tmp_path) as stack:
        stack.limits = WorkflowLimits(max_runtime_hours=1)
        await stack.start(runtime_seconds_left=3600.0)
        yield stack
        await stack.engine.close()


def _left(stack: WorkflowStack) -> float | None:
    return stack.store.get_run(stack.run_id).runtime_seconds_left


@pytest.mark.asyncio
async def test_working_time_limit_interrupts_and_resume_continues(timed: WorkflowStack) -> None:
    """At its limit the run and its attempt stop, and a person's resume gives it the full time again."""
    await timed.result("@0", "tool", _A)
    timed.now += 1800
    await timed.until(lambda: _left(timed) == 1800)

    timed.now += 1801
    run = await timed.engine.wait(timed.run_id)

    assert (run.state, run.runtime_seconds_left) == (WorkflowRunState.INTERRUPTED, 0)
    assert run.error == "The run used its 1 hours of working time. resume_workflow continues it."
    assert timed.attempt.state is ScriptRunState.CANCELLED

    await timed.engine.resume(timed.context, timed.run_id, limits=WorkflowLimits(max_runtime_hours=2))

    assert (_left(timed), (await timed.submit("@0", "tool", _A)).cached) == (7200, True)
    assert timed.store.get_run(timed.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_working_time_does_not_count_while_waiting(timed: WorkflowStack) -> None:
    """Eighty hours of waiting for a person use none of the run's hour."""
    set_run(timed.store, timed.run_id, state=WorkflowRunState.WAITING)
    await asyncio.sleep(0.05)
    timed.now += 80 * 3600
    await asyncio.sleep(0.05)
    set_run(timed.store, timed.run_id, state=WorkflowRunState.RUNNING)
    await asyncio.sleep(0.05)
    timed.now += 600
    await timed.until(lambda: _left(timed) == 3000)

    assert timed.store.get_run(timed.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_working_time_is_saved_when_the_attempt_ends(
    timed: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the run used is saved at its end too, not only once a minute."""
    monkeypatch.setattr(engine_module, "_CHARGE_SECONDS", 3600)
    await timed.end_attempt(ScriptRunState.FAILED)
    await timed.engine.resume(timed.context, timed.run_id, limits=WorkflowLimits(max_runtime_hours=1))
    timed.now += 900

    await timed.end_attempt(ScriptRunState.FAILED)

    assert _left(timed) == 2700


@pytest.mark.asyncio
async def test_run_without_a_limit_is_never_interrupted_for_its_time(timed: WorkflowStack) -> None:
    """`max_runtime_hours: 0` stores no limit."""
    await timed.end_attempt(ScriptRunState.FAILED)
    await timed.engine.resume(timed.context, timed.run_id, limits=WorkflowLimits(max_runtime_hours=0))
    timed.now += 10_000 * 3600
    await asyncio.sleep(0.05)

    run = timed.store.get_run(timed.run_id)
    assert (run.state, run.runtime_seconds_left) == (WorkflowRunState.RUNNING, None)


async def _replayed(stack: WorkflowStack) -> None:
    """Lose the attempt to an interruption of its process and let the pass start the next one."""
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)
    assert await stack.maintain() == [stack.run_id]


@pytest.mark.asyncio
async def test_settle_held_across_two_takeovers_marks_nothing_of_the_later_attempts(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call's task that outlives its attempt writes with that attempt's epoch, so its late write changes nothing."""
    answer = tool_args("counter", "answer")
    stack.counting_tool.answers = ["old", "new", "new", "new"]
    await stack.result("@0", "tool", answer, _EVERY_TIME)
    await stack.result("@1", "tool", _A)
    await _replayed(stack)
    held = HeldWrite(stack.store, "settle_node", monkeypatch)
    await stack.submit("@0", "tool", answer, _EVERY_TIME)
    await held.reached()

    # The attempt dies with its write held; the look at its receipt finds the changed answer and marks the append.
    await _replayed(stack)
    assert await stack.result("@0", "tool", answer, _EVERY_TIME) == "new"
    assert await stack.result("@1", "tool", _A) == "appended"
    before = stack.store.nodes(stack.run_id)
    await held.release()

    assert stack.store.nodes(stack.run_id) == before
    await _replayed(stack)
    assert await stack.result("@0", "tool", answer, _EVERY_TIME) == "new"
    assert (await stack.submit("@1", "tool", _A)).cached
    assert stack.counting_tool.lines == ["a", "a"]


@pytest.mark.asyncio
async def test_report_held_across_a_takeover_is_refused_and_the_next_attempts_report_stays(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report is written with the epoch its attempt began with: the attempt that follows keeps its own."""
    old = stack.attempt
    held = HeldWrite(stack.store, "update_run", monkeypatch, when=lambda *_args, **changes: "result" in changes)
    late = asyncio.create_task(stack.engine.finish(old, result="the first report", error=None))
    await held.reached()
    await _replayed(stack)
    await stack.engine.finish(stack.attempt, result="the second report", error=None)

    await held.release()
    with pytest.raises(SupersededAttemptError):
        await late
    await stack.end_attempt(ScriptRunState.EXITED)

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, run.result) == (WorkflowRunState.COMPLETED, 2, "the second report")
