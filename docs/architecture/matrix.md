---
icon: lucide/message-square
---

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

Streaming behavior is configured in `config.yaml` with `defaults.enable_streaming` (default: `true`).

## Agent Users

Each agent, team, and router has its own Matrix user.

The configured alias is the user-facing runtime handle, such as `@assistant` in chat.

Provisioning may request localparts such as `mindroom_assistant` or `mindroom_router`, but persisted Matrix state is authoritative after provisioning and may contain a different username.

For example, a persisted Matrix account such as `@assistant_live:example.com` can become the live assistant account even if the original provisioning request used `mindroom_assistant`.

Users are automatically created during orchestrator startup and credentials are persisted in `mindroom_data/matrix_state.yaml`.

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

Each agent bot runs its own sync loop with 30-second long-polling timeout. Sync loops are wrapped with `sync_forever_with_restart()` for automatic restart on connection failures.

Events are processed in background tasks:
1. Sync receives event via long-polling
2. Event callback triggered (`_on_message`, `_on_invite`, etc.)
3. Background task created for async processing
4. Agent responds in thread

### Streaming Responses

Agents stream responses by progressively editing messages.
Streaming is enabled only when the requesting user is online (checked via `should_use_streaming()`), saving API calls for offline users.
See [Streaming Responses](../streaming.md) for the full feature documentation.

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

MindRoom prevents duplicate responses using a `ResponseTracker` that records which events have already been processed.
When a sync reconnection or retry delivers the same event twice, the tracker suppresses the duplicate so only one agent response is sent per triggering message.
Tracking state is persisted under `mindroom_data/tracking/` and survives restarts.

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

## End-to-End Encryption

Agents fully participate in encrypted rooms: they decrypt inbound text and media, reply encrypted, and re-fetch and decrypt thread history from the homeserver.
Managed rooms can be created encrypted via `rooms.<key>.encrypted: true` or `matrix_room_access.encrypt_managed_rooms: true`, and existing managed rooms are reconciled to encrypted on startup and config reload when so configured.
Users can also enable encryption in any room with `!encrypt confirm` (room admin only), and `!e2ee` reports encryption diagnostics.
Enabling encryption on a Matrix room is irreversible; MindRoom never disables it.

When an agent receives an event it cannot decrypt from an authorized sender, it logs a `matrix_event_decryption_failed` warning, sends a best-effort room-key request once per session (delivered to the bot account's own devices, so recovery normally needs the sender to post a new message), and posts one notice per (room, session) so the user knows to resend.
All bots share a disk-backed notice ledger, so the first bot that fails on a session posts the only notice and multi-agent rooms never storm.
Notices are suppressed for events that predate a bot's room join or a start without sync continuity, since pre-join and already-replayed history is expected to be undecryptable.
Decryption-failure counters are exposed on `/api/health` under `e2ee`.

Each agent bootstraps a self-managed cross-signing identity at login (master and self-signing keys persisted next to its encryption store) and signs its own device, so clients that exclude non-cross-signed devices (MSC4153) keep sharing room keys with agents.
`!e2ee` reports the cross-signing status.
When the homeserver no longer has the uploaded identity (for example after a dev-server reset that kept `encryption_keys/`), the bootstrap detects the divergence and re-uploads the persisted keys instead of wedging.

If a bot's encryption store under `mindroom_data/encryption_keys/` is lost while its device identity persists, startup logs in as a fresh device instead of restoring a wedged crypto identity, and re-signs the new device with the persisted cross-signing keys; `mindroom doctor` reports missing stores.
Messages encrypted only to the lost device stay undecryptable, but the durable event cache preserves the agent's conversational context.

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
