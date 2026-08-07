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

MindRoom requires only that a non-empty recursive page reports `recursion_depth` at all, because the number itself is not comparable between servers.

The Matrix version advertised by `/versions` is not proof of recursion depth.

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

Tuwunel and the MindRoom Synapse fork both cap recursive relation traversal at depth three in source, and neither advertises that cap.

The two servers do not report `recursion_depth` with the same meaning, which a live run against Tuwunel established and which invalidates any numeric floor.

Synapse returns the constant three, describing the depth it is willing to traverse, while Tuwunel returns the depth of the deepest event it actually returned, so a root with one threaded reply and one edit of that reply reports one.

A required depth above zero would therefore reject ordinary complete pages on Tuwunel while proving nothing on Synapse, so the portable requirement is only that a non-empty page reports the field at all.

That requirement still catches the failure worth catching, which is a server that ignores `recurse` and silently returns direct children only, because such a server omits the field.

An empty relation page reports no depth on Tuwunel and must not be treated as a failure, since it has nothing that could have been truncated.

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
5. After the model returns but before its result is durable.
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
- A root, reply, edit, and redaction relation tree reports `recursion_depth` and supplies every indirectly related event.
- Redacting the latest edit reveals the prior unredacted edit when the server still retains it and reveals the original body after superseded edits have been purged.
- Edit-heavy streaming leaves one latest logical message without durable intermediate bodies.
- Deterministic retry after server acceptance creates one Matrix event.

Manual integration scripts follow the repository's existing `tests/manual/` convention.

### Recorded feasibility decision

The prototype is built, and `tests/manual/event_journal_measurements.py` reproduces every number below on demand.

Measured on `macOS-26.5.2-arm64` with Python 3.13.10:

Read measurements are taken against a conversation of 100,000 messages, walked to its end over 2,001 cursor pages, because an index that only helps the newest page would look fine over a small table.

| Measurement | Result | Target |
| --- | --- | --- |
| Durable admission, p95 | 0.14 ms | under 50 ms |
| Bounded conversation read, p95 | 0.16 ms | under 50 ms |
| Deepest cursor page, at 100,000 messages | 4.44 ms | no degradation with depth |
| Cursor pages walked | 2,001 | reaches the start of the conversation |
| Writer-queue wait, p95 | 7.4 ms | under 100 ms |
| SQLite lock failures, 50 concurrent conversations | 0 | zero |
| Concurrent admission throughput | ~9,600 per second | not set |
| Conversation read query plan | covering index `visible_messages_page` | indexed |
| Pending replay query plan | index `journal_events_pending` | indexed |
| Database size per message | 636 bytes | not set |
| Replacement source | 4,589 lines (the earlier 3,654 omitted `journal_dispatch.py` and `matrix/outbound_projection.py`) | smaller than replaced |
| Replaced owners | 15,870 lines | — |
| Projected net change | −12,216 lines *projected*; the branch today is **+2,624** | materially net negative, once the old owners are deleted |

The nine crash boundaries all produce one terminal turn and at most one visible response.
Enqueueing is what makes an answer durable, so boundary five costs a model run and every boundary after it re-uses the stored payload instead of asking the model again.

The live-server proof passes against a disposable Tuwunel, covering relation traversal, redaction of the currently visible edit, edit churn, deterministic transaction reuse, device-scoped deduplication, and bounded history exhaustion.

No compatibility facade, second writer, retained edit chain, or second recovery classifier was needed, so none of the stop conditions were triggered.

The remaining risk is not in these primitives; it is in the ingress restructuring the cutover requires, because coalescing, deferred turn settlement, and streaming currently run as background work that the pending worker would need to own.

### Resolved: live messages classified as room history

**Resolved.** With the nio fix pinned, the live Tuwunel fuzz at 45 concurrent conversations passes on seed 42: 200 operations, 45 roots, 27 batches, 123 canonical agent replies, one restart, zero lost replies, zero event-loop stalls.
That is the configuration that previously failed on this branch *and* on `main`, which is what identified the defect as pre-existing rather than introduced here.

The investigation that got there is kept below, because the first hypothesis was wrong in a way worth remembering.

The live Tuwunel fuzz at 45 concurrent conversations still loses replies, and the cause is upstream of everything this plan owns.

Durable evidence, read from the journal at the moment of the stall: the unanswered source events are present in both principals' journals with `event_class = context_only`, meaning nio reported `TimelineEventProvenance.HISTORY` for messages a user had just sent.
MindRoom then correctly declines to answer them, because this plan requires it to consume nio's provenance and never re-derive it.
Nothing is pending, no admission failed, and the event loop is healthy — the bot simply owes no work.

Two distinct causes were found.
The first is fixed: the harness sent its scenario before the agent's first sync completed, and everything in an initial sync timeline is history by design.
The scenario now waits for a warm-up reply, and the seed phase passes.

The second is now fixed in the fork, and the hypothesis above was close but named the wrong mechanism.

Mid-session a sync timeline event is always `LIVE`, so `HISTORY` can only arrive through the gap backfill nio runs when a timeline is `limited`.
Continuity for such a gap is proven by `target_reached or bounded_exhausted`, and the exact-token comparison is only the first of those.
The second never fired: `bounded_exhausted` requires `gap.membership_bound`, and `plan_sync_response` passed a `cursor_token` while leaving that flag at its default.
The sliding path sets it, from the very same cursor.
So on the classic path — the default `MatrixSyncConfig.mode` — a backfill that ran to the start of the visible history answered with no end token, proved nothing, and left every recovered event classified as history.

The classic gap is bounded at both ends: the target token above, and the `since` of the sync that opened it below, since everything at or before that arrived in an earlier response.
`plan_sync_response` now derives `membership_bound` from the same cursor it already passes, which is what the sliding path does.

One nio test pinned the old behavior and was changed deliberately, because the two errors are not symmetric.
Classifying already-seen history as `RECOVERED` costs nothing — the journal recognises the event ID and admits it as a duplicate, producing no turn.
Classifying a missed message as `HISTORY` is unrecoverable: it is admitted context-only and the reply never happens.

This was a message-loss bug in the current system rather than a regression introduced here; it reproduces on `main`.
The fix is `mindroom-nio` commit `0f2c318`, pinned by lockfile on the `feat/recursive-relations` branch rather than waiting for a release.

### Open defect: a rejected revision is never reconsidered

Largely fixed, with one case left and a correction to how it was scoped.

The wall-clock part is done: a seeded edit is assigned an ordering key one past the revision it
replaces rather than this machine's clock, so it can no longer claim the future. Every ordering in
which the seed precedes the later edit now reduces correctly.

What remains is the reverse: a genuinely later edit `L` arrives *before* the seed, the seed
replaces it (correctly, as the newest thing this bot knows), and the seed's own echo then
canonicalizes down to a timestamp below `L`. `L` was discarded when it lost, so nothing restores
it. The projection is a reduction and keeps no losers, which is what makes this hard to fix
locally — the honest options are to retain rejected revisions per logical message, or to re-derive
the message from the journal when a canonicalization moves a revision backwards.

**Correction to an earlier claim in this file.** These orderings were previously dismissed as
physically unreachable, on the reasoning that a homeserver stamps our send after everything it has
already delivered to us. That holds for a single server and fails under federation:
`origin_server_ts` is set by the originating server, so a remote server can stamp `L` at 3000
while ours stamps our edit at 2000, and `L` can arrive first. The dismissal was wrong and the
orderings are reachable in any federated room.

### Superseded note: a provisional revision timestamp outranks real ones

Found by enumerating all twenty-four orderings of {seed edit, edit echo, original, later edit}
rather than the orderings a review happened to report. Sixteen of the twenty-four end with the
answer frozen at the earlier edit. The probe is kept at `edit-ordering-matrix.py` in this
session's scratch directory and runs in about a second.

The four fixes made so far each closed one ordering and none closed the class. The class is this:
a seeded revision carries this machine's clock, and that value competes on equal terms with server
timestamps in `_is_newer`. When the local clock is ahead — which is the whole reason the
provisional bit exists — a seeded edit outranks a *genuine later* edit that has already arrived.
The echo then canonicalizes the seed down to its real timestamp, but the later edit was rejected
when it arrived and nothing revisits that decision, so the projection keeps the older body
forever.

Concretely, `later_edit -> seed_edit -> echo_edit -> original` ends on `$e1 "half"` when `$e2
"complete"` is the newest thing the server ever saw.

The same-ID and direction rules do not help here, because these are two *different* edits. What is
wrong is using a provisional timestamp as an ordering key at all.

Two candidate fixes, neither implemented:

- Seed with an ordering key that cannot claim the future — derive it from the revision currently
  installed rather than from the wall clock, so a seed is newer than what it replaces and nothing
  more.
- Keep a seeded revision out of the ordering comparison entirely: install it as the visible body
  but leave the authoritative ordering key untouched, so a later real edit still wins.

The first is smaller. Either needs the full twenty-four-ordering probe as its acceptance test,
because this defect has now survived four fixes that each looked complete against the ordering
that prompted them.

### A failure mode this work kept hitting

Three separate fixes in this design over-corrected past the defect they were aimed at, each time
producing something worse than the original:

| Reported defect | The fix | What it actually did |
| --- | --- | --- |
| A rejoin could resend a stale answer | Bind the transaction ID to the membership epoch | Turned a suppressed duplicate into a visible one — deduplication was the mechanism, not the failure |
| An echo could freeze a streamed answer | Treat a matching revision ID as a canonicalizing echo | Identity has no direction, so a late seed overwrote authoritative data with a local clock |
| An empty page with a continuation was read as exhaustion | Raise when the token stops advancing | A homeserver at the start of its history returns exactly that shape, so a normal room became permanently unhydratable |

Each was reasoned from the one ordering that had been demonstrated.
The lesson is cheap to apply: before committing, state the mirror-image input — the same events reversed, or the benign server response with the same shape — and say which way each choice fails.
Prefer the direction that degrades over the one that errors, because hydration and delivery both run once per something and a hard failure there is not self-healing.
Then write the test for the opposite input, not the reported one.
A twenty-line probe against a temporary SQLite store settled all three in under a minute each, and in every case the probe disagreed with the reasoning.

### Why a failed release re-raises, and what that costs

`AgentBot.stop()` runs all three releases, collects their failures, and then raises the first.
Both halves are deliberate and were arrived at by getting each one wrong first.

Running all three matters because the resources are independent: a faulted lane must not prevent
the store and the client from being released. Re-raising matters because of what the caller does
with success. `stop_entities` awaits `asyncio.gather(*stop_tasks)` and only then pops the entities
from `agent_bots`, so a clean return is taken as proof the bot is gone and a replacement is
started on the same database under the same principal. Swallowing turned a safe halt into a silent
double-open.

The cost is worth stating: because the pop loop runs after the gather, one bot's cleanup failure
blocks removal of every bot in that batch, including ones that stopped cleanly. That is the
pre-existing shape of `gather`-then-pop rather than anything the journal introduced, and the
conservative outcome — a reload that aborts rather than one that proceeds on a half-closed store —
is the right one to prefer while the journal is the thing being torn down.

Two known imperfections, neither fixed: only the first failure is raised, so later ones survive
only in the log, where an `ExceptionGroup` would carry all of them; and `gather` propagates on the
first exception while the remaining stop tasks are still running, so their releases are not
awaited. Both are worth revisiting when shutdown is next restructured.

### The 45-thread live harness is not a pass/fail gate on one run

Observed across this session, all on the same code and all at 45 threads: seed 42 passed, seed 7
passed, seed 42 failed with one lost reply then passed on re-run, seed 42 passed, seed 7 failed
with four lost replies then passed on re-run with identical counts. That is two spurious failures
in seven runs, roughly thirty percent.

Both failures showed the same signature -- replies missing at live batch 0, with
`coordinator_queue_wait_ms` and `thread_read_total_ms` in the seconds -- and neither log contained
any error from the code under test. Twelve-thread runs have not flaked at all.

