"""Generated tool job lifecycles checked against invariants that must hold on every path.

Execution, approval continuation, cancellation, Stop, revocation, result consumption, failed saves, orderly restart,
and crash recovery interleave at await boundaries.
Blocking work runs inline, so an idle event loop marks the end of each step and a crash lands between two awaits.
The oracle keeps the facts a caller observed and the outcome precedence the runtime documents, not its bookkeeping.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest

from mindroom.tool_jobs import runtime as runtime_module
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
from tests.tool_job_helpers import (
    job_owner,
    pending_outcomes,
    tool_job_runtime,
)

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
_WRITE = runtime_module.write_json_file_durable
# Steps that act on the whole runtime; every other step needs its job to exist, except the one that starts it.
_RUNTIME_ACTIONS = frozenset(
    {"deliver", "restart", "restart_stop", "crash", "crash_shutdown", "fail_write", "die_on_write"},
)
_PAYLOAD_SUFFIX = ".result.json"


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
        "fail_write",
        "die_on_write",
    ]
    job: int = 0
    script: Script = Script()
    # The adapter cleanup every settlement of a started job runs.
    cleanup: Cleanup = "none"
    # A continuation presents the previous generation.
    stale: bool = False
    # A save fault lets this many eligible saves land first.
    skip: int = 0


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
        # Each Cancel call with the number of injected faults before it, which may explain its failure.
        self._cancels: list[tuple[asyncio.Task[BackgroundJob], int]] = []
        # An armed save fault: the next save fails once, or the next metadata save kills the process.
        self._fault: Literal["fail", "die"] | None = None
        self._skip = 0
        self._faults = 0
        self._dead = False
        self._patch = pytest.MonkeyPatch()
        self._patch.setattr(runtime_module, "write_json_file_durable", self._write)
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
        faults = self._faults
        if action.kind in _RUNTIME_ACTIONS or action.kind == "start" or job_id in self.jobs:
            try:
                await getattr(self, f"_{action.kind}")(action, job_id)
            except (OSError, ExceptionGroup):
                # An injected save fault surfaces through whichever runtime call it interrupted.
                if self._faults == faults:
                    raise
        await self._idle()
        if self._dead:
            await self._lose_process()
        elif self._faults > faults:
            self._resync()
        await self.check()

    async def finish(self) -> None:
        """Release all parked work: every job must become ready and every withdrawn card reported."""
        self._fault = None
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
        self._patch.undo()

    def _write(self, path: Path, payload: object, **options: object) -> None:
        """Save through the real writer unless an armed fault fails this save or the process already died."""
        if not self._recovering and (self._dead or self._fault is not None):
            eligible = self._dead or self._fault == "fail" or not path.name.endswith(_PAYLOAD_SUFFIX)
            if eligible and not self._dead and self._skip:
                self._skip -= 1
            elif eligible:
                self._faults += 1
                self._dead = self._dead or self._fault == "die"
                self._fault = None
                msg = "process died" if self._dead else "disk full"
                raise OSError(msg)
        _WRITE(path, payload, **options)  # type: ignore[arg-type]

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
        task = asyncio.create_task(self.runtime.cancel(job_id, owner=job_owner(), depth=0))
        self._cancels.append((task, self._faults))

    async def _fail_write(self, action: Action, _job_id: str) -> None:
        self._fault, self._skip = "fail", action.skip

    async def _die_on_write(self, action: Action, _job_id: str) -> None:
        self._fault, self._skip = "die", action.skip

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
        faults = self._faults
        shutdown = asyncio.create_task(self.runtime.shutdown())
        await self._idle()
        if action is not None and action.kind == "restart_stop" and job_id in self.jobs and not shutdown.done():
            stopping = self._faults
            try:
                await self._stop(action, job_id)
            except (OSError, ExceptionGroup):
                # A failed save loses this Stop; shutdown goes on.
                if self._faults == stopping:
                    raise
            await self._idle()
        if action is not None and action.kind == "crash_shutdown":
            await self._lose_process()
            return
        self._release_all()
        await self._idle()
        assert shutdown.done()
        if shutdown.exception() is not None:
            # A failed save stopped shutdown from settling everything; what it did not save is lost as in a crash.
            assert self._faults > faults, shutdown.exception()
            await self._lose_process()
            return
        await self._deliver()
        await self._reopen()

    async def _restart_stop(self, action: Action, job_id: str) -> None:
        await self._restart(action, job_id)

    async def _crash_shutdown(self, action: Action, job_id: str) -> None:
        await self._restart(action, job_id)

    async def _crash(self, _action: Action | None = None, _job_id: str | None = None) -> None:
        await self._lose_process()

    async def _lose_process(self) -> None:
        """Lose the process between two awaits: no task runs again, and only saved state survives."""
        root = self._root / "tool_jobs"
        for job_id, model in list(self.jobs.items()):
            if not (root / f"{job_id}.json").exists():
                # Its admission save never landed, so no process ever owned it.
                self._forget(job_id)
                continue
            saved = read_job_snapshot(root / f"{job_id}.json")
            # Observation resumes from what was saved; memory a failed save never wrote is gone.
            model.observed = saved
            if saved.status not in READY_STATUSES:
                # A shutdown under way left nothing durable; a saved cancellation or Stop did.
                cancelled = saved.status == "cancel_requested" or saved.user_stop_receipt_order is not None
                model.causes[saved.generation] = ["cancel", "restart"] if cancelled else ["restart"]
                # So did an outcome execution stopped with but no settlement saved.
                if (admission := self.admissions.get((job_id, saved.generation))) is not None:
                    admission.exit = None
        self._crashing = True
        await self._end_tasks()
        self._crashing, self._dead, self._fault = False, False, None
        self.runtime._lease.close()
        await self._reopen()
        self._resync()

    def _forget(self, job_id: str) -> None:
        del self.jobs[job_id]
        self.claims.pop(job_id, None)
        for key in [key for key in self.admissions if key[0] == job_id]:
            del self.admissions[key]

    def _resync(self) -> None:
        """After a failed save, keep what the runtime holds: a step it did not accept changed nothing."""
        for job_id in list(self.jobs):
            entry = self.runtime._entries.get(job_id)
            if entry is None:
                self._forget(job_id)
                continue
            job, model = entry.job, self.jobs[job_id]
            model.stop_order = job.user_stop_receipt_order
            model.consumed, model.consumer = job.consumed_generation, job.consumed_by_source
            model.continued = {generation for generation in model.continued if generation < job.generation}
            for key in [key for key in self.admissions if key[0] == job_id and key[1] > job.generation]:
                del self.admissions[key]
            for generation in [generation for generation in model.causes if generation > job.generation]:
                del model.causes[generation]
            if job.status == "awaiting_approval" and job.user_stop_receipt_order is not None:
                # A saved Stop still owes this pause its cancellation, which recovery settles if nothing else does.
                model.causes[job.generation + 1] = ["cancel"]
            if job.status == "running" and job.generation in model.causes:
                # A cancellation that took effect would have saved its request.
                model.causes[job.generation] = [cause for cause in model.causes[job.generation] if cause != "cancel"]
            if (claim := entry.live_claim) is not None:
                self.claims[job_id] = _Claim(claim, self._epoch, ready=job.status in READY_STATUSES)

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
        # The saved current generations' payloads exist and a replaced generation's file is gone; a failed save may
        # leave the payload it wrote before its metadata until a retry or recovery settles it.
        payloads = {
            saved: {
                f"{entry.job.job_id}.g{entry.job.generation}{_PAYLOAD_SUFFIX}"
                for entry in self.runtime._entries.values()
                if entry.job.has_result_payload and entry.saved is saved
            }
            for saved in (True, False)
        }
        on_disk = {path.name for path in root.glob(f"*{_PAYLOAD_SUFFIX}")}
        assert payloads[True] <= on_disk <= payloads[True] | payloads[False], on_disk
        for admission in self.admissions.values():
            assert admission.executions <= 1
        for task, faults in [item for item in self._cancels if item[0].done()]:
            self._cancels.remove((task, faults))
            if task.cancelled():
                continue
            if task.exception() is None:
                assert task.result().status in TERMINAL_STATUSES
            else:
                assert self._faults > faults, task.exception()
        pending = {job.job_id for job in pending_outcomes(self.runtime)}
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
        payload = encode_result_payload(ToolResultPayload("paused")) if admission.script.payload else None
        return BackgroundOutcome("awaiting_approval", approval_state={"generation": generation}, result_payload=payload)
    admission.exit = finish
    if not admission.script.payload:
        return BackgroundOutcome(finish, finish)
    return BackgroundOutcome(finish, _LONG_RESULT, result_payload=encode_result_payload(ToolResultPayload(finish)))
