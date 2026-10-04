---
icon: simple/matrix
---

# Matrix Integration

This page covers how MindRoom appears on Matrix: the accounts it creates, mentions, end-to-end encryption, managed avatars, TLS trust, and sync tuning.
Room creation and room policy are covered in [Rooms](rooms.md), and Matrix environment variables in [Configuration](configuration/index.md#matrix).

## Why Matrix?

Every agent is an ordinary Matrix user, so you can reach it from any Matrix client, on any federated homeserver, in end-to-end encrypted rooms, and from Discord, Slack, Telegram, and other networks through [bridges](deployment/bridges/index.md).

## Agent Users

Each agent, team, and the router has its own Matrix user, created automatically at startup.
Account credentials are stored in `matrix_state.yaml` in the storage directory (default `mindroom_data/`).

In chat, address an agent or team by its configured key, such as `@assistant`.
MindRoom requests localparts such as `mindroom_assistant`, but the account recorded in `matrix_state.yaml` is authoritative, so an agent's actual Matrix ID (for example `@assistant_live:example.com`) can differ from the requested one.

`MATRIX_MANAGED_ACCOUNT_AUTH` selects how these accounts authenticate:

- `password` (default) generates a separate password for every account and uses normal Matrix registration and login.
- `appservice` registers passwordless accounts inside an exclusive application-service namespace and gives each account its own device token for encryption and sync.
  First register an application service with the homeserver whose exclusive user namespace covers every account MindRoom manages, then supply that registration's `as_token`.
  Provide exactly one of `MATRIX_APPSERVICE_TOKEN` or `MATRIX_APPSERVICE_TOKEN_FILE`; setting either one without `MATRIX_MANAGED_ACCOUNT_AUTH=appservice` stops startup.
  The application-service token is used only to register accounts and log in new devices.
  After switching an existing install to `appservice`, stored passwords are removed from `matrix_state.yaml` once each account logs in.

## Internal User

The optional `mindroom_user` section configures MindRoom's internal Matrix user account; omit it for hosted or public profiles.

```yaml
mindroom_user:
  username: mindroom_user
  display_name: MindRoomUser
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `username` | string | `mindroom_user` | Matrix localpart, without `@` or a domain, using lowercase letters, digits, `.`, `_`, `=`, `-`, or `/`; it must not match an agent, team, or router account |
| `display_name` | string | `MindRoomUser` | Display name, changeable at any time |

Set `username` before first startup.
Changing it after the account exists stops startup with `mindroom_user.username cannot be changed after the internal Matrix account has been created.`
Hosted provisioning can return a different actual Matrix ID, which MindRoom then uses.

## Mentions

In agent replies and other messages MindRoom sends, two forms become real Matrix mentions that render as clickable links and notify the agent or team:

- `@calculator`, the configured agent or team key.
- `@actual_calculator:localhost`, the agent's current full Matrix ID.

`@mindroom_calculator`, the `mindroom_` prefix plus the key, also mentions the agent's or team's current account.
Any other bare account localpart, such as `@actual_calculator`, is not a mention.
Any other valid full Matrix ID, such as a human user or an agent's old account like `@mindroom_calculator:localhost`, becomes a mention of that exact account and does not reach the agent's current account.

## End-to-End Encryption

Agents fully participate in encrypted rooms: they read encrypted text, media, and thread history, and they reply encrypted.
To encrypt managed rooms, set `encrypted: true` in their [room policy](rooms.md#room-policy).
Enabling encryption on a Matrix room is irreversible.

Each agent cross-signs its own device, so clients that only share room keys with cross-signed devices (MSC4153) keep sharing them with agents.

### `!encrypt`

Enable end-to-end encryption for the current room.

```
!encrypt
!encrypt confirm
```

`!encrypt` explains what enabling encryption means for the room without changing anything.
`!encrypt confirm` enables encryption and is restricted to Matrix room admins.
The responding bot also needs permission to change room state; if it lacks that permission, a room admin can enable encryption from their own Matrix client.
People joining an encrypted room later cannot read messages sent before they joined.

### `!e2ee`

Show encryption diagnostics for the current room.

```
!e2ee
```

The report includes the room's encryption state, the responding bot account and device, the encryption store status, cross-signing status, and decryption-failure counters since startup.
Use it when an agent seems to ignore messages in an encrypted room.

### Troubleshooting Undecryptable Messages

When an agent cannot decrypt a message from an authorized sender, it logs a `matrix_event_decryption_failed` warning and posts one notice for that message's encryption session asking the user to resend.
Only one agent posts the notice, even in multi-agent rooms.
Resending normally fixes it, because the sender's client then shares a new key with the agent.
Decryption-failure counters also appear on `/api/health` under `e2ee`.

If a bot's encryption store under `mindroom_data/encryption_keys/` is lost, that agent does not start, and `mindroom doctor` reports the missing store.
Restore the matching storage backup before restarting, as described in [Device recovery](deployment/upgrades.md#device-recovery-after-the-upgrade).

### Delivery Policy

Encrypted messages from agents are always shared with the room members' unverified devices.
MindRoom has no device-verification flow, so requiring verified devices would make every reply in an encrypted room fail.

## Managed Avatars

Every agent, team, and managed room gets a Matrix avatar by default, chosen from the painted stock avatars in the [MindRoom assets repository](https://github.com/mindroom-ai/assets/blob/main/avatars/painted/README.md).

- An agent named after a stock avatar (for example `mind`, `code`, `research`, `writer`, or `email`) uses that picture, and any other agent or team receives a stable stock avatar chosen from its name.
- A room served by exactly one agent or team shows that entity's avatar, and any other room receives a stable stock avatar chosen from its key.
- The root Matrix Space uses `avatars/spaces/root_space.png` when present and otherwise the stock `mind-logo` image.
- Avatars are set only where the Matrix profile or room has none, so pictures you set yourself are kept.
- Room avatars are chosen only when a room is created, so run `mindroom avatars sync` to fill in existing rooms.
- A failed stock avatar download is retried at the first start after 24 hours, or immediately for rooms and the root Space by `mindroom avatars sync`.

To choose a picture, place a PNG at `avatars/<agents|teams|rooms|spaces>/<name>.png` next to `config.yaml`; containerized deployments read these overrides from `<storage>/avatars/` instead.
Run `mindroom avatars sync --force` to reset every managed room and the root Space to its default avatar: its own file, its single agent's or team's avatar, or its stock pick.

`mindroom avatars generate` creates a custom avatar file for every entity without a workspace or bundled avatar file, using `gpt-6-astra` to write prompts and `gpt-image-2.5-sunburst` to render them; it requires only `OPENAI_API_KEY` or `OPENAI_API_KEY_FILE`.
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

## TLS Trust

MindRoom trusts OpenSSL's default CA file and directory plus certifi's public roots for the homeserver.
For a homeserver behind a private CA, set `SSL_CERT_FILE` or `SSL_CERT_DIR`; each replaces the matching OpenSSL default location, and setting either one stops MindRoom from adding certifi's roots.
A homeserver certificate that fails verification stops startup with an error naming the homeserver and the verification failure.

Requests to the hosted provisioning service always verify its certificate, whatever `MATRIX_SSL_VERIFY` says.
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
| `max_pending_bytes` | integer ≥ 1 | `67108864` (64 MiB) | Maximum size of received events waiting to be processed per bot |

Choose `mode` before first startup, because each bot's stored sync state stays bound to the mode it first used.
Changing `matrix_sync` through config reload restarts running agents.

If a large initial sync exceeds the response limit, raise `max_response_bytes` enough to fit it.
If processing received events exceeds the pending limit, raise `max_pending_bytes`; stored events can take more space than the HTTP response, so raising the response limit may also require raising the pending limit.
Each bot has its own sync session, so allow enough memory and disk space when raising these limits.
Large initial syncs may also need more startup time: `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` sets the first-sync allowance, and the runtime Helm chart's `probes.startup` values set the Kubernetes startup probe allowance.
