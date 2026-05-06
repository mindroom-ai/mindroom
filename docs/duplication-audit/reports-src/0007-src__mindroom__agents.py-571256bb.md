# Duplication Audit: `src/mindroom/agents.py`

## Summary

Top duplication candidates:

- `_resolve_runtime_worker_tools` duplicates `Config.get_agent_worker_tools` in `src/mindroom/config/main.py:1148`.
- `create_agent` resolves history limits inline even though `Config.get_entity_history_settings` already centralizes the same effective policy in `src/mindroom/config/main.py:1020`.
- `_wrap_direct_agent_toolkit_for_output_files` repeats output-file policy construction already used by registered tool creation in `src/mindroom/tool_system/metadata.py:555`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_CachedCultureManager	class	lines 84-88	not-a-behavior-symbol	CultureManager cache signature cached manager	src/mindroom/agents.py:109; src/mindroom/agents.py:812
_CultureAgentSettings	class	lines 92-97	not-a-behavior-symbol	culture feature flags add_culture_to_context update_cultural_knowledge enable_agentic_culture	src/mindroom/agents.py:762; src/mindroom/teams.py:1352
_AdditionalContextChunk	class	lines 101-106	not-a-behavior-symbol	context chunk title body kind preload context	src/mindroom/agents.py:168; src/mindroom/workspaces.py:25
show_tool_calls_for_agent	function	lines 116-121	related-only	show_tool_calls defaults agent override	src/mindroom/bot.py:780; src/mindroom/response_runner.py:418; src/mindroom/ai.py:249; src/mindroom/teams.py:1918
_uses_default_mind_workspace_scaffold	function	lines 124-130	none-found	default mind workspace scaffold memory_backend context_files	none
_ensure_default_mind_workspace	function	lines 133-135	related-only	ensure_workspace_template agent_workspace_root_path workspace template	src/mindroom/workspaces.py:360
ensure_default_agent_workspaces	function	lines 138-142	related-only	ensure default agent workspaces mind scaffold	src/mindroom/orchestrator.py:17; src/mindroom/agents.py:994
_get_datetime_context	function	lines 145-165	none-found	Current Date and Time strftime Timezone ZoneInfo	none
_load_context_files	function	lines 168-198	related-only	context_files resolve_agent_owned_path resolve_config_relative_path read_text warning	src/mindroom/workspaces.py:360; src/mindroom/runtime_resolution.py:225; src/mindroom/knowledge/manager.py:215
_read_context_file	function	lines 202-203	related-only	read_text encoding utf-8 strip	src/mindroom/credentials_sync.py:57; src/mindroom/codex_model.py:233; src/mindroom/custom_tools/subagents.py:101
_render_context_chunks	function	lines 206-211	none-found	render context chunks markdown section	none
_render_additional_context	function	lines 214-216	none-found	Personality Context render context chunks	none
_build_preload_truncation_groups	function	lines 219-223	none-found	preload truncation groups personality chunks	none
_drop_whole_chunks	function	lines 226-241	none-found	drop whole chunks max_preload_chars omitted chars	none
_trim_chunk_tails	function	lines 244-261	none-found	trim chunk tails max_preload_chars overflow	none
_apply_preload_cap	function	lines 264-289	none-found	max_preload_chars Content truncated omitted chars	none
_build_additional_context	function	lines 293-326	related-only	additional context context_files workspace_context_files max_preload_chars	src/mindroom/workspaces.py:25; src/mindroom/config/models.py:379
_tool_supports_base_dir	function	lines 329-334	related-only	TOOL_METADATA config_fields base_dir	src/mindroom/tool_system/metadata.py:528; src/mindroom/tool_system/metadata.py:530
_tool_base_dir_override	function	lines 337-345	related-only	base_dir override workspace_path tool_init_overrides	src/mindroom/tool_system/metadata.py:529; src/mindroom/tool_system/metadata.py:539
_build_registered_agent_tool	function	lines 348-391	related-only	get_tool_by_name worker_target worker_tools_override allowed_shared_services	src/mindroom/tool_system/metadata.py:490; src/mindroom/tool_system/sandbox_proxy.py:774
_log_toolkits_without_unique_model_functions	function	lines 394-413	none-found	get_functions get_async_functions unique model functions collide	none
_wrap_direct_agent_toolkit_for_output_files	function	lines 416-428	duplicate-found	ToolOutputFilePolicy.from_runtime wrap_toolkit_for_output_files	src/mindroom/tool_system/metadata.py:555; src/mindroom/custom_tools/attachments.py:490; src/mindroom/api/sandbox_runner.py:1445
_load_agent_model_instance	function	lines 432-444	related-only	model_loading.get_model_instance timing agent create	src/mindroom/teams.py:1330
build_agent_toolkit	function	lines 448-594	related-only	special tools delegate self_config compact_context dynamic_tools get_tool_by_name	src/mindroom/tool_system/dynamic_toolkits.py:119; src/mindroom/tool_system/metadata.py:490; src/mindroom/mcp/manager.py:570
get_agent_toolkit_names	function	lines 597-614	related-only	resolve_special_tool_names append missing tool_names	src/mindroom/tool_system/dynamic_toolkits.py:256; src/mindroom/mcp/manager.py:570
_resolve_runtime_worker_tools	function	lines 617-634	duplicate-found	worker_tools defaults default_worker_routed_tools get_agent_worker_tools	src/mindroom/config/main.py:1148
_is_learning_enabled	function	lines 651-654	related-only	agent_config.learning defaults.learning is not False	src/mindroom/agent_storage.py:38
_resolve_agent_learning	function	lines 657-673	none-found	LearningMachine LearningMode UserProfileConfig UserMemoryConfig	none
_build_dynamic_tooling_instruction_block	function	lines 676-710	related-only	Dynamic Toolkits allowed toolkits currently loaded sticky initial	src/mindroom/custom_tools/dynamic_tools.py:22; src/mindroom/mcp/manager.py:570
_enable_all_history_replay	function	lines 713-715	related-only	num_history_runs None all history Team Agent	src/mindroom/teams.py:1370; src/mindroom/history/runtime.py:1479
remove_run_by_event_id	function	lines 718-759	related-only	MATRIX_EVENT_ID_METADATA_KEY MATRIX_SOURCE_EVENT_IDS_METADATA_KEY remove run	src/mindroom/turn_store.py:285; src/mindroom/turn_store.py:421; src/mindroom/history/interrupted_replay.py:223
_resolve_culture_settings	function	lines 762-780	none-found	CultureMode automatic agentic manual feature flags	none
_culture_signature	function	lines 783-784	none-found	culture signature mode description	none
_resolve_agent_culture	function	lines 788-834	none-found	CultureManager culture cache private cache culture storage	none
_load_agent_plugins	function	lines 838-839	related-only	load_plugins HookRegistryPlugin timed agent create	src/mindroom/tool_system/plugins.py
_build_agent_tool_hook_bridge	function	lines 843-859	related-only	HookRegistry.from_plugins build_tool_hook_bridge dispatch_context	src/mindroom/tool_system/tool_hooks.py:739
_prune_openai_approval_gated_tools	function	lines 862-891	none-found	tool_requires_approval_for_openai_compat hidden toolkit functions async_functions	none
_resolve_agent_dynamic_tool_selection	function	lines 895-907	related-only	resolve_dynamic_toolkit_selection loaded_toolkits runtime_tool_configs	src/mindroom/tool_system/dynamic_toolkits.py:279
_load_agent_skills	function	lines 911-923	related-only	build_agent_skills workspace_skills_root	src/mindroom/tool_system/skills.py:156
_initialize_agent_instance	function	lines 927-928	not-a-behavior-symbol	Agent constructor timed wrapper	src/mindroom/agents.py:1235
create_agent	function	lines 932-1273	duplicate-found	Agent Team history settings model loading output policy worker tools special tools	src/mindroom/teams.py:1300; src/mindroom/config/main.py:1020; src/mindroom/config/main.py:1148; src/mindroom/tool_system/metadata.py:555
get_agent_ids_for_room	function	lines 1276-1290	related-only	room agent ids router include config ids rooms	src/mindroom/matrix/rooms.py:411; src/mindroom/config/main.py:1582
get_rooms_for_entity	function	lines 1293-1316	related-only	entity rooms router all configured rooms teams agents	src/mindroom/bot.py:182; src/mindroom/config/main.py:1582; src/mindroom/config/main.py:1651
```

## Findings

### 1. Worker-routed tool resolution is duplicated

`src/mindroom/agents.py:617` implements the same policy as `Config.get_agent_worker_tools` in `src/mindroom/config/main.py:1148`.
Both resolve `agent_config.worker_tools`, fall back to `defaults.worker_tools`, expand configured aliases, and otherwise load the registry and call `default_worker_routed_tools`.

Difference to preserve: `agents.py` passes `runtime_tool_names` after dynamic toolkit expansion, while `Config.get_agent_worker_tools` uses `self.get_agent_tools(agent_name)`.
That difference matters because dynamic toolkits can add session-specific tools.

### 2. Agent history settings are resolved inline instead of using the config-level policy helper

`src/mindroom/agents.py:1207` manually selects `num_history_messages`, then `num_history_runs`, then defaults.
`src/mindroom/config/main.py:1020` already exposes `get_entity_history_settings`, which handles agent/team/default history policy and max tool-call fallback.
`src/mindroom/teams.py:1334` uses the config helper for Team construction.

Difference to preserve: `create_agent` needs the raw Agno constructor fields plus an `include_all_history` flag, while `get_entity_history_settings` returns a `HistoryPolicy`.
The config helper also carries `max_tool_calls_from_history`, which `create_agent` currently resolves separately at `src/mindroom/agents.py:1229`.

### 3. Output-file policy wrapping is repeated across direct and registered toolkit paths

`src/mindroom/agents.py:416` builds `ToolOutputFilePolicy.from_runtime(...)` and calls `wrap_toolkit_for_output_files(...)` for direct MindRoom-owned toolkits.
Registered tool creation repeats the same policy construction in `src/mindroom/tool_system/metadata.py:555`.
Other call sites also re-create the same policy shape, including `src/mindroom/custom_tools/attachments.py:490` and `src/mindroom/api/sandbox_runner.py:1445`.

Difference to preserve: `_wrap_direct_agent_toolkit_for_output_files` returns the wrapped toolkit, while the registered-tool path mutates and continues into sandbox wrapping.

### 4. Dynamic-tool capability visibility is partially mirrored

`src/mindroom/agents.py:597` and `src/mindroom/tool_system/dynamic_toolkits.py:256` both append special dynamic tools while preserving configured order.
This is mostly related rather than duplicated because `agents.py` delegates the special-tool resolution to `resolve_special_tool_names`.
However, `src/mindroom/mcp/manager.py:570` independently maps the same special-tool conditions to provider-visible function names for MCP metadata.

Difference to preserve: runtime tool injection deals in toolkit names like `dynamic_tools`, while MCP metadata exposes concrete function names like `list_toolkits`, `load_tools`, and `unload_tools`.

### 5. Matrix run metadata parsing is related but not a direct duplicate

`src/mindroom/agents.py:718` removes persisted runs by matching `MATRIX_EVENT_ID_METADATA_KEY` and string members of `MATRIX_SOURCE_EVENT_IDS_METADATA_KEY`.
`src/mindroom/turn_store.py:285` parses the same metadata keys into `_PersistedTurnMetadata`.
The behavior overlaps around source-event normalization, but `turn_store` also handles prompt maps and response event ids, so this is not a clean extraction candidate by itself.

Difference to preserve: `remove_run_by_event_id` must tolerate arbitrary Agno run objects and only mutates storage when a run is actually removed.

## Proposed Generalization

1. Replace `_resolve_runtime_worker_tools` with a config-level helper that accepts the concrete runtime tool names.
   A minimal option is `Config.resolve_worker_tools_for_runtime(agent_name, runtime_paths, runtime_tool_names)`, with `get_agent_worker_tools` calling it using `self.get_agent_tools(agent_name)`.
2. Add a tiny conversion helper near history config, for example `history_fields_from_settings(settings) -> tuple[int | None, int | None, bool]`, and use it in both `create_agent` and team construction.
3. Add a shared `wrap_toolkit_for_runtime_output_files(toolkit, workspace_root, runtime_paths)` helper in `mindroom.tool_system.output_files`.
   Keep sandbox-specific wrapping in `metadata.py`.

No broader refactor recommended.

## Risk/tests

- Worker-tool consolidation needs tests covering explicit `agent.worker_tools`, `defaults.worker_tools`, alias expansion, and dynamic-tool runtime names.
- History consolidation needs tests for `num_history_messages` taking precedence, `num_history_runs`, default fallback, and all-history mode for both agents and teams.
- Output-file wrapping consolidation needs tests proving direct custom tools and registered tools still write into the same workspace root and still omit wrapping when no workspace root is present.
