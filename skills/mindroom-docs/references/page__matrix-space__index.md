# Matrix Space

MindRoom can create and maintain a root Matrix Space that groups all managed rooms together.
This makes it easy for users to discover and navigate MindRoom rooms in their Matrix client.

## Configuration

```yaml
matrix_space:
  enabled: true    # Default: true
  name: MindRoom   # Default: "MindRoom"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether to create and maintain a root Matrix Space for managed MindRoom rooms |
| `name` | string | `"MindRoom"` | Display name for the root Matrix Space when enabled |

## Behavior

When `enabled` is `true`, MindRoom creates a Space on startup and adds all managed rooms as children.
Rooms created later (by agents joining new rooms or config changes) are automatically added to the Space.
MindRoom invites concrete users from effective managed-room `invite_users` policies to the root Space without granting those users root Space admin power.
Startup and config updates maintain the child links without automatically granting human users Space admin power.
Adding, removing, or reordering Space children in a Matrix client requires sufficient existing Matrix power in that Space.
Existing Space admins are preserved; manage their permissions manually in a Matrix client.

Set `enabled: false` to disable Space creation entirely.
Disabling the setting does not delete an existing Space or remove its current child links.
The `name` field controls the Space's display name and can be changed at any time.
