"""Tests for an agent of a workflow run that waits for an approval when MindRoom stops.

Level: engine harness (`WorkflowStack`) with the real job runtime, real delegation and the real approval journal; each
agent's model is scripted. A stop is orderly (`restart()`) or a kill (`kill()`), which keeps only what was saved.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.approval import manager as approval_manager
from mindroom.delegation import continuation, recovery
from mindroom.delegation.background import delegation_child
from mindroom.delegation.recovery import read_child_run
from mindroom.delegation.state import RESTART_INTERRUPTION_REASON, child_card_id
from mindroom.event_journal import ApprovalDecisionMetadata, CallKey, tool_job_run_id
from mindroom.event_journal.opening import event_journal_sqlite_path
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.models import WorkflowNodeState
from tests.fakes.scripted_model import Await, answer, calls, tool_call, usage
from tests.tool_job_helpers import (
    _JOB_CARD_PRINCIPAL,
    FakeApprovalTransport,
    job_card_decision,
    reserve_job_card,
    wait_for_status,
)
from tests.workflow_helpers import NodeFailed, WorkflowStack, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.approval.tool_approval import BackgroundScriptToolOrigin
    from mindroom.event_journal import BackgroundApprovalDecision
    from mindroom.workflow_runs.models import WorkflowNode

_PROMPT: dict[str, object] = {"prompt": "Write a, then b and c"}


@dataclass
class _Cards:
    """Stand in for the approval manager over the stack's real journal: a card is one saved call, and so is its decision.

    What a person decided is read from the journal, so it survives a restart as it does in production; `asked` holds
    every request for a card, also one for a call whose card is open already, which reserves nothing more.
    """

    stack: WorkflowStack
    asked: list[str] = field(default_factory=list)
    posted: list[str] = field(default_factory=list)
    """The cards in the room, by the child's call: a call that has its card gets no second one."""
    transport: FakeApprovalTransport = field(default_factory=FakeApprovalTransport)

    async def request_background_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        tool_name: str,
        **_options: object,
    ) -> BackgroundApprovalDecision:
        await reserve_job_card(self.stack.jobs.journal, origin.run_id, origin.call_id, tool_name=tool_name)
        call_id = origin.call_id.rpartition(":")[2]
        self.asked.append(call_id)
        if call_id not in self.posted:
            self.posted.append(call_id)
        while (decision := await job_card_decision(self.stack.jobs.journal, origin.run_id, origin.call_id)) is None:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        return decision

    def _call(self, call_id: str) -> tuple[str, str]:
        job = self.stack.node_job("@0")
        return tool_job_run_id(job.job_id), child_card_id(delegation_child(job).delegation_id, call_id)

    async def decide(self, call_id: str, status: str = "approved") -> None:
        """Answer the open card of the child's call *call_id*, as the requester's click does."""
        approvals = self.stack.jobs.journal.principal(_JOB_CARD_PRINCIPAL).approvals
        recorded = await approvals.resolve_approval_call(
            CallKey.detached(*self._call(call_id)),
            requested_status=status,  # type: ignore[arg-type]
            reason=None,
            metadata=ApprovalDecisionMetadata(resolved_by=self.stack.context.requester_id),
        )
        assert recorded.recorded

    async def settle_pending_background_approvals(self, run_id: str, *, reason: str) -> int:
        """Deny the cards of one job that are still open, as the approval manager does."""
        approvals = self.stack.jobs.journal.principal(_JOB_CARD_PRINCIPAL).approvals
        return len(await approvals.deny_pending_detached_calls(owner_id=run_id, reason=reason))

    def saved(self) -> dict[str, tuple[int, str | None]]:
        """Return every card the journal holds, by the child's call: when it expires and how it was decided."""
        path = event_journal_sqlite_path(self.stack.context.runtime_paths.storage_root)
        with closing(sqlite3.connect(path)) as journal:
            rows = journal.execute("SELECT tool_call_id, expires_at_ns, decision FROM approval_calls").fetchall()
        return {str(call_id).rpartition(":")[2]: (expires, decision) for call_id, expires, decision in rows}

    async def decision(self, call_id: str) -> str | None:
        """Return what the journal holds for the card of the child's call *call_id*; None while it is open."""
        decided = await job_card_decision(self.stack.jobs.journal, *self._call(call_id))
        return None if decided is None else decided.status


