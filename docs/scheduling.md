---
icon: lucide/calendar
---

# Scheduling

Schedule agents or teams to perform tasks at specific times or intervals using natural language.

By default, tasks run in the same scope where they were created: the room timeline for room-level schedules, or the current thread for threaded schedules.
The `schedule()` tool accepts `new_thread=True` to start a fresh thread per fire: each fire posts a room-level root and the responding agent answers in a new thread under it with a fresh session.

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

For predictable behavior, include an explicit polling cadence.

The condition is embedded in the task message so the scheduled responder checks it on each poll cycle.

These are **not** real event subscriptions — they are periodic checks.

```
!schedule Every 5 minutes, check if I got an email about "urgent"; if so, @phone_agent call me
!schedule Every 10 minutes, check whether Bitcoin dropped below $40k; if so, @crypto_agent notify me
```

### Edit a Schedule

```
!edit_schedule <task-id> <new-task-description>
```

Edits an existing scheduled task by ID.

The task description is re-parsed to update timing and content.

### List and Cancel Schedules

```
!list_schedules                  # Show pending tasks
!cancel_schedule <task-id>       # Cancel specific task
!cancel_schedule all             # Cancel all tasks in room
```

Aliases: `!listschedules`, `!list-schedules`, `!list_schedule`, `!listschedule`, `!list-schedule`, `!inspect_schedules`, `!inspectschedules`, `!inspect-schedules`, `!inspect_schedule`, `!inspectschedule`, `!inspect-schedule`, `!cancelschedule`, `!cancel-schedule`, `!editschedule`, `!edit-schedule`

Use `!help schedule` for detailed inline help on scheduling commands.

## Silent Delivery

Schedules are visible by default.
Add `silently` or `quietly` to a natural-language request when the trigger and routine no-report result should stay out of the room timeline.

```
!schedule Every 5 minutes, quietly check the inbox for urgent messages and report only when one arrives
!schedule Daily at 9am, silently check whether the backup failed and report failures
```

A silent schedule does not post its trigger as a visible room message.
MindRoom sends no final message when a successful run returns only whitespace or the standalone marker `NO_REPLY`, matched case-insensitively after trimming.
Findings, failures, and messages explicitly sent by tools remain visible.
Silent runs also omit typing indicators, progress placeholders, stop controls, streaming updates, and tool-only final presentation.

Schedule confirmations and `!list_schedules` label each task as `Silent` or `Visible`.
An edit preserves the current mode when visibility is omitted.
Say `make this schedule silent` or `make this schedule visible` to change the mode through `!edit_schedule`.

Silent delivery controls room presentation, not storage or transport.
The task body still travels through Matrix as a custom timeline event and remains subject to homeserver retention, encrypted transport where enabled, and MindRoom's local durable recovery journal.
Every admitted silent run also writes a versioned JSON receipt to `<agent-workspace>/.mindroom/scheduled_runs/<sha256(source-event-id)>.json`.
The receipt starts with `status: "started"` before generation and is atomically replaced with `status: "completed"`, a `result` of `reported`, `no_report`, or `suppressed`, and the final response text after response hooks.
A receipt left in `started` shows that the run began but did not reach a final response decision.
Version 1 always includes `schema_version`, `source_event_id`, `entity_name`, `agent_name`, `room_id`, `thread_id`, `prompt`, `status`, `result`, `response_text`, `started_at`, and `completed_at`, with `result`, `response_text`, `thread_id`, and `completed_at` set to `null` until applicable.
Timestamps use UTC RFC 3339 strings ending in `Z`.
Replaying the same source event rewrites the same file and preserves a valid original `started_at` value.
Team runs write one receipt to each member agent's workspace, and private agents write inside the matching requester-scoped workspace.
The hidden `.mindroom` directory keeps receipts out of default workspace knowledge indexing.

## Agent and Team Mentions

Include `@agent_name` or `@team_name` in your schedule to have specific responders answer.

The scheduler validates that mentioned agents and teams are available in the room before creating the task.

## History Limits

Scheduled tasks normally use the responder's configured conversation history policy.
Add a context phrase when you want each run to see less of the current room or thread.
Use `with no history`, `without context`, or `context-free` when the scheduled responder should see no prior room or thread messages; the system prompt and fired task message remain available.
Use phrases such as `with only the last 5 messages of context` or `include the last 5 messages` to cap each scheduled run to recent context.

```
!schedule Every hour, @ops check deployment health with no history
!schedule Daily at 9am, @research summarize AI news with only the last 5 messages
```

For edits, omitted fields stay unchanged, including any existing history limit.
Use `restore full history` or `use unlimited history` in an edit to remove a history limit.

```
!edit_schedule task42 keep the same schedule but restore full history
!edit_schedule task42 every weekday at 8am check build status with no history
```

## Timezone

Schedules use the timezone from `config.yaml` (defaults to UTC):

```yaml
timezone: America/Los_Angeles
```

## Limitations

- **Schedule type cannot be changed** — editing a one-time task to be recurring (or vice versa) is not supported.

  Cancel the existing task and create a new one instead.
- **Conditional workflows are polling** — event-like schedules (`If ...`, `When ...`) are converted to recurring cron polls, not real event subscriptions.

## Persistence

Schedules are stored in Matrix room state and persist across restarts.

New schedules use the live runtime to start their in-memory runners immediately.

Edits are state-only Matrix writes.

Running tasks pick up edited state on their next poll instead of relying on caller-supplied cache or restart hooks.

Past one-time tasks within the recovery grace window are queued and started in order after Matrix sync is ready.
Older missed one-time tasks are marked failed instead of executing unexpectedly.

Only the router restores persisted schedules after startup — individual agents do not restore their own.

On shutdown, the router cancels its in-memory scheduled tasks before exiting.
