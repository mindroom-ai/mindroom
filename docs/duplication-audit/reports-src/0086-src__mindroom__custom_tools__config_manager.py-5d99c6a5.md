## Summary

Top duplication candidates are the agent YAML rendering and self-update flow shared with `src/mindroom/custom_tools/self_config.py`, plus repeated config load/validate/persist patterns shared with `src/mindroom/commands/config_commands.py`.
Tool metadata listing overlaps conceptually with `src/mindroom/api/tools.py`, but the consumers and output formats differ enough that a shared refactor is not recommended from this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_is_known_tool_entry	function	lines 39-41	related-only	tool_metadata membership; Unknown tools; Invalid tools	src/mindroom/custom_tools/self_config.py:128-135; src/mindroom/api/sandbox_runner.py:1478-1481
_preserve_tool_overrides	function	lines 44-50	related-only	preserve tool overrides; ToolConfigEntry; agent.tools update	src/mindroom/custom_tools/self_config.py:14-18,160; src/mindroom/config/agent.py:300-319
validate_knowledge_bases	function	lines 53-80	related-only	duplicate knowledge bases; unknown knowledge bases; knowledge_bases validation	src/mindroom/custom_tools/self_config.py:150-153; src/mindroom/config/agent.py:363-371; src/mindroom/config/main.py:648-664
_save_runtime_validated_config	function	lines 83-86	duplicate-found	validate_with_runtime then persist_runtime_validated_config; apply config change	src/mindroom/custom_tools/self_config.py:211-213; src/mindroom/commands/config_commands.py:331-338; src/mindroom/api/config_lifecycle.py:220-229
_InfoType	class	lines 89-100	none-found	InfoType; info_type; available_models; agent_template	none
ConfigManagerTools	class	lines 103-977	related-only	Toolkit config_manager; self_config; manage_agent; manage_team	src/mindroom/custom_tools/self_config.py:31-218
ConfigManagerTools.__init__	method	lines 110-130	related-only	Toolkit __init__ name tools runtime_paths config_path	src/mindroom/custom_tools/self_config.py:34-41; src/mindroom/custom_tools/browser.py:251-255
ConfigManagerTools.get_info	method	lines 132-187	none-found	info_type dispatcher get_info available_tools agent_config	none
ConfigManagerTools.manage_agent	method	lines 189-262	related-only	manage agent create update validate dispatch operation	src/mindroom/custom_tools/self_config.py:65-218
ConfigManagerTools.manage_team	method	lines 264-285	none-found	create team config manage_team coordinate collaborate	none
ConfigManagerTools._load_mindroom_docs	method	lines 289-299	none-found	README.md load cache mindroom docs	none
ConfigManagerTools._load_help_text	method	lines 301-305	none-found	get_command_help cache help_text	none
ConfigManagerTools._load_config_or_error	method	lines 307-317	related-only	load_config_or_user_error tolerate_plugin_load_errors footer	src/mindroom/custom_tools/self_config.py:50-53,114-118; src/mindroom/commands/config_commands.py:188-196,317-325
ConfigManagerTools._load_config_and_tool_metadata_or_error	method	lines 319-337	related-only	load config and resolved_tool_metadata_for_runtime	src/mindroom/custom_tools/self_config.py:114-135; src/mindroom/api/tools.py:317-323
ConfigManagerTools._get_available_models	method	lines 339-374	none-found	Available Models configured models router model	none
ConfigManagerTools._format_schema_field	method	lines 376-395	none-found	model_json_schema field formatter required default enum	none
ConfigManagerTools._get_config_schema	method	lines 397-423	none-found	AgentConfig model_json_schema TeamConfig schema yaml	none
ConfigManagerTools._get_mindroom_info	method	lines 425-446	none-found	README Content Available Commands Key Concepts	none
ConfigManagerTools._agent_entries_for_scope	method	lines 448-472	related-only	get_available_agents_in_room runtime_context room agents scope	src/mindroom/authorization.py:67-102
ConfigManagerTools._list_agents	method	lines 474-499	none-found	Configured Agents No agents configured Role Tools Model	none
ConfigManagerTools._list_teams	method	lines 501-522	none-found	Configured Teams No teams configured Agents Mode	none
ConfigManagerTools._list_available_tools	method	lines 524-547	related-only	available tools by category tool metadata export tools	src/mindroom/api/tools.py:306-340
ConfigManagerTools._get_tool_details	method	lines 549-586	none-found	Tool details display_name config_fields dependencies docs_url	none
ConfigManagerTools._create_agent_config	method	lines 588-666	related-only	create AgentConfig validate tools validate knowledge bases save config	src/mindroom/custom_tools/self_config.py:126-153,179-213; src/mindroom/commands/config_commands.py:240-252
ConfigManagerTools._update_agent_config	method	lines 668-771	duplicate-found	update agent config field changes validate tools knowledge_bases save config	src/mindroom/custom_tools/self_config.py:126-218
ConfigManagerTools._create_team_config	method	lines 773-826	none-found	create TeamConfig validate agents mode save team	none
ConfigManagerTools._validate_agent_config	method	lines 828-887	related-only	validate agent display role tools model warnings issues	src/mindroom/config/main.py:648-664; src/mindroom/config/agent.py:363-371
ConfigManagerTools._get_agent_config	method	lines 889-903	duplicate-found	authored_model_dump yaml.dump Configuration for agent	src/mindroom/custom_tools/self_config.py:50-63; src/mindroom/commands/config_commands.py:199-218
ConfigManagerTools._generate_agent_template	method	lines 905-977	none-found	agent template type_to_category role_descriptions available tools category	none
```

## Findings

### 1. Agent config YAML rendering is duplicated

`ConfigManagerTools._get_agent_config` loads config, checks agent existence, calls `authored_model_dump()`, dumps YAML with `yaml.dump(default_flow_style=False, sort_keys=False)`, and wraps it in the same `## Configuration for ...` fenced YAML response at `src/mindroom/custom_tools/config_manager.py:889-903`.
`SelfConfigTools.get_own_config` performs the same behavior for `self.agent_name` at `src/mindroom/custom_tools/self_config.py:50-63`.

