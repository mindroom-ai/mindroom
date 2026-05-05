## Summary

Top duplication candidates for `src/mindroom/scheduling.py`:

1. Scheduled task Matrix state parsing is implemented in `_parse_scheduled_task_record` and partially repeated in `restore_scheduled_tasks`, with the restore path bypassing the canonical parser and fallback handling.
2. Cron schedule conversion, display, and next-run calculation are split between `scheduling.py` and `src/mindroom/api/schedules.py`, especially `_cron_schedule_from_expression`, `_to_response_task`, and `_task_sort_key`.
3. Scheduled workflow delivery repeats a common Matrix pattern also present in delivery modules: resolve latest thread event, build content, send with `send_message_result`, and notify the conversation cache.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_AgentValidationResult	class	lines 77-82	none-found	AgentValidationResult valid_agents invalid_agents all_valid	src/mindroom/scheduling.py:1157; src/mindroom/authorization.py:257; src/mindroom/commands/handler.py:135
_raise_scheduled_workflow_send_error	function	lines 85-88	related-only	Failed to send scheduled workflow message send_message_result None	src/mindroom/delivery_gateway.py:564; src/mindroom/thread_summary.py:424; src/mindroom/hooks/sender.py:90
set_scheduling_hook_registry	function	lines 91-94	related-only	hook registry setter active hook registry	src/mindroom/hooks/context.py:498; src/mindroom/hook references via scheduling.py:846
CronSchedule	class	lines 100-119	duplicate-found	CronSchedule cron_expression cron fields split to_cron_string	src/mindroom/api/schedules.py:103; src/mindroom/api/schedules.py:123; src/mindroom/scheduling.py:946
CronSchedule.to_cron_string	method	lines 109-111	duplicate-found	cron expression join minute hour day month weekday	src/mindroom/api/schedules.py:103; src/mindroom/api/schedules.py:123; src/mindroom/scheduling.py:1398
CronSchedule.to_natural_language	method	lines 113-119	related-only	get_description cron natural language cron_description	src/mindroom/api/schedules.py:124; src/mindroom/scheduling.py:1397; src/mindroom/scheduling.py:1500
ScheduledWorkflow	class	lines 122-134	duplicate-found	ScheduledWorkflow schedule_type execute_at cron_schedule message description	src/mindroom/api/schedules.py:218; src/mindroom/api/schedules.py:222; src/mindroom/scheduling.py:1641
_WorkflowParseError	class	lines 137-141	none-found	WorkflowParseError error suggestion schedule parse error	none
ScheduledTaskRecord	class	lines 145-152	related-only	ScheduledTaskRecord task_id room_id status workflow response	src/mindroom/api/schedules.py:115; src/mindroom/scheduling.py:450
SchedulingRuntime	class	lines 156-165	none-found	SchedulingRuntime client config runtime_paths room conversation_cache event_cache	none
_DeferredOverdueTaskStart	class	lines 169-173	none-found	DeferredOverdueTaskStart overdue task queue task_id workflow	none
_parse_datetime	function	lines 176-185	duplicate-found	parse datetime fromisoformat Z timezone invalid returns None	src/mindroom/thread_tags.py:107; src/mindroom/approval_manager.py:102; src/mindroom/knowledge/utils.py:138; src/mindroom/approval_events.py:150
_parse_scheduled_task_record	function	lines 188-233	duplicate-found	parse scheduled task Matrix state workflow status created_at	src/mindroom/scheduling.py:450; src/mindroom/scheduling.py:1629; src/mindroom/scheduling.py:1641
_cancelled_task_content	function	lines 236-257	related-only	cancel Matrix state preserve workflow created_at updated_at	src/mindroom/scheduling.py:1550; src/mindroom/scheduling.py:1587; src/mindroom/thread_tags.py:685
_is_polling_cron_schedule	function	lines 260-271	none-found	polling cron interval minute hour day month weekday conditional	none
_is_polling_cron_schedule.<locals>.is_interval	nested_function	lines 268-269	none-found	cron interval field startswith */ wildcard	none
_validate_conditional_workflow	function	lines 274-294	none-found	conditional workflow polling cron validation	none
_start_scheduled_task	function	lines 297-343	related-only	create_task running_tasks duplicate start done cleanup	src/mindroom/orchestrator.py:445; src/mindroom/mcp/manager.py:395; src/mindroom/bot.py:1262
_queue_deferred_overdue_task	function	lines 346-359	related-only	deferred overdue queue ids running task duplicate	src/mindroom/bot.py:1262; src/mindroom/coalescing.py:213
drain_deferred_overdue_tasks	async_function	lines 362-400	related-only	drain queued tasks sleep between start failures	src/mindroom/bot.py:1262; src/mindroom/knowledge/watch.py:149
clear_deferred_overdue_tasks	function	lines 403-408	none-found	clear deferred overdue task queue ids	none
has_deferred_overdue_tasks	function	lines 411-413	none-found	bool deferred overdue tasks queue	none
_cancel_running_task	function	lines 416-420	related-only	cancel task by id remove tracking	src/mindroom/orchestrator.py:1920; src/mindroom/background_tasks.py:91; src/mindroom/mcp/manager.py:427
cancel_all_running_scheduled_tasks	async_function	lines 423-435	related-only	cancel all tasks gather return_exceptions	src/mindroom/background_tasks.py:91; src/mindroom/api/main.py:384; src/mindroom/orchestrator.py:1993
_workflows_differ	function	lines 438-440	none-found	model_dump json compare workflows	none
_cleanup_task_if_current	function	lines 443-447	related-only	current_task remove if owner task slot	src/mindroom/orchestrator.py:480; src/mindroom/matrix/cache/write_coordinator.py:501; src/mindroom/coalescing.py:746
_parse_task_records_from_state	function	lines 450-473	duplicate-found	parse room state events scheduled task records event type status	src/mindroom/scheduling.py:1457; src/mindroom/scheduling.py:1566; src/mindroom/scheduling.py:1623
get_scheduled_tasks_for_room	async_function	lines 476-487	related-only	room_get_state parse scheduled tasks for room	src/mindroom/api/schedules.py:247; src/mindroom/scheduling.py:1457; src/mindroom/scheduling.py:1566
get_scheduled_task	async_function	lines 490-505	related-only	room_get_state_event scheduled task parse	src/mindroom/api/schedules.py:281; src/mindroom/scheduling.py:1539; src/mindroom/thread_tags.py:685
_get_pending_task_record	async_function	lines 508-520	related-only	get scheduled task pending status guard	src/mindroom/scheduling.py:935; src/mindroom/scheduling.py:959; src/mindroom/scheduling.py:1048
_serialize_scheduled_task_created_at	function	lines 523-529	related-only	created_at isoformat now UTC fallback	src/mindroom/thread_tags.py:107; src/mindroom/commands/config_confirmation.py:60; src/mindroom/knowledge/utils.py:138
_persist_scheduled_task_state	async_function	lines 532-552	related-only	room_put_state scheduled task workflow status timestamps	src/mindroom/thread_tags.py:496; src/mindroom/commands/config_confirmation.py:154; src/mindroom/hooks/state.py:62
_save_pending_scheduled_task	async_function	lines 555-586	none-found	save pending scheduled task cancel persist start	none
_save_one_time_task_status	async_function	lines 589-602	none-found	save one time task terminal status	none
save_edited_scheduled_task	async_function	lines 605-635	duplicate-found	edit scheduled task status pending schedule_type invariant persist	src/mindroom/api/schedules.py:167; src/mindroom/api/schedules.py:218; src/mindroom/scheduling.py:1413
_parse_workflow_schedule	async_function	lines 638-727	none-found	AI workflow parser ScheduledWorkflow prompt schedule request	none
_build_workflow_message_content	async_function	lines 730-765	duplicate-found	build threaded Matrix message with mentions latest thread event	src/mindroom/delivery_gateway.py:531; src/mindroom/hooks/sender.py:81; src/mindroom/custom_tools/subagents.py:265
_build_scheduled_failure_content	async_function	lines 768-787	duplicate-found	build failure message content latest thread event	src/mindroom/thread_summary.py:409; src/mindroom/delivery_gateway.py:923; src/mindroom/scheduling.py:1020
_notify_scheduled_workflow_failure	async_function	lines 790-817	duplicate-found	send failure content notify outbound cache	src/mindroom/thread_summary.py:424; src/mindroom/delivery_gateway.py:934; src/mindroom/hooks/sender.py:90
_execute_scheduled_workflow	async_function	lines 820-910	duplicate-found	hook emit build content original sender source kind send notify failure	src/mindroom/hooks/sender.py:73; src/mindroom/custom_tools/subagents.py:274; src/mindroom/delivery_gateway.py:564
_run_cron_task	async_function	lines 913-1032	duplicate-found	cron next run pending state refresh execute failure cleanup	src/mindroom/api/schedules.py:131; src/mindroom/scheduling.py:1035; src/mindroom/knowledge/refresh_scheduler.py:117
_run_once_task	async_function	lines 1035-1154	duplicate-found	wait until execute_at poll pending execute status failure cleanup	src/mindroom/scheduling.py:913; src/mindroom/knowledge/refresh_runner.py:243; src/mindroom/tools/shell.py:603
_validate_agent_mentions	async_function	lines 1157-1192	related-only	validate mentioned agents allowed agents invalid suggestions	src/mindroom/thread_utils.py:89; src/mindroom/commands/handler.py:135; src/mindroom/authorization.py:257
_format_scheduled_time	function	lines 1195-1216	related-only	timezone datetime formatting humanize natural time	src/mindroom/agents.py:155; src/mindroom/response_runner.py:214; src/mindroom/memory/_file_backend.py:572
_extract_mentioned_agents_from_text	function	lines 1219-1238	duplicate-found	parse mentions into unique MatrixID agents	src/mindroom/thread_utils.py:120; src/mindroom/matrix/mentions.py:43; src/mindroom/commands/handler.py:135
schedule_task	async_function	lines 1241-1410	related-only	schedule workflow available agents parse validate persist success message	src/mindroom/api/schedules.py:218; src/mindroom/commands/handler.py:135; src/mindroom/turn_policy.py:527
edit_scheduled_task	async_function	lines 1413-1446	duplicate-found	edit task fetch pending schedule replacement failure wrapper	src/mindroom/api/schedules.py:270; src/mindroom/api/schedules.py:218; src/mindroom/scheduling.py:605
list_scheduled_tasks	async_function	lines 1449-1524	duplicate-found	list scheduled tasks parse state sort format by next execution	src/mindroom/api/schedules.py:235; src/mindroom/api/schedules.py:146; src/mindroom/scheduling.py:450
list_scheduled_tasks.<locals>._sort_key	nested_function	lines 1486-1488	duplicate-found	scheduled task sort key one time before recurring datetime max	src/mindroom/api/schedules.py:146; src/mindroom/api/schedules.py:131
list_scheduled_tasks.<locals>._append_task_lines	nested_function	lines 1493-1505	related-only	format scheduled task list lines preview time description	src/mindroom/api/schedules.py:115; src/mindroom/scheduling.py:1395
cancel_scheduled_task	async_function	lines 1527-1557	related-only	cancel task state event cancelled content put state	src/mindroom/api/schedules.py:302; src/mindroom/scheduling.py:1560; src/mindroom/thread_tags.py:685
cancel_all_scheduled_tasks	async_function	lines 1560-1606	duplicate-found	cancel all pending scheduled state events parse state cancel put state	src/mindroom/scheduling.py:450; src/mindroom/scheduling.py:1527; src/mindroom/api/schedules.py:302
restore_scheduled_tasks	async_function	lines 1609-1705	duplicate-found	restore scheduled tasks parse state validate start overdue fail old tasks	src/mindroom/scheduling.py:188; src/mindroom/scheduling.py:450; src/mindroom/scheduling.py:913
```

## Findings

### 1. Scheduled task state parsing is duplicated inside restore and cancellation flows

`_parse_scheduled_task_record` at `src/mindroom/scheduling.py:188` is the canonical parser for the `com.mindroom.scheduled.task` Matrix state content.
It handles workflow JSON, non-pending legacy cancellation records, created-at parsing, and invalid content.
`_parse_task_records_from_state` at `src/mindroom/scheduling.py:450` correctly uses it for room listing and API reads.

`restore_scheduled_tasks` at `src/mindroom/scheduling.py:1623` repeats a lower-level state iteration and directly does `json.loads(content["workflow"])` plus `ScheduledWorkflow(**workflow_data)` at `src/mindroom/scheduling.py:1640`.
That is the same persisted-state interpretation but with narrower behavior: it does not reuse created-at parsing, legacy non-pending handling, or the content shape guards already present in `_parse_scheduled_task_record`.

`cancel_all_scheduled_tasks` at `src/mindroom/scheduling.py:1566` also iterates raw state events, checks `content.get("status") == "pending"`, and then updates Matrix state.
It partially overlaps `_parse_task_records_from_state`, but it needs raw existing content to preserve metadata for `_cancelled_task_content`, so this is a weaker duplication than restore.

Differences to preserve:

- Restore must skip non-pending tasks and must handle missed one-time task grace-period behavior.
- Bulk cancel needs the original raw content for cancellation metadata preservation.
- The parser currently tolerates old cancellation records, while restore only cares about pending records.

### 2. Cron and schedule presentation logic is duplicated between scheduling and the API adapter

`CronSchedule.to_cron_string` and `CronSchedule.to_natural_language` live in `src/mindroom/scheduling.py:109`.
The API adapter reverses that representation in `_cron_schedule_from_expression` at `src/mindroom/api/schedules.py:103` by splitting a five-field string and validating it with `croniter`.
That is a legitimate inverse operation, but it is schedule-domain behavior living outside the scheduling module.

`_to_response_task` at `src/mindroom/api/schedules.py:115` recomputes cron expression, cron description, and next run.
`_run_cron_task` at `src/mindroom/scheduling.py:946` computes the next run from the same cron string.
`list_scheduled_tasks._sort_key` at `src/mindroom/scheduling.py:1486` and `_task_sort_key` at `src/mindroom/api/schedules.py:146` also encode the same “one-time tasks first, otherwise datetime max” ordering idea in different shapes.

Differences to preserve:

- API responses need `CroniterError` handling and a response DTO.
- Runtime cron execution should keep its existing logging and polling semantics.
- Chat listing formats human-readable strings, while API listing returns typed fields.

### 3. Matrix content delivery with conversation-cache notification is repeated

Scheduled execution builds content in `_build_workflow_message_content` at `src/mindroom/scheduling.py:730`, sends it in `_execute_scheduled_workflow` at `src/mindroom/scheduling.py:883`, and calls `conversation_cache.notify_outbound_message` at `src/mindroom/scheduling.py:886`.
Scheduled failure notification repeats the same send-and-cache update pattern at `src/mindroom/scheduling.py:809`.

Equivalent flows appear in:

- `src/mindroom/delivery_gateway.py:531` and `src/mindroom/delivery_gateway.py:564` for normal response delivery.
- `src/mindroom/hooks/sender.py:81` and `src/mindroom/hooks/sender.py:90` for hook-originated messages.
- `src/mindroom/thread_summary.py:409` and `src/mindroom/thread_summary.py:424` for summary notices.
- `src/mindroom/custom_tools/subagents.py:265` and `src/mindroom/custom_tools/subagents.py:275` for delegated subagent messages.

The behavior is functionally the same at the IO boundary: build Matrix content, send through `send_message_result`, and notify the same conversation cache when delivery succeeds.

Differences to preserve:

- Scheduled messages add `com.mindroom.source_kind = "scheduled"` and sometimes `ORIGINAL_SENDER_KEY`.
- Hook messages add hook metadata and may trigger dispatch.
- Delivery gateway supports replies, tool traces, skip mentions, and terminal status handling.

### 4. Mention extraction from free text duplicates the lower-level mention parsing pattern

`_extract_mentioned_agents_from_text` at `src/mindroom/scheduling.py:1219` calls `parse_mentions_in_text`, converts user IDs to `MatrixID`, filters to configured agents with `agent_name`, and preserves uniqueness.
`thread_utils.check_agent_mentioned` at `src/mindroom/thread_utils.py:89` uses `_agents_from_user_ids` after extracting mention IDs from Matrix content, and `matrix/mentions.py:43` already centralizes text mention parsing.

This is only a moderate duplication because scheduling starts from raw command text, while thread utilities start from Matrix event content.
Still, the “user IDs to unique configured agent MatrixIDs” transform is repeated in spirit and could be shared if more call sites appear.

Differences to preserve:

- Scheduling parses plain text before a Matrix event exists.
- Thread utilities must inspect event content and distinguish non-agent human mentions.

### 5. ISO datetime parsing is repeated but behavior is intentionally inconsistent

`_parse_datetime` at `src/mindroom/scheduling.py:176` accepts object input, handles trailing `Z`, returns `None` for invalid/non-string values, and does not force naive datetimes to UTC.
Similar helpers exist in `src/mindroom/thread_tags.py:107`, `src/mindroom/approval_manager.py:102`, `src/mindroom/approval_events.py:150`, and `src/mindroom/knowledge/utils.py:138`.

This is real duplication of a small utility, but the behavior differences matter.
Some callers raise on malformed values, some return `None`, and some normalize naive datetimes to UTC.

## Proposed Generalization

1. Refactor `restore_scheduled_tasks` to use `_parse_scheduled_task_record` or `_parse_task_records_from_state(include_non_pending=False)` before applying restore-specific validation and missed-task handling.
2. Add a small scheduling-domain helper, likely in `src/mindroom/scheduling.py`, for cron expression parsing and next-run calculation, then have `src/mindroom/api/schedules.py` call it instead of owning schedule-domain parsing.
3. Consider a narrow Matrix delivery helper only if the team wants to touch multiple delivery paths at once: `send_and_notify_conversation_cache(client, room_id, content, config, conversation_cache) -> DeliveredMessage | None` in `src/mindroom/matrix/client_delivery.py`.
4. Leave mention extraction as-is unless a third raw-text agent-mention call site appears; the current duplication is small and domain-specific.
5. Do not consolidate datetime parsing until the desired invalid-input and naive-timezone semantics are made explicit per call site.

## Risk/tests

Main test risk is scheduled-task restore behavior.
Any parser refactor should cover pending one-time tasks, pending cron tasks, malformed workflow JSON, non-pending legacy cancellation records, missed one-time tasks within the grace window, and missed tasks older than `_MISSED_TASK_MAX_AGE_SECONDS`.

Cron helper changes should cover five-field parsing, invalid expression errors, next-run calculation, and API response serialization.

Delivery helper changes would be broad because delivery, hooks, thread summaries, subagents, and scheduling all touch live Matrix send behavior.
If pursued, tests should assert successful sends notify the conversation cache exactly once and failed sends do not.
