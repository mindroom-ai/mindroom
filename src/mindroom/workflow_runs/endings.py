"""End an attempt or a run: stop its agents, reconcile unsettled nodes, settle its parent group, map outcome."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom.script_runs.models import TERMINAL_SCRIPT_RUN_STATES, ScriptRunState, utc_timestamp
from mindroom.workflow_runs.agent_nodes import works
from mindroom.workflow_runs.models import (
    LIVE_NODE_STATES,
    TERMINAL_RUN_STATES,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from mindroom.workflow_runs.replay import OUT_OF_TIME, RUN_ENDED, TIMED_PREFIX
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.workflow_runs.agent_nodes import AgentRuntime
    from mindroom.workflow_runs.coordination import RunCoordinator
    from mindroom.workflow_runs.dispatch import NodeDispatcher

# How long the end of a run waits for its agents' cancelled jobs, all together, before each node is settled as it
# stands: one whose job has not ended is one nobody could read.
_STOP_SECONDS, _STOP_POLL_SECONDS = 30.0, 0.05
_ERROR_TAIL_BYTES = 4096
_NO_RETURN = "The script ended without returning from main(args)."
_ATTEMPT_GONE = "The record of the run's attempt is gone."
# How often a running run's working time is saved, so a primary that dies loses at most this much of the count.
_CHARGE_SECONDS = 60.0
# How long after its report a script's exit is looked for, before the script runtime's own pass finds it.
_EXIT_POLL_SECONDS = (0.05, 0.2, 0.5, 1.0, 2.0, 5.0)


def run_outcome(
    run: WorkflowRun,
    attempt: ScriptRunRecord | None,
    stopped: WorkflowRunState = WorkflowRunState.CANCELLED,
) -> tuple[WorkflowRunState, str | None]:
    """Return how a run whose attempt ended ends: an accepted stop first, then a failed exit, then the report.

    A stop is a cancel unless the engine stopped the run as *stopped*. An attempt whose record is gone ended with the
    primary, for all that is known.
    """
    state = ScriptRunState.INTERRUPTED if attempt is None else attempt.state
    reason = _ATTEMPT_GONE if attempt is None else attempt.cancellation_reason
    if state is ScriptRunState.CANCELLED:
        return stopped, reason
    if attempt is not None and state is ScriptRunState.FAILED:
        tail = (attempt.error or attempt.output).encode()[-_ERROR_TAIL_BYTES:]
        return WorkflowRunState.FAILED, run.error or tail.decode(errors="ignore")
    if run.finished_at is not None:
        # The script reported and its process then ended cleanly or with the primary, so the report decides.
        return (WorkflowRunState.COMPLETED if run.error is None else WorkflowRunState.FAILED), run.error
    if state is ScriptRunState.EXITED:
        return WorkflowRunState.FAILED, _NO_RETURN
    return WorkflowRunState.INTERRUPTED, reason


class RunEndings:
    """Stop a run's agents, settle unsettled nodes, write the ending run state, and update its parent group."""

    def __init__(
        self,
        live: RunCoordinator,
        dispatch: NodeDispatcher,
        *,
        cancel: Callable[..., Awaitable[WorkflowRun]],
        settle: Callable[..., Awaitable[WorkflowRun]],
    ) -> None:
        self.live = live
        self.dispatch = dispatch
        self._cancel = cancel
        self._settle = settle

    async def _stop_agents(self, run: WorkflowRun) -> None:
        """Ask every agent of the ending *run*, and every run inside it, to stop, and wait for all of them together.

        The run is marked under its lock, so it takes no call from then on, and the lock is not held for the wait.
        The end of a job says what its turn did, which `end_run` reads then; one deadline is for all the jobs.
        """
        async with self.live.condition(run.run_id):
            current = self.live.store.get_run(run.run_id)
            if current.epoch != run.epoch or current.state in TERMINAL_RUN_STATES:
                return
            self.live.stopping.add(run.run_id)
            # Before any call of the run is stopped: the place it gives up starts no call that waited for one.
            self.live.places.pop(run.run_id, None)
            self.live.condition(run.run_id).notify_all()
            nodes = await asyncio.to_thread(self.live.store.nodes, run.run_id, include_stale=True, light=True)
        deadline = asyncio.get_running_loop().time() + _STOP_SECONDS
        handles = [node.handle for node in nodes if node.state in LIVE_NODE_STATES and node.handle is not None]
        runtime = self.live.agents()
        jobs = [handle.ref for handle in handles if handle.kind == "job" and runtime is not None]
        inside = [self._cancel(handle.ref, reason=RUN_ENDED) for handle in handles if handle.kind == "run"]
        await asyncio.gather(*inside, *(cast("AgentRuntime", runtime).jobs.request_cancel(job) for job in jobs))
        with suppress(TimeoutError):
            async with asyncio.timeout_at(deadline):
                while any(works(runtime, job) for job in jobs):  # noqa: ASYNC110 - no event for a job's end
                    await asyncio.sleep(_STOP_POLL_SECONDS)

    async def end_run(
        self,
        run: WorkflowRun,
        state: WorkflowRunState,
        error: str | None,
        **changes: object,
    ) -> WorkflowRun:
        """End *run*: stop its node tasks, read each unsettled node from its handle, store the state, wake waiters.

        A run that ends for good first stops its agents (`_stop_agents`), so each node is read from a job that has
        ended. With the state `starting` only the run's attempt is over. A run that has ended, or that moved on
        from the epoch *run* has, is returned as it is: what was decided about *run* says nothing about it.
        """
        condition = self.live.condition(run.run_id)
        try:
            if state in TERMINAL_RUN_STATES:
                # Without the lock, which the wait for the run's agents must not hold.
                await self._stop_agents(run)
            # Held to the run's last write, so no call is admitted between the look at its nodes and its end.
            async with condition:
                current = self.live.store.get_run(run.run_id)
                if current.epoch != run.epoch or current.state in TERMINAL_RUN_STATES:
                    return current
                ended = state in TERMINAL_RUN_STATES
                # Before any call of the run is stopped: the place it gives up starts no call that waited for one.
                self.live.places.pop(run.run_id, None)
                mine = self.live.tasks.get(run.run_id, set()) if not ended else self.live.tasks.pop(run.run_id, set())
                # The wait for a timed agent outlives the attempt: it stops the turn on time while no attempt waits.
                kept = () if ended else [task for task in mine if task.get_name().startswith(TIMED_PREFIX)]
                tasks = mine - {asyncio.current_task(), *kept}
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                current = self.live.store.get_run(run.run_id)
                nodes = await asyncio.to_thread(self.live.store.nodes, run.run_id, include_stale=True, light=True)
                # An agent's job outlives an attempt, and so do a nested run and a question: none outlives its run.
                to_reconcile = [
                    node.key
                    for node in nodes
                    if node.state in LIVE_NODE_STATES
                    and (
                        ended
                        or not (node.kind == "ask" or (node.handle is not None and node.handle.kind in ("job", "run")))
                    )
                ]
                for key in to_reconcile:
                    if (full := await asyncio.to_thread(self.live.store.get_node, run.run_id, key)) is not None:
                        await self.dispatch.reconcile(current, full, run.epoch, stop=ended)
                if state is WorkflowRunState.COMPLETED:
                    # What the completed script no longer reaches leaves the run's current view.
                    await asyncio.to_thread(self.live.store.mark_stale, run.run_id, run.epoch)
                at = current.finished_at or utc_timestamp()
                changes = {"state": state, "error": error, "finished_at": at, **self.live.time_left(current), **changes}
                attempt = current.script_run_id or ""

                def forget(written: asyncio.Future[WorkflowRun]) -> None:
                    if not written.cancelled() and written.exception() is None:
                        self.live.launched.pop(attempt, None)

                end = asyncio.ensure_future(
                    asyncio.to_thread(self.live.store.update_run, run.run_id, run.epoch, **changes),
                )
                # What is kept per attempt goes with the write: a task cancelled at the commit leaves none of it behind.
                end.add_done_callback(forget)
                ended = await asyncio.shield(end)
                self.live.charged.pop(run.run_id, None)
                self.live.refused.discard(run.run_id)
                condition.notify_all()
                if ended.state in TERMINAL_RUN_STATES and ended.parent_run_id is not None:
                    await self._settle_group(ended)
        finally:
            self.live.stopping.discard(run.run_id)
        return ended

    async def _settle_group(self, child: WorkflowRun) -> None:
        """Show how the nested run *child* ended on its group in the run it is in.

        Only the group's row is written, under the outer run's epoch and without its lock, which the end of the
        outer run holds while it ends this one.
        """
        ends = {
            WorkflowRunState.COMPLETED: WorkflowNodeState.COMPLETED,
            WorkflowRunState.CANCELLED: WorkflowNodeState.CANCELLED,
        }
        end = ends.get(child.state, WorkflowNodeState.FAILED)
        error = None if end is WorkflowNodeState.COMPLETED else {"kind": child.state.value, "message": child.error}
        with suppress(SupersededAttemptError, WorkflowRunStoreError):
            outer = self.live.store.get_run(str(child.parent_run_id))
            key = f"{child.parent_node_key}/{child.run_id}"
            group = self.live.store.get_node(outer.run_id, key)
            generation = 1 if group is None else group.generation
            settle = partial(
                self.live.store.settle_node,
                outer.run_id,
                key,
                generation,
                outer.epoch,
                state=end,
                error=error,
            )
            await asyncio.to_thread(settle)

    async def watch_time(self, run_id: str, epoch: int, hours: float) -> None:
        """Save the working time the attempt that began with *epoch* uses, and stop it when none is left."""
        left = self.live.store.get_run(run_id).runtime_seconds_left
        with suppress(SupersededAttemptError):
            while left is not None and left > 0:
                await asyncio.sleep(min(_CHARGE_SECONDS, left))
                # Under the run's lock, as a change of the run's state charges it: the time left is read and written
                # by one of them at a time, so neither takes the same time off twice or writes back an older time.
                async with self.live.condition(run_id):
                    run = self.live.store.get_run(run_id)
                    if charge := self.live.time_left(run):
                        run = await asyncio.to_thread(self.live.store.update_run, run_id, epoch, **charge)
                left = run.runtime_seconds_left
            if left is not None:
                # An attempt that had ended already is judged as the pass judges it, before the run is stopped.
                await self._settle(self.live.store.get_run(run_id), replay=True)
                out_of_time = OUT_OF_TIME.format(hours=hours)
                await self._cancel(run_id, reason=out_of_time, state=WorkflowRunState.INTERRUPTED, epoch=epoch)

    async def observe_exit(self, run_id: str, script_run_id: str) -> None:
        """End the run soon after its script reported: look for the exit instead of waiting for the next pass."""
        for delay in _EXIT_POLL_SECONDS:
            await asyncio.sleep(delay)
            if (await self.live.scripts.reconcile_durable(run_id=script_run_id)).state in TERMINAL_SCRIPT_RUN_STATES:
                # Only the run that reported: another run's ended attempt is the next pass's to judge.
                with suppress(WorkflowRunNotFoundError):
                    run = await asyncio.to_thread(self.live.store.get_run, run_id)
                    if run.script_run_id == script_run_id:
                        await self._settle(run)
                break
