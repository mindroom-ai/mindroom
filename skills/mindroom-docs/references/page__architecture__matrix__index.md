# Matrix Integration

MindRoom uses the Matrix protocol for all agent communication. The integration is implemented in `src/mindroom/matrix/`.

## Why Matrix?

- **Federated** - Connect to any Matrix homeserver
- **Bridgeable** - Bridge to Discord, Slack, Telegram, and more
- **Open** - Open standard and open-source implementations
- **End-to-End Encryption** - Secure communication with encrypted room support

## Matrix Client

MindRoom uses `mindroom-nio` for Matrix communication with SSL context handling and encryption key storage.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MATRIX_HOMESERVER` | `http://localhost:8008` | Matrix homeserver URL |
| `MATRIX_SERVER_NAME` | (from homeserver) | Federation server name |
| `MATRIX_SSL_VERIFY` | `true` | Set to `false` for dev/self-signed certs |
| `MATRIX_MANAGED_ACCOUNT_AUTH` | `password` | Authentication for accounts created and operated by MindRoom: `password` or `appservice` |
| `MATRIX_APPSERVICE_TOKEN` | -- | Application-service token used when managed account auth is `appservice` |
| `MATRIX_APPSERVICE_TOKEN_FILE` | -- | File alternative to `MATRIX_APPSERVICE_TOKEN` |

Streaming behavior is configured in `config.yaml` with `defaults.enable_streaming` (default: `true`).

## Agent Users

Each agent, team, and router has its own Matrix user.

The configured alias is the user-facing runtime handle, such as `@assistant` in chat.

Provisioning may request localparts such as `mindroom_assistant` or `mindroom_router`, but persisted Matrix state is authoritative after provisioning and may contain a different username.

For example, a persisted Matrix account such as `@assistant_live:example.com` can become the live assistant account even if the original provisioning request used `mindroom_assistant`.

Users are automatically created during orchestrator startup and credentials are persisted in `mindroom_data/matrix_state.yaml`.

Password mode generates a separate password for every managed account and uses normal Matrix registration and login.
Application-service mode registers passwordless accounts inside an exclusive application-service namespace, then obtains a normal per-user device token for encryption and sync.
Set `MATRIX_MANAGED_ACCOUNT_AUTH=appservice` and provide exactly one of `MATRIX_APPSERVICE_TOKEN` or `MATRIX_APPSERVICE_TOKEN_FILE`.
The application-service token is used only for account registration and fresh device login; normal agent traffic uses each account's own persisted device token.
Existing passwords are removed from `matrix_state.yaml` after a successful application-service login.

## Room Management

Agents can join existing rooms, create new rooms with AI-generated topics, respond to invites automatically, leave unconfigured rooms, and set room avatars.

Rooms are auto-created via `_ensure_room_exists()` (private) and `ensure_all_rooms_exist()` (public). DM rooms can be detected with `async is_dm_room(client, room_id) -> bool`.

## Threading (MSC3440)

MindRoom emits thread replies following [MSC3440](https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/3440-threading-via-relations.md), using `m.relates_to` with `rel_type: m.thread`.

Explicit `m.thread` metadata remains the primary source of thread conversation context.
For clients or bridges that send plain replies without thread metadata (`m.in_reply_to` but no `rel_type: m.thread`), MindRoom applies a transitive compatibility rule.
If a reply chain eventually reaches explicit thread `T` or a proven thread root, MindRoom treats the new reply as part of `T`.
Replies that never reach threaded context stay room-level.

### Resolution Rules

When deriving context for an incoming event, MindRoom:

1. Uses explicit `m.thread` relations as the primary inbound thread identity.
2. Lets plain replies inherit thread membership transitively when their reply chain reaches a threaded ancestor or proven thread root.
3. Lets edits, reactions, redactions, and other target-bound operations inherit the canonical thread membership of their target event.
4. May start a new thread under a room-root event when agent thread mode requires it.