So a single 45-thread failure is not evidence of a regression, and a single 45-thread pass is not
evidence of its absence. Treat the harness as a signal that needs repetition: re-run the same seed
before concluding anything, and grep the failing run for errors from the paths you changed before
assuming they are implicated. The cheap discriminator is that a real regression reproduces on the
same seed; a flake does not.

### Feasibility decision method

Before any production cutover, record the prototype's source size, database size, admission latency, writer-queue latency, bounded-read latency, query plans, lock failures, and crash results.

Proceed only if all correctness tests pass, no room scan is required after hydration, and the replacement has a credible path to removing substantially more production code than it adds.

Use the measured prototype size to set a written source-growth budget before opening the MindRoom implementation PR.

If the prototype needs compatibility facades, multiple writers, retained edit chains, or a second recovery classifier, stop and reject this design.

## Boundary Contracts

These tighten the ownership boundaries before any further cutover work.
Each one was checked against the working prototype rather than assumed; where the prototype already violates a contract, the violation is named with its evidence.

### 1. The projection is a prompt view, not a Matrix replica

It is a bounded, recent, latest-visible view whose purpose is prompt construction.
It gets no gap repair, no certification, no periodic scan, and no unbounded export API.
A genuine full export paginates Matrix directly instead.

### 2. Ownership transfers at durable handoff

The journal owns an actionable source only until its normalized, coalesced turn is durably adopted by `TurnStore`.
At that point the source is settled and compacted; `TurnStore` owns execution and result, and the outbox owns delivery.
Keeping the source pending through model execution and delivery is what forces cross-store coordination to exist at all.

**Prototype violation.** The source stays pending until the turn is terminal, which is why `release_terminal_turn_sources`, `_terminal_sources`, and `turn_is_terminal` exist, and why settlement has to be reachable from a non-loop thread.
Moving the handoff earlier should delete all three.
Media coalescing is the case that must be proven, because a batch's turn is adopted after a debounce that spans several sources; if that cannot be expressed as one durable adoption, the missing handoff gets written down rather than patched with another repair state.

### 3. The journal retains no raw history

A `CONTEXT_ONLY` event is projected transactionally and then keeps only enough identity to deduplicate.

**Prototype violation, measured.** Such an event is inserted with `state = settled` and its full `source_json`, and `settle` only clears rows `WHERE state = 'pending'`, so the payload is never compacted.
A probe stored a single context-only event and read back `state=settled outcome=None source_json_bytes=558`.
Left alone, the journal becomes exactly the raw-event cache this plan exists to delete.

### 4. Streaming progress is transport-only

Persist the initial logical event identity and the terminal visible body.
Do not write every self-authored intermediate edit into the projection, and do not let the sync echoes of those edits reduce into it either.
User-authored edits still reduce normally.
Without this, one streamed answer costs one projection write per progress edit, twice.

### 5. Acknowledged sends are provisional until the echo canonicalizes them

A Matrix send acknowledgement carries `room_id` and `event_id` only, so it cannot supply the authoritative `origin_server_ts` the projection orders by.
A durable acknowledged send is therefore seeded as a provisional row, and the sync echo replaces its ordering metadata with the server's.
`_project_original` currently inserts `ON CONFLICT DO NOTHING`, so it cannot perform that reconciliation and must change.
Intermediate streaming edits stay out of this path entirely.

### 6. Hydration is defined by the prompt window

Hydration fetches enough recent logical messages to fill the largest prompt the runtime will build — not an arbitrary page count, and not the whole room.

The hydration marker means the one-time walk ran to completion, which is a weaker statement than "the window is full", and the difference is deliberate.
The walk ends when the window fills, when the room is exhausted, or at a raw-event ceiling, and only the first two mean the window was filled.
The ceiling case is logged and still marks the conversation hydrated, because withholding the marker would re-run a twenty-thousand-event walk on every read of that room — a worse outcome than a prompt with less history than its maximum.

**Prototype violation.** `hydrated_from_ts` records the floor a bounded walk reached, but nothing reads it: no caller extends a read past it.
It therefore describes partial completeness without supporting it, which is the same overstatement it was added to fix.
Either incremental hydration is implemented deliberately, or the column and the promise go.

**Second prototype violation, found after the first fix.** Deleting the floor left the window measured in pages of raw Matrix events.
That is the wrong unit by an order of magnitude in this product specifically: a streamed answer is one original followed by a long run of `m.replace` edits, and all of them reduce to a single line in a prompt.
A fixed page budget therefore called a few dozen messages a two-thousand-message window in exactly the rooms MindRoom creates.
The walk now counts logical messages and keeps a separate raw-event ceiling, whose only job is to stop one pathological room from being walked end to end; reaching it is logged, because it is the one exit that does not mean the window was met.

### 7. Exactly one exceptional history repair

Point refetch after the currently visible edit is redacted, and nothing else.
It uses the shared relation reducer, is conditional on both membership epoch and refresh token, and has no background worker.

### 8. Membership epochs fence every derived and pending room fact

Visible projection, resolved sidecar plaintext, approval-card projection, hydration, and pending deliveries are all fenced.
An outbox entry that was never attempted must never deliver into the membership that follows a rejoin.

That invariant used to be stated without the qualifier, and the qualifier is load-bearing.
An *attempted* row has an outcome only the homeserver knows, and the two branches want opposite things.
If the send was accepted, the answer is already in the room and the only convergent move is to resend the identical transaction so it collapses onto the same event.
If it never arrived, resending delivers an old membership's answer into the new one.
Nothing in the row distinguishes them, so no rule satisfies both.

The choice is to resend, and the reason is that the failure modes are not equally bad.
Resending when the send never arrived answers a question that really was asked, slightly late, in a room the bot has rejoined.
The alternative — deriving a fresh transaction, or refreshing the payload under the existing one — either posts the answer twice or leaves the durable record and the room permanently disagreeing about what was said, which is the exact failure the outbox exists to prevent.

Phase 3 must pin both branches explicitly: accepted-but-unacknowledged before a rejoin, and never-received before a rejoin.
The second test will assert the stale delivery, because that is the deliberate cost, and an unpinned deliberate cost is indistinguishable from a bug.

Dropping the pending rows is only half the fence, and the half that is easy to mistake for the whole of it.
The delivery that matters is the one that was claimed and sent, whose network outcome is unknown: the turn behind it is still pending, so it runs again in the new membership.

The first attempt at this fence deleted that row too, and bound the Matrix transaction ID to the membership epoch so the turn's next attempt could not be deduplicated away.
That was wrong, and the reasoning behind it was wrong in an instructive way.
It treated deduplication as the failure, when deduplication is the mechanism: if the homeserver accepted the first send, the answer is *already in the room*, and collapsing the retry onto the same event is what leaves exactly one of it.
Giving the retry a fresh identity turns a suppressed duplicate into a visible one.

The fence is therefore drawn by `attempted`, not by acknowledgement.
An unattempted row is deleted: nothing outside this process has seen it, and sending it would answer the previous membership inside the new one.
An attempted row is kept, with its frozen payload and its transaction, so the only thing a retry can do is present the same transaction again.
That converges on one visible answer whether or not the first attempt landed, and the end-to-end test asserts the room's message count rather than the identity of a transaction.

### 9. One backend, several narrow views

`PrincipalStore` currently exposes 28 methods covering journal, membership, conversation, hydration, refresh, and outbox operations.
That is the shape of the next universal cache dependency.
The shared transaction and backend stay; runtime code receives narrow principal-bound journal, projection, and outbox views instead of the whole surface.

### 10. Special facts stay specialized

Resolved long-message plaintext belongs to the visible revision and dies with it.
Tool approvals get their own small projection.
The generic conversation projection is not widened into an arbitrary event lookup.

### 11. Recovery classification stays in nio

MindRoom never infers `LIVE`, `RECOVERED`, or `HISTORY` from tokens, timestamps, membership repetition, or server behavior.

### Contract status after the correction pass

| Contract | State |
| --- | --- |
| 1 projection is a prompt view | Held; no repair, certification, scan, or export API exists |
| 2 ownership transfers at durable handoff | **Corrected and blocked** — see below |
| 3 no raw history in the journal | **Done.** Context-only events now store identity only; probe previously read `source_json_bytes=558` on a settled row |
| 4 streaming progress is transport-only | **Done, and the gap was narrower than recorded.** One pure predicate in `matrix/transport_progress.py` refuses a self-authored `m.replace` whose `visible_content` carries `pending` or `streaming`, applied from both `projected_event` and hydration's `_projected_from_event`. Originals, terminal revisions of every kind, and other senders' edits all still reduce. **Correction to the previous entry:** live admission was never the expensive half. MindRoom sends in-progress updates as `m.notice` so they raise no push notification, and `_event_kind` owns `m.text` only, so a progress echo was already refused a kind before the projection saw it. Hydration has no such filter, fetches the whole relation tree, and did reinstall every progress edit on the first cold read of a room — which is why the rule had to run in both places rather than only at ingress |
| 5 acknowledged sends are provisional | **Superseded and deleted.** Seeding landed and was then removed: the sync echo is the only route into conversation content, so nothing is provisional because nothing is written before the server has ordered it. The ordering hazards this contract existed to manage are gone with the mechanism, and the tests that pinned them went with it. The cost -- a turn that reads the conversation immediately after speaking does not see its own message -- is recorded under the seeding audit |
| 6 hydration is the prompt window | **Done.** The unused floor is gone and the window is counted in logical messages, not pages of events. The ceiling case is documented as a completion, not a full window |
| 7 one exceptional repair | Held; point refetch is the only one |
| 8 membership epochs fence pending facts | **Wired, and being hardened.** `MembershipFence` (`event_journal/membership.py`) advances the epoch at both transitions: immediately on a local leave, and for sync-reported departures. The exactly-once rule is the substance -- one departure arrives twice and the obvious guard, `bot._local_departures_awaiting_sync`, is cleared by `_on_room_joined`, so a rejoin between a leave and its echo would let the echo fence a second time and delete the conversation just hydrated under the new membership. The fence keeps its own record that a join does not clear.<br><br>Review then found the in-process record is not enough: an advance that raises leaves the marker set and the echo is swallowed, giving the departure **zero** fences; a restart between the fence and its echo loses it; two leaves before one echo need two markers and have one; and a marker whose echo never arrives swallows a later genuine departure. Being made durable and atomic with the advance.<br><br>Separately, the fence does not yet stop an **in-flight** turn: `JournalEvent` carries `membership_epoch` but the envelope, response identity, and outbox row do not, so an old-membership turn can finish after a fence and enqueue an answer into the new membership. Enqueue has to become conditional on the epoch. |
| 9 narrow views, one backend | **Done.** Seven structural protocols in `event_journal/views.py`; each collaborator takes the slice it calls. Enforcement is the type checker: a hydrator reaching for `enqueue_delivery` fails `ty` before any test runs |
| 10 special facts stay specialized | **Done.** Resolved sidecar plaintext belongs to the visible revision: the projection refuses to store an unresolved preview and records the refresh debt instead, and hydration resolves the one current revision. Approvals have their own `approval_cards` table behind `ApprovalView`, holding only the cards this bot authored and owes a decision on, fenced by membership epoch. The generic projection was not widened for either |
| 11 classification stays in nio | Held |

### Correction to contract 2: the handoff is the outbox, not TurnStore adoption

Settling the journal source once `TurnStore` durably adopts the turn would lose answers.

Replay is driven entirely by the journal: startup calls `drain_once` over pending journal events, and `TurnStore.cleanup` only retains records for sources the journal still holds.
Nothing replays an adopted-but-undelivered turn.
So at crash boundary four — after durable turn creation, before the model runs — the journal source would already be settled, no outbox row would exist yet, and no owner would owe the work.
That is silent answer loss, and it breaks the first invariant in this plan.

The defensible handoff is **durable outbox enqueue**:

