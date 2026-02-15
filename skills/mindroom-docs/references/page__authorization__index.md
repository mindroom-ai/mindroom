# Authorization

MindRoom controls which Matrix users can interact with agents.

## Configuration

Configure authorization in `config.yaml`:

```
authorization:
  # Users with access to all rooms
  global_users:
    - "@admin:example.com"
    - "@developer:example.com"

  # Room-specific permissions (must use Matrix room IDs, not aliases)
  room_permissions:
    "!abc123:example.com":
      - "@user1:example.com"
      - "@user2:example.com"

  # Default for rooms not in room_permissions
  default_room_access: false

# Optional: configure the internal MindRoom user identity
mindroom_user:
  username: mindroom_user          # Set before first startup (cannot be changed later)
  display_name: MindRoomUser
```

**Defaults** (when `authorization` block is omitted):

- `global_users: []`
- `room_permissions: {}`
- `default_room_access: false`

This means only MindRoom system users (agents, teams, router, and the configured internal user, default `@mindroom_user`) can interact with agents by default.

`mindroom_user.username` is a one-time setting used to create the internal Matrix account. After the account exists, keep the same username and only change `mindroom_user.display_name` for visible name changes.

## Matrix ID Format

User IDs follow the Matrix format: `@localpart:homeserver.domain`

Examples: `@alice:matrix.org`, `@bob:example.com`, `@admin:company.internal`

## Authorization Flow

Authorization checks are performed in order:

1. **Internal system user** - `@{mindroom_user.username}:{domain}` is always authorized (default: `@mindroom_user:{domain}`). Note: that user ID from a different domain is NOT authorized.
1. **MindRoom agents/teams/router** - Configured agents, teams, and the router are authorized
1. **Global users** - Users in `global_users` have access to all rooms
1. **Room permissions** - If room is in `room_permissions`, user must be in that room's list (does NOT fall through to `default_room_access`)
1. **Default access** - Rooms not in `room_permissions` use `default_room_access`

> [!TIP] Set `default_room_access: false` and explicitly grant access via `global_users` or `room_permissions` for better security.
