# Matrix Event Pipeline Simplification Plan

## Status

This is a feasibility-first architecture and cutover plan, not a prediction of every file or commit that implementation will require.

The implementation proceeds only if a focused prototype proves that the replacement is correct, fast enough, and materially smaller than the current system.

All MindRoom production changes then land in one implementation PR.

The required mindroom-nio change necessarily remains a separate prerequisite PR because it is a different repository.

## Goal

Replace the overlapping Matrix callback-obligation, history-repair, conversation-cache, handled-source, and visible-delivery state machines with explicit non-overlapping owners.

| Fact | Sole owner |
| --- | --- |
| Matrix recovery provenance and callback redelivery | mindroom-nio |
| Accepted inbound event and pending semantic work | Principal-bound Matrix event journal |
| Latest visible conversation content | Conversation projection |
| Completed model execution | `TurnStore` |
| Initial and terminal Matrix delivery intent | Response outbox |
| Last accepted application sync checkpoint | `SyncContinuityStore` |

The desired production flow is:

```text
nio recovery and provenance
  -> durable journal admission and projection update
  -> pending event worker
  -> durable model result
  -> claimed deterministic outbox delivery
  -> Matrix
```

Intermediate AI edits remain transient transport updates and are not durable product data.

## Why This Is Worth Attempting

At baseline commit `b639b6ef3`, the overlapping subsystem contains 20,248 production lines.

The 29-file `matrix/cache/` package overlaps with conversation history, dispatch obligations, sync trust, source deduplication, and response reconciliation.

`conversation_cache.py` alone exposes five overlapping history-read paths plus advisory outbound notifications.

This duplication makes correctness depend on several stores agreeing about the same event after restarts, limited timelines, edits, redactions, and delivery failures.

The problem is not Sliding Sync alone.

Sliding Sync exposed recovery gaps, but most current complexity comes from MindRoom independently repairing, caching, certifying, and replaying state that nio or one durable application store should own.

## Non-Negotiable Invariants

- No accepted actionable event may be lost after readiness.
- The no-loss guarantee begins only after the first successful baseline response has committed and readiness has been published.
- No event may create more than one semantic turn.
- `LIVE` and `RECOVERED` events are actionable, while `HISTORY` events are context-only.
- MindRoom must consume nio provenance and must never reconstruct it from cursors, timestamps, membership repetition, `limited`, `prev_batch`, or server-specific pagination shapes.
- Nio must reject sync completion when MindRoom cannot durably admit an actionable callback.
- A failed admission must not advance the application checkpoint.
- An unrecovered room keeps the bot unready instead of silently becoming history.
- Conversation reads are bounded and indexed after at most one successful hydration per conversation and membership epoch.
- Intermediate edit bodies and edit chains are not retained.
- Redacting a current edit restores the server-authoritative previous visible revision without retaining a local edit chain.
- No read path may serve a revision after that revision's redaction has been durably admitted, so a pending refetch omits the message rather than returning deleted content.
- Initial and terminal response deliveries are idempotent across crashes.
- The final implementation PR head must not leave both the old and replacement production paths active.
- The single implementation PR must be green and must remove every active owner it replaces before merge.
- Existing authorization, E2EE metadata, media transcript, large-message sidecar, reaction, command, source-redaction, and room-membership behavior remains in scope.
- The existing continuity-checkpoint format remains readable during cutover so downtime events can still be recovered.
- `bot.py` remains lifecycle and dependency wiring rather than becoming an implementation owner.
- If a third review round still finds a new correctness class, implementation stops for redesign.

## State Decisions That Must Not Be Rediscovered During Implementation

### Principal ownership

One shared database backend may hold several principals, but runtime code receives only a principal-bound store view.

Operational methods such as `admit`, `pending`, `settle`, `load_conversation`, membership changes, and delivery methods therefore do not accept `principal_id`.

Inbound envelopes and conversation keys also omit `principal_id` because the bound store supplies it.

This prevents callers from accidentally reading or settling another bot's rows.

### Conversation identity storage

Typed APIs represent an unthreaded conversation as `thread_id=None`.

Durable SQLite and PostgreSQL tables represent it with `thread_id TEXT NOT NULL` and the empty string as the single canonical storage value.

One shared boundary helper encodes `None` to the empty string and decodes it back, so primary keys and uniqueness constraints never depend on nullable equality.

### Durable admission

Admission performs the journal insert or deduplication, membership-epoch validation, and projection update in one transaction.

The admission callback returns to nio only after that transaction commits.

Context-only payloads may be compacted after projection, while actionable payloads retain the exact replay input until terminal settlement.

A pending worker processes committed events in durable receipt order and leaves an event pending on cancellation or failure.

No durable `running` state is needed because a process crash must make the event eligible for retry.

