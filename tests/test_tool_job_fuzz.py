"""Shrinkable generated lifecycles for background tool jobs across Stop, approval waits, restarts, and crashes."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule, run_state_machine_as_test

from tests.tool_job_fuzz import Action, JobFuzzRunner, Script

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from tests.tool_job_fuzz import Cleanup

_LIVE = ("running", "cancel_requested", "awaiting_approval")
_JOB_STEPS = (
    *("wait", "wait", "ack", "drop"),
    *("cancel", "cancel", "stop", "stop", "revoke", "restart_stop"),
)
_SCRIPTS = st.builds(
    Script,
    finish=st.sampled_from(("completed", "failed", "raise", "self_cancel")),
    block=st.booleans(),
    approval=st.booleans(),
    stubborn=st.booleans(),
    unwind_completes=st.booleans(),
    payload=st.booleans(),
)
_CLEANUPS: st.SearchStrategy[Cleanup] = st.sampled_from(
    ("none", "none", "none", "completed", "failed", "cancelled", "raise", "park", "classify", "classify"),
)
_AWAITING = Script(approval=True, stubborn=True)
_PARKED = Script(block=True, stubborn=True)


async def _inline(function: object, /, *args: object, **kwargs: object) -> object:
    return function(*args, **kwargs)  # type: ignore[operator]


@pytest.fixture
def inline_blocking_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the runtime's blocking file work on the event loop, so an idle loop means every step has settled."""
    monkeypatch.setattr(asyncio, "to_thread", _inline)


async def _runner(root: Path) -> JobFuzzRunner:
    return JobFuzzRunner(root)


class JobLifecycles(RuleBasedStateMachine):
    """Steps on the jobs of one conversation, at most three of them live, each followed by a full invariant check."""

    def __init__(self) -> None:
        super().__init__()
        self._directory = tempfile.TemporaryDirectory(prefix="tool-job-fuzz-")
        self._loop = asyncio.new_event_loop()
        self.runner = self._run(_runner(Path(self._directory.name)))

    def _run[Result](self, coroutine: Coroutine[object, object, Result]) -> Result:
        return self._loop.run_until_complete(coroutine)

    def _step(self, kind: str, job: int = 0, **options: object) -> None:
        self._run(self.runner.step(Action(kind, job, **options)))  # type: ignore[arg-type]

    def _slots(self, *statuses: str) -> list[int]:
        return [
            int(job_id.removeprefix("job"))
            for job_id, entry in sorted(self.runner.runtime._entries.items())
            if not statuses or entry.job.status in statuses
        ]

    def _target(self, data: st.DataObject, *statuses: str) -> int:
        """Pick a job in one of `statuses`, or mostly a live one and sometimes any job."""
        live = self._slots(*(statuses or _LIVE))
        if statuses or (live and data.draw(st.integers(0, 3))):
            return data.draw(st.sampled_from(live))
        return data.draw(st.sampled_from(self._slots()))

    @precondition(lambda self: len(self._slots(*_LIVE)) < 3)
    @rule(script=_SCRIPTS, cleanup=_CLEANUPS)
    def start(self, script: Script, cleanup: Cleanup) -> None:
        """Start a new job."""
        self._step("start", len(self.runner.jobs), script=script, cleanup=cleanup)

    @precondition(lambda self: self._slots())
    @rule(data=st.data())
    def start_again(self, data: st.DataObject) -> None:
        """Start an existing job again, which its saved ownership refuses."""
        self._step("start", self._target(data))

    @precondition(lambda self: self._slots(*_LIVE))
    @rule(data=st.data())
    def release(self, data: st.DataObject) -> None:
        """Let parked execution finish, decide its approvals, or let its cleanup finish."""
        self._step("release", self._target(data, *_LIVE))

    @precondition(lambda self: self._slots())
    @rule(data=st.data())
    def consume(self, data: st.DataObject) -> None:
        """Wait for a job and save its outcome as a parent tool result."""
        job = self._target(data)
        self._step("wait", job)
        self._step("ack", job)

    @precondition(lambda self: self._slots())
    @rule(data=st.data(), kind=st.sampled_from(_JOB_STEPS))
    def act(self, data: st.DataObject, kind: str) -> None:
        """Wait, consume, release a claim, cancel, Stop, or revoke one job."""
        self._step(kind, self._target(data))

    @rule(kind=st.sampled_from(("restart", "crash", "crash", "crash_shutdown")))
    def lifecycle(self, kind: str) -> None:
        """Restart or crash the process."""
        self._step(kind)

    @rule(kind=st.sampled_from(("fail_write", "die_on_write")), skip=st.integers(0, 2))
    def fault(self, kind: str, skip: int) -> None:
        """Make a coming save fail once, or kill the process, after `skip` saves land."""
        self._step(kind, skip=skip)

    def teardown(self) -> None:
        """Settle every job, check an orderly restart, and release storage."""
        try:
            self._run(self.runner.finish())
        finally:
            self._run(self.runner.close())
            self._loop.close()
            self._directory.cleanup()


