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

  # Room-specific permissions
  room_permissions:
    "!roomid:example.com":
      - "@user1:example.com"
      - "@user2:example.com"
    "support":
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

Grant access to specific rooms:

```yaml
authorization:
  room_permissions:
    # By room ID
    "!abc123:example.com":
      - "@contractor:example.com"

    # By room name (as configured in rooms section)
    "support":
      - "@support-agent:example.com"
```

## Default Access

The `default_room_access` setting controls behavior for rooms without explicit configuration:

```yaml
authorization:
  default_room_access: false  # Deny by default (secure)
  # default_room_access: true  # Allow by default (open)
```

**Recommended:** Set to `false` and explicitly grant access.

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
│ Check       │────▶│ Authorized  │────▶ Process
│ Global      │ Yes └─────────────┘
└─────┬───────┘
      │ No
      ▼
┌─────────────┐     ┌─────────────┐
│ Check Room  │────▶│ Authorized  │────▶ Process
│ Permissions │ Yes └─────────────┘
└─────┬───────┘
      │ No
      ▼
┌─────────────┐     ┌─────────────┐
│ Check       │────▶│ Authorized  │────▶ Process
│ Default     │ Yes └─────────────┘
└─────┬───────┘
      │ No
      ▼
┌─────────────┐
│ Ignore      │
│ Message     │
└─────────────┘
```

## SaaS Platform Authorization

When running with the SaaS platform:

1. **Instance-level** - Users must have instance access via Supabase
2. **Room-level** - Additional filtering via `authorization` config
3. **JWT validation** - Tokens verified on each request

## Dashboard Configuration

Use the Rooms tab in the dashboard to configure per-room authorization visually.

## Best Practices

1. **Principle of least privilege** - Only grant necessary access
2. **Use global sparingly** - Reserve for true administrators
3. **Audit periodically** - Review who has access
4. **Default deny** - Set `default_room_access: false`
5. **Document access** - Keep records of who should have access
