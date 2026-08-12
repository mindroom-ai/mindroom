# Matrix Event Journal: Contracts and State Decisions

What the event journal guarantees, and the decisions behind it that are expensive to rediscover.

This is reference material for anyone changing `src/mindroom/event_journal/`, `src/mindroom/matrix/journal_ingress.py`, or the conversation projection.
It states current behaviour.
The history of how it got here, including the theories that turned out wrong, is archived in `docs/dev/archive/2026-08-05-matrix-event-journal-projection.md`.

## Ownership model

### Principal ownership

One shared database backend may hold several principals, but runtime code receives only a principal-bound store view.

Operational methods such as `admit`, `pending`, `settle`, `load_conversation`, membership changes, and delivery methods therefore do not accept `principal_id`.
Inbound envelopes and conversation keys also omit it, because the bound store supplies it.
This is what stops a caller from reading or settling another bot's rows by accident.

### Conversation identity storage

Typed APIs represent an unthreaded conversation as `thread_id=None`.
Durable SQLite and PostgreSQL tables represent it with `thread_id TEXT NOT NULL` and the empty string as the single canonical storage value.

One shared boundary helper encodes `None` to the empty string and decodes it back, so primary keys and uniqueness constraints never depend on nullable equality.

### Durable admission

Admission performs the journal insert or deduplication, membership-epoch validation, and the projection update in one transaction.
The admission callback returns to nio only after that transaction commits, so a crash in the gap redelivers the event rather than losing it.

Context-only payloads may be compacted after projection; actionable payloads retain the exact replay input until terminal settlement.

A pending worker processes committed events in durable receipt order and leaves an event pending on cancellation or failure.
There is deliberately no durable `running` state, because a process crash must make the event eligible for retry.

## The eleven boundary contracts

Each contract is stated as a rule, then as what actually shipped.

### 1. The projection is a prompt view, not a Matrix replica

A bounded, recent, latest-visible view whose purpose is prompt construction.
No certification, no periodic scan, no unbounded export API.

**Export reads this projection; it does not paginate Matrix itself.**
Before the cutover it did, and owning a second Matrix reducer meant an exported
thread and the history a model was shown could disagree about which edit won or
what a redaction left behind.
`thread_export/projected_history.py` now pages the same `ConversationReader` a
prompt uses, under its own far larger bounds (`EXPORT_WINDOW_MESSAGES`,
`EXPORT_MAX_FETCHED_EVENTS`, `EXPORT_MAX_MESSAGES_REQUESTS`), so a cold thread
still reaches the server — through the shared hydrator, not through a walk of
export's own.
Do not re-impose the prompt window on hydration on the belief that export is an
independent consumer.

One repair exists and is deliberate: contract 7's point refetch.

### 2. Ownership transfers at durable handoff

The journal owns an actionable source until the turn is durably handed off.

The handoff is **durable outbox enqueue**, not `TurnStore` adoption — settling on adoption would lose answers.
Enqueue-and-settle is one backend transaction, so there is no window in which a turn is delivered while its source is still pending, or settled with nothing owing it.

### 3. The journal retains no raw history

A `CONTEXT_ONLY` event is projected transactionally and then keeps only enough identity to deduplicate.
Without this the journal becomes the raw-event cache it was built to delete.

### 4. Streaming progress is transport-only

Persist the initial logical event identity and the terminal visible body.
Intermediate self-authored edits do not reach the projection; user-authored edits still reduce normally.

One pure predicate in `matrix/transport_progress.py` refuses a self-authored `m.replace` whose `visible_content` carries `pending` or `streaming`.
It is applied in **two** places — `projected_event` and hydration's `_projected_from_event` — because hydration fetches the whole relation tree and would otherwise reinstall every progress edit on the first cold read of a room.

### 5. Acknowledged sends are provisional — superseded and deleted

This contract no longer exists.
Outbound seeding was removed, so the sync echo is the only route into conversation content: nothing is provisional because nothing is written before the server has ordered it.

The cost is real and accepted: a turn that reads the conversation immediately after speaking does not see its own message.

### 6. Hydration is defined by the prompt window

Both walks are bounded, and the window counts logical messages rather than pages of events.

Thread hydration adapts the room bounds rather than copying them: `_fetch_relations` counts a logical message only when `replaces_event_id is None`, and `max_fetched_events` bounds the raw relation tree that streaming makes an order of magnitude larger than the message count.
The thread root is kept over and above the window, because a thread starting at its first reply is missing the message it is about.

**Why early truncation is safe.** The walk asks for `direction=back` explicitly rather than inheriting nio's default.
Under MSC3981 the server returns relations in the topological order `/messages` would give, and an edit is sent after the message it revises, so every edit arrives *before* its original.
The window may therefore only stop at the moment a logical message was just admitted, with its whole edit tail already collected.
The event ceiling can stop mid-message, and under this order that is the harmless direction: it drops an original and keeps edits nothing will claim, rather than keeping a message at a stale revision.

