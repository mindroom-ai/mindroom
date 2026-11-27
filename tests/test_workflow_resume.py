"""Tests for resuming a workflow run: replay of saved calls, `rerun`, changed calls, groups, values and working time.

Level: engine harness (`WorkflowStack`): the real stores, broker and engine; the test plays the script.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.approval.tool_approval import ToolApprovalDecision
from mindroom.script_runs.models import ScriptRunState, utc_timestamp
from mindroom.script_runs.reasons import PLUGIN_TOOLS_CHANGED, RUNTIME_RESTARTED
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs import nodes as nodes_module
from mindroom.workflow_runs.engine import SupersededAttemptError
from mindroom.workflow_runs.limits import WorkflowLimits
from mindroom.workflow_runs.models import DispatchHandle, NodeRequest, WorkflowNode, WorkflowNodeState, WorkflowRunState
from mindroom.workflow_runs.store import WorkflowRunStoreError
from tests.test_tool_result_transform import _broken, _registry
from tests.workflow_helpers import HeldWrite, NodeFailed, WorkflowStack, set_run, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.workflow_runs.models import NodeReceipt

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
    await stack.resume(stack.run_id, **options)  # type: ignore[arg-type]


def _node(stack: WorkflowStack, key: str) -> WorkflowNode:
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    return node


async def _group(stack: WorkflowStack, key: str, members: list[str]) -> NodeReceipt:
    """Submit a `foreach` at *key* whose items open the lanes *members*, in that order."""
    return await stack.submit(key, "group", _FOREACH, {"members": members})


@pytest.mark.asyncio
async def test_resume_reuses_saved_results_and_runs_only_what_failed(stack: WorkflowStack) -> None:
    """A finished write is not made again; the call that was refused before its tool runs as its next generation."""
    assert await stack.result("@0", "tool", _A) == "appended"
    approved = stack.resolver.decision
    stack.resolver.decision = ToolApprovalDecision(approved=False, reason="Not now.")
    with pytest.raises(NodeFailed) as denied:
        await stack.result("@1", "tool", _B)
    assert denied.value.kind == "denied"

    stack.resolver.decision = approved
    await _resume(stack)

    first = await stack.submit("@0", "tool", _A)
    assert (first.cached, first.state, first.result) == (True, "completed", "appended")
    assert stack.counting_tool.lines == ["a"]
    assert (await stack.result("@1", "tool", _B), stack.counting_tool.lines) == ("appended", ["a", "b"])
    assert (_node(stack, "@0").generation, _node(stack, "@1").generation) == (1, 2)
    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, run.error, run.finished_at) == (WorkflowRunState.RUNNING, 2, None, None)


@pytest.mark.asyncio
async def test_tool_that_failed_after_it_was_invoked_is_not_run_again_until_rerun_names_it(
    stack: WorkflowStack,
) -> None:
    """A tool that raised may have had its effect before it did: its own failure does not make its call safe to repeat."""
    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "tool", tool_args("counter", "fail_once"))
    # The script still reads why the tool failed.
    assert (failed.value.kind, str(failed.value).startswith("The counter failed once.")) == ("indeterminate", True)

    await _resume(stack)
    with pytest.raises(NodeFailed) as refused:
        await stack.result("@0", "tool", tool_args("counter", "fail_once"))
    assert (refused.value.kind, stack.counting_tool.calls) == ("indeterminate", 1)

    await _resume(stack, rerun=[f"n{_node(stack, '@0').seq}"])
    assert (await stack.result("@0", "tool", tool_args("counter", "fail_once")), stack.counting_tool.calls) == ("ok", 2)


@pytest.mark.asyncio
async def test_write_whose_result_a_check_withheld_is_not_made_again(stack: WorkflowStack) -> None:
    """The tool wrote and answered, and a result-transform hook withheld the answer: the write is not repeated.

    The reproducer of the second full review (Astra B1, Sol B3).
    """
    hooks = stack.resolver.context.hooks
    stack.resolver.context = replace(stack.resolver.context, hooks=replace(hooks, registry=_registry(_broken)))
    with pytest.raises(NodeFailed, match="withheld") as withheld:
        await stack.result("@0", "tool", _A)
    assert (withheld.value.kind, stack.counting_tool.lines) == ("indeterminate", ["a"])

    stack.resolver.context = replace(stack.resolver.context, hooks=hooks)
    await _resume(stack)
    with pytest.raises(NodeFailed):
        await stack.result("@0", "tool", _A)
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatched", [True, False])
async def test_stop_without_a_job_runtime_does_not_make_a_started_agent_safe_to_run_again(
    stack: WorkflowStack,
    dispatched: bool,
) -> None:
    """Nobody can read the agent's job while background jobs are off, so a saved job handle may have run anything.

    The reproducer of the second full review (Astra B3). The mark that the job was handed over changes nothing:
    a job starts before that mark is saved.
    """
    left = WorkflowNode(
        run_id=stack.run_id,
        key="@0",
        kind="agent",
        lane="",
        idx=0,
        digest="old",
        request={"prompt": "first"},
        attempt=1,
        state=WorkflowNodeState.WAITING,
        handle=DispatchHandle("job", "a-job-nobody-can-read", dispatched),
    )
    stack.store.put_node(left, stack.store.get_run(stack.run_id).epoch)
    assert stack.engine.agents() is None

    await stack.engine.cancel(stack.run_id, reason="Stopped while jobs were off.")

    assert _node(stack, "@0").state is WorkflowNodeState.INDETERMINATE


@pytest.mark.asyncio
async def test_report_of_an_attempt_is_taken_while_a_call_it_did_not_reach_waits_for_a_person(
    stack: WorkflowStack,
) -> None:
    """An earlier attempt left an agent waiting, and this one's script takes another path and returns.

    The run shows `waiting` for that agent, and the report of the attempt that serves it is still its report.
    The reproducer of the second full review (Astra M1): the report was refused, and the script exited without it.
    """
    left = WorkflowNode(
        run_id=stack.run_id,
        key="/0:p0@0",
        kind="agent",
        lane="/0:p0",
        idx=0,
        digest="old",
        request={"prompt": "first"},
        attempt=1,
        state=WorkflowNodeState.WAITING,
        handle=DispatchHandle("job", "a-job-that-waits", dispatched=True),
    )
    stack.store.put_node(left, stack.store.get_run(stack.run_id).epoch)
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=RUNTIME_RESTARTED)
    await stack.engine.settle(stack.store.get_run(stack.run_id), replay=True)
    await stack.engine.launch(stack.run_id)
    assert await stack.result("@1", "tool", _COUNT) == 1
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.WAITING)

    async with stack.http() as client:
        finish = await client.post("finish", json={"result": "done"})

    run = stack.store.get_run(stack.run_id)
    assert (finish.status_code, run.result, run.finished_at is not None) == (200, "done", True)


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
async def test_edited_cached_false_call_with_an_equal_result_keeps_the_lane_clean(stack: WorkflowStack) -> None:
    """The check's arguments changed and its answer did not, so what follows it saw nothing new and is reused."""
    await stack.result("@0", "tool", _TEXT, _EVERY_TIME)
    await stack.result("@1", "tool", _A)

    await _resume(stack)
    assert await stack.result("@0", "tool", tool_args("counter", "text", size=1, note="edited"), _EVERY_TIME) == "x"
    after_equal = await stack.submit("@1", "tool", _A)

    assert (_node(stack, "@0").generation, after_equal.cached, stack.counting_tool.lines) == (2, True, ["a"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["dirty", "rerun", "cached_before"])
async def test_edited_call_reruns_what_follows_unless_it_is_an_unforced_check_in_a_clean_lane(
    stack: WorkflowStack,
    cause: str,
) -> None:
    """An equal answer keeps the lane clean only for a check that ran because it always runs, not for another reason."""
    await stack.result("@0", "tool", _B)
    await stack.result("@1", "tool", _TEXT, None if cause == "cached_before" else _EVERY_TIME)
    await stack.result("@2", "tool", _A)
    first, check = _node(stack, "@0"), _node(stack, "@1")

    await _resume(stack, rerun=[f"n{check.seq}"] if cause == "rerun" else [])
    edited_first = tool_args("counter", "append", line="c") if cause == "dirty" else _B
    await stack.result("@0", "tool", edited_first)
    edited = tool_args("counter", "text", size=1, note="edited")
    assert await stack.result("@1", "tool", edited, _EVERY_TIME if cause != "cached_before" else None) == "x"

    assert await stack.result("@2", "tool", _A) == "appended"
    assert (first.generation, _node(stack, "@1").handle.same_as) == (1, None)  # type: ignore[union-attr]


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
        await nodes_module.submit_node(stack.engine, old, NodeRequest("@0", "tool", _A, {}))
    await stack.engine.launch(stack.run_id, stack.context)

    with pytest.raises(SupersededAttemptError):
        await nodes_module.submit_node(stack.engine, old, NodeRequest("@0", "tool", _A, {}))
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

    assert [node.key for node in stack.status(stack.run_id)[1]] == ["@0", "/0:a@0"]
    assert _node(stack, "/0:b@0").stale

    await _resume(stack)
    await _group(stack, "@0", ["a", "b"])
    back = await stack.submit("/0:b@0", "tool", _B)

    assert (back.cached, _node(stack, "/0:b@0").stale, stack.counting_tool.lines) == (True, False, ["a", "b"])


@pytest.mark.asyncio
async def test_resume_of_a_run_that_still_runs_is_refused(stack: WorkflowStack) -> None:
    """A run has one live attempt at a time."""
    with pytest.raises(WorkflowRunStoreError) as refusal:
        await stack.resume(stack.run_id)

    assert str(refusal.value) == f"Run {stack.run_id} is still running. Cancel it first."
    assert len(stack.scripts.launched) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["n7", "x1", "1", "n01", "n", "n1 ", "n\uff11", "n9223372036854775808"])
