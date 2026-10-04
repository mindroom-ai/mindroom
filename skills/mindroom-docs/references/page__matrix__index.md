# Matrix Integration

## Why Matrix?

- **Federated** - Connect to any Matrix homeserver
- **Bridgeable** - Bridge to Discord, Slack, Telegram, and more
- **Open** - Open standard and open-source implementations
- **End-to-End Encryption** - Secure communication with encrypted room support

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

## Internal User

The optional `mindroom_user` section configures MindRoom's internal Matrix user account; omit it for hosted or public profiles.

```yaml
mindroom_user:
  username: mindroom_user
  display_name: MindRoomUser
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `username` | string | `mindroom_user` | Matrix localpart to request, without `@` or a domain; set it before first startup, because it cannot be changed in place after the account exists |
| `display_name` | string | `MindRoomUser` | Display name, changeable at any time |

Hosted provisioning can return a different actual Matrix ID, which MindRoom then uses.

## Managed Avatars

Every agent, team, and managed room gets a Matrix avatar by default, chosen from the painted stock avatars in the [MindRoom assets repository](https://github.com/mindroom-ai/assets/blob/main/avatars/painted/README.md).

- An agent named after a stock avatar (for example `mind`, `code`, `research`, `writer`, or `email`) uses that picture, and any other agent or team receives a stable stock avatar chosen from its name.
- A room served by exactly one agent or team shows that entity's avatar, and any other room receives a stable stock avatar chosen from its key.
- The root Matrix Space uses `avatars/spaces/root_space.png` when present and otherwise the stock `mind-logo` image.
- Avatars are filled in only where the Matrix profile or room has none, so pictures you set yourself are kept.
- Managed room avatars are chosen only when a room is created, so run `mindroom avatars sync` to fill in existing rooms.
- A failed stock avatar download is retried at the first start after 24 hours; `mindroom avatars sync` retries room and root Space avatars immediately.

To choose a picture, place a PNG at `avatars/<agents|teams|rooms|spaces>/<name>.png` next to `config.yaml`; containerized deployments read these overrides from `<storage>/avatars/` instead.
Run `mindroom avatars sync --force` to reset every managed room and the root Space to its default avatar: its own file, its single agent's or team's avatar, or its stock pick.

`mindroom avatars generate` creates a custom avatar file for every entity without a workspace or bundled avatar file, using `gpt-6-astra` for prompt creation and `gpt-image-2.5-sunburst` for rendering; it requires only `OPENAI_API_KEY` or `OPENAI_API_KEY_FILE`.
Run `mindroom avatars generate --force` to overwrite existing workspace avatar files after changing prompts or styles.
Override the generation styles with the root `prompts` block:

```yaml
prompts:
  AVATAR_CHARACTER_STYLE: "professional AI avatar portrait, abstract geometric silhouette"
  AVATAR_ROOM_STYLE: "minimalist wayfinding icon, precise geometry, strong silhouette"
  AVATAR_AGENT_SYSTEM_PROMPT: "You are creating distinctive visual elements for a professional AI agent avatar."
  AVATAR_TEAM_SYSTEM_PROMPT: "You are creating distinctive visual elements for a professional AI team avatar."
  AVATAR_ROOM_SYSTEM_PROMPT: "You are creating a refined, minimalist icon design for a room avatar."
