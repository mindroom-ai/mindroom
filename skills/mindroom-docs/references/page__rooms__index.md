# Rooms & Spaces

This page covers the Matrix rooms MindRoom creates and manages, their join, listing, encryption, invitation, and admin settings, and the root Matrix Space that groups them.
Who may talk to an agent in a room is a separate setting covered in [Access Control](https://docs.mindroom.chat/authorization/).

## Configuration

List room keys under an agent's or team's `rooms`, and optionally set per-room policy under `rooms.<key>`:

```yaml
room_defaults:
  join_policy: invite
  listed: false
  invite_users:
    - "@owner:example.com"
  admins:
    - "@owner:example.com"

rooms:
  research:
    display_name: Research Lab
    description: Literature reviews and experiment notes
    encrypted: true
    invite_users:
      - "@owner:example.com"
      - "@scientist:example.com"

agents:
  assistant:
    rooms: [lobby, dev]

teams:
  research_team:
    rooms: [research]
```

Every room key listed by an agent or team, or under top-level `rooms`, is a managed room, created on startup or config reload if it does not exist yet.
Entries starting with `!` are existing Matrix room IDs, which are joined as they are and are not created or managed.

## Room policy

`room_defaults` sets the policy for every managed room, and `rooms.<key>` overrides it for one room.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `join_policy` | `invite`, `knock`, or `public` | `invite` | How people may join the room |
| `listed` | bool | `false` | Publish the room in the server's room directory |
| `encrypted` | bool | `false` | Enable end-to-end encryption; once enabled it cannot be turned off |
| `invite_users` | list of Matrix user IDs | `[]` | Users invited to the room |
| `admins` | list of Matrix user IDs | `[]` | Users granted Matrix admin power (level 100) in the room |
| `rooms.<key>.display_name` | string | the key with underscores as spaces, title-cased | Matrix room name |
| `rooms.<key>.description` | string | `""` | Room purpose shown in the dashboard |

`invite_users` and `admins` accept only concrete Matrix user IDs, no wildcards, plus the pairing owner placeholder.
A field set under `rooms.<key>` replaces the default, and a list replaces the whole default list instead of merging with it, so an empty list removes the inherited invitations or admins for that room.
MindRoom logs `Administrators, room invitees, or room admins are on another homeserver; remove them unless you trust them` when any of these users is on a different homeserver.

MindRoom applies this policy to new and existing managed rooms on startup and on every config reload:

- `invite_users` is the desired invitation list, so a listed user who leaves or is kicked is invited again; remove the user from configuration before intentionally removing access.
- `admins` grants missing admin power but never demotes anyone, so removing a user from `admins` only stops future grants; lowering an existing admin requires a Matrix account with higher power.
- `listed: true` requires the router to have moderator or admin power in the room.
- `encrypted: true` turns on encryption in existing rooms, and setting it back to `false` never disables it.

## Room Management

The router creates and maintains managed rooms:

- Creates missing rooms with the alias `#<key>:<server>` (plus the `MINDROOM_NAMESPACE` suffix when set), an AI-generated topic, and a [managed avatar](https://docs.mindroom.chat/matrix/#managed-avatars).
- Invites the configured agents and teams and the room's `invite_users`.
- Sets the room name from `display_name`, and adds a topic only when the room has none.
- Lets every room member write thread tags and call membership state, which requires the router to be joined and able to edit power levels.
- Applies the [room policy](#room-policy) above.

Agents and teams leave rooms they are no longer configured for, except direct-message rooms and rooms they joined by accepting an invitation.

On a shared homeserver, any account can create or repoint an alias such as `#lobby:<server>`, so MindRoom only uses an aliased room it recorded itself or one the router created and aliased.
Otherwise it logs `managed_alias_target_refused`, never joins that room, and creates its own room without the alias.
A recorded room stays in use even when its alias now points elsewhere, which logs `managed_alias_points_elsewhere`.
If the alias of a recorded room is deleted, the router restores it, and creates a replacement room only when it can no longer read or is no longer joined to the recorded room.

## Room Cleanup

On startup, MindRoom removes Matrix accounts of agents and teams that were deleted from `config.yaml` from the rooms they had joined.
Direct-message rooms are left untouched, and no manual cleanup is needed.

## Matrix Space

MindRoom creates and maintains a root Matrix Space that contains every managed room, so users can find all MindRoom rooms in one place in their Matrix client.



```yaml
matrix_space:
  enabled: true
  name: MindRoom
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Create and maintain the root Space |
| `name` | string | `"MindRoom"` | Space display name; cannot be empty |

Rooms added later are linked into the Space automatically, and the name follows the configured value on every startup.
Every user in any managed room's `invite_users` is invited to the Space, without Space admin power.
Room `admins`, `administrators`, and other access settings do not cause Space invitations.
MindRoom never removes existing Space admins, and adding, removing, or reordering Space children in a Matrix client requires sufficient power in the Space; manage those permissions in a Matrix client.
Setting `enabled: false` stops maintaining the Space but does not delete it or remove its child links.
The Space avatar is described in [Managed Avatars](https://docs.mindroom.chat/matrix/#managed-avatars).
