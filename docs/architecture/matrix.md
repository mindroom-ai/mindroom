---
icon: lucide/message-square
---

# Matrix Integration

This page records the Matrix contracts that MindRoom's code does not make obvious: one identity invariant and the model-selection wire protocol that external clients implement.
User-facing Matrix behavior is on [Matrix Integration](../matrix.md), event-journal decisions and homeserver behaviour are on [Matrix Event-Journal Contracts](matrix-event-journal-contracts.md), and invites and delivery epochs are on [Bot Runtime](bot-runtime.md#durable-dispatch-boundary).

## Identity Invariant

Code must resolve an entity's Matrix ID through the persisted identity registry, keyed by configured alias, and never derive it from the requested localpart, because the actual account can differ (see [Agent Users](../matrix.md#agent-users)).

## Model Selection Protocol

See [Model Overrides in Chat](../configuration/models.md#model-overrides-in-chat).

Discovery uses Olm-encrypted to-device events, including for unencrypted rooms, so it needs no room-state advertisement, state permissions, or external endpoint.
Only the router answers discovery.
It authenticates the actual requesting device and requires the requester and router to be currently joined, plus at least one configured, joined agent the requester may address.
An included thread must be a readable, unredacted root in that room, whether or not it is encrypted.
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

The router stops processing a request after 12 seconds, but a response already handed to nio is not retracted.
Admission caps queued/in-flight requests at eight, and at two per Matrix user across all of that user's devices, and accepts at most eight fresh requests per user in 12 seconds; concurrent duplicate requests share the active request.
Authorization is last checked when the response is handed to nio, so a requester who loses room access while nio prepares or retries the send can still receive that already-authorized catalog.
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
