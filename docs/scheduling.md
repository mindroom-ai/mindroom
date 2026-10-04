---
icon: lucide/calendar
---

# Scheduling

Schedule agents or teams to perform tasks at specific times or intervals using natural language.

By default, tasks run in the same scope where they were created: the room timeline for room-level schedules, or the current thread for threaded schedules.
The `schedule()` tool accepts `new_thread=True` to start a fresh thread per fire: each fire posts a room-level root and the responding agent answers in a new thread under it with a fresh session.
A fire's text never runs as a chat command, even when it starts with `!`; it reaches the agents as an ordinary scheduled message.

Schedules with a recorded creator are automatically canceled once live membership checks confirm that neither the creator nor any permitted human alias is joined to the room.
Configured bot accounts and managed identities do not count as human aliases.
The scheduler checks membership every 30 seconds while waiting and again before execution, including after a restart.
A confirmed join for any equivalent human identity permits execution; otherwise, uncertain membership makes execution wait for a successful lookup.
Membership checks use the runtime's current applied configuration, so revoking a human alias takes effect on the next check without recreating the runner.

<video controls playsinline preload="metadata" aria-label="A scheduled task posts a morning brief" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/cfbbac8e-6942-4bac-a920-ac3946951174#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/a3bacdb7-7d59-42d2-8d0f-33f391c412b2#t=0.1" type="video/mp4">
</video>

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

### `!schedule`

Schedule a one-time or recurring task using natural language.

Tasks run in the same scope where they were created: the room timeline for room-level schedules, or the current thread for threaded schedules.

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

For predictable behavior, include an explicit polling cadence.

```
!schedule Every 5 minutes, check if I got an email about "urgent"; if so, @phone_agent call me
!schedule Every 10 minutes, check whether Bitcoin dropped below $40k; if so, @crypto_agent notify me
```

Include `@agent_name` or `@team_name` in your schedule to target specific responders.

The scheduler validates that mentioned agents and teams are available in the room before creating the task.

Add `with no history`, `without context`, or `context-free` when each scheduled run should see no prior conversation messages.

Add a phrase such as `with only the last 5 messages of context` to cap each scheduled run to recent context.

```
!schedule Every hour, @ops check deployment health with no history
!schedule Daily at 9am, @research summarize AI news with only the last 5 messages
```

Add `silently` or `quietly` when a scheduled check should post only findings, failures, or messages explicitly sent by tools.

```
!schedule Every 5 minutes, quietly check the inbox for urgent messages and report only when one arrives
```

Silent schedules hide their trigger and omit a successful final response that is empty or contains only `NO_REPLY`.
Schedules remain visible by default.

Schedules use the timezone from `config.yaml` (defaults to UTC).

### Edit a Schedule

```
!edit_schedule <task-id> <new-task-description>
```

Edits an existing scheduled task by ID.

The task description is re-parsed to update timing and content.

### `!edit_schedule`

Replace an existing scheduled task with new timing and content.
Omitted fields stay unchanged, including any existing history limit or silent-delivery mode.
Use `restore full history` or `use unlimited history` to remove a history limit.
Use `make this schedule silent` or `make this schedule visible` to change its delivery mode.

```
!edit_schedule <task-id> <new-task-description>
```

The task description is re-parsed to update timing and content.

Schedule type cannot be changed (one-time to recurring or vice versa) -- cancel and recreate instead.

```
!edit_schedule task42 keep the same schedule but restore full history
!edit_schedule task42 every weekday at 8am check build status with no history
!edit_schedule task42 keep the same schedule but make it silent
```

**Aliases:** `!editschedule`, `!edit-schedule`

### List and Cancel Schedules

```
!list_schedules                  # Show pending tasks
!cancel_schedule <task-id>       # Cancel specific task
!cancel_schedule all             # Cancel all tasks in room
```

Aliases: `!listschedules`, `!list-schedules`, `!list_schedule`, `!listschedule`, `!list-schedule`, `!inspect_schedules`, `!inspectschedules`, `!inspect-schedules`, `!inspect_schedule`, `!inspectschedule`, `!inspect-schedule`, `!cancelschedule`, `!cancel-schedule`, `!editschedule`, `!edit-schedule`

