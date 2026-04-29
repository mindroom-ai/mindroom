# Embedded API Lifecycle Plan

Last updated: 2026-04-29

Status: Draft for PR 794 stabilization.

## Purpose

This document records the shutdown invariant for the embedded API server and the plan for finishing PR 794 without continuing review-by-patch.
The immediate goal is to make the embedded FastAPI/Uvicorn server a critical runtime participant while preserving graceful shutdown and clear failure propagation.
The broader goal is to make the lifecycle policy explicit enough that future reviews can check behavior against one model instead of inferring it from individual tests.

## Recent Bug Pattern

The recent branch history shows one repeated class of bugs rather than several unrelated failures.
The code changed the status of the embedded API server from a restartable auxiliary task into a critical runtime task.
That change exposed unclear ownership of process shutdown, API shutdown, orchestrator shutdown, and concurrent task failures.

The first commit made unexpected `server.serve()` return fatal.
That solved silent API death, but it left Uvicorn signal behavior coupled to the embedded process.

The polish commit improved lifecycle log context and API-server identity plumbing.
That made behavior easier to observe, but it did not change the underlying shutdown ownership model.

The simultaneous-failure commit fixed a real race where the first completed task could hide another completed task's failure.
That made completed-task draining more correct, but it still left the API graceful-shutdown path ambiguous.

The shutdown-handoff commit fixed Uvicorn's captured-signal re-raise by handling `should_exit` locally.
That avoided process termination before MindRoom cleanup, but it made `shutdown_requested` race with Uvicorn's own graceful shutdown.

The bounded graceful-shutdown change adds a bounded wait for the API task after `shutdown_requested` wins.
That points at the correct invariant, and the committed PR must preserve that behavior before merge.

## Root Cause

The root cause is an underspecified invariant at the boundary between Uvicorn and `MultiAgentOrchestrator.main`.
`shutdown_requested` has been treated as both a notification that shutdown has begun and permission to cancel pending runtime tasks immediately.
Those are different states.

Uvicorn owns FastAPI lifespan shutdown once `server.should_exit` is set.
The orchestrator owns top-level runtime teardown once a shutdown request is observed.
Those two owners must coordinate rather than compete.

The embedded API server is critical while the runtime is running.
The embedded API server is also allowed to finish normally after an intentional runtime shutdown request.
That means API task completion is fatal before shutdown is requested and expected after shutdown is requested.

## Non-Negotiable Invariants

- Unexpected embedded API exit before application shutdown is fatal.
- Uvicorn process signals must request application shutdown without letting Uvicorn re-raise captured signals into the process after `serve()` returns.
- A shutdown request starts runtime teardown, but it does not imply immediate API task cancellation.
- When the API task is pending during requested shutdown, the orchestrator gives it a bounded chance to finish `server.serve()` and FastAPI lifespan shutdown.
- The orchestrator cancels the API task only after that grace window expires or if the outer runtime is already being forcibly cancelled.
- If several monitored tasks complete together, every completed task is drained before deciding whether the run is clean.
- A non-cancellation failure from any completed critical task beats a clean shutdown signal.
- The orchestrator task must be cancelled during final teardown if it is still waiting on its runtime shutdown event.
- Auxiliary watchers remain non-critical supervisors and should stop when `shutdown_requested` is set.

## Target Lifecycle Model

The runtime has three critical wait targets.
They are the orchestrator task, the optional API task, and a task waiting on `shutdown_requested`.

The normal running state waits until at least one critical target completes.
After one target completes, the runtime drains every target that is already done.
This avoids losing simultaneous failures.

If the API task completed and `shutdown_requested` is not set, the runtime raises `RuntimeError("Embedded API server exited unexpectedly")`.
If the API task completed and `shutdown_requested` is set, the runtime treats API completion as expected.

If `shutdown_requested` completed and the API task is still pending, the runtime waits for bounded API graceful shutdown.
During that grace window, the runtime continues monitoring and draining completed critical tasks.
If API graceful shutdown finishes inside the grace window, final teardown proceeds without cancelling the API task.
If API graceful shutdown does not finish inside the grace window, the runtime logs `embedded_api_server_shutdown_timeout` and final teardown cancels the API task.
The grace wait must use timeout semantics that do not cancel the API task before final teardown.

If the orchestrator task fails, the runtime raises that failure.
If the orchestrator task returns cleanly before shutdown is requested, that is suspicious and should be considered for future tightening, but the current PR does not need to solve that path unless a concrete bug appears.

## Proposed Code Shape

