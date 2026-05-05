## Summary

Top duplication candidates for `src/mindroom/config/main.py`:

1. Git repository URL credential stripping is duplicated between config validation and knowledge management.
2. Worker-routed tool resolution is duplicated between `Config.get_agent_worker_tools()` and agent startup.
3. Path-overlap checks and worker-grantable credential defaulting have smaller helper-level duplication.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ConfigRuntimeValidationError	class	lines 102-108	related-only	ConfigRuntimeValidationError errors config UX	src/mindroom/cli/doctor.py:122; src/mindroom/cli/main.py:134; src/mindroom/api/config_lifecycle.py:140
ConfigRuntimeValidationError.errors	method	lines 105-108	related-only	ValidationError-like errors include_context	src/mindroom/cli/config.py:326; src/mindroom/api/config_lifecycle.py:126
iter_config_validation_messages	function	lines 120-132	related-only	iter_config_validation_messages ValidationError yaml user messages	src/mindroom/cli/config.py:326; src/mindroom/cli/doctor.py:123; src/mindroom/api/config_lifecycle.py:126
format_invalid_config_message	function	lines 135-145	related-only	format_invalid_config_message Invalid configuration footer	src/mindroom/commands/config_commands.py:252; src/mindroom/custom_tools/config_manager.py:663; src/mindroom/custom_tools/self_config.py:213
ResolvedRuntimeModel	class	lines 152-156	not-a-behavior-symbol	ResolvedRuntimeModel model_name context_window	none
AuthoredOptionalModel	class	lines 160-164	not-a-behavior-symbol	AuthoredOptionalModel tri-state optional model	none
StaticCompactionConfigSemantics	class	lines 168-173	not-a-behavior-symbol	StaticCompactionConfigSemantics compaction semantics	none
AvatarPromptsConfig	class	lines 176-185	not-a-behavior-symbol	AvatarPromptsConfig prompt fields	none
AvatarConfig	class	lines 188-196	not-a-behavior-symbol	AvatarConfig prompts config	none
_history_policy_from_limits	function	lines 199-208	none-found	HistoryPolicy num_history_runs num_history_messages ResolvedHistorySettings	src/mindroom/history/types.py:1; src/mindroom/config/main.py:1008
_normalize_optional_config_sections	function	lines 211-220	none-found	optional root sections normalize null plugins	src/mindroom/cli/config.py:322; src/mindroom/api/config_lifecycle.py:140
_normalized_config_data	function	lines 223-230	none-found	model_validator before normalized config data	src/mindroom/config/main.py:425; src/mindroom/config/main.py:926
_authored_optional_model	function	lines 233-239	none-found	AuthoredOptionalModel field_is_set model_fields_set	src/mindroom/config/main.py:554; src/mindroom/config/main.py:1078
_strip_empty_root_sections	function	lines 242-253	none-found	authored_model_dump strip empty root sections	src/mindroom/config/main.py:950; src/mindroom/custom_tools/config_manager.py:83
_effective_static_compaction_enabled	function	lines 256-270	none-found	compaction enabled override model clear model_fields_set	src/mindroom/config/main.py:568; src/mindroom/config/main.py:1091
_relative_paths_overlap	function	lines 273-275	duplicate-found	path overlap is_relative_to ancestor descendant	src/mindroom/api/knowledge.py:317; src/mindroom/workspaces.py:193; src/mindroom/orchestration/plugin_watch.py:150
KnowledgeBaseSourceSemantics	class	lines 279-286	not-a-behavior-symbol	KnowledgeBaseSourceSemantics git semantics	none
_credential_free_repo_url_for_config_validation	function	lines 289-316	duplicate-found	credential free repo url urlparse urlunparse repo_url	src/mindroom/knowledge/manager.py:361; src/mindroom/knowledge/redaction.py:24
_knowledge_base_source_semantics	function	lines 319-330	none-found	KnowledgeBaseSourceSemantics git_repo_identity branch lfs	src/mindroom/knowledge/manager.py:293; src/mindroom/knowledge/utils.py:203
_template_contains_overlapping_subtree	function	lines 333-340	related-only	template_dir rglob relative_to overlapping subtree	src/mindroom/workspaces.py:124; src/mindroom/workspaces.py:142
_skip_private_template_dir_validation	function	lines 343-349	none-found	MINDROOM_SANDBOX_RUNNER_MODE DEDICATED_WORKER template validation	src/mindroom/workspaces.py:302
Config	class	lines 352-1747	not-a-behavior-symbol	Config root model fields validators methods	none
Config.validate_raw_root_config	method	lines 425-427	none-found	validate_raw_root_config normalized root config	none
Config.normalize_plugins	method	lines 431-444	none-found	legacy string plugin entries path normalize plugins	src/mindroom/tool_system/plugin_imports.py:1
Config.validate_entity_names	method	lines 447-462	none-found	agent team mcp name pattern overlapping names	src/mindroom/mcp/config.py:1; src/mindroom/matrix_identifiers.py:1
Config.validate_agent_reply_permissions	method	lines 465-473	none-found	agent_reply_permissions known entities router wildcard	src/mindroom/authorization.py:1
Config.validate_delegate_to	method	lines 476-486	related-only	delegate_to unknown agent self delegation	src/mindroom/agent_policy.py:96; src/mindroom/custom_tools/delegate.py:87
Config.validate_toolkit_references	method	lines 489-503	related-only	allowed_toolkits initial_toolkits subset unknown toolkit	src/mindroom/custom_tools/dynamic_tools.py:99; src/mindroom/tool_system/dynamic_toolkits.py:101
Config.validate_team_agents	method	lines 506-510	none-found	team agents supported private requester state	src/mindroom/agent_policy.py:263
Config._invalid_compaction_model_references	method	lines 512-524	none-found	compaction.model unknown models static semantics	src/mindroom/config/main.py:604
Config._compaction_models_missing_context_window	method	lines 526-538	none-found	compaction model context_window missing	src/mindroom/config/main.py:604
Config._static_compaction_semantics	method	lines 540-601	none-found	static compaction semantics defaults agents teams	src/mindroom/config/main.py:512; src/mindroom/config/main.py:526
Config.validate_compaction_model_references	method	lines 604-618	none-found	compaction model references context_window validation	none
Config.validate_shared_only_integration_assignments	method	lines 621-646	none-found	shared-only integrations worker_scope unsupported_shared_only	src/mindroom/tool_system/worker_routing.py:1
Config.validate_knowledge_base_assignments	method	lines 649-664	related-only	knowledge_bases unknown assignments validate_knowledge_bases	src/mindroom/custom_tools/config_manager.py:53; src/mindroom/custom_tools/self_config.py:150
Config.validate_reserved_knowledge_base_ids	method	lines 667-679	none-found	private knowledge base reserved prefix validation	src/mindroom/agent_policy.py:355
Config.validate_knowledge_base_ids_are_path_safe	method	lines 682-696	none-found	knowledge base id path safe slash dot segments	src/mindroom/api/knowledge.py:515
Config.validate_knowledge_base_paths_do_not_overlap	method	lines 699-727	related-only	knowledge base paths overlap exact aliases source semantics	src/mindroom/api/knowledge.py:317; src/mindroom/runtime_resolution.py:223
Config.validate_private_knowledge	method	lines 730-746	none-found	private knowledge enabled path required	src/mindroom/config/agent.py:65
Config.validate_private_git_knowledge_paths	method	lines 749-794	related-only	git-backed private knowledge dedicated subtree memory template overlap	src/mindroom/workspaces.py:124; src/mindroom/api/knowledge.py:317
Config.validate_private_template_dirs	method	lines 797-812	related-only	private template_dir validate_workspace_template_dir	src/mindroom/workspaces.py:111; src/mindroom/workspaces.py:352
Config.validate_culture_assignments	method	lines 815-851	none-found	cultures agents one-to-one unknown agents	none
Config.validate_internal_user_username_not_reserved	method	lines 854-876	none-found	mindroom_user username conflicts agent localpart	src/mindroom/entity_resolution.py:1; src/mindroom/matrix_identifiers.py:1
Config.validate_root_space_alias_does_not_collide_with_managed_rooms	method	lines 879-900	none-found	root space alias managed room collision	src/mindroom/matrix_identifiers.py:1
Config.get_domain	method	lines 902-906	related-only	matrix_domain runtime_paths wrapper	src/mindroom/entity_resolution.py:1
Config.get_ids	method	lines 908-917	related-only	entity_matrix_ids wrapper agents teams	src/mindroom/entity_resolution.py:1
Config.get_mindroom_user_id	method	lines 919-923	related-only	mindroom_user_id wrapper runtime_paths	src/mindroom/entity_resolution.py:1
Config.validate_with_runtime	method	lines 926-948	related-only	validate_with_runtime tool entry runtime plugin load errors	src/mindroom/api/config_lifecycle.py:140; src/mindroom/custom_tools/config_manager.py:83
Config.authored_model_dump	method	lines 950-952	none-found	authored_model_dump exclude_unset strip empty	root none
Config.from_yaml	method	lines 955-972	duplicate-found	yaml.safe_load resolve_runtime_paths validate_with_runtime logging	src/mindroom/config/main.py:1750; src/mindroom/matrix/state.py:174; src/mindroom/api/sandbox_runner.py:159
Config.get_agent_culture	method	lines 974-979	none-found	get_agent_culture cultures agents membership	none
Config.get_agent	method	lines 981-998	related-only	Unknown agent Available agents lookup	src/mindroom/api/credentials.py:377; src/mindroom/custom_tools/delegate.py:87
Config.get_team	method	lines 1000-1006	none-found	Unknown team Available teams lookup	none
Config.get_default_history_settings	method	lines 1008-1018	none-found	default history settings history policy max tool calls	none
Config.get_entity_history_settings	method	lines 1020-1049	none-found	entity history settings agent team defaults fallback	none
Config.get_default_compaction_config	method	lines 1051-1055	related-only	compaction config model_dump model_validate defaults	src/mindroom/config/main.py:1061
Config.has_authored_default_compaction_config	method	lines 1057-1059	none-found	has authored default compaction	none
Config.get_entity_compaction_config	method	lines 1061-1098	related-only	merge compaction defaults override clear threshold tokens percent	src/mindroom/config/main.py:540
Config.has_authored_entity_compaction_config	method	lines 1100-1109	none-found	authored entity compaction default override unknown entity	none
Config.get_model_context_window	method	lines 1111-1114	related-only	model context_window lookup	src/mindroom/model_loading.py:1
Config.get_toolkit	method	lines 1116-1123	related-only	Unknown toolkit Available toolkits	src/mindroom/custom_tools/dynamic_tools.py:99
Config.get_toolkit_scope_incompatible_tools	method	lines 1125-1136	none-found	toolkit scope incompatible unsupported_shared_only	none
Config.get_agent_scope_incompatible_toolkits	method	lines 1138-1144	none-found	allowed_toolkits incompatible tools map	none
Config.get_agent_worker_tools	method	lines 1146-1165	duplicate-found	worker_tools defaults default_worker_routed_tools expand_tool_names	src/mindroom/agents.py:617
Config.get_worker_grantable_credentials	method	lines 1167-1172	duplicate-found	worker_grantable_credentials DEFAULT_WORKER_GRANTABLE_CREDENTIALS	src/mindroom/workers/runtime.py:138
Config.get_agent_execution_scope	method	lines 1174-1187	related-only	resolve_agent_policy_from_data effective_execution_scope	src/mindroom/agent_policy.py:1
Config.get_agent_scope_label	method	lines 1189-1202	related-only	resolve_agent_policy_from_data scope_label	src/mindroom/agent_policy.py:1
Config.get_agent_private_knowledge_base_id	method	lines 1204-1212	related-only	private_knowledge_base_id policy	src/mindroom/agent_policy.py:1
Config.get_private_knowledge_base_agent	method	lines 1214-1223	related-only	resolve_private_knowledge_base_agent build seeds	src/mindroom/agent_policy.py:355
Config.get_agent_knowledge_base_ids	method	lines 1225-1232	none-found	shared and private knowledge base ids	none
Config.get_knowledge_base_config	method	lines 1234-1267	related-only	synthetic private knowledge base config	src/mindroom/knowledge/registry.py:826; src/mindroom/api/knowledge.py:56
Config._validate_authored_tool_entry	method	lines 1269-1292	related-only	validate authored tool entry overrides unknown tool	src/mindroom/tool_system/metadata.py:81
Config._validate_authored_tool_entries	method	lines 1294-1311	none-found	resolved_tool_validation_snapshot_for_runtime authored entries	none
Config._validate_authored_tool_entries_with_snapshot	method	lines 1313-1352	none-found	defaults agents toolkits validation snapshot reserved tools	none
Config.get_toolkit_tool_configs	method	lines 1354-1366	related-only	ResolvedToolConfig apply_authored_overrides expand_tool_names	src/mindroom/tool_system/dynamic_toolkits.py:210
Config.get_agent_tool_configs	method	lines 1368-1391	related-only	ResolvedToolConfig defaults agent overrides expand_tool_names	src/mindroom/tool_system/dynamic_toolkits.py:210
Config.get_agent_tools	method	lines 1393-1395	related-only	effective tool names get_agent_tool_configs	src/mindroom/agents.py:604; src/mindroom/agent_descriptions.py:43
Config.get_agent_tool_runtime_overrides	method	lines 1397-1418	related-only	authored_tool_overrides_to_runtime get_tool_overrides	src/mindroom/tool_system/catalog.py:18
Config.get_private_agent_names	method	lines 1420-1424	none-found	private agent names private is not none	none
Config._agent_hard_dependency_tool_names	method	lines 1426-1432	none-found	hard dependency tool names initial_toolkits	none
Config.get_entities_referencing_tools	method	lines 1434-1444	none-found	entities referencing tools agents teams matching	none
Config.get_agent_delegation_closure	method	lines 1446-1460	related-only	get_agent_delegation_closure wrapper	src/mindroom/agent_policy.py:218
Config.get_private_team_targets	method	lines 1462-1476	related-only	get_private_team_targets wrapper	src/mindroom/agent_policy.py:240
Config.get_unsupported_team_agents	method	lines 1478-1497	related-only	get_unsupported_team_agents wrapper	src/mindroom/agent_policy.py:263
Config.unsupported_team_agent_message	method	lines 1500-1511	related-only	unsupported_team_agent_message wrapper	src/mindroom/agent_policy.py:280
Config.assert_team_agents_supported	method	lines 1513-1532	related-only	assert team agents supported unsupported message	src/mindroom/memory/_policy.py:47
Config.get_tool_preset	method	lines 1535-1537	none-found	TOOL_PRESETS get tool preset	none
Config.is_tool_preset	method	lines 1540-1542	none-found	is tool preset TOOL_PRESETS	none
Config.expand_tool_names	method	lines 1545-1559	related-only	expand tool names implied tools dedupe order	src/mindroom/tool_system/dynamic_toolkits.py:263
Config.get_agent_memory_backend	method	lines 1561-1568	related-only	memory backend agent override default	src/mindroom/memory/_policy.py:13; src/mindroom/memory/auto_flush.py:248
Config.uses_file_memory	method	lines 1570-1574	related-only	uses file memory any agent default	src/mindroom/memory/_policy.py:13; src/mindroom/memory/auto_flush.py:248
Config.uses_mem0_memory	method	lines 1576-1580	none-found	uses mem0 memory any agent default	src/mindroom/response_runner.py:2184
Config.get_all_configured_rooms	method	lines 1582-1594	none-found	all configured rooms agents teams rooms	src/mindroom/entity_resolution.py:1
Config.get_entity_thread_mode	method	lines 1596-1649	related-only	resolve_agent_thread_mode router_agents_for_room inheritance	src/mindroom/entity_resolution.py:1
Config.get_entity_model_name	method	lines 1651-1681	related-only	entity model router team agent unknown available	src/mindroom/entity_resolution.py:1
Config.get_effective_entity_model_name	method	lines 1683-1692	related-only	effective_entity_model_name wrapper room model	src/mindroom/entity_resolution.py:1
Config.resolve_runtime_model	method	lines 1694-1721	none-found	active model context_window room specific runtime model	none
Config.save_to_yaml	method	lines 1723-1747	related-only	yaml dump temp safe_replace save config	src/mindroom/api/config_lifecycle.py:1
load_config	function	lines 1750-1771	duplicate-found	yaml.safe_load validate_with_runtime loaded configuration logging	src/mindroom/config/main.py:955; src/mindroom/api/sandbox_runner.py:159
load_config_or_user_error	function	lines 1774-1787	related-only	load_config catch config errors format invalid config	src/mindroom/custom_tools/config_manager.py:313; src/mindroom/custom_tools/self_config.py:50
```

## Findings

### 1. Git repo credential stripping is duplicated

`_credential_free_repo_url_for_config_validation()` in `src/mindroom/config/main.py:289` is behaviorally the same as `_credential_free_repo_url()` in `src/mindroom/knowledge/manager.py:361`.
Both parse a repo URL, preserve scp-like or non-URL strings, strip URL params/query/fragment, preserve `ssh://git@host/...` style usernames, and remove credential-bearing userinfo for other URL forms.

