---
icon: lucide/layout-grid
---

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

Set `enabled: false` to disable Space creation entirely.
The `name` field controls the Space's display name and can be changed at any time.
