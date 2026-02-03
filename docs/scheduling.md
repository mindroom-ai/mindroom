---
icon: lucide/calendar
---

# Scheduling

MindRoom supports scheduled tasks using natural language, powered by AI parsing.

## Overview

Schedule agents to perform tasks at specific times or intervals. The scheduling system uses AI to understand natural language requests and converts them to either one-time tasks or recurring cron-based schedules. Scheduled tasks run in the context of the thread where they were created.

## Commands

### Schedule a Task

```
!schedule <natural-language-request>
```

Write your scheduling request in natural language. The AI will parse it and determine the timing and task.

**Simple Reminders:**

```
!schedule in 5 minutes Check the deployment
!schedule tomorrow at 3pm Send the weekly report
!schedule later Ping me about the meeting
```

**Recurring Tasks:**

```
!schedule Every hour, @shell check server status
!schedule Daily at 9am, @finance market report
!schedule Weekly on Friday, @analyst prepare weekly summary
!schedule Every Monday, @research AI news and @email_assistant send me a summary
```

**Event-Driven Workflows:**

The system can convert event-based requests into smart polling schedules:

```
!schedule If I get an email about "urgent", @phone_agent call me
!schedule When Bitcoin drops below $40k, @crypto_agent notify me
!schedule If server CPU > 80%, @ops_agent scale up
!schedule Whenever I get email from boss, @notification_agent alert me immediately
```

### List Schedules

```
!list_schedules
```

Shows all pending scheduled tasks in the current thread.

### Cancel a Schedule

```
!cancel_schedule <task-id>
!cancel_schedule all
```

Cancel a specific scheduled task by its ID, or use `all` to cancel all scheduled tasks in the room.

Use `!list_schedules` to see task IDs.

## How It Works

### AI-Powered Parsing

MindRoom uses AI to parse your natural language request into a structured schedule. The AI determines:

1. **Schedule type**: One-time or recurring (cron-based)
2. **Timing**: When to execute the task
3. **Message**: What to post when the task executes, including agent mentions

### Schedule Types

| Type | Description | Example |
|------|-------------|---------|
| One-time | Executes once at a specific time | `in 30 minutes`, `tomorrow at 3pm` |
| Recurring | Executes repeatedly on a cron schedule | `every day at 9am`, `weekly on Monday` |

### Agent Mentions

Include `@agent_name` in your schedule to have specific agents respond. The scheduler validates that mentioned agents are available in the thread before creating the task.

## Cron Reference

For recurring tasks, the system internally uses standard 5-field cron expressions:

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-6, Sunday=0)
│ │ │ │ │
* * * * *
```

**Common Patterns:**

| Natural Language | Cron Equivalent | Description |
|------------------|-----------------|-------------|
| `every day at 9am` | `0 9 * * *` | Daily at 9:00 AM |
| `every monday at noon` | `0 12 * * 1` | Weekly on Monday at noon |
| `every hour` | `0 * * * *` | Hourly at minute 0 |
| `every 15 minutes` | `*/15 * * * *` | Every 15 minutes |
| `weekdays at 6pm` | `0 18 * * 1-5` | Mon-Fri at 6:00 PM |

## Timezone

Schedules use the timezone from `config.yaml`:

```yaml
timezone: America/Los_Angeles
```

If not set, UTC is used. Times are displayed in your configured timezone with relative time indicators (e.g., "in 2 hours").

## Storage and Persistence

Schedules are stored in the room's Matrix state using the `com.mindroom.scheduled.task` event type. This means:

- Tasks persist across bot restarts
- The router agent automatically restores pending tasks on startup
- Past one-time tasks are skipped during restoration

## Best Practices

1. **Use descriptive requests** - Be clear about timing and what you want to happen
2. **Mention agents explicitly** - Include `@agent_name` for specific agent responses
3. **Test with short intervals** - Try `in 5 minutes` before setting up daily tasks
4. **Set appropriate timezone** - Ensure `config.yaml` has your timezone
5. **Clean up old schedules** - Periodically review with `!list_schedules`
6. **Use threads** - Scheduled tasks execute in the thread where they were created
