---
icon: lucide/calendar
---

# Scheduling

Schedule agents or teams to do one-time or recurring work, such as reminders, daily reports, or periodic checks, using natural language.
Create schedules with the `!schedule` chat command or let an agent create them with the `scheduler` tool.

<video controls playsinline preload="metadata" aria-label="A scheduled task posts a morning brief" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/cfbbac8e-6942-4bac-a920-ac3946951174#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/a3bacdb7-7d59-42d2-8d0f-33f391c412b2#t=0.1" type="video/mp4">
</video>

## Commands

Use `!help schedule` for inline help on the scheduling commands.

### `!schedule`

Schedule a one-time or recurring task.

```
!schedule <natural-language-request>
```

```
!schedule in 5 minutes Check the deployment
!schedule tomorrow at 3pm Send the weekly report
!schedule Every hour, @shell check server status
!schedule Daily at 9am, @finance market report
!schedule Weekly on Friday, @analyst prepare weekly summary
```

Include `@agent_name` or `@team_name` to choose who answers each run.
MindRoom checks that mentioned agents and teams are available in the room before it creates the task.

A task runs where it was created: in the room timeline for a room-level schedule, or in the current thread for a threaded schedule.
The task text never runs as a chat command, even when it starts with `!`; agents receive it as an ordinary message.

Conditional requests such as `if` or `when` become recurring polling schedules, not event subscriptions.
The condition is part of the task message, so the responder checks it on each run.
Include an explicit polling cadence for predictable behavior.

```
!schedule Every 5 minutes, check if I got an email about "urgent"; if so, @phone_agent call me
!schedule Every 10 minutes, check whether Bitcoin dropped below $40k; if so, @crypto_agent notify me
```

Add phrases to the request to change how each run behaves:

