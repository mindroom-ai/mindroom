---
icon: lucide/shield
---

# Authorization

MindRoom controls which Matrix users can interact with agents.

Room access (joinability/discoverability) is configured separately through `matrix_room_access`.

## Configuration

Configure authorization in `config.yaml`:

```yaml
authorization:
  # Users with access to all rooms
  global_users:
    - "@admin:example.com"
    - "@developer:example.com"

  # Room-specific permissions (room ID, full alias, or managed room key)
  room_permissions:
    "!abc123:example.com":
      - "@user1:example.com"
      - "@user2:example.com"
    "#lobby:example.com":
      - "@user3:example.com"
    "ops":
      - "@user4:example.com"

  # Default for rooms not in room_permissions
  default_room_access: false

  # Optional: per-agent/team/router reply allowlists
  # Keys must match an agent name, team name, "router", or "*"
  # Values are canonical Matrix user IDs or glob patterns (aliases are resolved)
  # Examples: "*:example.com", "@admin:*", "*"
  agent_reply_permissions:
    "*":
      - "@admin:example.com"
    code:
      - "@admin:example.com"
    research:
      - "@developer:example.com"
    router:
      - "*"

# Optional: configure the internal MindRoom user identity
mindroom_user:
  username: mindroom_user          # Set before first startup (cannot be changed later)
  display_name: MindRoomUser

# Optional: room onboarding/discoverability policy
matrix_room_access:
  mode: single_user_private        # default
  multi_user_join_rule: public     # public or knock (multi_user only)
  publish_to_room_directory: false # publish managed rooms to public directory
  invite_only_rooms: []            # room keys/aliases/IDs that stay restricted
  reconcile_existing_rooms: false  # migrate existing managed rooms when true
```

**Defaults** (when `authorization` block is omitted):

- `global_users: []`
- `room_permissions: {}`
- `default_room_access: false`
- `agent_reply_permissions: {}`

This means only MindRoom system users (agents, teams, router, and the configured internal user, default `@mindroom_user`) can interact with agents by default.

`mindroom_user.username` is a one-time setting used to create the internal Matrix account. After the account exists, keep the same username and only change `mindroom_user.display_name` for visible name changes.

For `authorization.room_permissions`, MindRoom accepts these key formats:

- Room ID: `!roomid:example.com`
- Full room alias: `#alias:example.com`
- Managed room key: `alias` (the configured room name/key used by MindRoom)

## Matrix Room Onboarding for OIDC Users

When users authenticate through Synapse OIDC, they are regular Matrix users. To let them join managed MindRoom rooms by alias without manual invites:

1. Set `matrix_room_access.mode: multi_user`.
2. Set `multi_user_join_rule` to `public` (direct join) or `knock` (request access).
3. Set `publish_to_room_directory: true` if rooms should appear in Explore/public room directory.

If you keep `mode: single_user_private` (default), managed rooms remain invite-only and private in the directory.

### Required Service Account Permissions

MindRoom applies room join rules and directory visibility using its managing account (typically the router account, e.g. `@mindroom_router:<domain>`).

- The managing account must be joined to the room.
- The managing account must have enough power to send `m.room.join_rules`.
- To publish to the room directory, Synapse requires moderator/admin-level power in that room.

If permissions are insufficient, MindRoom logs actionable warnings including the Matrix API error and required permission hint.

## Migration Guide (Existing Deployments)

Use this opt-in migration flow to move existing managed rooms to multi-user onboarding safely:

1. Update config:
   - `matrix_room_access.mode: multi_user`
   - choose `multi_user_join_rule`
   - set `publish_to_room_directory` as needed
   - optionally list restricted rooms in `invite_only_rooms`
2. Enable reconciliation once:
   - `matrix_room_access.reconcile_existing_rooms: true`
3. Restart MindRoom and verify logs for each managed room.
4. After migration is complete, set `reconcile_existing_rooms: false` again (recommended steady state).

