---
icon: lucide/message-square
---

# Matrix Integration

MindRoom uses the Matrix protocol for all agent communication. The integration is implemented in `src/mindroom/matrix/`.

See [Matrix Integration](../matrix.md#why-matrix), [Matrix Client](../configuration/index.md#matrix), [Room Management](../rooms.md#room-management), and [Threading (MSC3440)](../configuration/threads.md#threading-msc3440).

## Message Flow

### Sync Loop

Each agent bot runs an owned Nio ingestion session with a five-second long-polling timeout.
The default `matrix_sync.mode: classic` streams events through classic `/v3/sync` and backfills limited-timeline gaps from `/messages`.
Set `matrix_sync.mode: sliding` to use MSC4186 Simplified Sliding Sync on homeservers advertising `org.matrix.simplified_msc3575`.
Each agent uses a stable connection ID, a discovery range of `[0,99]`, and explicit subscriptions for its configured resolved rooms.
Subscriptions refresh after deferred joins and room configuration changes without replacing the durable session or discarding accepted input.
`matrix_sync.sliding_timeline_limit` defaults to 100 events per room window.
A durable store is bound to its transport; changing this setting does not convert an existing store.
Nio owns transport cursors, crypto preparation, and persisted per-event provenance.
Both transports distinguish initial history, live continuations, and recovered gaps; MindRoom uses the provenance Nio supplies without reclassifying it.
This provenance remains attached across recovery, restart, and decryption independently of application turn settlement.
`matrix/durable_ingestion.py` converts one trusted Nio batch and atomically commits its receipt, ordered membership effects, semantic events, and conversation projection in the MindRoom journal before acknowledging that batch to Nio.
An admission failure leaves the batch unsettled for retry, and replay after a committed admission returns the original receipt without duplicating semantic work.
Typing, presence and read receipts are excluded from durable admission.
MindRoom requires `mindroom-nio[e2e]==1.1.4`, and `uv.lock` pins the same published release.
Nio 1.0.2 avoids rereading queued payloads for byte accounting on SQLite 3.43 and newer, preserving exact accounting on older drivers.
Nio 1.0.3 returns typed membership errors for refused durable joined-member queries and preserves Matrix error codes after retry exhaustion.
Nio 1.0.4 avoids repeated pending-queue size scans during durable sync preparation while preserving queue limits and rollback.
Nio 1.0.5 moves durable sync response decoding and captured-input replay off the event loop while preserving durable capture and membership ordering.
Nio 1.0.6 uses unfiltered incremental Classic sync for local joins proven fresh at the current cursor, while retaining full-state recovery for stale evidence or incomplete room baselines.
It retains the encrypted-attachment and null room-avatar parsing fixes that prevent those event shapes from blocking history hydration.
Admission is fail-closed at every provenance, not only for recovery, because an event the journal never accepted is one no later process would see again.
Silent schedules use the custom `io.mindroom.scheduled.trigger` timeline event so clients do not render the task body as a room message.
Ingress admits that hidden event only from a managed sender, leaves it out of the visible-message projection, and classifies cold-history copies as context-only.
Journal dispatch validates and normalizes a live or recovered trigger into the existing formatted-message turn path, while an intentional no-report result records the turn and settles the trigger without a visible response.
Conversation history is hydrated on demand rather than pre-warmed at join: a bounded backward walk fills one room or thread and records the membership epoch it filled under, so a rejoin rebuilds from what the new membership can see instead of merging two memberships into one conversation.
Every derived conversation row, pending turn, and delivery outbox entry is tied to that membership epoch.
A departure advances the epoch, removes old projected history, retires unsent old-membership delivery work, and prevents an in-flight response admitted before departure from being sent after rejoin.
Attempted but unacknowledged deliveries retain their frozen transaction identity for exact reconciliation instead of being blindly resent.
Changing `matrix_sync` restarts running entities on config hot reload.
Sync loops are wrapped with `sync_forever_with_restart()` for automatic restart on connection failures.

An event reaches an agent through durable admission, never straight from the sync callback:

1. Sync receives the event via long-polling, and nio states its provenance once.
2. The owned ingestion pump validates and commits each batch through `PrincipalStore.admit_ingestion_batch()` before acknowledging it to Nio.
3. Control-room departures and history loss revoke uncertain grants before admission; live membership grant changes run after the durable commit, before the next batch.
4. `PendingEventWorker` drains what is still pending, so an event whose turn was interrupted is re-dispatched instead of lost.
5. `TurnController` owns the turn and the agent responds in thread.

Invites are the deliberate event-journal exception because an invite has no stable Matrix event ID to key a journal row on.
The owned ingestion callback stores the pending room and inviter before starting background handling.
The pending record wakes unfinished work, but it does not make Matrix repeat an already-checkpointed invite and does not grant authority.
The stored inviter is not authorization evidence: routers and agents require nio's current invite sender after fence persistence and immediately before starting the Matrix join request.
Nio owns invited-room cache updates, and the join path rechecks current inviter evidence immediately before its membership command.

All activity after joining uses ordinary responder conversation authorization.
See [Bot Runtime](bot-runtime.md) for the full durable dispatch boundary.

See [Streaming Responses](../streaming.md#streaming-responses_1) and [Mentions](../matrix.md#mentions).

## Response Tracking

Duplicate responses are prevented at two durable layers, both in `tracking/event_journal.db` under `mindroom_data/`.

`journal_events` is keyed `(principal_id, event_id)`, so a Matrix event redelivered by a sync reconnection or a `/messages` walk is recognised as already admitted rather than admitted twice.
A settled row is retained for exactly that reason, with only its replay payload cleared.

`TurnStore` owns the answer to "has this turn finished?", through the handled-turn ledger in `handled_turns.py`.
It shares the journal's database, so a terminal turn record and the settlement of the journal sources it answers commit in one transaction instead of two substrates approximately agreeing.
Its scope is the agent rather than the sync principal, because the proof that a message was already answered stays true across a re-login.

Delivery itself is owned by the `matrix_delivery_outbox` table, keyed `(principal_id, delivery_id, stage)` over `INITIAL` and `FINAL` delivery stages.
A `FINAL` stage edits an existing event when it has an edit target and otherwise publishes a standalone terminal event.
Each row freezes its explicit Matrix event type, payload, and deterministic transaction ID before the first send attempt, so ordinary responses and tool-approval cards recover through the same worker after a crash between sending and recording.
The claim also stores the sending device, because a transaction ID is only idempotent for the device that used it and a re-login would otherwise let a resend post a duplicate.
After a device change, standalone deliveries that reply outside a journal turn reconcile by exact frozen content and retain their debt when history cannot prove which event won.

See [Room Cleanup](../rooms.md#room-cleanup).

## Identity Management

The `MatrixID` class handles Matrix user ID parsing.
Runtime entity resolution uses the persisted identity registry, keyed by configured alias:

```python
mid = MatrixID.parse("@assistant_live:example.com")
mid.username  # "assistant_live"
mid.domain    # "example.com"
mid.full_id   # "@assistant_live:example.com"

# Resolve the current persisted Matrix ID for a configured alias
registry = entity_identity_registry(config, runtime_paths)
assistant_id = registry.current_id("assistant")
agent_name = registry.current_entity_name_for_user_id(assistant_id.full_id)
```

See [Root Space](../rooms.md#root-space), [Configuration](../rooms.md#configuration), [Delivery Policy](../matrix.md#delivery-policy), and [End-to-End Encryption](../matrix.md#end-to-end-encryption).

## Model Selection Protocol

Implementation owners under `src/mindroom/`:

| Module | Responsibility |
| --- | --- |
| `model_catalog.py` | Allowlisted metadata, Matrix icon upload/cache, and catalog revision |
| `model_catalog_receiver.py` | Router discovery admission, authenticated response, and scope/lifetime checks |
| `model_selection.py` | Structured request/result values and frozen acknowledgement metadata |
| `model_selection_scope.py` | Current joined membership and readable-root eligibility |

See [Model Overrides in Chat](../configuration/models.md#model-overrides-in-chat).

Send type `io.mindroom.models.request` with exact content:

```json
{"version":1,"request_id":"random-uuid","room_id":"!room:example.org","thread_id":"$root"}
```

Omit `thread_id` for room capability discovery; `null` is invalid.
Request IDs allow up to 128 characters; room/thread IDs allow up to 1024.
Do not add claimed sender or device fields.

The private reply type is `io.mindroom.models.response`:

```json
{
  "version": 1,
  "request_id": "random-uuid",
  "room_id": "!room:example.org",
  "thread_id": "$root",
  "capabilities": ["model_selection"],
  "agent_user_ids": ["@mindroom_helper:example.org"],
  "catalog_revision": "sha256-of-published-entries",
  "models": [{"key":"fast","display_name":"Quick helper","provider":"openai","id":"gpt-6-astra","icon_url":"mxc://example.org/image"}],
  "selection": {"override":null,"inherited":[{"entity":"helper","model":"fast"}]}
}
```

The response omits `thread_id` when the request did.
`selection.override` is always present and is `null` for absent or deleted model overrides.
It names a thread override only when every requester-visible responding entity uses that override, and is `null` when those entities have different thread overrides or some have none.
`inherited` lists requester-visible responding entities with their room-level model, ignoring thread overrides.
This explains what resetting the thread will use, including different defaults across agents and teams.

Each request reads current config.
Models are sorted by stable key; the SHA-256 revision covers only published entries, including labels and resolved Matrix icons.
Display names may repeat and fall back to the key.
Icons use Matrix `mxc://` URIs only; local raster publication follows the [model configuration rules](../configuration/models.md).

Application processing expires after 12 seconds; cancellation does not retract a send already retained by NIO.
Admission caps queued/in-flight requests at eight, and at two per Matrix user across all of that user's devices, and accepts at most eight fresh requests per user in 12 seconds; concurrent duplicate requests share the active request.
Immediately before handing the response to NIO, MindRoom rechecks current scope, captured device identity, and config identity, including after the final awaited scope check.
NIO then owns device validation, encryption, persistence, and delivery retries.
A requester who loses room access during NIO preparation or retry can still receive that already-authorized catalog.
Clients must independently enforce current joined-agent eligibility and discard expired responses.

Clients register their response listener before sending, correlate request, room, thread, and actual authenticated runtime device, and independently check returned agent membership.
Candidate runtime accounts are hints; clients require an owner-signed runtime device.
Multiple runtime devices remain separate choices.
Refresh on picker opening; expire requests after 12 seconds and discard late results.

### Thread Changes and Acknowledgements

Mutations use the ordinary `m.room.message` / `m.text` path with readable body `!model fast` or `!model reset`, carrying this additional content:

```json
{"io.mindroom.model_selection":{"version":1,"runtime_user_id":"@mindroom_router:example.org","runtime_device_id":"ROUTER_DEVICE","operation":"set","model":"fast"}}
```

`operation` is `set` or `reset`; reset omits `model`.
Explicit set permits keys such as `default`, `reset`, or `list`.
The required runtime device ID comes from authenticated discovery, routes the command, and grants no authority.
Actual room and canonical thread come from Matrix event routing.
Malformed structured metadata is rejected without falling back to text parsing; unrelated runtime users/devices ignore targeted commands.
Existing authorization, command policy, and durable deduplication still apply.

The normal command reply carries the persisted result alongside readable text:

```json
{"io.mindroom.model_selection_result":{"version":1,"command_event_id":"$command","room_id":"!room:example.org","thread_id":"$root","runtime_user_id":"@mindroom_router:example.org","runtime_device_id":"ROUTER_DEVICE","operation":"set","model":"fast","status":"applied","override":"fast"}}
```

Applied reset omits `model` and has `override:null`.
Rejection has `status:"rejected"`, no `override`, and may include a readable `error`.
Uncertain crash recovery may omit result metadata; clients refresh after timeout rather than infer success from sending.
Text and metadata share the existing durable command-result checkpoint and delivery outbox.
Accept an acknowledgement only for the client's current pending command event, exact room/thread, runtime user/device, operation, and model.
Encrypted events additionally authenticate the sender device.
In plaintext rooms, Matrix authenticates the sender account; device metadata only correlates the result.
Serialize pending changes per runtime/thread and prevent earlier discovery results from replacing later confirmed changes.
Other clients and text commands become visible on refresh; there is no global selection revision.