@pytest_asyncio.fixture
async def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with the real job runtime and scripted agents; `append` asks for an approval, on `stack.cards`."""
    with workflow_stack(tmp_path, extra_tools=("workflow",)) as stack:
        monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
        stack.continues = lambda job: job.kind == "delegation"
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()
        stack.cards = _Cards(stack)  # type: ignore[attr-defined]
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: stack.cards)  # type: ignore[attr-defined]
        yield stack
        await stack.close()


def _node(stack: WorkflowStack, key: str = "@0") -> WorkflowNode:
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    return node


async def _paused_run(stack: WorkflowStack) -> RunRecord | None:
    """Return the run the agent's job names, as the child's own storage holds it."""
    child = delegation_child(stack.node_job("@0"))
    return await read_child_run(child, stack.context.config, stack.context.runtime_paths)


async def _waiting_on_two_cards(stack: WorkflowStack, *, decided: bool) -> _Cards:
    """Let the agent write `a` after its approval, then ask for `b` and `c` at once; with *decided*, approve `b`."""
    cards: _Cards = stack.cards  # type: ignore[attr-defined]
    stack.child_script(
        calls(tool_call("append", "c1", line="a")),
        calls(tool_call("append", "c2", line="b"), tool_call("append", "c3", line="c")),
        answer("sent"),
    )
    await stack.submit("@0", "agent", _PROMPT)
    await stack.until(lambda: cards.asked == ["c1"])
    await cards.decide("c1")
    await stack.until(lambda: sorted(cards.asked) == ["c1", "c2", "c3"])
    await stack.until(lambda: _node(stack).state is WorkflowNodeState.WAITING)
    if decided:
        await cards.decide("c2")
    return cards


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["graceful", "kill"])
async def test_paused_child_run_survives_a_graceful_restart_and_a_kill(gated: WorkflowStack, stop: str) -> None:
    """Step 0: after either stop the child's run is still paused on its calls, and the cards keep what was decided."""
    cards = await _waiting_on_two_cards(gated, decided=True)

    await (gated.restart() if stop == "graceful" else gated.kill())

    run = await _paused_run(gated)
    assert run is not None
    assert run.status is RunStatus.paused
    assert [tool.tool_call_id for tool in run.tools or () if tool.is_paused] == ["c2", "c3"]
    assert (await cards.decision("c2"), await cards.decision("c3")) == ("approved", None)
    assert gated.counting_tool.lines == ["a"]


async def _stopped(stack: WorkflowStack, stop: str) -> None:
    """Stop the primary in the way *stop* names and run the next one's passes: the agents' jobs, then the runs."""
    await (stack.restart() if stop == "graceful" else stack.kill())
    await stack.continue_agents()
    # The job that goes on with its paused turn never reads as working: a stop right now finds it waiting again.
    assert stack.node_job("@0").status == "awaiting_approval"
    assert await stack.recover() == [stack.run_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["graceful", "kill"])
@pytest.mark.parametrize("moment", ["before", "after"])
async def test_crash_on_either_side_of_an_approval_decision_does_not_repeat_a_write(
    gated: WorkflowStack,
    stop: str,
    moment: str,
) -> None:
    """The agent wrote `a` and waits on two cards; the stop comes before or after the first was approved.

    The next primary resumes the same paused turn: no call gets a second card, a decision that was given stands, the
    open card keeps its expiry, and each line is written once.
    """
    cards = await _waiting_on_two_cards(gated, decided=moment == "after")
    before, job_id, requests = cards.saved(), gated.node_job("@0").job_id, len(gated.models["watcher"].requests)

    await _stopped(gated, stop)
    # The stopped turn's own model goes on: the agent is not sent its task again.
    gated.child_script(answer("sent"))
    waiting = asyncio.ensure_future(gated.result("@0", "agent", _PROMPT))
    await gated.until(lambda: _node(gated).state is WorkflowNodeState.WAITING)

    assert (cards.posted, cards.saved(), gated.counting_tool.lines) == (["c1", "c2", "c3"], before, ["a"])
    if moment == "before":
        await cards.decide("c2")
    await cards.decide("c3")

    assert await waiting == "sent"
    node, job = _node(gated), gated.node_job("@0")
    assert (node.generation, job.job_id, job.status) == (1, job_id, "completed")
    # The job no longer says it waits for approvals, so a later wait of another kind is not taken for one.
    assert job.adapter.get("wait") is None
    assert (cards.posted, sorted(gated.counting_tool.lines)) == (["c1", "c2", "c3"], ["a", "b", "c"])
    # One more request: the answer after the approved calls. The task was not sent a second time.
    assert (len(gated.models["watcher"].requests), requests) == (1, 2)


