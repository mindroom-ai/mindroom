## Summary

The main duplication candidates in `src/mindroom/api/schedules.py` are the API-specific list/edit/cancel flows that partially repeat behavior already owned by `src/mindroom/scheduling.py`.
The module also repeats small Matrix room resolution and Matrix client login setup patterns used by other API/runtime code.
Most Pydantic response/request classes are endpoint DTOs and are not meaningful duplication.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ScheduledTaskResponse	class	lines 39-56	related-only	ScheduledTaskResponse ScheduledTaskRecord ScheduledWorkflow task_id room_alias cron_description	src/mindroom/scheduling.py:122, src/mindroom/scheduling.py:144, src/mindroom/scheduling.py:1493
ListSchedulesResponse	class	lines 59-63	not-a-behavior-symbol	ListSchedulesResponse tasks timezone response_model	none
UpdateScheduleRequest	class	lines 66-74	related-only	UpdateScheduleRequest edit_scheduled_task schedule_type execute_at cron_expression message description	src/mindroom/scheduling.py:1413, src/mindroom/scheduling.py:1241, src/mindroom/custom_tools/scheduler.py:65
CancelScheduleResponse	class	lines 77-81	not-a-behavior-symbol	CancelScheduleResponse success message response_model	none
_resolve_room_id	function	lines 89-92	duplicate-found	resolve_room_aliases room alias room_id single room resolution	src/mindroom/matrix/rooms.py:211, src/mindroom/api/matrix_operations.py:102, src/mindroom/bot.py:184
_configured_room_ids	function	lines 95-100	duplicate-found	get_all_configured_rooms resolve_room_aliases configured room ids dict.fromkeys	src/mindroom/bot.py:183, src/mindroom/api/matrix_operations.py:102
_cron_schedule_from_expression	function	lines 103-114	related-only	CronSchedule croniter split five fields to_cron_string cron expression	src/mindroom/scheduling.py:100, src/mindroom/scheduling.py:946, src/mindroom/scheduling.py:1397
_to_response_task	function	lines 117-155	duplicate-found	ScheduledTaskRecord response task cron next_run_at to_natural_language list_scheduled_tasks append task lines	src/mindroom/scheduling.py:1449, src/mindroom/scheduling.py:1485, src/mindroom/scheduling.py:1493
_task_sort_key	function	lines 158-162	duplicate-found	sort scheduled tasks pending next_run_at datetime.max schedule sort key	src/mindroom/scheduling.py:1485
_resolve_schedule_fields	function	lines 165-196	duplicate-found	schedule_type change not supported execute_at cron_expression edit validation save_edited_scheduled_task	src/mindroom/scheduling.py:605, src/mindroom/scheduling.py:1311, src/mindroom/scheduling.py:1413
_resolve_message_fields	function	lines 199-211	related-only	message strip description strip message cannot be empty schedule_task description message	src/mindroom/scheduling.py:1241, src/mindroom/scheduling.py:1404
_build_updated_workflow	function	lines 214-232	duplicate-found	build updated ScheduledWorkflow edit existing workflow preserve created_by thread_id new_thread room_id	src/mindroom/scheduling.py:605, src/mindroom/scheduling.py:1347, src/mindroom/scheduling.py:1413
_get_router_client	async_function	lines 235-244	duplicate-found	create_agent_user login_agent_user RouterAgent runtime_matrix_homeserver Matrix client setup	src/mindroom/api/matrix_operations.py:89, src/mindroom/api/matrix_operations.py:195, src/mindroom/bot.py:877, src/mindroom/bot.py:1105
list_schedules	async_function	lines 248-278	duplicate-found	list schedules get room state parse tasks include cancelled sort scheduled tasks	src/mindroom/scheduling.py:476, src/mindroom/scheduling.py:1449, src/mindroom/commands/handler.py:255, src/mindroom/custom_tools/scheduler.py:92
update_schedule	async_function	lines 282-311	duplicate-found	update schedule get task not found build workflow save edited task close client	src/mindroom/scheduling.py:605, src/mindroom/scheduling.py:1413, src/mindroom/commands/handler.py:281, src/mindroom/custom_tools/scheduler.py:65
cancel_schedule	async_function	lines 315-339	duplicate-found	cancel schedule get task not found cancel_scheduled_task close client	src/mindroom/scheduling.py:1527, src/mindroom/commands/handler.py:263, src/mindroom/custom_tools/scheduler.py:112
```

## Findings

### 1. API list response construction repeats scheduled-task presentation logic

`_to_response_task` and `_task_sort_key` in `src/mindroom/api/schedules.py:117` and `src/mindroom/api/schedules.py:158` transform `ScheduledTaskRecord` into display-facing fields: cron string, cron description, next run time, room alias, and stable sort order.
`list_scheduled_tasks` in `src/mindroom/scheduling.py:1449` performs the same functional work for chat output: it parses records, computes display time for one-time or cron schedules, truncates/presents messages, and sorts by execution time at `src/mindroom/scheduling.py:1485`.

The API has extra fields (`room_alias`, `status`, `created_at`, `next_run_at`) and structured output, while the chat path renders Markdown.
The shared behavior is not the final formatting, but the derived scheduled-task view data and sort semantics.

### 2. API edit validation and workflow reconstruction overlap with scheduling edit behavior

`_resolve_schedule_fields`, `_resolve_message_fields`, and `_build_updated_workflow` in `src/mindroom/api/schedules.py:165`, `src/mindroom/api/schedules.py:199`, and `src/mindroom/api/schedules.py:214` validate schedule edits, preserve immutable workflow fields, reject schedule type changes, and build an edited `ScheduledWorkflow`.
`save_edited_scheduled_task` in `src/mindroom/scheduling.py:605` already enforces pending-only edits and schedule-type stability.
`edit_scheduled_task` in `src/mindroom/scheduling.py:1413` fetches the existing task and reschedules through `schedule_task`, preserving the existing new-thread/thread target at `src/mindroom/scheduling.py:1429`.

The API edit path is intentionally patch-like and deterministic, while the chat/tool edit path reparses a natural-language replacement request.
That difference should be preserved, but the "edited workflow must keep existing ownership/thread metadata and cannot change schedule type" rule is duplicated.

### 3. API list/update/cancel endpoints repeat Matrix scheduled-task access orchestration

`list_schedules`, `update_schedule`, and `cancel_schedule` in `src/mindroom/api/schedules.py:248`, `src/mindroom/api/schedules.py:282`, and `src/mindroom/api/schedules.py:315` all create a Matrix client, fetch one or more scheduled task records, map missing tasks to a not-found response, call a scheduling primitive, and close the client in `finally`.
The same scheduled-task primitives are invoked by command and tool surfaces in `src/mindroom/commands/handler.py:255`, `src/mindroom/commands/handler.py:281`, `src/mindroom/custom_tools/scheduler.py:92`, and `src/mindroom/custom_tools/scheduler.py:112`.
The low-level state access is already centralized in `get_scheduled_tasks_for_room`, `get_scheduled_task`, `save_edited_scheduled_task`, and `cancel_scheduled_task` in `src/mindroom/scheduling.py:476`, `src/mindroom/scheduling.py:490`, `src/mindroom/scheduling.py:605`, and `src/mindroom/scheduling.py:1527`.

The remaining duplication is mostly orchestration and error translation.
API responses need HTTP status codes and structured DTOs, while commands/tools need user-visible strings.

### 4. Room alias resolution and configured-room expansion are repeated API/runtime glue

`_resolve_room_id` and `_configured_room_ids` in `src/mindroom/api/schedules.py:89` and `src/mindroom/api/schedules.py:95` are thin wrappers around `resolve_room_aliases`.
Similar configured-room alias expansion appears in router startup in `src/mindroom/bot.py:183` and API room operations in `src/mindroom/api/matrix_operations.py:102`.

This is real but low-impact duplication.
The schedule API adds ordered de-duplication, which should be retained if generalized.

### 5. Router Matrix client login setup repeats broader entity client setup

`_get_router_client` in `src/mindroom/api/schedules.py:235` creates or retrieves the router user and logs it in.
The same create/login sequence appears in `src/mindroom/api/matrix_operations.py:89`, `src/mindroom/api/matrix_operations.py:195`, and the bot lifecycle split across `src/mindroom/bot.py:877` and `src/mindroom/bot.py:1105`.

The schedule API hard-codes the router identity, while other call sites accept an arbitrary configured entity.
This is duplicated setup behavior, but any helper would need to avoid obscuring lifecycle ownership in `bot.py`.

## Proposed Generalization

1. Add a small scheduling read-model helper in `src/mindroom/scheduling.py`, for example a dataclass such as `ScheduledTaskView`, plus a pure function that derives cron string, cron description, next run time, and sort key from `ScheduledTaskRecord`.
2. Keep API DTO classes in `src/mindroom/api/schedules.py`, but build them from the shared read model.
3. Add a small pure helper in `src/mindroom/scheduling.py` for patch-style edited workflow construction, parameterized by message, description, execute_at, cron_schedule, and existing workflow.
4. Optionally add a `resolve_configured_room_ids(config, runtime_paths) -> list[str]` helper near `mindroom.matrix.rooms.resolve_room_aliases` if another endpoint needs the exact ordered de-duplicated behavior.
5. Consider an API-only async context helper for "login configured Matrix entity and close client" if more API endpoints need ad hoc Matrix clients; do not push this into `bot.py`.

## Risk/tests

The main risk is accidentally changing chat command/tool wording if API read-model extraction touches `list_scheduled_tasks`.
Tests should cover API list sorting, cron `next_run_at`, room alias inclusion, cancelled-task filtering, update rejection for schedule-type changes, update preservation of `created_by`, `thread_id`, and `new_thread`, and cancel not-found behavior.
No refactor is required for DTO-only classes.
