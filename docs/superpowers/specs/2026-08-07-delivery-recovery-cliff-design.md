# Delivery Recovery Cliff Design

Implementation base: `6a994538f684fac01fa5dec26fd440a479de6aad` after the final user-requested rebase to the current PR tip.

## Purpose

Prevent durable response recovery from blocking Matrix receive progress, and add a live acceptance profile that exercises the production-shaped workload which exposed the cycle.

The implementation must fix the receive-loop dependency rather than raise Matrix timeline limits or shorten the existing delivery retry window.

## Verified failure

MindRoom currently awaits response-outbox recovery from the nio sync response callback.

nio awaits that callback before requesting the next Sliding Sync response.

When a room still has an open recovery gap, nio refuses an ordinary send with `SendRetryError`, and MindRoom retries that send for up to 30 seconds.

The next sync response may be the only event that can advance the room gap, so the callback can wait for progress that it prevents.

`ResponseDelivery` scans owed deliveries serially, so multiple blocked rows turn the per-row retry window into cross-room head-of-line blocking.

## Runtime design

The response outbox remains the only durable statement that delivery work is owed.

`AgentBot` owns one transient `asyncio.Event` wake and at most one owner-scoped recovery task.

Every accepted sync response sets the wake, even when a recovery task is already running.

The task clears the wake before each pass, asks the outbox for recoverable deliveries, and performs at most one coalesced follow-up pass for any number of wakes that arrived during the preceding pass.

An exception or an incomplete recovery result schedules another pass with capped exponential backoff.

A fresh sync wake interrupts that backoff so recovery can retry immediately after nio advances its room state.

The task is created through `create_background_task` with the bot runtime view as owner.

Sync shutdown marks the runtime as shutting down and wakes the task, while the existing owner-scoped shutdown drain finishes or cancels it.

No second worker framework, durable retry flag, queue, or pending-worker dependency is introduced.

The first-sync room-member callback registration stays synchronous because later responses must not pass before it is installed.

Bot-ready and orchestrator-ready work retains its current lifecycle path in this change.

The broader rule that first-sync hooks should not perform outbound Matrix I/O belongs to a separate review because moving readiness changes startup ordering beyond the reproduced every-response regression.

## Deterministic regression contract

The central test drives two real sync responses in sequence.

The first recovery pass waits on an event that only the second response can set.

The first callback must return before that event is set, proving that recovery no longer owns receive-loop progress.

A second test parks one pass, delivers multiple sync wakes, and requires exactly one non-overlapping follow-up pass.

A third test proves that the task is owned by the bot shutdown lifecycle and receives cancellation when the bounded owner drain expires.

The existing retry test remains responsible for exception, incomplete, successful, and later-new-debt outcomes, but it waits for owner-scoped background work after each response.

Tests use causal `asyncio.Event` boundaries rather than short wall-clock timeouts.

## Live acceptance profile

The existing `saturation` profile is renamed `short-stream-correctness` because it proves short streamed reply correctness, not production capacity.

A separate `recovery-cliff` profile uses Sliding Sync with a timeline limit of 100.

The stack configures a managed sender account and a distinct managed responder using the built-in `synthetic` provider.

The responder uses deterministic variable responses paced at 80 characters per second for approximately 50 to 60 seconds, with the normal optional tool-call phase enabled and shell execution pinned to the isolated local runtime.

The harness authenticates as the already managed sender account and emits 100 distinct thread roots that mention the responder before awaiting any response.

Every root carries a unique run and thread marker.

The observer advances one raw-event cursor only after forward `/messages` enumeration proves the complete positioned sync interval, retaining reply and edit evidence omitted from limited or compacted `/sync` timelines.

The observer correlates each responder original to its exact source event through the Matrix thread relation and requires exactly one canonical response per source.

The workload is valid only if the exact room logs a post-warm `Waiting to retry Matrix delivery after sync recovery` marker, at least one workload FINAL outbox row is observed attempted and unacknowledged, the `general` detached worker later resends delivery, and the room records no recovery abandonment.

After all responses complete, the harness waits through one fixed bounded drain window and requires zero unacknowledged response-outbox rows and zero actionable pending journal rows.

The health endpoint must remain healthy while waiting and after drain, no sync-watchdog stall may occur, and sync progress must advance once more after load.

MindRoom must then stop cleanly within its fixed shutdown bound.

The live profile uses a fixed overall service-level deadline and never extends that deadline because partial replies continue arriving.

## Deliberate exclusions

The change does not raise the Sliding Sync timeline limit.

The change does not reduce streaming edits or alter edit throttling.

The change does not parallelize the durable outbox scan across rooms.

The change does not replace the 30-second send retry window.

The change does not add a second in-export Matrix reducer or a second source of recovery truth.

## Acceptance

The focused delivery tests must fail on the published PR head and pass with the runtime fix.

Harness unit tests must prove the new profile selects Sliding Sync, uses the built-in synthetic responder, launches all roots before waiting, enforces exact canonical replies, and rejects unhealthy or undrained completion.

The full `test_sync_task_cancellation.py` and all harness-focused tests must pass serially.

Ruff, format checks, `git diff --check`, and Tach boundaries must pass.

The live `recovery-cliff` run is the final behavioral acceptance test when the local Matrix stack and host capacity are available.
