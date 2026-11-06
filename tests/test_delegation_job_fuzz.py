"""Shrinkable generated lifecycles for background subagents with child approvals, Stop, restarts, and crashes."""

from __future__ import annotations

import asyncio
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule, run_state_machine_as_test

from mindroom.delegation import job_approvals
from mindroom.event_journal import ToolJobStore
from mindroom.reply_scope import ReplyRuntime
from mindroom.tool_jobs.client_cancel import cancel_stopped_reply_jobs
from tests.delegation_job_fuzz import ChildScript, DelegationFuzzRunner, Step
from tests.reply_span_helpers import reply_span

if TYPE_CHECKING:
    from collections.abc import Coroutine

_SCRIPTS = st.builds(
    ChildScript,
    approvals=st.integers(0, 2),
    hold_start=st.booleans(),
    hold_tool=st.booleans(),
)
_BUDGETS = st.sampled_from((None, None, 0, 0.01))
_INDEXES = st.integers(0, 3)


async def _runner(root: Path, monkeypatch: pytest.MonkeyPatch) -> DelegationFuzzRunner:
    runner = DelegationFuzzRunner(root, monkeypatch)
    await runner.open()
    return runner


class SubagentLifecycles(RuleBasedStateMachine):
    """Steps on up to four background children of one parent conversation, each followed by invariant checks."""

    def __init__(self) -> None:
        super().__init__()
        self._directory = tempfile.TemporaryDirectory(prefix="delegation-fuzz-")
        self._loop = asyncio.new_event_loop()
        self._patch = pytest.MonkeyPatch()
        self.runner = self._run(_runner(Path(self._directory.name), self._patch))

    def _run[Result](self, coroutine: Coroutine[object, object, Result]) -> Result:
        return self._loop.run_until_complete(coroutine)

    def _step(self, kind: str, **options: object) -> None:
        self._run(self.runner.step(Step(kind, **options)))  # type: ignore[arg-type]

    def _has_job(self) -> bool:
        return any(child.job_id is not None for child in self.runner.children)

    @precondition(lambda self: len(self.runner.children) < 4)
    @rule(script=_SCRIPTS, budget=_BUDGETS)
    def delegate(self, script: ChildScript, budget: float | None) -> None:
        """Delegate a new background child from a parent turn."""
        self._step("delegate", script=script, budget=budget)

    @precondition(lambda self: self._has_job())
    @rule(index=_INDEXES, budget=_BUDGETS)
    def wait(self, index: int, budget: float | None) -> None:
        """Wait for a child's job from a parent turn."""
        self._step("wait", index=index, budget=budget)

    # Hypothesis calls preconditions synchronously; a card its job's end already denied is skipped by the rule.
    @precondition(lambda self: any(not card.decision.done() for card in self.runner.approvals.cards))
    @rule(index=_INDEXES, approve=st.booleans())
    def approve(self, index: int, approve: bool) -> None:
        """Answer one approval card a child's job posted."""
        self._step("approve", index=index, approve=approve)

    @precondition(lambda self: self._has_job())
    @rule(kind=st.sampled_from(("cancel", "client_cancel")), index=_INDEXES)
    def end(self, kind: str, index: int) -> None:
        """Cancel or Stop a child's job."""
        self._step(kind, index=index)

    @precondition(lambda self: self.runner.turns or self._has_job())
    @rule()
    def release(self) -> None:
        """Let held children and tools finish."""
        self._step("release")

    @precondition(lambda self: self._has_job())
    @rule(kind=st.sampled_from(("restart", "crash")))
    def lifecycle(self, kind: str) -> None:
        """Restart or crash the process."""
        self._step(kind)

    def teardown(self) -> None:
        """Settle every child, check consistency, and release storage."""
        try:
            self._run(self.runner.finish())
        finally:
            self._run(self.runner.close())
            self._loop.close()
            self._patch.undo()
            self._directory.cleanup()


