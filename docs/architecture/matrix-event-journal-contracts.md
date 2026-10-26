# Matrix Event Journal: Contracts and State Decisions

The decisions behind the event journal that its code does not record, and the homeserver behaviour it relies on that you would otherwise rediscover by debugging.
Invariants whose reasons are visible where they are enforced are explained in the docstrings of `src/mindroom/event_journal/` and the Matrix ingestion and hydration modules, not here.
Which plaintext the journal keeps and which principal owns it is on [the security page](matrix-event-journal-security.md).
The history of how the design got here, including the theories that turned out wrong, is recorded in `docs/dev/2026-08-05-matrix-event-journal-projection.md`.

## Decisions

### Nio owns sync, provenance, and recovery

MindRoom trusts nio's typed sync records and does not reproduce a canonical JSON, digest, or per-record proof protocol over them.
Nio owns recovery bounds, cold-history classification, and history-loss records, and MindRoom takes timeline provenance from nio rather than inferring it from raw sync state blocks, timeline limits, cursor presence, or repeated memberships.
Nio also owns encryption recipient selection, room-key sharing, and confirmation of local membership commands and their sync echoes, so MindRoom keeps its own membership tenure fencing but no second echo protocol or departure-echo counter.
Producer membership positions belong to the bound nio stream and application tenures belong to events and deliveries, so the two have separate lifetimes even though they commit together.

### Ownership transfers at durable outbox enqueue

The journal owns an actionable source until its answer is durably enqueued in the outbox, not until the turn store adopts the turn, because settling on adoption would lose answers.

### Nothing outbound enters the conversation before the server orders it

Outgoing messages are not seeded into the projection as provisional content: they enter conversation content only as server-ordered events, through their sync echo or, for edits and interactive prompts, through the server copy fetched when delivery is acknowledged.
The accepted cost is that a turn reading the conversation immediately after sending a plain message does not see it until the echo arrives.

### Admission does not wait for the whole outbox

An interactive source that arrives before the attempted outgoing edit it depends on is projected waits for outbox recovery to make progress, not for the entire outbox to empty, because an unrelated failed send must not block a source that is already projectable.

### Shutdown does not wait on stuck input

Shutdown and entity removal stop waiting for captured input they cannot admit within a bounded time and leave it unacknowledged in nio for redelivery after restart, so input that cannot be admitted cannot block them indefinitely.

### The projection is a prompt view, not a Matrix replica

Its reads apply bounded prompt and export windows, but the stored projection is not storage-bounded, and it offers no certification or independent Matrix-reduction path.

## Evidence

Paging a conversation with the disjunctive cursor `created_ts < ? OR (created_ts = ? AND logical_event_id < ?)` instead of a row-value comparison cannot use the index as a bound and degrades to a filtering scan: on PostgreSQL at 10⁶ messages it took 130 s against 0.57 s for the row-value form (the SQLite figures and query plans are in the comment on the conversation cursor clause).

## Homeserver behaviour not observable from this repository

These come from the fork repositories and the deployment configuration rather than from MindRoom source.

### Tuwunel purges superseded edits

Tuwunel purges superseded `m.replace` events in a background job that the fork disables by default and production enables, with a 24-hour minimum age, an hourly interval, and a 10,000-event batch size.
This is why edit-redaction recovery asks the server instead of trusting local history.
The 24-hour floor means a current-edit refetch normally returns the true previous edit and returns the original body only once superseded edits have aged out, both correct because every Matrix client sees the same server state.
The purge reclaims storage from MindRoom's own streaming edit churn, which is already treated as transient, so it is not a reason to retain edit history locally.

### `recursion_depth` is not comparable between servers

Tuwunel and the MindRoom Synapse fork both cap recursive relation traversal at depth three in source, and neither advertises that cap.
Synapse reports `recursion_depth` as that constant cap while Tuwunel reports the depth it actually returned, so the only portable check is that a non-empty page reports the field at all, which still catches a server that ignores `recurse` and returns only direct children.
An empty relation page reports no depth on Tuwunel and is not a failure, and the Matrix version advertised by `/versions` is not proof of recursion depth.

### Tuwunel's recursive relation paging drops and reorders edits

Tuwunel's recursive `/relations` paging leaves out some edits of a reply whose parent fell on an earlier page and returns others after the reply instead of before it, while its direct `/relations/{event}/m.replace` lookup for that reply is complete.
On one production thread a backwards walk returned 356 of 419 events and missed 26 of 99 edits, and every affected streamed reply was stored at its placeholder under a marker that called the thread whole.
Thread hydration therefore looks up the direct edits of each message whose newest walked revision still says its reply is unfinished, point refetches read a message's direct edits instead of its relation tree, and markers carry a walk version so threads an older walk marked are walked again.
User edits of ordinary messages are not looked up, because nothing in a message says an edit is missing, so one this paging drops keeps its earlier text.

### Transaction deduplication is per sending device

Both homeservers deduplicate a repeated transaction ID per sending device rather than per access token.
MindRoom keeps its device across restarts and soft-logout renewals and refuses to start rather than replace a missing or changed device, so deterministic outbox retries survive a crash, but they would not survive a re-login with a new device.
Synapse expires stored transaction mappings on a periodic cleanup, so a deterministic retry is idempotent for a bounded time rather than indefinitely.

### Nio's durable producer

Nio's durable producer keeps one successful local membership intent until its outcome is acknowledged and an authoritative sync boundary observes it.
A later local command waits for that observation, and shutdown may leave the acknowledged observation marker for the next start.
A join command returns only once nio can send to the room, including a room the producer already reports as joined, because nio refuses plaintext sends into a joined room until a sync delivers its complete state, which neither the client's first sync under MindRoom's sync filter nor an incremental sync that first reports a room does.
Under Sliding Sync MindRoom subscribes, besides its configured rooms, every joined room whose state nio still waits for, because a room outside both the room list window and the configured subscriptions is never reported, so neither that state nor the observation of a join a restart interrupted would arrive; a room nio can already send to needs no subscription.
When nio restores decrypted to-device events, it never restores their application subtypes.