- Boundaries four and five stay journal-owned, so an interrupted turn replays and the model re-runs, which is the cost already documented.
- Boundaries six through nine become outbox-owned, recovered by resending the identical claimed payload under the same transaction ID.
- `turn_is_terminal`, `_terminal_sources`, and the terminal-source callback still disappear, because settlement is triggered by enqueue rather than by asking another store whether a turn finished.
- Turns that never enqueue — commands, router decisions, intentionally ignored inputs — keep settling through the existing intentionally-ignored path.

This cannot land before the delivery cutover: `enqueue_delivery` and `ResponseDelivery` currently have no production call sites, so the settlement point does not yet exist outside tests.
Contract 2 is therefore sequenced as part of phase 3 rather than ahead of it.

**What `TurnStore` would have to hold before it could own the decision at all.**
Even at the corrected handoff point, the journal payload is the only durable copy of some of a turn's input.
`TurnRecord` persists the anchor, source event IDs, per-source prompts and revisions, `SourceEventMetadata` (sender, timestamp, discovery event), owner, requester, correlation, command result, history scope, and conversation target.
It persists nothing about attachments or media.
A coalesced batch of one caption and three images therefore replays from `TurnStore` as text alone, so any design in which `TurnStore` decides whether an adopted turn runs needs a normalized durable input snapshot first.

Red tests that must exist before contract 2 is implemented, in the order they should be written:

- Crash after the durable turn record commits and before the response task is created.
- Restart when the journal sources for that turn are no longer pending.
- A coalesced batch of text plus several media sources replays with its exact media, and the turn executes once.
  **Done for the media half** (`TestReplayFidelity`): a plain image keeps its MXC reference, an encrypted image keeps the key material that makes the reference openable, and a batch of three images plus a caption replays whole and in receipt order.
  The remaining half is that the replayed batch executes exactly one turn, which needs the handoff to exist.
- A durable-turn persistence failure leaves every journal source pending.
- A crash between adoption and journal settlement deduplicates the handoff instead of running twice.

### Invariants the delivery cutover must carry

These are behavioral contracts the current pipeline satisfies, restated against the owners that replace it.
They are gates on phase 3, not on the phases before it.

| Invariant | Owner after the cutover | State |
| --- | --- | --- |
| A continuation turn produces no final delivery between attempts, and exactly one after the last | Outbox | **Deferred.** The assertion becomes "no `FINAL` enqueue between attempts, exactly one after the last", and it cannot be written against the real path while `enqueue_delivery` has no production caller. Writing it against a spying `DeliveryGateway` now would pin the mechanism being deleted |
| Once the model result is durable, restart recovery never runs the model again | Outbox | **Held.** Crash boundaries six through nine assert `model_runs == 1` after recovery; boundary five, where nothing is durable yet, correctly re-runs |
| One stop converges to one durable terminal outcome and one visible cancellation | `UserStopReconciler` | **Held.** Pinned with a real `TurnStore` for a single stop, a redelivered stop, and two racing stops |

### What these contracts delete

| Mechanism | Why it goes |
| --- | --- |
| `release_terminal_turn_sources`, `_terminal_sources`, `turn_is_terminal` | Contract 2 moves the handoff to durable adoption, so no store needs to ask another whether a turn finished |
| The off-loop settlement wake in `bot.py` | Only needed because settlement happens after model execution |
| Retained `source_json` on context-only rows | Contract 3 compacts at projection time |
| Projection writes for self-authored streaming edits | Contract 4 makes them transport-only |
| `hydrated_from_ts`, unless incremental hydration is built | Contract 6 forbids recording a promise nothing honors |
| The full-surface `PrincipalStore` dependency | Contract 9 replaces it with narrow views |

### Gate check before more cutover work

- One pending-event worker and one outbox recovery path: **holds today.**
- No cache repair, certification, or gap machinery in the replacement: **holds today.**
- No duplicate "should this event run?" authority: **does not hold** until contract 2 lands, because the journal and `TurnStore` both currently gate execution.
- Bounded prompt reads: **holds today**, and contract 6 is what ties the bound to the actual requirement.
- Materially fewer production lines: **does not hold yet, and the earlier claim that it did was measuring a projection rather than the branch.**
  `git diff --numstat origin/main -- src/mindroom` currently reports +4,799 / −2,175, a net of **+2,624 production lines**.
  The replaced owners are still present and still required — the old cache and its read and trust machinery total roughly 15,800 lines — so nothing has been collected yet.
  The gate is only satisfiable after the read cutover and the delivery cutover both land and their old owners are deleted; until then this row should read "not yet", because a plan that grades itself on a projection is not measuring anything.

The edge cases that must each stay inside one owner are crash boundaries, edit-before-original, redaction-before-original, current-edit redaction, membership re-entry, sidecar plaintext, approvals, and send-acknowledgement-before-echo.
If handling one of them needs changes in several owners, the boundary is wrong and gets reconsidered rather than coordinated around.

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

**Sequencing correction found while starting phase 2.**
Phase 2 cannot precede phase 3, and the reason is the subject of section 3a below.
The cache does not only answer reads; it also writes MindRoom's own outbound messages directly, through `ThreadOutboundWritePolicy`, so a prompt assembled immediately after a send already contains it.
The journal learns of that message only when its sync echo arrives.
Moving reads onto the projection before outbound seeding exists would therefore build prompts that omit the assistant's own last turn — a silent context regression, and exactly the class of thing a passing test suite would not notice.
Phase 2's read work is unchanged, but it runs after phase 3.

**Where phase 2 stands, and the one decision it is waiting on.**

The seam is a single method, `ConversationResolver._read_thread_messages`, whose dispatch table maps four `ThreadReadMode` values onto four cache methods.
Those four collapse onto the reader's two: `ADVISORY_FULL` and `DISPATCH_SNAPSHOT` onto `read`, `DISPATCH_FULL` and `STRICT_FULL` onto `read_strict`.
Only four places construct `ConversationResolverDeps` (`bot.py` and three test modules), so injecting the reader is small.

