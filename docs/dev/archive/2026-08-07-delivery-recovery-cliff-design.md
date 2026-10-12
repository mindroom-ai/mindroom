# Delivery Recovery Cliff Design

Dated record of the fix that keeps durable response-delivery recovery from blocking Matrix receive progress, and of the live acceptance profile that exercises the production-shaped workload which exposed it.
Implementation base: `6a994538f684fac01fa5dec26fd440a479de6aad`, whose lock pinned mindroom-nio 0.37.0.
The live run preceded a final rebase onto that base whose intervening delta changed only test isolation.

## Why

Awaiting response-outbox recovery from the nio sync response callback stalled receive progress, because nio awaits that callback before requesting the next Sliding Sync response.
When a room still had an open recovery gap, nio refused an ordinary send with `SendRetryError`, and MindRoom retries such a send for up to 30 seconds.
The next sync response may be the only event that can advance the room gap, so the callback waited for progress that it prevented.
The outbox scan walks owed deliveries serially, so several blocked rows turned the per-row retry window into cross-room head-of-line blocking of receive progress.

## Decisions

The fix had to remove the receive-loop dependency itself rather than tune around it: recovery runs in one owner-scoped background task per bot that each sync response wakes, so a sync callback never waits for a recovery send.
A new sync wake interrupts the retry backoff of an incomplete pass, because new sync progress is what can unblock a refused send.
The response outbox stays the only durable statement that delivery work is owed: the wake is transient, and no second source of recovery truth, durable retry flag, queue, second worker framework, or pending-worker dependency was added.

## Rejected alternatives

Because each of these tunes the cliff instead of removing the dependency, the fix deliberately does not:

- raise the Sliding Sync timeline limit;
- reduce streaming edits or change edit throttling;
- shorten or replace the 30-second send retry window;
- parallelize the outbox scan across rooms.

## Limitations and open questions

Recovery scans owed deliveries serially, so a row inside its retry window delays recovery of later rows in other rooms, but not receive progress.
Moving bot-ready and orchestrator-ready work off the first-sync path, and the broader question of whether first-sync hooks should avoid outbound Matrix I/O, were deferred to a separate review, because moving readiness changes startup ordering beyond the every-response regression.

## Evidence: acceptance workload

The live `recovery-cliff` profile that this work added to `scripts/testing/fuzz_live_matrix.py` (merged in `e1d1624ca`, #1800) exercises this production-shaped workload:

- Sliding Sync with a timeline limit of 100.
- A managed sender account and a distinct managed responder on the built-in `synthetic` provider: seed 1, 4,000 to 4,800 response characters in 40-character chunks at 80 characters per second (about 50 to 60 seconds per stream), tool-call probability 0.2, and shell execution pinned to the isolated local runtime.
- After one warm-up turn, the runtime is paused at a confirmed process-group boundary, a derived 601-event context gap is sent, and the runtime resumes after 100 distinct thread roots, each mentioning the responder and carrying a unique run and thread marker, all sent before awaiting any response.

A run counted as exercising the failure only if the room logged a post-warm `Waiting to retry Matrix delivery after sync recovery` marker, at least one workload FINAL outbox row was observed attempted and unacknowledged, the responder's detached recovery worker later logged a resend, and the room recorded no recovery abandonment.
PASS also required exactly one completed canonical reply per root, zero unacknowledged outbox rows and zero pending journal rows after one fixed drain window, a healthy health endpoint throughout, no sync-watchdog stall, sync progress after load, and clean shutdown within MindRoom's shutdown bound.
The run command was `uv run python scripts/testing/fuzz_live_matrix.py --profile recovery-cliff --reply-timeout 180`, whose 180-second overall service-level deadline was fixed and was not extended because partial replies kept arriving.
The observer enumerated each positioned sync interval forward through `/messages`, because limited or compacted `/sync` timelines omit reply and edit evidence.