```

## Mentions

Mentions are parsed via `format_message_with_mentions()` which handles multiple formats:
- `@calculator` - Stable configured agent or team key
- `@actual_calculator:localhost` - Current full Matrix ID

Bare Matrix account localparts such as `@actual_calculator` are not runtime handles.
A generated-looking full Matrix ID such as `@mindroom_calculator:localhost` is not a runtime handle unless it is the current persisted Matrix ID for that agent or team.

Returns content with `m.mentions` and `formatted_body` containing clickable links.

## TLS Trust

The startup readiness probe trusts the same certificates as the `mindroom-nio` Matrix clients: OpenSSL's default CA file and directory plus certifi's roots.
Setting `SSL_CERT_FILE` or `SSL_CERT_DIR` replaces the matching OpenSSL default location, and setting either one stops MindRoom from adding certifi's roots.
A homeserver certificate that fails verification during the startup probe stops startup with a permanent error naming the homeserver and the verification failure.
Matrix client requests, including login, keep their usual connection-failure handling, because a captive portal or TLS interception can clear up on its own.
Each Matrix client logs the first request of an outage that cannot reach the homeserver as a `matrix_request_transport_failed` warning with aiohttp's error type and message, and logs repeats at debug level until the homeserver answers a request.
Requests to the provisioning service always verify its certificate, whatever `MATRIX_SSL_VERIFY` says, because that service names the install's owner and issues its client credentials.
For a provisioning service behind a private CA, set `SSL_CERT_FILE` or `SSL_CERT_DIR` in the process environment.

## Matrix Sync

```yaml
matrix_sync:
  mode: classic
  sliding_timeline_limit: 100
  max_response_bytes: 16777216
  max_pending_bytes: 67108864
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `classic` or `sliding` | `classic` | `classic` uses `/v3/sync`; `sliding` uses MSC4186 Simplified Sliding Sync and requires a homeserver that advertises it |
| `sliding_timeline_limit` | integer ≥ 1 | `100` | Per-room timeline window for Sliding Sync |
| `max_response_bytes` | integer ≥ 1 | `16777216` (16 MiB) | Maximum HTTP sync response size per bot |
| `max_pending_bytes` | integer ≥ 1 | `67108864` (64 MiB) | Maximum encoded pending output per bot |

A durable store stays bound to the `matrix_sync.mode` it first used.
If a large initial sync exceeds the response bound, increase `max_response_bytes` enough to fit the response.
If preparing events exceeds the pending bound, increase `max_pending_bytes`; encoded events can take more space than the HTTP response, so raising the response limit may also require raising the pending limit.
Each bot has its own sync session, so allow sufficient memory and disk space when increasing these limits.
Changing `matrix_sync` through config reload restarts running agents.
Large initial syncs may also need more startup time: `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` sets the first-sync allowance, and the runtime Helm chart's `probes.startup` values set the Kubernetes startup probe allowance.

## End-to-End Encryption

Agents fully participate in encrypted rooms: they decrypt inbound text and media, reply encrypted, and re-fetch and decrypt thread history from the homeserver.
Managed rooms can be created encrypted through `room_defaults.encrypted: true` or `rooms.<key>.encrypted: true`, and existing managed rooms are reconciled to encrypted on startup and config reload when so configured.
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

### `!encrypt`

Enable Matrix end-to-end encryption for the current room.

```
!encrypt
!encrypt confirm
```

`!encrypt` reviews what enabling encryption means for the room without changing anything.
`!encrypt confirm` enables encryption and is a Matrix room-admin-only action.
Enabling encryption is irreversible: a room can never go back to unencrypted, and people joining later cannot read messages sent before they joined.
Managed rooms can also be encrypted with `rooms.<key>.encrypted: true`.

### `!e2ee`

Show encryption diagnostics for the current room.

```
!e2ee
```

The report includes the room's encryption state, the responding bot account and device, the encryption store status, and decryption-failure counters since startup.
Use it when an agent seems to ignore messages in an encrypted room.

## Delivery Policy

Outgoing encrypted Matrix sends always deliver to unverified devices.
MindRoom bots have no interactive device-verification flow, so enforcing nio's device-trust checks would fail every send to an encrypted room with an `OlmUnverifiedDeviceError` and the agent would appear to silently ignore messages.
A configurable trust policy only becomes meaningful once a device-verification mechanism exists (for example trust-on-first-use, a verification command, or cross-signing support).

While a room's timeline is still recovering from a limited sync, nio rejects sends to that room with `SendRetryError` until the gap closes, so MindRoom retries the affected delivery in place instead of dropping it.
Streaming progress updates and completed terminal deliveries reuse the identical prepared payload and retry for up to 30 seconds — one recovery pump — backing off from 50ms to 500ms between attempts.
Cancelled and errored terminal updates never wait on recovery, so a stopped or failed turn still settles immediately.
If the window expires the delivery is reported as failed, the placeholder settles as a delivery failure, and the failure update itself is sent without waiting on recovery again.