```
├── User: @assistant help with this code
│   ├── Assistant: I can help! Let me look at it...
│   ├── User: It should return a list
│   └── Assistant: Here's the updated version...
```

Use `build_message_content()` from `message_builder.py` to construct thread-aware messages, and `EventInfo.from_event()` to analyze event relations (threads, edits, replies, reactions).

## Message Flow

### Sync Loop

Each agent bot runs its own sync loop with a 30-second long-polling timeout.
The default `matrix_sync.mode: classic` streams events through classic `/v3/sync` and backfills limited-timeline gaps from `/messages`.
Set `matrix_sync.mode: sliding` to opt into MSC4186 Simplified Sliding Sync on homeservers that advertise `org.matrix.simplified_msc3575`.
`matrix_sync.sliding_timeline_limit` (default 100) bounds the per-room timeline window of each sliding request.
Sliding positions remain connection-scoped, while callback admission uses mindroom-nio's persisted per-event provenance.
Sliding Sync classifies its validated `num_live` tail as live, ordinary continuations without `num_live` as live, and initial or expanded timelines without `num_live` as history.
Classic Sync classifies initial timelines and `/messages` recovery as history, while `since` continuations are live.
This provenance remains attached across recovery, restart, and decryption independently of journal checkpoint persistence.
`matrix/journal_ingress.py` commits every inbound event to the event journal before nio treats it as delivered, and a refused write raises `nio.CallbackNotAcceptedError` so nio redelivers the event rather than advancing the checkpoint past it.
Admission is fail-closed at every provenance, not only for recovery, because an event the journal never accepted is one no later process would see again.
Conversation history is hydrated on demand rather than pre-warmed at join: a bounded backward walk fills one room or thread and records the membership epoch it filled under, so a rejoin rebuilds from what the new membership can see instead of merging two memberships into one conversation.
Changing `matrix_sync` restarts running entities on config hot reload.
Sync loops are wrapped with `sync_forever_with_restart()` for automatic restart on connection failures.

An event reaches an agent through durable admission, never straight from the sync callback:

1. Sync receives the event via long-polling, and nio states its provenance once.
2. `JournalIngress._admit` runs first, as nio's event-admission callback, and commits the event and its projection row in one transaction before nio treats the event as delivered.
3. Only after every admission callback succeeds do the ordinary event callbacks run, and they load already-persisted work rather than the parsed event they were handed.
4. `PendingEventWorker` drains what is still pending, so an event whose turn was interrupted is re-dispatched instead of lost.
5. `TurnController` owns the turn and the agent responds in thread.