### 7. Exactly one exceptional history repair

The point refetch, and nothing else.

### 8. Membership epochs fence every derived and pending fact

`MembershipFence` (`event_journal/membership.py`) advances the epoch on a local leave and on a sync-reported departure.

Exactly-once is the substance of this contract, and it is durable rather than in-process: `fence_departure(room_id, source=LOCAL|REPORTED)` returns a `DepartureOutcome`, and `rooms_owing_departure_reports` / `retire_owed_departure_reports` carry the owed-report set across a restart.
An in-process marker was not enough — an advance that raised left the marker set and swallowed the echo, a restart lost it, and two leaves before one echo needed two markers.

An epoch advance drops `conversation_hydration`, `visible_messages`, `unresolved_edits`, `redaction_tombstones`, `room_history_recovery`, and `approval_cards` in one transaction, plus unattempted outbox rows.
**Attempted** rows survive deliberately: their outcome is unknown, so keeping the frozen payload and its transaction ID means a retry collapses onto the same event instead of posting a second answer.

The same transaction also **force-settles pending turn-backed events** for that room, clearing `source_json` and `semantic_consumer` while keeping the rows.
This is what makes the enqueue refusal below *final* rather than permanent: left pending, the worker would offer the source again on every replay, the model would run again, and enqueue would refuse again, forever.
Only turn-backed kinds are swept. A redaction, reaction, approval reply, or decryption failure enqueues no answer, so the epoch predicate never blocks it — and a redaction in particular still owes real cleanup, which sweeping it here would drop silently.

The in-flight turn is fenced at enqueue: `_enqueue_delivery` compares the epoch that admitted the turn against the room's current one and refuses to write the row when they differ.

### 9. One backend, several narrow views

Eleven structural protocols — ten in `event_journal/views.py` (`AdmissionView`, `ReplayView`, `DispatchView`, `PendingTurnView`, `RelationView`, `ConversationReadView`, `HistoryRecoveryRecordView`, `HydrationView`, `OutboxView`, `ApprovalView`) plus `MembershipView` in `event_journal/membership.py`.
Count them rather than quoting this line; the archived plan's count named two views that no longer exist and missed one that does.
Each collaborator takes the slice it calls, and the type checker enforces it: a hydrator reaching for `enqueue_delivery` fails `ty` before any test runs.

`grep -rn ": PrincipalStore" src/` returns nothing outside `event_journal/` itself.

### 10. Special facts stay specialized

Resolved sidecar plaintext belongs to the visible revision, so the projection refuses to store an unresolved preview and records the refresh debt instead.
Approvals get their own `approval_cards` table behind `ApprovalView`, fenced by membership epoch.
The generic projection was not widened for either.

### 11. Recovery classification stays in nio — scoped to the timeline

Timeline ingress maps nio provenance directly, with no local inference.
`bot.py` asks `timeline_member_event_class(event)` for timeline member events and admits with the class nio gave; when that returns `None` the event is **skipped rather than guessed at**, because nio saying nothing means the event is already journaled with its true class.

**State-block member events cannot consume provenance, because none exists** — `RoomInfo.state` carries no `TimelineEventProvenance`, and `record_completed_timeline_event` is called only from the timeline walk.
So room-lifecycle state events get their own stated rule: `room_member_sync_state_plan` may consult `join_info.timeline.limited` and `prev_membership` to decide *dispatch versus baseline record*, never to label an event `LIVE`, `RECOVERED`, or `HISTORY`.

## Visible-message projection

One row per logical message with its latest visible body.

A valid same-sender edit replaces that row only when `(origin_server_ts, event_id)` is newer than the current replacement identity.

An edit received before its original is stored as one latest unresolved edit **per target and sender**.
Including the sender in that key is what stops an attacker evicting the legitimate author's edit before the original arrives.
When the original arrives, only its sender's unresolved edit may apply, and all unresolved rows for that target are deleted.

### Redaction

Admission records every redaction target as a compact durable tombstone *before* projection, so an original or edit arriving later cannot resurrect redacted content.

- Redacting the logical original tombstones the logical message.
- Redacting the currently visible replacement clears the row's visible body and marks the row with a durable refresh token derived from the redaction's journal receipt order.
- Redacting an already superseded replacement does not change visible content.

Clearing the body in the same admission transaction is required: a redacted revision must never be readable, and a stale-but-visible row would let any non-strict caller serve content the sender deleted.

### The point refetch

A strict conversation read waits for one shared point refetch rather than serving content known to be stale.
A non-strict read never waits and never serves a body-cleared row, so it omits that logical message until a refetch installs the server-authoritative revision.