@pytest.mark.timeout(900)
@pytest.mark.usefixtures("inline_blocking_work")
def test_generated_job_lifecycles_preserve_outcomes_and_ownership() -> None:
    """No interleaving loses, replays, relabels, or leaks a job or a consumed result."""
    run_state_machine_as_test(
        JobLifecycles,
        settings=settings(
            max_examples=200,
            stateful_step_count=50,
            deadline=None,
            print_blob=True,
            suppress_health_check=[HealthCheck.too_slow],
        ),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("inline_blocking_work")
@pytest.mark.parametrize(
    "actions",
    [
        pytest.param([Action("start", script=_PARKED), Action("cancel"), Action("crash")], id="cancel-then-crash"),
        pytest.param(
            [Action("start", script=_AWAITING), Action("cancel"), Action("crash")],
            id="cancelled-approval-wait-then-crash",
        ),
        pytest.param(
            [
                Action("start", script=_AWAITING),
                Action("start", job=1, script=_PARKED),
                Action("restart_stop"),
            ],
            id="stop-approval-wait-during-shutdown",
        ),
        pytest.param(
            [
                Action("start", script=_PARKED, cleanup="park"),
                Action("stop"),
                Action("crash_shutdown"),
            ],
            id="stop-cleanup-then-crash-during-shutdown",
        ),
        pytest.param(
            [Action("start", script=_PARKED, cleanup="classify"), Action("restart")],
            id="cleanup-sees-orderly-shutdown",
        ),
        pytest.param(
            [
                Action("start", script=_AWAITING),
                Action("fail_write", skip=1),
                Action("stop"),
                Action("release"),
            ],
            id="stop-saved-but-cancellation-save-failed",
        ),
        pytest.param(
            [Action("start", script=_AWAITING), Action("fail_write"), Action("release")],
            id="failed-approval-status-save-fails-the-job",
        ),
        pytest.param(
            [Action("start", script=Script(payload=True, block=True)), Action("die_on_write"), Action("release")],
            id="death-between-payload-and-metadata",
        ),
        pytest.param(
            [Action("start", script=_PARKED, cleanup="classify"), Action("cancel"), Action("restart")],
            id="cleanup-sees-cancellation-before-shutdown",
        ),
    ],
)
async def test_job_lifecycle_regressions(tmp_path: Path, actions: list[Action]) -> None:
    """Interleavings that once broke an invariant keep holding it."""
    runner = JobFuzzRunner(tmp_path)
    try:
        await runner.run(actions)
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("inline_blocking_work")
@pytest.mark.parametrize("corruption", ["disk", "outcome", "replay", "consumption"])
async def test_job_fuzz_oracle_detects_corruption(tmp_path: Path, corruption: str) -> None:
    """The fuzzer must fail when saved state, outcomes, execution counts, or consumption lie."""
    runner = JobFuzzRunner(tmp_path)
    try:
        await runner.step(Action("start", script=_AWAITING))
        await runner.step(Action("start", job=1))
        entries, root = runner.runtime._entries, tmp_path / "tool_jobs"
        if corruption == "disk":
            (root / "job1.json").write_text((root / "job0.json").read_text().replace('"job0"', '"job1"'))
        elif corruption == "outcome":
            entries["job1"].job = replace(entries["job1"].job, status="interrupted")
            runner.jobs["job1"].observed = None
            entries["job1"].saved = False
        elif corruption == "replay":
            runner.admissions["job1"].executions += 1
        else:
            entries["job1"].job = replace(entries["job1"].job, consumed=True)
            entries["job1"].saved = False
        with pytest.raises(AssertionError):
            await runner.check()
    finally:
        await runner.close()