async def test_resume_refuses_an_unknown_call_in_rerun_by_name(stack: WorkflowStack, unknown: str) -> None:
    """Only the exact ID of a saved call names it: any other starts nothing and leaves the run and its calls as they ended."""
    await stack.result("@0", "tool", _A)
    await stack.end_attempt(ScriptRunState.FAILED)
    before = stack.status(stack.run_id)

    with pytest.raises(WorkflowRunStoreError) as refusal:
        await stack.resume(stack.run_id, rerun=["n1", unknown])

    assert str(refusal.value) == f"Run {stack.run_id} has no call {unknown}."
    assert stack.status(stack.run_id) == before
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

    assert await stack.engine.settle(seen, replay=True) == stack.store.get_run(stack.run_id)

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, len(stack.scripts.launched)) == (WorkflowRunState.STARTING, None, 1)


@pytest.mark.asyncio
async def test_two_resumes_at_once_start_one_attempt(stack: WorkflowStack) -> None:
    """The second resume finds the run starting and is refused; one process starts."""
    await stack.end_attempt(ScriptRunState.FAILED)

    outcomes = await asyncio.gather(
        stack.resume(stack.run_id),
        stack.resume(stack.run_id),
        return_exceptions=True,
    )

    assert sorted(type(outcome).__name__ for outcome in outcomes) == ["WorkflowRun", "WorkflowRunStoreError"]
    assert (stack.store.get_run(stack.run_id).attempt, len(stack.scripts.launched)) == (2, 2)


