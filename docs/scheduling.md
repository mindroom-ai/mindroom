---
icon: lucide/calendar
---

# Scheduling

MindRoom supports scheduled tasks using cron expressions or natural language.

## Overview

Schedule agents to perform tasks at specific times or intervals. Scheduled tasks run in the context of the room where they were created.

## Commands

### Schedule a Task

```
!schedule <cron-or-natural-language> <message>
```

**Examples:**

```
!schedule "0 9 * * 1" @researcher Send me a weekly market summary
!schedule "every day at 9am" @assistant Good morning! What's on my calendar?
!schedule "in 30 minutes" @reminder Check on the deployment
```

### List Schedules

```
!list_schedules
```

Shows all scheduled tasks in the current room.

### Cancel a Schedule

```
!cancel_schedule <schedule-id>
```

Remove a scheduled task by its ID.

## Cron Expressions

Standard 5-field cron format:

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-6, Sunday=0)
│ │ │ │ │
* * * * *
```

**Examples:**

| Expression | Description |
|------------|-------------|
| `0 9 * * *` | Every day at 9:00 AM |
| `0 9 * * 1` | Every Monday at 9:00 AM |
| `*/15 * * * *` | Every 15 minutes |
| `0 0 1 * *` | First day of each month at midnight |
| `0 18 * * 1-5` | Weekdays at 6:00 PM |

## Natural Language

MindRoom parses natural language schedules:

| Input | Interpreted As |
|-------|----------------|
| `every day at 9am` | `0 9 * * *` |
| `every monday at noon` | `0 12 * * 1` |
| `every hour` | `0 * * * *` |
| `in 30 minutes` | One-time, 30 mins from now |
| `tomorrow at 3pm` | One-time, next day 15:00 |

## Timezone

Schedules use the timezone from `config.yaml`:

```yaml
timezone: America/Los_Angeles
```

If not set, UTC is used.

## Configuration

No additional configuration is needed. Scheduling is built into the core system.

## Storage

Schedules are stored in the room's Matrix state and persist across restarts.

## Best Practices

1. **Use descriptive messages** - Include the agent mention and clear task
2. **Test with short intervals** - Try `in 5 minutes` before setting up daily tasks
3. **Set appropriate timezone** - Ensure `config.yaml` has your timezone
4. **Clean up old schedules** - Periodically review with `!list_schedules`
