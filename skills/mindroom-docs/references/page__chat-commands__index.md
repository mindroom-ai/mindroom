# Chat Commands

Chat commands are messages starting with `!` that you type in any Matrix room where MindRoom agents or teams are present.
Use them to get help, manage schedules, switch models or modes, manage room encryption, and, when enabled, change configuration from chat.
This page lists every command and fully covers `!help`, `!hi`, and `!config`; the other commands are documented on the linked pages.

## Command List

| Command | Description | Details |
|---------|-------------|---------|
| `!help [topic]` | Get help on commands or a specific topic | [`!help`](#help) |
| `!hi` | Show the welcome message again | [`!hi`](#hi) |
| `!schedule <task>` | Schedule a task or reminder | [Scheduling](https://docs.mindroom.chat/scheduling/#schedule) |
| `!list_schedules` | List pending scheduled tasks | [Scheduling](https://docs.mindroom.chat/scheduling/#list_schedules) |
| `!cancel_schedule <id>` | Cancel a scheduled task | [Scheduling](https://docs.mindroom.chat/scheduling/#cancel_schedule) |
| `!edit_schedule <id> <task>` | Edit an existing scheduled task | [Scheduling](https://docs.mindroom.chat/scheduling/#edit_schedule) |
| `!desktop [setup\|status\|confirm\|rotate\|disconnect]` | Manage your Desktop target for one agent | [Desktop](https://docs.mindroom.chat/tools/desktop/#desktop) |
| `!mode <agent> minimal\|standard\|show\|reset` | Switch one agent between standard tools and a Bash-only interface | [Minimal Agent Mode](https://docs.mindroom.chat/tools/agent-cli/#mode) |
| `!model [name\|list\|reset]` | Show or switch the model used in the current thread | [Models](https://docs.mindroom.chat/configuration/models/#model) |
| `!room_model [name\|list\|reset]` | Show the room model default or switch it (set/reset require a room admin) | [Models](https://docs.mindroom.chat/configuration/models/#room_model) |
| `!thread_mode [room\|thread\|reset\|show]` | Show or switch the thread mode used in the current room (set/reset require a room admin) | [Threads](https://docs.mindroom.chat/configuration/threads/#thread_mode) |
| `!encrypt [confirm]` | Enable end-to-end encryption for this room (irreversible, room admin only) | [Matrix](https://docs.mindroom.chat/matrix/#encrypt) |
| `!e2ee` | Show encryption diagnostics for this room | [Matrix](https://docs.mindroom.chat/matrix/#e2ee) |
| `!config <operation>` | View and modify configuration (disabled by default, platform administrators only) | [`!config`](#config) |
| `!reload-plugins` | Force-reload all configured plugins (platform administrators only) | [Plugins](https://docs.mindroom.chat/plugins/#reload-plugins) |

Command names are case-insensitive.
Any other message starting with `!` gets `❌ Unknown command. Try !help for available commands.`

## Command Handling

The router answers commands, in the main room and in threads, even in rooms with a single agent.
The exception is `!desktop`, which a private agent may answer itself when the room contains only you and that agent; see [`!desktop`](https://docs.mindroom.chat/tools/desktop/#desktop) for its room requirements.

Commands follow the same [authorization](https://docs.mindroom.chat/authorization/) rules as ordinary messages.
"Room admin" means a Matrix room administrator, while "platform administrator" means a user listed in `administrators`.

Commands must be typed as text; voice messages are never turned into commands.

An agent can send `!help`, `!mode`, `!model`, and the schedule commands on your behalf, and they run with your permissions.
Any other command sent by an agent for you is refused with `❌ Agents cannot run this command for you. Send it yourself.`

## `!help`

`!help` lists all commands, and `!help <topic>` shows detailed usage and examples for one topic.

```
!help
!help schedule
!help config
```

**Topics:** `schedule`, `list_schedules`, `inspect_schedules`, `cancel`, `cancel_schedule`, `edit`, `edit_schedule`, `config`, `mode`, `model`, `room_model`, `room-model`, `roommodel`, `thread_mode`, `thread-mode`, `threadmode`, `reload-plugins`, `reload_plugins`, `encrypt`, `e2ee`, `encryption`

Any other topic shows the general command list.

## `!hi`

`!hi` shows the room's welcome message again: the agents and teams you can address, their roles and tools, and quick-start instructions.

## `!config`

`!config` views and changes `config.yaml` from chat.
It is disabled by default; set `authorization.config_command_enabled: true` (boolean, default `false`) to enable it.
Even when enabled, only platform administrators can use it.
Otherwise it replies `❌ Config command disabled.` or `❌ Admin only.`

```
!config show
!config get agents
!config get agents.analyst.display_name
!config set agents.analyst.display_name "Research Expert"
!config set models.default.id gpt-6-astra
!config set defaults.markdown false
!config set agents.analyst.tools [shell, file]
```

`!config` alone is the same as `!config show`.
Paths use dot notation, such as `agents.analyst.role`, and list items use indexes, such as `agents.analyst.tools.0` for the first tool.
Values are parsed as YAML, so `false`, `42`, and `[shell, file]` become a boolean, a number, and a list.
Quote a value to keep its exact spacing.

Shown values are redacted like `config_manager` output; see [Access, Redaction, and Saving](https://docs.mindroom.chat/tools/agent-orchestration/#access-redaction-and-saving).
`!config set` refuses a value containing the `***redacted***` marker or a masked URL password such as `user:***@host`, so copying shown values back cannot overwrite a hidden secret; set the field to its real value instead.

### Configuration Confirmations

`!config set` validates the change against the full configuration and replies with an error ending in `Changes were NOT applied.` when it is invalid.
A valid change gets a preview of the current and new values from the router, with ✅ and ❌ reactions.
Only the user who requested the change can confirm or cancel it, and that user must still be a platform administrator.
Unconfirmed changes expire after 24 hours.

Pending changes usually survive a MindRoom restart; if one cannot, such as a secret value, the confirmation asks you to run `!config set` again.

On confirmation, the change is saved to `config.yaml` and applies to new agent interactions, except [event journal](https://docs.mindroom.chat/deployment/storage/#event-journal) changes, which apply after MindRoom restarts.

## Stop Button

Cancel an in-progress response by reacting with 🛑 on the message being generated.
When `defaults.show_stop_button` is `true` (the default), MindRoom adds that 🛑 reaction to the message for you to click.
Setting `show_stop_button: false` hides only that reaction; a 🛑 reaction you add yourself still cancels.

The stop button only works on messages still being generated, and 🛑 reactions from agents are ignored.
See [Cancellation and Errors](https://docs.mindroom.chat/streaming/#cancellation-and-errors) for how a cancelled response is finalized.
