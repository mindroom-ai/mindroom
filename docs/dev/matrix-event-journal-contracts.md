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

### Durable sync batch boundary

The Matrix client uses `nio.durable.open_durable_sync` with Classic or Simplified Sliding Sync.
Both development and published MindRoom wheels require the released `mindroom-nio>=1.0.0,<2` package, with no Git source override.
Account, device, consumer and stream ownership bind once when opening the session.
Soft-logout renewal requests the existing device; it preserves the bound stream, membership positions, and attempted-delivery sending identity.
Hard logout, missing device storage, or changed identity stops startup instead of attempting a stream replacement.
Initial credentials are persisted after the local store exists and before journal binding, so an interrupted bind reopens the same device.
The application trusts nio's typed records and does not reproduce a canonical JSON, digest, or per-record proof protocol.
Pre-durable application journals and older continuity files are unsupported.
Existing deployments use the explicit fresh-journal cutover in [the Nio 1.0 upgrade guide](../deployment/nio-upgrade.md), preserving their Matrix account, device, and encryption keys.

One `SyncBatch` becomes an ordered vector of application dispositions.
One journal transaction advances the consumer's `next_sequence` (starting at 1), applies every record, and freezes interactive associations for newly admitted actionable sources.
Any failure, including pending delivery projection, rolls back the entire vector and its sequence advance.
The consumer sequence is the sole batch-acceptance record; no separate receipt table is needed.
Redelivery of the last admitted sequence retries ordered hooks without repeating semantic effects.
Every acknowledged batch wakes semantic dispatch, including a retry whose journal transaction committed before the previous pump was interrupted.
Earlier or skipped sequences are rejected.
Empty completion batches advance the same sequence and must be acknowledged.

Nio splits membership authorization barriers into singleton batches. The pump
runs pre-admission hooks, commits, then runs post-admission hooks in vector order
before acknowledging. Live membership post-hooks retry after failure even when
the batch was already admitted; recovered membership never grants new authority.
Recovered membership changes fail the affected room's reply grants closed before admission and request an authoritative roster refresh, including when gap recovery succeeds without a history-loss record.
Ordinary semantic callbacks run only for newly admitted actionable events.
Auxiliary nio callbacks and sync completion run at least once until acknowledgement
and may repeat after a crash or callback failure.
Application joined-member lookups never write nio's room projection or its completeness flag; nio owns encryption recipient selection and room-key sharing.
MindRoom caches lookup results separately for responder and display-name decisions, invalidating them on membership or history-loss records and projection changes while sharing concurrent requests within each room lifetime.
Invitation authorization is checked after local membership queue and admission waits, immediately before handing the command to Nio; stale-position retries repeat that check.
Once Nio accepts the durable membership intent, Nio owns its HTTP and recovery fate.

Restored decrypted to-device events pass through the same current signed-device
authentication helper as fresh events, using the original encrypted envelope.
Removed or changed devices fail closed. Nio never restores application subtypes.
Self-authored pending/streaming replacements are filtered before classification;
original placeholders, terminal and foreign edits, redactions, undecrypted content
and unknown statuses retain normal handling.
Malformed ordinary timeline payloads have no semantic disposition and settle through the compatibility path so they cannot block later valid input.
Malformed producer ownership or membership metadata still rejects admission.

Unreadable live and recovered ciphertext never owns a canonical application event or a semantic callback.
Nio owns its cryptographic recovery; a later readable observation is admitted using that observation's trusted provenance.
Unreadable history retains only an opaque, settled envelope identity, without a ciphertext payload, so later decryption can populate conversation context but can never turn that old event into a request.
Opaque duplicates must match the original room, sender, and timestamp; ordinary readable duplicates must also match kind and thread.
An opaque reobservation cannot downgrade or settle an already admitted readable event.
Runtime-owned decryption diagnostics wait off the ingestion pump for response admission, check current authorization and the original producer membership epoch, and use Nio's public room-key request API.
They recheck authorization and tenure before sending a session-level warning and drain with their runtime before the client closes.
These diagnostics are best effort after acknowledgement and never settle application event IDs.

Quiesce stops new polling and drains already captured input while the pump stays alive, bounded by the five-second sync preparation timeout.
If projection recovery prevents admission, shutdown continues and leaves the unacknowledged producer batch for restart.
Close releases the session before the HTTP client.
Entity removal keeps ingestion alive through room departures, then cancels it before closing the session and journal.
Removal commands share a five-second deadline so retained input that cannot be admitted cannot block removal indefinitely.
Local membership changes use the public ordered session API and the last admitted producer position.
Commands wait for retained producer batches to finish admission and acknowledgement before testing a no-op or selecting their expected position.
If captured input changes that position while nio takes command ownership, the gateway reads the admitted position again and retries.
An unobserved producer position remains unknown, so startup cleanup must issue a leave request even when nio's initial position is `leave/0`.
The resulting local confirmation is admitted without advancing the producer epoch.
Producer membership positions and application tenures commit with admitted batch progress, but have separate lifetimes.
Application tenures belong to events and deliveries; producer positions belong to the bound nio stream.
Reported or locally confirmed departures advance application tenure once, and rejoins retain it.
Membership post-hooks compare admitted producer positions before applying replayed effects.
An unsuccessful join preserves its pending invitation and decrypt fence because the producer's boolean result does not distinguish terminal rejection from stale position or exhausted HTTP retries.

### Durable admission

