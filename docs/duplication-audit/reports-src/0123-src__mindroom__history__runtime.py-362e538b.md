Summary: top duplication candidates are history-policy-to-Agno-limit conversion, creation/loading of scope sessions, canonical history-scope/storage resolution, and unique DB close handling.
The overlaps are real but mostly localized around the history runtime boundary, so the safest refactors would be small helpers in `mindroom.history` or `mindroom.agent_storage`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_elapsed_ms	function	lines 87-89	related-only	elapsed_ms time.monotonic duration_ms	src/mindroom/timing.py:198; src/mindroom/coalescing.py:335; src/mindroom/hooks/execution.py:212; src/mindroom/history/compaction.py:1070
_compaction_failure_status	function	lines 92-96	none-found	compaction failure timeout timed out status	none
_load_compaction_model	function	lines 100-106	related-only	get_model_instance timed model init	src/mindroom/model_loading.py references; no same timed compaction wrapper found
ScopeSessionContext	class	lines 110-116	not-a-behavior-symbol	dataclass scope storage session context	none
BoundTeamScopeContext	class	lines 120-125	not-a-behavior-symbol	dataclass bound team scope owner	none
note_prepared_history_timing	function	lines 128-148	related-only	pipeline_timing compaction_decision fitted_replay_tokens	src/mindroom/timing.py:107; no duplicate history metadata writer found
_sync_loaded_session	function	lines 151-158	none-found	session metadata runs summary sync target_session	none
_clear_forced_compaction_after_failure	function	lines 161-181	related-only	clear force_compact_before_next_run read_scope_state write_scope_state	src/mindroom/history/storage.py clear_force_compaction_state callers; no duplicate reload-clear-sync flow found
_ResolvedPreparationInputs	class	lines 185-192	not-a-behavior-symbol	dataclass preparation inputs	none
_ScopeCompactionLifecycleResult	class	lines 196-198	not-a-behavior-symbol	dataclass lifecycle result	none
PreparedScopeHistory	class	lines 202-213	not-a-behavior-symbol	dataclass prepared history result	none
_start_compaction_lifecycle	async_function	lines 216-230	related-only	lifecycle.start Failed to send compaction lifecycle notice	src/mindroom/delivery_gateway.py:930; src/mindroom/history/types.py:196
_complete_compaction_lifecycle_success	async_function	lines 233-246	related-only	complete_success notice_event_id compaction lifecycle	src/mindroom/delivery_gateway.py:961; src/mindroom/history/types.py:202
_update_compaction_lifecycle_progress	async_function	lines 249-262	related-only	lifecycle.progress notice_event_id compaction lifecycle	src/mindroom/delivery_gateway.py:949; src/mindroom/history/types.py:199
_compaction_progress_callback	function	lines 265-279	none-found	progress_callback duration_ms replace lifecycle progress	none
_compaction_progress_callback.<locals>.progress_callback	nested_async_function	lines 273-277	none-found	progress callback replace duration_ms	none
_complete_compaction_lifecycle_failure	async_function	lines 282-296	related-only	complete_failure notice_event_id compaction lifecycle	src/mindroom/delivery_gateway.py:977; src/mindroom/history/types.py:205
_complete_no_compactable_history_failure	async_function	lines 299-323	none-found	No compactable history remained CompactionLifecycleFailure	none
resolve_history_scope	function	lines 326-334	duplicate-found	HistoryScope agent.team_id agent.id configured team history_scope	src/mindroom/conversation_state_writer.py:42; src/mindroom/conversation_state_writer.py:52; src/mindroom/teams.py:1340
prepare_scope_history	async_function	lines 338-483	related-only	prepare scope history compaction classify replay	src/mindroom/execution_preparation.py:792; no duplicate durable compaction flow found
_run_scope_compaction_with_lifecycle	async_function	lines 486-612	related-only	compaction lifecycle failure success timeout duration	src/mindroom/delivery_gateway.py:930; src/mindroom/history/compaction.py:1040
_run_scope_compaction	async_function	lines 615-655	related-only	compact_scope_history summary_model load model	src/mindroom/history/compaction.py:284; no duplicate runtime wrapper found
_estimated_context_tokens	function	lines 658-666	none-found	static_prompt_tokens replay_plan estimated_tokens	none
finalize_history_preparation	function	lines 669-782	related-only	replay_plan prepared_context_tokens PreparedHistoryState	src/mindroom/execution_preparation.py:716; no duplicate persisted replay finalizer found
prepare_history_for_run	async_function	lines 785-881	related-only	prepare_history_for_run open_scope_session_context finalize_history_preparation	src/mindroom/execution_preparation.py:782; no duplicate full durable flow found
prepare_bound_scope_history	async_function	lines 885-964	related-only	bound_scope team static_prompt_tokens _resolve_entity_preparation_inputs	src/mindroom/teams.py:1340; no duplicate compaction preparation flow found
resolve_bound_history_owner	function	lines 967-977	duplicate-found	sorted agent ids owner min agents	src/mindroom/conversation_state_writer.py:57; src/mindroom/history/runtime.py:1287
resolve_bound_team_scope_context	function	lines 980-999	duplicate-found	ad hoc team scope id team_name config.teams HistoryScope	src/mindroom/conversation_state_writer.py:52; src/mindroom/teams.py:1340; src/mindroom/history/runtime.py:1287
estimate_preparation_static_tokens	function	lines 1002-1008	related-only	estimate_agent_static_tokens wrapper	src/mindroom/history/compaction.py:708; src/mindroom/execution_preparation.py:801
estimate_preparation_prompt_tokens	function	lines 1011-1016	related-only	estimate_text_tokens full_prompt wrapper	src/mindroom/token_budget.py:12
estimate_preparation_static_tokens_for_team	function	lines 1019-1025	related-only	estimate_team_static_tokens wrapper	src/mindroom/history/compaction.py:736; src/mindroom/execution_preparation.py:872
open_scope_storage	function	lines 1029-1048	duplicate-found	create storage try yield finally close	src/mindroom/turn_store.py:388; src/mindroom/response_runner.py:471; src/mindroom/response_lifecycle.py:359
_build_scope_session_context	function	lines 1051-1088	duplicate-found	get agent/team session create AgentSession/TeamSession timestamp	src/mindroom/history/interrupted_replay.py:323; src/mindroom/history/interrupted_replay.py:330; src/mindroom/teams.py:1124
open_resolved_scope_session_context	function	lines 1092-1121	related-only	open storage build session context	src/mindroom/conversation_state_writer.py:62; src/mindroom/turn_store.py:388
open_scope_session_context	function	lines 1125-1147	related-only	resolve scope then open_resolved_scope_session_context	src/mindroom/conversation_state_writer.py:42; no duplicate contextmanager found
open_bound_scope_session_context	function	lines 1151-1192	related-only	bound team open scope session context	src/mindroom/teams.py:1118; no duplicate contextmanager found
create_scope_session_storage	function	lines 1195-1218	duplicate-found	create_session_storage create_state_storage team scope storage	src/mindroom/conversation_state_writer.py:62; src/mindroom/agent_storage.py:71; src/mindroom/agent_storage.py:104
close_unique_state_dbs	function	lines 1221-1231	duplicate-found	close distinct storage handles seen id	src/mindroom/response_runner.py:471; src/mindroom/response_lifecycle.py:359; src/mindroom/turn_store.py:388
close_agent_runtime_state_dbs	function	lines 1234-1244	related-only	get_agent_runtime_state_dbs close shared_scope_storage	src/mindroom/agent_storage.py runtime state DB helpers; no duplicate exact close filter found
close_team_runtime_state_dbs	function	lines 1247-1262	related-only	team_db agents runtime state dbs shared_scope_storage	src/mindroom/teams.py team request cleanup references; no duplicate exact close filter found
_scope_session_storage_name	function	lines 1265-1270	none-found	TEAM_STORAGE_NAME_PATTERN sha256 scope.key storage name	none
_team_scope_state_root	function	lines 1273-1278	none-found	teams storage root runtime_paths storage_root	none
_scope_session_agent_id	function	lines 1281-1284	none-found	scope_session_agent_id agent team storage name	none
_ad_hoc_team_scope_id	function	lines 1287-1291	duplicate-found	team_ join sorted agent ids team member names	src/mindroom/conversation_state_writer.py:57; src/mindroom/teams.py:1340
_history_settings_from_agent	function	lines 1294-1306	duplicate-found	num_history_messages num_history_runs HistoryPolicy ResolvedHistorySettings	src/mindroom/config/main.py:199; src/mindroom/config/main.py:1008; src/mindroom/config/main.py:1020
_resolve_entity_preparation_inputs	function	lines 1309-1367	duplicate-found	get_entity_history_settings get_default_history_settings resolve_runtime_model execution_plan	src/mindroom/config/main.py:1008; src/mindroom/config/main.py:1020; src/mindroom/agents.py:1208
_resolve_preparation_inputs	function	lines 1370-1400	related-only	agent static tokens history settings from agent entity prep inputs	src/mindroom/execution_preparation.py:801; no duplicate full resolver found
_prepare_scope_state_for_run	function	lines 1403-1425	related-only	consume_pending_force_compaction_scope clear_force_compaction_state unavailable	src/mindroom/history/storage.py:force compaction helpers; no duplicate run preparation flow found
plan_replay_that_fits	function	lines 1428-1476	none-found	binary search replay plan available_history_budget	none
apply_replay_plan	function	lines 1479-1487	duplicate-found	add_history_to_context num_history_runs num_history_messages assignment	src/mindroom/teams.py:1359; src/mindroom/agents.py:1248
_context_window_guard_limit_bounds	function	lines 1490-1503	none-found	limit bounds completed_top_level_runs policy mode	none
_find_fitting_history_limit_for_budget	function	lines 1506-1539	none-found	binary search history limit budget	none
_log_replay_plan	function	lines 1542-1570	none-found	Replay planner reduced disabled persisted replay logger	none
_configured_replay_plan	function	lines 1573-1588	related-only	ResolvedReplayPlan configured history_limit_fields	src/mindroom/teams.py:1359; related Agno limit assignment only
_history_settings_with_limit	function	lines 1591-1602	none-found	ResolvedHistorySettings policy with limit copy fields	none
_history_limit_fields	function	lines 1605-1613	duplicate-found	policy mode runs messages limit to Agno fields	src/mindroom/teams.py:1361; src/mindroom/agents.py:1210; src/mindroom/config/main.py:199
_has_effective_persisted_replay	function	lines 1616-1626	none-found	session summary replay add_history_to_context completed runs	none
_session_has_summary_replay	function	lines 1629-1632	none-found	session.summary summary strip	none
_session_summary_replay_tokens	function	lines 1635-1638	related-only	estimate_session_summary_tokens session.summary	src/mindroom/history/compaction.py:1513
```

## Findings

1. History policy and Agno replay-limit mapping is repeated in several places.
`src/mindroom/history/runtime.py:1294` maps `num_history_messages` / `num_history_runs` into `HistoryPolicy`, while `src/mindroom/config/main.py:199` does the same conversion for config objects.
`src/mindroom/history/runtime.py:1605` converts a `HistoryPolicy` mode/limit back into Agno fields, while `src/mindroom/teams.py:1361` and `src/mindroom/agents.py:1210` hand-code the same precedence and field assignment.
The differences to preserve are that configured entity settings apply defaults in `Config`, while ad hoc live-agent settings in runtime read the already-materialized Agno agent.

2. Scope/session creation and loading is duplicated.
`src/mindroom/history/runtime.py:1051` loads either a team or agent session and can create a missing `TeamSession` / `AgentSession`.
`src/mindroom/history/interrupted_replay.py:323` and `src/mindroom/history/interrupted_replay.py:330` implement the same session-type split and timestamped empty-session construction, and `src/mindroom/teams.py:1124` repeats the missing `TeamSession` creation for bound seen-event persistence.
The behavior is the same shape: choose session class by scope, populate `metadata={}`, `runs=[]`, and set matching `created_at` / `updated_at`.

3. Canonical team/agent history scope and storage selection is repeated across runtime and conversation state writing.
`src/mindroom/history/runtime.py:326`, `src/mindroom/history/runtime.py:980`, and `src/mindroom/history/runtime.py:1287` resolve agent/team scopes and ad hoc team IDs.
`src/mindroom/conversation_state_writer.py:42`, `src/mindroom/conversation_state_writer.py:52`, and `src/mindroom/conversation_state_writer.py:62` repeat the configured-team vs agent scope decision, ad hoc `team_` + sorted member-name ID, and storage creation split.
The main difference to preserve is input type: runtime works from live `Agent` objects, while conversation state writer often starts from Matrix IDs and runtime config.

4. Closing storage handles has repeated "try/finally close" and "close once" behavior.
`src/mindroom/history/runtime.py:1029` owns a scope-storage contextmanager and `src/mindroom/history/runtime.py:1221` closes distinct DB handles by identity.
Other modules open storage and close it manually, for example `src/mindroom/turn_store.py:388`, `src/mindroom/response_runner.py:471`, and `src/mindroom/response_lifecycle.py:359`.
This is lower-impact duplication but easy to centralize because storage handles share the same `close()` contract.

## Proposed Generalization

1. Add a small public helper near `HistoryPolicy`, such as `history_policy_from_limits()` and `history_limit_fields()`, then have config, runtime, agents, and teams call it.
2. Move empty session construction and session loading by `HistoryScope` into `mindroom.agent_storage` or `mindroom.history.storage`, for example `get_scope_session()` and `new_scope_session()`.
3. Expose one canonical ad hoc team scope helper that accepts already-resolved member names, then let both runtime and `ConversationStateWriter` reuse it after each has converted its own input type.
4. Reuse `open_scope_storage()` or a more general `closing_storage()` contextmanager at manual close sites where ownership is local.
5. Keep lifecycle wrappers local for now; related code exists in delivery, but runtime is orchestration and delivery is Matrix IO, so merging them would blur boundaries.

## Risk/tests

The highest risk is changing history replay limits because `None` has semantic meaning: it can mean "all history" and also avoids Agno's default three-run fallback.
Tests should cover agent config override precedence, team replay field assignment, ad hoc agent history settings, and all-history behavior.
Session helper extraction should cover both agent and team sessions, missing-session creation timestamps, and scope IDs for team-backed agent IDs.
Scope helper extraction should cover configured teams, ad hoc sorted member names, empty member lists, and the digest-backed team storage name.
Storage-close refactors should include a double-reference case to verify the same DB handle is closed once.
