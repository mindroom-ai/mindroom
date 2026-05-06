# Summary

Top duplication candidates for `src/mindroom/tool_system/tool_hooks.py`:

- Tool hook context assembly in `_ResolvedToolContext.hook_context_kwargs` and `_resolve_tool_context` overlaps with runtime hook binding/context assembly in `src/mindroom/tool_system/runtime_context.py:405` and `src/mindroom/tool_system/runtime_context.py:568`.
- Tool call record metadata forwarding in `_record_debug_tool_success` and `_execute_bridge` overlaps with the record-builder parameter sets in `src/mindroom/tool_system/tool_calls.py:349` and `src/mindroom/tool_system/tool_calls.py:385`.
- Async/sync bridge mechanics are related to approval transport loop handoff in `src/mindroom/approval_transport.py:102`, but they solve different directionality and no shared helper is recommended.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_DeferredAsyncToolHookResult	class	lines 79-82	related-only	Deferred async hook result sentinel sync bridge awaitable	src/mindroom/approval_transport.py:102; src/mindroom/sync_bridge_state.py:18; src/mindroom/tool_system/runtime_context.py:168
_ResolvedToolContext	class	lines 86-119	related-only	tool resolved context hook fields runtime context bindings	src/mindroom/tool_system/runtime_context.py:66; src/mindroom/tool_system/runtime_context.py:148; src/mindroom/tool_system/runtime_context.py:405; src/mindroom/hooks/context.py:577
_ResolvedToolContext.hook_context_kwargs	method	lines 103-119	duplicate-found	hook context kwargs ToolBeforeCallContext ToolAfterCallContext CustomEventContext	src/mindroom/tool_system/runtime_context.py:568; src/mindroom/hooks/context.py:196; src/mindroom/hooks/context.py:577
_ToolHookBridgeContext	class	lines 123-129	related-only	bridge context static hook inputs dispatch context config runtime_paths	src/mindroom/agents.py:843; src/mindroom/tool_system/runtime_context.py:97
_correlation_id_for_runtime_context	function	lines 132-139	related-only	correlation_id current_llm_request_log_context uuid4 fallback	src/mindroom/ai.py:426; src/mindroom/response_runner.py:769; src/mindroom/tool_system/runtime_context.py:566
_ambient_tool_dispatch_context	function	lines 142-149	related-only	get_tool_runtime_context active_tool_execution_identity LiveToolDispatchContext	src/mindroom/tool_system/runtime_context.py:395; src/mindroom/tool_system/runtime_context.py:470; src/mindroom/tool_system/worker_routing.py:106
_explicit_bridge_dispatch_context	function	lines 152-165	related-only	dispatch context matches runtime context LiveToolDispatchContext	src/mindroom/tool_system/runtime_context.py:127; src/mindroom/tool_system/runtime_context.py:448; src/mindroom/tool_system/runtime_context.py:470
_resolve_tool_context	function	lines 168-237	duplicate-found	resolve tool context runtime bindings execution identity reply_to_event_id	src/mindroom/tool_system/runtime_context.py:190; src/mindroom/tool_system/runtime_context.py:405; src/mindroom/tool_system/runtime_context.py:432; src/mindroom/tool_system/runtime_context.py:568
_should_record_successful_tool_call	function	lines 240-242	related-only	log_llm_requests tool success record debug config	src/mindroom/llm_request_logging.py:342; src/mindroom/model_loading.py:168; src/mindroom/tool_system/tool_calls.py:500
_record_debug_tool_success	function	lines 245-270	duplicate-found	record_tool_success metadata forwarding room thread requester session correlation	src/mindroom/tool_system/tool_calls.py:385; src/mindroom/tool_system/tool_calls.py:500; src/mindroom/tool_system/tool_calls.py:517
_format_declined_result	function	lines 273-274	none-found	TOOL CALL DECLINED declined result template reason	none
_approval_status_reason	function	lines 277-286	related-only	approval status reason approved denied expired pending	src/mindroom/approval_manager.py:42; src/mindroom/approval_manager.py:50; src/mindroom/tool_approval.py:96
_await_result	async_function	lines 289-290	related-only	await awaitable helper inspect isawaitable asyncio run	src/mindroom/tool_approval.py:224; src/mindroom/api/sandbox_runner.py:474
_run_coroutine_from_sync	function	lines 293-302	related-only	run coroutine from sync inspect awaitable asyncio.run deferred	src/mindroom/tool_approval.py:224; src/mindroom/custom_tools/browser.py:274; src/mindroom/api/sandbox_runner.py:1249
_run_deferred_result_from_sync	function	lines 305-328	related-only	sync bridge blocked loop copy_context thread asyncio.run	src/mindroom/approval_transport.py:102; src/mindroom/sync_bridge_state.py:18; src/mindroom/workers/backends/kubernetes.py:211
_run_deferred_result_from_sync.<locals>.runner	nested_function	lines 316-320	related-only	thread runner context.run asyncio.run result_box error_box	src/mindroom/workers/backends/kubernetes.py:211; src/mindroom/approval_transport.py:124
_resolve_deferred_sync_result	function	lines 331-334	none-found	resolve deferred sync result sentinel loop	none
_patch_agno_sync_tool_hook_chain	function	lines 337-357	none-found	patch agno build_nested_execution_chain sync FunctionCall	none
_patch_agno_sync_tool_hook_chain.<locals>._patched_build_nested_execution_chain	nested_function	lines 345-354	none-found	patched build_nested_execution_chain sync wrapper	none
_patch_agno_sync_tool_hook_chain.<locals>._wrapped_execution_chain	nested_function	lines 351-352	none-found	wrapped execution chain resolve deferred sync result	none
_patch_agno_async_tool_hook_chain	function	lines 360-383	none-found	patch agno build_nested_execution_chain_async FunctionCall	none
_patch_agno_async_tool_hook_chain.<locals>._patched_build_nested_execution_chain_async	nested_async_function	lines 368-380	none-found	patched build_nested_execution_chain_async wrapper	none
_patch_agno_async_tool_hook_chain.<locals>._wrapped_execution_chain	nested_async_function	lines 374-378	none-found	async wrapped execution chain deferred awaitable	none
_call_tool	async_function	lines 390-411	related-only	iscoroutinefunction asyncio.to_thread inspect.isawaitable tool entrypoint	src/mindroom/tool_approval.py:222; src/mindroom/timing.py:231; src/mindroom/api/sandbox_runner.py:474
_emit_after_call	async_function	lines 414-434	duplicate-found	ToolAfterCallContext emit after call hook context kwargs	src/mindroom/tool_system/tool_hooks.py:651; src/mindroom/hooks/execution.py:253; src/mindroom/hooks/context.py:577
_blocked_tool_result	async_function	lines 437-461	related-only	blocked declined tool result emit after hooks duration	src/mindroom/hooks/execution.py:292; src/mindroom/tool_approval.py:238; src/mindroom/approval_manager.py:274
_maybe_block_for_tool_approval	async_function	lines 464-515	related-only	request_tool_approval_for_call ToolApprovalCall blocked declined result	src/mindroom/tool_approval.py:238; src/mindroom/approval_manager.py:274; src/mindroom/tool_approval.py:199
_maybe_block_for_before_hooks	async_function	lines 518-564	related-only	ToolBeforeCallContext emit_gate declined decline_reason before hooks	src/mindroom/hooks/execution.py:292; src/mindroom/hooks/context.py:577; src/mindroom/hooks/execution.py:186
_execute_bridge	async_function	lines 567-736	duplicate-found	tool bridge execute before approval call after record failure success OAuth	src/mindroom/tool_system/tool_calls.py:349; src/mindroom/tool_system/tool_calls.py:385; src/mindroom/hooks/execution.py:253; src/mindroom/tool_approval.py:238
build_tool_hook_bridge	function	lines 739-796	related-only	build agno tool hook bridge sync async bridge has hooks	src/mindroom/agents.py:843; tests/test_tool_hooks.py:465
build_tool_hook_bridge.<locals>.bridge	nested_async_function	lines 750-762	related-only	async bridge delegates execute_bridge captured hook registry	src/mindroom/agents.py:843; tests/test_tool_hooks.py:474
build_tool_hook_bridge.<locals>.sync_bridge	nested_function	lines 764-793	related-only	sync bridge deferred async tool hook result run_coroutine_from_sync	src/mindroom/approval_transport.py:102; src/mindroom/sync_bridge_state.py:18
prepend_tool_hook_bridge	function	lines 799-813	none-found	prepend tool hook bridge toolkit functions async_functions preserving existing hooks	none
_prepend_function_tool_hook	function	lines 816-821	none-found	prepend function tool_hooks deduplicate bridge hooks existing hooks	none
```

# Findings

## 1. Hook context field assembly is repeated across tool-call and runtime custom-event paths

`_ResolvedToolContext.hook_context_kwargs` in `src/mindroom/tool_system/tool_hooks.py:103` builds the shared fields for `ToolBeforeCallContext` and `ToolAfterCallContext`.
`emit_custom_event` in `src/mindroom/tool_system/runtime_context.py:568` builds a similar hook context directly from `ToolRuntimeContext` plus `resolve_tool_runtime_hook_bindings` from `src/mindroom/tool_system/runtime_context.py:405`.
Both paths map runtime/config/correlation/Matrix helper bindings into hook-facing context fields.

The differences to preserve are important.
Tool hook contexts support detached dispatch without a live `ToolRuntimeContext`, allow `config` and `runtime_paths` to be `None`, carry tool `arguments`, and use `requester_id`.
Custom events require an active runtime context, use `sender_id`, include plugin payload fields, and currently set `plugin_name=""` with source plugin metadata.

## 2. Tool call record metadata forwarding is duplicated at the call sites

`_record_debug_tool_success` in `src/mindroom/tool_system/tool_hooks.py:245` forwards the same execution metadata fields that `record_tool_success` passes into `build_tool_success_record` in `src/mindroom/tool_system/tool_calls.py:500` and `src/mindroom/tool_system/tool_calls.py:385`.
The failure path in `_execute_bridge` at `src/mindroom/tool_system/tool_hooks.py:667` repeats the equivalent field mapping into `record_tool_failure`, whose record-builder shape is at `src/mindroom/tool_system/tool_calls.py:349`.

This is not literal copy/paste, but the same record metadata contract is hand-threaded in multiple places.
The difference to preserve is that `tool_hooks.py` owns resolving the live or detached execution context, while `tool_calls.py` owns sanitization and durable persistence.

## 3. After-call hook construction is duplicated in one special branch

Most after-call emissions go through `_emit_after_call` at `src/mindroom/tool_system/tool_hooks.py:414`.
The `OAuthConnectionRequired` branch in `_execute_bridge` constructs `ToolAfterCallContext` inline at `src/mindroom/tool_system/tool_hooks.py:651` instead of using `_emit_after_call`.

This is internal to the primary file rather than duplicated elsewhere in `./src`, but it is an active duplicate behavior path.
It should preserve the branch-specific behavior that OAuth-required results are treated as successful tool results with `error=None` and `blocked=False`.

# Proposed Generalization

No broad refactor recommended.

If this area is touched later, the smallest useful cleanup would be:

- Add a tiny helper in `tool_hooks.py` to build/persist a tool-call record from `_ResolvedToolContext`, `dispatch_context`, and the success/failure payload.
- Reuse `_emit_after_call` for the OAuth-required branch so all after-call context construction stays in one function.

Do not move `_ResolvedToolContext` into `runtime_context.py` unless detached dispatch and live runtime dispatch can share a typed hook-context projection without adding optional-field ambiguity.

# Risk/tests

The main behavioral risks are hook context field drift, missing Matrix helper bindings for live runtime calls, different `reply_to_event_id` resolution for detached calls, and success/failure record metadata drift between debug logging and durable tool-call persistence.

Tests needing attention for any cleanup:

- `tests/test_tool_hooks.py` for before/after hook ordering, declined calls, approval blocking, sync/async bridge behavior, and debug tool logging.
- `tests/test_issue_154_logging_integration.py` for tool-call record metadata and correlation IDs.
- `tests/test_dispatch_timing_instrumentation.py` for timing phases around bridge entry and tool entry.
- `tests/test_tool_output_files.py` and `tests/test_sandbox_proxy.py` where bridge prepending affects toolkit execution.