Admission performs the journal insert or deduplication, membership-epoch validation, and the projection update in one transaction.
The ingestion pump acknowledges nio only after the application transaction commits and ordered hooks finish, so a crash before acknowledgement redelivers the retained batch.

If an interactive source reaches admission before an attempted outgoing edit has a durable projection, `DeliveryProjectionPendingError` rolls back admission and leaves the Nio batch unacknowledged.
The ingestion pump waits for progress from the bot's existing outbox recovery worker, then retries that same batch against the authoritative projection barrier.
One bot-owned event signals each recovery pass; the sole pump consumes it without resetting an active worker's retry backoff.
Waiting for the entire outbox to empty is deliberately excluded: an unrelated failed send must not block an already-projectable source.
Pump cancellation does not cancel the independently owned recovery worker; shutdown wakes the waiter and stops admission.
Other admission errors propagate normally.

Context-only payloads may be compacted after projection; actionable payloads retain the exact replay input until terminal settlement.

A pending worker processes committed events in durable receipt order and leaves an event pending on cancellation or failure.
There is deliberately no durable `running` state, because a process crash must make the event eligible for retry.

## The eleven boundary contracts

Each contract is stated as a rule, then as what actually shipped.

### 1. The projection is a prompt view, not a Matrix replica

A latest-visible view whose read APIs apply bounded prompt and export windows.
The durable projection itself is not storage-bounded, and it provides no certification or independent Matrix-reduction path.

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

`admit_ingestion_batch()` (`event_journal/journal.py`) applies owned Nio lifecycle records and advances the epoch on a local leave or reported departure.

The lifecycle effect and admitted sequence commit atomically, so replaying an unacknowledged batch cannot advance the epoch twice.
Nio supplies the prior and current membership epochs, while the journal rejects inconsistent transitions and absorbs already-accounted local departure echoes.
Post-commit call and invited-room cleanup checks the current journal epoch before acting and retries unfinished effects when the batch is replayed.

An epoch advance drops conversation projections, reconciles approval cards and continuations across their distinct principals, removes delivery rows proven unattempted, and retires fully acknowledged approval deliveries after tombstoning their card event IDs.
**Attempted but unacknowledged** rows survive deliberately: their outcome is unknown, so keeping the frozen payload and its transaction ID means a retry collapses onto the same event instead of posting a second answer.

The same transaction also **force-settles pending turn-backed events** for that room, clearing `source_json` and `semantic_consumer` while keeping the rows.
This is what makes the enqueue refusal below *final* rather than permanent: left pending, the worker would offer the source again on every replay, the model would run again, and enqueue would refuse again, forever.
Only turn-backed kinds and reactions already claimed by `INTERACTIVE_REACTION` are swept.
Other reactions, redactions, and approval replies enqueue no answer, so the epoch predicate does not retire them; a redaction in particular still owes real cleanup that sweeping would drop silently.
An interactive-reaction claim that races after departure is rejected and settles that stale reaction against the new membership epoch.

The in-flight turn is fenced at enqueue: `_enqueue_matrix_delivery` compares the epoch that admitted the turn against the room's current one and refuses to write the row when they differ.

### 9. One backend, several narrow views

Structural protocols in `event_journal/views.py` expose admission, owned-batch admission, replay, dispatch, pending turns, relation and conversation reads, history recovery, hydration, Matrix delivery, and approval delivery.
Each collaborator takes only the slice it calls, and the type checker enforces that boundary.

The generic worker accepts only `MatrixDeliveryView`, while approval collaborators accept `ApprovalDeliveryView` rather than the full principal store.

### 10. Special facts stay specialized

Resolved sidecar plaintext belongs to the visible revision, so the projection refuses to store an unresolved preview and records the refresh debt instead.
Approvals keep their exact-call identity in `approval_cards` behind `ApprovalDeliveryView`, while Matrix transport state stays in the generic outbox inherited through `MatrixDeliveryView`.
The generic projection was not widened for either.

### 11. Recovery classification stays in nio — scoped to the timeline

Both Classic and Simplified Sliding Sync feed nio's owned durable ingestion session.
Nio owns recovery bounds, cold-history classification, and explicit history-loss records.
MindRoom maps timeline provenance directly: `HISTORY` is context-only; live and recovered records may own semantic work.
Own-room lifecycle records carry explicit previous/current membership and producer epochs.
Member timeline records use the same provenance-based admission as other timeline events.
Only `LIVE` member observations update reply grants; recovered member events can retain semantic obligations without granting new authority.
MindRoom does not infer provenance from raw sync state blocks, timeline limits, cursor presence, or repeated memberships.

The specialized media parser is used only for encrypted media messages; every other source uses nio's outer-event-type parser.
Media-shaped extension fields on reactions or redactions cannot change their journal kind or create a message projection.
Encrypted attachment fields and malformed-media rejection retain their existing validation path.

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

Initial and final delivery stages use deterministic transaction IDs derived from principal, delivery ID, and stage.

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

### Producer-owned local membership confirmation

The durable producer keeps one successful local membership intent until its outcome is acknowledged and an authoritative sync boundary observes it.
A subsequent local command waits for that observation; shutdown may leave the acknowledged observation marker for restart.
Nio reconciles reported echoes and owns producer membership epochs.

Typed batch admission stores producer membership positions separately from application tenure in the same transaction as admitted batch progress.
Nio owns local-command confirmation and reported echoes; MindRoom keeps no second departure-echo counter.
