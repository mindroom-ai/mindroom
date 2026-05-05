Summary: Top duplication candidates are config duplicate-item validation shared between `config.models` and `config.agent`, repeated `num_history_runs`/`num_history_messages` exclusivity checks across default/agent/team config, and the intentionally paired `CompactionConfig`/`CompactionOverrideConfig` field and validator shape.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ResolvedToolConfig	class	lines 16-20	related-only	ResolvedToolConfig usages and resolved tool config construction	src/mindroom/config/main.py:1354, src/mindroom/config/main.py:1368, src/mindroom/tool_system/dynamic_toolkits.py:210
StreamingConfig	class	lines 27-49	none-found	StreamingConfig streaming timing update_interval min_update_interval max_idle	src/mindroom/bot.py:343, src/mindroom/streaming.py
CoalescingConfig	class	lines 52-66	none-found	CoalescingConfig debounce_ms upload_grace_ms coalescing gate settings	src/mindroom/bot.py:343, src/mindroom/coalescing.py:676
DebugConfig	class	lines 69-73	none-found	DebugConfig log_llm_requests llm_request_log_dir	src/mindroom/llm_request_logging.py:338
_normalize_tool_entry_overrides	function	lines 76-86	none-found	tool entry overrides must be mapping normalize overrides	src/mindroom/config/models.py:89, src/mindroom/tool_system/catalog.py
_coerce_named_tool_entry	function	lines 89-96	none-found	explicit name overrides tool entry coerce	src/mindroom/config/models.py:132
_coerce_single_key_tool_entry	function	lines 99-119	none-found	single-key mapping tool entry overrides YAML form	src/mindroom/config/models.py:132
ToolConfigEntry	class	lines 122-161	none-found	ToolConfigEntry string single-key mapping serialize compact form	src/mindroom/custom_tools/config_manager.py:45, src/mindroom/config/agent.py:170
ToolConfigEntry.coerce_entry	method	lines 132-146	none-found	coerce tool entries strings mappings single-key	src/mindroom/config/models.py:76, src/mindroom/mcp/config.py:69
ToolConfigEntry.validate_name	method	lines 150-156	related-only	strip reject empty name validators	src/mindroom/credentials.py:34, src/mindroom/mcp/config.py:16, src/mindroom/config/agent.py:145
ToolConfigEntry.serialize	method	lines 159-161	none-found	model_serializer compact YAML no overrides	src/mindroom/config/main.py:242, src/mindroom/config/plugin.py
ToolkitDefinition	class	lines 164-184	related-only	ToolkitDefinition tools list tool_names unique tools	src/mindroom/config/agent.py:163, src/mindroom/config/main.py:1116
ToolkitDefinition.tool_names	method	lines 176-178	duplicate-found	tool_names property entry.name for self.tools	src/mindroom/config/agent.py:277, src/mindroom/config/models.py:431
ToolkitDefinition.validate_unique_tools	method	lines 182-184	duplicate-found	validate_unique_tools field validator validate_unique_tool_entries	src/mindroom/config/agent.py:337, src/mindroom/config/models.py:436
validate_unique_tool_entries	function	lines 187-203	duplicate-found	duplicate items preserving order seen duplicates validation	src/mindroom/config/agent.py:380, src/mindroom/config/agent.py:343, src/mindroom/config/matrix.py:145
_validate_compaction_threshold_choice	function	lines 206-213	none-found	threshold_tokens threshold_percent mutually exclusive compaction	src/mindroom/config/models.py:244, src/mindroom/config/models.py:282, src/mindroom/history/compaction.py:983
CompactionOverrideConfig	class	lines 216-251	duplicate-found	CompactionOverrideConfig CompactionConfig same fields enabled threshold reserve model	src/mindroom/config/models.py:254, src/mindroom/config/agent.py:203, src/mindroom/config/agent.py:407
CompactionOverrideConfig.validate_threshold_choice	method	lines 245-251	duplicate-found	validate threshold choice compaction override config	src/mindroom/config/models.py:282
CompactionConfig	class	lines 254-289	duplicate-found	CompactionConfig CompactionOverrideConfig same fields enabled threshold reserve model	src/mindroom/config/models.py:216, src/mindroom/config/main.py:1051, src/mindroom/config/main.py:1061
CompactionConfig.validate_threshold_choice	method	lines 283-289	duplicate-found	validate threshold choice compaction config	src/mindroom/config/models.py:244
DefaultsConfig	class	lines 292-466	related-only	default config fields overlap agent effective settings history worker tools	src/mindroom/config/agent.py:163, src/mindroom/config/main.py:1008, src/mindroom/agents.py:651
DefaultsConfig.reject_legacy_defaults_fields	method	lines 410-422	related-only	reject legacy fields removed Use instead	src/mindroom/config/agent.py:314
DefaultsConfig._check_history_config	method	lines 425-429	duplicate-found	num_history_runs num_history_messages mutually exclusive validator	src/mindroom/config/agent.py:304, src/mindroom/config/agent.py:437
DefaultsConfig.tool_names	method	lines 432-434	duplicate-found	tool_names property entry.name for self.tools	src/mindroom/config/agent.py:277, src/mindroom/config/models.py:175
DefaultsConfig.validate_unique_tools	method	lines 438-440	duplicate-found	validate_unique_tools default agent toolkit tools	src/mindroom/config/agent.py:337, src/mindroom/config/models.py:180
DefaultsConfig.validate_worker_grantable_credentials	method	lines 444-466	related-only	worker_grantable_credentials validate_service_name credential_service_policy	src/mindroom/credentials.py:375, src/mindroom/credentials.py:394, src/mindroom/config/main.py:1167
EmbedderConfig	class	lines 469-479	related-only	EmbedderConfig memory embedder model api_key host dimensions	src/mindroom/config/memory.py:14, src/mindroom/config/memory.py:179
ModelConfig	class	lines 482-499	related-only	ModelConfig provider id host api_key extra_kwargs context_window	src/mindroom/model_loading.py:46, src/mindroom/cli/doctor.py:289, src/mindroom/ai_run_metadata.py:40
RouterConfig	class	lines 502-510	none-found	RouterConfig model accept_invites startup_thread_prewarm	src/mindroom/config/main.py:391, src/mindroom/orchestrator.py
```

Findings:

1. Duplicate collection duplicate detection appears in two config modules.
   `src/mindroom/config/models.py:187` implements `validate_unique_tool_entries()` with a `seen` set and first-duplicate-order list.
   `src/mindroom/config/agent.py:380` implements `_duplicate_items()` with the same algorithm for string lists, then repeats the same raise pattern in validators at `src/mindroom/config/agent.py:343`, `src/mindroom/config/agent.py:353`, `src/mindroom/config/agent.py:363`, `src/mindroom/config/agent.py:427`, and `src/mindroom/config/agent.py:456`.
   These are functionally the same duplicate-detection behavior; the main difference is that `validate_unique_tool_entries()` operates on `ToolConfigEntry.name` and formats tool-specific messages, while `_duplicate_items()` operates on raw strings and leaves message text to callers.

2. History replay limit exclusivity is repeated for defaults, agents, and teams.
   `DefaultsConfig._check_history_config()` at `src/mindroom/config/models.py:425` rejects simultaneous `num_history_runs` and `num_history_messages`.
   `AgentConfig._check_history_config()` at `src/mindroom/config/agent.py:304` repeats the same condition and message before also checking a private/worker-scope rule.
   `TeamConfig.validate_history_settings()` at `src/mindroom/config/agent.py:437` repeats the same condition and message.
   The duplicated behavior is the exact cross-field validation for history limit mode; agent has an additional unrelated validation that should remain local.

3. Compaction default and override models intentionally duplicate most field shape and the threshold-choice validator.
   `CompactionOverrideConfig` at `src/mindroom/config/models.py:216` and `CompactionConfig` at `src/mindroom/config/models.py:254` both declare `enabled`, `threshold_tokens`, `threshold_percent`, `reserve_tokens`, `model`, and call `_validate_compaction_threshold_choice()`.
   Differences to preserve are important: override fields are nullable to represent "inherit/unset", while concrete defaults have non-null defaults for `enabled` and `reserve_tokens`.
   This is real structural duplication, but a refactor would need to avoid making authored override semantics less explicit.

4. Authored tool-name projection is repeated but small.
   `ToolkitDefinition.tool_names` at `src/mindroom/config/models.py:175`, `DefaultsConfig.tool_names` at `src/mindroom/config/models.py:431`, and `AgentConfig.tool_names` at `src/mindroom/config/agent.py:277` all return `[entry.name for entry in self.tools]`.
   This is duplicated behavior, but it is a one-line property in nearby Pydantic models and is currently clearer than an abstract mixin or helper.

Proposed generalization:

1. Add a small `duplicate_items(values: Iterable[str]) -> list[str]` helper in a config-focused utility module, or move `_duplicate_items()` to a public helper in `src/mindroom/config/agent.py` only if import direction stays clean.
2. Reimplement `validate_unique_tool_entries()` by projecting `entry.name` and using that duplicate helper, while keeping its existing error message.
3. Add a small `validate_history_limit_choice(num_history_runs, num_history_messages) -> None` helper near `_history_policy_from_limits()` or in the same config utility, then call it from defaults, agent, and team validators.
4. Leave `CompactionConfig` and `CompactionOverrideConfig` separate for now; the nullable-vs-concrete semantics are more valuable than eliminating the repeated field declarations.
5. Leave `tool_names` properties inline unless more config models acquire the same `tools: list[ToolConfigEntry]` shape.

Risk/tests:

Changing duplicate detection can affect validation error ordering and exact messages, so tests should cover duplicate default tools, agent tools, allowed/initial toolkits, team agents, culture agents, and knowledge bases.
Changing history-limit validation should preserve the exact current message for defaults, agents, and teams.
No production code was edited for this audit.