@pytest.mark.asyncio
async def test_engine_that_stops_during_a_resume_leaves_the_run_starting(stack: WorkflowStack) -> None:
    """A resume cut short by a stopping MindRoom stays accepted, as a start cut short does: the next primary starts it."""
    await stack.end_attempt(ScriptRunState.FAILED)
    stack.scripts.hold_launch = asyncio.Event()
    resuming = asyncio.create_task(stack.resume(stack.run_id))
    await stack.until(lambda: len(stack.scripts.launched) == 2)

    await stack.engine.close()

    run = await resuming
    assert (run.state, run.error, run.attempt) == (WorkflowRunState.STARTING, None, 1)


@pytest.mark.asyncio
async def test_resume_whose_script_cannot_start_fails_the_run_with_the_reason(stack: WorkflowStack) -> None:
    """A person's resume that is refused says why, as a refused start does."""
    await stack.end_attempt(ScriptRunState.FAILED)
    stack.scripts.refuse_launch = RuntimeError("No worker is available.")

    run = await stack.resume(stack.run_id)

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


_OUT_OF_TIME = 'The run used its 1 hours of working time. workflow(action="resume", run_id=...) continues it.'
# The write of the watch that charges the first ten minutes of the run's hour.
_CHARGED = {"runtime_seconds_left": 3000.0}