Use `!help schedule` for detailed inline help on scheduling commands.

Schedules are room-managed resources rather than creator-private resources.
Thread context filters schedule listings for usability, but it is not an authorization boundary.
An authorized participant in the room can edit or cancel any room schedule by task ID, and `!cancel_schedule all` applies to the whole room.

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

## [`scheduler`]

<video controls playsinline preload="metadata" aria-label="A check asked for in conversation becomes a weekly scheduled task" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/41986747-dfb3-41cd-b3c6-b60f8eabdab8#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/d8729fbc-7377-4b80-b3c2-c39394eb19a4#t=0.1" type="video/mp4">
</video>

`scheduler` is MindRoom's built-in task scheduler for future messages, reminders, and recurring agent or team work.

### What It Does

`scheduler` exposes `schedule()`, `edit_schedule()`, `list_schedules()`, and `cancel_schedule()`.
It reuses the same backend as `!schedule`, `!edit_schedule`, `!list_schedules`, and `!cancel_schedule`.
Pass `new_thread=False` to post back into the current room or thread scope, or `new_thread=True` to schedule a future room-level root message.
Pass `model="cheap"` to run a task with a model alias configured under `models:`, including all team members.
The choice applies only to scheduled runs and takes precedence over room and thread model settings.
The optional `history_limit` argument caps how many recent messages the scheduled responder sees each time the task fires.
Use `history_limit=0` for no prior conversation context, or a positive integer to keep that many recent messages.
Pass `silent=True` to hide the scheduled trigger and omit successful final responses that are empty or contain only `NO_REPLY`.
Findings, failures, and messages explicitly sent by tools remain visible for silent schedules.
Scheduled tasks are stored in Matrix room state and persist across restarts.
The scheduler validates mentioned agents and teams against the current room or thread before it saves a task.
If no Matrix room context is available, the tool returns an unavailable error instead of creating a task.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  assistant:
    tools:
      - scheduler
