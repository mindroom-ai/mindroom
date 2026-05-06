Summary: The main duplication candidates are backend-agnostic memory orchestration flows mirrored between `src/mindroom/memory/_file_backend.py` and `src/mindroom/memory/_mem0_backend.py`: visible-scope lookup, replica mutation, update/delete wrappers, and agent-plus-team search merging.
The file-specific markdown parsing and path layout helpers are mostly unique to the file backend.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_file_memory_root	function	lines 46-59	related-only	file memory root configured path resolve_config_relative_path default dirname	src/mindroom/memory/_policy.py:180; src/mindroom/knowledge/manager.py:611; src/mindroom/api/knowledge.py:74
_scope_dir_name	function	lines 62-63	related-only	re.sub safe name alphanumeric underscore default agent team names	src/mindroom/config/main.py:448; src/mindroom/mcp/config.py:23; src/mindroom/tool_system/plugin_identity.py:16
_scope_entrypoint_path	function	lines 66-67	none-found	FILE_MEMORY_ENTRYPOINT MEMORY.md path helper	none
_scope_daily_memory_dir	function	lines 70-71	none-found	FILE_MEMORY_DAILY_DIR memory dir helper	none
_resolve_scope_markdown_path	function	lines 74-83	related-only	resolve relative path within root markdown suffix path outside root	src/mindroom/api/knowledge.py:74; src/mindroom/knowledge/manager.py:611; src/mindroom/tools/file.py:42; src/mindroom/custom_tools/coding.py:1062
_scope_dir	function	lines 86-107	related-only	file memory scope path mkdir configured root agent_memory_scope_path	src/mindroom/memory/_policy.py:180; src/mindroom/memory/_mem0_backend.py:38; src/mindroom/knowledge/manager.py:611
_scope_markdown_files	function	lines 110-123	related-only	rglob markdown files entrypoint daily sorted relative path	src/mindroom/knowledge/manager.py:753; src/mindroom/api/knowledge.py:88
_load_scope_id_entries	function	lines 126-162	none-found	FILE_MEMORY_ENTRY_PATTERN read markdown id memory source_file line	id parser unique; checked src/mindroom/memory/_mem0_backend.py:99; src/mindroom/custom_tools/memory.py:121
_load_scope_entries_for_search	function	lines 166-171	not-a-behavior-symbol	timed wrapper load scope id entries	none
_extract_query_tokens	function	lines 174-175	none-found	re.findall query tokens lowercase alnum search score	none
_match_score	function	lines 178-185	none-found	token overlap score query tokens memory text	none
_format_entry_line	function	lines 188-190	related-only	normalize whitespace join strip split id markdown line	src/mindroom/matrix/message_builder.py:246; src/mindroom/memory/_file_backend.py:830
_append_scope_memory_entry	function	lines 193-230	related-only	append memory entry new_memory_id source_file metadata write_text	src/mindroom/memory/_mem0_backend.py:257; src/mindroom/memory/_mem0_backend.py:278; src/mindroom/memory/_mem0_backend.py:477
_search_scope_memory_entries	function	lines 233-284	related-only	search scope entries score dedupe snippets sort limit	src/mindroom/memory/_mem0_backend.py:70; src/mindroom/memory/_mem0_backend.py:89; src/mindroom/memory/_mem0_backend.py:303
_scan_scope_memory_snippets	function	lines 288-322	none-found	markdown snippet search skip headings id lines file:path:line	none
_get_scope_memory_by_path_id	function	lines 325-351	none-found	file:path:line memory id retrieve markdown line	none
_get_scope_memory_by_id	function	lines 354-366	related-only	get memory by id allowed scoped result backend get	src/mindroom/memory/_mem0_backend.py:99; src/mindroom/memory/_mem0_backend.py:366
_replace_scope_memory_entry	function	lines 369-402	related-only	update delete memory entry by id preserves indentation write lines	src/mindroom/memory/_mem0_backend.py:217; src/mindroom/memory/_mem0_backend.py:392; src/mindroom/memory/_mem0_backend.py:435
load_scope_entrypoint_context	function	lines 406-421	related-only	read text splitlines max lines strip entrypoint context	src/mindroom/response_runner.py:136; src/mindroom/custom_tools/coding.py:414; src/mindroom/matrix/visible_body.py:32
_find_file_replica_memory_ids	function	lines 424-448	duplicate-found	replica memory ids anchor memory metadata source_file unique match	src/mindroom/memory/_mem0_backend.py:133; src/mindroom/memory/_mem0_backend.py:141
_find_file_anchor_memory_result	function	lines 451-478	duplicate-found	allowed memory user ids storage paths anchor memory result	src/mindroom/memory/_mem0_backend.py:175; src/mindroom/memory/_mem0_backend.py:366
_file_mutation_target_ids	function	lines 481-495	duplicate-found	mutation target ids direct match replica fallback	src/mindroom/memory/_mem0_backend.py:199
_mutate_file_memory_targets	function	lines 498-530	duplicate-found	mutate memory targets storage paths dedupe target ids update delete	src/mindroom/memory/_mem0_backend.py:217
add_file_agent_memory	function	lines 533-550	related-only	add agent memory resolve storage append scope log	src/mindroom/memory/_mem0_backend.py:278; src/mindroom/memory/functions.py:119
append_agent_daily_file_memory	function	lines 553-582	none-found	daily file memory current date timezone append target_relative_path	none
_search_agent_file_scope_memories	function	lines 586-601	duplicate-found	agent scope memory search wrapper timed	src/mindroom/memory/_mem0_backend.py:70
_search_team_file_scope_memories	function	lines 605-620	duplicate-found	team scope memory search wrapper timed	src/mindroom/memory/_mem0_backend.py:89
search_file_agent_memories	function	lines 623-681	duplicate-found	search agent memories include team memories dedupe by memory sort limit	src/mindroom/memory/_mem0_backend.py:303
list_file_agent_memories	function	lines 684-704	related-only	list memories for agent resolve storage scope filter limit	src/mindroom/memory/_mem0_backend.py:344
get_file_agent_memory	function	lines 707-735	duplicate-found	get memory visible to caller allowed user ids storage paths	src/mindroom/memory/_mem0_backend.py:366
update_file_agent_memory	function	lines 738-777	duplicate-found	update memory anchor mutate targets not found log	src/mindroom/memory/_mem0_backend.py:392
delete_file_agent_memory	function	lines 780-818	duplicate-found	delete memory anchor mutate targets not found log	src/mindroom/memory/_mem0_backend.py:435
store_file_conversation_memory	function	lines 821-869	duplicate-found	store conversation memory effective storage paths team scope replica id log	src/mindroom/memory/_mem0_backend.py:477; src/mindroom/memory/functions.py:469
```

Findings:

1. Visible-memory lookup flow is duplicated across file and mem0 backends.
`get_file_agent_memory` and `_find_file_anchor_memory_result` iterate `get_allowed_memory_user_ids(...)`, then `storage_paths_for_scope_user_id(...)`, resolve or open the backend for each storage path, and return the first matching memory.
The same traversal appears in `_find_mem0_anchor_memory_result` and `get_mem0_agent_memory` at `src/mindroom/memory/_mem0_backend.py:175` and `src/mindroom/memory/_mem0_backend.py:366`.
The behavior is functionally the same: enumerate visible scope/storage targets and stop at the first backend-specific ID lookup.
Differences to preserve: file lookup resolves `FileMemoryResolution` and supports `file:path:line` synthetic IDs; mem0 lookup creates a `ScopedMemoryCrud` instance and enforces caller access inside `_get_scoped_memory_by_id`.

2. Replica mutation orchestration is duplicated across file and mem0 backends.
`_find_file_replica_memory_ids`, `_file_mutation_target_ids`, `_mutate_file_memory_targets`, `update_file_agent_memory`, and `delete_file_agent_memory` mirror `_find_mem0_replica_memory_ids`, `_mem0_mutation_target_ids`, `_mutate_mem0_memory_targets`, `update_mem0_agent_memory`, and `delete_mem0_agent_memory` in `src/mindroom/memory/_mem0_backend.py:141`, `src/mindroom/memory/_mem0_backend.py:199`, `src/mindroom/memory/_mem0_backend.py:217`, `src/mindroom/memory/_mem0_backend.py:392`, and `src/mindroom/memory/_mem0_backend.py:435`.
Both backends find an anchor, derive direct or replica target IDs, de-duplicate target IDs with `dict.fromkeys`, mutate each storage target, log success, and raise `MemoryNotFoundError` when no target was changed.
Differences to preserve: file replicas fall back to exact memory text plus optional `source_file`; mem0 prefers `MEM0_REPLICA_KEY` and only falls back to exact memory plus metadata when no replica key exists.

3. Agent-plus-team search merging is duplicated across file and mem0 backends.
`search_file_agent_memories` searches the agent scope first, then searches each team scope from `get_team_ids_for_agent`, skips duplicate memory text, sorts by score, and applies `limit`.
`search_mem0_agent_memories` performs the same agent-first/team-followup merge at `src/mindroom/memory/_mem0_backend.py:303`, except the mem0 backend searches all team scopes through one memory instance and does not re-sort by score.
Differences to preserve: file search resolves storage roots per team scope and has local token scoring/snippet fallback; mem0 delegates scoring and result order to mem0.

4. Safe path containment logic is related but not a clear dedupe candidate.
`_resolve_scope_markdown_path` resolves a candidate under a root, rejects paths outside the scope, and enforces a `.md` suffix.
Related root-containment checks exist in `src/mindroom/api/knowledge.py:74`, `src/mindroom/knowledge/manager.py:611`, `src/mindroom/tools/file.py:42`, and `src/mindroom/custom_tools/coding.py:1062`.
The shared behavior is root containment, but each caller has different error semantics and file-type constraints.

Proposed generalization:

1. Add a small private traversal helper in `src/mindroom/memory/_policy.py` or a new memory-private helper module that yields `(scope_user_id, target_storage_path)` for sorted allowed scopes.
2. Use that helper in both file and mem0 visible lookup paths, keeping backend-specific resolution and ID lookup callbacks local to each backend.
3. Add a backend-neutral mutation skeleton only if both backends can express target resolution and mutation as tiny callables; otherwise leave replica mutation duplicated because async mem0 and sync file operations differ enough to keep the current code readable.
4. Consider a shared helper for appending unique team memories by `memory` text, parameterized by whether final score sorting is required.
5. Do not generalize markdown parsing, synthetic `file:path:line` IDs, or daily file paths; those are file-backend-specific.

Risk/tests:

The main risk in deduplicating traversal is changing memory visibility across agent, team, and private runtime storage roots.
Tests should cover agent-owned memory lookup, team memory lookup, team reads of member memory, private agent storage roots, update/delete replica handling across multiple target storage paths, and file synthetic snippet IDs.
For search merging, tests should verify duplicate memory text is still suppressed and score ordering remains unchanged for the file backend.