@pytest.mark.asyncio
async def test_agent_that_waits_through_two_stops_is_resumed_once_and_its_usage_is_counted_once(
    gated: WorkflowStack,
) -> None:
    """A wait outlasts any number of restarts, which are none that cut the agent short: the same turn, the same cards."""
    cards: _Cards = gated.cards  # type: ignore[attr-defined]
    gated.child_script(calls(tool_call("append", "c1", line="a"), metrics=usage(7, 2)), answer("sent", usage(5, 3)))
    await gated.submit("@0", "agent", _PROMPT)
    await gated.until(lambda: _node(gated).state is WorkflowNodeState.WAITING)

    await _stopped(gated, "graceful")
    await gated.submit("@0", "agent", _PROMPT)
    await gated.until(lambda: _node(gated).state is WorkflowNodeState.WAITING)
    await _stopped(gated, "kill")
    await _stopped(gated, "graceful")
    gated.child_script(answer("sent", usage(5, 3)))
    waiting = asyncio.ensure_future(gated.result("@0", "agent", _PROMPT))
    await gated.until(lambda: _node(gated).state is WorkflowNodeState.WAITING)
    assert (cards.posted, gated.counting_tool.lines) == (["c1"], [])
    await cards.decide("c1")

    assert await waiting == "sent"
    totals = gated.store.usage_totals(gated.run_id)
    assert (totals["input_tokens"], totals["output_tokens"], totals["tool_calls"]) == (12, 5, 1)
    assert (gated.counting_tool.lines, _node(gated).generation) == (["a"], 1)


@pytest.mark.asyncio
async def test_stop_of_the_run_ends_an_agent_that_still_waits_to_be_resumed(gated: WorkflowStack) -> None:
    """A run that is cancelled before its waiting agent was resumed ends that agent: its open card is closed."""
    cards = await _waiting_on_two_cards(gated, decided=False)
    await gated.kill()
    job_id = gated.node_job("@0").job_id
    assert [job.job_id for job in gated.jobs.awaiting_continuation()] == [job_id]

    await gated.engine.cancel(gated.run_id, reason="Stopped.")
    await wait_for_status(gated.jobs, job_id, "cancelled")

    saved, run = cards.saved(), await _paused_run(gated)
    assert (saved["c2"][1], saved["c3"][1], gated.counting_tool.lines) == ("denied", "denied", ["a"])
    # Only a stop of MindRoom leaves a turn paused: a cancelled agent's turn is over. It had appended its first
    # line, so no resume runs it again without `rerun`.
    ended = (_node(gated).state, run is not None and run.status)
    assert ended == (WorkflowNodeState.INDETERMINATE, RunStatus.cancelled)


@pytest.mark.asyncio
async def test_agent_that_waits_for_a_person_in_another_way_ends_at_the_restart_as_before(gated: WorkflowStack) -> None:
    """Only a wait for approvals is one to go on from: a job that waits and does not say for what ends, as it did."""
    await _waiting_on_two_cards(gated, decided=False)
    job = gated.node_job("@0")
    # Arranged: what a call that waits for a person outside the approval cards leaves, as a secret request does.
    await gated.jobs.save_adapter(job.job_id, wait=None)

    await gated.kill()

    assert (gated.node_job("@0").status, gated.jobs.awaiting_continuation()) == ("interrupted", ())
    assert await gated.recover() == [gated.run_id]
    with pytest.raises(NodeFailed) as stopped:
        await gated.result("@0", "agent", _PROMPT)
    assert (stopped.value.kind, gated.counting_tool.lines) == ("indeterminate", ["a"])


