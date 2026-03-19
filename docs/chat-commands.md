---
icon: lucide/terminal-square
---

# Chat Commands

MindRoom provides chat commands that users can type in any Matrix room where MindRoom agents are present.
Commands start with `!` and are handled by the router agent.

## Quick Reference

| Command | Description |
|---------|-------------|
| `!help [topic]` | Get help on commands or a specific topic |
| `!hi` | Show the welcome message again |
| `!schedule <task>` | Schedule a task or reminder |
| `!list_schedules` | List pending scheduled tasks |
| `!cancel_schedule <id>` | Cancel a scheduled task |
| `!edit_schedule <id> <task>` | Edit an existing scheduled task |
| `!config <operation>` | View and modify configuration |
| `!skill <name> [args]` | Run a skill by name |

## Who Handles Commands

The **router** handles all commands exclusively.
Even in single-agent rooms, commands are always processed by the router, not the agent.
Commands work in both main room messages and within threads.

Voice messages that contain commands (e.g., spoken `!schedule`) are recognized after transcription and processed the same way.

## Permission Behavior

Commands are subject to the same authorization rules as normal messages.
The sender must be authorized to interact with agents in the room (via `global_users`, `room_permissions`, or `default_room_access`).
See [Authorization](authorization.md) for details.

For `!config set`, only the user who requested the change can confirm or cancel it via reactions.
Pending config changes expire after 24 hours.

## Commands

### `!help`

Display available commands or get detailed help on a specific topic.

```
!help
!help schedule
!help config
!help skill
!help cancel_schedule
!help edit_schedule
```

**Topics:** `schedule`, `skill`, `config`, `list_schedules`, `inspect_schedules`, `cancel`, `cancel_schedule`, `edit`, `edit_schedule`

### `!hi`

Show the welcome message for the current room, listing available agents, their roles and tools, and quick-start instructions.

```
!hi
```

### `!schedule`

Schedule a one-time or recurring task using natural language.
Tasks run in the thread where they were created.

```
!schedule <natural-language-request>
```

**One-time tasks:**

```
!schedule in 5 minutes Check the deployment
!schedule tomorrow at 3pm Send the weekly report
```

**Recurring tasks:**

```
!schedule Every hour, @shell check server status
!schedule Daily at 9am, @finance market report
!schedule Weekly on Friday, @analyst prepare weekly summary
```

**Conditional workflows (polling-based):**

Conditional requests are converted to recurring cron-based polling schedules.
These are periodic checks, not real event subscriptions.

```
!schedule If I get an email about "urgent", @phone_agent call me
!schedule When Bitcoin drops below $40k, @crypto_agent notify me
```

Include `@agent_name` in your schedule to target specific agents.
The scheduler validates that mentioned agents are available in the room before creating the task.

Schedules use the timezone from `config.yaml` (defaults to UTC).
See [Scheduling](scheduling.md) for full details.

### `!list_schedules`

List pending scheduled tasks in the current room or thread.

```
!list_schedules
```

**Aliases:** `!listschedules`, `!list-schedules`, `!list_schedule`, `!listschedule`, `!list-schedule`, `!inspect_schedules`, `!inspectschedules`, `!inspect-schedules`, `!inspect_schedule`, `!inspectschedule`, `!inspect-schedule`

### `!cancel_schedule`

Cancel a specific scheduled task or all tasks in the room.

```
!cancel_schedule <task-id>
!cancel_schedule all
```

Use `!list_schedules` to find task IDs.

**Aliases:** `!cancelschedule`, `!cancel-schedule`

### `!edit_schedule`

Replace an existing scheduled task with new timing and content.

```
!edit_schedule <task-id> <new-task-description>
```

The task description is re-parsed to update timing and content.
Schedule type cannot be changed (one-time to recurring or vice versa) -- cancel and recreate instead.

**Aliases:** `!editschedule`, `!edit-schedule`

### `!config`

View and modify MindRoom configuration from chat.
Changes are validated against the Pydantic config schema before applying.

**View configuration:**

```
!config show
!config get agents
!config get models.default
!config get agents.analyst.display_name
```

**Modify configuration:**

```
!config set agents.analyst.display_name "Research Expert"
!config set models.default.id gpt-5.4
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
Unconfirmed changes expire after 24 hours.

Changes are saved to `config.yaml` immediately on confirmation and take effect for new agent interactions.

### `!skill`

Run a user-invocable skill by name.

```
!skill <name> [args]
```

**Examples:**

```
!skill repo-quick-audit
!skill summarize Release notes for v2.3
```

**Agent resolution:**

- If you mention an agent (e.g., `@mindroom_code !skill build`), that agent handles the skill.
- If only one agent in the room has the skill enabled, it handles the request.
- If multiple agents have the skill, you must mention one to disambiguate.

**Rules:**

- The skill must be in the agent's `skills` allowlist and `user-invocable` must be `true`.
- If `command-dispatch: tool` is set in the skill's frontmatter, the tool runs directly.
- If `disable-model-invocation: true` and no tool dispatch is configured, the command fails.

See [Skills](skills.md) for skill configuration details.

## Stop Button

MindRoom supports cancelling in-progress responses via a reaction-based stop button, not a chat command.

When `defaults.show_stop_button` is `true` (the default), MindRoom adds a 🛑 reaction to the agent's message while it is generating.
React with 🛑 on the message to cancel the response.
The agent finalizes the partial text with `**[Response cancelled by user]**`.

The stop button only works on messages currently being generated.
Only non-agent users can trigger cancellation — agent reactions are ignored.

See [Streaming — Cancellation](streaming.md#cancellation-and-errors) for details on how cancelled responses are finalized.

## Unknown Commands

Any message starting with `!` that does not match a known command returns an error message suggesting `!help`.
