# Duplication Audit: src/mindroom/workspaces.py

## Summary

Top duplication candidates:

1. Path containment and symlink-escape validation is repeated in several source modules, with `workspaces.py` already serving as a partial shared helper.
2. Workspace-relative output path handling in `tool_system/output_files.py` duplicates part of the preserving-leaf containment walk from `workspaces.py`, but adds output-file-specific syntax, parent-type, and write-time checks.
3. Template traversal, workspace knowledge symlink management, and private workspace resolution are mostly unique to `workspaces.py`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ResolvedAgentWorkspace	class	lines 21-26	related-only	ResolvedAgentWorkspace workspace dataclass root context_files file_memory_path	src/mindroom/runtime_resolution.py:62; src/mindroom/runtime_resolution.py:244; src/mindroom/agents.py:1115
_EffectiveAgentWorkspace	class	lines 30-34	none-found	_EffectiveAgentWorkspace root_path template_dir context_files file_memory_path	none
resolve_relative_path_within_root	function	lines 37-58	duplicate-found	resolve relative path within root symlink escape relative_to is_relative_to	src/mindroom/runtime_resolution.py:92; src/mindroom/api/knowledge.py:74; src/mindroom/tools/file.py:39; src/mindroom/custom_tools/coding.py:350; src/mindroom/tool_system/worker_routing.py:662; src/mindroom/knowledge/manager.py:597
resolve_relative_path_within_root_preserving_leaf	function	lines 61-93	duplicate-found	preserving leaf workspace output path parent symlink relative path	src/mindroom/constants.py:864; src/mindroom/tool_system/output_files.py:252; src/mindroom/tool_system/output_files.py:295; src/mindroom/tool_system/output_files.py:316
resolve_workspace_relative_path	function	lines 96-108	related-only	resolve workspace relative path workspace root wrapper	src/mindroom/runtime_resolution.py:216; src/mindroom/runtime_resolution.py:234; src/mindroom/tool_system/worker_routing.py:640; src/mindroom/tool_system/worker_routing.py:673
validate_workspace_template_dir	function	lines 111-121	related-only	template_dir validate exists symlink template traversal	src/mindroom/config/main.py:797; src/mindroom/config/main.py:333; src/mindroom/knowledge/manager.py:632
_iter_workspace_template_entries	function	lines 124-136	related-only	template entries deterministic sorted iterdir no symlinks	src/mindroom/config/main.py:333; src/mindroom/knowledge/manager.py:632; src/mindroom/tool_system/skills.py:346
_iter_workspace_template_entries.<locals>._walk	nested_function	lines 128-133	related-only	recursive walk sorted iterdir no symlink	src/mindroom/knowledge/manager.py:639; src/mindroom/tool_system/skills.py:346
_copy_workspace_template	function	lines 139-162	none-found	copy template directory workspace force copy2 symlink safe	none
ensure_workspace_template	function	lines 165-176	related-only	ensure workspace template mind scaffold memory	src/mindroom/agents.py:133; src/mindroom/cli/config.py:153
_build_workspace_knowledge_links	function	lines 179-200	related-only	workspace knowledge links symlink overlap is_relative_to	src/mindroom/runtime_resolution.py:215; src/mindroom/api/knowledge.py:317
_remove_stale_workspace_knowledge_links	function	lines 203-217	none-found	remove stale symlink workspace knowledge desired protected unlink	none
_apply_workspace_knowledge_links	function	lines 220-234	none-found	apply symlink links symlink_to resolved target already exists	none
ensure_workspace_knowledge_links	function	lines 237-270	related-only	ensure workspace knowledge links knowledge root protected paths	src/mindroom/runtime_resolution.py:215
_private_root_name	function	lines 273-277	related-only	private root default agent_name_data config private root	src/mindroom/config/agent.py:130; src/mindroom/tool_system/worker_routing.py:635
_effective_workspace	function	lines 280-299	none-found	effective workspace private config template_dir context_files file memory	none
_template_unavailable_for_dedicated_worker	function	lines 302-308	related-only	dedicated worker missing template env flag sandbox runner	src/mindroom/config/main.py:797; tests/api/test_sandbox_runner_api.py:2906
_resolve_workspace	function	lines 311-384	related-only	resolve agent workspace state path private root context files memory	src/mindroom/runtime_resolution.py:177; src/mindroom/tool_system/worker_routing.py:673
resolve_agent_workspace_from_state_path	function	lines 387-404	related-only	resolve agent workspace from state path public wrapper	src/mindroom/runtime_resolution.py:207
```

## Findings

### 1. Repeated containment and symlink-escape validation

`src/mindroom/workspaces.py:37` resolves a root-relative path, walks each lexical component, rejects symlink components, resolves the candidate, and verifies it remains under the resolved root.
The same behavior is repeated in narrower forms elsewhere:

- `src/mindroom/api/knowledge.py:74` rejects absolute paths and `..`, resolves under a knowledge root, and raises HTTP-specific errors on escape.
- `src/mindroom/tools/file.py:39` and `src/mindroom/custom_tools/coding.py:350` resolve tool paths under a base directory and reject resolved escapes.
- `src/mindroom/tool_system/worker_routing.py:640` plus `src/mindroom/tool_system/worker_routing.py:662` validate workspace-relative agent-owned paths and then resolve them under an agent workspace.
- `src/mindroom/knowledge/manager.py:597` plus `src/mindroom/knowledge/manager.py:611` walk path components to reject symlinks and verify resolved containment.

The shared behavior is canonical "root plus relative path must not escape root", with varying error types and stricter syntax in some callers.
Differences to preserve: dashboard API functions need `HTTPException`, tool functions return strings or support `restrict_to_base_dir=False`, knowledge indexing requires existing files and semantic filters, and workspace resolution rejects symlink path components even when the final resolved candidate might still be contained.

### 2. Preserving-leaf output path validation overlaps with workspace path helper

`src/mindroom/workspaces.py:61` resolves a root-relative path without following the final path component, rejects absolute paths and `..`, rejects symlink parents, and verifies the resolved parent stays under root.
`src/mindroom/tool_system/output_files.py:252`, `src/mindroom/tool_system/output_files.py:295`, and `src/mindroom/tool_system/output_files.py:316` perform the same parent-preserving containment check for `mindroom_output_path`, then add output-specific rules: reject env/user expansion, reject empty/root destinations, reject existing leaf symlinks/directories, create parents one component at a time, and re-check at write time.

This is real functional overlap, but `output_files.py` already imports `resolve_relative_path_within_root_preserving_leaf`.
The remaining duplication is the second parent-component walk and write-time parent creation logic, which is not a direct replacement for the helper because it returns user-facing strings and handles races while creating directories.

### 3. Template and workspace knowledge-link behavior is workspace-specific

`validate_workspace_template_dir`, `_iter_workspace_template_entries`, and `_copy_workspace_template` have related traversal patterns in `src/mindroom/knowledge/manager.py:632` and `src/mindroom/tool_system/skills.py:346`, but those modules are scanning managed knowledge files or skill files, not copying a template tree while preserving directory entries and avoiding symlink traversal.

`_build_workspace_knowledge_links`, `_remove_stale_workspace_knowledge_links`, `_apply_workspace_knowledge_links`, and `ensure_workspace_knowledge_links` are also specific to exposing workspace-local knowledge aliases.
`src/mindroom/runtime_resolution.py:215` prepares inputs for this behavior, and `src/mindroom/api/knowledge.py:317` has a small generic path-overlap predicate, but no other source module manages the same desired/stale/protected symlink lifecycle.

## Proposed Generalization

Keep `workspaces.py` as the current source of truth for workspace containment helpers.

For a future cleanup, the only conservative refactor worth considering is a small generic helper for "resolve a user-supplied relative path under a root and adapt the error".
It could live in `src/mindroom/path_safety.py` or remain in `workspaces.py` if the helpers are intended only for workspace-adjacent code.
It would need parameters for allowing the leaf to be unresolved, rejecting env/user expansion, and error-message construction.

No refactor is recommended for template copying or workspace knowledge-link management.
Those flows are cohesive and not meaningfully duplicated elsewhere.

## Risk/tests

Path validation is security-sensitive.
Any consolidation should run focused tests around symlink escapes, `..`, absolute paths, missing paths, existing leaf symlinks, and concurrent parent creation.
Relevant existing coverage is in `tests/test_agents.py`, `tests/test_tool_output_files.py`, `tests/test_knowledge_manager.py`, `tests/api/test_knowledge_api.py`, `tests/test_coding_tools.py`, and `tests/test_workspace_env_hook.py`.

No production code was edited.
