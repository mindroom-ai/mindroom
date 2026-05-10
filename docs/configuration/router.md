---
icon: lucide/route
---

# Router Configuration

The router is a built-in system component that handles intelligent message routing and room management.
It decides which agent or team should respond when no specific agent or team is mentioned, sends welcome messages to new rooms, and manages various system-level tasks.

## Configuration

```yaml
router:
  # Model for routing decisions (defaults to "default")
  model: haiku

  # Accept authorized room invites and preserve them across restarts (default: true)
  accept_invites: true

  # Participate in room-level startup prewarm for rooms already joined at first sync (default: true)
  startup_thread_prewarm: true
```

The router has three configuration options:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `model` | string | `"default"` | Model to use for routing decisions |
| `accept_invites` | bool | `true` | When enabled, the router accepts authorized room invites, persists accepted room IDs, rejoins them after restart, and preserves them during room cleanup |
| `startup_thread_prewarm` | bool | `true` | When enabled, the router may prewarm recent thread snapshots for rooms already joined when first sync completes, which can reduce cold-cache latency for early thread replies after startup |

Startup thread prewarm is a background, best-effort cache warmup for rooms already joined when first sync completes.

## How Routing Works

When a message arrives in a room without a specific agent or team mention, MindRoom first builds the eligible responder candidate set for that sender and room.

1. If the thread already requires explicit targeting, MindRoom stays silent until someone mentions an agent or team
2. If exactly one eligible responder remains, that agent or team handles the message directly
3. If multiple eligible responders remain, the router analyzes the message content and any recent thread context (up to 3 previous messages)
4. Based on the candidate entities' roles, tools, and instructions, it selects the best match
5. The router posts a message mentioning the selected entity (e.g., "@agent could you help with this?")
6. The mentioned agent or team sees the mention and responds in the thread

For configured rooms, routing candidates come only from `agents.<name>.rooms` and `teams.<name>.rooms`, then are filtered by the sender's per-entity reply permissions.
For ad-hoc rooms accepted through invites, routing candidates come from the sender-visible MindRoom agents and teams currently joined to that room, then are filtered by the same sender permissions.

When multiple responders are eligible, the router uses a structured output schema to ensure consistent routing decisions, including the selected agent or team name and reasoning for the selection.

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
- `!config <operation>` - Manage configuration

Even in single-responder rooms, commands are always processed by the router.

### Welcome Messages

When the router joins a room after an invite, it sends a requester-scoped welcome message.

That welcome message lists:

- Available agents and teams visible to the inviter with their descriptions
- How to interact with agents and teams (mentions, commands)
- Quick command reference

Startup welcomes with no requester list configured room responders when the room is statically configured.
Startup welcomes for ad-hoc rooms send the general interaction guidance and quick command reference without an available-responder list.

Use `!hi` in any room to see the welcome message again.

The `!hi` welcome lists responders visible to the requester.

### Room Management

The router creates and manages rooms:

- Creates configured rooms that don't exist yet
- Invites configured agents, teams, and users to their rooms
- Applies `matrix_room_access` policy for managed rooms (when enabled)
- Reconciles managed room power levels so the custom thread-tags state event can be written at PL0
- Generates AI-powered room topics based on configured agents and teams
- Has admin privileges to manage room membership
- Cleans up orphaned bots on startup

By default (`matrix_room_access.mode: single_user_private`), rooms remain invite-only and private in the room directory.
In `multi_user` mode, the router can set join rules (`public`/`knock`) and optionally publish rooms to the server directory.
That same reconciliation path also updates `m.room.power_levels` for managed rooms, so the router must be joined and able to edit room power levels when thread tags are enabled.

### Voice Message Processing

Audio events are handled through the shared media pipeline on all bots.
The router only posts a visible handoff when it must disambiguate between multiple eligible responders in a room.
When the responder is already clear, normalized audio follows the normal direct agent or team dispatch rules without an extra router message.
By default, `voice.visible_router_echo: true` also lets the router post the normalized voice text as a display-only message when it is allowed to reply.
Set `voice.visible_router_echo: false` to suppress that display-only echo.

See [Voice Messages](../voice.md) for the detailed dispatch behavior.

### Configuration Confirmations

The router handles interactive configuration changes.
When a config change is requested, the router posts a confirmation message with reactions, and only the router processes the confirmation reactions.

### Scheduled Task Restoration

When the router joins a room, it restores any previously scheduled tasks and pending configuration changes to ensure they persist across restarts.

## Routing Behavior Details

### Single Responder Optimization

When there is only one eligible responder for a room, the router skips AI routing entirely.
The single responder handles messages directly, which is faster and more efficient.

### Multi-Human Thread Protection

When multiple human users have posted in a thread, the router, agents, and teams require an explicit `@mention` before responding.
This prevents MindRoom entities from injecting themselves into human-to-human conversations.

The rules are:

1. **Mentioned eligible agents or teams respond** — an explicit `@agent` or `@team` bypasses AI routing, but room configuration and reply permissions still apply.
2. **Non-thread messages** — a single eligible agent or team can auto-respond, regardless of how many humans are present.
3. **Threads with one human** — normal auto-response behavior applies, so the agent or team continues the conversation.
4. **Threads with two or more humans** — agents and teams stay silent unless explicitly mentioned.
5. **Mentioning a non-MindRoom user** — if a message tags only humans or unmanaged users, agents and teams stay silent.

#### Bot accounts

By default, any Matrix user that is not a MindRoom agent or team counts as a "human" for the rules above.
This includes bridge bots (Telegram, Slack, etc.) and other non-MindRoom bots.
If a bridge bot relays a message into a thread, it looks like a second human to MindRoom and triggers the mention requirement.

To prevent this, list those accounts in `bot_accounts`:

```yaml
bot_accounts:
  - "@telegram:example.com"
  - "@slackbot:example.com"
```

Accounts in this list are treated like MindRoom entities for response logic — their messages and mentions don't count toward the multi-human detection.

### Routing Fallback

If routing fails (model error, invalid suggestion, etc.), the router sends a helpful error message that asks the user to mention an agent or team directly or rephrase the request.

Users can mention eligible agents or teams directly with `@entity_name` to bypass routing, while configured-room allowlists and reply permissions still decide whether that entity may answer.

## Note on the Router Agent

The router is always present and cannot be disabled.
It automatically joins any room with configured agents or teams.
If no `router` section is configured, it uses the default model.

The router account is not a conversational AI agent to tag directly.
If a message mentions only the router and no other users, agents, or teams, the router replies with the rules of engagement instead of answering the prompt.
Mention a specific agent or team when you want that entity to answer.
Mention multiple agents when you want an ad-hoc collaboration, or mention a configured team directly for its team workflow.
When one human and one agent or team are already talking in a thread, continuing without an explicit tag is fine.
Once a thread has multiple human users or multiple agent/team participants, tag the agents or teams you want next.
In a new untagged message, automatic routing can still choose an agent or team when that is appropriate.
