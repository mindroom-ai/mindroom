Summary: The primary duplication candidates are the context-scoped async stream wrapper in `ToolRuntimeSupport.stream_in_context`, which mirrors `worker_routing.stream_with_tool_execution_identity`, and plugin state-root resolution in `get_plugin_state_root`, which mirrors hook context state-root resolution.
Most other symbols are canonical runtime-context dataclasses, builders, or accessors with related consumers but no independent duplicate implementation found.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_AsyncClosableIterator	class	lines 52-56	duplicate-found	"_AsyncClosableIterator aclose AsyncGenerator stream close"	src/mindroom/tool_system/worker_routing.py:44
_AsyncClosableIterator.aclose	async_method	lines 55-56	duplicate-found	"aclose protocol async iterator explicit close"	src/mindroom/tool_system/worker_routing.py:48
_tool_runtime_context_scope	function	lines 60-63	related-only	"tool_runtime_context contextmanager set reset scoped operation"	src/mindroom/tool_system/worker_routing.py:115; src/mindroom/custom_tools/delegate.py:131
ToolRuntimeContext	class	lines 67-94	related-only	"ToolRuntimeContext dataclass runtime metadata room_id requester hook_registry attachment_ids"	src/mindroom/message_target.py:56; src/mindroom/custom_tools/matrix_helpers.py:22; src/mindroom/custom_tools/attachment_helpers.py:31
ToolDispatchContext	class	lines 98-124	related-only	"ToolDispatchContext execution_identity dataclass dispatch context"	src/mindroom/agents.py:1003; src/mindroom/tool_system/tool_hooks.py:122; src/mindroom/response_runner.py:568
ToolDispatchContext.from_target	method	lines 104-124	related-only	"from_target build_tool_execution_identity MessageTarget room thread session"	src/mindroom/tool_system/runtime_context.py:326; src/mindroom/message_target.py:56; src/mindroom/tool_system/worker_routing.py:188
LiveToolDispatchContext	class	lines 128-145	related-only	"LiveToolDispatchContext runtime_context execution_identity live dispatch"	src/mindroom/tool_system/tool_hooks.py:142; src/mindroom/tool_system/tool_hooks.py:152; src/mindroom/response_runner.py:573
LiveToolDispatchContext.__post_init__	method	lines 133-137	related-only	"execution_identity_matches_tool_runtime_context ValueError validate live dispatch"	src/mindroom/tool_system/tool_hooks.py:159; src/mindroom/tool_system/runtime_context.py:448
LiveToolDispatchContext.from_runtime_context	method	lines 140-145	related-only	"from_runtime_context build_execution_identity_from_runtime_context"	src/mindroom/tool_system/tool_hooks.py:145; src/mindroom/tool_system/tool_hooks.py:164; src/mindroom/tool_system/runtime_context.py:301
ToolRuntimeHookBindings	class	lines 149-156	related-only	"ToolRuntimeHookBindings message_sender room_state_querier message_received_depth"	src/mindroom/hooks/context.py:270; src/mindroom/hooks/context.py:590; src/mindroom/hooks/context.py:680
WorkerProgressEvent	class	lines 160-165	related-only	"WorkerProgressEvent tool_name function_name progress queue"	src/mindroom/streaming_delivery.py:341; src/mindroom/tool_system/sandbox_proxy.py:831
WorkerProgressPump	class	lines 169-174	related-only	"WorkerProgressPump loop queue shutdown Event"	src/mindroom/streaming.py:1099; src/mindroom/streaming_delivery.py:341; src/mindroom/streaming_delivery.py:370
ToolRuntimeSupport	class	lines 178-382	related-only	"ToolRuntimeSupport build_context build_dispatch run_in_context stream_in_context"	src/mindroom/bot.py:409; src/mindroom/response_runner.py:355; src/mindroom/turn_store.py:83
ToolRuntimeSupport.build_context	method	lines 190-238	none-found	"ToolRuntimeContext construction client event_cache hook_context MessageTarget"	none
ToolRuntimeSupport.build_required_context	method	lines 240-266	none-found	"build_context none raise live Matrix tool dispatch initialized runtime support"	none
ToolRuntimeSupport.build_dispatch_context	method	lines 268-299	related-only	"build_dispatch_context build_execution_identity LiveToolDispatchContext ToolDispatchContext"	src/mindroom/tool_system/tool_hooks.py:142; src/mindroom/tool_system/tool_hooks.py:152
ToolRuntimeSupport.build_required_live_dispatch_context	method	lines 301-324	none-found	"build_required_live_dispatch_context build_required_context LiveToolDispatchContext"	none
ToolRuntimeSupport.build_execution_identity	method	lines 326-345	related-only	"build_tool_execution_identity channel matrix transport_agent_name runtime_paths target"	src/mindroom/tool_system/runtime_context.py:432; src/mindroom/tool_system/worker_routing.py:188; src/mindroom/bot.py:1914; src/mindroom/turn_store.py:364
ToolRuntimeSupport.run_in_context	async_method	lines 347-355	duplicate-found	"run_in_context operation contextmanager await operation tool_execution_identity"	src/mindroom/tool_system/worker_routing.py:125; src/mindroom/response_runner.py:565
ToolRuntimeSupport.stream_in_context	method	lines 357-382	duplicate-found	"stream_in_context wrapped_stream anext StopAsyncIteration aclose contextmanager"	src/mindroom/tool_system/worker_routing.py:135
ToolRuntimeSupport.stream_in_context.<locals>.wrapped_stream	nested_async_function	lines 365-380	duplicate-found	"wrapped_stream stream_factory anext StopAsyncIteration AsyncGenerator aclose"	src/mindroom/tool_system/worker_routing.py:142
get_tool_runtime_context	function	lines 395-397	related-only	"get_tool_runtime_context current context ContextVar get"	src/mindroom/tool_system/worker_routing.py:101; src/mindroom/custom_tools/attachments.py:405; src/mindroom/history/compaction.py:237
get_worker_progress_pump	function	lines 400-402	related-only	"get_worker_progress_pump ContextVar get progress pump"	src/mindroom/tool_system/sandbox_proxy.py:831; src/mindroom/streaming_delivery.py:341
resolve_tool_runtime_hook_bindings	function	lines 405-413	related-only	"resolve hook bindings room_state_querier build_hook_room_state_querier"	src/mindroom/tool_system/tool_hooks.py:176; src/mindroom/history/compaction.py:237; src/mindroom/hooks/context.py:590
resolve_current_session_id	function	lines 416-429	related-only	"resolve session_id execution_identity runtime_context current session"	src/mindroom/custom_tools/compact_context.py:41; src/mindroom/ai_run_metadata.py:302
build_execution_identity_from_runtime_context	function	lines 432-445	related-only	"build execution identity from runtime context MessageTarget.from_runtime_context"	src/mindroom/tool_system/runtime_context.py:326; src/mindroom/message_target.py:56; src/mindroom/tool_system/tool_hooks.py:145
execution_identity_matches_tool_runtime_context	function	lines 448-467	related-only	"execution_identity matches runtime context valid_thread_ids tenant account transport"	src/mindroom/tool_system/tool_hooks.py:159; src/mindroom/tool_system/runtime_context.py:133
runtime_context_from_dispatch_context	function	lines 470-474	related-only	"runtime_context_from_dispatch_context isinstance LiveToolDispatchContext runtime_context"	src/mindroom/response_runner.py:573; src/mindroom/response_runner.py:973; src/mindroom/response_runner.py:1770
build_scheduling_runtime_from_tool_runtime_context	function	lines 477-492	related-only	"SchedulingRuntime from tool runtime context client config room caches matrix_admin"	src/mindroom/custom_tools/scheduler.py:48; src/mindroom/custom_tools/scheduler.py:76
attachment_id_available_in_tool_runtime_context	function	lines 495-505	related-only	"attachment_id_available strip attachment_ids runtime_attachment_ids"	src/mindroom/custom_tools/attachments.py:81; src/mindroom/custom_tools/attachments.py:116
list_tool_runtime_attachment_ids	function	lines 508-514	related-only	"list attachment ids preserve order dedupe attachment_ids runtime_attachment_ids"	src/mindroom/custom_tools/attachments.py:78; src/mindroom/dispatch_handoff.py:231
append_tool_runtime_attachment_id	function	lines 517-530	related-only	"append runtime attachment id strip dedupe current context"	src/mindroom/custom_tools/attachments.py:196; src/mindroom/custom_tools/attachments.py:647
get_plugin_state_root	function	lines 533-549	duplicate-found	"plugin state root storage_root plugins validate_plugin_name mkdir"	src/mindroom/hooks/context.py:63; src/mindroom/hooks/context.py:288; src/mindroom/hooks/context.py:608; src/mindroom/hooks/context.py:698
emit_custom_event	async_function	lines 552-587	related-only	"emit custom event hook_registry has_hooks CustomEventContext source_plugin payload"	src/mindroom/tool_system/tool_hooks.py:750; src/mindroom/hooks/context.py:270; src/mindroom/hooks/types.py:165
tool_runtime_context	function	lines 591-597	related-only	"ContextVar set reset contextmanager tool runtime context"	src/mindroom/tool_system/worker_routing.py:115; src/mindroom/custom_tools/delegate.py:131
worker_progress_pump_scope	function	lines 601-612	related-only	"worker_progress_pump_scope queue shutdown Event ContextVar set reset"	src/mindroom/streaming.py:1099; src/mindroom/streaming_delivery.py:370; src/mindroom/tool_system/worker_routing.py:115
```

Findings:

1. `ToolRuntimeSupport.stream_in_context` duplicates the generic async stream context-boundary wrapper in `src/mindroom/tool_system/worker_routing.py:135`.
Both implementations create the stream inside a context manager, re-enter the context for each `anext`, stop on `StopAsyncIteration`, and close `AsyncGenerator` or `_AsyncClosableIterator` streams inside the same context.
The behavior differs only by which context manager is applied: `tool_runtime_context` in `runtime_context.py` and `tool_execution_identity` in `worker_routing.py`.
`ToolRuntimeSupport.run_in_context` similarly mirrors `run_with_tool_execution_identity` at `src/mindroom/tool_system/worker_routing.py:125`, but the duplication is small; the stream wrapper is the meaningful repeated behavior.

2. `_AsyncClosableIterator` is duplicated literally in `src/mindroom/tool_system/runtime_context.py:52` and `src/mindroom/tool_system/worker_routing.py:44`.
Both protocols define the same minimal `aclose()` contract solely to support safe stream cleanup.

3. `get_plugin_state_root` duplicates hook plugin state-root resolution in `src/mindroom/hooks/context.py:63`.
Both validate the plugin name, build `runtime_paths.storage_root / "plugins" / normalized_plugin_name`, create the directory, and return it.
The error message and runtime-path source differ: hook contexts require an explicit `runtime_paths`, while tool runtime code can fall back to the active `ToolRuntimeContext`.

Proposed generalization:

1. Add one small helper in `src/mindroom/tool_system/worker_routing.py` or a neutral utility module that wraps an async iterator factory with an arbitrary context manager factory.
Then have both `stream_with_tool_execution_identity` and `ToolRuntimeSupport.stream_in_context` call it.
Keep the existing public functions unchanged.

2. Move the duplicated `_AsyncClosableIterator` protocol next to that stream helper and reuse it from both callers.

3. Extract the shared plugin state-root path creation into a neutral helper, for example `mindroom.tool_system.plugin_state.resolve_plugin_state_root(runtime_paths, plugin_name, *, missing_message)`, or export the existing hook helper if its module boundary is acceptable.
Keep `get_plugin_state_root` responsible only for resolving `runtime_paths` from the ambient tool context.

Risk/tests:

The stream wrapper is sensitive to `ContextVar` token lifetime.
Tests should cover that the context is active during stream construction, each yielded `anext`, and `aclose`, but does not leak across yields.

The plugin state-root change should preserve both error messages or update tests that assert them.
Tests should cover plugin name normalization, directory creation, and the no-active-runtime/no-explicit-runtime error path.

No refactor was performed because the task explicitly prohibited production-code edits.