@pytest.mark.asyncio
async def test_time_after_the_script_ended_is_no_working_time_and_the_run_gets_its_next_attempt(
    timed: WorkflowStack,
) -> None:
    """A plugin reload stopped the script with a second of working time left, and the engine looked ten minutes later.

    The run is charged up to where its script ended, so it has that second still and goes on like any other.
    """
    set_run(timed.store, timed.run_id, runtime_seconds_left=1.0)
    attempt = await timed.scripts.end(timed.attempt.run_id, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)
    ended = utc_timestamp(datetime.fromisoformat(str(attempt.finished_at)) - timedelta(seconds=600))
    with timed.script_store._write_transaction() as connection:
        connection.execute("UPDATE script_runs SET finished_at = ?", (ended,))
    timed.now += 600
    await asyncio.sleep(0.1)

    assert await timed.maintain() == [timed.run_id]
    run = timed.store.get_run(timed.run_id)
    assert (run.state, run.attempt, run.runtime_seconds_left) == (WorkflowRunState.RUNNING, 2, 1.0)


@pytest.mark.asyncio
async def test_run_out_of_time_whose_script_had_been_stopped_for_a_reload_ends_as_out_of_time(
    timed: WorkflowStack,
) -> None:
    """The time ran out before the script was stopped, and nothing had looked at the stopped script yet.

    The run ends as any run that waits for an attempt with no time left: with the sentence that says how it continues.
    """
    set_run(timed.store, timed.run_id, runtime_seconds_left=1.0)
    timed.now += 2
    await timed.scripts.end(timed.attempt.run_id, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)

    await timed.until(lambda: timed.store.get_run(timed.run_id).state is WorkflowRunState.STARTING)
    assert await timed.maintain() == []

    run = timed.store.get_run(timed.run_id)
    assert (run.state, run.error, run.runtime_seconds_left) == (WorkflowRunState.INTERRUPTED, _OUT_OF_TIME, 0)
    assert len(timed.scripts.launched) == 1


