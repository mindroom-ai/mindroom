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
4. The router posts a message mentioning the selected agent (e.g., "@agent could you help with this?")
5. The mentioned agent sees the mention and responds in the thread

The router uses a structured output schema to ensure consistent routing decisions, including the agent name and reasoning for the selection.

## Router Responsibilities

The router is a special system agent that handles several important tasks beyond message routing:

### Command Handling

The router exclusively handles all commands (`!help`, `!hi`, `!schedule`, `!list_schedules`, `!cancel_schedule`, `!widget`, `!config`, `!skill`). Even in single-agent rooms, commands are always processed by the router.

### Welcome Messages

When the router joins a room with no messages (or only a previous welcome message), it automatically sends a welcome message listing:

- All available agents in that room with their descriptions
- How to interact with agents (mentions, commands)
- Quick command reference

Use `!hi` in any room to see the welcome message again.

### Room Management

The router creates and manages rooms:

- Creates configured rooms that don't exist yet
- Invites agents and users to their configured rooms
- Generates AI-powered room topics based on configured agents
- Has admin privileges to manage room membership
- Cleans up orphaned bots on startup

### Voice Message Processing

Voice message callbacks are registered only on the router to avoid duplicate processing. When a voice message is received, the router transcribes it and posts the text (prefixed with a microphone emoji), which can then be routed to the appropriate agent.

### Configuration Confirmations

The router handles interactive configuration changes. When a config change is requested, the router posts a confirmation message with reactions, and only the router processes the confirmation reactions.

### Scheduled Task Restoration

When the router joins a room, it restores any previously scheduled tasks and pending configuration changes to ensure they persist across restarts.

## Routing Behavior Details

### Single-Agent Optimization

When there's only one agent configured in a room, the router skips AI routing entirely. The single agent handles messages directly, which is faster and more efficient.

### Routing Fallback

If routing fails (model error, invalid suggestion, etc.), the router sends a helpful error message: "I couldn't determine which agent should help with this. Please try mentioning an agent directly with @ or rephrase your request."

Users can always mention agents directly with `@agent_name` to bypass routing.

## Note on the Router Agent

The router is always present in MindRoom - it cannot be disabled. It automatically joins any room that has configured agents. Even if you don't explicitly configure a `router` section, it uses the default model for routing decisions.
