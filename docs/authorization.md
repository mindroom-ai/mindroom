---
icon: lucide/shield
---

# Authorization

MindRoom supports fine-grained access control to determine who can interact with agents.

## Overview

Authorization controls which Matrix users can:

- Send messages that agents respond to
- Execute commands
- Access specific rooms

## Configuration

Configure authorization in `config.yaml`:

```yaml
authorization:
  # Users with access to all rooms
  global_users:
    - "@admin:example.com"
    - "@developer:example.com"

  # Room-specific permissions (must use Matrix room IDs)
  room_permissions:
    "!abc123:example.com":
      - "@user1:example.com"
      - "@user2:example.com"
    "!xyz789:example.com":
      - "@support-team:example.com"

  # Default for rooms not explicitly configured
  default_room_access: false
```

## Global Users

Users listed in `global_users` can interact with agents in any room:

```yaml
authorization:
  global_users:
    - "@admin:example.com"
```

Use this for:

- Administrators
- Developers
- Trusted team members

## Room Permissions

Grant access to specific rooms using their Matrix room ID:

```yaml
authorization:
  room_permissions:
    "!abc123:example.com":
      - "@contractor:example.com"
    "!xyz789:example.com":
      - "@support-agent:example.com"
```

Note: Room permissions must use the full Matrix room ID (starting with `!`), not room aliases or names.

## Default Access

The `default_room_access` setting controls behavior for rooms without explicit configuration:

```yaml
authorization:
  default_room_access: false  # Deny by default (secure)
  # default_room_access: true  # Allow by default (open)
```

**Recommended:** Set to `false` and explicitly grant access.

**Note:** If no `authorization` block is configured at all, the defaults are:
- `global_users: []` (empty)
- `room_permissions: {}` (empty)
- `default_room_access: false`

This means only MindRoom system users (agents, teams, router, and `@mindroom_user`) can interact with agents by default.

## Matrix ID Format

User IDs follow the Matrix format:

```
@localpart:homeserver.domain
```

Examples:

- `@alice:matrix.org`
- `@bob:example.com`
- `@admin:company.internal`

## Authorization Flow

```
┌─────────────┐
│ Message     │
│ Received    │
└─────┬───────┘
      │
      ▼
┌─────────────┐     ┌─────────────┐
│ Is internal │────▶│ Authorized  │────▶ Process
│ system user │ Yes └─────────────┘
└─────┬───────┘
      │ No
      ▼
┌─────────────┐     ┌─────────────┐
│ Is MindRoom │────▶│ Authorized  │────▶ Process
│ agent/team/ │ Yes └─────────────┘
│ router      │
└─────┬───────┘
      │ No
      ▼
┌─────────────┐     ┌─────────────┐
│ Check       │────▶│ Authorized  │────▶ Process
│ Global      │ Yes └─────────────┘
└─────┬───────┘
      │ No
      ▼
┌───────────────────┐
│ Room in           │
│ room_permissions? │
└─────────┬─────────┘
          │
     ┌────┴────┐
    Yes       No
     │         │
     ▼         ▼
┌─────────────┐     ┌─────────────┐
│ User in     │     │ default_    │
│ room list?  │     │ room_access │
└─────┬───────┘     └─────┬───────┘
   ┌──┴──┐             ┌──┴──┐
  Yes   No           true  false
   │     │             │     │
   ▼     ▼             ▼     ▼
 ┌───────────┐     ┌───────────┐
 │ Authorize │     │  Ignore   │
 │ (Process) │     │  Message  │
 └───────────┘     └───────────┘
```

The authorization checks are performed in order:

1. **Internal system user** - The `@mindroom_user:{domain}` account (e.g., `@mindroom_user:example.com`) is always authorized. Note: `@mindroom_user` from a different domain is NOT automatically authorized.
2. **MindRoom agents/teams/router** - Configured agents, teams, and the router are authorized to communicate
3. **Global users** - Users in `global_users` have access to all rooms
4. **Room permissions** - Users listed for a specific room ID. If a room is in `room_permissions` but the user is not listed, access is denied. It does NOT fall through to `default_room_access`.
5. **Default access** - For rooms not in `room_permissions` at all, falls back to `default_room_access` setting

## SaaS Platform Authorization

When running with the SaaS platform:

1. **Instance-level** - Users must have instance access via Supabase
2. **Room-level** - Additional filtering via `authorization` config
3. **JWT validation** - Tokens verified on each request

## Best Practices

1. **Principle of least privilege** - Only grant necessary access
2. **Use global sparingly** - Reserve for true administrators
3. **Audit periodically** - Review who has access
4. **Default deny** - Set `default_room_access: false`
5. **Document access** - Keep records of who should have access