Differences to preserve:

- The config helper is used for duplicate knowledge-root compatibility checks and names its output as a comparable identity.
- The knowledge manager helper is used for persistent Git config and clone/update behavior.
- The implementations are currently identical except for local variable naming and docstring intent.

### 2. Worker-routed tool resolution is duplicated

`Config.get_agent_worker_tools()` in `src/mindroom/config/main.py:1146` duplicates most of `_resolve_runtime_worker_tools()` in `src/mindroom/agents.py:617`.
Both resolve `agent_config.worker_tools`, fall back to `config.defaults.worker_tools`, expand configured presets, and otherwise load the tool registry before calling `default_worker_routed_tools()`.

Differences to preserve:

- `Config.get_agent_worker_tools()` computes the fallback from `self.get_agent_tools(agent_name)`.
- `_resolve_runtime_worker_tools()` accepts `runtime_tool_names`, which already include special runtime tools from `resolve_special_tool_names()`.
- This means the duplicate can only be collapsed if the config helper accepts an optional concrete runtime tool list or if agent startup remains the one place where special runtime tool names are included.

### 3. Path-overlap helpers are repeated

`_relative_paths_overlap()` in `src/mindroom/config/main.py:273` and `_path_overlaps()` in `src/mindroom/api/knowledge.py:317` both answer whether two paths are equal or ancestor/descendant via `Path.is_relative_to()`.
The API helper omits an explicit equality check, but equality is still covered because `Path.is_relative_to()` returns true for the same path.

