# Scheduling

Schedule agents or teams to do one-time or recurring work, such as reminders, daily reports, or periodic checks, using natural language.
Create schedules with the `!schedule` chat command or let an agent create them with the `scheduler` tool.

<video controls playsinline preload="metadata" aria-label="A scheduled task posts a morning brief" style="width: 100%" poster="https://github.com/user-attachments/assets/4ba7b6f1-e2be-4b2a-a38a-e7a7bb49826e" data-poster-light="https://github.com/user-attachments/assets/4ba7b6f1-e2be-4b2a-a38a-e7a7bb49826e" data-poster-dark="https://github.com/user-attachments/assets/38261697-8f2a-478d-a256-32559e107315">
  <source src="https://github.com/user-attachments/assets/e59e09c8-c8c7-4e90-8e3c-30b8f18b6de2" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/fdbc3860-9301-4d62-907a-857c818d4e73" type="video/mp4">
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

<video controls playsinline preload="metadata" aria-label="A check asked for in conversation becomes a weekly scheduled task" style="width: 100%" poster="https://github.com/user-attachments/assets/5750a5bc-e5cb-4112-86ac-21ed044fa5d1" data-poster-light="https://github.com/user-attachments/assets/5750a5bc-e5cb-4112-86ac-21ed044fa5d1" data-poster-dark="https://github.com/user-attachments/assets/6194d6c7-89e7-4e66-8810-048efc3b3001">
  <source src="https://github.com/user-attachments/assets/2fa5d666-16bd-4864-b27f-fb212609e2a1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/7a68cee9-24d4-43cf-84de-599405fd16d7" type="video/mp4">
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
| `schedule_tool_call(tool_name, arguments_json, execute_at, description)` | One approval-gated call that the requester approves now; see [Pre-Approved Tool Calls](#pre-approved-tool-calls) |
| `run_scheduled_call(task_id, arguments_json=None)` | Runs a pre-approved scheduled call when its task asks; see [Pre-Approved Tool Calls](#pre-approved-tool-calls) |
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

## Pre-Approved Tool Calls

An agent can store one of its own approval-gated tool calls to run later, and the requester approves it while scheduling it, so a gated action such as an external message runs at its time without a second approval.
The agent calls `schedule_tool_call(tool_name, arguments_json, execute_at, description)` from a thread.
`tool_name` must be one of that agent's own tools, `arguments_json` is a JSON object of the call's arguments, and `execute_at` is an ISO 8601 time with a UTC offset, such as `2026-10-04T09:00:00-04:00`.
The `tool_approval` policy must require approval for the call (or the tool must ask for its own confirmation), the scheduler must be an agent rather than a team, and the agent must be able to reply to the requester in that room; otherwise the scheduler refuses it.

```python
schedule_tool_call(
    tool_name="post_slack_message",
    arguments_json='{"channel": "U0123ABCD", "text": "Good morning! The report is ready."}',
    execute_at="2026-10-04T09:00:00-04:00",
    description="Morning report DM",
)
```

MindRoom stores the call and posts an approval card in the thread showing the tool, its arguments, and the send time; only the requester can approve or deny it, and it expires at the send time.
The requester approves either the stored arguments or, when the card offers it, any arguments to the same tool.
Approving any arguments lets the agent decide the arguments for that tool when the task runs, following the task's description, so choose it only when the content must be decided later.
The card offers it only when requesters approve their own calls; operators can stop offering it with [`tool_approval.scheduled_any_arguments`](https://docs.mindroom.chat/tool-approval/), and generic MCP `*_call_tool` calls and tools that ask for their own confirmation are always approved exactly.

When the task runs, it asks the agent to call `run_scheduled_call(task_id)`, which runs the stored call once with the stored arguments, without a new card, and posts an approved receipt naming who approved it, when, and for which scope.
With an any-arguments approval the agent may pass `arguments_json` to `run_scheduled_call` to replace the arguments for the same tool.
The approval can be used once, by that agent for that requester in that thread, from when the task runs until 15 minutes after its scheduled time.
If the card was not approved in time, `run_scheduled_call` runs nothing and the agent can call the tool directly, which asks for approval as usual.
If the requester denies the card, the task does not run, and cancelling the task withdraws the approval.
A pre-approved task cannot be edited; cancel it and schedule the call again.
The arguments are not posted in the task or its trigger message; MindRoom keeps them with the approval, and the card shows them with secrets hidden.
Recurring schedules cannot pre-approve calls.

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

MindRoom cancels a schedule automatically once neither its creator nor any of the creator's [bridge aliases](https://docs.mindroom.chat/authorization/#bridge-aliases) is joined to the room.
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
For `schedule:fired` hooks that run more than once for the same occurrence, see [Hooks](https://docs.mindroom.chat/hooks/#event-notes).

## Built-in Automations

Built-in automations are checks MindRoom runs on a cron schedule; when one passes, the agent posts the automation's prompt in its room and answers it with a normal, visible run.
The check runs in code, so a schedule that finds nothing to do costs no model call and posts nothing.
Enable them per agent in `config.yaml`, or under `defaults` for every eligible agent:

```yaml
defaults:
  automations: []                # inherited by agents that omit the field

agents:
  mind:
    memory_backend: file
    automations:
      - prompt_curation          # the built-in with its defaults
      # or: {name: prompt_curation, cron: "0 4 * * *", room: personal, trigger_tokens: 30000}
```

- An entry is a built-in name, or a mapping with `name` plus overrides; unknown names and fields fail config load.
- `cron` is a five-field expression in the configured [timezone](#timezone).
- `room` is a room alias or ID; it defaults to the agent's first configured room, and each prompt starts a new thread.
- `agents.<name>.automations: []` turns inherited defaults off for one agent.
- Automations run unattended, so requester-private agents cannot list them and do not inherit defaults.
- Edits apply on config reload without restarting the agent.
- The agent posts the prompt in its own name and mentions itself, so it answers even in a room with other agents.
- When the response to that prompt is final, or after an hour without one, the automation's verify step runs and posts a one-line notice in the prompt's thread; a run paused for tool approval longer than that hour is verified as it stood, and later edits are not checked.
- An automation does not post again while its previous prompt awaits verify.
- Nothing is persisted: the schedule is the cooldown, and a restart skips the occurrence it missed.

For conditions that need your own code, gate an ordinary recurring schedule with a [`schedule:fired` hook](https://docs.mindroom.chat/hooks/#event-notes), which can suppress a fire or rewrite its message.

### `prompt_curation`

Agents append to `MEMORY.md` and their `context_files` far more often than they condense them, and every model call re-sends those files.
`prompt_curation` checks their total size daily and, once it passes the trigger, asks the agent for a gradual cut.

1. The check measures `MEMORY.md` plus the agent's `context_files`, except `protected_files`, with the estimate behind `static_prompt_tokens` (characters / 4).
2. Above `trigger_tokens`, it snapshots those files and posts a prompt with exact numbers, for example "bring them to at most 46876 tokens in total, but not below 44272", which asks for a 10 to 15% cut, or only the gap to 90% of the trigger when that is smaller.
3. The prompt asks the agent to commit the files to git first, keep each fact once in the file that owns it, move detail and history verbatim into `memory/` topic files with one-line pointers, and never invent facts.
4. Verify writes the snapshot back over every changed file, and posts why, when any of these holds:
    - a file can no longer be read safely, for example because it became a link or grew past 1 MiB;
    - a file shrank by more than `max_file_shrink`;
    - the files total less than the floor, or did not shrink;
    - a protected file changed, or a file is no longer valid UTF-8;
    - total memory content (the files plus `memory/**`) dropped by more than `max_content_loss` of the files' size, which means detail was deleted instead of moved.

A restore also rewrites any `memory/` topic file that lost archived text during the run, while files and lines the run added are kept, so moved detail is never lost.
A reply that rewrites a curated file during the run can be overwritten when verify restores the snapshot.

| Field | Default | Description |
|---|---|---|
| `cron` | `0 4 * * *` | When to check |
| `room` | first configured room | Where to post the prompt |
| `trigger_tokens` | `50000` (min 1) | Post the prompt once the files exceed this many estimated tokens |
| `min_reduction` | `0.10` | Cut the prompt asks for, or only the gap to 90% of the trigger when that is smaller |
| `max_reduction` | `0.15` | Largest cut one run may make |
| `max_file_shrink` | `0.25` | Largest shrink of any single file in one run |
| `max_content_loss` | `0.05` | Largest net drop in total memory content, as a fraction of the files' size |
| `protected_files` | `[]` | Workspace-relative paths the run must leave unchanged |

`prompt_curation` needs `memory_backend: file`, because moved detail must stay searchable, and the prompt template is overridable as `PROMPT_CURATION_PROMPT_TEMPLATE`.
