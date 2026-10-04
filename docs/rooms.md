---
icon: lucide/layout-grid
---

# Rooms & Spaces

See also [Access Control](authorization.md).

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

When a room doesn't exist, it's created with an AI-generated topic, power users are invited, and it receives a managed avatar (see [Managed Avatars](matrix.md#managed-avatars)).

## Room policy

`room_defaults` supplies the default `join_policy`, `listed`, `encrypted`, `invite_users`, and `admins` values for every managed room.

`rooms.<key>` also accepts `display_name` for the Matrix room name and `description` (string, default `""`) for the room's purpose shown in the dashboard.
An authored field under `rooms.<key>` replaces the corresponding default.
List overrides replace the whole default list instead of merging with it.
An explicit empty list therefore disables the inherited invitations or admins for that room.
MindRoom grants missing room admins but does not demote existing power-level 100 admins when they are removed from configuration, because the managing Matrix account cannot demote an equal-power user.
Removing an admin from configuration therefore stops future grants; lowering an existing grant requires a Matrix authority with greater power.

`join_policy` accepts `invite`, `knock`, or `public`.
Publishing managed rooms to the room directory with `listed: true` requires the managing service account (typically the router) to have moderator or admin power in each room.
MindRoom reconciles join policy, directory visibility, invitations, and power levels for existing managed rooms.
Encryption can be enabled but never disabled because enabling Matrix room encryption is irreversible.

`invite_users` is declarative desired invitation state.
A listed user who leaves or is kicked is invited again during reconciliation, so remove the user from configuration before intentionally removing access.

The root Matrix Space receives the union of managed-room `invite_users` as invitations.
Invitees do not automatically receive root Space admin power.

## Room Management

Agents can join existing rooms, create new rooms with AI-generated topics, respond to invites automatically, leave unconfigured rooms, and set room avatars.

Rooms are auto-created via `_ensure_room_exists()` (private) and `ensure_all_rooms_exist()` (public). DM rooms can be detected with `async is_dm_room(client, room_id) -> bool`.

Any account on a shared homeserver can publish, repoint, or delete a managed alias such as `#lobby:<server>`, so a resolved alias alone never makes a room managed.
A room already recorded in `matrix_state.yaml` for its key stays in use, and an alias that now resolves elsewhere only logs `managed_alias_points_elsewhere`.
When the alias of a recorded room is deleted, the router points it back at that room, and replaces the room with a new aliased one only when the router can no longer read the room or is no longer joined to it.
An unrecorded key adopts its alias target only when the router created that room, with no additional room version 12 creators, and the router itself set the alias as the room's canonical alias.
Otherwise the router logs `managed_alias_target_refused`, never joins that room, and creates and records a fresh room without the alias, which later passes keep using.
If the alias lookup or a state read fails transiently, a recorded room stays in use and an unrecorded key is retried on the next pass.
A fresh room without the alias that is lost before its record is saved, for example by a crash, stays unrecorded, and the next pass creates another.

A restart without current invite-cache evidence may require another invitation.

### Room Management

The router creates and manages rooms:

- Creates configured rooms that don't exist yet
- Invites configured agents, teams, and users to their rooms
- Applies effective `room_defaults` and per-room policy for managed rooms
- Reconciles managed room power levels so the custom thread-tags state event can be written at PL0
- Generates AI-powered room topics based on configured agents and teams
- Has admin privileges to manage room membership
- Cleans up orphaned bots on startup

See [When the Router Is Missing](tool-approval.md#when-the-router-is-missing).

By default, `room_defaults.join_policy: invite` and `room_defaults.listed: false` keep managed rooms private.
Set `room_defaults.join_policy` to `public` or `knock`, and use `room_defaults.listed` to control room-directory visibility.
Per-room values replace these defaults when configured under `rooms.<key>`.
That same reconciliation path also updates `m.room.power_levels` for managed rooms, so the router must be joined and able to edit room power levels when thread tags are enabled.

## Room Cleanup

On startup, MindRoom detects orphaned bot memberships left over from a previous configuration.
When an agent is removed from `config.yaml`, its Matrix bot account may still be a member of rooms it previously joined.
The global sweep removes only persisted bot identities that no longer belong to a configured router, agent, or team.
Current entities reconcile their own configured and retained rooms after startup hooks and invitation handling, so the early global sweep cannot remove them before that reconciliation.
An entity that cannot start keeps its memberships until its own lifecycle recovers.
Unreadable or invalid retention files stop membership initialization instead of being treated as empty ownership records.
This runs automatically — no manual intervention is needed.

## Matrix Space

MindRoom can create and maintain a root Matrix Space that groups all managed rooms together.
This makes it easy for users to discover and navigate MindRoom rooms in their Matrix client.

### Configuration

```yaml
matrix_space:
  enabled: true    # Default: true
  name: MindRoom   # Default: "MindRoom"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether to create and maintain a root Matrix Space for managed MindRoom rooms |
| `name` | string | `"MindRoom"` | Display name for the root Matrix Space when enabled |

### Behavior

When `enabled` is `true`, MindRoom creates a Space on startup and adds all managed rooms as children.
Rooms created later (by agents joining new rooms or config changes) are automatically added to the Space.
MindRoom invites concrete users from effective managed-room `invite_users` policies to the root Space without granting those users root Space admin power.
Startup and config updates maintain the child links without automatically granting human users Space admin power.
Adding, removing, or reordering Space children in a Matrix client requires sufficient existing Matrix power in that Space.
Existing Space admins are preserved; manage their permissions manually in a Matrix client.

Set `enabled: false` to disable Space creation entirely.
Disabling the setting does not delete an existing Space or remove its current child links.
The `name` field controls the Space's display name and can be changed at any time.

## Root Space

MindRoom can create and maintain a root Matrix Space that groups all managed rooms.

```yaml
matrix_space:
  enabled: true        # Default: true
  name: MindRoom       # Display name for the Space
```

When enabled, `ensure_root_space()` creates the Space on first boot (or resolves an existing one by alias), links all managed rooms as children, and sets the Space avatar from workspace or bundled assets, falling back to the stock `mind-logo` image.
The Space alias follows the same adoption rules as managed room aliases, so a Space held by another account is replaced by a fresh Space without the alias.
The Space name is reconciled on each startup to match the configured value.
Startup and config updates write child links without automatically granting human users root Space admin power.
Concrete users from effective managed-room `invite_users` policies are invited to the root Space without receiving Space admin power.
Platform `administrators`, room `admins`, responder users, and credential managers are not root Space invitation sources.
MindRoom does not remove existing Space admins during reconciliation.
