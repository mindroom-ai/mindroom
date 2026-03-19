---
icon: lucide/calendar
---

# Scheduling

Schedule agents to perform tasks at specific times or intervals using natural language. Tasks run in the thread where they were created.

## Commands

### Schedule a Task

```
!schedule <natural-language-request>
```

**One-Time Tasks:**

```
!schedule in 5 minutes Check the deployment
!schedule tomorrow at 3pm Send the weekly report
```

**Recurring Tasks:**

```
!schedule Every hour, @shell check server status
!schedule Daily at 9am, @finance market report
!schedule Weekly on Friday, @analyst prepare weekly summary
```

**Conditional Workflows (polling-based):**

Conditional or event-like requests are converted to recurring cron-based polling schedules.
The AI picks an appropriate polling frequency based on urgency, and the condition is embedded in the task message so the agent checks it on each poll cycle.
These are **not** real event subscriptions — they are periodic checks.

```
!schedule If I get an email about "urgent", @phone_agent call me
!schedule When Bitcoin drops below $40k, @crypto_agent notify me
```

### Edit a Schedule

```
!edit_schedule <task-id> <new-task-description>
```

Edits an existing scheduled task by ID. The task description is re-parsed to update timing and content.

### List and Cancel Schedules

```
!list_schedules                  # Show pending tasks
!cancel_schedule <task-id>       # Cancel specific task
!cancel_schedule all             # Cancel all tasks in room
```

Aliases: `!listschedules`, `!list-schedules`, `!list_schedule`, `!listschedule`, `!list-schedule`, `!inspect_schedules`, `!inspectschedules`, `!inspect-schedules`, `!inspect_schedule`, `!inspectschedule`, `!inspect-schedule`, `!cancelschedule`, `!cancel-schedule`, `!editschedule`, `!edit-schedule`

Use `!help schedule` for detailed inline help on scheduling commands.

## Agent Mentions

Include `@agent_name` in your schedule to have specific agents respond. The scheduler validates that mentioned agents are available in the room before creating the task.

## Timezone

Schedules use the timezone from `config.yaml` (defaults to UTC):

```yaml
timezone: America/Los_Angeles
```

## Limitations

- **Schedule type cannot be changed** — editing a one-time task to be recurring (or vice versa) is not supported. Cancel the existing task and create a new one instead.
- **Conditional workflows are polling** — event-like schedules (`If ...`, `When ...`) are converted to recurring cron polls, not real event subscriptions.

## Persistence

Schedules are stored in Matrix room state and persist across restarts.
Past one-time tasks are automatically skipped during restoration.
Only the router restores persisted schedules after startup — individual agents do not restore their own.
On shutdown, the router cancels its in-memory scheduled tasks before exiting.
