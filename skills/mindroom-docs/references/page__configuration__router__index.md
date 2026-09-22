# Router Configuration

The router is a built-in system component that handles intelligent message routing and room management.
It decides which agent or team should respond when no specific agent or team is mentioned, sends welcome messages to new rooms, and manages various system-level tasks.

## Configuration

```yaml
router:
  # Model for routing decisions (defaults to "default")
  model: haiku

  # Accept all, none, or matching inviter ID patterns (default: true)
  accept_invites: true

```

The router supports these configuration options:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `model` | string | `"default"` | Model to use for routing decisions |
| `access` | object or null | `null` | Membership-based responder access policy |
| `accept_invites` | bool or list[string] | `true` | Accept all inbound Matrix room invites with `true`, none with `false` or `[]`, or only inviters matching an exact or wildcard Matrix user ID in the list. Accepted room IDs are persisted, rejoined after restart, and preserved during room cleanup |

Invitation patterns are matched after identity alias resolution and use the same case-sensitive wildcard semantics as responder `access.users`.
Invitation acceptance grants room membership only and remains independent from responder access.
The router applies its `access` policy to every interaction after joining.
Omitted router access resolves to `current_room_members: true` with empty room and user grants.
Setting only `access.users` preserves that current-room-member grant; set `current_room_members: false` explicitly to remove it.
See [Authorization](https://docs.mindroom.chat/authorization/) for the policy fields.

## How Routing Works

When a message arrives in a room without a specific agent or team mention, MindRoom first builds the eligible responder candidate set for that sender and room.

1. If the thread requires explicit targeting, the router stays silent; eligible existing individual agents may still use the opt-in adaptive participation described below
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

The router owns commands by default.
A requester-scoped `!desktop` command may instead be owned by an eligible private agent when the room contains exactly that requester and agent.

- `!help [topic]` - Get help on commands or specific topics
- `!hi` - Show the welcome message again
- `!schedule <task>` - Schedule tasks and reminders
- `!list_schedules` - List scheduled tasks
- `!cancel_schedule <id>` - Cancel a scheduled task
- `!edit_schedule <id> <task>` - Edit an existing scheduled task
- `!reload-plugins` - Reload configured plugins (admin only)
- `!config <operation>` - Manage configuration when explicitly enabled for global admins
- `!desktop [setup|status|confirm|rotate|disconnect]` - Manage the requester's Desktop target
- `!model [name|list|reset]` - Show or switch the current thread model
- `!room_model [name|list|reset]` - Show the current room model default or switch it (set/reset require a room admin)
- `!thread_mode [room|thread|reset|show]` - Manage room thread mode (room admin only)
- `!encrypt [confirm]` - Enable room encryption (irreversible, room admin only)
- `!e2ee` - Show room encryption diagnostics

Except for the requester-scoped `!desktop` case above, commands are processed by the router even in single-responder rooms.

### Welcome Messages

After accepting an invite, the router sends a requester-scoped welcome message only when the room has no existing message history.

That welcome message lists:

- Available agents and teams visible to the inviter with their descriptions
- How to interact with agents and teams (mentions, commands)
- Quick command reference

Startup welcomes with no requester list configured room responders when the room is statically configured.
Startup does not send requester-less welcomes in persisted ad-hoc invite rooms because the original inviter cannot be re-authorized safely from persisted state.
The live invite callback sends the requester-scoped welcome when the inviter currently has router reply access.

Use `!hi` in any room to see the welcome message again.

The `!hi` welcome lists responders visible to the requester.

### Room Management

The router creates and manages rooms:

- Creates configured rooms that don't exist yet
- Invites configured agents, teams, and users to their rooms
- Applies effective `room_defaults` and per-room policy for managed rooms
- Reconciles managed room power levels so the custom thread-tags state event can be written at PL0
- Generates AI-powered room topics based on configured agents and teams
- Has admin privileges to manage room membership
- Cleans up orphaned bots on startup

Every concrete Matrix agent operating in a room also receives a built-in zero-argument `invite_router` recovery tool.
The tool can invite only the persisted router identity and only into the agent's current room.
The router accepts the invite and persists the room only when `router.accept_invites` allows the current Matrix transport account's user ID.
A team member therefore authorizes recovery through the team's Matrix account, not the member agent's account.
The recovery tool waits briefly for joined membership and reports a pending state when the router has not joined yet.
This lets an agent recover router-backed approvals without adding persistent prompt instructions or exposing arbitrary invite targets.

By default, `room_defaults.join_policy: invite` and `room_defaults.listed: false` keep managed rooms private.
Set `room_defaults.join_policy` to `public` or `knock`, and use `room_defaults.listed` to control room-directory visibility.
Per-room values replace these defaults when configured under `rooms.<key>`.
That same reconciliation path also updates `m.room.power_levels` for managed rooms, so the router must be joined and able to edit room power levels when thread tags are enabled.

### Voice Message Processing

Audio events are handled through the shared media pipeline on all bots.
The router only posts a visible handoff when it must disambiguate between multiple eligible responders in a room.
When the responder is already clear, normalized audio follows the normal direct agent or team dispatch rules without an extra router message.
By default, `voice.visible_router_echo: true` also lets the router post an immediate display-only transcription placeholder and replace it with the normalized transcript or fallback text when voice STT is enabled and it is allowed to reply.
With STT disabled, the router posts the display-only fallback directly.
Set `voice.visible_router_echo: false` to suppress that display-only echo.

See [Voice Messages](https://docs.mindroom.chat/voice/) for the detailed dispatch behavior.

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

When multiple human users have posted in a thread, explicit `@mention` targeting is the default.
An authorized, materializable individual agent that already replied in the thread can opt into judging an untagged turn with `agents.<name>.participation`.
It can answer after participation approval; a decline produces no text reply, and failed checks stay quiet after any configured judgment fallback.
A configured `decline_reaction` can acknowledge a deliberate decline.
See [Adaptive Participation](https://docs.mindroom.chat/configuration/agents/#adaptive-participation) for eligibility and configuration.
The router stays silent and multi-human threads do not automatically form ad-hoc teams.

The rules are:

1. **Mentioned eligible agents or teams respond** — an explicit `@agent` or `@team` bypasses AI routing, but room configuration and reply permissions still apply.
2. **Non-thread messages** — a single eligible agent or team can auto-respond, regardless of how many humans are present.
3. **Threads with one human** — normal auto-response behavior applies, so the agent or team continues the conversation.
4. **Threads with two or more humans** — explicit targeting is required by default; eligible individual agents can use the opt-in participation exception above.
5. **Mentioning another joined room participant** — if a message tags only joined users who are neither managed entities nor configured bot accounts, agents and teams stay silent.

Mentions of unmanaged user IDs that are absent from the room or merely invited do not suppress normal routing or automatic responses.
Mentioning a configured agent that is not joined to the room still counts as an explicit mention for the other agents.

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
Explicit tags select the agents or teams you want next.
An untagged single-human thread can also continue an eligible ad-hoc team of previously mentioned or participating individual agents; named team IDs do not become ad-hoc members.
Multi-human threads use the explicit-targeting default and opt-in individual participation described above.
In a new untagged message, automatic routing can still choose an agent or team when that is appropriate.