The adapter that renders a projected page as a `ThreadHistoryResult` is written and tested (kept at `adapter.patch` alongside `test_projected_thread_history.py` in this session's scratch directory, not committed, because the repository rejects a function with no production caller and landing it separately would be exactly that).
It maps `logical_event_id` to `event_id`, `revision_event_id` to `latest_event_id`, `revision_ts` to `edited_timestamp` when the two differ, and derives `stream_status` through the existing `ResolvedVisibleMessage` constructor so a streaming answer cannot read as a finished one.
`is_full_history` is passed in by the caller rather than inferred, because a page that omitted a message and a conversation that never had one are indistinguishable from inside the adapter.

**Decision, now settled: the read limit is the hydration window.**
The worry was that this turns an unbounded read into a bounded one for every caller at once.
Measurement dissolves it: the consumers already truncate far harder than the window would.
Team prompts render `max_messages=30` (`_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS`), and the agent path is capped by `num_history_messages`, both applied in `_context_messages_from_visible_messages` as `messages[-max_messages:]`.
The hydration window is more than sixty times the larger of those, so bounding the read cannot change prompt content in any realistic conversation — it only stops materialising rows that are discarded a moment later.
A caller wanting more than the window was never served anyway, because that is exactly what hydration guarantees and no more.

**The production swap is written and measured. It is held at `read-cutover.patch` in this session's scratch directory, unapplied, because it leaves 44 tests failing and the branch must stay green.**

What the patch contains, all of it verified to import and to work on the resolver's own path:

- `ConversationHydrator` takes the runtime view instead of a client, with a `_client()` accessor identical to the delivery gateway's, which is what makes it constructible before login.
- `_ConversationReader` and `_StaleConversationError` become public; `HYDRATED_PROMPT_WINDOW_MESSAGES` loses its underscore.
- `projected_thread_history` lands in `conversation_reads.py` with four tests.
- `ConversationResolverDeps` gains `conversation_reader`, wired in `bot.py` from the journal store and a hydrator over the runtime view.
- `_read_thread_messages` loses its four-entry dispatch table: `strict = mode in (DISPATCH_FULL, STRICT_FULL)` selects `read_strict` or `read`, bounded by the hydration window.

The 44 failures are one shape, not forty-four: a test seeds thread history through `make_conversation_cache_mock`, the resolver now reads the projection, and the projection is empty.
They fall in nine files — `test_thread_context_resolution.py` (20), `test_turn_controller_focused.py` (6), `test_conversation_resolver.py` (5), `test_multi_agent_bot.py` (4), `test_turn_dispatch_pipeline.py` (3), `test_thread_mode.py` (2), `test_tach_split_matrix_client_boundaries.py` (2), and one each in `test_multi_agent_e2e.py` and `test_live_message_coalescing.py`.
The full list is in `cutover-failures.txt` beside the patch.

Migrating them by stubbing the reader would be the fast route and the wrong one: it would pin the projection's *absence*.
Seed the journal projection instead, through `store.admit(...)`, so these tests exercise the path that will actually run.
That is the next unit of work, and it should be done in one pass rather than interleaved with production changes.

**There are two migration shapes, not one.**
Tests built on a real `AgentBot` have a journal store, so they seed it with `seed_thread_history`
and let the real read path serve them. Tests built on a store-less unit harness -- `_Harness` in
`test_turn_controller_focused.py` constructs a `TurnController` directly with a mocked
`conversation_cache` and no bot -- have nothing to seed. Those get a fake reader returning a real
`ConversationPage`.

That is not the "stub the reader and pin its absence" mistake warned about below: the objection
there is to stubbing a reader that a real store sits behind, so the test passes whether or not
anything reached the projection. A harness with no store at all has no projection to reach, and a
fake reader is the honest analogue of the `conversation_cache` mock it already carries. The v2
patch currently gives these harnesses a bare `MagicMock()`, which will not survive contact with
the adapter -- it needs to return a constructed page.

**One thing that migration has to decide, and it is not mechanical.**
The current expectations are built with `_message(...)`, which calls `ResolvedVisibleMessage.synthetic` and therefore produces `thread_id=None` — even for messages the test fetched *by thread id*.
That was invisible while a mock returned the same objects the test constructed: the assertion compared a value against itself.
A projected row carries the real thread id, so `context.thread_history == expected_history` cannot hold by dataclass equality any more, and no amount of seeding will make it.

The expectations therefore have to change, which means deciding what they should have said.

**Checked against production, because the whole migration turns on it.**
`client_thread_history.py` and `client_visible_messages.py` both build their results with `thread_id=EventInfo.from_event(event.source).thread_id`.
The real read path has always populated the thread; only the test helper omitted it.
So these assertions were comparing the mock's own objects against themselves and said nothing about thread membership at all — the cutover is not breaking them so much as revealing them.

That also makes the migration mechanical rather than a redesign, which the earlier reading got wrong.
Give `_message` a `thread_id` parameter, seed through `store.admit(...)` with `origin_server_ts=0` and `content={"body": body}`, and every remaining field lines up: `synthetic` produces exactly what the adapter reconstructs.
Both pieces are written and included in `read-cutover-v2.patch` (`_message(..., thread_id=...)` and an async `seed_thread_history(bot, room_id, thread_id, messages)` in `threading_helpers.py`).

**`DISPATCH_SNAPSHOT` does change behaviour — but "make it strict" is the wrong fix.**

An independent triage classified two failures as real behaviour bugs rather than harness noise and
proposed making the dispatch snapshot a strict read. The first half is right and the remedy is not.

Reading the test settles it: `test_plain_reply_with_unproven_root_is_not_admitted_under_guessed_key`
injects `TimeoutError` into `get_dispatch_thread_snapshot` and requires that failure to stop a
reply being admitted under a guessed coalescing key. The old snapshot read the *homeserver* and
could time out. The projection read is a local query and essentially cannot, so the injection point
is gone. That is not a lost safety property; it is the property the projection exists to make
unnecessary.

What survives is the real risk, and it has a different shape. A non-blocking read of a conversation
that was never hydrated returns an incomplete page, and treating "no evidence" as "evidence of
absence" is how a root gets judged unproven and a key gets guessed. The failure mode moved from
*the read failed* to *the read was incomplete*.

Making the snapshot strict does address that, by hydrating before deciding — and reintroduces
exactly the homeserver dependency on the dispatch path that this design removes. The snapshot is
deliberately the non-blocking mode.

**Phase 2's production side is complete and verified. `read-cutover-v13.patch` is the state to start from.**

v13 = v8 plus the winning trigger, applied and measured in the real worktree rather than a scratch
copy: **17 full-suite failures, of which one is the known `test_attachments_tool` timing flake**,
so 16 real — matching the independent count. `test_live_message_coalescing` disappears from the
failure list entirely; both behaviour targets are fixed.

The six in `test_thread_context_resolution.py` are simply **not migrated yet** — confirmed, not
inferred. `test_extract_context_plain_reply_to_thread_reply_inherits_existing_thread` builds its
`expected_history` with bare `_message(event_id=..., body=...)` calls and never calls
`seed_thread_history`, so the expectation carries `thread_id=None` and `timestamp=None` while the
projection returns neither.

They were missed because the earlier migration pass was regex-driven and matched only the
`with patch.object(... "get_thread_history" ...)` block shape. These six use other shapes. The six:

- `test_extract_context_plain_reply_to_thread_reply_inherits_existing_thread`
- `test_extract_context_plain_reply_chain_stays_threaded_transitively`
- `test_extract_context_plain_reply_to_promoted_plain_reply_stays_threaded`
- `test_dispatch_room_demotion_clears_source_and_resolved_thread_ids`
- `test_degraded_dispatch_history_uses_strict_history_before_policy`
- `test_full_history_thread_resolution_uses_full_history_to_prove_root`

The fix is the pattern already applied nine times in that file: give each `_message()` its
`thread_id`, then `await seed_thread_history(bot, room_id=..., thread_id=..., messages=expected_history)`
before the call under test. The seeder stamps the ordinals, so expectation and projection agree.

All 16 remaining are v8 fixture migrations with per-test categories in `CODEX_REVIEW10.md`:
6 `test_thread_context_resolution`, 3 `test_turn_dispatch_pipeline`, 2 `test_multi_agent_bot`,
2 `test_thread_mode`, and one each in `test_multi_agent_e2e`, `test_turn_controller_focused`,
`test_tach_split_matrix_client_boundaries`.

The trigger itself:

```python
mode.dispatch_safe
and source_event_id is not None
and await reader.may_have_unread_history(room_id=..., thread_id=..., source_event_id=...)
```

backed by `has_other_admitted_room_event(..., excluding=source_event_id)` on the journal. The
excluding clause is what makes it work: production admits the source event before the callback
runs, so at proof time a real room has another admitted event and a fresh room does not.

**Superseded: resolved, and not by me — the trigger is in `CODEX_REVIEW12.md`, 18 to 16.**

```python
mode.dispatch_safe
and source_event_id is not None
and not conversation_is_hydrated(room_id, candidate_thread_id)
and has_other_admitted_room_event(room_id, excluding=source_event_id)
```

Both target failures gone, no regressions, two new backend contract cases passing. The exact diff
is in that review.

The load-bearing clause is `excluding=source_event_id`. "The room has other admitted events" was a
lead I raised and then **refuted incorrectly**: I checked whether the two tests *seed* anything,
saw neither did, and concluded the axis was empty on both sides. But production admits the source
event before the callback runs, so at proof time the target room does have another admitted event
and a genuinely fresh room does not. I compared test setup where I should have compared runtime
state.

`read-cutover-v11.patch`'s `page.refresh_pending` trigger is still correct and still costs nothing
(18, no regressions) — it is just not sufficient on its own, because a redacted revision is a
narrower condition than an unread room. Whether to keep both is a judgement: `refresh_pending` is
the honest signal for *known-missing* content, and the clause above is the signal for *unread*
content. They answer different questions and the proof path arguably wants both.

**Superseded: the trigger is `page.refresh_pending`.** That was safe but insufficient.

| Attempt | Trigger | Full suite |
| --- | --- | --- |
| baseline (v8) | — | 18 |
| v9 | `complete is False` | 19 |
| v10 | conversation not hydrated | 24 |
| **v11** | **`bool(page.refresh_pending)`** | **18** |

v11 costs nothing and is the only one that is *semantically* right. The two rejected triggers both
tried to infer incompleteness from ambient state; `refresh_pending` is the projection stating it
outright — a message is present but its visible revision was redacted and not yet refetched, so
absence of a child is not proof of absence. A fresh room has nothing pending and stays undegraded,
which is exactly what v10 got wrong.

It does **not** make the two target tests pass, and that is the finding, not a shortfall. They
inject `TimeoutError` into a cache method that no longer exists. A local read cannot time out, so
no trigger can satisfy them as written.

The rewrite is reachable through the ordinary admission API with no mocking, and the recipe is
verified: admit a message, admit an edit of it, then admit a redaction of the *edit*. Probe output
against the real store:

```
messages: []
refresh_pending: 1
```

**The rewrite is started in `read-cutover-v12-test-rewrite-wip.patch` and is not finished.**
It carries v11 plus a `_leave_root_awaiting_refetch()` helper in `test_live_message_coalescing.py`
that admits a message, an edit, and a redaction of that edit through the real store, replacing the
`TimeoutError` injection. The test now reaches its own assertion and fails on
`DID NOT RAISE RuntimeError` rather than on a missing name, so the plumbing is right and the
condition is not yet reproduced in that flow.

Two things to check first, both unverified: whether the event IDs the helper seeds are the ones the
reply actually targets (`root_response()` derives the root from the reply's `in_reply_to`, and the
helper is currently called with a hard-coded `$root:localhost`), and whether the degrade signal
reaches `_thread_messages_root_proof()` on the coalescing path at all, which is worth a print
before more edits.

Two mechanical notes that cost time: `ruff --fix` moves `InboundEvent`/`ProjectedEvent` into a
type-checking block even though the helper constructs them at runtime, so that import needs
`# noqa: TC001`; and the helper must be inserted above the `@pytest.mark.asyncio` decorator, not
above the `async def`.

So the page comes back with the message withheld rather than shown stale, and `refresh_pending`
non-empty — precisely the state v11 degrades on. Build that, drop the timeout injection, and keep
the existing assertion that the reply is not admitted under a guessed key. That assertion is the
behaviour under test and survives the rewrite intact.

So phase 2's production side is now complete in v11. What remains is 18 fixture failures, of which
these two need rewriting rather than repair, and the other 16 are categorised per-test in
`CODEX_REVIEW10.md`.

**Superseded: both candidate conditions are too broad; the marker is right and the trigger unknown.**

| Attempt | Condition | Marker | Full suite |
| --- | --- | --- | --- |
| baseline | — | — | 18 |
| v9 | `complete=False` | flag only | 19 |
| v10 | conversation not hydrated | source + flag | 24 |

v10 does fix the two target tests and `test_history_summary_call`, so the *marker* is settled: the
proof path reads `THREAD_HISTORY_SOURCE_DIAGNOSTIC`, not the boolean. What it gets wrong is the
trigger. Six new tests fail with `ThreadMembershipLookupError: Could not resolve canonical
coalescing thread`, and the reason is straightforward once seen: a brand-new room is legitimately
un-hydrated and legitimately not a thread. Reporting "proof unavailable" there breaks the ordinary
first-message path.

That lead is refuted: both the target test and the one v10 broke use `_make_bot(tmp_path)` and
seed nothing, so "the room has admitted events" is empty on both sides and cannot discriminate.

Which reframes the problem usefully. The old signal was an *explicit* one — the read failed. The
projection has no automatic equivalent, and both attempts so far tried to infer one from ambient
state (strictness, hydration). But the page already carries an explicit statement of known-missing
content: `ConversationPage.refresh_pending`, non-empty exactly when a message is present but its
visible revision was redacted and not yet refetched.

So the next candidate — untested, but different in kind from the two that failed — is
`degraded = bool(page.refresh_pending)`. `projected_thread_history` already computes
`complete and not page.refresh_pending`, so the value is in hand. It is narrow by construction: a
fresh room has nothing pending and stays undegraded, which is what broke v10.

Note this may also mean the two target tests cannot be made to pass by a trigger alone. They
inject a *timeout*, and if the projection's only honest incompleteness signal is a pending refresh,
the tests have to be rewritten to create that state rather than to fail a read. Establish what the
rewritten test should assert before hunting further for a trigger that satisfies the current one.

So the condition is narrower than either "not strict" or "not hydrated". It is something like
"this conversation may have history we have not read" — an un-hydrated room that the bot has been
a member of, as distinct from one it has just joined or created. Whatever expresses that is the
trigger; the store knows membership epochs and the projection knows whether anything was ever
admitted for the room, so the information is probably there.

Both attempts are kept — `read-cutover-v9-degraded-toobroad.patch` and
`read-cutover-v10-hydration-gate.patch` — so the next person can see two dead ends rather than
rediscover them. `read-cutover-v8.patch` remains the best state at 18 failures.

**Superseded: the fix is diagnostic-only after all — but with the source marker, and gated on hydration.**

Two independent corrections landed on this, and together they specify it:

A runtime probe of `_thread_messages_root_proof()` shows it consults
`is_thread_history_source_degraded()`, which recognises `thread_read_source == degraded`. It does
*not* consult `THREAD_HISTORY_DEGRADED_DIAGNOSTIC`:

```
diagnostics={thread_read_degraded: true}                              => NOT_A_THREAD_ROOT
diagnostics={thread_read_degraded: true, thread_read_source: degraded} => PROOF_UNAVAILABLE
```

`NOT_A_THREAD_ROOT` is what lets coalescing resolve room-level and admit under a guessed key, so
the marker has to be `THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED`. Setting
only the boolean flag changes nothing on that path.

Note also that the resolver guard cited earlier (post-v8 lines 678-702 and 723-730) escalates to
`STRICT_FULL`, which hydrates through Matrix — so it is not the non-blocking demotion it was
described as. The non-blocking fix is to make an incomplete projected root proof return
`PROOF_UNAVAILABLE` *before* room-level classification, which the source marker achieves.

And the condition is hydration, not strictness — see the measurement below.

**Superseded in part: the one-liner as stated is wrong, and the measurement is the proof.** Marking every
non-complete page degraded takes the full suite from 18 failures to 19: it breaks
`test_history_summary_call.py` and does not fix
`test_plain_reply_with_unproven_root_is_not_admitted_under_guessed_key`.

The reason is a conflation. `complete=False` means "this was an advisory read", which
`ADVISORY_FULL` does deliberately and constantly — an advisory read is *supposed* to be
incomplete and is not degraded. The condition that actually matters is "this conversation was
never hydrated", which is a different fact and one the reader can ask the store for
(`conversation_is_hydrated`). Degrade on that, not on non-strictness.

The attempt is kept at `read-cutover-v9-degraded-toobroad.patch` so the next person can see the
shape without repeating it.

The guard the fix needs already exists.
`conversation_resolver.py:707` reads `if mode.dispatch_safe and is_thread_history_degraded(...)`,
which is exactly the "do not decide on incomplete evidence" rule the timeout used to trigger.
`is_thread_history_degraded` consults the diagnostics dict, and `projected_thread_history` never
sets it — so an incomplete projected page looks perfectly healthy and the guard never fires.

Have `projected_thread_history` mark a page degraded when `complete` is false (the same
`THREAD_HISTORY_DEGRADED_DIAGNOSTIC` the cache set), and the existing demotion path does the rest.
No mapping change, no blocking read on the dispatch path, and the safety property comes back in the
form the projection can actually express.

Rewrite the two tests to inject incompleteness rather than a timeout: a timeout is no longer
something a local read can do, and a test that keeps injecting one is asserting against a
mechanism that no longer exists.

`read-cutover-v8.patch` (854 lines) is the current state at 18 failures.
`CODEX_REVIEW10.md` has a per-test category and minimal fix for all 21 and is worth reading — note
that several tests should end up asserting `requires_model_history_refresh=True`, since an advisory
read is intentionally not complete.

An independent triage of all 21 failures classified two of them as genuine behaviour bugs rather
than harness noise: `test_dispatch_candidate_without_proof_history_demotes_without_retry` and
`test_plain_reply_with_unproven_root_is_not_admitted_under_guessed_key`. Both say the same thing —
the dispatch snapshot read must be **strict**.

The swap maps `strict = mode in (DISPATCH_FULL, STRICT_FULL)`, so `DISPATCH_SNAPSHOT` takes the
non-blocking `read`. That is wrong where the snapshot decides *thread membership*: a non-blocking
read can return an incomplete conversation, an unproven root is then treated as proven or demoted
on incomplete evidence, and a reply gets admitted under a guessed coalescing key. The second test
asserts an exact `RuntimeError` for precisely that case.

Fix the production mapping before any more of the fixtures. Two of the twenty are load-bearing and
the rest are downstream noise, which is exactly the trap of grinding a failure list from the top.

`read-cutover-v8.patch` (854 lines) is the current state at 18 failures: v7 plus
`test_multi_agent_bot`'s bare `AsyncMock` replaced with `_make_matrix_client_mock()`, which fixed
two of its four by letting hydration succeed.

The triage also gives a per-test category and minimal fix for all 21 in
`CODEX_REVIEW10.md` — worth reading before touching any of them, since several need
`requires_model_history_refresh=True` rather than the value they currently assert (an advisory read
is intentionally not complete).

**Superseded: hydration is the cause and the way to see it is to instrument the reader.** That was
right for the four `test_multi_agent_bot` failures and is now fixed; it was not the whole story.

```
READ_STRICT RAISED _HydrationError:
  Could not fetch thread root '$thread_root_id': <AsyncMock name='mock.room_get_event()'>
```

Obtained by monkeypatching `ConversationReader.read_strict` to print and re-raise, then running the
test with `-s`. Reasoning about it produced two wrong answers first; one print produced the right
one in a single run.

The confusion was that `make_matrix_client_mock` *does* wire `room_get_event`,
`room_get_event_relations`, and `room_messages` correctly (`tests/conftest.py:833-839`) — but
`test_multi_agent_bot` does not use that helper. Its bot carries a bare `AsyncMock`, so
`room_get_event` returns a mock rather than a `RoomGetEventResponse`, `_fetch_thread` raises, and
the turn ends with no response and nothing in the log.

The fix is therefore per-harness, not global: every bot whose test drives a *threaded* turn needs
either `make_matrix_client_mock` or a pre-hydrated conversation. Check each failing file for which
client mock it builds before assuming the shared helper covers it.

Note the shape of this bug for the remaining triage: a hydration failure surfaces as a turn that
silently does nothing, which is indistinguishable from a behaviour regression until you look. The
same instrumentation will answer the rest.

**Superseded twice: first "the real blocker is hydration", then "that explanation is wrong". The
first was right; the retraction was checked against `make_matrix_client_mock`, which this test
does not use.**

The note that follows claims those tests fail because a strict read hydrates against a mocked
client. That was checked afterwards and does not hold: `make_matrix_client_mock` already wires
`room_messages` to a real empty `RoomMessagesResponse` with no end token (exhaustion),
`room_get_event` to a real `RoomGetEventResponse`, and `room_get_event_relations` to an empty
async iterator (`tests/conftest.py:833-839`). Hydration therefore succeeds in those tests.

Adding `install_hydrated_conversation` to `seed_thread_history` is still right on its own terms —
a seeded conversation should be a known one — and it is kept in v7. But it moved the total only
from 21 to 20, and the reason those four tests produce no response is still undiagnosed. Two
hypotheses have now been advanced and both were wrong; the next person should instrument the turn
rather than reason about it, because the failure is silent and reads like a behaviour regression
while the surrounding fixture noise makes it easy to assume otherwise.

That question — is any of the remaining 20 a real behaviour change rather than a fixture artifact —
is the one thing worth answering before any more of them are "fixed".

**Superseded and incorrect: the real blocker is hydration, not fixture equality.**

This is the thing to understand before touching the rest. Under the cutover a strict read calls
`ensure_hydrated` before it answers, and hydration talks to Matrix. Every bot in the test suite has
a mocked client, so `room_messages` returns a mock rather than a `RoomMessagesResponse`,
`_fetch_room` raises, and the turn produces nothing — with no visible error, which is why
`test_multi_agent_bot` fails with "Expected 'stream_agent_response' to have been called once.
Called 0 times" and reads like a behaviour regression rather than a harness problem.

v7 adds `install_hydrated_conversation` to `seed_thread_history`, which fixes it for the tests that
seed. It only moved the total from 21 to 20, because the tests that hurt most never call the
seeder: their old stubs returned an *empty* history, so the earlier survey concluded they needed no
seeding. That conclusion was right about the data and wrong about the consequence — an empty
conversation still has to be a *known* one.

So the remaining work is mostly one change in the shared bot fixture, not per-test edits: a bot
constructed for tests should have its conversations marked hydrated, so a strict read answers from
the projection instead of reaching for a mocked homeserver. `install_runtime_cache_support` in
`tests/conftest.py` is the natural place. Do that before grinding through individual files —
the per-file counts below are mostly downstream of this one cause.

**Superseded: `read-cutover-v6.patch`, 33 failures down to 21.**

It contains v5, the seeding-ordinal fix, and the mechanism assertions removed properly. Remaining:
8 in `test_thread_context_resolution.py`, 4 in `test_multi_agent_bot.py`, 3 in
`test_turn_dispatch_pipeline.py`, 2 in `test_thread_mode.py`, and one each in
`test_live_message_coalescing.py`, `test_multi_agent_e2e.py`, `test_turn_controller_focused.py`,
`test_tach_split_matrix_client_boundaries.py`.

Removing the assertions worked once done syntactically: parse the module, find `Expr` statements
whose call attribute is one of `assert_awaited*`/`assert_not_awaited` with a root `Name` starting
`mock_`, and `Assert` statements whose test mentions `await_args` on such a name, then drop those
`lineno..end_lineno` ranges. That removed 34 calls and 12 asserts cleanly. Regex on the same job
corrupted the file twice, because the calls span lines and deleting the first one strands its
arguments.

The 8 left in that file are a different shape: expectations built with `make_visible_message()`,
which defaults `thread_id` to `None` and leaves `timestamp` unset, compared by
`ThreadHistoryResult.__eq__` against projected messages that carry both. Give those expectations
the thread and the ordinal, exactly as the `_message()` call sites already do. The other 13 across
five files are still unexamined.

**Superseded: two of the three remaining problems are understood; do not remove assertions with a regex.**

The ordering cause is fixed by a three-line change to `seed_thread_history`: enumerate the
messages and use the position as both `origin_server_ts` and `message.timestamp`. Callers pass the
very list they later assert against, so setting the field on those objects keeps expectation and
projection in agreement, and the conversation reads back in the order it happened rather than
alphabetically by event ID. With that alone `test_thread_context_resolution.py` drops from 20
failures to 12.

The remaining 12 are all `mock_fetch.assert_awaited_once*` and `await_args.args` assertions on the
cache methods being deleted. They must go, but **not** by regex: the calls span multiple lines,
and deleting only the first line leaves an orphaned argument block that turns into an
`IndentationError` and then an unmatched `)`. Two attempts at this corrupted the file badly enough
to need `git checkout`. Delete each call as a whole statement — an editor that understands the
syntax, or `ast`-based removal, not line patterns.

**Superseded: the swap is not one test from green. Use `read-cutover-v5.patch`.**
That earlier claim came from running `tests/test_conversation_resolver.py` alone and reporting it
as the suite. With v5 applied the resolver file does pass, and the full suite has **33** failures:
20 in `test_thread_context_resolution.py`, 4 in `test_multi_agent_bot.py`, 3 in
`test_turn_dispatch_pipeline.py`, 2 in `test_thread_mode.py`, and one each in
`test_live_message_coalescing.py`, `test_multi_agent_e2e.py`, `test_turn_controller_focused.py`,
and `test_tach_split_matrix_client_boundaries.py`.

v5 also fixes a real bug in v4: `_resolver()` accepted a `conversation_reader` argument and then
ignored it, always passing the empty reader.

**The 20 are one cause, and it is a defect in `seed_thread_history`, not in the tests.**
Seeding preserves each message's timestamp, and `_message()` builds them through
`ResolvedVisibleMessage.synthetic`, which sets `timestamp=0`. So every seeded message shares
`created_ts=0`, the page order falls back to `logical_event_id`, and `$thread_msg` sorts before
`$thread_root` — the reverse of the conversation order the expectations assert.

Fixing it needs distinct, increasing creation times, and that reaches the expectations too,
because the adapter maps `created_ts` onto `ResolvedVisibleMessage.timestamp` and the assertions
compare whole message objects. Either give `_message()` a timestamp parameter and set increasing
values at each call site, or have `seed_thread_history` assign ordinals and relax the comparison
to the fields that carry meaning. The first keeps the assertions strict and is preferable.

Do not "fix" this by sorting the expectation to match the projection. The order a conversation
reads back in is the thing under test.

**Superseded: use `read-cutover-v4.patch`, not v3.** It carries everything v3 had plus the two resolutions
below, and is one failing test from green. Apply with `git apply --exclude='docs/*'`.

`test_reply_to_candidate_retries_strictly_after_degraded_dispatch_proof` is **deleted** in v4, and
that was a judgement rather than a fix. It stubbed a degraded dispatch read and a proving strict
read and asserted the resolver retried the second. Both modes now route to `read_strict`, so the
degraded-then-retry sequence it observes no longer exists — that backoff is the machinery being
deleted, and the projection has no degraded mode. Supplying it a page would have left it asserting
a retry that never happens. The scenario it covered is the test immediately above it.

`test_reply_to_proven_thread_root_joins_that_thread` still fails in v4 and is the last thing
standing. An independent review reproduced the same two failures in a scratch copy and traced it,
so the remaining step is mechanical:

Root proof is `any(message.event_id != thread_root_id)` (`matrix/thread_membership.py`), so the
page must contain the **child**, not the root. The projected child must carry `thread_id=_PARENT`;
the adapter preserves that field and maps `logical_event_id` to `event_id`. A root-only page
proves nothing. If the test is meant to model an ordinary non-redacted-root read rather than the
minimum proof page, the faithful page is `_PARENT` followed by `$child:localhost`, and the exact
event-ID assertion should be strengthened to match — a root-only page is wrong under either
reading.

An earlier version of this note said both tests merely need "a page containing the thread root".
That was wrong twice over: the first needs the *child* tagged into `_PARENT`, and the second
cannot retain its retry meaning through any page at all.

If the deleted retry test is instead kept and rewritten, its expected child must also be built
with `thread_id=_PARENT` — `make_visible_message()` defaults that field to `None` while
`ThreadHistoryResult.__eq__` compares whole message objects, so leaving it unset fails an equality
assertion that is otherwise correct. Correcting the fixture strengthens that assertion rather than
weakening it.

Do not make either pass by relaxing an assertion, faking a degraded page, or restoring
cache-method call assertions. Proving that a reply to a thread root joins that thread is the
behaviour this whole read path exists to get right.

**Superseded: the swap was two tests from green in `read-cutover-v3.patch`.**
Applied to head `2d91a4919` it leaves the full resolver suite at two failures, both in
`test_conversation_resolver.py`: `test_reply_to_proven_thread_root_joins_that_thread` and
`test_reply_to_candidate_retries_strictly_after_degraded_dispatch_proof`.

Those two prove thread membership by finding the reply target inside the returned history, so the
empty fake reader the other store-less harnesses use is not enough — they each need a page
containing the thread root. That is the only work left in the patch. Everything else is done: the
adapter, the public `ConversationReader`/`StaleConversationError`/`HYDRATED_PROMPT_WINDOW_MESSAGES`
names, the `conversation_reader` dependency and its `bot.py` wiring, the four-mode dispatch table
collapsed to `strict = mode in (DISPATCH_FULL, STRICT_FULL)`, the fake readers in the three
store-less harnesses, and the removal of the three `assert_awaited_once_with` calls that asserted
the cache method being deleted.

Do not re-add those awaited-once assertions. They pin the mechanism, not the behaviour, and the
returned history is what carries the meaning.

**The two remaining tests are not the same problem, and the second is not mechanical.**

`test_reply_to_proven_thread_root_joins_that_thread` is straightforward: its fake reader needs
`read_strict` to return a page containing `$child:localhost` in the `$PARENT` thread, because
`DISPATCH_FULL` now routes there. Nothing about what it asserts changes.

`test_reply_to_candidate_retries_strictly_after_degraded_dispatch_proof` is different. It stubs
`get_dispatch_thread_history` to return a *degraded* empty result and `get_strict_thread_history`
to return the proving child, and asserts the resolver retries the second after the first comes
back degraded. Under the cutover both `DISPATCH_FULL` and `STRICT_FULL` map to `read_strict`, so
there is no longer a degraded-then-retry sequence to observe — that backoff is precisely the
machinery being deleted, and the projection has no degraded mode.

So that test cannot be repaired by supplying a page. It has to be rewritten to assert the outcome
it actually cares about — a reply to a proven thread root resolves to that thread even when the
first read is unhelpful — or deleted as covered by the test above it. Decide which deliberately;
supplying a page that makes it pass would leave it asserting a retry that no longer happens.

**Measured again, and much smaller than the stub count suggested.**
Counting `patch.object` sites overstated the work by roughly an order of magnitude, because most
stubs return an *empty* history — and the projection returns empty by default too, so those tests
match after the swap with no seeding at all. Their stubs simply become unnecessary.

Across the six remaining files, exactly **one** stub carries messages
(`test_multi_agent_bot.py`); `test_multi_agent_e2e.py`, `test_live_message_coalescing.py`,
`test_turn_dispatch_pipeline.py`, `test_thread_mode.py`, and `test_turn_controller_focused.py`
have none. `test_thread_context_resolution.py` held nine and is fully migrated.

So the remaining migration is one seeded conversation plus the fake reader those store-less
harnesses need — not the sixty-site sweep the earlier inventory implied. The count below is
retained because it is still the right map of *where the stubs are*; it is the wrong measure of
how much has to change.

**Size, measured rather than estimated.**
In `test_thread_context_resolution.py` alone: 51 `patch.object(bot._conversation_cache, ...)` sites, of which 10 are the simple `get_thread_history` shape, and 36 references to `mock_fetch`.
Most of those 51 patch methods that survive the cutover (`get_thread_id_for_event`, `get_event`) and must be left alone — only the history reads move.
The `mock_fetch.assert_awaited_once()` and `mock_fetch.await_args.args` assertions have no replacement and should simply go: they assert that a particular cache method was called, which is the mechanism being deleted, and the returned history is what carries the meaning.

Use `read-cutover-v2.patch` rather than the first one; it is rebased over the narrow-views commit and carries the two helpers.
Apply with `git apply -3 --exclude='docs/*'`.
Note that the patch also reverts the hydrator to taking a client; the runtime-view change has to be redone on top, since later commits touched the same lines.

**Full inventory of what the migration has to touch.**
Two of the 44 failures are structural rather than behavioral — `test_tach_split_matrix_client_boundaries.py` wants explicit tach modules for the two new import edges (`conversation_reads` to `client_visible_messages`, `conversation_hydration` to `runtime_protocols`). Fix those by editing `tach.toml`, not the tests.

The remaining 42 are history stubs in three idioms, all of which become a `seed_thread_history(...)` call:

| Idiom | Example |
| --- | --- |
| Attribute assignment on a cache mock | `conversation_cache.get_dispatch_thread_history.return_value = ...` |
| `patch.object` on the bot's cache | `patch.object(bot._conversation_cache, "get_dispatch_thread_snapshot", AsyncMock(...))` |
| Class-level decorator | `@patch("mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history")` |

Stub-site counts per file, which exceed the failure counts because some stubs feed tests that never read the history: `test_thread_context_resolution.py` 20 (of 51 total cache patches), `test_thread_mode.py` 13, `test_multi_agent_e2e.py` 12, `test_live_message_coalescing.py` 5, `test_multi_agent_bot.py` 4, `test_turn_dispatch_pipeline.py` 4, `test_turn_controller_focused.py` 2.
Only the ones whose test actually asserts on history need to move; the rest can simply lose the stub.

**Why the reader could not simply be a value in the deps.**
Neither `ConversationHydrator` nor `_ConversationReader` was built anywhere in production; they existed and were tested, but only tests instantiated them.
The hydrator needed a `nio.AsyncClient`, and the bot does not have one when its collaborators are assembled — the client arrives at login, and `bot.py` sets `self.client` later.

Resolved by following the precedent already in the tree: the delivery gateway holds the runtime view and reads `runtime.client` at call time, raising if it is not ready.
The hydrator now does the same, so it is constructible at assembly and only requires a client when a read actually happens.

### 2. Conversation projection cutover

Add latest-visible projection storage, bounded reads, and one-time thread and room hydration.

Change conversation resolution, reply lookup, reaction lookup, stale-stream cleanup, hooks, and streaming thread targeting to use the bounded projection API.

Before the implementation PR can merge, remove the Matrix cache package, conversation-cache read variants, room-scan thread history and repair, cache trust, cache certification, advisory outbound cache writes, and cache write coordination.

Reduce checkpoint publication to successful durable admission plus nio's exact unrecovered-room result.

Do not preserve deleted cache interfaces to keep implementation-specific tests green.

#### Cached facts the projection does not yet own

An audit of what the Matrix cache persists against who consumes it found two durable facts that the latest-visible projection cannot represent as designed.
Both must have a named owner before the cache package is deleted, because deleting it otherwise removes a behavior rather than replacing it.

**Resolved sidecar text.**
`_download_mxc_text` in `src/mindroom/matrix/message_content.py` downloads, decrypts, and durably caches the plaintext behind an MXC reference, so rebuilding a conversation does not refetch and re-decrypt the same oversized message every time.
The projection stores the Matrix content, which holds the reference and not the resolved text.
Attach the resolved text to the visible revision as one nullable value, cleared whenever the revision changes: an edit, a redaction, or a membership epoch advance all invalidate it, and a value that outlived any of those would serve the wrong body.

**Tool-approval card recovery.**
`ApprovalManager._cached_trusted_pending_approval_for_card` in `src/mindroom/approval_manager.py` reads an arbitrary `io.mindroom.tool_approval` event and its edits from the cache to recover pending approval state after a restart.
Neither new owner can substitute for it: `visible_messages` models conversation messages, and the journal clears a settled event's payload on purpose.
Give approvals their own small durable projection, owned by the approval subsystem, rather than widening either.

### 3a. How MindRoom's own messages reach the conversation

Sync is the only source of conversation content, including MindRoom's own messages: the Client-Server API returns a client's own events in its timeline, carrying the transaction ID that sent them.
There is therefore no separate outbound ingestion path, and the advisory post-send cache notifications are deleted rather than reimplemented.

For a turn triggered by a room event, waiting for the echo costs nothing: the room timeline orders MindRoom's own message before the user's next one, so by the time the follow-up is admitted its own echo already has been.
The gap is elsewhere — a turn that reads the conversation after sending within the same turn, and a turn no room event triggered at all, such as a scheduled task or a todo poke.
Those would read a room they have already spoken in as one they have not, and `read_strict` cannot express "wait for my own echo": it can only wait on a refresh token.

The initial and final deliveries therefore seed the projection at acknowledgement, in the same transaction that records the acknowledgement.
This costs at most two writes per turn, because intermediate streaming edits never enter the outbox at all.

A seeded row is explicitly **provisional**, and the plan must not pretend otherwise.
A Matrix send response carries only `room_id` and `event_id` (`nio.RoomEventIdResponse`), while the projection orders messages and revisions by `origin_server_ts`, which only the server knows.
Seeding therefore stores provisional ordering metadata, and the self-authored sync echo replaces it with the authoritative values.
That replacement has to be written deliberately: `_project_original` currently inserts `ON CONFLICT DO NOTHING`, so today an echo could not correct a seeded row at all.
Once the authoritative values are installed, any repeated echo is a genuine no-op.

The accompanying rules:

- A self-authored event updates the projection and must never create a second semantic turn.
- Code editing a message it just sent uses the event ID from the send response, never a projection read.
- Recovered events use the same ingestion path as live ones.
- An outbound redaction takes effect immediately on acknowledgement, because MindRoom must stop serving deleted content without waiting for a round trip.

**Landed, and now superseded.** `send_text` and `edit_text` both seed on acceptance, which covers blocking answers and streamed ones — a streamed answer reaches its final text by editing, so seeding only the original would have a turn that reads immediately afterwards see the placeholder.

> **Superseded by the audit below (2026-08-06).** Provisional seeding is to be
> deleted: the sync echo is the only route into conversation content. The rest
> of this section describes machinery that is being removed, and is kept
> because the ordering hazards it documents are what the echo-ordering tests
> now pin. Read it as a record of why the mechanism was hard, not as a
> specification to build against.

The two halves need different mechanisms, and the reason is worth stating.
For an original the whole row is provisional, so the echo replaces it, guarded by a `provisional` column.
For an edit only the *time* is provisional: which logical message it revises is certain, so it takes the ordinary edit path.
What is not certain is ordering, and revisions are ordered by timestamp — so a bot whose clock runs ahead would install a revision stamped in the future and every genuine later edit would lose the comparison, freezing the answer at whatever it said first.

The echo of a seeded revision is recognised by identity rather than by a flag: it is already the installed `revision_event_id`, so it is not a competitor to compare against but the authoritative account of the revision already shown, and it is installed unconditionally.
Both hazards are pinned — `test_a_seed_from_a_fast_clock_does_not_outrank_later_edits` for originals and `test_the_echo_of_a_seeded_edit_replaces_its_guessed_time` for revisions — and both tests fail if the comparison is restored.

Not every outbound message is a two-stage response delivery.
Approval cards, Matrix-tool messages, and summaries have no turn and stage, so they are not seeded: they reach the conversation through sync like any other event.
Approval cards additionally need the durable projection described above, because their state must survive a restart and the journal clears a settled event's payload.

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

## Audit: is durable outbound seeding load-bearing? (2026-08-06)

Reviewed after a request to make the Matrix sync echo the sole authoritative
route for outbound conversation content and delete provisional seeding.

### The architectural argument holds

Self-authored echoes already reach the projection through ordinary ingress.
`_event_class_for()` in `matrix/journal_ingress.py:91-101` classifies purely by
nio provenance, not by sender: a LIVE echo is `ACTIONABLE` and is admitted and
projected like any other event. What stops it from starting a turn is the
downstream echo drop in `ingress_validation.py`, not an ingress-level discard.

That matters because the ordering question answers itself. A later user message
and this bot's own echo arrive on the same timeline, ordered by the server. If
the user's message came after our send, the echo precedes it, so by the time the
user's turn is resolved the echo is already in the projection. Nothing needs to
be seeded for a *later* turn to see a *previous* answer.

Seeding is therefore only load-bearing for a read that happens before any sync
at all -- a genuine same-execution read-after-send.

### Two such readers do exist

The claim that no workflow reads after sending is too strong.

- `thread_summary._load_thread_history()` runs from a background task queued in
  `post_response_effects._queue_thread_summary()` immediately after delivery,
  and reads full history.
- The agent tool surface exposes both `_message_send_or_reply` and
  `_message_read` in `custom_tools/matrix_conversation_operations.py`, so one
  model run can send and then read.

Neither is ordered by the timeline, so seeding does not make either *correct* --
it only makes the just-sent message visible sooner. For a thread summary,
missing the newest reply degrades one summary and the next one recovers it.

### A cost of seeding, against it

`DeliveryGateway` calls `outbound_projection.record_sent()` on every send and
every edit (`delivery_gateway.py:533` and `:580`), including the "Thinking..."
placeholder. Transient UI state therefore enters the durable conversation record
and stays until the echo or the final edit supersedes it.

### Current surface

`matrix/outbound_projection.py` is 85 lines; `provisional` appears 44 times
across `src/mindroom`, 32 of them in `event_journal/projection.py` and 7 in
`event_journal/schema.py`. Tests naming `provisional`, `seed_outbound`, or
`OutboundProjection`: `test_outbound_projection.py`, `test_turn_store.py`,
`test_event_journal_store.py`, `test_turn_policy.py`, `test_team_mode_decision.py`,
`test_response_runner_agent.py`.

### Conclusion (authoritative)

Removal is justified, and the ordering guarantee -- not the absence of
read-after-send callers -- is the reason. Prove it first with the echo-ordering
tests before deleting anything, and decide explicitly what `thread_summary` is
allowed to miss rather than letting seeding hide the question.

**This decision overrides the "Landed" note earlier in this document**, which
described seeding as the mechanism for making a bot's own answer readable. Where
the two disagree, this section wins. Concretely:

- The sync echo is the only route into conversation content.
- Provisional seeding and its ordering machinery are deleted: `seed_outbound`,
  `OutboundProjection`, `SeedingView`, and the `provisional` /
  `revision_provisional` columns.
- The open "a rejected revision is never reconsidered" defect (task 11) is
  caused by provisional seeding and disappears with it, rather than needing its
  own fix.
- Reads issued in the same execution that sent a message are documented as
  echo-ordered, not read-your-writes. A turn that must know its own last message
  uses the event ID from the send response.

The echo-ordering tests landed first, as required: `TestEchoOrdering` in
`tests/test_journal_ingress.py` pins that a bot's own message is admitted and
ordered against the traffic around it, so deletion is now unblocked.

## Blocking: the projection serves truncated bodies for sidecar'd messages (2026-08-06)

Raised by the operator: most agent messages exceed the Matrix message size
threshold and are stored through the v2 large-message sidecar, so their event
content carries a preview body plus an MXC reference rather than the full text.

The read cutover exposed this. Both old read paths resolve the sidecar while
building a `ResolvedVisibleMessage` -- `client_thread_history.py:1046` and
`client_visible_messages.py:224` both call `resolve_event_source_content()`.
The projection does not. `projected_event()` in `matrix/journal_ingress.py:146-166`
stores `event.source["content"]` verbatim, and `projected_thread_history()` in
`matrix/conversation_reads.py` reads `message.content.get("body", "")` straight
out of it.

So every sidecar'd message now reaches prompt assembly as its preview. With most
agent turns over the threshold, that is most of the agent's own history.

Not caught by CI: no projection test carries a sidecar'd message.

### Where the resolved text should live

Resolving on every read is what the old path avoids by caching plaintext in the
event cache (`get_mxc_text` / `store_mxc_text`, keyed by room, event, and MXC
URL, guarded by the membership epoch). Deleting `matrix/cache/` deletes that
cache, so the journal has to own the same fact.

Resolving during admission is the wrong place: admission commits before nio
accepts the event, and an MXC download inside that path makes sync acceptance
depend on a media fetch that can fail or hang.

The shape that fits what already exists is lazy resolution on read, persisted
into the journal -- the same contract as `refresh_pending`. A sidecar body is
content the projection knows it does not have yet, which is exactly what a
pending refresh already models: a non-blocking read may serve the preview, a
strict read must resolve it before answering, and the resolved text is written
back so the fetch happens once.

### Implemented (2026-08-06)

Ownership: **Matrix owns the sidecar; the projected row owns the resolved
visible content.** No separate plaintext table -- the old `mxc_text` cache in
the event cache is not to be recreated, because a second owner needs its own
invalidation and, critically, its own redaction cleanup. Plaintext living in the
projected row means redaction removes it as a consequence of the projection
already working.

Storing resolved content in the projected row does not hurt replay: replay reads
`journal_events`, which keeps the exact admitted source. `visible_messages` is a
reduction and is allowed to hold canonical content.

Projection stays inside the admission transaction. An earlier sketch here
proposed splitting it out so a worker could resolve the sidecar before
projecting; that is rejected. It would break the crash invariant this document
states at the top, and it would strand context-only history, which is admitted
already settled with its source payload cleared and so never reaches a worker.
Recovering it would need a second obligation kind and a second fence -- a
recovery state machine bought to avoid one media fetch.

What is implemented instead reuses the mechanism redaction already established:

1. The projection refuses to store content whose text is a preview.
   `_stored_body` (`event_journal/projection.py`) writes a null body and a
   refresh token for any content still carrying sidecar metadata. This is a
   pure content inspection, so admission stays network-free.
2. That is the same row shape a redacted revision leaves behind, so the readers
   that already handle it need no change: `read_conversation` omits the message
   and reports it in `refresh_pending`, a non-strict read reports the page
   incomplete (`conversation_reads.py:71`), and a strict read resolves and
   re-reads before answering, raising `_StaleConversationError` if it still
   cannot (`conversation_reads.py:144`).
3. `ConversationHydrator._resolved_content` performs the fetch, from the point
   refetch path that redaction already used. Failure returns nothing, so the
   message keeps its token and stays repairable rather than settling the debt
   with the preview. `install_refetched_revision` refuses unresolved content as
   a backstop; a mutation test confirms that backstop alone keeps behaviour
   correct when the resolver is broken.
4. Redaction is unchanged. It already refetches the original plus surviving
   relations under the membership-epoch fence and the refresh token, and
   resolution now happens on that same path.

Collapse-before-download falls out of laziness rather than worker timing. An
earlier sketch proposed the worker "skip revisions already superseded by a later
edit"; that is rejected because a worker processing an edit cannot know a later
one is coming. Because each edit overwrites the visible row and nothing
downloads until a read asks, a streamed answer resolves only the revision that
won, whatever order the edits arrived in. `TestSidecarResolution::
test_a_streamed_answer_downloads_only_the_revision_that_won` pins one download
across three intermediate edits.

Resolution is bounded by the reader, not by hydration. Hydration installs rows
without fetching attachments, and the fetch happens per refetched message on the
strict read that wants it, so the bound is that read's page limit.
`HYDRATED_PROMPT_WINDOW_MESSAGES` (2_000, `matrix/conversation_hydration.py:61`)
is the hydration walk's ceiling and never a resolution budget. An earlier note
here claimed `execution_preparation.py:599` is "the real prompt trim"; that is
wrong -- it is the explicit `history_limit` selection used by scheduled and
tool-driven turns, and general prompt trimming happens through token-budget
selection elsewhere.

Steady state: one download per newly prompt-relevant visible revision, then
local reads until that revision is edited or redacted.

## Review of the read cutover, and what it found (2026-08-06)

An independent review of the branch raised seven findings. Two were correct
about code this cutover introduced, one was correct about the plan, and four
attacked a design that is not being built. Recording all of them, because the
refuted ones are the ones most likely to be raised again.

### Correct, and fixed

**A bounded page reported itself as full history.** `projected_thread_history`
derived `is_full_history` from whether the caller waited and whether a refetch
was owed, never from whether the page reached the start of the conversation. A
read that filled its limit and left a cursor behind reported the suffix as the
whole thread. `complete_thread_history` then marked it complete for summaries,
which count what they receive and record that count as the thread's size. The
cursor now participates in the answer.

### Correct, and open

**A first admitted event can prove an empty room.** `may_have_unread_history`
falls back to asking whether the journal holds any other event for the room, on
the reasoning that a room MindRoom has never seen an event in can have nothing
behind the event it just got. That is a fact about the journal, not the room. A
first run, or a rebuilt store, meets its first event in a room with years of
history and concludes that the empty page it can serve is complete; thread-root
proof reads a complete-looking empty page as "this root has no children" and
demotes a real threaded reply to a room-level message.

The hole is narrow -- it closes as soon as any second event in the room reaches
the journal -- but it is real, and it is worst exactly at startup.

**The obvious fix does not work, and this is the useful part.** Making
hydration the only proof of freshness (`return not await
conversation_is_hydrated(...)`) was tried and reverted. Non-blocking `read`
never hydrates; only `read_strict` does. So "degraded unless hydrated" makes
every dispatch read degraded forever, nothing ever hydrates on that path, and
the dispatch-safe property collapses into a strict read: eleven tests fail,
including one that asserts in as many words that a command must not block on a
strict read.

So the fix has to make hydration happen on the dispatch path rather than make
the predicate stricter -- a first dispatch read that reports degraded while
hydrating in the background, becoming complete once it lands. That is a real
piece of work, not a predicate change, and it needs its own phase.

The review instead proposed epoch-scoped projection watermarks and a durable
journal fence. Those are not needed: projection commits in the admission
transaction, so there is no window between admission and projection to fence,
and hydration already carries the epoch.

### Correct about the plan

The consumer-budget claim, the outbound-seeding contradiction, and the
split-projection sketch were all wrong in the plan text. All three are
corrected above.

### Refuted

**"Moving projection to the worker loses cold history"** and **"admission-to-
projection reads need a durable fence"** are both true of splitting projection
out of admission, which is why that split is rejected above. Projection stays
in the admission transaction, so cold history projects exactly as it always
did, and the window those findings describe does not exist. The remedy proposed
for them -- a second obligation kind (`projection_pending` alongside
`semantic_pending`) plus retained source payloads -- would add the recovery
state machine this architecture exists to remove.

**"Skip revisions already superseded is not enough"** is right that a worker
cannot know a later edit is coming, and moot: nothing downloads until a reader
asks, so only the revision that won is ever resolved.

**"Eager worker resolution contradicts the consumer budget"** describes eager
resolution, which is not what was built. Resolution is lazy and bounded by the
page the reader asked for.

## Delivery cutover: what the phase actually requires (2026-08-06)

`ResponseDelivery` (`src/mindroom/response_delivery.py`) is complete and has
**no production caller**. Nothing constructs it, so `enqueue_delivery`,
`claim_delivery`, `acknowledge_delivery`, and `unacknowledged_deliveries` are
reachable only from tests. The cutover is wiring, not new mechanism.

### The blocker

The outbox keys every row on `(turn_id, stage)`, and the transaction ID is
derived from them (`delivery_transaction_id(principal_id, turn_id, stage)`).
That is what makes a resend a no-op on the homeserver. **No delivery request
carries a turn identity.** `SendTextRequest`, `EditTextRequest`, and
`FinalDeliveryRequest` all take a `MessageTarget` and nothing that survives a
restart as the same turn.

The identity to use is `MessageEnvelope.source_event_id`: it is the Matrix
event that caused the turn, it is what the handled-turn ledger already keys on
(`same_turn_identity`, `turn_record.py:538`), and it is stable across restarts.
`ResponseIdentity` already carries the envelope, so `FinalDeliveryRequest` can
derive it today; `SendTextRequest` cannot.

### Do not route `send_text` wholesale

`SendTextRequest` has callers that are not response turns at all:
`visible_voice_echo.py`, `commands/config_confirmation.py`, `bot.py:424`,
`turn_controller.py:1682` and `:1768`,
`visible_response_reconciliation.py:152`. Giving those a synthetic turn ID
would put rows in the outbox that no recovery pass can reason about, and two
unrelated sends sharing a derived ID would silently collapse into one visible
message on the homeserver.

Only deliveries that carry a `ResponseIdentity` belong in the outbox.

### Order of work

1. Add the turn identity to the response-carrying delivery requests, derived
   from the envelope rather than generated, so the same turn re-derives it
   after a restart.
2. Construct `ResponseDelivery` in the bot and route `FinalDeliveryRequest`
   through it as `DeliveryStage.FINAL`. This is the delivery whose loss or
   duplication is visible to a user.
3. Route the streaming placeholder as `DeliveryStage.INITIAL`, which needs an
   identity on that one `SendTextRequest` call site
   (`response_runner.py:1309`) and not on the others.
4. Run `ResponseDelivery.recover()` at startup, after which contract 2 --
   settling the journal source at outbox enqueue rather than at TurnStore
   adoption -- becomes expressible.

Intermediate streaming edits stay off the outbox: contract 4 makes them
transport-only, and giving each one a durable row would put a claim-before-send
round trip in the streaming loop.

### What contract 2 protects against

Enqueue happens in the same transaction that settles the journal source. The
hazard is a crash between "the model produced an answer" and "the answer is
durably owed to a room": settling at TurnStore adoption marks the source done
while nothing durable yet says what to send, so recovery has no reason to send
anything and the turn is lost silently. Settling at enqueue means the source
stops being pending only once the delivery exists, and recovery finds it.

## Outbound seeding is deleted (2026-08-06)

Done. `seed_outbound`, its ordering key, `OutboundProjection`, `SeedingView`,
and the `provisional` / `revision_provisional` columns are gone, along with the
seed/echo race handling in the original and edit projection paths. Net −327
production lines.

The sync echo is now the only route into conversation content.

### What closed with it

**Task 11, "restore a revision discarded before a backwards canonicalization",
needed no fix.** Canonicalization existed only to promote a seeded row to
authoritative; with no seeded rows there is no canonicalization and no
discarded revision.

**The sidecar echo CASE went too.** An echo could replace an already-resolved
sidecar body with its own preview only because the seeded row was still marked
provisional and therefore yielded. That was a real bug and a real special case;
both are gone rather than maintained.

### What it costs, stated plainly

A turn that reads a conversation immediately after speaking in it sees the room
as it was before it spoke, until the echo lands. That is echo ordering, not
read-your-writes. Code that needs the identity of what it just sent uses the
send response, which is the only account of it that is certain at that moment.

The affected callers are the ones the earlier audit named: `thread_summary`,
and the Matrix conversation tools. Each reads to build context rather than to
confirm its own last message, so an echo-ordered read is the correct input; the
audit's original conclusion that this is what they should get stands.

### Why the enumeration tests went

`TestSeedOrderingMatrix` enumerated every arrival order of two seeds, two
echoes, and an original, because six defects in that family had each been fixed
against the single ordering that exposed them. The orderings it enumerated
cannot occur any more -- there are no seeds to order against echoes -- so the
matrix is deleted rather than kept green against a mechanism that does not
exist. `TestEchoOrdering` in `tests/test_journal_ingress.py` is what now pins
that a bot's own message reaches the conversation, and it does so through
admission, which is the only route left.

## Delivery cutover status (2026-08-06)

Every delivery point that carries a turn's answer is now durable, and startup
resends anything whose outcome this process cannot know.

| Delivery point | Route | Stage |
| --- | --- | --- |
| Placeholder that creates the visible message | outbox | `INITIAL` |
| Final answer, sent (no placeholder) | outbox | `FINAL` |
| Final answer, edited onto a placeholder | outbox | `FINAL`, with `edits_event_id` |
| Streamed terminal text, edited onto a placeholder | outbox | `FINAL`, with `edits_event_id` |
| Streamed terminal text, as the stream's first event | outbox | `FINAL` |

The streamed cases go through callbacks the gateway hands to
`StreamingResponse` (`terminal_edit` and `terminal_send`), so no extra Matrix
round trip is added: the edit or send the stream was going to make anyway is
enqueued first and acknowledged after. An unacknowledged row therefore means
exactly "the terminal update never landed", which is the condition recovery
acts on and the only one.

Sends that are not turns stay direct, as decided above: voice echoes, command
confirmations, reconciliation notices. So do intermediate streaming edits,
cancellation notices, and failure updates, which are transport rather than a
turn's answer. A terminal update that still reads `Thinking...` is also direct,
because a stream that never answered must not settle the turn -- `deliver_final`
delivers the answer in exactly that case, and would find its own row
acknowledged and send nothing.

### Recovery is a retry loop, not a startup step

Recovery runs after a sync response rather than at startup, because nio refuses
ordinary sends into an encrypted room until sync has rebuilt device state --
so a startup pass would spend its retry budget failing in exactly the rooms
this exists for.

The first response is not always enough either. It can arrive while a room is
still unrecovered. `recover()` therefore reports what it still owes, and the
pass runs again on later sync responses until it owes nothing. Tying "recovery
finished" to "first sync observed" stranded any row that failed the first pass
until the process restarted.

An unacknowledged `INITIAL` is skipped whenever a `FINAL` row exists at all,
acknowledged or not. Both rows unacknowledged is the ordinary shape of a crash
between claiming the answer and recording it, and recovery walks rows oldest
first, so requiring acknowledgement put the placeholder into the room *before*
the answer. An edit-shaped `FINAL` cannot be stranded by the skip: its target
event ID only exists because the placeholder send returned one.

### What is still open, precisely

**A final-response transform edits the answer outside the outbox.**
`finalize_streamed_response` applies `_apply_final_response_transform` after the
terminal edit has already been claimed and acknowledged. When the hook changes
the text, `_finalize_visible_replacement_edit` issues a second, direct edit, so
the room shows the transformed answer while the frozen `FINAL` row holds the
untransformed one.

Only one visible message exists either way -- the second edit revises the same
event -- so the outbox's central invariant holds. What diverges is the durable
record: a rerun turn reading the row back reports the untransformed body, and a
crash between the acknowledgement and the transform edit recovers to the
untransformed text.

The fix is not to update the frozen row, which is frozen for a reason: a retry
must resend what may already have been accepted. The right shape is to apply
the transform *inside* the terminal-edit callback, before the enqueue, so the
transformed text is both what is frozen and what is sent, and
`finalize_streamed_response` finds nothing left to change. That moves a hook
with its own cancellation semantics onto the streaming terminal path, so it is
its own change rather than a call-site edit.

**An oversized terminal edit freezes different bytes than it sends.**
`send_message_result` runs `prepare_large_message`, which for content above the
event limit uploads a sidecar and rewrites the payload with a fresh MXC URI --
and, in an encrypted room, fresh file keys. The row is frozen *before* that
rewrite, so recovery re-runs the upload and sends different bytes than the
first attempt did.

The visible outcome still converges, because Matrix deduplicates on the
transaction ID alone rather than on content. The costs are a redundant upload
on every recovered oversized answer, and a stored payload that does not
describe the event it produced. The fix is to prepare the wire payload before
enqueueing and give both the live and recovery paths a send-already-prepared
primitive, so nothing is rebuilt after the claim.

## Membership fencing is live (2026-08-06)

`advance_membership_epoch` had no production caller, which made every fence
built on it -- hydration, approval cards, unattempted outbox rows, and the
reply-fallback read -- correct but unreachable. Two independent reviews found
this in the same round.

`MembershipFence` (`src/mindroom/event_journal/membership.py`) now owns the
decision and `bot.py` calls it at the two membership transitions: immediately
on a local leave, and for sync-reported departures.

The interesting part is not the wiring but the exactly-once rule. One departure
reaches the bot twice -- locally, and again when sync reports it -- and both
reviews proposed guarding the second with `_local_departures_awaiting_sync`.
That set is wrong for the job: `_on_room_joined` discards from it, so a rejoin
between the leave and its echo re-arms the guard and the echo fences a second
time. The second fence deletes the conversation just hydrated under the *new*
membership along with any answer queued for it, which is the exact damage the
epoch exists to prevent.

The fence therefore keeps its own record of departures awaiting an echo, and a
join does not clear it: the echo is still owed, and when it arrives it still
describes the departure that was already accounted for.
`test_a_rejoin_before_the_echo_keeps_its_projection` pins this.

A cache-trust reset deliberately does not advance the epoch. Legacy
certification failure is not a Matrix membership transition.

## The read cutover's remaining reply-fallback defects (2026-08-06)

Two found by review, both real, one of them a genuine regression.

**A redacted revision was offered as a reply target.** `latest_visible_event_id`
returned `revision_event_id` unconditionally. When the revision currently on
screen is redacted, `_project_redaction` clears the body but keeps the row and
its revision pointer, so the query answered with a deleted event and a reply
quoting it renders as nothing. The row's logical event is not redacted -- a
redaction of the logical event deletes the whole row -- so it is the correct
answer in that window, and the query now returns it.

Not a regression: returning the *revision* rather than the logical event. The
old cache path did the same thing (`visible_event_id` is `latest_event_id`,
which is the edit's ID), so the spec argument against it, whatever its merits,
is about a choice this cutover inherited rather than one it made.

**A caller that just sent was made to guess.** Deleting outbound seeding made
reads after a send echo-ordered, and the plan already says a turn that must
know its own last message uses the event ID from the send response. Two
compound sends did not: the voice tool discarded `companion_event_id` and
re-queried, and the message tool passed only the thread root to its first
attachment. Both chained under the message before the one they had just sent.

`ConversationReader.latest_thread_event_id` now takes
`known_latest_thread_event_id` alongside the `reply_to_event_id` and
`existing_event_id` short-circuits it already owned, so one place decides what
outranks what. The shared test double follows the same precedence, because one
that answered a fixed value would let a caller silently stop passing it.

## The cache census, as the remaining work plan (2026-08-06)

Confirmed against HEAD by review. The 8c -> 8e -> 8f macro-order holds, with
the membership fence landing first (done above). The census added seven items
the phase list had missed:

- Raw-room replay proof (`turn_controller.py`, `dispatch_replay_guard.py`)
  reads recent room events and resolves their thread IDs through the cache.
- `ThreadReadMode` and the point-read `turn_scope` memoization still leak
  cache-specific semantics into the already-cut-over resolver and turn
  controller.
- Hook context reads the cache-only `AgentMessageSnapshot`.
- Sidecar resolution keeps legacy event-ownership and MXC branches in
  `message_content.py`, though the hydrator already resolves current-revision
  sidecars without the cache.
- Tool-runtime construction refuses to build a context without an event cache
  that no production tool consumes.
- Thread export is a third direct old-history consumer and needs its own slim
  Matrix pagination path, because the projection is bounded and non-exporting
  by design.
- `sync_continuity` imports its persisted checkpoint type from the
  certification owner slated for deletion, so that type must move first without
  changing the persisted format.

8c is narrowed on the same evidence: only two production consumers of
`get_event` exist, and one of them dies with the cache. The replacement is
relation resolution against the projection with a Matrix point fetch when the
event was never observed, memoized for the turn and persisting no raw event
JSON -- not a durable general-purpose lookup, which would rebuild the cache
this phase exists to delete.

## A second wedge, found while proving the first (2026-08-06)

The Classic-sync livelock has a fix, and it is not the whole story. A later
live run wedged again with none of that defect's signatures: zero
`matrix_sync_rebuild_retry_backoff`, zero `Abandoning recovery at the room
event cap`, zero `sync_recovery_incomplete`, zero
`matrix_sync_certification_uncertain`. The livelock is loud on every retry, and
that window was silent. It also needs more than fifty events in one sync window
to make the server report `limited`, which a short burst right after a restart
does not reach.

What the log does show is where the turn stopped. The event reached
`coalescing_gate_message_enqueued` and that line is the last
`mindroom.coalescing` entry in the file. Every other enqueue in the same run is
followed by `coalescing_gate_flush_started` within about a millisecond and then
`flush_finished outcome=dispatched`. This one never gets a `flush_started` at
all.

So the stall is in the coalescing gate's flush -- after admission, before
dispatch. That is the same "admitted, never dispatched" shape the journal
exists to make impossible, arrived at by a different route: the durable record
is correct and complete, and nothing schedules the work that would drain it.
The event loop was healthy throughout; a later event in the same process got a
complete streamed reply.

Two correlations worth chasing, neither yet a conclusion. The stranded enqueue
lands mid-startup, between `matrix_user_joined_room` and
`startup_phase_finished rooms_and_memberships`, which is not true of any
enqueue that did flush. And that same event fired
`matrix_event_callback_started` twice for the agent, 13 ms and 19 ms apart,
with one `Received message` and one enqueue -- duplicate delivery across the
restart.

`src/mindroom/coalescing.py` and `src/mindroom/ingress_lanes.py` are where to
look. This is a real open defect, distinct from the livelock, and it is the
reason the live proof cannot yet be called green.