@pytest.mark.asyncio
async def test_working_time_limit_interrupts_and_resume_continues(timed: WorkflowStack) -> None:
    """At its limit the run and its attempt stop, and a person's resume gives it the full time again."""
    await timed.result("@0", "tool", _A)
    timed.now += 1800
    await timed.until(lambda: _left(timed) == 1800)

    timed.now += 1801
    run = await timed.engine.wait(timed.run_id)

    assert (run.state, run.runtime_seconds_left) == (WorkflowRunState.INTERRUPTED, 0)
    assert run.error == 'The run used its 1 hours of working time. workflow(action="resume", run_id=...) continues it.'
    assert timed.attempt.state is ScriptRunState.CANCELLED

    await timed.resume(timed.run_id, limits=WorkflowLimits(max_runtime_hours=2))

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
async def test_call_that_waits_for_an_approval_uses_none_of_the_runs_time(timed: WorkflowStack) -> None:
    """The engine shows the run waiting itself: the time before the wait is charged, and the time of the wait is not."""
    timed.now += 600
    timed.resolver.hold_approvals = asyncio.Event()
    await timed.submit("@0", "tool", _A)
    await timed.until(lambda: timed.store.get_run(timed.run_id).state is WorkflowRunState.WAITING)
    timed.now += 80 * 3600
    await asyncio.sleep(0.05)
    timed.resolver.hold_approvals.set()

    assert await timed.result("@0", "tool", _A) == "appended"
    timed.now += 300
    await timed.until(lambda: _left(timed) == 2700)

    assert timed.store.get_run(timed.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_working_time_is_read_and_charged_on_the_event_loop_only(
    timed: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clock and the time of the last charge are the loop's: the look at the run in a thread is handed both."""
    seen, clock, record_charge = set(), timed.engine.clock, timed.engine._record_charge

    def read() -> float:
        seen.add(threading.current_thread())
        return clock()

    def write_charge(run_id: str, at: float) -> None:
        seen.add(threading.current_thread())
        record_charge(run_id, at)

    monkeypatch.setattr(timed.engine, "clock", read)
    monkeypatch.setattr(timed.engine, "_record_charge", write_charge)
    timed.now += 600
    timed.resolver.hold_approvals = asyncio.Event()
    await timed.submit("@0", "tool", _A)
    await timed.until(lambda: timed.store.get_run(timed.run_id).state is WorkflowRunState.WAITING)
    timed.resolver.hold_approvals.set()
    assert await timed.result("@0", "tool", _A) == "appended"
    await timed.until(lambda: timed.store.get_run(timed.run_id).state is WorkflowRunState.RUNNING)

    assert (seen, _left(timed)) == ({threading.current_thread()}, 3000)


@pytest.mark.asyncio
async def test_time_charged_while_the_run_starts_to_wait_stays_charged(
    timed: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's time has one writer at a time: the look that shows it waiting does not write back an older time.

    The watch charged ten minutes and its write is on its way. A look at the run that read the run before that write
    landed, and wrote after it, would give the ten minutes back.
    """
    charge = HeldWrite(timed.store, "update_run", monkeypatch, when=lambda *_args, **changes: changes == _CHARGED)
    timed.now += 600
    await charge.reached()
    waits = lambda *_args, **changes: changes.get("state") is WorkflowRunState.WAITING  # noqa: E731
    shown = HeldWrite(timed.store, "update_run", monkeypatch, when=waits)
    timed.resolver.hold_approvals = asyncio.Event()
    call = asyncio.create_task(timed.submit("@0", "tool", _A))
    # A look that does not wait for the charge reads the run meanwhile and stands before its own write.
    with suppress(TimeoutError):
        async with asyncio.timeout(1):
            await timed.until(lambda: shown.stands)

    await charge.release()
    await shown.reached()
    await shown.release()
    await call

    run = timed.store.get_run(timed.run_id)
    assert (run.state, run.runtime_seconds_left) == (WorkflowRunState.WAITING, 3000)


@pytest.mark.asyncio
async def test_working_time_is_saved_when_the_attempt_ends(
    timed: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the run used is saved at its end too, not only once a minute."""
    monkeypatch.setattr(engine_module, "_CHARGE_SECONDS", 3600)
    await timed.end_attempt(ScriptRunState.FAILED)
    await timed.resume(timed.run_id, limits=WorkflowLimits(max_runtime_hours=1))
    timed.now += 900

    await timed.end_attempt(ScriptRunState.FAILED)

    # Less the real milliseconds between the script's end and the engine's look at it.
    assert _left(timed) == pytest.approx(2700, abs=1)


@pytest.mark.asyncio
async def test_run_without_a_limit_is_never_interrupted_for_its_time(timed: WorkflowStack) -> None:
    """`max_runtime_hours: 0` stores no limit."""
    await timed.end_attempt(ScriptRunState.FAILED)
    await timed.resume(timed.run_id, limits=WorkflowLimits(max_runtime_hours=0))
    timed.now += 10_000 * 3600
    await asyncio.sleep(0.05)

    run = timed.store.get_run(timed.run_id)
    assert (run.state, run.runtime_seconds_left) == (WorkflowRunState.RUNNING, None)


async def _replayed(stack: WorkflowStack) -> None:
    """Lose the attempt to an interruption of its process and let the pass start the next one."""
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)
    assert await stack.maintain() == [stack.run_id]


@pytest.mark.asyncio
async def test_attempt_end_whose_writer_is_cancelled_at_its_commit_makes_the_next_attempt_run_no_saved_call(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass can time out while its write of an attempt's end commits: the next attempt still starts clean.

    What the engine remembers of an attempt, the lanes its calls ran in among it, goes with the write of its end and
    not with the task that waited for it, so the replay reuses both saved writes.
    """
    old = stack.attempt.run_id
    assert await stack.result("@0", "tool", _A) == "appended"
    assert await stack.result("@1", "tool", _B) == "appended"
    await stack.scripts.end(old, ScriptRunState.INTERRUPTED, reason=PLUGIN_TOOLS_CHANGED)
    ends = lambda *_args, **changes: changes.get("state") is WorkflowRunState.STARTING  # noqa: E731
    held = HeldWrite(stack.store, "update_run", monkeypatch, when=ends)
    timed_out = asyncio.create_task(stack.maintain())
    await held.reached()
    timed_out.cancel()
    with pytest.raises(asyncio.CancelledError):
        await timed_out
    await held.release()
    await stack.until(lambda: old not in stack.engine.launched)

    assert await stack.maintain() == [stack.run_id]
    assert [(await stack.submit(key, "tool", args)).cached for key, args in (("@0", _A), ("@1", _B))] == [True, True]
    assert stack.counting_tool.lines == ["a", "b"]
    assert list(stack.engine.launched) == [stack.attempt.run_id]


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
    late = asyncio.create_task(nodes_module.finish(stack.engine, old, result="the first report", error=None))
    await held.reached()
    await _replayed(stack)
    await nodes_module.finish(stack.engine, stack.attempt, result="the second report", error=None)

    await held.release()
    with pytest.raises(SupersededAttemptError):
        await late
    await stack.end_attempt(ScriptRunState.EXITED)

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, run.result) == (WorkflowRunState.COMPLETED, 2, "the second report")


@pytest.mark.asyncio
async def test_call_and_stop_of_an_attempt_that_the_run_moved_on_from_reach_no_later_attempt(
    stack: WorkflowStack,
) -> None:
    """What an attempt had under way when it was replaced acts with its own epoch: on the next attempt it does nothing.

    A call that was accepted before the takeover and decided after it is refused, and the stop of an attempt that
    ran out of time ends that attempt only.
    """
    old, asked = stack.attempt, stack.store.get_run(stack.run_id)
    await _replayed(stack)
    current = stack.store.get_run(stack.run_id)

    with pytest.raises(SupersededAttemptError):
        await nodes_module.admit(stack.engine, asked, old, NodeRequest("@0", "tool", _A, {}))
    out_of_time = {"reason": "Out of time.", "state": WorkflowRunState.INTERRUPTED, "epoch": asked.epoch}
    stopped = await stack.engine.cancel(stack.run_id, **out_of_time)

    assert stopped == stack.store.get_run(stack.run_id) == current
    assert (current.state, current.attempt) == (WorkflowRunState.RUNNING, 2)
    assert (stack.store.nodes(stack.run_id), stack.counting_tool.calls) == ([], 0)
    assert stack.attempt.state is ScriptRunState.RUNNING


@pytest.mark.asyncio
async def test_cached_node_restamps_phase_on_reuse_without_invalidating_result(stack: WorkflowStack) -> None:
    """Renaming a phase around an unchanged call reuses the cached result and restamps the stored phase."""
    assert await stack.result("@0", "tool", _A, {"phase": "Old"}) == "appended"
    assert stack.counting_tool.lines == ["a"]
    await _replayed(stack)

    reused = await stack.submit("@0", "tool", _A, {"phase": "New"})
    node = stack.store.get_node(stack.run_id, "@0")

    assert (reused.cached, reused.result, stack.counting_tool.lines) == (True, "appended", ["a"])
    assert node is not None
    assert (node.phase, node.generation) == ("New", 1)
