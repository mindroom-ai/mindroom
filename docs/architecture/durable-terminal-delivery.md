# Durable terminal delivery

MindRoom streams a response by sending a visible placeholder and editing it as output arrives.
The terminal edit publishes the committed response body and completed stream status.
Matrix can temporarily reject that edit during limited-sync recovery or a transport outage.

Durable terminal delivery keeps that exact committed edit retryable without creating a second persistence authority.

## Canonical authority

The existing `TurnRecord` is the only durable authority for a response turn.
An optional `TerminalEditCheckpoint` on that record freezes the exact prepared Matrix content, stable transaction ID, response identity, target-placeholder fact, and retry-safe lifecycle progress.
The same record's `response_event_id` identifies the visible event to edit.

`mindroom.delivery_gateway` prepares the complete edit before committing it.
`mindroom.turn_store` validates ownership and mutates the canonical turn through the handled-turn ledger.
`mindroom.terminal_delivery` coordinates Matrix transport, lifecycle convergence, and retries for checkpoints already stored on those records.
`mindroom.bot` starts and stops that coordinator and wakes it after Matrix sync state advances.

Only a completed edit of an existing visible response enters this path.
First sends, empty terminal bodies, unavailable-client failures, failed model runs, and cancelled model runs keep their ordinary behavior.

## Commit and retry

The gateway prepares the complete Matrix edit before the first transport attempt.
That prepared content already includes any large-message reference, so retries reuse the same content without uploading the body again.
A deterministic transaction ID is derived from the response owner, source event, correlation, target event, and exact prepared content.
Every attempt reuses that transaction ID and content.

Checkpoint commit atomically marks the canonical turn completed, records its visible response event, and stores the frozen checkpoint in the handled-turn ledger before Matrix transport begins.
The ledger writes the transaction to disk before publishing it to shared process memory.
A failed write therefore does not publish a new in-memory checkpoint.
Disk mutations are allowed to finish before caller cancellation propagates.
An edit regeneration keeps retrying its frozen initial checkpoint after transient persistence failures, so a newer edit cannot be reported handled before it becomes durable.

The coordinator scans the unique canonical `TurnRecord` values that still contain checkpoints.
It attempts up to eight checkpoints concurrently and serializes work that shares a canonical turn or visible target.
Matrix readiness gates transport.
Successful sync responses wake the worker, and periodic scans preserve progress when no wakeup arrives.
There is no retry-count or age limit that silently abandons a committed terminal edit.

## Identity and supersession

Replay lookup succeeds only when every candidate source ID resolves to the same canonical record with either an actual terminal checkpoint or a matching settled-delivery receipt.
A completed turn without either form of matching terminal ownership does not short-circuit response execution.
When a checkpoint lookup succeeds, response execution reuses the checkpoint's original visible target and placeholder fact without rerunning model or delivery work.
A settled-delivery receipt prevents the same response episode from being redispatched after checkpoint clearing.

Committing the same transaction again is idempotent.
A strictly newer edit regeneration for the same visible target may atomically replace an older deferred checkpoint.
Unrelated, stale, or non-monotonic replacements are rejected while the current checkpoint remains authoritative.
When a different turn claims the same visible target, one atomic ledger transaction clears all previous target owners and installs the new checkpoint.
Attempts, lifecycle updates, and checkpoint clearing compare the expected transaction ID, so stale work cannot mutate a replacement.

## Redaction

Source redactions and response-target redactions use the same canonical `TurnStore` transaction path as checkpoint updates.
The coordinator holds the affected turn and event locks while the durable mutation runs.

Redacting a source tombstones that source on its canonical record and retains any checkpoint as target-cleanup debt until the visible response is redacted.
Redacting a visible response target clears that target and checkpoint from every canonical owner and creates a tombstone for the redacted event in the same transaction.
This ordering prevents replay from recreating a redacted answer and prevents an outstanding checkpoint from editing a redacted target.
Cached Matrix history is sanitized after the durable redaction attempt.

## Lifecycle progress

After Matrix accepts the exact edit, the coordinator notifies the conversation cache and converges the checkpointed response effects.
It durably claims the after-response hook before invocation, which makes that hook at most once across retries and restarts.
Hook failures are logged after the claim and are not retried.

Interactive registration uses a deterministic idempotency key derived from the terminal transaction.
The checkpoint records interactive completion only after registration succeeds.
Registration failure leaves the checkpoint available for another attempt.
The checkpoint is cleared only after Matrix delivery, the after-response claim, and any required interactive registration have converged.

Thread summaries, run-metadata linkage, and memory persistence remain on the ordinary best-effort post-response path.
They are not frozen into the terminal checkpoint and are not replayed by the terminal coordinator.

## Recovery and cleanup

Startup warms the existing handled-turn ledger, then the coordinator scans its outstanding checkpoints.
No terminal-specific store is loaded or reconciled.
Stale-stream cleanup serializes its final ownership check and edit with terminal delivery for the same visible target.
It excludes targets owned by either an outstanding checkpoint or a settled-delivery receipt, so it cannot overwrite a committed answer with an interruption notice.

The checkpoint remains on its canonical turn until delivery and required checkpointed lifecycle progress converge, another turn supersedes its visible target, or redaction cleanup resolves it.
Clearing the checkpoint leaves the ordinary handled `TurnRecord` as the durable response outcome.
