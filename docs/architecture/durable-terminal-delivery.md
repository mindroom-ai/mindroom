# Durable terminal delivery

MindRoom streams a response by sending a placeholder and editing it repeatedly.
The last edit publishes the terminal outcome: the final body plus a terminal stream status.
Matrix can reject that last edit while limited-sync timeline recovery is pending.

`mindroom.matrix.client_delivery` retries such a send immediately inside a bounded window.
When that window is exhausted the response outcome is already committed but nothing is visible, so without further repair the user is left with a partial stream forever.

Durable terminal delivery closes that gap: once MindRoom commits a successful final response, transient transport failure delays visibility but never loses it.

## Components

| Module | Responsibility |
| --- | --- |
| `mindroom.terminal_delivery` | Durable record, state machine, precedence, and JSON-backed store |
| `mindroom.terminal_delivery_worker` | Scheduling: wakeups, backoff, bounded concurrency, per-room ordering |
| `mindroom.terminal_delivery_lifecycle` | Serialized response-lifecycle facts required after restart |
| `mindroom.terminal_delivery_replay` | Replays success-only lifecycle effects after repair |
| `mindroom.delivery_gateway` | Freezes and records committed intents and performs one repair attempt |
| `mindroom.bot` | Owns store warm-up, worker lifecycle, and the sync-response wakeup |

## Scope

Only a completed stream's final body is durably retried, and only as an edit of an existing visible event.

`StreamTransportOutcome` carries the canonical success body, but not the note-suffixed terminal text for an error or a cancellation, so publishing an approximation of a terminal notice later would be worse than today's behaviour.
A plain send that never landed leaves nothing visible, so it produces no stuck partial and is left to the existing failure path.

## State machine

```text
record ---> pending ---> attempting ---> deleted        (delivered, or superseded)
              ^             |
              +-- retry_wait <-            (transient failure, exponential backoff with jitter)
```

A delivered or superseded row is deleted outright rather than retained as history, so the file only ever holds outstanding work and cannot grow with uptime.
A committed answer is never abandoned for taking too long: there is no retry budget, so a transient failure retries indefinitely at the capped backoff.
That is safe because the visible placeholder already carries a delivery-failure note (see below), so a slow repair costs latency rather than the outcome.

Every transition applies to shared in-memory state and is durably written under one advisory file lock before it becomes observable, so any crash leaves exactly one valid recoverable state.

## Persistence

One JSON file per entity: `<storage>/tracking/<entity>_pending_terminal_deliveries.json`, schema-versioned, written through `write_json_file_durable` (fsynced temp file, atomic replace, directory fsync) under `advisory_file_lock`.
An unreadable or wrong-schema file is quarantined and the worker continues with an empty store.
Malformed individual rows are dropped with a count logged; valid rows in the same file survive.

Each record persists only what is needed to finish the exact committed response: owning entity, serialized `MessageTarget`, target event ID, every coalesced source event ID, rendered visible body, exact prepared Matrix edit payload, transaction ID, response-lifecycle facts, delivery revision, runtime generation, attempt count, timestamps, next attempt time, and a short error reason.
It never persists clients, access tokens, auth headers, or raw exception payloads.
The prepared payload already contains any large-message sidecar reference, so retries never upload a second copy or rebuild content from mutable thread state.

## Identity, ordering, and precedence

`delivery_id` is `sha256(entity, room, target_event_id, anchor_event_id)`, so every terminal intent for one visible target maps to one row.
`transaction_id` is persisted as `mindroom-td-<delivery_id>-<revision>`, giving at-least-once transport an exactly-once visible effect: an identical retry reuses the same Matrix transaction ID and exact prepared payload.
Ordinary sends are unaffected and keep nio's per-call identifier.

Ordering never uses wall-clock time.

1. A new intent with a different response correlation ID is by construction a later response turn for the same target, so it supersedes and bumps `revision`.
2. Re-recording the same turn is a no-op, which keeps recording idempotent and preserves the existing retry budget.
3. A redacted or missing target event settles the row as `superseded`; it is never recreated.
4. A source-event redaction settles every row owned by that source.

Every state transition and Matrix attempt is scoped to the revision it was leased against.
Recording a replacement and sending the prior revision share one per-delivery lock, and the attempt revalidates the current durable revision before transport.
A stale revision therefore cannot reach Matrix or settle, charge, or reschedule the replacement.

## Retry policy

The immediate bounded retry in `client_delivery` runs first.
After it is exhausted, `DeliveryGateway` prepares the exact repair edit once, persists it with its transaction ID, and each later attempt either lands, supersedes, or defers.

Before every attempt the worker checks the target with `room_get_event`: a missing or redacted event settles the row instead of resurrecting content.
Otherwise the edit is retried with exponential backoff plus jitter, capped.

Recording does not classify the failure, so a permanently undeliverable payload would retry forever.
That is bounded in cost and, crucially, not silent: recording a durable row still runs the ordinary visible delivery-failure repair, so the placeholder shows `Response delivery failed. Please retry.` exactly as it did before this feature existed.
A later successful retry simply replaces that note with the real answer.
The durable layer is therefore purely additive: visible behaviour on failure is unchanged, plus an eventual repair.

## Worker lifecycle

The worker starts during bot startup, right after the durable store is warmed, and stays idle until the bot is running with a live, synced client.
It wakes on every applied sync response — limited-sync recovery closes its gaps while a sync is applied, which makes that the practical recovery-ready signal — and otherwise scans on a bounded interval, so correctness never depends on a single notification.
A due row only shortens that wait once delivery can actually be attempted: during startup, where warmed rows are already due but the runtime is not ready, the loop parks on the wake event instead of spinning against work it may not run yet.
Settling an attempt is shielded from cancellation so shutdown cannot discard an outcome that already happened.

Attempts acquire the per-room ordering lock before the global semaphore, so queued work for one blocked room does not consume every global slot and one room never has two concurrent terminal writes.
Sends resolve the live client per attempt, so a replaced client or a config reload is picked up on the next attempt rather than retained.

Shutdown cancels the loop and returns every leased row to `retry_wait`.
A hard crash leaves rows in `attempting`; the next `warm()` converts leaked or expired leases back into due `retry_wait` rows.

After Matrix accepts the repair, the worker replays the success-only response lifecycle from persisted typed facts before deleting the row.
This includes the after-response hook, interactive registration, and eligible thread-summary scheduling.
Generic post-response effects that do not require successful visibility still run during the original turn and are not repeated.

## Interaction with stale-stream cleanup

Startup stale-stream cleanup repairs partial streams left by a restart by writing an interruption note and optionally auto-resuming the turn.
That must not fight durable delivery, so cleanup skips any target event a durable row still owns: no interruption note is written over a committed final response, and no duplicate turn is auto-resumed for work that has already been answered.

## Observability

Structured events, all free of response bodies and transport secrets:
`terminal_delivery_pending_recorded`, `terminal_delivery_intent_superseded_on_record`, `terminal_delivery_leases_recovered`, `terminal_delivery_startup_recovery`, `terminal_delivery_worker_wake`, `terminal_delivery_deferred`, `terminal_delivery_recovered`, `terminal_delivery_superseded`, `terminal_delivery_store_quarantined`, `terminal_delivery_records_quarantined`, and `terminal_delivery_drain_completed` (attempted count, backlog size, distinct rooms, oldest unsettled age, max attempts).

## Retention

Settled rows are deleted immediately.
Every committed unsettled row is retained until it is delivered, superseded by a newer response, or invalidated by source or target redaction.
The drain summary exposes backlog size, distinct rooms, oldest age, and maximum attempts without imposing an answer-dropping capacity limit.
