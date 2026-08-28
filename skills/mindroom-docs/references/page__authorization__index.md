# Authorization

MindRoom supports a membership-based access model that keeps room invitations, conversation access, Matrix power, platform administration, and credential management independent.

Set `access_model: room_membership` to opt in.

## Membership-based configuration

```yaml
access_model: room_membership

administrators:
  - "@owner:example.com"

room_defaults:
  join_policy: invite
  listed: false
  encrypted: true
  invite_users:
    - "@member:example.com"
  admins: []

rooms:
  engineering:
    display_name: Engineering
    invite_users:
      - "@engineer:example.com"
    admins:
      - "@room-admin:example.com"

agents:
  code:
    display_name: Code
    rooms: [engineering]
    access:
      current_room_members: false
      members_of_rooms: [engineering]
      users:
        - "@contractor:example.com"
    credential_managers:
      - "@credential-owner:example.com"

router:
  access:
    current_room_members: true
    members_of_rooms: []
    users: []

authorization:
  config_command_enabled: false
  aliases:
    "@owner:example.com":
      - "@telegram_owner:example.com"
```

Each field has one responsibility.

| Field | Responsibility |
| --- | --- |
| `administrators` | Platform configuration and credential authority plus a responder-policy bypass |
| `room_defaults` | Default desired Matrix state for managed rooms |
| `rooms.<key>.invite_users` | Automatic invitations for one managed room |
| `rooms.<key>.admins` | Matrix power level 100 for one managed room |
| `agents.<name>.rooms` and `teams.<name>.rooms` | Rooms where a responder operates |
| `<responder>.access` | Users who may converse with that responder |
| `agents.<name>.credential_managers` | Users who may manage that agent's credentials and OAuth connections |

No field in this table implies another row.

Administrators are not automatically invited and do not receive Matrix room power.
Room administrators are not platform administrators or credential managers.
Responder users and room membership do not grant credential authority.
Credential managers do not gain responder access.

## Room policy

`room_defaults` supplies the default `join_policy`, `listed`, `encrypted`, `invite_users`, and `admins` values for every managed room.

An authored field under `rooms.<key>` replaces the corresponding default.
List overrides replace the whole default list instead of merging with it.
An explicit empty list therefore disables the inherited invitations or admins for that room.

`join_policy` accepts `invite`, `knock`, or `public`.
MindRoom reconciles membership-mode join policy, directory visibility, invitations, and power levels for existing managed rooms without consulting the legacy `reconcile_existing_rooms` flag.
Encryption can be enabled but never disabled because enabling Matrix room encryption is irreversible.

`invite_users` is declarative desired invitation state.
A listed user who leaves or is kicked is invited again during reconciliation, so remove the user from configuration before intentionally removing access.

The root Matrix Space receives the union of managed-room `invite_users` as invitations.
Membership-mode invitees do not automatically receive root Space admin power.

## Responder access

Responder access supports three independent clauses.

- `current_room_members: true` allows authoritative joined members of the current room.
- `members_of_rooms` allows authoritative joined members of any listed managed room.
- `users` allows canonical Matrix user IDs or glob patterns.

Agent and team access defaults `members_of_rooms` to that responder's configured `rooms` when the `access` block is omitted or its `members_of_rooms` field is omitted.
The router defaults `current_room_members` to `true`.
An explicit `members_of_rooms: []` disables inferred room grants.

MindRoom resolves aliases before administrator and static-user matching.
Internal MindRoom identities bypass responder restrictions because they are system participants.
The authoritative membership index fails closed while a referenced room is missing, stale, unresolved, or unavailable.
Invitations do not count as joined membership, and leave, kick, or ban events revoke membership grants.

The same responder gate covers text, media, calls, reactions, approval actors, external triggers, background scripts, delegation, attachment access, visible voice echoes, room lifecycle responses, and scheduled resumes.

## Platform and credential authority

`administrators` contains concrete Matrix user IDs and does not accept wildcards.
Platform administrators may use administrative commands when their independent feature flag is enabled, manage any agent's credentials, and bypass responder policies.

`agents.<name>.credential_managers` also contains concrete Matrix user IDs and does not accept wildcards.
A credential manager may manage only the named agent's credentials and OAuth connections.

Agent-scoped dashboard and OAuth requests return HTTP 403 before credentials are exposed or changed when the requester is neither an administrator nor a configured credential manager.
Standalone deployments should set `MINDROOM_OWNER_USER_ID` so API-key dashboard requests resolve to the owner Matrix identity.

`!config` remains disabled by default through `authorization.config_command_enabled`.
When enabled, `!config`, confirmation reactions, and `!reload-plugins` require a platform administrator.

## Bridge aliases

`authorization.aliases` remains available in both access models.
It maps bridge-created Matrix IDs to a canonical Matrix user before access, administration, or credential checks.

```yaml
authorization:
  aliases:
    "@alice:example.com":
      - "@telegram_123:example.com"
      - "@signal_456:example.com"
```

## Legacy compatibility

Omitting `access_model` preserves the existing authorization behavior unchanged.

Legacy mode continues to use `authorization.global_users`, `authorization.room_permissions`, `authorization.default_room_access`, `authorization.agent_reply_permissions`, and `matrix_room_access`.
Legacy static `agent_reply_permissions.<entity>.users` entries continue to authorize credential management for that agent.
Legacy `joined_rooms` grants conversation access only.

Membership mode rejects non-default values in overlapping legacy fields because their old meanings combined capabilities that now require separate operator decisions.

| Legacy field | Manual membership-mode decision |
| --- | --- |
| `authorization.global_users` | Decide separately among `administrators`, room `invite_users`, responder `access.users`, and `credential_managers` |
| `authorization.room_permissions.<room>` | Decide separately between `rooms.<room>.invite_users` and responder access |
| `authorization.default_room_access` | Choose explicit responder access, usually `current_room_members` |
| `authorization.agent_reply_permissions.<entity>` | Move conversation grants to responder `access` and separately choose credential managers |
| `matrix_room_access.mode` and `multi_user_join_rule` | Move the desired join policy to `room_defaults` or a room override |
| `matrix_room_access.publish_to_room_directory` | Move visibility to `room_defaults.listed` or `rooms.<key>.listed` |
| `matrix_room_access.invite_only_rooms` | Use per-room `join_policy`, `listed`, and `invite_users` overrides |
| `matrix_room_access.encrypt_managed_rooms` | Move encryption intent to `room_defaults.encrypted` or `rooms.<key>.encrypted` |
| `matrix_room_access.room_admins` | Move Matrix power to `room_defaults.admins` or `rooms.<key>.admins` |

Run `mindroom config explain-access` to print the effective legacy inputs and a conservative membership-mode skeleton.
The command is read-only and never assumes that legacy global users should become platform administrators.

## Bot accounts

The top-level `bot_accounts` field lists non-MindRoom bot identities that should be treated like agents for response logic.
These accounts are not exempt from responder access checks.

```yaml
bot_accounts:
  - "@telegram_bot:example.com"
  - "@slack_bot:example.com"
```
