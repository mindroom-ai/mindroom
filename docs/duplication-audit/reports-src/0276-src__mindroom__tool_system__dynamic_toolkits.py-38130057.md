# Duplication Audit: src/mindroom/tool_system/dynamic_toolkits.py

## Summary

No meaningful duplication found.
The module is the central implementation for dynamic toolkit session state and runtime tool-config merging.
Nearby code in `agents.py`, `custom_tools/dynamic_tools.py`, and `config/main.py` consumes or supplies this behavior rather than reimplementing it.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
DynamicToolkitSelection	class	lines 22-26	none-found	DynamicToolkitSelection loaded_toolkits runtime_tool_configs dataclass	src/mindroom/agents.py:895; src/mindroom/agents.py:1022
DynamicToolkitMergeError	class	lines 29-30	none-found	DynamicToolkitMergeError merge error dynamic toolkit conflict	none
DynamicToolkitConflictError	class	lines 33-52	none-found	DynamicToolkitConflictError conflicting overrides toolkit tool	src/mindroom/custom_tools/dynamic_tools.py:167
DynamicToolkitConflictError.__init__	method	lines 36-52	none-found	toolkit_name tool_name existing_overrides candidate_overrides conflict message	src/mindroom/custom_tools/dynamic_tools.py:168; src/mindroom/tool_system/metadata.py:528
_toolkit_scope_key	function	lines 55-60	none-found	session_id find :$ room thread scope key loaded toolkits	none
_ordered_loaded_toolkits	function	lines 63-68	related-only	ordered loaded toolkits allowed_toolkits preserve allowed order	src/mindroom/config/main.py:1545; src/mindroom/custom_tools/dynamic_tools.py:57
_initial_loaded_toolkits	function	lines 71-73	none-found	initial_toolkits allowed_toolkits ordered loaded toolkits	src/mindroom/agents.py:696; src/mindroom/custom_tools/dynamic_tools.py:60
_coerce_loaded_toolkits	function	lines 76-89	related-only	coerce list strings dedupe preserve order normalized string list	src/mindroom/ai.py:364; src/mindroom/attachments.py:103; src/mindroom/config/agent.py:380
_sanitize_loaded_toolkits	function	lines 92-109	related-only	allowed_toolkits config.toolkits scope incompatible invalid toolkits	src/mindroom/custom_tools/dynamic_tools.py:99; src/mindroom/config/main.py:1138
_normalize_effective_tool_config_overrides	function	lines 112-116	related-only	validate_authored_tool_entry_overrides tool_config_overrides runtime overrides	src/mindroom/config/main.py:1287; src/mindroom/api/sandbox_runner.py:1336; src/mindroom/tool_system/metadata.py:528
resolve_special_tool_names	function	lines 119-146	none-found	delegate self_config dynamic_tools allow_self_config MAX_DELEGATION_DEPTH	src/mindroom/agents.py:597; src/mindroom/agents.py:555
get_loaded_toolkits_for_session	function	lines 149-183	none-found	get loaded toolkits session init sanitize in-memory state	src/mindroom/custom_tools/dynamic_tools.py:50
save_loaded_toolkits_for_session	function	lines 186-195	none-found	save loaded toolkits session coerce in-memory state	src/mindroom/custom_tools/dynamic_tools.py:178; src/mindroom/custom_tools/dynamic_tools.py:237
clear_session_toolkits	function	lines 198-200	none-found	clear session toolkits pop scope key	none
merge_runtime_tool_configs	function	lines 203-253	related-only	merge static dynamic toolkit tool configs conflict overrides get_agent_tool_configs get_toolkit_tool_configs	src/mindroom/config/main.py:1354; src/mindroom/config/main.py:1368; src/mindroom/custom_tools/dynamic_tools.py:160
_inject_special_tool_configs	function	lines 256-276	related-only	append special tool configs if missing resolve_special_tool_names	src/mindroom/agents.py:597
resolve_dynamic_toolkit_selection	function	lines 279-302	none-found	resolve dynamic toolkit selection loaded toolkits runtime tool configs	src/mindroom/agents.py:895
```

## Findings

No real duplicated behavior was found.

Related code reviewed:

- `src/mindroom/config/main.py:1354` and `src/mindroom/config/main.py:1368` build static `ResolvedToolConfig` lists for toolkits and agents.
  `merge_runtime_tool_configs` composes those authoritative config results and adds conflict detection, so this is upstream config resolution rather than duplicate merge behavior.
- `src/mindroom/custom_tools/dynamic_tools.py:99` repeats user-facing load/unload precheck concepts such as unknown toolkit, not allowed toolkit, and scope incompatibility.
  This intentionally returns specific JSON statuses and messages for the agent tool, while `_sanitize_loaded_toolkits` silently drops invalid persisted state for runtime safety.
- `src/mindroom/agents.py:597` appends special tools for a static toolkit-name listing, using the shared `resolve_special_tool_names`.
  The actual special-tool decision logic is not duplicated.
- `src/mindroom/ai.py:364` and `src/mindroom/attachments.py:103` contain generic ordered string-list normalization patterns.
  `_coerce_loaded_toolkits` has the same broad shape, but its behavior is session-state coercion for a specific in-memory map and does not justify a cross-module helper on its own.

## Proposed Generalization

No refactor recommended.
The only overlap is generic ordered-deduplication and adjacent validation/precheck logic with different callers and error semantics.
Extracting a helper would add indirection without removing a meaningful duplicated dynamic-toolkit workflow.

## Risk/Tests

No production code was changed.
If this area is refactored later, tests should cover:

- session IDs with Matrix thread suffixes sharing a toolkit scope;
- invalid loaded toolkit state being dropped and persisted back sanitized;
- initial toolkits staying ordered by `allowed_toolkits`;
- runtime merge conflicts when static tools and loaded toolkits define the same tool with different overrides;
- `delegate`, `self_config`, and `dynamic_tools` special-tool injection ordering.