@pytest.mark.timeout(900)
def test_generated_subagent_lifecycles_keep_effects_and_traces_exact() -> None:
    """No interleaving runs an unapproved or repeated effect, replays a child, or leaves an orphaned card answerable."""
    run_state_machine_as_test(
        SubagentLifecycles,
        settings=settings(
            max_examples=30,
            stateful_step_count=20,
            deadline=None,
            print_blob=True,
            suppress_health_check=[HealthCheck.too_slow],
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "steps",
    [
        pytest.param(
            [Step("delegate", script=ChildScript(hold_start=True)), Step("crash")],
            id="crash-after-cancellation-settled-the-child",
        ),
        pytest.param(
            [
                Step("delegate", script=ChildScript(approvals=1)),
                Step("approve"),
                Step("crash"),
            ],
            id="crash-after-approved-child-completed",
        ),
        pytest.param(
            [
                Step("delegate", script=ChildScript(approvals=1, hold_tool=True)),
                Step("approve"),
                Step("cancel"),
            ],
            id="cancel-during-approved-tool",
        ),
        pytest.param(
            [Step("delegate", script=ChildScript(approvals=1)), Step("cancel")],
            id="cancel-while-awaiting-approval-denies-the-card",
        ),
        pytest.param(
            [Step("delegate", script=ChildScript(approvals=1)), Step("crash")],
            id="crash-while-awaiting-approval-denies-the-card",
        ),
        pytest.param(
            [Step("delegate", script=ChildScript(approvals=2)), Step("approve", approve=False), Step("approve")],
            id="denied-then-approved-calls-in-one-child",
        ),
    ],
)
async def test_subagent_lifecycle_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: list[Step],
) -> None:
    """Interleavings that once broke an invariant keep holding it."""
    runner = await _runner(tmp_path, monkeypatch)
    try:
        await runner.run(steps)
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["unapproved", "repeated", "orphaned_card"])
async def test_subagent_fuzz_oracle_detects_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    """The fuzzer must fail when an effect runs unapproved or twice, or a card outlives the job that posted it."""
    runner = await _runner(tmp_path, monkeypatch)
    try:
        await runner.step(Step("delegate", script=ChildScript(approvals=1)))
        if corruption == "unapproved":
            runner.executed["w0-0"] += 1
        elif corruption == "repeated":
            await runner.step(Step("approve"))
            runner.executed["w0-0"] += 1
        else:
            save = ToolJobStore.save

            async def save_without_denial(
                store: ToolJobStore,
                job_id: str,
                job_json: str,
                result_payload_json: str | None = None,
                *,
                approval_end_reason: str | None,
            ) -> None:
                del approval_end_reason
                await save(store, job_id, job_json, result_payload_json, approval_end_reason=None)

            monkeypatch.setattr(ToolJobStore, "save", save_without_denial)
            with pytest.raises(AssertionError):
                await runner.step(Step("cancel"))
            return
        with pytest.raises(AssertionError):
            await runner.step(Step("release"))
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_while", ["planning", "waiting"])
async def test_a_stop_ends_a_subagent_job_whose_child_asks_for_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_while: str,
) -> None:
    """A Stop of the reply that started a subagent ends its job once its child paused for approval, card posted or not.

    While the policy still decides the child's call, the job reads as running; approving the card it would post
    afterwards must not run the call.
    """
    runner = await _runner(tmp_path, monkeypatch)
    planning, release = asyncio.Event(), asyncio.Event()
    original_plan = job_approvals.plan_approval_calls

    async def slow_plan(*args: object, **kwargs: object) -> object:
        planning.set()
        if stop_while == "planning":
            await release.wait()
        return await original_plan(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(job_approvals, "plan_approval_calls", slow_plan)
    principal = runner.runtime.journal.principal("leader@replies")
    replies = ReplyRuntime(
        store=principal,
        entity_name="leader",
        generation="gen-test",
        retry_sources=lambda _room_id, _sources: None,
        approval_ended=lambda _reply_id: None,
        reply_stopped=partial(cancel_stopped_reply_jobs, runner.paths),
    )
    try:
        async with reply_span(
            principal,
            source_event_id="$question",
            room_id="!room:example.org",
            placeholder_event_id="$answer",
            runtime=replies,
        ) as handle:
            # The parent's turn runs in the reply's span, so its subagent call's job belongs to the reply.
            await runner._delegate(Step("delegate", script=ChildScript(approvals=1)))
            [parent] = runner.turns
            replies.spans.register(handle.span_id, parent.task)
            await asyncio.wait_for(planning.wait(), 10)
            await runner._idle()
            assert bool(await runner.approvals.pending()) is (stop_while == "waiting")
            [job] = runner.runtime.retained_jobs()
            assert runner.runtime._entries[job.job_id].reply_id == handle.reply_id

            stopped = await principal.replies.stop(handle.reply_id, receipt_order=1, now_ns=time.time_ns())
            assert stopped is not None
            await replies.run_effects(stopped.post_commit)
            await asyncio.gather(parent.task, return_exceptions=True)
            runner.turns.clear()
            release.set()
            await runner.step(Step("approve"))

            assert not await runner.approvals.pending()
            assert [job.status for job in runner.runtime.retained_jobs()] == ["cancelled"]
            assert not runner.executed
    finally:
        release.set()
        await runner.close()
