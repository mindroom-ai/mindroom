# Duplication Audit: src/mindroom/mcp/manager.py

## Summary

Top duplication candidates:

- `MCPServerManager.sync_servers` duplicates the desired-enabled-MCP-server filtering and dynamic registry reconciliation shape in `src/mindroom/mcp/registry.py`.
- `MCPServerManager._discover_catalog` duplicates remote-tool include/exclude filtering with `MindRoomMCPToolkit._filtered_tools`, with the important difference that manager-level filtering controls the cached catalog and toolkit-level filtering controls per-agent/runtime exposure.
- MCP function-name collision handling in `_discover_catalog`, `_function_name_collision_messages`, `_agent_collision_messages`, and `_apply_function_name_collision_errors` is related to collision checks in `src/mindroom/mcp/toolkit.py`, `src/mindroom/mcp/registry.py`, and `src/mindroom/tool_system/metadata.py`, but the scopes differ.
- `_toolkit_function_names` duplicates toolkit function enumeration logic from `src/mindroom/history/compaction.py`, and is related to direct `toolkit.functions` / `toolkit.async_functions` handling in `agents.py` and `sandbox_proxy.py`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MCPServerManager	class	lines 37-650	related-only	MCPServerManager manager lifecycle mcp state registry worker manager	src/mindroom/workers/manager.py:14; src/mindroom/mcp/registry.py:192; src/mindroom/orchestration/config_updates.py:195
MCPServerManager.__init__	method	lines 40-51	none-found	MCPServerManager init runtime_paths on_catalog_change states shutdown	none
MCPServerManager.has_server	method	lines 53-55	related-only	has_server tracked server id manager catalog registry	src/mindroom/mcp/registry.py:121
MCPServerManager.failed_server_ids	method	lines 57-63	none-found	failed_server_ids catalog None last_error usable catalog	none
MCPServerManager.get_catalog	method	lines 65-73	related-only	get_catalog cached catalog last_error not connected	src/mindroom/mcp/registry.py:123; src/mindroom/mcp/registry.py:150
MCPServerManager.get_catalog_for_tool	method	lines 75-81	related-only	get_catalog_for_tool mcp_server_id_from_tool_name not MCP tool	src/mindroom/mcp/registry.py:33; src/mindroom/orchestration/config_updates.py:180
MCPServerManager.sync_servers	async_method	lines 83-116	duplicate-found	desired enabled mcp servers reconcile state registry sync config.mcp_servers	src/mindroom/mcp/registry.py:184; src/mindroom/mcp/registry.py:192; src/mindroom/orchestration/config_updates.py:159
MCPServerManager.shutdown	async_method	lines 118-129	related-only	cancel refresh_task gather disconnect clear shutdown background tasks	src/mindroom/matrix/typing.py:80; src/mindroom/streaming_delivery.py:544
MCPServerManager.call_tool	async_method	lines 131-149	related-only	manager call_tool cached session refresh require catalog toolkit bridge	src/mindroom/mcp/toolkit.py:94; src/mindroom/tool_system/tool_hooks.py:390
MCPServerManager._call_tool_once_or_reconnect	async_method	lines 151-172	none-found	auto_reconnect call_tool reconnect MCPConnectionError MCPTimeoutError	none
MCPServerManager._call_tool_with_lock	async_method	lines 174-193	none-found	semaphore call_lock read connected last_error call once	none
MCPServerManager._call_tool_once	async_method	lines 195-215	related-only	session.call_tool read_timeout_seconds tool_result_from_call_result runtime exception	src/mindroom/mcp/toolkit.py:94; src/mindroom/mcp/results.py:107
MCPServerManager._refresh_server_catalog	async_method	lines 217-259	related-only	refresh catalog hash disconnect connect discover validate notify stale refresh_task	src/mindroom/mcp/registry.py:192; src/mindroom/orchestration/config_updates.py:159
MCPServerManager._connect_and_discover	async_method	lines 261-302	none-found	build_transport_handle AsyncExitStack ClientSession initialize discover wait_for startup timeout	none
MCPServerManager._connect_and_discover.<locals>.open_session_and_discover	nested_async_function	lines 265-277	none-found	open session discover nested ClientSession initialize list tools	none
MCPServerManager._discover_catalog	async_method	lines 304-372	duplicate-found	list_tools cursor include_tools exclude_tools validate function names catalog hash	src/mindroom/mcp/toolkit.py:65; src/mindroom/mcp/toolkit.py:82; src/mindroom/mcp/config.py:28
MCPServerManager._build_message_handler	method	lines 374-389	none-found	message_handler ServerNotification ToolListChangedNotification stale schedule refresh	none
MCPServerManager._build_message_handler.<locals>.handle_message	nested_async_function	lines 375-387	none-found	handle_message ServerNotification ToolListChangedNotification state stale	none
MCPServerManager._schedule_refresh_task	method	lines 391-419	related-only	schedule refresh_task create_task stale shutdown finally reschedule	src/mindroom/matrix/typing.py:80; src/mindroom/knowledge/watch.py:233
MCPServerManager._schedule_refresh_task.<locals>.refresh	nested_async_function	lines 398-417	related-only	refresh nested task log warning finally clear stale reschedule	src/mindroom/matrix/typing.py:80; src/mindroom/knowledge/watch.py:233
MCPServerManager._remove_server	async_method	lines 421-429	related-only	remove server cancel task gather disconnect state	src/mindroom/matrix/typing.py:86; src/mindroom/streaming_delivery.py:544
MCPServerManager._disconnect_state_when_idle	async_method	lines 431-433	none-found	disconnect state when idle call_lock write	none
MCPServerManager._disconnect_state	async_method	lines 435-453	related-only	AsyncExitStack aclose connected logger session None close_error	src/mindroom/mcp/manager.py:261; src/mindroom/streaming_delivery.py:544
MCPServerManager._require_state	method	lines 455-460	related-only	require state unknown key lookup raise KeyError	src/mindroom/mcp/registry.py:33
MCPServerManager._require_catalog_tool	method	lines 462-466	related-only	require catalog tool remote_name set cached catalog protocol error	src/mindroom/mcp/toolkit.py:65; src/mindroom/mcp/toolkit.py:82
MCPServerManager._function_name_collision_messages	method	lines 469-486	duplicate-found	function name collision local function names across servers error messages	src/mindroom/mcp/toolkit.py:82; src/mindroom/tool_system/metadata.py:1002; src/mindroom/mcp/registry.py:166
MCPServerManager._connected_catalog_server_ids	method	lines 488-492	none-found	connected catalog server ids catalog not None last_error None	none
MCPServerManager._agent_collision_messages	method	lines 494-517	duplicate-found	agent collision messages server_ids_by_function_name visible server ids function_name sets	src/mindroom/mcp/toolkit.py:82; src/mindroom/agents.py:398; src/mindroom/tool_system/metadata.py:1002
MCPServerManager._apply_function_name_collision_errors	async_method	lines 519-528	related-only	apply collision errors disconnect set catalog none last_error protocol error	src/mindroom/mcp/registry.py:166; src/mindroom/tool_system/metadata.py:1002
MCPServerManager._validate_global_function_names	async_method	lines 530-544	related-only	validate global function names connected servers agents collision errors lock	src/mindroom/mcp/registry.py:192; src/mindroom/tool_system/metadata.py:1002
MCPServerManager._configured_agent_tool_names	method	lines 546-553	related-only	configured agent tool names allowed_toolkits initial_toolkits get toolkit configs	src/mindroom/config/main.py:630; src/mindroom/config/main.py:1135
MCPServerManager._partition_tool_names	method	lines 556-567	related-only	partition tool names mcp_server_id_from_tool_name local names server ids	src/mindroom/mcp/registry.py:33; src/mindroom/orchestration/config_updates.py:180
MCPServerManager._agent_special_function_names	method	lines 570-584	none-found	delegate_task self config list_toolkits load_tools unload_tools special functions	none
MCPServerManager._tool_function_names_for_local_tools	method	lines 586-605	related-only	local tools get_tool_by_name skip exceptions toolkit function names	src/mindroom/agents.py:398; src/mindroom/history/compaction.py:819
MCPServerManager._configured_function_surface	method	lines 607-628	related-only	configured function surface ensure registry loaded agent tools special functions	src/mindroom/agents.py:398; src/mindroom/tool_system/metadata.py:992
MCPServerManager._toolkit_function_names	method	lines 631-643	duplicate-found	toolkit functions async_functions tools Function names enumerate	src/mindroom/history/compaction.py:819; src/mindroom/agents.py:398; src/mindroom/agents.py:874; src/mindroom/tool_system/sandbox_proxy.py:1038
MCPServerManager._wrap_runtime_exception	method	lines 645-650	related-only	wrap runtime exception MCPError TimeoutError connection error	src/mindroom/history/runtime.py:94; src/mindroom/custom_tools/matrix_room.py:110
```

## Findings

### 1. Enabled MCP server filtering and reconciliation are repeated

`MCPServerManager.sync_servers` builds `desired_servers` from `config.mcp_servers.items()` where `server_config.enabled` is true at `src/mindroom/mcp/manager.py:87`.
The same enabled-server projection exists in `src/mindroom/mcp/registry.py:184` as `_desired_server_entries`.
Both then reconcile an existing state against the desired set: manager removes absent live states at `src/mindroom/mcp/manager.py:91`, while registry removes absent dynamic tool entries at `src/mindroom/mcp/registry.py:204`.

Differences to preserve:

- `sync_servers` owns live sessions, semaphores, stale flags, catalog refresh, and validation.
- `sync_mcp_tool_registry` owns global `TOOL_METADATA`, `_TOOL_REGISTRY`, and `_MCP_TOOL_NAMES`.
- The shared behavior is only the enabled-server projection and set-based reconciliation shape, not the side effects.

### 2. MCP remote tool include/exclude filtering is duplicated at catalog and toolkit layers

`MCPServerManager._discover_catalog` filters `discovered_tools` using `server_config.include_tools` and `server_config.exclude_tools` at `src/mindroom/mcp/manager.py:321`.
`MindRoomMCPToolkit._filtered_tools` repeats the same include/exclude algorithm over `catalog.tools` at `src/mindroom/mcp/toolkit.py:65`.

Differences to preserve:

- Manager-level filtering applies server configuration before catalog hashing and before the tool becomes globally visible.
- Toolkit-level filtering applies per-toolkit runtime overrides and should not change the cached server catalog.
- The item attribute differs: manager checks `tool.name`, toolkit checks `tool.remote_name`.

This is the strongest duplication candidate because both paths implement the same allowlist/denylist precedence and could drift.

### 3. Function-name collision checks are repeated across MCP layers

`_discover_catalog` rejects duplicate function names inside one MCP server catalog at `src/mindroom/mcp/manager.py:337`.
`MindRoomMCPToolkit._register_catalog_tools` repeats a same-server duplicate-function-name check before registering async functions at `src/mindroom/mcp/toolkit.py:82`.
`_function_name_collision_messages` and `_agent_collision_messages` detect provider-visible collisions between MCP servers and local tools at `src/mindroom/mcp/manager.py:469` and `src/mindroom/mcp/manager.py:494`.
Related collision checks for dynamic MCP tool names against normal tool names live in `src/mindroom/mcp/registry.py:166` and `src/mindroom/tool_system/metadata.py:1002`.

Differences to preserve:

- Catalog duplicate checks operate on function names inside a single remote server.
- Agent collision checks operate on the agent-visible provider function surface.
- Registry/metadata collision checks operate on MindRoom tool names, not provider-visible function names.
- Toolkit duplicate checks are defensive because it consumes an already validated catalog, but still protects against constructed test catalogs or stale invalid state.

### 4. Toolkit function enumeration is duplicated

`MCPServerManager._toolkit_function_names` reads `toolkit.functions`, `toolkit.async_functions`, and then falls back to `toolkit.tools` names at `src/mindroom/mcp/manager.py:631`.
`src/mindroom/history/compaction.py:819` implements similar enumeration in `_toolkit_functions`, including fallback to `toolkit.tools` and inclusion of async functions.
Other code directly repeats parts of the same function-surface concept, including uniqueness logging in `src/mindroom/agents.py:398`, OpenAI approval pruning in `src/mindroom/agents.py:874`, and sandbox wrapping in `src/mindroom/tool_system/sandbox_proxy.py:1038`.

Differences to preserve:

- The MCP manager only needs function names and accepts a loose `object` because it validates tool instances created dynamically.
- History compaction needs actual `Function` objects.
- Agents and sandbox proxy intentionally mutate or inspect concrete `Toolkit` objects and should remain direct unless a helper improves clarity.

## Proposed Generalization

Refactor recommended for two narrow helpers only:

- Add a small MCP-local helper near `src/mindroom/mcp/toolkit.py` or a new focused `src/mindroom/mcp/tool_filters.py` only if more filtering sites appear.
  It would accept an iterable, `include_tools`, `exclude_tools`, and a name accessor, preserving the distinction between catalog-level and toolkit-level filtering.
- Add a shared tool-system helper that returns provider-visible toolkit function names or function objects, then reuse it from `_toolkit_function_names` and `history/compaction.py`.
  This should be typed against `Toolkit` if production call sites can keep concrete types.

No broad refactor is recommended for manager lifecycle, refresh tasks, reconnection, or MCP catalog discovery.
Those flows are specific to live MCP session ownership and are not meaningfully duplicated elsewhere.

## Risk/tests

Risks:

- Unifying include/exclude filtering must preserve the current denylist-before-allowlist behavior.
- Moving toolkit function enumeration could alter fallback behavior for `toolkit.tools` or precedence between sync and async functions.
- Collision helper extraction could accidentally mix tool-name collisions with provider-visible function-name collisions, which are separate namespaces.

Tests to cover before any future refactor:

- MCP catalog discovery with include-only, exclude-only, both include and exclude, and duplicate resolved function names.
- `MindRoomMCPToolkit` runtime filtering with string and list filters.
- Agent-visible collision validation between local tool functions, two MCP servers, and special functions such as `delegate_task`.
- Toolkit function enumeration for `functions`, `async_functions`, and fallback `tools`.