Only managed rooms (rooms configured through MindRoom agents/teams) are reconciled.

## Matrix ID Format

User IDs follow the Matrix format: `@localpart:homeserver.domain`

Examples: `@alice:matrix.org`, `@bob:example.com`, `@admin:company.internal`

## Authorization Flow

Authorization checks are performed in order:

1. **Internal system user** - `@{mindroom_user.username}:{domain}` is always authorized (default: `@mindroom_user:{domain}`). Note: that user ID from a different domain is NOT authorized.
2. **MindRoom agents/teams/router** - Configured agents, teams, and the router are authorized
3. **Alias resolution** - If the sender matches a bridge alias in `aliases`, it is resolved to the canonical user ID for the remaining checks
4. **Global users** - Users in `global_users` have access to all rooms
5. **Room permissions** - If any matching room identifier exists in `room_permissions` (room ID, full alias, or managed room key), user must be in that list (does NOT fall through to `default_room_access`)
6. **Default access** - Rooms not in `room_permissions` use `default_room_access`

> [!TIP]
> Set `default_room_access: false` and explicitly grant access via `global_users` or `room_permissions` for better security.

## Bridge Aliases

When using Matrix bridges (e.g., mautrix-telegram, mautrix-signal), messages from the bridged platform arrive with a different Matrix user ID. Use `aliases` to map these bridge-created IDs to a canonical user so they inherit the same permissions:

```yaml
authorization:
  global_users:
    - "@alice:example.com"
  room_permissions:
    "!room1:example.com":
      - "@bob:example.com"
  aliases:
    "@alice:example.com":
      - "@telegram_123:example.com"
      - "@signal_456:example.com"
    "@bob:example.com":
      - "@telegram_789:example.com"
```

In this example, messages from `@telegram_123:example.com` are treated as `@alice:example.com` (global access), and messages from `@telegram_789:example.com` are treated as `@bob:example.com` (access to `!room1:example.com` only).

## Per-Agent Reply Permissions

Use `authorization.agent_reply_permissions` to restrict which users each agent can reply to.

- The map key is an entity name: agent name, team name, `router`, or `*`.
- The `*` key is a default rule for entities that do not have an explicit entry.
- The value is a list of allowed Matrix user IDs.
- Values support glob-style matching (for example `*:example.com`).
- A `*` user entry means "allow any sender" for that specific entity.
- If an entity is not present in the map, it has no extra reply restriction.
- Alias mapping from `authorization.aliases` is applied before matching, so bridged IDs inherit canonical user permissions.
- Internal MindRoom identities (agents, teams, router, and the internal `mindroom_user`) always bypass reply permissions — they are system participants, not end users.
- `bot_accounts` are **not** exempt. Bridge bots listed in `bot_accounts` are still subject to reply permission checks.
- Keys that do not match any configured agent, team, `router`, or `*` are rejected at config load time.
- For voice messages, the permission check uses the original human sender, not the router that posted the transcription.

```yaml
authorization:
  global_users:
    - "@alice:example.com"
    - "@bob:example.com"
  aliases:
    "@alice:example.com":
      - "@telegram_111:example.com"
  agent_reply_permissions:
    "*":
      - "@alice:example.com"
    code:
      - "@alice:example.com"
    research:
      - "@bob:example.com"
    router:
      - "*"
```

In this example, `*` restricts all entities to Alice by default, `research` overrides that and replies only to Bob, and `router` can reply to anyone.

## Bot Accounts

The `bot_accounts` field is a **top-level** config option (not under `authorization:`). It lists Matrix user IDs of non-MindRoom bots — such as bridge bots for Telegram, Slack, or other platforms — that should be treated like agents for response logic. Bots in this list won't trigger the multi-human-thread mention requirement.

```yaml
# Top-level config, not under authorization:
bot_accounts:
  - "@telegram_bot:example.com"
  - "@slack_bot:example.com"
```

For more details on how `bot_accounts` affects routing behavior, see the [Router configuration](configuration/router.md) page.
