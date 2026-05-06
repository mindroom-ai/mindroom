## Summary

Top duplication candidate: plugin state-root resolution is duplicated between `src/mindroom/hooks/context.py` and `src/mindroom/tool_system/runtime_context.py`.
The primary module also contains intentional intra-file duplication across `HookContext`, `ToolBeforeCallContext`, `ToolAfterCallContext`, and `ScheduleFiredContext`, already partially centralized through private helper functions.
No other meaningful cross-file duplication was found for the remaining dataclass-only hook context symbols.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_UnsetType	class	lines 23-24	none-found	"_UnsetType _UNSET sentinel omitted optional hook arguments"	none
_resolve_plugin_state_root	function	lines 63-73	duplicate-found	"storage_root / \"plugins\" validate_plugin_name plugin state root mkdir"	src/mindroom/tool_system/runtime_context.py:533-549
_send_bound_message	async_function	lines 76-107	related-only	"hook send_message source_hook ORIGINAL_SENDER_KEY HOOK_MESSAGE_RECEIVED_DEPTH_KEY trigger_dispatch"	src/mindroom/bot.py:1672-1704; src/mindroom/delivery_gateway.py:523-570; src/mindroom/matrix/mentions.py:360-413
_query_bound_room_state	async_function	lines 110-121	related-only	"query_room_state room_state_querier No room state querier available"	src/mindroom/hooks/state.py:13-42; src/mindroom/thread_tags.py:884-900
_put_bound_room_state	async_function	lines 124-136	related-only	"put_room_state room_state_putter No room state putter available"	src/mindroom/hooks/state.py:45-65; src/mindroom/thread_tags.py:903-957
HookContextSupport	class	lines 140-212	related-only	"HookContextSupport hook bindings base_kwargs message_sender room_state_querier matrix_admin"	src/mindroom/tool_system/runtime_context.py:177-238; src/mindroom/tool_system/runtime_context.py:405-413
HookContextSupport.registry	method	lines 152-154	none-found	"hook_registry_state.registry property active hook registry snapshot"	none
HookContextSupport.message_sender	method	lines 156-165	related-only	"hook_message_sender orchestrator.hook_message_sender runtime.client"	src/mindroom/tool_system/runtime_context.py:231-233; src/mindroom/tool_system/runtime_context.py:405-409
HookContextSupport.room_state_querier	method	lines 167-174	related-only	"build_hook_room_state_querier chain_hook_room_state_queriers orchestrator fallback"	src/mindroom/orchestrator.py:986-991; src/mindroom/tool_system/runtime_context.py:410
HookContextSupport.room_state_putter	method	lines 176-183	related-only	"build_hook_room_state_putter chain_hook_room_state_putters orchestrator fallback"	src/mindroom/orchestrator.py:993-998; src/mindroom/tool_system/runtime_context.py:411
HookContextSupport.matrix_admin	method	lines 185-194	related-only	"hook_matrix_admin build_hook_matrix_admin router fallback"	src/mindroom/orchestrator.py:1000-1005; src/mindroom/tool_system/runtime_context.py:234
HookContextSupport.base_kwargs	method	lines 196-212	related-only	"base_kwargs hook context construction event_name plugin_name settings config runtime_paths logger"	src/mindroom/tool_system/tool_hooks.py:103-119; src/mindroom/delivery_gateway.py:107-130
MessageEnvelope	class	lines 216-231	related-only	"MessageEnvelope source_event_id requester_id message_received_depth hook_source"	src/mindroom/conversation_resolver.py:223-275; src/mindroom/conversation_resolver.py:277-322; src/mindroom/response_runner.py:746-778
ResponseDraft	class	lines 235-243	related-only	"ResponseDraft response_text response_kind tool_trace extra_content envelope suppress"	src/mindroom/delivery_gateway.py:88-111; src/mindroom/hooks/execution.py:363-379
FinalResponseDraft	class	lines 247-252	related-only	"FinalResponseDraft response_text response_kind envelope"	src/mindroom/delivery_gateway.py:113-136; src/mindroom/hooks/execution.py:382-398
ResponseResult	class	lines 256-263	related-only	"ResponseResult response_event_id delivery_kind response_kind envelope"	src/mindroom/delivery_gateway.py:138-166
HookContext	class	lines 267-376	related-only	"HookContext base fields state_root send_message query_room_state put_room_state"	src/mindroom/tool_system/tool_hooks.py:85-119; src/mindroom/tool_system/runtime_context.py:66-95
HookContext.state_root	method	lines 288-290	duplicate-found	"state_root plugin_name runtime_paths plugins validate_plugin_name"	src/mindroom/tool_system/runtime_context.py:533-549
HookContext.send_message	async_method	lines 292-326	related-only	"send_message hook context trigger_dispatch requester_id message_received_depth"	src/mindroom/hooks/context.py:612-634; src/mindroom/hooks/context.py:702-724; src/mindroom/bot.py:1672-1704
HookContext.query_room_state	async_method	lines 328-341	related-only	"query_room_state HookContext _query_bound_room_state"	src/mindroom/hooks/context.py:636-649; src/mindroom/hooks/context.py:726-739
HookContext.get_latest_agent_message_snapshot	async_method	lines 343-359	related-only	"get_latest_agent_message_snapshot agent_message_snapshot_reader runtime_started_at"	src/mindroom/bot.py:1706-1729; src/mindroom/matrix/cache/event_cache.py:75
HookContext.put_room_state	async_method	lines 361-376	related-only	"put_room_state HookContext _put_bound_room_state"	src/mindroom/hooks/context.py:651-666; src/mindroom/hooks/context.py:741-756
MessageReceivedContext	class	lines 380-385	none-found	"MessageReceivedContext envelope skip_plugin_names suppress"	none
MessageEnrichContext	class	lines 389-405	related-only	"MessageEnrichContext _items EnrichmentItem add_metadata"	src/mindroom/hooks/execution.py:325-360
MessageEnrichContext.add_metadata	method	lines 397-405	related-only	"add_metadata EnrichmentItem key text cache_policy volatile"	src/mindroom/hooks/context.py:417-425; src/mindroom/hooks/execution.py:325-335
SystemEnrichContext	class	lines 409-425	related-only	"SystemEnrichContext _items EnrichmentItem add_instruction"	src/mindroom/hooks/execution.py:325-360
SystemEnrichContext.add_instruction	method	lines 417-425	related-only	"add_instruction EnrichmentItem key text cache_policy volatile"	src/mindroom/hooks/context.py:397-405; src/mindroom/hooks/execution.py:325-335
BeforeResponseContext	class	lines 429-432	related-only	"BeforeResponseContext draft ResponseDraft"	src/mindroom/delivery_gateway.py:107-111; src/mindroom/hooks/execution.py:363-379
FinalResponseTransformContext	class	lines 436-439	related-only	"FinalResponseTransformContext draft FinalResponseDraft"	src/mindroom/delivery_gateway.py:128-136; src/mindroom/hooks/execution.py:382-398
AfterResponseContext	class	lines 443-446	related-only	"AfterResponseContext result ResponseResult"	src/mindroom/delivery_gateway.py:151-166
CancelledResponseInfo	class	lines 450-456	related-only	"CancelledResponseInfo envelope visible_response_event_id response_kind failure_reason"	src/mindroom/delivery_gateway.py:168-180
CancelledResponseContext	class	lines 460-463	related-only	"CancelledResponseContext info CancelledResponseInfo"	src/mindroom/delivery_gateway.py:168-180
AgentLifecycleContext	class	lines 467-475	none-found	"AgentLifecycleContext entity_name entity_type rooms matrix_user_id stop_reason"	none
CompactionHookContext	class	lines 479-490	related-only	"CompactionHookContext messages token_count compaction_summary"	src/mindroom/hooks/execution.py:148-162
ScheduleFiredContext	class	lines 494-528	related-only	"ScheduleFiredContext workflow room_id thread_id created_by message_text suppress"	src/mindroom/scheduling.py:857-858
ScheduleFiredContext.send_message	async_method	lines 505-528	related-only	"ScheduleFiredContext send_message _UNSET default thread_id created_by"	src/mindroom/hooks/context.py:292-326; src/mindroom/hooks/context.py:612-634; src/mindroom/hooks/context.py:702-724
ReactionReceivedContext	class	lines 532-540	none-found	"ReactionReceivedContext reaction_key target_event_id thread_id"	none
ConfigReloadedContext	class	lines 544-550	none-found	"ConfigReloadedContext changed_entities added_entities removed_entities plugin_changes"	none
SessionHookContext	class	lines 554-561	none-found	"SessionHookContext agent_name scope session_id room_id thread_id"	none
CustomEventContext	class	lines 565-573	related-only	"CustomEventContext payload source_plugin room_id thread_id sender_id message_received_depth"	src/mindroom/tool_system/runtime_context.py:552-587
ToolBeforeCallContext	class	lines 577-666	related-only	"ToolBeforeCallContext tool_name arguments declined decline_reason hook_context_kwargs"	src/mindroom/tool_system/tool_hooks.py:85-119; src/mindroom/tool_system/tool_hooks.py:532-543
ToolBeforeCallContext.decline	method	lines 602-605	none-found	"decline declined decline_reason ToolBeforeCallContext"	none
ToolBeforeCallContext.state_root	method	lines 608-610	duplicate-found	"ToolBeforeCallContext state_root plugin_name runtime_paths plugins validate_plugin_name"	src/mindroom/tool_system/runtime_context.py:533-549
ToolBeforeCallContext.send_message	async_method	lines 612-634	related-only	"ToolBeforeCallContext send_message requester_id message_received_depth trigger_dispatch"	src/mindroom/hooks/context.py:292-326; src/mindroom/hooks/context.py:702-724
ToolBeforeCallContext.query_room_state	async_method	lines 636-649	related-only	"ToolBeforeCallContext query_room_state _query_bound_room_state"	src/mindroom/hooks/context.py:328-341; src/mindroom/hooks/context.py:726-739
ToolBeforeCallContext.put_room_state	async_method	lines 651-666	related-only	"ToolBeforeCallContext put_room_state _put_bound_room_state"	src/mindroom/hooks/context.py:361-376; src/mindroom/hooks/context.py:741-756
ToolAfterCallContext	class	lines 670-756	related-only	"ToolAfterCallContext result error blocked duration_ms hook_context_kwargs"	src/mindroom/tool_system/tool_hooks.py:85-119; src/mindroom/tool_system/tool_hooks.py:650-661
ToolAfterCallContext.state_root	method	lines 698-700	duplicate-found	"ToolAfterCallContext state_root plugin_name runtime_paths plugins validate_plugin_name"	src/mindroom/tool_system/runtime_context.py:533-549
ToolAfterCallContext.send_message	async_method	lines 702-724	related-only	"ToolAfterCallContext send_message requester_id message_received_depth trigger_dispatch"	src/mindroom/hooks/context.py:292-326; src/mindroom/hooks/context.py:612-634
ToolAfterCallContext.query_room_state	async_method	lines 726-739	related-only	"ToolAfterCallContext query_room_state _query_bound_room_state"	src/mindroom/hooks/context.py:328-341; src/mindroom/hooks/context.py:636-649
ToolAfterCallContext.put_room_state	async_method	lines 741-756	related-only	"ToolAfterCallContext put_room_state _put_bound_room_state"	src/mindroom/hooks/context.py:361-376; src/mindroom/hooks/context.py:651-666
_requester_id_for_hook_send	function	lines 759-778	related-only	"requester_id_for_hook_send envelope requester_id ScheduleFired created_by sender_id trigger_dispatch"	src/mindroom/tool_system/tool_hooks.py:168-217; src/mindroom/conversation_resolver.py:223-275
_message_received_depth_for_hook_send	function	lines 781-783	related-only	"message_received_depth_for_hook_send current next depth hook send"	src/mindroom/conversation_resolver.py:133-175; src/mindroom/tool_system/runtime_context.py:405-413
_current_message_received_depth	function	lines 786-793	related-only	"current_message_received_depth envelope CustomEventContext ToolBeforeCallContext ToolAfterCallContext"	src/mindroom/hooks/execution.py:107-116; src/mindroom/tool_system/runtime_context.py:405-413
_message_envelope_for_hook_context	function	lines 796-806	related-only	"message_envelope_for_hook_context MessageReceivedContext draft result info envelope"	src/mindroom/hooks/execution.py:107-116
_next_message_received_depth	function	lines 809-811	none-found	"next_message_received_depth current_depth + 1"	none
```

## Findings

### 1. Plugin state-root resolution is duplicated

`src/mindroom/hooks/context.py:63-73` and `src/mindroom/tool_system/runtime_context.py:533-549` both validate a plugin name, build `runtime_paths.storage_root / "plugins" / normalized_plugin_name`, create the directory, and return the path.
The behavior is functionally the same once `runtime_paths` has been resolved.
The only difference to preserve is error wording and `runtime_context.get_plugin_state_root()`'s ability to derive `runtime_paths` from the ambient tool runtime context when no explicit `runtime_paths` is passed.

### 2. Hook send/query/put methods repeat accessors within this module, but are already centralized

`HookContext.send_message()` at `src/mindroom/hooks/context.py:292-326`, `ScheduleFiredContext.send_message()` at `src/mindroom/hooks/context.py:505-528`, `ToolBeforeCallContext.send_message()` at `src/mindroom/hooks/context.py:612-634`, and `ToolAfterCallContext.send_message()` at `src/mindroom/hooks/context.py:702-724` share the same hook-message dispatch behavior through `_send_bound_message()` at `src/mindroom/hooks/context.py:76-107`.
The room-state methods use the same pattern through `_query_bound_room_state()` and `_put_bound_room_state()`.
This is not cross-file duplication, and it is already reduced to thin context-specific wrappers.
Differences to preserve are schedule default-thread behavior and tool contexts' direct `requester_id` use.

### 3. Envelope extraction is related to hook execution scope, but not enough to deduplicate now

`_message_envelope_for_hook_context()` at `src/mindroom/hooks/context.py:796-806` overlaps with `_context_scope_envelope()` at `src/mindroom/hooks/execution.py:107-116`.
Both inspect hook context variants and return the carried `MessageEnvelope`.
The execution helper is private to hook scoping and works over `HookExecutionContext`; the context helper supports send-depth and requester resolution.
The duplication is small, but the two functions are in different ownership areas and changing this would increase coupling for little benefit.

## Proposed Generalization

1. Move the canonical plugin-state path builder to a small shared helper, for example `mindroom.tool_system.plugin_state.resolve_plugin_state_root(runtime_paths, plugin_name)`.
2. Have `src/mindroom/hooks/context.py:_resolve_plugin_state_root()` delegate to that helper while keeping its current error message for missing `runtime_paths`.
3. Have `src/mindroom/tool_system/runtime_context.py:get_plugin_state_root()` keep ambient-context resolution, then delegate path creation to the shared helper.
4. Do not refactor hook send/query/put wrappers; the existing private helpers already provide the useful deduplication.
5. Do not merge `_message_envelope_for_hook_context()` with `hooks.execution._context_scope_envelope()` unless a future change adds a third copy.

## Risk/tests

The plugin state-root refactor is low risk but should be covered by tests that assert both hook contexts and `get_plugin_state_root()` create the same validated directory under `runtime_paths.storage_root / "plugins"`.
Tests should also preserve the missing-runtime-path error behavior for hook `state_root` and the ambient-context fallback behavior for tool runtime `get_plugin_state_root()`.
No production code was edited for this audit.
