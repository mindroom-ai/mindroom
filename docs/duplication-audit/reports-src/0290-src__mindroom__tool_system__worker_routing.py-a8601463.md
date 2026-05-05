Summary: Top duplication candidates are the async stream/contextvar wrapping pattern shared with `llm_request_logging.py` and `tool_system/runtime_context.py`, and the workspace-relative containment validation that overlaps with `workspaces.py`.
Worker key, worker scope, shared-only integration, and storage-root behavior appears centralized in `worker_routing.py`; other files mostly consume these helpers rather than duplicate them.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_AsyncClosableIterator	class	lines 45-49	duplicate-found	AsyncClosableIterator aclose async iterator close protocol	src/mindroom/llm_request_logging.py:32; src/mindroom/tool_system/runtime_context.py:51
_AsyncClosableIterator.aclose	async_method	lines 48-49	duplicate-found	aclose Protocol AsyncClosableIterator	src/mindroom/llm_request_logging.py:36; src/mindroom/tool_system/runtime_context.py:55
ToolExecutionIdentity	class	lines 53-65	related-only	ToolExecutionIdentity dataclass execution identity construction	src/mindroom/api/credentials.py:289; src/mindroom/knowledge/refresh_runner.py:884; src/mindroom/tool_system/runtime_context.py:326
_ResolvedWorkerExecution	class	lines 69-74	none-found	ResolvedWorkerExecution worker_scope execution_identity worker_key	none
ResolvedWorkerTarget	class	lines 78-92	none-found	ResolvedWorkerTarget worker target routing_agent_name private_agent_names	none
get_tool_execution_identity	function	lines 101-103	related-only	get_tool_execution_identity ContextVar current identity	src/mindroom/tool_system/runtime_context.py:395
active_tool_execution_identity	function	lines 106-112	related-only	active execution identity explicit or current context	src/mindroom/tool_system/runtime_context.py:416
tool_execution_identity	function	lines 116-122	related-only	contextmanager ContextVar set reset token	src/mindroom/llm_request_logging.py:240; src/mindroom/tool_system/runtime_context.py:590
run_with_tool_execution_identity	async_function	lines 125-132	related-only	run async operation inside context manager	src/mindroom/tool_system/runtime_context.py:347
stream_with_tool_execution_identity	function	lines 135-159	duplicate-found	stream context per anext aclose AsyncGenerator	src/mindroom/llm_request_logging.py:256; src/mindroom/tool_system/runtime_context.py:357
stream_with_tool_execution_identity.<locals>.wrapped_stream	nested_async_function	lines 142-157	duplicate-found	wrapped_stream bind context stream_factory anext aclose	src/mindroom/llm_request_logging.py:262; src/mindroom/tool_system/runtime_context.py:365
_normalize_worker_key_part	function	lines 162-164	none-found	normalize worker key part regex default	none
_normalize_worker_requester_part	function	lines 167-169	none-found	normalize worker requester part colon regex default	none
_normalize_worker_dir_part	function	lines 172-174	none-found	normalize worker dir part regex worker default	none
_identity_requester_key	function	lines 177-180	none-found	identity requester key normalize requester_id	none
build_tool_execution_identity	function	lines 183-207	related-only	build ToolExecutionIdentity runtime env CUSTOMER_ID ACCOUNT_ID	src/mindroom/api/credentials.py:289; src/mindroom/tool_system/runtime_context.py:326
resolve_worker_execution_scope	function	lines 210-237	related-only	resolve worker execution scope worker_key identity	src/mindroom/runtime_resolution.py:143
resolve_worker_target	function	lines 240-284	related-only	resolve worker target tenant account private names	src/mindroom/api/credentials.py:585; src/mindroom/oauth/service.py:57
build_worker_target_from_runtime_env	function	lines 287-303	none-found	build worker target runtime env CUSTOMER_ID ACCOUNT_ID	none
worker_scope_allows_shared_only_integrations	function	lines 306-308	none-found	worker_scope shared only integrations allowed shared None	none
requires_shared_only_integration_scope	function	lines 311-326	none-found	shared-only integration mcp server id tool name	none
supports_tool_name_for_worker_scope	function	lines 329-333	none-found	supports tool name worker scope shared only	none
unsupported_shared_only_integration_names	function	lines 336-352	none-found	unsupported shared only integration names worker scope	none
tool_stays_local	function	lines 355-357	none-found	local only shared integration tool names	none
unsupported_shared_only_integration_message	function	lines 360-376	none-found	unsupported shared only integration message worker_scope agent_name	none
resolve_worker_key	function	lines 379-406	none-found	resolve worker key v1 tenant shared user user_agent	none
resolve_unscoped_worker_key	function	lines 409-426	none-found	resolve unscoped worker key v1 unscoped tenant	none
require_worker_key_for_scope	function	lines 429-448	none-found	require worker key for scope failure_message	none
is_unscoped_worker_key	function	lines 451-454	related-only	parse worker key split v1 unscoped	src/mindroom/oauth/service.py:95
resolved_worker_key_scope	function	lines 457-465	related-only	parse worker key scope shared user user_agent unscoped	src/mindroom/oauth/service.py:95
worker_key_agent_name	function	lines 468-483	none-found	worker key agent name min parts by scope	none
resolve_execution_identity_for_worker_scope	function	lines 486-519	none-found	resolve execution identity shared scope tenant account	none
worker_dir_name	function	lines 522-529	related-only	worker_dir_name sha256 prefix dirname	src/mindroom/api/sandbox_exec.py:104; src/mindroom/workers/backends/local.py:304
worker_root_path	function	lines 532-537	none-found	worker root path workers worker_dir_name	none
shared_storage_root	function	lines 540-547	related-only	storage root expanduser resolve	src/mindroom/constants.py:320; src/mindroom/api/sandbox_exec.py:149
agent_state_root_path	function	lines 550-559	related-only	agent state root agents normalized agent name	src/mindroom/matrix/invited_rooms_store.py:21
private_instance_scope_root_path	function	lines 562-567	related-only	private instance scope root private_instances worker_dir_name	src/mindroom/runtime_resolution.py:92
_private_instance_state_root_path	function	lines 570-577	related-only	private instance state root agent under private scope	src/mindroom/runtime_resolution.py:128
_is_resolved_agent_state_root	function	lines 580-582	none-found	is resolved agent state root parent agents	none
_is_resolved_private_instance_scope_root	function	lines 585-589	none-found	is resolved private instance scope root parent private_instances	none
_is_resolved_worker_root	function	lines 592-594	none-found	is resolved worker root parent workers	none
visible_state_roots_for_worker_key	function	lines 597-632	none-found	visible state roots worker key private agent names	none
agent_workspace_root_path	function	lines 635-637	related-only	agent workspace root state root workspace	src/mindroom/workspaces.py:324; src/mindroom/tool_system/skills.py:266
agent_workspace_relative_path	function	lines 640-659	duplicate-found	validate normalize workspace relative path no absolute no dotdot no env	src/mindroom/workspaces.py:37; src/mindroom/workspaces.py:61; src/mindroom/tool_system/output_files.py:252
_resolve_agent_workspace_target	function	lines 662-670	duplicate-found	resolve target relative_to workspace root raise	src/mindroom/workspaces.py:37; src/mindroom/workspaces.py:96; src/mindroom/api/sandbox_worker_prep.py:197
resolve_agent_owned_path	function	lines 673-687	duplicate-found	resolve agent owned path workspace-relative containment	src/mindroom/workspaces.py:96; src/mindroom/tool_system/output_files.py:252
resolve_agent_state_storage_path	function	lines 690-700	none-found	resolve agent state storage path canonical durable root	none
```

Findings:

1. Async stream context binding is duplicated across three modules.
`stream_with_tool_execution_identity()` in `src/mindroom/tool_system/worker_routing.py:135` creates a stream under a context manager, binds the same context for every `anext()`, catches `StopAsyncIteration`, yields chunks outside the context token, and closes async generators/closable iterators under the same context.
`stream_with_llm_request_log_context()` in `src/mindroom/llm_request_logging.py:256` implements the same flow with `bind_llm_request_log_context()`.
`ToolRuntimeSupport.stream_in_context()` in `src/mindroom/tool_system/runtime_context.py:357` implements the same flow with `_tool_runtime_context_scope()`.
The `_AsyncClosableIterator` protocol is also copied in all three modules at `worker_routing.py:45`, `llm_request_logging.py:32`, and `runtime_context.py:51`.
Differences to preserve: `worker_routing.py` accepts a factory, `llm_request_logging.py` accepts an already-created async iterator and calls `__aiter__()`, and `runtime_context.py` is an instance method wrapping a factory.

2. Agent-owned workspace path validation partially duplicates generic workspace containment helpers.
`agent_workspace_relative_path()` in `src/mindroom/tool_system/worker_routing.py:640` rejects empty strings, env-variable references, absolute paths, and `..` components before `_resolve_agent_workspace_target()` resolves under the workspace root and checks `relative_to()`.
`resolve_relative_path_within_root()` and `resolve_relative_path_within_root_preserving_leaf()` in `src/mindroom/workspaces.py:37` and `src/mindroom/workspaces.py:61` already centralize "resolve a relative path under a root and reject escapes/symlinks" behavior.
`tool_system/output_files.py:252` performs a sibling workspace-output validation path, using `resolve_relative_path_within_root_preserving_leaf()` plus parent-component checks.
Differences to preserve: `agent_workspace_relative_path()` intentionally rejects `$` env-variable references and returns a lexical `Path` before joining to the agent workspace, while the `workspaces.py` helpers handle symlink escape checks and error wording for configured workspace paths.

Non-findings:

- Worker key construction and parsing are centralized.
Searches for `v1:`, `shared:user:user_agent`, `resolved_worker_key_scope`, `worker_key_agent_name`, and `worker_dir_name` found callers and validators, but no other implementation of the scoped worker-key grammar.
- Shared-only integration restriction logic is centralized.
`config/main.py`, `api/tools.py`, `api/credentials.py`, and `credentials.py` call `unsupported_shared_only_integration_names()` or related helpers rather than reimplementing the restriction set.
- Worker storage roots are centralized enough.
`runtime_resolution.py`, `sandbox_worker_prep.py`, `sandbox_exec.py`, and `credentials.py` consume worker-root and visible-root helpers, with some related but distinct path validation for runtime-specific sandbox mounts.

Proposed generalization:

1. Add a small helper in a focused module such as `mindroom.async_context_streams` that wraps an async iterator/factory with a caller-supplied context-manager factory.
2. Move the `_AsyncClosableIterator` protocol into that helper module.
3. Update the three stream wrappers to delegate to the helper while keeping their public functions and method signatures unchanged.
4. Leave agent workspace validation unchanged for now, or later introduce a narrow `validate_workspace_relative_literal()` helper in `workspaces.py` if another call site needs the same `$`/empty/absolute/`..` policy.

Risk/tests:

- The stream helper is behavior-sensitive because context tokens must not span `yield` points.
Tests should cover context binding during stream construction, each `anext()`, `StopAsyncIteration`, and `aclose()` for all three public wrappers.
- The workspace path logic should not be refactored without tests for empty paths, `$` references, absolute paths, `..`, symlink parents, and normal relative paths.
- No production code was edited for this audit.
