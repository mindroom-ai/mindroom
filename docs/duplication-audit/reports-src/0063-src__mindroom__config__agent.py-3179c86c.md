## Summary

Top duplication candidates in `src/mindroom/config/agent.py` are repeated ordered duplicate detection for string lists, repeated `num_history_runs`/`num_history_messages` mutual-exclusion validation, and repeated knowledge chunk-size/chunk-overlap validation.
There are also smaller related patterns around authored tool-name projection and workspace-relative path validation, but those currently preserve different error contracts and call-site semantics.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_validate_safe_relative_path	function	lines 25-53	related-only	relative path absolute dotdot empty reserved workspace path validation	src/mindroom/tool_system/worker_routing.py:640; src/mindroom/workspaces.py:37; src/mindroom/workspaces.py:61; src/mindroom/tool_system/output_files.py:237; src/mindroom/knowledge/manager.py:561
AgentPrivateKnowledgeConfig	class	lines 56-104	related-only	private knowledge config chunk_size chunk_overlap watch git path	src/mindroom/config/knowledge.py:47; src/mindroom/config/main.py:730; src/mindroom/config/main.py:749
AgentPrivateKnowledgeConfig.validate_private_knowledge_path	method	lines 88-96	related-only	private knowledge path safe relative path validation	src/mindroom/tool_system/worker_routing.py:640; src/mindroom/knowledge/manager.py:561; src/mindroom/workspaces.py:96
AgentPrivateKnowledgeConfig.validate_chunking	method	lines 99-104	duplicate-found	chunk_overlap smaller than chunk_size validation	src/mindroom/config/knowledge.py:92
AgentPrivateConfig	class	lines 107-160	related-only	private root template_dir context_files workspace config	src/mindroom/workspaces.py:287; src/mindroom/workspaces.py:340; src/mindroom/config/main.py:797
AgentPrivateConfig.validate_private_root	method	lines 132-140	related-only	private.root reserved relative path validation	src/mindroom/workspaces.py:347; src/mindroom/tool_system/worker_routing.py:640
AgentPrivateConfig.validate_template_dir	method	lines 144-152	related-only	template_dir strip empty validation	src/mindroom/config/plugin.py:26; src/mindroom/mcp/config.py:16; src/mindroom/config/models.py:150
AgentPrivateConfig.validate_private_context_files	method	lines 156-160	related-only	private.context_files safe relative path list validation	src/mindroom/workspaces.py:360; src/mindroom/tool_system/worker_routing.py:640
AgentConfig	class	lines 163-377	related-only	agent config defaults team config history tools context_files	src/mindroom/config/models.py:292; src/mindroom/config/agent.py:391
AgentConfig.tool_names	method	lines 278-280	duplicate-found	tool_names return entry.name for entry in tools	src/mindroom/config/models.py:175; src/mindroom/config/models.py:431
AgentConfig.get_tool_overrides	method	lines 282-298	none-found	per-agent runtime overrides normalize_authored_tool_overrides TOOL_METADATA agent_override_fields	none
AgentConfig.authored_model_dump	method	lines 300-302	related-only	authored_model_dump model_dump exclude_unset	src/mindroom/config/main.py:950; src/mindroom/custom_tools/self_config.py:61; src/mindroom/custom_tools/config_manager.py:900
AgentConfig._check_history_config	method	lines 305-312	duplicate-found	num_history_runs num_history_messages mutually exclusive private worker_scope	src/mindroom/config/models.py:424; src/mindroom/config/agent.py:437
AgentConfig.reject_legacy_agent_fields	method	lines 316-335	related-only	reject legacy removed fields sandbox_tools memory_dir knowledge_base	src/mindroom/config/models.py:408
AgentConfig.validate_unique_tools	method	lines 339-341	related-only	validate_unique_tool_entries tools scope_name agent toolkit default	src/mindroom/config/models.py:180; src/mindroom/config/models.py:436; src/mindroom/config/models.py:187
AgentConfig.validate_unique_allowed_toolkits	method	lines 345-351	duplicate-found	_duplicate_items duplicate allowed_toolkits list validator	src/mindroom/config/agent.py:380; src/mindroom/config/auth.py:47; src/mindroom/config/matrix.py:142
AgentConfig.validate_unique_initial_toolkits	method	lines 355-361	duplicate-found	_duplicate_items duplicate initial_toolkits list validator	src/mindroom/config/agent.py:380; src/mindroom/config/auth.py:47; src/mindroom/config/matrix.py:142
AgentConfig.validate_unique_knowledge_bases	method	lines 365-371	duplicate-found	_duplicate_items duplicate knowledge_bases list validator	src/mindroom/custom_tools/config_manager.py:53; src/mindroom/config/agent.py:380
AgentConfig.validate_context_files	method	lines 375-377	related-only	agent_workspace_relative_path context_files workspace-relative validation	src/mindroom/tool_system/worker_routing.py:640; src/mindroom/workspaces.py:360
_duplicate_items	function	lines 380-388	duplicate-found	ordered duplicate detection preserving first duplicate order	src/mindroom/config/models.py:187; src/mindroom/config/auth.py:47; src/mindroom/custom_tools/config_manager.py:53; src/mindroom/config/matrix.py:142
TeamConfig	class	lines 391-443	related-only	team config agents rooms history validation	src/mindroom/config/agent.py:163; src/mindroom/teams.py:758
TeamConfig.validate_unique_agents	method	lines 429-435	duplicate-found	_duplicate_items duplicate agents team validator	src/mindroom/config/agent.py:380; src/mindroom/config/agent.py:458
TeamConfig.validate_history_settings	method	lines 438-443	duplicate-found	num_history_runs num_history_messages mutually exclusive team defaults agent	src/mindroom/config/models.py:424; src/mindroom/config/agent.py:305
CultureConfig	class	lines 446-464	related-only	culture config agents mode unique agents assignment	src/mindroom/config/main.py:831
CultureConfig.validate_unique_agents	method	lines 458-464	duplicate-found	_duplicate_items duplicate agents culture validator	src/mindroom/config/agent.py:380; src/mindroom/config/agent.py:429
```

## Findings

1. Ordered duplicate detection is repeated across config validation paths.
`_duplicate_items()` in `src/mindroom/config/agent.py:380` returns duplicate strings while preserving first duplicate encounter order, and five validators in the same file use it at `src/mindroom/config/agent.py:345`, `src/mindroom/config/agent.py:355`, `src/mindroom/config/agent.py:365`, `src/mindroom/config/agent.py:429`, and `src/mindroom/config/agent.py:458`.
The same functional loop appears for tool entries in `src/mindroom/config/models.py:187`, bridge aliases in `src/mindroom/config/auth.py:47`, and chat config-manager knowledge-base validation in `src/mindroom/custom_tools/config_manager.py:53`.
`src/mindroom/config/matrix.py:142` checks the same invariant but sorts duplicates, so ordering differences would need to be preserved or accepted deliberately.

2. History limit mutual exclusion is duplicated for defaults, agents, and teams.
`AgentConfig._check_history_config()` in `src/mindroom/config/agent.py:305`, `TeamConfig.validate_history_settings()` in `src/mindroom/config/agent.py:437`, and `DefaultsConfig._check_history_config()` in `src/mindroom/config/models.py:424` all reject simultaneous `num_history_runs` and `num_history_messages` with the same message.
`AgentConfig._check_history_config()` additionally rejects `private` together with `worker_scope`, so only the history-limit branch is duplicated.

3. Knowledge chunking validation is duplicated between private and shared knowledge config.
`AgentPrivateKnowledgeConfig.validate_chunking()` in `src/mindroom/config/agent.py:99` and `KnowledgeBaseConfig.validate_chunking()` in `src/mindroom/config/knowledge.py:92` both enforce `chunk_overlap < chunk_size`.
The only behavior difference is the error message prefix for the private nested field.

4. Tool-name projection is repeated but low risk.
`AgentConfig.tool_names()` in `src/mindroom/config/agent.py:277`, `ToolkitDefinition.tool_names()` in `src/mindroom/config/models.py:175`, and `DefaultsConfig.tool_names()` in `src/mindroom/config/models.py:431` all return `[entry.name for entry in self.tools]`.
This is small and readable, and the duplication is not a strong refactor target unless config helpers are already being touched.

5. Workspace-relative lexical path checks are related but not identical.
`_validate_safe_relative_path()` in `src/mindroom/config/agent.py:25` overlaps with `agent_workspace_relative_path()` in `src/mindroom/tool_system/worker_routing.py:640`, `resolve_relative_path_within_root_preserving_leaf()` in `src/mindroom/workspaces.py:61`, and output-file path validation in `src/mindroom/tool_system/output_files.py:237`.
The config helper supports reserved first components and configurable current-directory allowance, while runtime helpers resolve symlinks or reject environment/user expansion, so this should not be merged without a deliberate path-policy design.

## Proposed Generalization

1. Move `_duplicate_items()` to a small shared config utility, for example `src/mindroom/config/validation.py`, and reuse it from `agent.py`, `models.py`, `auth.py`, and `custom_tools/config_manager.py`.
Keep `matrix.py` unchanged unless sorted duplicate output is no longer required.

2. Add a tiny helper such as `validate_history_limit_choice(num_history_runs, num_history_messages) -> None` in the same config validation utility or near `_history_policy_from_limits`.
Call it from defaults, agent, and team validators, leaving the agent-only `private`/`worker_scope` check local.

3. Add a small `validate_chunking_bounds(chunk_size, chunk_overlap, field_prefix="") -> None` helper only if both shared and private knowledge models are edited together.
Parameterize the message prefix to preserve current validation text.

4. Leave `tool_names` properties and path validators as-is for now.
The duplication is either too small to justify indirection or has meaningful policy differences.

## Risk/tests

The main risk is changing validation error text or duplicate ordering in user-facing config errors.
Tests should cover duplicate ordering for agent toolkits, team agents, culture agents, tool entries, aliases, and config-manager knowledge-base edits.
History tests should assert that defaults, agents, and teams still reject simultaneous `num_history_runs` and `num_history_messages`.
Chunking tests should assert both shared knowledge and private knowledge reject `chunk_overlap >= chunk_size` with their current messages.