### Visible-message projection

The projection stores one row per logical message with its latest visible body.

A valid same-sender edit replaces that row only when `(origin_server_ts, event_id)` is newer than the current replacement identity.

An edit received before its original is stored as one latest unresolved edit per target and sender.

Including sender in the unresolved-edit key prevents an attacker from evicting the legitimate author's edit before the original arrives.

When the original arrives, only its sender's unresolved edit may apply, and all unresolved rows for that target are deleted.

Admission records every redaction target as a compact durable tombstone before projection so an original or edit arriving later cannot resurrect redacted content.

Redacting the logical original tombstones the logical message.

Redacting the currently visible replacement clears the row's visible body and marks the logical row with a durable refresh token derived from the redaction's journal receipt order.

Clearing the body in the same admission transaction is required because a redacted revision must never be readable, and a stale-but-visible row would otherwise let any non-strict caller serve content the sender deleted.

A strict conversation read waits for one shared point refetch of the logical original and its current relations instead of serving content known to be stale.

A non-strict read never waits and never serves a body-cleared row, so it omits that logical message until a refetch installs the server-authoritative revision.

The point refetch uses the same relation traversal and reducer as initial hydration, retains no edit chain, and installs the reconstructed visible row only when both the membership epoch and exact refresh token still match.

A newer edit or redaction changes the projection revision and prevents an older in-flight refetch from overwriting it.

Successful conditional installation clears the refresh token, while failure or cancellation leaves it durable and makes strict reads fail closed until retry succeeds.

The next strict read of that conversation drives the retry, so no background refresh worker exists and a permanently unreachable homeserver degrades reads rather than accumulating retry state.

Redacting an already superseded replacement does not change visible content.

### Bounded conversation reads

Every conversation read requires a positive limit and an optional stable cursor composed of `(created_ts, logical_event_id)`.

The store queries the newest bounded page through a principal, room, thread, timestamp, and event-ID index and returns the page in chronological order.

Prompt assembly requests pages only until its context budget is satisfied.

Full exports iterate explicit pages.

No runtime API may materialize an unbounded room-scoped conversation.

### Hydration

A thread is hydrated by fetching its root and traversing recursive event relations without a relation-type filter.

Mindroom-nio must expose `recurse`, parse the returned `recursion_depth`, and support a required minimum depth.

MindRoom requires a reported depth of at least three on every recursive page before installing any page's events.

The Matrix version advertised by `/versions` is not proof of recursion depth.

Tuwunel and Synapse currently report depth three, but the real-server proof must verify behavior rather than trust that observation.

A room-scoped conversation may perform one serialized initial `/messages` traversal.

Concurrent first readers share one hydration task, and hydration becomes complete only after successful pagination and an atomic membership-epoch-checked installation.

Failure remains a visible readiness or request failure rather than reviving room-wide repair scans.

Current-edit redaction reuses this hydrator for a logical-message point refetch rather than introducing a second history-repair implementation.

### Deterministic delivery

Initial and final delivery stages use deterministic transaction IDs derived from principal, turn, and stage.

The completed model result is durable in `TurnStore` before final outbox enqueue so recovery does not rerun a completed model call merely to rebuild delivery content.

Enqueue may create a row or update an unattempted row.

The worker then atomically claims the row by committing `attempted=true` before network I/O.

Claiming makes the payload and target immutable and returns the exact stored delivery to send.

An attempted but unacknowledged row is retried with the same payload and transaction ID.

This ordering closes the case where Matrix accepted an older deterministic transaction while a restarted model run produced different content that could never become visible.

### Storage concurrency

SQLite uses one writer task and a bounded command queue.

Writer and reader connections use WAL-compatible settings and an explicit `busy_timeout`.

PostgreSQL implements the same behavioral contract without a second application protocol.

The two backends run the same admission, projection, membership, pagination, and outbox contract tests.

### Homeserver behavior that is not observable from this repository

These facts come from the fork repositories and the deployment configuration rather than from MindRoom source, so implementation must not rediscover them by debugging.

Tuwunel purges superseded `m.replace` events on a background job, which is why edit-redaction recovery must ask the server instead of trusting any local history.

The purge is disabled by default in the fork but enabled in the MindRoom production deployment, with a 24-hour minimum age, an hourly interval, and a 10,000-event batch size.

That 24-hour floor means a current-edit refetch normally returns the true previous edit and returns the original body only once superseded edits have aged out, and both outcomes are correct because every Matrix client sees the same server state.

The purge exists to reclaim storage from MindRoom's own streaming edit churn, which this plan already treats as transient, so it is not a reason to retain edit history locally.

Tuwunel and the MindRoom Synapse fork both cap recursive relation traversal at depth three in source, and neither advertises that cap, which is why the required depth must be read from `recursion_depth` on each page.

