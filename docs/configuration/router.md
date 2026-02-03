---
icon: lucide/route
---

# Router Configuration

The router is a built-in system component that handles intelligent message routing and room management. It decides which agent should respond when no specific agent is mentioned, sends welcome messages to new rooms, and manages various system-level tasks.

## Configuration

```yaml
router:
  # Model for routing decisions (defaults to "default")
  model: haiku
```

The router only has one configuration option:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `model` | string | `"default"` | Model to use for routing decisions |

## How Routing Works

When a message arrives in a room without a specific agent mention:

1. The router checks if there are configured agents in that room
2. It analyzes the message content and any recent thread context (up to 3 previous messages)
3. Based on the available agents' roles, tools, and instructions, it selects the best match
4. The selected agent responds to the message in a thread

The router uses a structured output schema to ensure consistent routing decisions, including the agent name and reasoning for the selection.

## Router Responsibilities

The router is a special system agent that handles several important tasks beyond message routing:

### Welcome Messages

When the router joins an empty room (or is invited to one), it automatically sends a welcome message listing:

- All available agents in that room with their descriptions
- How to interact with agents (mentions, commands)
- Quick command reference (`!help`, `!hi`, `!schedule`, `!widget`)

Use `!hi` in any room to see the welcome message again.

### Room Management

The router creates and manages rooms:

- Creates configured rooms that don't exist yet
- Invites agents and users to their configured rooms
- Has admin privileges to manage room membership

### Voice Message Processing

Voice message callbacks are registered only on the router to avoid duplicate processing. When a voice message is received, the router transcribes it and posts the text, which can then be routed to the appropriate agent.

### Scheduled Task Restoration

When the router joins a room, it restores any previously scheduled tasks to ensure reminders and scheduled messages persist across restarts.

## Routing Fallback Behavior

If routing fails (model error, invalid suggestion, etc.), the router logs the error and does not route the message. Users can always mention agents directly with `@agent_name` to bypass routing.

## Note on the Router Agent

The router is always present in MindRoom - it cannot be disabled. It automatically joins any room that has configured agents. Even if you don't explicitly configure a `router` section, it uses the default model for routing decisions.
