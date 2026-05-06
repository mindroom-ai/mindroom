## Summary

Top duplication candidate: `src/mindroom/hooks/execution.py` has its own message-envelope resolver in `_context_scope_envelope`, duplicating the same context-to-envelope mapping implemented by `_message_envelope_for_hook_context` in `src/mindroom/hooks/context.py`.
The rest of the module is mostly hook-execution orchestration.
Several helpers are related to patterns elsewhere, especially deepcopy isolation and tool hook context construction, but I did not find another active implementation with the same behavior and contract.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_HookInvocationResult	class	lines 54-56	none-found	"_HookInvocationResult succeeded value invocation result dataclass"	none
_scope_agent_name	function	lines 59-71	related-only	"agent scope agent_name target_entity_name entity_name CompactionHookContext SessionHookContext ToolBeforeCallContext ToolAfterCallContext"	src/mindroom/tool_system/tool_hooks.py:168; src/mindroom/hooks/context.py:759
_scope_room_ids	function	lines 74-90	related-only	"room scope room_ids room_id rooms ReactionReceivedContext ScheduleFiredContext AgentLifecycleContext CustomEventContext"	src/mindroom/tool_system/tool_hooks.py:168; src/mindroom/hooks/context.py:759
_hook_in_scope	function	lines 93-104	none-found	"RegisteredHook agents rooms hook scope filter hooks_for agents rooms"	none
_context_scope_envelope	function	lines 107-116	duplicate-found	"context envelope MessageReceivedContext MessageEnrichContext BeforeResponseContext AfterResponseContext CancelledResponseContext draft envelope result envelope info envelope"	src/mindroom/hooks/context.py:796
_context_logger	function	lines 119-124	none-found	"mindroom.hooks bind plugin_name hook_name event_name context logger"	none
_snapshot_tool_observer_value	function	lines 127-134	related-only	"deepcopy value repr observer snapshot result fallback deepcopy"	src/mindroom/workers/runtime.py:90; src/mindroom/metadata_merge.py:15; src/mindroom/tool_system/tool_hooks.py:599
_snapshot_tool_observer_error	function	lines 137-145	related-only	"deepcopy error BaseException Exception str(error) observer snapshot"	src/mindroom/tool_system/tool_hooks.py:663; src/mindroom/tool_system/tool_calls.py: record_tool_failure search
_snapshot_compaction_messages	function	lines 148-162	related-only	"deepcopy messages model_copy(deep=True) compaction messages snapshot"	src/mindroom/ai_runtime.py:62; src/mindroom/vertex_claude_prompt_cache.py:32; src/mindroom/history/compaction.py:1335; src/mindroom/history/compaction.py:1590
_bind_hook_context	function	lines 165-183	related-only	"replace context plugin_name settings logger arguments result error messages _items hook context"	src/mindroom/tool_system/tool_hooks.py:103; src/mindroom/hooks/context.py:196
_merge_observer_context_changes	function	lines 186-198	none-found	"merge observer context changes declined decline_reason suppress message_text"	none
_effective_timeout_ms	function	lines 201-202	related-only	"default_timeout_ms_for_event timeout_ms RegisteredHook"	src/mindroom/hooks/types.py:170
_invoke_hook	async_function	lines 205-227	none-found	"asyncio.timeout hook.callback duration_ms Hook execution failed succeeded timeout_ms"	none
_eligible_hooks	function	lines 230-250	none-found	"hooks_for hook_in_scope skip_plugin_names EVENT_MESSAGE_RECEIVED eligible hooks"	none
emit	async_function	lines 253-289	related-only	"emit observer hooks serially recursion limit ContextVar _EMIT_DEPTH continue_on_cancelled"	src/mindroom/hooks/execution.py:292; src/mindroom/hooks/execution.py:423
emit_gate	async_function	lines 292-322	related-only	"emit_gate gate hooks serial first decline recursion limit ToolBeforeCallContext"	src/mindroom/hooks/execution.py:253; src/mindroom/tool_system/tool_hooks.py:518
_normalize_collector_result	function	lines 325-335	related-only	"EnrichmentItem list hook_context _items add_metadata add_instruction collector result"	src/mindroom/hooks/context.py:397; src/mindroom/hooks/context.py:417
emit_collect	async_function	lines 338-360	related-only	"emit_collect collector hooks concurrently Semaphore gather EnrichmentItem"	src/mindroom/knowledge/manager.py:1620; src/mindroom/knowledge/utils.py:581; src/mindroom/matrix/conversation_cache.py:744
emit_collect.<locals>.run_hook	nested_async_function	lines 350-354	related-only	"run_hook semaphore bind hook context invoke normalize collector result"	src/mindroom/knowledge/manager.py:1623; src/mindroom/matrix/conversation_cache.py:733
emit_transform	async_function	lines 363-379	related-only	"emit_transform before_response serial transform ResponseDraft preserve_failed_draft"	src/mindroom/hooks/execution.py:382; src/mindroom/delivery_gateway.py:88
emit_final_response_transform	async_function	lines 382-398	related-only	"emit_final_response_transform final_response copy_on_write best effort isolation"	src/mindroom/hooks/execution.py:363; src/mindroom/delivery_gateway.py:113
_copy_transform_draft	function	lines 401-402	related-only	"deepcopy draft ResponseDraft FinalResponseDraft transform draft copy"	src/mindroom/delivery_gateway.py:101; src/mindroom/streaming.py:849
_transform_context_with_draft	function	lines 405-406	none-found	"replace context draft transform context with draft"	none
_next_transform_draft	function	lines 409-420	none-found	"next transform draft invocation succeeded preserve_failed_draft isinstance invocation.value"	none
_emit_serial_transform	async_function	lines 423-455	related-only	"serial transform hooks copy_on_write preserve_failed_draft continue_on_cancelled invoke hook current_draft"	src/mindroom/hooks/execution.py:253; src/mindroom/hooks/execution.py:292
```

## Findings

### 1. Duplicate context-to-envelope extraction in hook execution and hook context modules

`src/mindroom/hooks/execution.py:107` implements `_context_scope_envelope`, which returns the message envelope for:

- `MessageReceivedContext`, `MessageEnrichContext`, and `SystemEnrichContext` through `context.envelope`.
- `BeforeResponseContext` and `FinalResponseTransformContext` through `context.draft.envelope`.
- `AfterResponseContext` through `context.result.envelope`.
- `CancelledResponseContext` through `context.info.envelope`.

`src/mindroom/hooks/context.py:796` implements `_message_envelope_for_hook_context` with the same type cases and field paths.
The behavior is functionally the same for all overlapping context types.
The context module helper accepts `object`, while the execution helper accepts `HookExecutionContext`, but the returned value and extraction rules match.

Differences to preserve:

- `_context_scope_envelope` is private to execution and typed for hook dispatch scope checks.
- `_message_envelope_for_hook_context` is private to context and used for hook-originated send metadata and message depth propagation.
- Tool contexts do not carry an envelope in either implementation.

## Proposed Generalization

Move the shared envelope extraction to one private hook-context utility or make the existing `src/mindroom/hooks/context.py:796` helper importable within `hooks.execution`.
The minimal refactor would be:

1. Rename `_message_envelope_for_hook_context` to a still-private but package-level helper with a neutral name, for example `message_envelope_for_hook_context`.
2. Use that helper in `src/mindroom/hooks/execution.py` for `_context_scope_envelope` or replace `_context_scope_envelope` entirely if the wider `object` parameter is acceptable.
3. Keep `_scope_agent_name` and `_scope_room_ids` behavior unchanged for non-envelope contexts.
4. Add focused tests for envelope extraction across received, enrich, before-response, final-response, after-response, and cancelled contexts.

No other refactor recommended.
The related-only patterns are either local orchestration policy, event-specific hook semantics, or generic deepcopy/concurrency idioms that would become less clear if centralized.

## Risk/tests

The main risk is import-cycle pressure inside `mindroom.hooks`, because `context.py` already defines the dataclasses consumed by `execution.py`.
If the helper stays in `context.py`, keep imports one-way from `execution.py` to `context.py`.
Tests should cover both scope filtering and hook send metadata so the shared helper cannot drift.

No production code was edited.