Differences to preserve:

- The config helper is documented for relative paths and is used against private workspace subtrees.
- The API helper is used with resolved absolute paths when rejecting mutations under git-backed knowledge roots.

### 4. Worker-grantable credential defaulting is repeated

`Config.get_worker_grantable_credentials()` in `src/mindroom/config/main.py:1167` and `_resolve_worker_grantable_credentials()` in `src/mindroom/workers/runtime.py:138` both return `DEFAULT_WORKER_GRANTABLE_CREDENTIALS` when no explicit frozenset/list is provided.

Differences to preserve:

- The config method reads authored defaults from `self.defaults.worker_grantable_credentials`.
- The worker runtime helper accepts an already computed optional frozenset and should stay independent of full config loading.

## Proposed Generalization

1. Move the duplicated Git URL identity sanitizer into a shared knowledge/redaction helper, for example `mindroom.knowledge.redaction.credential_free_repo_url_identity()`, then import it from both config validation and knowledge manager.
2. Keep worker tool resolution in agent startup unless `Config.get_agent_worker_tools()` is changed to accept `runtime_tool_names`; otherwise extracting it would risk dropping special runtime tools.
3. Consider a tiny shared path helper only if more config/API path-overlap call sites appear; the current duplication is small enough that a refactor is optional.
4. Leave worker-grantable credential defaulting alone unless worker runtime starts accepting full config; the duplicate branch is only two lines and avoids coupling.

## Risk/tests

- Git URL sanitizer consolidation would need focused tests for HTTPS credentials, `ssh://git@host`, URL params/query/fragments, and non-URL scp-style strings.
- Worker tool resolution refactoring would need tests that special runtime tools remain eligible for default worker routing.
- Path helper consolidation should test equal paths, parent paths, child paths, and unrelated paths with both relative and resolved inputs.
- No production code was edited for this audit.
