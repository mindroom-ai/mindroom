---
icon: lucide/message-square
---

# Matrix Integration

MindRoom uses the Matrix protocol for all agent communication. The integration is implemented in `src/mindroom/matrix/`.

See [Matrix Integration](../matrix.md#why-matrix), [Matrix Environment Variables](../configuration/index.md#matrix), [Room Management](../rooms.md#room-management), and [Threading (MSC3440)](../configuration/threads.md#threading-msc3440).

## Message Flow

### Sync Loop

Each agent bot runs an owned Nio ingestion session with a five-second long-polling timeout.
See [Matrix Sync](../matrix.md#matrix-sync) for the `classic` and `sliding` transports, their settings, and reload behavior.
Classic sync backfills limited-timeline gaps from `/messages`.
Sliding sync uses a stable connection ID per agent, a discovery range of `[0,99]`, and explicit subscriptions for the agent's configured resolved rooms.
Subscriptions refresh after deferred joins and room configuration changes without replacing the durable session or discarding accepted input.
Nio owns transport cursors, crypto preparation, and persisted per-event provenance.
Both transports distinguish initial history, live continuations, and recovered gaps; MindRoom uses the provenance Nio supplies without reclassifying it.
Typing, presence and read receipts are excluded from durable admission.
MindRoom requires `mindroom-nio[e2e]==1.1.4`, and `uv.lock` pins the same published release.
Silent schedules use the custom `io.mindroom.scheduled.trigger` timeline event so clients do not render the task body as a room message.
Ingress admits that hidden event only from a managed sender, leaves it out of the visible-message projection, and classifies cold-history copies as context-only.
Journal dispatch validates and normalizes a live or recovered trigger into the existing formatted-message turn path, while an intentional no-report result records the turn and settles the trigger without a visible response.
Conversation history is hydrated on demand rather than pre-warmed at join: a bounded backward walk fills one room or thread and records the membership epoch it filled under, so a rejoin rebuilds from what the new membership can see instead of merging two memberships into one conversation.
A read that must come straight from the homeserver, such as the thread-summary pin check, restart auto-resume, and thread-root proofs for tools, walks only a bounded window of recent room history.
A root older than that window is reported as unproven, so those callers fail closed.
Sync loops are wrapped with `sync_forever_with_restart()` for automatic restart on connection failures.

An event reaches an agent through durable admission, never straight from the sync callback:

1. Sync receives the event via long-polling, and nio states its provenance once.
2. The owned ingestion pump commits each batch to the event journal before acknowledging it to Nio, so an admitted event is never lost or admitted twice.
3. `PendingEventWorker` drains what is still pending, so an event whose turn was interrupted is re-dispatched instead of lost.
4. `TurnController` owns the turn, and the agent replies in a thread or in the room according to [thread mode resolution](../configuration/threads.md#thread-mode-resolution).

Invites have no stable Matrix event ID, so they are recorded outside the event journal, and the stored inviter never grants authority.
All activity after joining uses ordinary responder conversation authorization.
See [Bot Runtime](bot-runtime.md#durable-dispatch-boundary) for admission, membership epochs, and invite handling.

See [Streaming Responses](../streaming.md) and [Mentions](../matrix.md#mentions).

## Response Tracking

Duplicate responses are prevented at two durable layers, both in the [event journal](../deployment/storage.md#event-journal).

The event journal recognises a Matrix event redelivered by a sync reconnection or a `/messages` walk as already admitted, and keeps settled rows for that reason.
`TurnStore` answers "has this turn finished?" through the handled-turn ledger in `handled_turns.py`.
It shares the journal's database, so a terminal turn record and the settlement of the sources it answers commit together.
It is scoped to the agent rather than the sync principal, so the proof that a message was already answered survives a re-login.

The delivery outbox freezes each send's event type, payload, transaction ID, and sending device before the first attempt.
After a crash between sending and recording, ordinary responses and tool-approval cards recover through the same worker without posting duplicates, including after a device change.

While nio recovers a limited-sync gap in a room, it rejects sends to that room with `SendRetryError`.
MindRoom then retries the same prepared payload in place for up to 30 seconds and reports a delivery failure if the gap does not close in time.

See [Room Cleanup](../rooms.md#room-cleanup).

## Identity Management

Runtime code resolves an entity's Matrix ID through the persisted identity registry (`entity_identity_registry()`), keyed by configured alias, rather than deriving it from the requested localpart.
See [Agent Users](../matrix.md#agent-users) for why the actual account can differ.

## Encryption

Each agent's cross-signing master and self-signing keys are persisted next to its encryption store.
When the homeserver no longer has the uploaded identity, for example after a homeserver reset that kept `encryption_keys/`, login detects the divergence and re-uploads the persisted keys once instead of wedging.

Decryption-failure notices are deduplicated per room and Megolm session through a disk-backed ledger shared by every bot, so the first bot that fails on a session posts the only notice.
Before a live room join, the bot persists a join fence for that room, which survives restarts until a trusted sync response confirms the join.
While the fence is held, a decryption failure in that room still logs, updates E2EE counters, and requests missing keys, but posts no notice.
Cold history is admitted rather than rejected: nio's `HISTORY` provenance makes an event context-only, so it joins the conversation projection but never starts a turn.

See [Matrix Space](../rooms.md#matrix-space), [Configuration](../rooms.md#configuration), [Delivery Policy](../matrix.md#delivery-policy), and [End-to-End Encryption](../matrix.md#end-to-end-encryption).

## Model Selection Protocol

See [Model Overrides in Chat](../configuration/models.md#model-overrides-in-chat).

Discovery uses Olm-encrypted to-device events, including for unencrypted rooms, so it needs no room-state advertisement, state permissions, or external endpoint.
Only the router registers the receiver.
The receiver authenticates the actual requesting device and requires the requester and router to be currently joined, plus at least one configured, joined agent the requester may address.
An included thread must be a readable, unredacted root in that room; encrypted roots are decrypted before validation.
Replies go only to the authenticated device, and sending a catalog never verifies a previously untrusted device.
Blocked devices, malformed requests, and unauthorized room or thread scopes receive no response.

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
Icons use Matrix `mxc://` URIs only; other local icon rules are on the [model configuration page](../configuration/models.md).

Application processing expires after 12 seconds; cancellation does not retract a send already retained by NIO.
Admission caps queued/in-flight requests at eight, and at two per Matrix user across all of that user's devices, and accepts at most eight fresh requests per user in 12 seconds; concurrent duplicate requests share the active request.
Immediately before handing the response to NIO, MindRoom rechecks current scope, captured device identity, and config identity.
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
Accept an acknowledgement only for the client's current pending command event, exact room/thread, runtime user/device, operation, and model.
Encrypted events additionally authenticate the sender device.
In plaintext rooms, Matrix authenticates the sender account; device metadata only correlates the result.
Serialize pending changes per runtime/thread and prevent earlier discovery results from replacing later confirmed changes.
Other clients and text commands become visible on refresh; there is no global selection revision.
