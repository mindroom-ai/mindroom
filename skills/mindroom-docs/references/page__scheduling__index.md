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

**Event-Driven Workflows:**

Conditional requests are converted to polling schedules:

```
!schedule If I get an email about "urgent", @phone_agent call me
!schedule When Bitcoin drops below $40k, @crypto_agent notify me
```

### List and Cancel Schedules

```
!list_schedules                  # Show pending tasks
!cancel_schedule <task-id>       # Cancel specific task
!cancel_schedule all             # Cancel all tasks in room
```

Aliases: `!listschedules`, `!list-schedules`, `!cancelschedule`, `!cancel-schedule`

## Agent Mentions

Include `@agent_name` in your schedule to have specific agents respond. The scheduler validates that mentioned agents are available in the room before creating the task.

## Timezone

Schedules use the timezone from `config.yaml` (defaults to UTC):

```
timezone: America/Los_Angeles
```

## Persistence

Schedules are stored in Matrix room state and persist across restarts. Past one-time tasks are automatically skipped during restoration.
