Summary: One meaningful duplication candidate found.
`src/mindroom/memory/_policy.py` centralizes memory scope IDs and storage-root resolution, but `src/mindroom/conversation_state_writer.py` independently builds the same sorted `team_...` scope ID for team history.
Most other matching behavior under `src/` is call-site usage of these policy helpers, not duplicated implementation.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
use_file_memory_backend	function	lines 19-23	related-only	get_agent_memory_backend memory.backend file uses_file_memory	src/mindroom/config/main.py:1561; src/mindroom/config/main.py:1570; src/mindroom/workspaces.py:298; src/mindroom/workspaces.py:325; src/mindroom/memory/functions.py:157
use_disabled_memory_backend	function	lines 26-30	none-found	get_agent_memory_backend memory.backend none disabled memory	src/mindroom/config/main.py:1561; src/mindroom/memory/functions.py:155; src/mindroom/memory/functions.py:213; src/mindroom/memory/functions.py:249
caller_uses_file_memory_backend	function	lines 33-37	related-only	caller_context list str team_uses_file_memory_backend use_file_memory_backend	src/mindroom/memory/functions.py:281; src/mindroom/memory/functions.py:313; src/mindroom/memory/functions.py:347; src/mindroom/memory/functions.py:464
caller_uses_disabled_memory_backend	function	lines 40-44	related-only	caller_context list str team_uses_disabled_memory_backend use_disabled_memory_backend	src/mindroom/memory/functions.py:281; src/mindroom/memory/functions.py:313; src/mindroom/memory/functions.py:347; src/mindroom/memory/functions.py:459
team_uses_file_memory_backend	function	lines 47-50	related-only	assert_team_agents_supported all get_agent_memory_backend file	src/mindroom/config/main.py:1513; src/mindroom/memory/functions.py:464; src/mindroom/workspaces.py:298; src/mindroom/workspaces.py:325
team_uses_disabled_memory_backend	function	lines 53-56	none-found	assert_team_agents_supported all get_agent_memory_backend none	src/mindroom/config/main.py:1513; src/mindroom/memory/functions.py:461
effective_storage_paths_for_context	function	lines 59-76	related-only	effective_storage_paths_for_context resolve_agent_runtime state_root distinct storage paths	src/mindroom/memory/_file_backend.py:834; src/mindroom/memory/_mem0_backend.py:46; src/mindroom/runtime_resolution.py:177; src/mindroom/agent_storage.py:98
_effective_storage_path_for_agent	function	lines 79-90	related-only	resolve_agent_runtime state_root agent storage path	src/mindroom/runtime_resolution.py:177; src/mindroom/agent_storage.py:98; src/mindroom/api/sandbox_runner.py:513; src/mindroom/api/sandbox_runner.py:844
build_team_user_id	function	lines 93-95	duplicate-found	team_ join sorted team scope id HistoryScope scope_id	src/mindroom/conversation_state_writer.py:52; src/mindroom/conversation_state_writer.py:60; src/mindroom/memory/_file_backend.py:841; src/mindroom/memory/_mem0_backend.py:501
agent_scope_user_id	function	lines 98-100	related-only	agent_ user_id scope account key agent_scope_user_id	src/mindroom/matrix/users.py:27; src/mindroom/memory/_file_backend.py:549; src/mindroom/memory/_mem0_backend.py:296; src/mindroom/memory/functions.py:138
agent_name_from_scope_user_id	function	lines 103-107	none-found	startswith agent_ removeprefix scope user id	src/mindroom/memory/_file_backend.py:472; src/mindroom/memory/_file_backend.py:521; src/mindroom/memory/_file_backend.py:728; src/mindroom/matrix/identity.py:79
get_team_ids_for_agent	function	lines 110-119	related-only	teams.items team_config.agents agent_name in team_config.agents build_team_user_id	src/mindroom/entity_resolution.py:31; src/mindroom/entity_resolution.py:114; src/mindroom/memory/_file_backend.py:651; src/mindroom/memory/_mem0_backend.py:332
_team_members_from_scope_user_id	function	lines 122-130	none-found	startswith team_ split plus team members from scope user id	src/mindroom/conversation_state_writer.py:60; src/mindroom/memory/_policy.py:127
storage_paths_for_scope_user_id	function	lines 133-152	related-only	scope_user_id storage paths team members agent scope unsupported memory scope	src/mindroom/memory/_file_backend.py:460; src/mindroom/memory/_file_backend.py:510; src/mindroom/memory/_mem0_backend.py:185; src/mindroom/memory/_mem0_backend.py:232
get_allowed_memory_user_ids	function	lines 155-169	related-only	allowed memory user ids team_reads_member_memory agent_scope_user_id get_team_ids_for_agent	src/mindroom/memory/_file_backend.py:460; src/mindroom/memory/_file_backend.py:716; src/mindroom/memory/_mem0_backend.py:107; src/mindroom/memory/_mem0_backend.py:377
file_memory_resolution_from_paths	function	lines 172-194	none-found	FileMemoryResolution use_configured_path preserve_resolved_storage_path original_storage_path resolved_storage_path	src/mindroom/memory/_file_backend.py:48; src/mindroom/memory/_shared.py:81; src/mindroom/memory/functions.py:187; src/mindroom/memory/auto_flush.py:458
storage_paths_match	function	lines 197-199	related-only	expanduser resolve compare storage paths equality	src/mindroom/constants.py:889; src/mindroom/credentials.py:334; src/mindroom/memory/config.py:50; src/mindroom/knowledge/registry.py:147
resolve_file_memory_resolution	function	lines 202-240	related-only	resolve_file_memory_resolution resolve_agent_runtime file_memory_root FileMemoryResolution	src/mindroom/memory/_file_backend.py:542; src/mindroom/memory/_file_backend.py:635; src/mindroom/memory/functions.py:130; src/mindroom/runtime_resolution.py:177
```

Findings:

1. Duplicated team scope ID construction.
   `src/mindroom/memory/_policy.py:93` returns `team_{'+'.join(sorted(agent_names))}` for memory scope IDs.
   `src/mindroom/conversation_state_writer.py:52` computes team member names from Matrix IDs and then independently returns `HistoryScope(kind="team", scope_id=f"team_{'+'.join(sorted(team_member_names))}")` at `src/mindroom/conversation_state_writer.py:60`.
   The behavior is functionally the same canonical team identifier format: sorted member names, plus-delimited, with the `team_` prefix.
   The difference to preserve is that configured team bots use their configured team name as the history scope at `src/mindroom/conversation_state_writer.py:55`, while ad hoc team responses use the member-derived scope ID.

2. Related but not duplicate: Matrix account keys also use an `agent_` prefix.
   `src/mindroom/memory/_policy.py:98` builds memory scope user IDs as `agent_<name>`.
   `src/mindroom/matrix/users.py:27` builds Matrix state account keys as `agent_<name>`.
   The string shape matches, but the domains differ: memory authorization and Matrix account state.
   Sharing this would conflate unrelated concepts, so no refactor is recommended.

3. Related but not duplicate: backend and storage resolution call sites.
   `src/mindroom/memory/functions.py:145`, `src/mindroom/memory/functions.py:201`, `src/mindroom/memory/functions.py:272`, and `src/mindroom/memory/functions.py:443` route through the `_policy.py` helpers rather than reimplementing backend selection.
   `src/mindroom/memory/_file_backend.py:460`, `src/mindroom/memory/_file_backend.py:510`, `src/mindroom/memory/_mem0_backend.py:185`, and `src/mindroom/memory/_mem0_backend.py:232` similarly call policy helpers for allowed scopes and storage paths.
   These are intended uses of the central policy module, not active duplication.

Proposed generalization:

1. If refactoring is desired later, import `build_team_user_id` into `src/mindroom/conversation_state_writer.py` and use it for the ad hoc team-history scope at line 60.
2. Keep configured team history scopes unchanged: configured teams should continue to use `self.deps.agent_name`.
3. Do not share `agent_scope_user_id` with Matrix account keys unless a broader naming-domain abstraction is introduced, which is not justified by this audit.

Risk/tests:

Changing `ConversationStateWriter.team_history_scope` to call `build_team_user_id` is low risk if it preserves the exact string for ad hoc teams.
Tests should cover ad hoc team history scope stability with unsorted member order, and configured team scope behavior where `self.deps.agent_name` is already a configured team.
No production code was edited in this audit.
