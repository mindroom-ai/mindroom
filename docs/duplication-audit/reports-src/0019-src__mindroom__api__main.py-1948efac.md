## Summary

Top duplication candidates in `src/mindroom/api/main.py`:

1. Agent and team config CRUD endpoints duplicate the same dictionary-section behavior: list entities with injected IDs, update with API-only `id` stripping, create with display-name-derived unique IDs, and delete with a 404 when missing.
2. The local `_published_snapshot` helper overlaps with `src/mindroom/api/config_lifecycle.py` snapshot publication logic, but it preserves extra fields (`runtime_paths`, `auth_state`) and optional generation behavior, so this is related rather than a direct extraction target.
3. The API app context accessors mirror sandbox-runner context accessors, but they are intentionally typed to different app state payloads.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
DraftAgentPolicyDefaultsRequest	class	lines 60-65	related-only	worker_scope draft policy request pydantic DefaultsConfig	/src/mindroom/config/models.py:351; /src/mindroom/agent_policy.py:58
DraftAgentPolicyKnowledgeRequest	class	lines 68-74	related-only	private knowledge enabled path request AgentPrivateKnowledgeConfig	/src/mindroom/config/agent.py:61; /src/mindroom/agent_policy.py:94
DraftAgentPolicyPrivateRequest	class	lines 77-83	related-only	private per knowledge request AgentPrivateConfig	/src/mindroom/config/agent.py:127; /src/mindroom/agent_policy.py:79
DraftAgentPolicyAgentRequest	class	lines 86-93	related-only	worker_scope private delegate_to draft agent AgentConfig	/src/mindroom/config/agent.py:264; /src/mindroom/agent_policy.py:82
AgentPoliciesRequest	class	lines 96-102	related-only	agent_policies payload defaults agents	/src/mindroom/config/main.py:1178; /src/mindroom/agent_policy.py:120
RawConfigSourceRequest	class	lines 105-108	none-found	raw config source request source	none
_worker_cleanup_interval_seconds	function	lines 111-118	none-found	MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS cleanup interval env float max	none
_cleanup_workers_once	function	lines 121-162	related-only	cleanup_idle_workers primary worker manager kubernetes validation snapshot	/src/mindroom/api/sandbox_runner.py:1302; /src/mindroom/workers/manager.py:61
_worker_cleanup_loop	async_function	lines 165-202	none-found	background worker cleanup loop wait_for stop_event interval	none
api_runtime_paths	function	lines 205-207	related-only	api_runtime_paths request committed runtime paths	/src/mindroom/api/config_lifecycle.py:342; /src/mindroom/api/sandbox_runner.py:440
_published_snapshot	function	lines 210-237	related-only	published_snapshot ApiSnapshot generation auth_state runtime_paths	/src/mindroom/api/config_lifecycle.py:308
_app_context	function	lines 240-242	related-only	app_context FastAPI state snapshot	/src/mindroom/api/sandbox_runner.py:407; /src/mindroom/api/config_lifecycle.py:107
_app_runtime_paths	function	lines 245-247	related-only	app_runtime_paths FastAPI runtime_paths	/src/mindroom/api/sandbox_runner.py:418; /src/mindroom/api/config_lifecycle.py:342
initialize_api_app	function	lines 250-286	related-only	ensure_app_state ApiState ApiSnapshot register_api_app runtime swap	/src/mindroom/api/config_lifecycle.py:48; /src/mindroom/api/runtime_reload.py:52
_sync_standalone_knowledge_watchers	async_function	lines 289-295	none-found	knowledge_source_watcher sync runtime_config runtime_paths	none
_watch_config	async_function	lines 298-343	related-only	watch config file mtime reload stop_event	/src/mindroom/api/config_lifecycle.py:795; /src/mindroom/file_watcher.py:1
_lifespan	async_function	lines 347-393	related-only	FastAPI lifespan startup shutdown stop_event task cancel scheduler watcher	/src/mindroom/api/sandbox_runner_app.py:20; /src/mindroom/orchestrator.py:1885
bind_orchestrator_knowledge_refresh_scheduler	function	lines 396-401	none-found	orchestrator_knowledge_refresh_scheduler app_state bind	none
_sanitize_entity_payload	function	lines 424-428	duplicate-found	pop id entity payload sanitize	/src/mindroom/api/main.py:609; /src/mindroom/api/main.py:632; /src/mindroom/api/main.py:692; /src/mindroom/api/main.py:715
_resolve_unique_entity_id	function	lines 431-438	duplicate-found	unique entity id suffix display_name lower replace	/src/mindroom/api/main.py:626; /src/mindroom/api/main.py:709
_set_config_generation_header	function	lines 441-443	related-only	CONFIG_GENERATION_HEADER response header generation	/src/mindroom/api/main.py:503; /src/mindroom/api/main.py:535; /src/mindroom/api/config_lifecycle.py:347
health_check	async_function	lines 462-481	related-only	health endpoint runtime_state matrix sync health JSONResponse	/src/mindroom/api/sandbox_runner_app.py:42; /src/mindroom/orchestration/runtime.py:25
readiness_check	async_function	lines 485-493	related-only	readiness endpoint runtime_state ready 503	/src/mindroom/api/sandbox_runner_app.py:42; /src/mindroom/runtime_state.py:1
load_config	async_function	lines 497-506	related-only	read_committed_config committed_generation header	/src/mindroom/api/main.py:529; /src/mindroom/api/config_lifecycle.py:677
save_config	async_function	lines 510-525	related-only	replace_committed_config expected generation header success	/src/mindroom/api/main.py:542; /src/mindroom/api/config_lifecycle.py:740
get_raw_config_source	async_function	lines 529-538	related-only	raw config source committed_generation header	/src/mindroom/api/main.py:497; /src/mindroom/api/config_lifecycle.py:767
save_raw_config_source	async_function	lines 542-557	related-only	replace raw config source expected generation header success	/src/mindroom/api/main.py:510; /src/mindroom/api/config_lifecycle.py:778
get_agent_policies	async_function	lines 561-578	related-only	build_agent_policy_seeds resolve_agent_policy_index asdict	/src/mindroom/config/main.py:1178; /src/mindroom/runtime_resolution.py:152; /src/mindroom/agent_policy.py:120
get_agents	async_function	lines 582-594	duplicate-found	config section entities to list with id	/src/mindroom/api/main.py:664; /src/mindroom/api/matrix_operations.py:138
get_agents.<locals>.read_agents	nested_function	lines 585-592	duplicate-found	get agents dict inject id list	/src/mindroom/api/main.py:668
update_agent	async_function	lines 598-616	duplicate-found	config section update sanitize entity payload	/src/mindroom/api/main.py:680; /src/mindroom/api/main.py:756
update_agent.<locals>.mutate	nested_function	lines 606-609	duplicate-found	ensure agents section assign sanitized payload	/src/mindroom/api/main.py:689
create_agent	async_function	lines 620-640	duplicate-found	create config entity display_name slug unique id sanitize	/src/mindroom/api/main.py:702
create_agent.<locals>.mutate	nested_function	lines 628-633	duplicate-found	ensure section resolve unique id assign sanitized payload	/src/mindroom/api/main.py:711
delete_agent	async_function	lines 644-661	duplicate-found	delete config entity missing 404	/src/mindroom/api/main.py:726
delete_agent.<locals>.mutate	nested_function	lines 651-654	duplicate-found	missing section or id raise HTTPException del	/src/mindroom/api/main.py:734
get_teams	async_function	lines 665-677	duplicate-found	config section entities to list with id	/src/mindroom/api/main.py:581; /src/mindroom/api/matrix_operations.py:138
get_teams.<locals>.read_teams	nested_function	lines 668-675	duplicate-found	get teams dict inject id list	/src/mindroom/api/main.py:585
update_team	async_function	lines 681-699	duplicate-found	config section update sanitize entity payload	/src/mindroom/api/main.py:597
update_team.<locals>.mutate	nested_function	lines 689-692	duplicate-found	ensure teams section assign sanitized payload	/src/mindroom/api/main.py:606
create_team	async_function	lines 703-723	duplicate-found	create config entity display_name slug unique id sanitize	/src/mindroom/api/main.py:619
create_team.<locals>.mutate	nested_function	lines 711-716	duplicate-found	ensure section resolve unique id assign sanitized payload	/src/mindroom/api/main.py:628
delete_team	async_function	lines 727-744	duplicate-found	delete config entity missing 404	/src/mindroom/api/main.py:643
delete_team.<locals>.mutate	nested_function	lines 734-737	duplicate-found	missing section or id raise HTTPException del	/src/mindroom/api/main.py:651
get_models	async_function	lines 748-753	related-only	read config mapping section default empty dict	/src/mindroom/api/main.py:778
update_model	async_function	lines 757-775	related-only	ensure models section update config write	/src/mindroom/api/main.py:598; /src/mindroom/api/main.py:681
update_model.<locals>.mutate	nested_function	lines 765-768	related-only	ensure section assign model_data	/src/mindroom/api/main.py:606; /src/mindroom/api/main.py:689
get_room_models	async_function	lines 779-784	related-only	read config mapping section default empty dict	/src/mindroom/api/main.py:747
update_room_models	async_function	lines 788-803	related-only	write whole config mapping section	/src/mindroom/api/main.py:757
update_room_models.<locals>.mutate	nested_function	lines 795-796	related-only	assign whole room_models section	/src/mindroom/api/main.py:765
get_available_rooms	async_function	lines 807-817	related-only	collect agent rooms from config sorted set rooms	/src/mindroom/api/matrix_operations.py:102; /src/mindroom/api/matrix_operations.py:136
get_available_rooms.<locals>.read_rooms	nested_function	lines 810-815	related-only	agent_data rooms set sorted	/src/mindroom/api/matrix_operations.py:102
```

## Findings

### 1. Agent and team CRUD duplicate one generic config-section entity flow

The agent endpoints at `src/mindroom/api/main.py:581`, `src/mindroom/api/main.py:597`, `src/mindroom/api/main.py:619`, and `src/mindroom/api/main.py:643` match the team endpoints at `src/mindroom/api/main.py:664`, `src/mindroom/api/main.py:680`, `src/mindroom/api/main.py:702`, and `src/mindroom/api/main.py:726`.
Both sections store entities as `dict[id, payload]`, expose list responses with an injected `id`, strip API-only `id` before writing, derive create IDs from `display_name.lower().replace(" ", "_")`, append numeric suffixes for collisions, and raise `404` on delete when the section or entity is missing.

Differences to preserve:

- The config section names are `agents` and `teams`.
- The default create IDs are `new_agent` and `new_team`.
- Delete errors say `Agent not found` or `Team not found`.
- Error prefixes are entity-specific: `Failed to save/create/delete agent` versus `team`.

### 2. `_published_snapshot` is related to config-lifecycle snapshot publishing

`src/mindroom/api/main.py:210` and `src/mindroom/api/config_lifecycle.py:308` both build a new `ApiSnapshot` while preserving unspecified fields and advancing generation.
The main-module variant also supports runtime-path swaps, auth-state replacement, config-load result replacement, and `increment_generation=False`.
The config-lifecycle variant is narrower and uses `dataclasses.replace`.

This is real conceptual overlap, but not a clean duplicate because the main helper currently covers initialization/runtime-swap state that the config-lifecycle helper does not model.

### 3. API app context accessors mirror sandbox-runner app context accessors

`src/mindroom/api/main.py:240` and `src/mindroom/api/sandbox_runner.py:407` both centralize typed FastAPI app-state access.
Their runtime-path helpers at `src/mindroom/api/main.py:245` and `src/mindroom/api/sandbox_runner.py:418` are also structurally identical.
The payloads differ (`ApiSnapshot` versus `_SandboxRunnerContext`), so this is a shared pattern rather than a strong refactor candidate.

### 4. Config file watching overlaps with the generic watcher helper but handles runtime rebinding

`src/mindroom/api/main.py:298` manually polls the active config path, tracks mtime, reloads app config, and keeps watching when runtime paths change.
`src/mindroom/api/config_lifecycle.py:795` wraps `mindroom.file_watcher.watch_file` for a fixed runtime config path.
The behavior is related, but the main watcher has active runtime-path rebinding and standalone knowledge watcher sync.

## Proposed Generalization

1. Add a small private helper near the existing config endpoints, for example `_list_config_entities(config_data, section)`, returning `[{"id": entity_id, **entity_data}, ...]`.
2. Add `_upsert_config_entity(candidate_config, section, entity_id, entity_data)` that creates the section if missing and applies `_sanitize_entity_payload`.
3. Add `_create_config_entity(candidate_config, section, entity_data, default_id)` that derives the display-name ID, calls `_resolve_unique_entity_id`, writes the sanitized payload, and returns the new ID.
4. Add `_delete_config_entity(candidate_config, section, entity_id, missing_detail)` for the shared 404/delete flow.
5. Leave `_published_snapshot`, app context accessors, and config watching alone unless a broader API lifecycle refactor is already underway.

No production code was changed for this audit.

## Risk/Tests

The CRUD dedupe is low risk if constrained to the agent/team endpoints, but tests should cover:

- `GET /api/config/agents` and `GET /api/config/teams` still inject IDs and preserve payload fields.
- Creating agents and teams keeps the current display-name slug behavior and numeric suffix collision behavior.
- Updating agents and teams continues to strip payload `id`.
- Deleting missing agents and teams still returns the existing 404 detail.

The snapshot and watcher related-only candidates are higher risk because they touch app startup, runtime reload, auth state, generation counters, and knowledge watcher synchronization.
No refactor is recommended for those in a narrow duplication cleanup.