Both homeservers deduplicate a repeated transaction ID per sending device rather than per access token, and MindRoom persists its device across restarts, so deterministic outbox retries survive a crash but would not survive re-login with a new device.

Synapse expires stored transaction mappings on a periodic cleanup, so the real-server proof must record how long a deterministic retry stays idempotent rather than assuming it is unbounded.

## Feasibility Proof Before Production Cutover

The first implementation work occurs on an isolated prototype branch that is not separately merged into `main`.

It proves the risky primitives without wiring a second production path into MindRoom.

If it passes, its implementation and proof harness become the starting point of the single MindRoom implementation PR rather than being rebuilt.

If it fails, the branch is discarded without adding unused architecture to `main`.

### Store and projection proof

The prototype must demonstrate:

- Principal isolation through bound store views.
- Exact journal deduplication and pending replay on SQLite and PostgreSQL.
- Correct ordered and shuffled edit reduction, including pre-original cross-sender edits.
- Current-edit redaction restoring the latest remaining server revision without retaining previous bodies.
- A durable refresh token surviving restart, strict reads waiting for it, and refetch failure serving no stale content.
- A newer edit racing point refetch and winning through conditional installation.
- Non-strict reads omitting a body-cleared message instead of returning the redacted revision, on every read path, including across a restart with the refetch still pending.
- Bounded cursor reads over a 100,000-message room conversation.
- One indexed projection update per edit and no retained intermediate edit chain.
- Zero SQLite lock failures under 50 concurrent Matrix conversations with an explicit `busy_timeout`.

### Crash proof

The harness must cover these boundaries:

1. Before journal commit.
2. After journal commit but before nio records callback acceptance.
3. After callback acceptance but before the pending worker starts.
4. After durable turn creation but before model execution.
5. After the model result is durable but before outbox enqueue.
6. After outbox enqueue but before claim commits.
7. After claim commits but before network I/O.
8. After Matrix accepts the transaction but before acknowledgement is stored.
9. After acknowledgement but before journal settlement.

Every case must produce one terminal turn and at most one visible response.

Cases five through nine must execute the model exactly once.

### Real-server proof

The manual harness must run against both Tuwunel and the MindRoom Synapse fork.

It must prove:

- The realistic restart case where bounded `/messages` exhaustion returns an empty chunk and omits `end` still recovers and replies once.
- Cold history populates context and never starts a turn.
- A root, reply, edit, and redaction relation tree reports and supplies the required recursive depth.
- Redacting the latest edit reveals the prior unredacted edit when the server still retains it and reveals the original body after superseded edits have been purged.
- Edit-heavy streaming leaves one latest logical message without durable intermediate bodies.
- Deterministic retry after server acceptance creates one Matrix event.

Manual integration scripts follow the repository's existing `tests/manual/` convention.

### Feasibility decision

Before any production cutover, record the prototype's source size, database size, admission latency, writer-queue latency, bounded-read latency, query plans, lock failures, and crash results.

Proceed only if all correctness tests pass, no room scan is required after hydration, and the replacement has a credible path to removing substantially more production code than it adds.

Use the measured prototype size to set a written source-growth budget before opening the MindRoom implementation PR.

If the prototype needs compatibility facades, multiple writers, retained edit chains, or a second recovery classifier, stop and reject this design.

## One-PR Implementation Sequence

### 0. mindroom-nio prerequisite

Land a focused mindroom-nio PR that adds recursive relation query support, parses `recursion_depth`, and enforces an optional minimum recursion depth before yielding page events.

The current MindRoom baseline already pins mindroom-nio 0.36.0, so MindRoom must bump to the first release containing this additional contract.

That release is a hard prerequisite for every downstream hydration change, and MindRoom must not add a fallback when depth is absent or too shallow.

Do not add batch admission or MindRoom storage policy to nio during this work.

All remaining phases occur on one MindRoom branch and in one implementation PR.

They are internal cutover checkpoints, not separately mergeable PRs, and only the final state may be merged.

### 1. Ingress ownership cutover

Introduce only the journal, membership epoch, principal-bound store, admission adapter, and pending worker needed to replace inbound callback durability.

Before the implementation PR can merge, remove dispatch obligations, dispatch admission, the cold-history fence, and settlement retry ownership.

This checkpoint must pass realistic nio restart recovery, provenance mapping, crash replay, authorization, command, media, reaction, redaction, and decryption-failure behavior.

The journal replacement must be net simpler than the ingress owners it deletes.

### 2. Conversation projection cutover

Add latest-visible projection storage, bounded reads, and one-time thread and room hydration.

Change conversation resolution, reply lookup, reaction lookup, stale-stream cleanup, hooks, and streaming thread targeting to use the bounded projection API.

