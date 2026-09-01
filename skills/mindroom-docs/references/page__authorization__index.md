# Authorization

MindRoom keeps room invitations, conversation access, Matrix power, platform administration, and credential management independent.

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

Agent and team access defaults `members_of_rooms` to that responder's configured `rooms` when the `access` block is omitted or its `members_of_rooms` field is omitted.
Only managed room keys are inferred this way: raw Matrix room IDs and full aliases listed in `rooms` produce no membership grant, and explicit `members_of_rooms` entries must also name configured managed room keys.
The router defaults `current_room_members` to `true`.
An explicit `members_of_rooms: []` disables inferred room grants.

MindRoom resolves aliases before administrator and static-user matching.
Internal MindRoom identities bypass responder restrictions because they are system participants.
The authoritative membership index fails closed while a referenced room is missing, stale, unresolved, or unavailable.
Invitations do not count as joined membership, and leave, kick, or ban events revoke membership grants.
For ordinary activity, the router owns this authoritative index, so it must be joined to a room before `current_room_members` can authorize activity there.
An authenticated self-invite is a narrow bootstrap exception for the router: when invite acceptance is enabled, the exact inviter in Matrix's current invite cache may satisfy the router's `current_room_members` policy for that invite only.
Agents and teams may use `current_room_members` for invite acceptance only when the router-owned index already authorizes that inviter in the room; otherwise they require an administrator, static user, internal identity, or grant-room authorization.
One live cache entry owns at most one join attempt, and a failed attempt requires fresh invite evidence.
After the router joins, MindRoom reloads the current policy and requires the inviter to remain a joined member when acceptance depended on the bootstrap exception.
The cached invite object must still identify the authenticated sender, but a joined sync normally removes its cache entry, so absence after a successful join does not revoke acceptance while a still-present different invite fails closed.
An authoritative leave or ban revokes both live invite work and accepted-room preservation.
An authoritative final invite fences the ended membership epoch, tears down room-scoped calls, and revokes accepted-room preservation while retaining the new live invite evidence, while unresolved Sliding Sync membership does none of those things.
A confirmed runtime-owned local leave, including entity removal, immediately revokes accepted-room preservation, while transient invite evidence is consumed only by its exact invite attempt or an authoritative departure.
Configured-room reconciliation and runtime-owned self-leaves share one per-room owner, so an older invite attempt cannot leave after configured setup has taken ownership.
Changes to the router's invite or responder-access policy immediately reconsider cached invites, while disabling invite acceptance also stops preserving accepted ad-hoc rooms.
Accepted-room storage preserves an existing ad-hoc membership across restart but never authorizes joining an absent room.
MindRoom does not persist unfinished invite attempts, so interruption or a temporary failure before acceptance may require another invitation even though ordinary post-join failures receive one best-effort leave.
Interrupted acceptance or failed compensation can leave the bot joined until ordinary cleanup or restart; the room is not preserved, decrypt failures stay silent, and every event still passes the normal post-join access policy.
A same-sender cancellation and reinvite that overlaps an in-flight join may be consumed by that attempt and require another invitation.
For an ad-hoc room where an ordinarily authorized agent arrived first, use the agent's `invite_router` recovery tool and retry after the router joins.

The same responder policy covers text, media, calls, reactions, approval actors, external triggers, background scripts, delegation, attachment access, visible voice echoes, room lifecycle responses, and scheduled resumes.
The immediate router invite welcome may confirm the inviter through a fresh authoritative joined-members query while the router-owned index catches up to the new room.

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
