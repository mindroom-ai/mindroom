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

Each delivery row has an independent JSON file written with an fsynced temporary file, atomic replacement, and directory fsync.
Store mutations persist only the affected row and publish it to process memory only after the write succeeds.
A failed write therefore leaves both durable and in-memory state unchanged.

Rows have no durable attempt lease.
One process owns one retry loop, and the stable transaction ID makes replay after a crash safe.
Due rows are selected round-robin across rooms and assigned to at most eight workers.
Each worker serializes one room at a time, while unrelated rooms continue independently and every row already due remains eligible for the same drain.
The retry loop immediately scans again after productive drains, so work that became due during a drain does not wait for the poll interval.
Deferred rows retry indefinitely with bounded exponential backoff.
Wakeups after sync recovery reduce latency, while periodic polling preserves correctness when a wakeup is missed.

## Identity and precedence

The delivery ID is derived from the entity, room, source event, and response correlation, independently of the mutable visible target event.
The original target event and whether it was a placeholder are frozen in the row.
Before creating a replay placeholder, response execution looks up pending and settled durable ownership and returns the original target without rerunning model or delivery work.
Re-entering the same response correlation therefore reuses the existing frozen row without rebuilding content or resetting attempts.
A newer response correlation for the same visible target publishes the next revision and immediately supersedes the old in-memory authority.
Target-scoped coordinator locks prevent old and new correlations from editing the same Matrix event out of order.
Startup resolves crash-left same-target siblings to the highest durable revision before any retry.
Every attempt and settlement revalidates its exact revision, so stale work cannot publish or delete its replacement.

## Redaction

Source and target redactions are durable tombstones.
A tombstoned source prevents a later record from recreating its answer.
Redaction announces itself before waiting for an in-flight attempt, and the attempt revalidates immediately before transport.
Before writing the authoritative TurnStore tombstone, redaction writes a source-keyed barrier row through the same atomic durable writer as terminal rows.
Recording, selecting, and attempting terminal work all consult these barriers after startup loading.
The barrier survives TurnStore failure and process restart, and background reconciliation retries the tombstone before removing matching terminal rows and then the barrier.
The barrier is only a failure-window write-ahead fence and is deleted once TurnStore owns the tombstone.
Malformed barrier siblings make terminal delivery not ready without hiding valid rows or barriers.
Failure to persist a new barrier makes terminal delivery not ready for the rest of that process, while the process-local redaction announcement continues blocking sends.
If neither the barrier store nor TurnStore can write, no redaction state reached durable storage and safety cannot be carried across process loss.
The same authority lock gives the tombstone, regeneration, transport, and settlement one durable commit order.
An attempt that already crossed its final check commits before the tombstone; every other attempt observes the redaction announcement or tombstone and stops.

## Lifecycle progress

Transport success is checkpointed separately from response lifecycle work.
The after-response hook is claimed before invocation and is therefore explicitly at-most-once across a crash.
Interactive registration and thread summaries use stable idempotency identities and are checkpointed only after their observable work succeeds.
Interactive persistence failures and Matrix reaction failures raise back to the authority instead of being logged as success.
Durable thread-summary execution is awaited, uses a deterministic Matrix transaction ID, and treats history, generation, or send failure as retryable.
The exact frozen summary bypasses later volatile eligibility checks, and its delivered message count advances only after Matrix accepts the frozen payload.
One outstanding frozen summary owns its thread until delivery succeeds or its row is removed, preventing concurrent responses from preparing competing summaries.
Once transport and all lifecycle steps are settled, the row remains as a receipt until the outer handled-turn ledger is durably flushed for every source.

Shutdown cancels the retry loop and its shielded settlement batch.
Cancellation propagates through stalled transport and lifecycle hooks, while the durable row retains the progress needed for restart recovery.

## Recovery and cleanup

Startup loads every valid pending row.
A startup reconciliation retries every surviving redaction barrier before terminal delivery becomes eligible.
A malformed individual row is dropped without discarding valid siblings.
An unreadable row propagates its I/O failure instead of publishing an incomplete in-memory snapshot.
Stale-stream cleanup skips visible events still owned by durable terminal delivery, so it cannot overwrite a committed answer with an interruption notice.

## Retention

Rows remain until settled delivery is reflected in the handled-turn ledger, superseded by a newer response, or invalidated by source or target redaction.
No retry or age budget silently abandons a committed answer.