The refetch reuses hydration's relation traversal and reducer, retains no edit chain, and installs only when both the membership epoch and the exact refresh token still match.
A newer edit or redaction changes the revision and prevents an older in-flight refetch from overwriting it.

Success clears the token; failure or cancellation leaves it durable and makes strict reads fail closed until a retry succeeds.
The next strict read drives that retry, so there is no background refresh worker and a permanently unreachable homeserver degrades reads rather than accumulating retry state.

## Bounded conversation reads

Every read requires a positive limit and an optional stable cursor of `(created_ts, logical_event_id)`.

The cursor is compared as a **row value** — `(created_ts, logical_event_id) < (?, ?)` — not as a disjunction.
The disjunctive form cannot use the index as a bound and degrades to a filtering scan: at 10⁶ messages that was 77 s on SQLite and 130 s on PostgreSQL, against 0.38 s and 0.57 s for the row-value form.

Prompt assembly requests pages only until its context budget is satisfied; full exports iterate explicit pages.
No runtime API may materialize an unbounded room-scoped conversation.

## Hydration

A thread is hydrated by fetching its root and traversing recursive event relations without a relation-type filter.

A room-scoped conversation may perform one serialized initial `/messages` traversal.
Concurrent first readers share one hydration task, and hydration projects in bounded membership-epoch-checked transactions before a final transaction publishes coverage.
Projected rows from committed chunks may be locally visible before that final transaction; they are additive facts, while the coverage marker and exact recovery settlement remain unpublished until every chunk succeeds.
Failure stays a visible readiness or request failure rather than reviving room-wide repair scans.

## Deterministic delivery

Initial and final delivery stages use deterministic transaction IDs derived from principal, turn, and stage.

The completed model result is durable in `TurnStore` before final outbox enqueue, so recovery does not rerun a completed model call merely to rebuild delivery content.

Enqueue may create a row or update an unattempted one.
The worker then atomically claims the row by committing `attempted=true` **before** network I/O; claiming freezes the payload and target and returns the exact stored delivery to send.
An attempted but unacknowledged row is retried with the same payload and transaction ID.

That ordering closes the case where Matrix accepted an older deterministic transaction while a restarted model run produced different content that could never become visible.

Acknowledgement and the terminal turn record commit in **one** transaction, and an acknowledgement loser writes neither row — that is what stops the outbox and the turn record naming different events.

## Storage concurrency

SQLite uses one writer task and a command queue.
The writer opens `synchronous = FULL`; readers use `NORMAL`.
Writer and reader connections use WAL-compatible settings and an explicit `busy_timeout`.

PostgreSQL implements the same behavioural contract without a second application protocol.

Both backends run the same admission, projection, membership, pagination, and outbox contract tests.
A rule that holds on only one backend is a rule MindRoom does not actually have.

## Homeserver behaviour not observable from this repository

These come from the fork repositories and the deployment configuration rather than from MindRoom source.
Do not rediscover them by debugging.

### Tuwunel purges superseded edits

Tuwunel purges superseded `m.replace` events on a background job.
It is disabled by default in the fork but enabled in production, with a 24-hour minimum age, an hourly interval, and a 10,000-event batch size.

This is why edit-redaction recovery asks the server instead of trusting local history.
The 24-hour floor means a current-edit refetch normally returns the true previous edit, and returns the original body only once superseded edits have aged out — both correct, because every Matrix client sees the same server state.

The purge exists to reclaim storage from MindRoom's own streaming edit churn, which is already treated as transient, so it is not a reason to retain edit history locally.

### `recursion_depth` is not comparable between servers

Tuwunel and the MindRoom Synapse fork both cap recursive relation traversal at depth three in source, and neither advertises that cap.

They do not report `recursion_depth` with the same meaning:

- **Synapse** returns the constant `3`, describing the depth it is willing to traverse.
- **Tuwunel** returns the depth of the deepest event it actually returned, so a root with one threaded reply and one edit of that reply reports `1`.

A required depth above zero would therefore reject ordinary complete pages on Tuwunel while proving nothing on Synapse.
The portable requirement is only that a **non-empty** page reports the field at all, which still catches the failure worth catching: a server that ignores `recurse` and silently returns direct children omits the field entirely.

An empty relation page reports no depth on Tuwunel and must not be treated as a failure — it has nothing that could have been truncated.

The Matrix version advertised by `/versions` is not proof of recursion depth.

### Transaction deduplication is per sending device

Both homeservers deduplicate a repeated transaction ID per sending device rather than per access token, and MindRoom persists its device across restarts.
So deterministic outbox retries survive a crash, but would **not** survive re-login with a new device.

Synapse expires stored transaction mappings on a periodic cleanup, so a deterministic retry is idempotent for a bounded time rather than indefinitely.