| Phrase | Effect |
| --- | --- |
| `with no history`, `without context`, `context-free` | Each run sees no prior messages; see [History limits](#history-limits) |
| `with only the last 5 messages of context` | Each run sees only recent messages |
| `silently`, `quietly` | Hide the trigger and post only findings; see [Silent delivery](#silent-delivery) |

### `!edit_schedule`

Replace an existing task's timing and content.

```
!edit_schedule <task-id> <new-task-description>
```

MindRoom re-parses the description to update timing and content.
Omitted settings stay unchanged, including the history limit, silent mode, and model.
A task cannot switch between one-time and recurring; cancel it and create a new one instead.

```
!edit_schedule task42 keep the same schedule but restore full history
!edit_schedule task42 every weekday at 8am check build status with no history
!edit_schedule task42 keep the same schedule but make it silent
```

**Aliases:** `!editschedule`, `!edit-schedule`

### `!list_schedules`

List pending tasks in the current room, or in the current thread when used inside one.
Each entry shows its task ID, timing, mode (`Silent` or `Visible`), and selected model if any.

```
!list_schedules
```

**Aliases:** `!listschedules`, `!list-schedules`, `!list_schedule`, `!listschedule`, `!list-schedule`, `!inspect_schedules`, `!inspectschedules`, `!inspect-schedules`, `!inspect_schedule`, `!inspectschedule`, `!inspect-schedule`

### `!cancel_schedule`

Cancel one task, or every task in the room.

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

The `scheduler` tool lets an agent create, edit, list, and cancel the same tasks as the chat commands.
It is included in `defaults.tools` by default, needs no setup, and has no tool-specific configuration fields.
It only works from a live room or thread; elsewhere it returns `Scheduler tool is unavailable in this context.`

```yaml
agents:
  assistant:
    tools:
      - scheduler
```

| Function | Arguments |
| --- | --- |
| `schedule(request, new_thread, history_limit=None, silent=None, model=None)` | `new_thread` is required |
| `edit_schedule(task_id, request, history_limit=None, silent=None, model=None)` | Omitted arguments keep the task's current settings |
| `list_schedules()` | |
| `cancel_schedule(task_id)` | |

- `new_thread=False` posts each run in the current room or thread.
  `new_thread=True` posts each run as a new room-level message, and the responder answers in a new thread under it with a fresh session.
- `history_limit` caps how many recent messages each run sees: `0` for none, a positive integer for that many, unset for full history.
- `silent=True` turns on [silent delivery](#silent-delivery); unset infers the mode from the request text.
- `model` picks a model alias for each run; see [Model selection](#model-selection).

```python
schedule("tomorrow at 9am @ops check the deployment", new_thread=False)
schedule("every weekday at 8am post the on-call handoff summary", new_thread=True)
schedule("every hour @ops check deployment health", new_thread=False, history_limit=0, model="cheap")
schedule("every 5 minutes check the inbox for urgent mail", new_thread=False, history_limit=0, silent=True)
list_schedules()
edit_schedule("a1b2c3d4", "tomorrow at 10am @ops check the deployment", history_limit=5, silent=False)
cancel_schedule("a1b2c3d4")
```

## History Limits

Scheduled runs normally use the responder's configured conversation history.
Use `with no history`, `without context`, or `context-free` when each run should see no prior room or thread messages; the system prompt and the task message remain available.
Use phrases such as `with only the last 5 messages of context` or `include the last 5 messages` to cap each run to recent context.
In an edit, say `restore full history` or `use unlimited history` to remove a limit.
Agents set the same limit with the tool's `history_limit` argument.

## Silent Delivery

Schedules are visible by default.
Add `silently` or `quietly` to a request when the trigger and routine no-report results should stay out of the room timeline.

```
!schedule Every 5 minutes, quietly check the inbox for urgent messages and report only when one arrives
!schedule Daily at 9am, silently check whether the backup failed and report failures
```

A silent schedule does not show its trigger in the room.
A successful run that returns nothing, whitespace, or only `NO_REPLY` (any case) posts no message.
Findings, failures, and messages that tools send explicitly remain visible.
Silent runs also show no typing indicator, progress placeholder, stop control, or streaming updates.
With `new_thread=True`, a silent schedule posts any finding or failure as a new room-level message.
Say `make this schedule silent` or `make this schedule visible` in `!edit_schedule` to change the mode.

Silent delivery only hides messages from the room; the task text still travels through Matrix and is subject to homeserver retention.

Each silent run writes a JSON receipt to `<agent-workspace>/.mindroom/scheduled_runs/`, so you can check what a silent run did.
A receipt records the prompt, room, thread, `status` (`started` or `completed`), `result` (`reported`, `no_report`, or `suppressed`), the final response text, and UTC start and completion times.
A receipt still in `started` means the run began but never reached a final response.
Team runs write a receipt to each member agent's workspace, and private agents write to the requester's workspace.
Workspace knowledge indexing skips these receipts.

## Model Selection

Pass `model` to `schedule()` to run each scheduled response with a model alias from `models:` in `config.yaml`, for example a cheaper model for simple recurring checks.

```python
schedule("every hour @ops check deployment health", new_thread=False, history_limit=0, model="cheap")
edit_schedule("task42", "keep the same schedule and task", model="cheap")
edit_schedule("task42", "keep the same schedule and task", model="")
```

The model applies to the whole scheduled response, including a team's coordinator and members.
It takes precedence over room and thread model settings without changing them for later messages.
Omitting `model` on creation uses normal model selection; omitting it on an edit keeps the saved choice, and an empty string restores normal model selection.
Parsing the scheduling request itself always uses the default model.

## Timezone

The top-level `timezone` setting (default `UTC`) controls how MindRoom interprets times in requests and displays scheduled times.

```yaml
timezone: America/Los_Angeles
```

Recurring schedules run on UTC cron times, so their local clock time shifts when the timezone's UTC offset changes, such as at daylight-saving transitions.
Edit or recreate a recurring schedule after an offset change if it must keep the same local time.

## Permissions and Automatic Cancellation

Schedules belong to the room, not to their creator.
Any authorized participant in the room can edit or cancel any of its schedules by task ID, and listing inside a thread only filters the view.
Each run acts as the schedule's creator, as if the creator had sent the message, so it uses that person's access, per-user credentials, and private-agent workspace.
Editing a schedule with `!edit_schedule` or the `scheduler` tool makes the editor its new creator.

MindRoom cancels a schedule automatically once neither its creator nor any of the creator's [bridge aliases](authorization.md#bridge-aliases) is joined to the room.
Bot accounts and managed identities do not count.
A run waits rather than proceeding when membership cannot be confirmed.

## Persistence and Missed Runs

Schedules are stored in Matrix room state and survive restarts.
MindRoom only acts on schedule state written by its own router, agent, or team accounts; schedule state written by any other account, including a room admin, is ignored and logged.
The homeserver must support `GET /_matrix/client/v3/rooms/{roomId}/state/{eventType}/{stateKey}?format=event` (Matrix spec v1.16), as Synapse, Tuwunel, and Dendrite do; otherwise reading tasks fails.

### Scheduled Task Restoration

The router restores a room's schedules and pending configuration changes when it joins the room after startup.
A one-time task missed by less than 24 hours runs once Matrix sync is ready; an older one is marked failed instead.

### Recurring task recovery

After a restart or a late wake-up, a recurring task runs its latest missed occurrence if it is within the catch-up window, and multiple missed occurrences run once.
Occurrences outside the window are skipped and logged, and later runs keep the normal cadence.

```yaml
scheduler_catch_up_grace_seconds: 3600  # default; 0 disables catch-up
```

Recurring progress is kept in `tracking/recurring_schedules/` under the storage directory, which must persist across restarts.
Without it, or after a schedule edit, MindRoom resumes from the next future occurrence without replaying past ones.
Restarts do not post the same trigger twice.
For `schedule:fired` hooks that run more than once for the same occurrence, see [Hooks](hooks.md#event-notes).