Keep the refactor narrow and local to `src/mindroom/orchestrator.py`.
Do not introduce a new lifecycle framework.
Do not move Uvicorn ownership out of `_run_api_server`.
Do not make auxiliary watchers critical.

Use a small context dataclass for API host and port log fields.
Keep `_SignalAwareUvicornServer` as the only Uvicorn-specific signal adapter.
Keep `_run_api_server` responsible for initializing the API app, running `server.serve()`, logging serve return, and classifying unexpected serve return.
Keep `_wait_for_runtime_completion` responsible for top-level critical task coordination.
Keep `_consume_completed_runtime_tasks` responsible for draining all tasks in the completed set.
Keep `_await_api_task_completion` responsible for classifying one API task completion.
Add or keep `_await_api_task_graceful_shutdown` for the bounded wait after shutdown request wins.
Keep `_await_api_task_graceful_shutdown` monitoring the orchestrator task during the API grace window so orchestrator failures cannot be hidden by requested shutdown.
Use `asyncio.wait` with a timeout inside the bounded grace wait so timeout does not cancel the API task before final teardown.

The grace timeout should be a named module constant.
The initial value can be `5.0` seconds because it is long enough for local FastAPI lifespan cleanup and short enough for CLI shutdown.
Tests should patch the constant down rather than sleeping for real time.

## Implementation Plan

1. Include the bounded API graceful-shutdown fix in the committed PR diff.
2. Ensure `_wait_for_runtime_completion` waits for `_await_api_task_graceful_shutdown` only when `shutdown_wait_task` wins and `api_task` is still pending.
3. Ensure `_consume_completed_runtime_tasks` still drains the API task when it is already done in the first completed set.
4. Ensure `_await_api_task_graceful_shutdown` still drains orchestrator failures that complete during the API grace window.
5. Ensure `_SignalAwareUvicornServer.handle_exit` does not call `super().handle_exit`.
6. Preserve Uvicorn's important behavior by setting `should_exit` on the first signal and `force_exit` on repeated `SIGINT`.
7. Keep final teardown cancellation in `main` as the last-resort cleanup path for any task still pending.
8. Keep the API shutdown timeout log structured with host, port, and timeout seconds.
9. Update the PR description so it names the bounded graceful-shutdown invariant.

## Required Tests

- Uvicorn signal handling sets `shutdown_requested`, sets `server.should_exit`, and does not append captured signals.
- `_run_api_server` raises when `server.serve()` returns before shutdown is requested.
- `_run_api_server` allows `server.serve()` to return after shutdown is requested.
- Simultaneous orchestrator failure and clean API completion raises the orchestrator failure.
- Orchestrator failure during the API graceful-shutdown window raises instead of being hidden by final teardown.
- `main` waits for API graceful shutdown after `shutdown_requested` is set and does not cancel an API task that finishes inside the grace window.
- `main` waits for API graceful shutdown, logs `embedded_api_server_shutdown_timeout` on grace expiry, then cancels the API task when the API shutdown path is stuck.
- `main` still raises when the API task fails unexpectedly.
- The coalescing and Matrix ingress logging tests remain separate from lifecycle tests.

## Review Checklist For This PR

- Check that the committed PR diff covers the bounded graceful-shutdown invariant.
- Check whether any test encodes immediate API cancellation after shutdown request as desired behavior.
- Check whether any path can await the API task forever after `shutdown_requested` is set.
- Check whether Uvicorn can re-raise SIGTERM after `serve()` returns.
- Check whether completed critical task failures can be hidden by another completed task.
- Check whether FastAPI lifespan shutdown can be skipped by premature cancellation.
- Check whether logs describe lifecycle state without logging message content.

## Refactor Decision

A large refactor is not justified for PR 794.
The recurring issues are clustered around one lifecycle boundary and can be resolved by making that boundary explicit.

A small refactor is justified.
The small refactor should consolidate critical runtime task waiting, API task completion classification, and bounded API graceful shutdown into named helpers.
That creates one source of truth for the shutdown policy without spreading lifecycle state across the orchestrator.

A larger refactor should be reconsidered only if another review round finds a new bug class outside this boundary.
Examples would include auxiliary watcher failure semantics, orchestrator clean-return semantics, or API app global-state ownership.

## Merge Criteria

- The committed PR includes the bounded API graceful-shutdown fix.
- The committed tests cover both graceful API completion and timeout cancellation.
- Focused lifecycle tests pass.
- The touched coalescing and Matrix ingress tests pass or are already green in CI.
- Pre-commit passes before merge.