Invites are the deliberate exception: an invite has no Matrix event ID to key a durable row on, and an unacted-on invite reappears in every sync response, so `_on_invite` is a plain background task relying on homeserver redelivery.
See [Bot Runtime](https://docs.mindroom.chat/architecture/bot-runtime/) for the full durable dispatch boundary.

### Streaming Responses

Agents stream responses by progressively editing messages.
Streaming is enabled only when the requesting user is online (checked via `should_use_streaming()`), saving API calls for offline users.
See [Streaming Responses](https://docs.mindroom.chat/streaming/) for the full feature documentation.

Tool call telemetry is emitted as plain inline markers and mirrored in `io.mindroom.tool_trace` metadata on the same message content.

Marker format:
```text
🔧 `tool_name` [N] ⏳     ← pending
🔧 `tool_name` [N]        ← completed
```

Where `N` is 1-indexed per message and maps to `io.mindroom.tool_trace.events[N-1]`.

## Presence

Agents set their Matrix presence with status messages containing model and role information (e.g., "🤖 Model: anthropic/claude-sonnet-5 | 💼 Code assistant | 🔧 5 tools available").

**Presence States:**
- **online** - Agent running and ready
- **unavailable** - Agent idle but connected (treated as online for streaming)
- **offline** - Agent stopped or disconnected

## Typing Indicators

Agents show typing indicators while processing via `typing_indicator()` context manager.
The indicator auto-refreshes at `min(timeout/2, 15)` seconds to remain visible during long operations.

## Mentions

Mentions are parsed via `format_message_with_mentions()` which handles multiple formats:
- `@calculator` - Stable configured agent or team key
- `@actual_calculator:localhost` - Current full Matrix ID

Bare Matrix account localparts such as `@actual_calculator` are not runtime handles.
A generated-looking full Matrix ID such as `@mindroom_calculator:localhost` is not a runtime handle unless it is the current persisted Matrix ID for that agent or team.

Returns content with `m.mentions` and `formatted_body` containing clickable links.

## Large Messages

Messages exceeding the 64KB Matrix event limit are automatically handled by `prepare_large_message()`:

- Messages > 55,000 bytes and edits > 27,000 bytes use a fallback event
- Full original Matrix message content is uploaded as a JSON sidecar (`message-content.json`)
- Preview text included in message body (maximum that fits)
- Custom metadata dict `io.mindroom.long_text` contains `version: 2`, `encoding: "matrix_event_content_json"`, original and preview sizes, and a completeness flag
- Preview event is compact (for example no inline `io.mindroom.tool_trace`), while the sidecar preserves full content fidelity
- Encrypted rooms: sidecar JSON is encrypted before upload (`message-content.json.enc`)

## Response Tracking

Duplicate responses are prevented at two durable layers, both in `tracking/event_journal.db` under `mindroom_data/`.

`journal_events` is keyed `(principal_id, event_id)`, so a Matrix event redelivered by a sync reconnection or a `/messages` walk is recognised as already admitted rather than admitted twice.
A settled row is retained for exactly that reason, with only its replay payload cleared.

`TurnStore` owns the answer to "has this turn finished?", through the handled-turn ledger in `handled_turns.py`.
It shares the journal's database, so a terminal turn record and the settlement of the journal sources it answers commit in one transaction instead of two substrates approximately agreeing.
Its scope is the agent rather than the sync principal, because the proof that a message was already answered stays true across a re-login.

Delivery itself is owned by the `response_outbox` table, keyed `(principal_id, turn_id, stage)` over an `INITIAL` placeholder and a `FINAL` answer.
A row's payload is claimed before the first send attempt and its Matrix transaction ID is deterministic, so a crash between sending and recording resolves by resending the same row rather than by generating a second, different answer.
The claim also stores the sending device, because a transaction ID is only idempotent for the device that used it and a re-login would otherwise let a resend post a duplicate.

## Room Cleanup

On startup, MindRoom detects orphaned bot memberships left over from a previous configuration.
When an agent is removed from `config.yaml`, its Matrix bot account may still be a member of rooms it previously joined.
The cleanup process leaves those rooms safely without ejecting currently configured entities from their required rooms.
This runs automatically — no manual intervention is needed.

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

## Root Space

MindRoom can create and maintain a root Matrix Space that groups all managed rooms.

```yaml
matrix_space:
  enabled: true        # Default: true
  name: MindRoom       # Display name for the Space
```

When enabled, `ensure_root_space()` creates the Space on first boot (or resolves an existing one by alias), links all managed rooms as children, and sets the Space avatar from workspace or bundled assets.
The Space name is reconciled on each startup to match the configured value.
Root Space admin power is granted before child links are written.
The grant set is the concrete Matrix users in `authorization.global_users` plus the configured `mindroom_user` when that internal account exists.
MindRoom does not remove existing Space admins during reconciliation, including manual Matrix admins or users removed from `authorization.global_users`.
Room-scoped authorization entries are intentionally not used for root Space admin grants.

## Delivery Policy

Outgoing encrypted Matrix sends always deliver to unverified devices.
MindRoom bots have no interactive device-verification flow, so enforcing nio's device-trust checks would fail every send to an encrypted room with an `OlmUnverifiedDeviceError` and the agent would appear to silently ignore messages.
A configurable trust policy only becomes meaningful once a device-verification mechanism exists (for example trust-on-first-use, a verification command, or cross-signing support).

While a room's timeline is still recovering from a limited sync, nio rejects sends to that room with `SendRetryError` until the gap closes, so MindRoom retries the affected delivery in place instead of dropping it.
Streaming progress updates and completed terminal deliveries reuse the identical prepared payload and retry for up to 30 seconds — one recovery pump — backing off from 50ms to 500ms between attempts.
Cancelled and errored terminal updates never wait on recovery, so a stopped or failed turn still settles immediately.
If the window expires the delivery is reported as failed, the placeholder settles as a delivery failure, and the failure update itself is sent without waiting on recovery again.

## End-to-End Encryption

Agents fully participate in encrypted rooms: they decrypt inbound text and media, reply encrypted, and re-fetch and decrypt thread history from the homeserver.
Managed rooms can be created encrypted via `rooms.<key>.encrypted: true` or `matrix_room_access.encrypt_managed_rooms: true`, and existing managed rooms are reconciled to encrypted on startup and config reload when so configured.
Users can also enable encryption in any room with `!encrypt confirm` (room admin only), and `!e2ee` reports encryption diagnostics.
Enabling encryption on a Matrix room is irreversible; MindRoom never disables it.

When an agent receives an event it cannot decrypt from an authorized sender, it logs a `matrix_event_decryption_failed` warning, sends a best-effort room-key request once per session (delivered to the bot account's own devices, so recovery normally needs the sender to post a new message), and posts one notice per (room, session) so the user knows to resend.
All bots share a disk-backed notice ledger, so the first bot that fails on a session posts the only notice and multi-agent rooms never storm.
After a live room join, decryption-failure callbacks for that exact unfinished join stay fenced across restarts until a trusted sync response confirms joined membership.
A rejected sync certification keeps that join fence closed; once admission succeeds again, the next trusted response atomically advances continuity and clears the fence.
The fence suppresses only the user-visible notice, so a fenced failure still logs diagnostics, updates E2EE statistics, and requests missing keys without claiming the visible-notice ledger.
Cold history is admitted rather than rejected: nio's `HISTORY` provenance classes an event context-only, so it joins the conversation the projection serves but can never start a turn.
`LIVE` and recovered events are admitted as actionable independently of response-level sync positions, recovery gaps, and sync-certification state.
The join fence does not compare federated event timestamps with the local wall clock.
Decryption-failure counters are exposed on `/api/health` under `e2ee`.

Each agent bootstraps a self-managed cross-signing identity at login (master and self-signing keys persisted next to its encryption store) and signs its own device, so clients that exclude non-cross-signed devices (MSC4153) keep sharing room keys with agents.
`!e2ee` reports the cross-signing status.
When the homeserver no longer has the uploaded identity (for example after a dev-server reset that kept `encryption_keys/`), the bootstrap detects the divergence and re-uploads the persisted keys instead of wedging.

If a bot's encryption store under `mindroom_data/encryption_keys/` is lost while its device identity persists, startup logs in as a fresh device instead of restoring a wedged crypto identity, and re-signs the new device with the persisted cross-signing keys; `mindroom doctor` reports missing stores.
Messages encrypted only to the lost device stay undecryptable, but the durable visible-message projection preserves the agent's conversational context.

## Configuration

Matrix settings are derived from `config.yaml`:

```yaml
agents:
  assistant:
    rooms: [lobby, dev]  # Room aliases (auto-created if needed)

teams:
  research_team:
    rooms: [research]
```

Room aliases are resolved to room IDs automatically. Full room IDs (starting with `!`) are also supported.

When a room doesn't exist, it's created with an AI-generated topic, power users are invited, and managed avatars are resolved from workspace overrides or bundled defaults if available.
