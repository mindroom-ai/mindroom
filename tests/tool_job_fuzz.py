"""Generated tool job lifecycles checked against invariants that must hold on every path.

Execution, approval continuation, cancellation, Stop, revocation, result consumption, orderly restart, and crash
recovery interleave at await boundaries.
Blocking work runs inline, so an idle event loop marks the end of each step and a crash lands between two awaits.
The oracle keeps the facts a caller observed and the outcome precedence the runtime documents, not its bookkeeping.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest

from mindroom.tool_jobs.control import job_stopped_by_shutdown
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload
from mindroom.tool_jobs.runtime import (
    READY_STATUSES,
    TERMINAL_STATUSES,
    BackgroundJob,
    BackgroundOutcome,
    JobClaim,
    JobContinuationError,
    ToolJobRuntime,
    read_job_snapshot,
    saved_job_paths,
)
from tests.tool_job_helpers import job_owner, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

type Finish = Literal["completed", "failed", "raise", "pause", "self_cancel"]
type Cleanup = Literal["none", "completed", "failed", "cancelled", "raise", "park", "classify"]
type _Cause = Literal["cancel", "shutdown", "restart"]

_RESTARTED = "Tool execution was interrupted by a runtime restart; it was not replayed."
_SHUT_DOWN = "Tool execution was interrupted by runtime shutdown; it was not replayed."
_CLASSIFIED = "Interrupted by a shutdown or restart."
# Loop iterations one step may take before it counts as livelocked.
_IDLE_ROUNDS = 10_000
_LONG_RESULT = "x" * 600
# Steps that act on the whole runtime; every other step needs its job to exist, except the one that starts it.
_RUNTIME_ACTIONS = frozenset({"deliver", "restart", "restart_stop", "crash", "crash_shutdown"})


@dataclass(frozen=True)
class Script:
    """What one admission of an operation does: its own outcome, and how it meets cancellation."""

    finish: Finish = "completed"
    # Parks until an explicit release instead of finishing at once.
    block: bool = False
    # Cancelled while parked, it parks again until released, then re-raises or completes anyway.
    stubborn: bool = False
    unwind_completes: bool = False
    payload: bool = False


@dataclass(frozen=True)
class Action:
    """A printable, shrinkable step; it names a job slot, never state an earlier step created."""

    kind: Literal[
        "start",
        "continue",
        "release",
        "wait",
        "ack",
        "drop",
        "cancel",
        "stop",
        "revoke",
        "deliver",
        "restart",
        "restart_stop",
        "crash",
        "crash_shutdown",
    ]
    job: int = 0
    script: Script = Script()
    # The adapter cleanup every settlement of a started job runs.
    cleanup: Cleanup = "none"
    # A continuation presents the previous generation.
    stale: bool = False


@dataclass
class _Admission:
    script: Script
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    executions: int = 0
    # The status the operation itself produced, when it produced one.
    exit: str | None = None


@dataclass
class _Claim:
    claim: JobClaim
    epoch: int
    # Whether the wait that returned it saw a ready outcome, which only then may be consumed.
    ready: bool


@dataclass
class _Job:
    cleanup: Cleanup
    cleanup_gate: asyncio.Event = field(default_factory=asyncio.Event)
    observed: BackgroundJob | None = None
    stop_order: int | None = None
    consumed: int | None = None
    consumer: str | None = None
    # What stopped each generation, in order; the first cause stands.
    causes: dict[int, list[_Cause]] = field(default_factory=dict)
    continued: set[int] = field(default_factory=set)


class JobFuzzRunner:
    """Drive one real runtime incarnation after another over the same storage, checking every step."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._epoch = 0
        self._order = 0
        self._sources = 0
        self._recovering = False
        self._crashing = False
        self._baseline = asyncio.all_tasks()
        self._cancels: list[asyncio.Task[BackgroundJob]] = []
        self.jobs: dict[str, _Job] = {}
        self.admissions: dict[tuple[str, int], _Admission] = {}
        self.claims: dict[str, _Claim] = {}
        self.owed_withdrawals: set[str] = set()
        self.violations: list[str] = []
        self.runtime = self._open()

    def _open(self) -> ToolJobRuntime:
        return tool_job_runtime(self._root, cancel=self._cleanup)

    async def run(self, actions: list[Action]) -> None:
        """Apply each step, then settle everything and check that an orderly restart changes nothing."""
        for action in actions:
            await self.step(action)
        await self.finish()

    async def step(self, action: Action) -> None:
        """Apply one step, let every task it woke run to a standstill, then check every invariant."""
        job_id = f"job{action.job}"
        if action.kind in _RUNTIME_ACTIONS or action.kind == "start" or job_id in self.jobs:
            await getattr(self, f"_{action.kind}")(action, job_id)
        await self._idle()
        await self.check()

    async def finish(self) -> None:
        """Release all parked work: every job must become ready and every withdrawn card reported."""
        self._release_all()
        await self._idle()
        await self.check()
        for job_id, entry in self.runtime._entries.items():
            assert entry.job.status in READY_STATUSES, (job_id, entry.job.status)
            assert entry.task is None or entry.task.done(), job_id
            assert entry.drain is None, job_id
        await self._deliver()
        assert not self.owed_withdrawals
        await self._restart()
        await self.check()

    async def close(self) -> None:
        """End every task this example started and release storage, even after a failed check."""
        self._crashing = True
        self._release_all()
        await self._end_tasks()
        self.runtime._lease.close()

    async def _cleanup(self, job: BackgroundJob) -> BackgroundOutcome | None:
        model = self.jobs[job.job_id]
        if model.cleanup == "park" and not self._recovering:
            await model.cleanup_gate.wait()
        if model.cleanup == "raise":
            msg = "cleanup failed"
            raise RuntimeError(msg)
        if model.cleanup == "classify":
            # Like a native child, record whether a shutdown or restart stopped it rather than a cancellation.
            return BackgroundOutcome("interrupted", _CLASSIFIED) if job_stopped_by_shutdown() else None
        if model.cleanup in {"none", "park"}:
            return None
        return BackgroundOutcome(model.cleanup, f"cleanup {model.cleanup}")

    def _operation(self, job_id: str, generation: int, script: Script) -> Callable[[], Awaitable[BackgroundOutcome]]:
        admission = self.admissions[job_id, generation] = _Admission(script)

        async def operation() -> BackgroundOutcome:
            admission.executions += 1
            if script.block:
                try:
                    await admission.gate.wait()
                except asyncio.CancelledError:
                    if not script.stubborn or self._crashing:
                        raise
                    await admission.gate.wait()
                    if not script.unwind_completes:
                        raise
                    admission.exit = "completed"
                    return BackgroundOutcome("completed", "Finished while unwinding")
            return _finish(admission, generation)

        return operation

    def _refused(self, job_id: str) -> Callable[[], Awaitable[BackgroundOutcome]]:
        async def operation() -> BackgroundOutcome:
            self.violations.append(f"{job_id}: a refused continuation ran")
            return BackgroundOutcome("failed", "refused continuation ran")

        return operation

    def _current(self, job_id: str) -> BackgroundJob:
        return self.runtime._entries[job_id].job

    def _cause(self, job_id: str, cause: _Cause) -> None:
        """Record why work stopped: running work at its generation, a paused approval at the fresh one it gets."""
        job = self._current(job_id)
        if job.status in TERMINAL_STATUSES or (job.status == "awaiting_approval" and cause != "cancel"):
            return
        generation = job.generation + 1 if job.status == "awaiting_approval" else job.generation
        self.jobs[job_id].causes.setdefault(generation, []).append(cause)

    async def _start(self, action: Action, job_id: str) -> None:
        options = {"tool_name": "tool", "depth": 0, "adapter": {}, "owner": job_owner()}
        if job_id in self.jobs:
            with pytest.raises(ValueError, match="already exists"):
                await self.runtime.start(job_id, operation=self._refused(job_id), **options)
            return
        self.jobs[job_id] = _Job(action.cleanup)
        _, claim = await self.runtime.start(job_id, operation=self._operation(job_id, 0, action.script), **options)
        assert claim is not None
        assert claim.generation == 0
        self.claims[job_id] = _Claim(claim, self._epoch, ready=False)

    async def _continue(self, action: Action, job_id: str) -> None:
        current = self._current(job_id)
        expected = current.generation - 1 if action.stale else current.generation
        applies = current.status == "awaiting_approval" and not action.stale and self.jobs[job_id].stop_order is None
        generation = current.generation + 1
        operation = self._operation(job_id, generation, action.script) if applies else self._refused(job_id)

        async def resume() -> BackgroundJob:
            return await self.runtime.continue_job(
                job_id,
                owner=job_owner(),
                depth=0,
                expected_generation=expected,
                operation=operation,
                adapter={"generation": generation},
            )

        if not applies:
            with pytest.raises(JobContinuationError):
                await resume()
            return
        resumed = await resume()
        assert resumed.generation == generation
        self.jobs[job_id].continued.add(current.generation)

    async def _release(self, _action: Action, job_id: str) -> None:
        for (owner, _generation), admission in self.admissions.items():
            if owner == job_id:
                admission.gate.set()
        self.jobs[job_id].cleanup_gate.set()

    def _release_all(self) -> None:
        for admission in self.admissions.values():
            admission.gate.set()
        for model in self.jobs.values():
            model.cleanup_gate.set()

    async def _wait(self, _action: Action, job_id: str) -> None:
        held = self.claims.get(job_id)
        result = await self.runtime.wait(
            job_id,
            owner=job_owner(),
            depth=0,
            timeout=0,
            claim=None if held is None else held.claim,
        )
        if result.job.status in READY_STATUSES:
            # The only waiter always gets the ready generation's claim.
            assert result.claim is not None
            assert result.claim.generation == result.job.generation
            self.claims[job_id] = _Claim(result.claim, self._epoch, ready=True)
        else:
            assert result.claim is None
            self.claims.pop(job_id, None)

    async def _ack(self, _action: Action, job_id: str) -> None:
        held = self.claims.get(job_id)
        if held is None or not held.ready:
            return
        del self.claims[job_id]
        self._sources += 1
        source = f"$source{self._sources}"
        if held.epoch != self._epoch or held.claim.generation != self._current(job_id).generation:
            # A claim from an earlier process, or on a generation a cancellation replaced, consumes nothing.
            with pytest.raises(ValueError, match="no longer belongs"):
                await self.runtime.acknowledge_wait(job_id, held.claim, source_event_id=source)
            return
        await self.runtime.acknowledge_wait(job_id, held.claim, source_event_id=source)
        model = self.jobs[job_id]
        if model.consumed != held.claim.generation:
            model.consumed, model.consumer = held.claim.generation, source

    async def _drop(self, _action: Action, job_id: str) -> None:
        if (held := self.claims.pop(job_id, None)) is not None:
            await self.runtime.release_wait(job_id, held.claim)

    async def _cancel(self, _action: Action, job_id: str) -> None:
        self._cause(job_id, "cancel")
        self._cancels.append(asyncio.create_task(self.runtime.cancel(job_id, owner=job_owner(), depth=0)))

    async def _stop(self, _action: Action, job_id: str) -> None:
        # During shutdown, the shutdown that already stopped running work stays its first cause.
        self._cause(job_id, "cancel")
        self._order += 1

        async def matches(job: BackgroundJob) -> bool:
            return job.job_id == job_id

        await self.runtime.stop_jobs(receipt_order=self._order, matches=matches)
        self.jobs[job_id].stop_order = self._order

    async def _revoke(self, _action: Action, job_id: str) -> None:
        self._cause(job_id, "cancel")
        await self.runtime.cancel_revoked(denied=lambda job: job.job_id == job_id)

    async def _deliver(self, _action: Action | None = None, _job_id: str | None = None) -> None:
        """Report withdrawn approval cards as the delivery coordinator does, on its own schedule."""
        taken = self.runtime.take_withdrawn_approvals()
        for job_id in taken:
            # A withdrawn card can never belong to a pause that is still current.
            assert self._current(job_id).status != "awaiting_approval", job_id
        self.owed_withdrawals -= taken

    async def _restart(self, action: Action | None = None, job_id: str | None = None) -> None:
        """Shut down in order, optionally Stopping a job while shutdown drains, then recover."""
        for owner in self.jobs:
            self._cause(owner, "shutdown")
        shutdown = asyncio.create_task(self.runtime.shutdown())
        await self._idle()
        if action is not None and action.kind == "restart_stop" and job_id in self.jobs and not shutdown.done():
            await self._stop(action, job_id)
            await self._idle()
        if action is not None and action.kind == "crash_shutdown":
            await self._crash()
            return
        self._release_all()
        await self._idle()
        assert shutdown.done()
        shutdown.result()
        await self._deliver()
        await self._reopen()

    async def _restart_stop(self, action: Action, job_id: str) -> None:
        await self._restart(action, job_id)

    async def _crash_shutdown(self, action: Action, job_id: str) -> None:
        await self._restart(action, job_id)

    async def _crash(self, _action: Action | None = None, _job_id: str | None = None) -> None:
        """Lose the process between two awaits: no task runs again, and only saved state survives."""
        for job_id, model in self.jobs.items():
            job = self._current(job_id)
            if job.status not in READY_STATUSES:
                # A shutdown already under way left nothing durable; a saved cancellation or Stop did.
                causes = model.causes.get(job.generation, [])
                model.causes[job.generation] = [cause for cause in causes if cause == "cancel"] + ["restart"]
                # So did an outcome execution stopped with but no settlement saved.
                if (admission := self.admissions.get((job_id, job.generation))) is not None:
                    admission.exit = None
        self._crashing = True
        await self._end_tasks()
        self._crashing = False
        self.runtime._lease.close()
        await self._reopen()

    async def _end_tasks(self) -> None:
        current = asyncio.current_task()
        while pending := [
            task
            for task in asyncio.all_tasks()
            if task not in self._baseline and task is not current and not task.done()
        ]:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._cancels.clear()

    async def _reopen(self) -> None:
        """Recover a fresh incarnation: no operation reruns, and ready work keeps its exact saved state."""
        saved = {path.stem: read_job_snapshot(path) for path in saved_job_paths(self._root / "tool_jobs")}
        executions = {key: admission.executions for key, admission in self.admissions.items()}
        self._epoch += 1
        self.runtime = self._open()
        self._recovering = True
        try:
            await self.runtime.recover()
        finally:
            self._recovering = False
        assert {key: admission.executions for key, admission in self.admissions.items()} == executions
        assert set(saved) == set(self.jobs)
        for job_id, before in saved.items():
            after = self._current(job_id)
            stopped_pause = before.status == "awaiting_approval" and before.user_stop_receipt_order is not None
            if before.status in READY_STATUSES and not stopped_pause:
                assert after == before, job_id
            else:
                assert after.status in TERMINAL_STATUSES, (job_id, after.status)
                assert after.generation == before.generation + int(stopped_pause), job_id

    async def _idle(self) -> None:
        """Yield until no task is runnable; with blocking work inline, every waiter then awaits a test gate."""
        loop = asyncio.get_running_loop()
        for _ in range(_IDLE_ROUNDS):
            await asyncio.sleep(0)
            if not loop._ready:  # type: ignore[attr-defined]
                return
        msg = "Tool job runtime never became idle"
        raise AssertionError(msg)

    async def check(self) -> None:
        """Compare memory, disk, outcomes, and discovery with what callers observed."""
        assert not self.violations, self.violations
        root = self._root / "tool_jobs"
        for job_id, entry in self.runtime._entries.items():
            job = entry.job
            if entry.saved:
                assert read_job_snapshot(root / f"{job_id}.json") == job, job_id
            self._observe(job)
        # Exactly the saved current generations' payloads exist; a replaced generation's file is gone.
        assert {path.name for path in root.glob("*.result.json")} == {
            f"{entry.job.job_id}.g{entry.job.generation}.result.json"
            for entry in self.runtime._entries.values()
            if entry.saved and entry.job.has_result_payload
        }
        for admission in self.admissions.values():
            assert admission.executions <= 1
        for task in [task for task in self._cancels if task.done()]:
            self._cancels.remove(task)
            if not task.cancelled():
                assert task.result().status in TERMINAL_STATUSES
        pending = {job.job_id for job in await self.runtime.pending_outcomes()}
        assert pending == {job_id for job_id in self.jobs if self._pending(job_id)}

    def _pending(self, job_id: str) -> bool:
        job, model, held = self._current(job_id), self.jobs[job_id], self.claims.get(job_id)
        claimed = held is not None and held.epoch == self._epoch and held.claim.generation == job.generation
        return (
            job.status in READY_STATUSES
            and model.consumed != job.generation
            and model.stop_order is None
            and not claimed
        )

    def _observe(self, job: BackgroundJob) -> None:
        model = self.jobs[job.job_id]
        previous, model.observed = model.observed, job
        assert job.user_stop_receipt_order == model.stop_order, job.job_id
        assert (job.consumed_generation, job.consumed_by_source) == (model.consumed, model.consumer), job.job_id
        if previous is not None:
            self._transition(model, previous, job)
        if job.status in TERMINAL_STATUSES and (previous is None or previous.status not in TERMINAL_STATUSES):
            reason = job.result if job.status == "interrupted" else None
            assert (job.status, reason) == self._expected(job.job_id, job.generation), job.job_id

    def _transition(self, model: _Job, previous: BackgroundJob, job: BackgroundJob) -> None:
        assert job.generation in {previous.generation, previous.generation + 1}, job.job_id
        if job.generation > previous.generation:
            # Only a continuation or a cancelled pause starts a generation.
            assert previous.status == "awaiting_approval", job.job_id
        elif previous.status == "awaiting_approval":
            assert job.status == "awaiting_approval", job.job_id
        elif previous.status == "cancel_requested":
            assert job.status == "cancel_requested" or job.status in TERMINAL_STATUSES, job.job_id
        if (
            previous.status == "awaiting_approval"
            and job.generation > previous.generation
            and previous.generation not in model.continued
        ):
            # A pause that ended without its approval owes the withdrawal of the card that presented it.
            self.owed_withdrawals.add(job.job_id)
        if previous.status in TERMINAL_STATUSES:
            assert (job.status, job.generation, job.result) == (
                previous.status,
                previous.generation,
                previous.result,
            ), job.job_id

    def _expected(self, job_id: str, generation: int) -> tuple[str, str | None]:
        """The documented outcome precedence for one settled generation."""
        model = self.jobs[job_id]
        admission = self.admissions.get((job_id, generation))
        exit_status = None if admission is None else admission.exit
        causes = model.causes.get(generation, [])
        if not causes:
            assert exit_status in TERMINAL_STATUSES, (job_id, generation, exit_status)
            return exit_status, None
        # A cleanup that completed or failed outranks what execution stopped with.
        decided = {"completed": "completed", "failed": "failed", "raise": "failed"}.get(model.cleanup)
        if decided is not None:
            return decided, None
        if exit_status in TERMINAL_STATUSES:
            return exit_status, None
        if model.cleanup == "cancelled" or causes[0] == "cancel":
            return "cancelled", None
        if model.cleanup == "classify":
            return "interrupted", _CLASSIFIED
        return "interrupted", _SHUT_DOWN if causes[0] == "shutdown" else _RESTARTED


def _finish(admission: _Admission, generation: int) -> BackgroundOutcome:
    finish = admission.script.finish
    if finish == "raise":
        admission.exit = "failed"
        msg = "operation failed"
        raise RuntimeError(msg)
    if finish == "self_cancel":
        admission.exit = "cancelled"
        raise asyncio.CancelledError
    if finish == "pause":
        admission.exit = "awaiting_approval"
        return BackgroundOutcome("awaiting_approval", approval_state={"generation": generation})
    admission.exit = finish
    if not admission.script.payload:
        return BackgroundOutcome(finish, finish)
    return BackgroundOutcome(finish, _LONG_RESULT, result_payload=encode_result_payload(ToolResultPayload(finish)))
