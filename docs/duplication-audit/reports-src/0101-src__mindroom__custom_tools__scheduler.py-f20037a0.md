## Summary

The strongest duplication candidate is the scheduler command/tool adapter split: `src/mindroom/custom_tools/scheduler.py` exposes `schedule`, `edit_schedule`, `list_schedules`, and `cancel_schedule` through Agno tools, while `src/mindroom/commands/handler.py` exposes the same backend behavior for Matrix bang commands.
This duplication is mostly adapter-level because both paths already delegate to the shared scheduling primitives in `src/mindroom/scheduling.py`.
A smaller related duplication exists in live `SchedulingRuntime` construction between `commands/handler.py` and `tool_system/runtime_context.py`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_raise_for_scheduler_error	function	lines 17-19	related-only	"❌" scheduler error RuntimeError Unable to retrieve scheduled tasks	src/mindroom/scheduling.py:1300, src/mindroom/scheduling.py:1456, src/mindroom/scheduling.py:1460, src/mindroom/scheduling.py:1527, src/mindroom/commands/handler.py:246
SchedulerTools	class	lines 22-132	related-only	SchedulerTools Toolkit scheduler custom tool command handler	src/mindroom/commands/handler.py:240, src/mindroom/custom_tools/homeassistant.py:22, src/mindroom/tools_metadata.json:7827
SchedulerTools.__init__	method	lines 25-29	related-only	Toolkit name scheduler tools schedule edit_schedule list_schedules cancel_schedule	src/mindroom/custom_tools/homeassistant.py:49, src/mindroom/tools_metadata.json:7827
SchedulerTools.schedule	async_method	lines 31-63	duplicate-found	schedule_task full_text scheduled_by thread_id new_thread command schedule	src/mindroom/commands/handler.py:240, src/mindroom/commands/handler.py:246, src/mindroom/scheduling.py:1241
SchedulerTools.edit_schedule	async_method	lines 65-90	duplicate-found	edit_scheduled_task task_id full_text scheduled_by thread_id command edit_schedule	src/mindroom/commands/handler.py:281, src/mindroom/commands/handler.py:284, src/mindroom/scheduling.py:1413
SchedulerTools.list_schedules	async_method	lines 92-110	duplicate-found	list_scheduled_tasks room_id thread_id config command list_schedules	src/mindroom/commands/handler.py:255, src/mindroom/commands/handler.py:256, src/mindroom/scheduling.py:1449, src/mindroom/api/schedules.py:247
SchedulerTools.cancel_schedule	async_method	lines 112-132	duplicate-found	cancel_scheduled_task task_id room_id command cancel_schedule	src/mindroom/commands/handler.py:263, src/mindroom/commands/handler.py:275, src/mindroom/scheduling.py:1527, src/mindroom/api/schedules.py:314
```

## Findings

### 1. Scheduler tool methods duplicate command scheduling dispatch

`src/mindroom/custom_tools/scheduler.py:31`, `src/mindroom/custom_tools/scheduler.py:65`, `src/mindroom/custom_tools/scheduler.py:92`, and `src/mindroom/custom_tools/scheduler.py:112` are the tool-facing versions of the command branches in `src/mindroom/commands/handler.py:240`, `src/mindroom/commands/handler.py:255`, `src/mindroom/commands/handler.py:263`, and `src/mindroom/commands/handler.py:281`.
Both adapters gather current Matrix room/thread/user context and then call `schedule_task`, `edit_scheduled_task`, `list_scheduled_tasks`, or `cancel_scheduled_task`.

The important difference is the calling contract.
The command path returns user-visible text directly so it can be posted back to Matrix.
The tool path raises on scheduler error strings so model tool execution records failure instead of treating a textual error as a successful tool result.
The tool path also supports `new_thread`, while the command path currently schedules into the effective command thread.

### 2. Live `SchedulingRuntime` construction is repeated across adapters

`src/mindroom/tool_system/runtime_context.py:477` and `src/mindroom/commands/handler.py:41` both construct `SchedulingRuntime` from live Matrix runtime collaborators.
They copy the same fields: `client`, `config`, `runtime_paths`, `room`, `conversation_cache`, `event_cache`, and `matrix_admin`.

This is real duplication, but it is small and each helper adapts from a different typed source.
The tool helper must validate that `ToolRuntimeContext.room` is present, while the command helper receives an explicit `nio.MatrixRoom`.

### 3. Error-string escalation is tool-specific and not meaningfully duplicated

`_raise_for_scheduler_error` in `src/mindroom/custom_tools/scheduler.py:17` maps scheduler user-facing failure strings to `RuntimeError`.
The matching error strings originate in shared scheduler functions such as `src/mindroom/scheduling.py:1300`, `src/mindroom/scheduling.py:1460`, and `src/mindroom/scheduling.py:1546`.

This is related behavior rather than a duplicate implementation.
Commands intentionally preserve these strings as chat replies, while tools intentionally turn them into tool failures.

## Proposed Generalization

No production refactor is required for the main scheduling behavior because the substantial logic is already centralized in `src/mindroom/scheduling.py`.

If this area changes again, the smallest useful cleanup would be:

1. Add a tiny adapter helper near the scheduling integration boundary that accepts a resolved live runtime, room ID, thread ID, requester ID, and operation enum/callable.
2. Keep error escalation configurable so command replies remain text and tool calls can raise on failures.
3. Optionally add a `SchedulingRuntime.from_live_parts(...)` constructor or pure helper in `src/mindroom/scheduling.py` only if more runtime-construction call sites appear.
4. Leave API schedule endpoints separate because they return typed HTTP models and use different edit/list semantics.

## Risk/Tests

The main risk in deduplicating the adapter layer is changing user-visible error handling.
Command paths must keep returning scheduler strings, while tool paths must keep raising failures for `❌` responses and `"Unable to retrieve scheduled tasks."`.

Tests that would need attention for any future refactor:

- Tool tests for unavailable context, schedule failure raising, edit/list/cancel failure raising, and `new_thread=True`.
- Command handler tests for `!schedule`, `!edit_schedule`, `!list_schedules`, and `!cancel_schedule` preserving reply text.
- Scheduling backend tests around `schedule_task`, `edit_scheduled_task`, `list_scheduled_tasks`, and `cancel_scheduled_task`.
