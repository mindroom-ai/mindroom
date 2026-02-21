# Router Configuration

The router is a built-in system component that handles intelligent message routing and room management. It decides which agent should respond when no specific agent is mentioned, sends welcome messages to new rooms, and manages various system-level tasks.

## Configuration

```
router:
  # Model for routing decisions (defaults to "default")
  model: haiku
```

The router only has one configuration option:

| Option  | Type   | Default     | Description                        |
| ------- | ------ | ----------- | ---------------------------------- |
| `model` | string | `"default"` | Model to use for routing decisions |

## How Routing Works

When a message arrives in a room without a specific agent mention:

1. The router checks if there are configured agents in that room
1. It analyzes the message content and any recent thread context (up to 3 previous messages)
1. Based on the available agents' roles, tools, and instructions, it selects the best match
1. The router posts a message mentioning the selected agent (e.g., "@agent could you help with this?")
1. The mentioned agent sees the mention and responds in the thread

The router uses a structured output schema to ensure consistent routing decisions, including the agent name and reasoning for the selection.

## Router Responsibilities

The router is a special system agent that handles several important tasks beyond message routing:

### Command Handling

The router exclusively handles all commands:

- `!help [topic]` - Get help on commands or specific topics
- `!hi` - Show the welcome message again
- `!schedule <task>` - Schedule tasks and reminders
- `!list_schedules` - List scheduled tasks
- `!cancel_schedule <id>` - Cancel a scheduled task
- `!edit_schedule <id> <task>` - Edit an existing scheduled task
- `!widget [url]` - Add configuration widget to the room
- `!config <operation>` - Manage configuration
- `!skill <name> [args]` - Run a skill by name

Even in single-agent rooms, commands are always processed by the router.

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
- Applies `matrix_room_access` policy for managed rooms (when enabled)
- Generates AI-powered room topics based on configured agents
- Has admin privileges to manage room membership
- Cleans up orphaned bots on startup

By default (`matrix_room_access.mode: single_user_private`), rooms remain invite-only and private in the room directory. In `multi_user` mode, the router can set join rules (`public`/`knock`) and optionally publish rooms to the server directory.

### Voice Message Processing

Voice message callbacks are registered only on the router to avoid duplicate processing. When a voice message is received, the router transcribes it and posts the text (prefixed with a microphone emoji), which can then be routed to the appropriate agent.

### Configuration Confirmations

The router handles interactive configuration changes. When a config change is requested, the router posts a confirmation message with reactions, and only the router processes the confirmation reactions.

### Scheduled Task Restoration

When the router joins a room, it restores any previously scheduled tasks and pending configuration changes to ensure they persist across restarts.

## Routing Behavior Details

### Single-Agent Optimization

When there's only one agent configured in a room, the router skips AI routing entirely. The single agent handles messages directly, which is faster and more efficient.

### Multi-Human Thread Protection

When multiple human users have posted in a thread, the router and agents require an explicit `@mention` before responding. This prevents agents from injecting themselves into human-to-human conversations.

The rules are:

1. **Mentioned agents always respond** — an explicit `@agent` overrides all other rules.
1. **Non-thread messages** — agents auto-respond if they're the only agent in the room, regardless of how many humans are present.
1. **Threads with one human** — normal auto-response behavior applies (the agent continues the conversation).
1. **Threads with two or more humans** — agents stay silent unless explicitly mentioned.
1. **Mentioning a non-agent user** — if a message tags only humans (not agents), agents stay silent.

#### Bot accounts

By default, any Matrix user that is not a MindRoom agent counts as a "human" for the rules above. This includes bridge bots (Telegram, Slack, etc.) and other non-MindRoom bots. If a bridge bot relays a message into a thread, it looks like a second human to MindRoom and triggers the mention requirement.

To prevent this, list those accounts in `bot_accounts`:

```
bot_accounts:
  - "@telegram:example.com"
  - "@slackbot:example.com"
```

Accounts in this list are treated like MindRoom agents for response logic — their messages and mentions don't count toward the multi-human detection.

### Routing Fallback

If routing fails (model error, invalid suggestion, etc.), the router sends a helpful error message: "I couldn't determine which agent should help with this. Please try mentioning an agent directly with @ or rephrase your request."

Users can always mention agents directly with `@agent_name` to bypass routing.

## Note on the Router Agent

The router is always present and cannot be disabled. It automatically joins any room with configured agents. If no `router` section is configured, it uses the default model.
