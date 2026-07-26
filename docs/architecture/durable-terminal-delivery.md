# Durable terminal delivery

MindRoom streams a response by sending a placeholder and editing it repeatedly.
The final edit publishes the committed response body and terminal stream status.
Matrix can temporarily reject that edit during limited-sync recovery or a transport outage.

Durable terminal delivery makes a committed terminal edit eventual instead of leaving the user with a partial stream.

## Authority

`mindroom.terminal_delivery` is the single authority for record precedence, retry scheduling, transport, redaction ordering, and success-only lifecycle progress.
`mindroom.delivery_gateway` builds and freezes the exact Matrix edit before asking that authority to commit it.
`mindroom.bot` owns the authority's startup, sync wakeups, and shutdown.

Only successful terminal edits of an existing visible event enter the durable path.
Failed or cancelled model runs and first sends without a visible event keep their ordinary failure behavior.

## Commit and retry

The exact prepared Matrix payload is persisted before the first transport attempt.
This preparation includes any large-message sidecar reference, so a retry never uploads the body again.
Every revision has one deterministic Matrix transaction ID, and every retry reuses that ID and the persisted payload.

The per-entity JSON file is written with an fsynced temporary file, atomic replacement, directory fsync, and an advisory lock.
Store mutations build a candidate snapshot, persist it, and publish it to process memory only after the write succeeds.
A failed write therefore leaves both durable and in-memory state unchanged.

Rows have no durable attempt lease.
One process owns one retry loop, and the stable transaction ID makes replay after a crash safe.
Due rows are selected round-robin across rooms and retried indefinitely with bounded exponential backoff.
Wakeups after sync recovery reduce latency, while periodic polling preserves correctness when a wakeup is missed.

## Identity and precedence

The delivery ID is derived from the entity, room, visible target event, and source event.
Re-entering the same response correlation reuses the existing frozen revision without rebuilding content or resetting attempts.
A newer response correlation atomically replaces the old row and increments the revision.
Every attempt and settlement revalidates its exact revision, so stale work cannot publish or delete its replacement.

## Redaction

Source and target redactions are durable tombstones.
A tombstoned source prevents a later record from recreating its answer.
Redaction announces itself before waiting for an in-flight attempt, and the attempt revalidates immediately before transport.
The same authority lock gives the tombstone, regeneration, transport, and settlement one durable commit order.
An attempt that already crossed its final check commits before the tombstone; every other attempt observes the redaction announcement or tombstone and stops.

## Lifecycle progress

Transport success is checkpointed separately from response lifecycle work.
The after-response hook is claimed before invocation and is therefore explicitly at-most-once across a crash.
Interactive registration and thread summaries use stable idempotency identities and are checkpointed only after their observable work succeeds.
Interactive persistence failures and Matrix reaction failures raise back to the authority instead of being logged as success.
Durable thread-summary execution is awaited, uses a deterministic Matrix transaction ID, and treats history, generation, or send failure as retryable.
Once transport and all lifecycle steps are settled, the row is deleted.

Shutdown cancels transport work but awaits any shielded settlement that already has a transport result.

## Recovery and cleanup

Startup loads every valid pending row.
A malformed individual row is dropped without discarding valid siblings.
An unreadable or wrong-schema top-level file is quarantined.
Stale-stream cleanup skips visible events still owned by durable terminal delivery, so it cannot overwrite a committed answer with an interruption notice.

## Retention

Rows remain until delivered, superseded by a newer response, or invalidated by source or target redaction.
No retry or age budget silently abandons a committed answer.
