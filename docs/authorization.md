---
icon: lucide/shield
---

# Authorization

MindRoom keeps room invitations, conversation access, Matrix power, platform administration, and credential management independent.

## Authority at a glance

MindRoom answers five authority questions independently.

| Question | Owner |
| --- | --- |
| Who may make a router, agent, or team join a room? | That entity's `accept_invites` policy |
| Who may interact with a responder? | That responder's `access` policy |
| Whose state and credentials does an interaction use? | The canonical requester plus the agent's requester-private and worker scope |
| Who may administer platform or credential configuration? | `administrators` and `agents.<name>.credential_managers` |
| Who may execute one sensitive tool action? | Tool availability plus any applicable tool approval policy |

No answer grants another authority.
Joining a room does not grant conversation access, and conversation access does not grant credential management.
Requester-private state placement does not grant access to the agent.
Tool approval is an additional action gate and does not replace conversation access.

## Configuration

```yaml
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
    accept_invites:
      - "@owner:example.com"
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
MindRoom grants missing room admins but does not demote existing power-level 100 admins when they are removed from configuration, because the managing Matrix account cannot demote an equal-power user.
Removing an admin from configuration therefore stops future grants; lowering an existing grant requires a Matrix authority with greater power.

`join_policy` accepts `invite`, `knock`, or `public`.
MindRoom reconciles join policy, directory visibility, invitations, and power levels for existing managed rooms.
Encryption can be enabled but never disabled because enabling Matrix room encryption is irreversible.

`invite_users` is declarative desired invitation state.
A listed user who leaves or is kicked is invited again during reconciliation, so remove the user from configuration before intentionally removing access.

The root Matrix Space receives the union of managed-room `invite_users` as invitations.
Invitees do not automatically receive root Space admin power.

## Responder access

Responder access supports three independent clauses.

- `current_room_members: true` allows authoritative joined members of the current room.
- `members_of_rooms` allows authoritative joined members of any listed managed room.
- `users` allows canonical Matrix user IDs or glob patterns.

A requester is allowed when any configured clause matches.

Agent and team access defaults `members_of_rooms` to that responder's configured managed `rooms` when the `access` block is omitted or its `members_of_rooms` field is omitted.
Only managed room keys are inferred this way: raw Matrix room IDs and full aliases listed in `rooms` produce no membership grant, and explicit `members_of_rooms` entries must also name configured managed room keys.
The configured rooms used by `members_of_rooms` are managed grant rooms; they are not a separate identity or invitation concept.
The router defaults `current_room_members` to `true`.
An explicit `members_of_rooms: []` disables inferred room grants.

MindRoom resolves aliases before administrator and static-user matching.
Internal MindRoom identities bypass responder restrictions because they are system participants.
The authoritative membership index fails closed while a referenced room is missing, stale, unresolved, or unavailable.
Invitations do not count as joined membership, and leave, kick, or ban events revoke membership grants.
The router owns this authoritative index, so it must be joined to a room before `current_room_members` can authorize activity there.
For an ad-hoc room where an agent arrived first, use the agent's `invite_router` recovery tool and retry after the router joins.

Inbound invitation policy is independent from responder access.
Accepting an invitation grants room membership but never grants permission to interact with the router, an agent, or a team.
The router, agents, and teams use their own `accept_invites` setting to accept all inviters, reject all inviters, or allow explicit Matrix user ID patterns.
Every interaction after joining still uses the responder rules above.

The same responder gate covers text, media, calls, reactions, approval actors, external triggers, background scripts, delegation, attachment access, visible voice echoes, room lifecycle responses, and scheduled resumes.

## Requester identity and private state

MindRoom resolves a trusted inbound requester through `authorization.aliases` before selecting requester-owned state.
The raw authenticated Matrix sender remains transport provenance and is not used as a second downstream ownership decision.
One canonical requester therefore owns the same requester-scoped conversations, state, credentials, approvals, triggers, scripts, attachments, and usage when arriving through a configured bridge alias.

An agent's `private` field controls requester-private state placement.
It does not authorize anyone to interact with the agent.
MindRoom checks the agent's ordinary `access` policy before selecting a requester-private instance.

## Platform and credential authority

`administrators` contains concrete Matrix user IDs and does not accept wildcards.
Platform administrators may use administrative commands when their independent feature flag is enabled, manage any agent's credentials, and bypass responder policies.

`agents.<name>.credential_managers` also contains concrete Matrix user IDs and does not accept wildcards.
A credential manager may manage only the named shared agent's credentials and OAuth connections.
Authenticated requesters may manage OAuth connections for their own requester-private agent scope without a static credential-manager entry.
Deployment-global OAuth client configuration remains restricted to platform administrators.

Shared-agent dashboard and OAuth requests return HTTP 403 before credentials are exposed or changed when the requester is neither an administrator nor a configured credential manager.
Standalone deployments should set `MINDROOM_OWNER_USER_ID` so API-key dashboard requests resolve to the owner Matrix identity.

`!config` remains disabled by default through `authorization.config_command_enabled`.
When enabled, `!config`, confirmation reactions, and `!reload-plugins` require a platform administrator.

## Tool approval and resource ownership

Conversation access allows a requester to ask a responder to act, but the responder must still have the tool and any configured approval must still succeed.
Tool approval is bound to the canonical requester who initiated the action and rechecks current responder access.

Schedules are room-managed resources, while external triggers, background scripts, attachments, requester-private workers, and requester-scoped credentials are requester-owned.
These ownership rules do not create additional responder access.

## Bridge aliases

`authorization.aliases` maps bridge-created identities before access checks.
It maps bridge-created Matrix IDs to a canonical Matrix user before access, administration, or credential checks.

```yaml
authorization:
  aliases:
    "@alice:example.com":
      - "@telegram_123:example.com"
      - "@signal_456:example.com"
```

## Automatic migration

Loading a monolithic configuration with retired access fields automatically converts it to this schema.
MindRoom validates the converted configuration before replacing `config.yaml` atomically and saves the exact original bytes once as `config.yaml.pre-membership-access`.
When `config.yaml` is a single-file Docker bind mount that cannot be replaced atomically, migration stops and directs the operator to run `mindroom config migrate --path <host-config.yaml>` on the host.
The migration preserves explicit new-schema values and removes the retired fields.
The normalized YAML does not preserve comments or hand formatting; the exact backup preserves both for recovery.
Before retrying a rejected migration, replace non-concrete identity grants with concrete Matrix user IDs and unresolved room IDs or aliases with managed room keys.
Also remove `authorization.agent_reply_permissions` entries whose agent or team is no longer configured.

Access migration does not support configurations that use `!include`.
If retired access fields and any `!include` are present together, loading fails without changing the root file, changing included files, or creating a backup.
Remove the includes or migrate the combined configuration manually before retrying.

## Bot accounts

The top-level `bot_accounts` field lists non-MindRoom bot identities that should be treated like agents for response logic.
These accounts are not exempt from responder access checks.

```yaml
bot_accounts:
  - "@telegram_bot:example.com"
  - "@slack_bot:example.com"
```
