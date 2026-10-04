---
icon: lucide/terminal-square
---

# Chat Commands

MindRoom provides chat commands that users can type in any Matrix room where MindRoom agents or teams are present.
Commands start with `!` and are normally handled by the router agent.

## Quick Reference

| Command | Description |
|---------|-------------|
| `!help [topic]` | Get help on commands or a specific topic |
| `!hi` | Show the welcome message again |
| `!schedule <task>` | Schedule a task or reminder |
| `!list_schedules` | List pending scheduled tasks |
| `!cancel_schedule <id>` | Cancel a scheduled task |
| `!edit_schedule <id> <task>` | Edit an existing scheduled task |
| `!desktop [setup\|status\|confirm\|rotate\|disconnect]` | Manage your Desktop target for one agent |
| `!mode <agent> minimal\|standard\|show\|reset` | Switch one agent between standard tools and a Bash-only interface |
| `!model [name\|list\|reset]` | Show or switch the model used in the current thread |
| `!room_model [name\|list\|reset]` | Show the room model default or switch it (set/reset require a room admin) |
| `!thread_mode [room\|thread\|reset\|show]` | Show or switch the thread mode used in the current room |
| `!encrypt [confirm]` | Enable end-to-end encryption for this room (irreversible, room admin only) |
| `!e2ee` | Show encryption diagnostics for this room |
| `!config <operation>` | View and modify configuration (disabled by default, admin only when enabled) |
| `!reload-plugins` | Force-reload all configured plugins (admin only) |

## Who Handles Commands

The **router** normally handles commands.
`!desktop` has its own room requirements; see [`!desktop`](tools/desktop.md#desktop).
Commands work in both main room messages and within threads.

Voice transcription is not rewritten into chat-command syntax; commands must arrive as text commands.

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

## Permission Behavior

Commands are subject to the same authorization rules as normal messages.
The responder's `access` policy authorizes the sender.
See [Authorization](authorization.md) for details.

`!config` is disabled by default.
Set `authorization.config_command_enabled: true` to enable it.
When enabled, callers must be platform administrators.
For `!config set`, only the user who requested the change can confirm or cancel it via reactions.
Pending config changes expire after 24 hours.

## Commands

### `!help`

Display available commands or get detailed help on a specific topic.

```
!help
!help schedule
!help config
!help cancel_schedule
!help edit_schedule
```

**Topics:** `schedule`, `config`, `mode`, `model`, `room_model`, `room-model`, `roommodel`, `thread_mode`, `thread-mode`, `threadmode`, `list_schedules`, `inspect_schedules`, `cancel`, `cancel_schedule`, `edit`, `edit_schedule`, `reload-plugins`, `reload_plugins`, `encrypt`, `e2ee`, `encryption`

### `!hi`

Show the welcome message for the current room, listing available agents and teams, their roles and tools, and quick-start instructions.

```
!hi
```

See [Scheduling](scheduling.md#schedule), [`!desktop`](tools/desktop.md#desktop), [`!mode`](tools/agent-cli.md#mode), [`!model`](configuration/models.md#model), [`!room_model`](configuration/models.md#room_model), [`!thread_mode`](configuration/threads.md#thread_mode), [`!encrypt`](matrix.md#encrypt), and [`!e2ee`](matrix.md#e2ee).

### `!config`

View and modify MindRoom configuration from chat.
This command is disabled by default.
Set `authorization.config_command_enabled: true` to enable it.
When enabled, only platform administrators can use it.
Changes are validated against the Pydantic config schema before applying.

**View configuration:**

```
!config show
!config get agents
!config get models.default
!config get agents.analyst.display_name
```

Shown values, including the current and new values in a `!config set` preview, are redacted like `config_manager` inspection.
Every value inside a field the config schema marks secret is masked, such as MCP server `env` and `headers`, plugin `settings`, model `extra_kwargs`, API keys, and Git repository URLs, except `${NAME}` environment references, which name where a secret lives and are shown as written.
Other typed fields keep their values; entries in free-form maps, such as tool overrides, are also masked when their key names look like credentials, and credential patterns in any text, such as URL passwords and bearer tokens, are masked.
In typed text fields such as `role` and `instructions`, an ordinary word after "API key" or "bearer" and a kebab-case name such as `sk-learn` are shown as written.

**Modify configuration:**

```
!config set agents.analyst.display_name "Research Expert"
!config set models.default.id gpt-6-astra
!config set defaults.markdown false
!config set timezone America/New_York
```

**Path syntax:**

- Use dot notation to navigate nested config (e.g., `agents.analyst.role`)
- Arrays use indexes (e.g., `agents.analyst.tools.0` for first tool)
- String values with spaces must be quoted

#### Confirmation flow

When you use `!config set`, MindRoom:

1. Validates the proposed change against the config schema
2. Shows a preview with the current and new values
3. Adds reaction buttons to the preview message
4. Waits for the requester to react with ✅ (confirm) or ❌ (cancel)

Only the user who requested the change can confirm or cancel it.
Pending changes are persisted in Matrix room state and survive restarts.
Room state is readable by every room member and is not end-to-end encrypted, so the pending change stores the requester, room, thread, creation time, configuration path, and decision progress, but never the current value.
It stores the new value only when redaction leaves that value unchanged and room state can carry it, which rules out decimal numbers and integers beyond 2^53 - 1.
Any other new value is kept only in the running MindRoom process; if MindRoom restarts before you confirm, the confirmation replies that the pending change was lost and asks you to run `!config set` again.
`!config set` refuses a value containing the `***redacted***` marker or a URL password masked as `user:***@host`, which only appear in redacted output, so copying shown values back never replaces a hidden real value; set that field to its real value instead.
Unconfirmed changes expire after 24 hours.

Changes are saved to `config.yaml` immediately on confirmation and take effect for new agent interactions.

#### Configuration Confirmations

The router handles interactive configuration changes.
When a config change is requested, the router posts a confirmation message with reactions, and only the router processes the confirmation reactions.

See [`!reload-plugins`](plugins.md#reload-plugins).

## Stop Button

MindRoom supports cancelling in-progress responses via a reaction-based stop button, not a chat command.

When `defaults.show_stop_button` is `true` (the default), MindRoom adds a 🛑 reaction to the agent's message while it is generating.
React with 🛑 on the message to cancel the response.
Setting `show_stop_button: false` hides only that reaction: responses still stream, and a 🛑 reaction you add yourself still cancels.
The agent finalizes the partial text with `**[Response cancelled by user]**`.

The stop button only works on messages currently being generated.
Only non-agent users can trigger cancellation — agent reactions are ignored.
A 🛑 reaction only stops a response in the room where the reaction was sent.

See [Streaming — Cancellation](streaming.md#cancellation-and-errors) for details on how cancelled responses are finalized.

## Unknown Commands

Any message starting with `!` that does not match a known command returns an error message suggesting `!help`.