The only behavior difference is the missing-agent wording: config manager says `not found`, while self-config says `not found in configuration`.
The target agent name also comes from a parameter in one tool and instance state in the other.

### 2. Agent update flows duplicate the same edit pipeline

`ConfigManagerTools._update_agent_config` loads runtime config and tool metadata, checks agent existence, validates requested tools and knowledge bases, applies field updates, computes human-readable changes, persists with runtime validation, and maps validation/runtime errors to user-facing messages at `src/mindroom/custom_tools/config_manager.py:668-771`.
`SelfConfigTools.update_own_config` repeats the same pipeline for the current agent at `src/mindroom/custom_tools/self_config.py:114-218`.

Self-config has additional restrictions and fields that must be preserved: it blocks `config_manager`, optionally blocks inherited privileged default tools, supports `skills`, `show_tool_calls`, `thread_mode`, history fields, compression settings, and `context_files`, and validates by constructing a full `AgentConfig`.
Config manager currently supports create/update/validate operations for other agents and returns a slightly different success message.

### 3. Runtime-validated config persistence has repeated call-site choreography

`_save_runtime_validated_config` revalidates an authored config dump with `Config.validate_with_runtime()` and then calls `config_lifecycle.persist_runtime_validated_config()` at `src/mindroom/custom_tools/config_manager.py:83-86`.
`apply_config_change` performs the same validate-then-persist sequence inline at `src/mindroom/commands/config_commands.py:331-338`.
`SelfConfigTools.update_own_config` already imports and reuses the helper at `src/mindroom/custom_tools/self_config.py:211-213`, which confirms this behavior is shared beyond config manager.

The command handler starts from a raw dict, while config manager starts from a `Config` object.
That input-shape difference should be preserved if this is generalized.

## Proposed Generalization

1. Extract a small display helper such as `format_agent_config_yaml(agent_name: str, agent: AgentConfig) -> str` near config command display helpers or in a focused `custom_tools/config_formatting.py` module.
2. Extract the shared "validate tool names against resolved metadata" behavior into a helper that accepts an optional blocked-tool set and returns the existing error string shape.
3. Consider a small `apply_agent_updates(agent: AgentConfig, requested_updates: Sequence[tuple[str, object | None]])` helper that returns the validated agent plus formatted changes; keep self-config privilege checks outside it.
4. Move `_save_runtime_validated_config` to a neutral config lifecycle/helper module if `commands.config_commands` should reuse it directly.
5. Keep create-agent and create-team flows local for now because no equivalent creation flow was found elsewhere under `src`.

## Risk/tests

The main risk is changing user-visible tool response text, especially missing-agent errors and success/change summaries.
Tests should cover `ConfigManagerTools._get_agent_config`, `SelfConfigTools.get_own_config`, `ConfigManagerTools._update_agent_config`, and `SelfConfigTools.update_own_config` for unchanged output on no-op updates, invalid tools, invalid knowledge bases, successful updates, and validation failures.
For persistence helper movement, tests around `commands.config_commands.apply_config_change` and config-manager create/update should confirm runtime validation still rejects invalid runtime paths and plugin/tool metadata failures are still tolerated where currently requested.