Before the implementation PR can merge, remove the Matrix cache package, conversation-cache read variants, room-scan thread history and repair, cache trust, cache certification, advisory outbound cache writes, and cache write coordination.

Reduce checkpoint publication to successful durable admission plus nio's exact unrecovered-room result.

Do not preserve deleted cache interfaces to keep implementation-specific tests green.

### 3. Delivery ownership cutover

Add the claimed deterministic outbox and durable model-result handoff.

Keep only the latest unsent streaming content in memory.

Before the implementation PR can merge, remove duplicate pending-visible, delivery-retry, handled-source, and response-reconciliation state when the journal, `TurnStore`, or outbox owns the same fact.

Preserve only unique model-execution, cancellation, redaction, and business-outcome data in `TurnStore`.

### 4. Final deletion and proof

Remove any remaining cache, repair, replay, or delivery owner made obsolete by the cutovers.

Update the architecture documentation to name one owner for each durable fact.

Run the complete backend, crash, performance, full repository, and real-server suites from the final state.

The implementation PR may not add a compatibility path merely to avoid deleting an old implementation-specific test.

## Merge Gates

The single implementation PR tracks these results as its internal checkpoints complete and reports the final values in its description.

| Gate | Required result |
| --- | --- |
| Lost actionable events | Zero across the crash matrix and restart recovery. |
| Duplicate turns | Zero. |
| Duplicate terminal responses | Zero. |
| Historical turns | Zero from `HISTORY` or a cold baseline. |
| Model reruns after durable completion | Zero. |
| Edit storage | One visible row per logical message and at most one unresolved row per target and sender. |
| Current-edit redaction | Strict reads return the server-authoritative prior revision after point refetch and never stale content. |
| Redacted revision exposure | Zero reads of any kind return a revision whose redaction was durably admitted. |
| Conversation reads | Bounded cursor pages using the conversation index. |
| Post-hydration room scans | Zero. |
| SQLite lock failures | Zero in the 50-conversation stress run. |
| Backend parity | The same behavioral contract passes on SQLite and PostgreSQL. |
| Competing owners | No replaced active path remains. |
| Source size | The PR reports additions, deletions, new-owner size, and deleted-owner size. |

Timing thresholds are manual release evidence rather than flaky CI assertions.

The initial performance targets are p95 durable admission below 50 milliseconds, p95 bounded conversation reads below 50 milliseconds, and p95 writer-queue wait below 100 milliseconds on the standard development host.

Those targets must be recorded with host details and may be revised only from measured prototype evidence.

The complete implementation diff must be materially net negative in production source lines.

The previous 8,000-line reduction remains a target, not proof by itself, because moving the same complexity into new modules would still be a failed simplification.

## Expected Deletions

The exact touched files are decided during implementation, but these ownership groups must disappear if their responsibility is replaced.

- `matrix/cache/` and its write coordinator.
- `matrix/client_thread_history.py` room-scan, refill, gap, snapshot, and repair behavior.
- The overlapping history-read variants and advisory outbound notifications in `matrix/conversation_cache.py`.
- `dispatch_obligations/`.
- `dispatch_admission.py`.
- `cold_history_fence.py`.
- `turn_settlement_retry.py`.
- Cache generation trust and certification.
- Duplicate handled-source, pending-visible, response-idempotency, and retry-source state.

The implementation may retain a small focused module under an old filename only if it still owns a unique fact and the PR explains that ownership.

## Explicit Non-Goals

- Retaining or reconstructing intermediate edit bodies.
- Preserving old cache schemas or internal cache APIs.
- Adding a MindRoom recovery classifier beside nio provenance.
- Adding room-wide fallback repair after strict hydration fails.
- Inferring recursive relation capability from the Matrix spec version.
- Adding a speculative nio batch-admission API before measurement proves per-event admission is the remaining bottleneck.
- Preserving implementation-specific tests for deleted owners.

## Stop Conditions

Stop and redesign if any of these occur:

- The prototype loses or duplicates an admitted actionable event.
- Accepted deterministic delivery can display content different from the durable model result.
- Correct hydration requires retaining edit chains or restoring room-wide repair scans.
- Current-edit redaction cannot recover the server-authoritative visible revision through the shared point hydrator without serving stale content.
- Principal isolation cannot be expressed through one bound store interface.
- SQLite still produces lock failures with one writer and a configured `busy_timeout`.
- A cutover needs the old and new active paths simultaneously after merge.
- A cutover adds as much durable state or production code as it removes.
- Three review rounds continue to reveal new correctness classes.

Passing these gates does not guarantee that every later cutover is easy.

It does establish that the core replacement is real before MindRoom undergoes another large rewrite that only sounds simpler in a plan.