@pytest.mark.asyncio
async def test_unloadable_paused_run_continues_with_a_follow_up_turn_and_one_new_card(
    gated: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused turn that cannot be resumed had not run its gated call: the agent goes on and asks for it again."""
    cards: _Cards = gated.cards  # type: ignore[attr-defined]
    gated.child_script(calls(tool_call("append", "c1", line="a")), calls(tool_call("append", "c2", line="b")))
    await gated.submit("@0", "agent", _PROMPT)
    await gated.until(lambda: cards.asked == ["c1"])
    await cards.decide("c1")
    await gated.until(lambda: cards.asked == ["c1", "c2"])
    await gated.until(lambda: _node(gated).state is WorkflowNodeState.WAITING)
    job_id = gated.node_job("@0").job_id

    async def unloadable(*_args: object, **_options: object) -> None:
        return None

    monkeypatch.setattr(recovery, "paused_for_approval", unloadable)
    await gated.kill()
    thinking = asyncio.Event()
    model = gated.child_script([Await(thinking.wait), *calls(tool_call("append", "c3", line="b"))], answer("sent"))
    await gated.continue_agents()
    assert await gated.recover() == [gated.run_id]
    waiting = asyncio.ensure_future(gated.result("@0", "agent", _PROMPT))
    await gated.until(lambda: len(model.requests) == 1)
    # The agent works again, in a new turn: its job and its call no longer read as waiting for a person.
    await gated.until(lambda: _node(gated).state is WorkflowNodeState.RUNNING)
    assert gated.node_job("@0").status == "running"
    thinking.set()
    await gated.until(lambda: cards.posted == ["c1", "c2", "c3"])

    # The card of the turn that is over is closed, and the agent was told what that turn had finished.
    saved = cards.saved()
    assert (saved["c2"][1], saved["c3"][1], gated.counting_tool.lines) == ("denied", None, ["a"])
    sent = str([message.content for message in model.requests[0].messages if message.role == "user"][-1])
    assert ("Write a, then b and c" in sent, "line=a" in sent, "line=b" in sent) == (True, True, False), sent
    await cards.decide("c3")

    assert await waiting == "sent"
    assert (_node(gated).generation, gated.node_job("@0").job_id, gated.counting_tool.lines) == (1, job_id, ["a", "b"])


@pytest.mark.asyncio
async def test_child_with_a_started_unrecorded_call_is_indeterminate(
    gated: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that cannot be resumed and holds a call that started and never ended is not continued, nor run again."""
    cards = await _waiting_on_two_cards(gated, decided=False)

    async def unloadable(*_args: object, **_options: object) -> None:
        return None

    monkeypatch.setattr(recovery, "paused_for_approval", unloadable)
    await gated.kill()
    read = continuation.read_child_run

    async def with_open_call(*args: object, **options: object) -> object:
        run = await read(*args, **options)  # type: ignore[arg-type]
        assert run is not None
        return replace(run, tools=[*(run.tools or ()), ToolExecution(tool_call_id="c9", tool_name="append")])

    monkeypatch.setattr(continuation, "read_child_run", with_open_call)
    model = gated.child_script(answer("must not be asked"))
    await gated.continue_agents()
    assert await gated.recover() == [gated.run_id]

    with pytest.raises(NodeFailed) as stopped:
        await gated.result("@0", "agent", _PROMPT)
    assert (stopped.value.kind, _node(gated).state) == ("indeterminate", WorkflowNodeState.INDETERMINATE)
    assert (model.requests, gated.counting_tool.lines, cards.posted) == ([], ["a"], ["c1", "c2", "c3"])


@pytest.mark.asyncio
async def test_ordinary_subagent_approval_wait_at_a_restart_is_unchanged(gated: WorkflowStack) -> None:
    """A subagent job that is no call of a run ends at the restart as before: its card is denied, its turn not resumed."""
    cards = await _waiting_on_two_cards(gated, decided=False)
    job = gated.node_job("@0")
    # Arranged: the same job without what marks it as a run's call, saved as a subagent's own job is.
    job.adapter["child"] = {**job.adapter["child"], "workflow_run_id": None, "workflow_node_key": None}
    await gated.jobs.save_adapter(job.job_id)

    await gated.kill()

    [ended] = [saved for saved in gated.jobs.retained_jobs() if saved.job_id == job.job_id]
    run = await read_child_run(delegation_child(ended), gated.context.config, gated.context.runtime_paths)
    assert run is not None
    assert (ended.status, run.status, gated.jobs.awaiting_continuation()) == ("interrupted", RunStatus.error, ())
    assert (await cards.decision("c2"), await cards.decision("c3")) == ("denied", "denied")


@pytest.mark.parametrize(("of_run", "listed"), [(True, False), (False, True)])
def test_only_a_workflow_agents_account_leaves_out_the_calls_that_waited(of_run: bool, listed: bool) -> None:
    """An ordinary subagent's account of its cut-short run names every call, as it did before workflows."""
    waited = ToolExecution(tool_call_id="c2", tool_name="append", result=RESTART_INTERRUPTION_REASON, is_error=True)
    done = ToolExecution(tool_call_id="c1", tool_name="text", result="xxx")
    run = RunRecord(run_id="run-1", session_id="session-1", status=RunStatus.error, tools=[done, waited])

    account = continuation._calls_account(run, {}, of_run=of_run)

    assert ("`text` tool" in account, "`append` tool" in account) == (True, listed)