```

```python
schedule("tomorrow at 9am @ops check the deployment", new_thread=False)
schedule("every weekday at 8am post the on-call handoff summary", new_thread=True)
schedule("every hour @ops check deployment health", new_thread=False, history_limit=0, model="cheap")
schedule("every 5 minutes check the inbox for urgent mail", new_thread=False, history_limit=0, silent=True)
list_schedules()
edit_schedule("a1b2c3d4", "tomorrow at 10am @ops check the deployment", history_limit=5, silent=False)
cancel_schedule("a1b2c3d4")
```

### Notes

- `scheduler` needs no dashboard setup and is included in `defaults.tools` by default unless you explicitly disable that inheritance.
- Editing preserves the original schedule type, so switching between one-time and recurring schedules requires cancelling the old task and creating a new one.
- Editing preserves the chosen model when `model` is omitted; pass `model=""` to restore normal model selection.
- Editing preserves an existing history limit unless the edit request or explicit tool argument changes it.
- Editing preserves the current silent-delivery mode unless the natural-language request or `silent` argument changes it.
- Use natural-language edit phrases such as `restore full history` to remove a history limit through chat, or pass `history_limit` through the tool when the agent should set a concrete cap.
- A silent schedule with `new_thread=True` posts any finding or failure as a room-level root because its hidden trigger cannot serve as a visible thread root.
- Silent delivery controls room presentation only; the task body still travels through Matrix and remains subject to homeserver retention and MindRoom's durable recovery journal.
- Conditional phrases such as `if` and `when` are converted into recurring polling schedules rather than real event subscriptions.

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

## Model Selection

Use the `model` argument on `schedule()` to choose a configured model alias for each run, for example a cheaper model for simple recurring checks.
The alias must exist under `models:` in `config.yaml`.

```python
schedule("every hour @ops check deployment health", new_thread=False, history_limit=0, model="cheap")
edit_schedule("task42", "keep the same schedule and task", model="cheap")
edit_schedule("task42", "keep the same schedule and task", model="")
```

The override applies only to the scheduled response, including both the coordinator and members of a team.
It takes precedence over room and thread model settings without changing them for later messages.
Omitting `model` on creation uses normal model selection; omitting it on an edit preserves the saved choice.
Pass an empty string on an edit to remove the override and restore normal model selection.
Schedule confirmations and listings show the selected alias.
The option controls task execution; parsing the scheduling request still uses the default model.

## Timezone

The timezone in `config.yaml` controls natural-language time interpretation and displayed timestamps (defaults to UTC):

```yaml
timezone: America/Los_Angeles
```

Recurring schedules are stored and evaluated as UTC cron expressions.
Their local execution time can shift when the timezone’s UTC offset changes, including daylight-saving transitions.
Edit or recreate a recurring schedule after an offset change if it must keep the same local clock time.

## Limitations

- **Schedule type cannot be changed** — editing a one-time task to be recurring (or vice versa) is not supported.

  Cancel the existing task and create a new one instead.
- **Conditional workflows are polling** — event-like schedules (`If ...`, `When ...`) are converted to recurring cron polls, not real event subscriptions.

## Persistence

Schedules are stored in Matrix room state and persist across restarts.

MindRoom only lists, edits, cancels, restores, or runs schedule state whose Matrix sender is one of its bot accounts: the router, an agent, or a team, including agents and teams since removed from the configuration.
Schedule state written by any other account, including a room admin or the internal `mindroom_user`, is ignored and logged, because a schedule's recorded creator is the requester its triggers run as.
Ignored state is never canceled or overwritten automatically.
The homeserver must support `GET /_matrix/client/v3/rooms/{roomId}/state/{eventType}/{stateKey}?format=event` (Matrix spec v1.16); Synapse, Tuwunel, and Dendrite do.
Each read of one task compares the room's create event with and without `format=event`, so on a homeserver that ignores it, task reads fail instead of trusting a sender or an older bot-written version of the task that state content names.

New schedules use the live runtime to start their in-memory runners immediately.

Edits are state-only Matrix writes.

Running tasks pick up edited state on their next poll instead of relying on caller-supplied cache or restart hooks.

Past one-time tasks within the recovery grace window are queued and started in order after Matrix sync is ready.
Older missed one-time tasks are marked failed instead of executing unexpectedly.

Only the router restores persisted schedules after startup — individual agents do not restore their own.

On shutdown, the router cancels its in-memory scheduled tasks before exiting.

### Scheduled Task Restoration

When the router joins a room, it restores any previously scheduled tasks and pending configuration changes to ensure they persist across restarts.

### Recurring task recovery

Recurring timers save their next due time under the runtime storage directory in `tracking/recurring_schedules/`.
After a restart, they wait for Matrix sync readiness and run the latest missed occurrence if it falls within the catch-up window.
The same window applies when a live timer wakes late.
The default window is one hour:

```yaml
scheduler_catch_up_grace_seconds: 3600
```

Set this to `0` to disable catch-up for unattempted occurrences.
Ordinary future timers still run when catch-up is disabled.
Multiple missed occurrences coalesce into one run; older occurrences outside the window are skipped.
The checkpoint retains the most recent skipped time and reason, and structured logs report skips and coalescing.
Normal future occurrences keep their original cron cadence.

Each trigger has a stable Matrix transaction ID derived from its schedule identity and intended execution time.
Before sending, MindRoom durably freezes its content and sending device.
Temporary checkpoint failures keep the timer alive and retry without repeating an acknowledged delivery.
A retry reuses that content and transaction ID, so a restart after Matrix accepts a trigger does not create another trigger on the same device.
Already-attempted deliveries remain pending until acknowledged, independently of the catch-up window.
Already-triggered agent work continues through normal event recovery.
Invalid content discovered before delivery is frozen fails the occurrence and advances the timer.
If the Matrix login device changes while delivery is unresolved, automatic resending is held and logs report that reconciliation is needed.

The checkpoint directory must survive restarts.
On first adoption without a checkpoint, or after a workflow edit, MindRoom establishes a future cursor without replaying unknown past occurrences.
Cancelling a schedule still prevents its pending timer from firing.
Cancellation does not erase Matrix schedule history or local checkpoints.

The trigger transaction does not make arbitrary `schedule:fired` hook side effects exactly-once.
Hooks can replay after a crash or preparation failure before trigger content is durably frozen.
Recurring hooks receive a `correlation_id` that is stable for that occurrence and changes for the next one; use it with an idempotent destination when performing side effects.
