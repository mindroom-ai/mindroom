# Membership-Based Access Schema Implementation Plan

This record explains why MindRoom's access configuration was replaced by a membership-based schema whose fields each have one purpose, and the decisions behind the automatic migration from the retired fields.
The field reference lives in [Authorization](../authorization.md) and [Room policy](../rooms.md#room-policy), and the operator upgrade procedure in [Membership Access Migration](../deployment/upgrades.md#membership-access-migration).

## Why

The retired model combined unrelated decisions:

- `authorization.room_permissions` was both a room-level sender allowlist and an invitation source.
- `authorization.default_room_access` changed the meaning of rooms missing from that map.
- `authorization.agent_reply_permissions` added a second responder-specific gate after room authorization, and its static `users` entries also authorized agent credential management.
- `matrix_room_access` owned global room defaults while `rooms.<key>` owned only some room-specific overrides.

Because both the room gate and the responder gate had to pass, a current room member could satisfy the responder's membership policy and still be rejected by the room allowlist.
Because authorization lists doubled as invitation sources, a change intended for conversation access also changed room membership.

## Decisions

### One purpose per field

Each kind of authority has its own field, listed in [Authority at a glance](../authorization.md#authority-at-a-glance), so a change meant for one decision cannot silently change another.
No field implies another: administrators and credential managers are not invited, invitees are not promoted to room admins, room admins are not platform administrators, and responder access never makes anyone a credential manager.
Room policy and responder access are each resolved in one place, so ingress, room reconciliation, and credential checks share one interpretation and never merge lists or reinterpret omitted values themselves.

### Conversation access

- Responders default to membership grants so the common case needs no authored `access` block: an agent or team admits current members of its configured rooms, and the router admits current members of the room it is answering in.
- The room-level sender allowlist was removed from ingress because Matrix admission already establishes that the sender could send into the current room; the selected responder's policy is the only conversation gate.
- Administrators and internal system identities bypass responder policies, but administrators still need Matrix membership to send an event in the first place.

### Room state

- A room-specific value replaces the default, and a list replaces the whole default list instead of merging, so a room can drop inherited invitees or admins, and an empty list configures none.
- `invite_users` is declarative desired state: reconciliation invites a listed user again after they leave or are kicked, so remove the user from configuration before intentionally removing their access.
- The room policy applies to existing managed rooms without an opt-in, so migration deliberately drops the retired `reconcile_existing_rooms: false` setting instead of honoring it.
- Encryption is irreversible in Matrix, so reconciliation may enable it but never disables it.
- Admin power is grant-only because the managing Matrix account cannot demote an administrator of equal power; removing a user from `admins` stops future grants, and lowering an existing admin needs a Matrix account with greater power.

### Migration

- Retired fields are converted once when the configuration loads instead of being kept behind a runtime compatibility path, so runtime code supports only the membership model.
- Loading validates the migrated data before writing anything, so a rejected migration leaves the original file untouched.
- Values with no safe membership equivalent, such as room references that do not resolve to one managed room key, fail before anything is written instead of being guessed.
- The original root file is saved once as a sibling backup that is never overwritten, because the rewritten YAML loses comments and hand formatting and the backup is the only recovery source for them.
- Only single-file configurations are migrated automatically; when retired fields appear together with any `!include`, loading fails before validation or persistence.
- A single-file Docker bind mount cannot be replaced atomically, so migration there fails and asks the operator to run `mindroom config migrate` against the host config path.
- Explicit new-schema values win over migrated scalar defaults, while list-valued grants are combined without duplicates so an authored capability is not silently discarded.
- Concrete static agent reply users carry over as `credential_managers`, but wildcard and pattern entries do not, because the field requires concrete identities; this intentionally removes implicit or wildcard credential authority, and platform administrators retain it.
- `authorization.aliases` and `authorization.config_command_enabled` stay under `authorization` because they are not access grants.

## Non-goals

- Kicking room members omitted from `invite_users`.
- Redesigning identity aliases, bot-account classification, external-trigger authentication, or tool-approval policy.
