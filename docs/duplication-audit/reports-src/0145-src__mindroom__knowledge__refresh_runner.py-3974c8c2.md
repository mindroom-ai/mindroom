## Summary

Top duplication candidates for `src/mindroom/knowledge/refresh_runner.py`:

1. Bounded per-key `asyncio.Lock` registries are repeated in refresh locks, OpenAI completion locks, response lifecycle locks, and Matrix event-cache room locks.
2. `ToolExecutionIdentity` JSON serialization/deserialization is duplicated between knowledge refresh subprocess payloads and memory auto-flush persistence.
3. Published-index readiness and publish-from-state checks are repeated inside refresh result handling, unchanged-index publishing, cancellation reconciliation, and status/registry helpers.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
KnowledgeRefreshResult	class	lines 67-74	related-only	KnowledgeRefreshResult refresh result dataclass indexed_count index_published availability	src/mindroom/knowledge/refresh_scheduler.py:23; tests/test_knowledge_manager.py:3916
_SubprocessRefreshRequest	class	lines 78-84	related-only	subprocess refresh request config_data storage_root execution_identity force_reindex	src/mindroom/api/sandbox_runner.py:843; src/mindroom/api/sandbox_runner.py:987; src/mindroom/tool_system/sandbox_proxy.py:321
_SubprocessSessionKwargs	class	lines 87-88	related-only	start_new_session TypedDict subprocess kwargs	src/mindroom/api/sandbox_exec.py:400; src/mindroom/tools/shell.py:412
_RefreshLockEntry	class	lines 99-101	duplicate-found	asyncio Lock borrowers active_users lock entry prune	src/mindroom/matrix/cache/sqlite_event_cache.py:253; src/mindroom/matrix/cache/postgres_event_cache.py:338
_borrow_refresh_lock_for_key	function	lines 107-115	duplicate-found	get or create per-key asyncio.Lock prune bounded lock cache	src/mindroom/api/openai_compat.py:210; src/mindroom/response_lifecycle.py:128; src/mindroom/matrix/cache/sqlite_event_cache.py:290
_release_refresh_lock_for_key	function	lines 118-124	duplicate-found	decrement borrowers release lock entry prune active_users	src/mindroom/matrix/cache/sqlite_event_cache.py:301; src/mindroom/api/openai_compat.py:226
_prune_refresh_locks_locked	function	lines 127-138	duplicate-found	prune lock cache locked active users max locks	src/mindroom/api/openai_compat.py:217; src/mindroom/response_lifecycle.py:134; src/mindroom/matrix/cache/postgres_event_cache.py:646
_acquire_refresh_lock	async_function	lines 142-152	duplicate-found	asynccontextmanager acquire per-key lock release borrowed lock	src/mindroom/matrix/cache/sqlite_event_cache.py:299; src/mindroom/response_lifecycle.py:170
mark_refresh_active	function	lines 155-158	related-only	active refresh counts increment scheduler active	src/mindroom/knowledge/refresh_scheduler.py:160; src/mindroom/response_lifecycle.py:118
mark_refresh_inactive	function	lines 161-168	related-only	active refresh counts decrement pop scheduler inactive	src/mindroom/knowledge/refresh_scheduler.py:166; src/mindroom/attachments.py:278
is_refresh_active	function	lines 171-174	related-only	active refresh counts query locked	src/mindroom/knowledge/refresh_scheduler.py:81; src/mindroom/response_lifecycle.py:118; src/mindroom/tool_approval.py:280
is_refresh_active_for_binding	function	lines 177-195	none-found	resolve refresh target active binding ValueError	false none
refresh_knowledge_binding_in_subprocess	async_function	lines 198-262	related-only	create_subprocess_exec stdin payload env child interpreter cleanup reconcile	src/mindroom/tools/shell.py:406; src/mindroom/api/sandbox_exec.py:400; src/mindroom/knowledge/manager.py:1041
_serialize_subprocess_refresh_request	function	lines 265-281	duplicate-found	asdict execution_identity json serialize ToolExecutionIdentity config authored_model_dump	src/mindroom/memory/auto_flush.py:96; src/mindroom/tool_system/sandbox_proxy.py:321; src/mindroom/tool_system/sandbox_proxy.py:379
_send_subprocess_refresh_request	async_function	lines 284-295	related-only	process stdin write drain close wait_closed	src/mindroom/knowledge/manager.py:1041; src/mindroom/tools/shell.py:406
_refresh_file_lock_path	function	lines 298-300	related-only	sha256 lock path tempfile fcntl lock file	src/mindroom/codex_model.py:113; src/mindroom/oauth/state.py:44; src/mindroom/handled_turns.py:430
_open_refresh_file_lock_sync	function	lines 303-309	related-only	fcntl optional open lock file mkdir	src/mindroom/codex_model.py:118; src/mindroom/oauth/state.py:48; src/mindroom/handled_turns.py:441
_try_acquire_refresh_file_lock_sync	function	lines 312-320	related-only	fcntl LOCK_NB BlockingIOError try acquire src/mindroom/interactive.py:328; src/mindroom/handled_turns.py:444
_close_refresh_file_lock_sync	function	lines 323-326	related-only	close lock file handle if not acquired	src/mindroom/codex_model.py:127; src/mindroom/oauth/state.py:58
_release_refresh_file_lock_sync	function	lines 329-336	related-only	fcntl unlock finally close	src/mindroom/codex_model.py:123; src/mindroom/oauth/state.py:54; src/mindroom/interactive.py:307
_acquire_refresh_file_lock	async_function	lines 340-353	related-only	asynccontextmanager fcntl file lock polling across processes	src/mindroom/handled_turns.py:444; src/mindroom/interactive.py:293; src/mindroom/oauth/state.py:50
_subprocess_session_kwargs	function	lines 356-359	related-only	os.name nt start_new_session	src/mindroom/api/sandbox_exec.py:400; src/mindroom/tools/shell.py:412
_terminate_refresh_subprocess	async_function	lines 362-378	related-only	terminate subprocess process group SIGTERM SIGKILL wait timeout	src/mindroom/tools/shell.py:655; src/mindroom/api/sandbox_exec.py:435
_cleanup_cancelled_refresh_subprocess	async_function	lines 381-400	related-only	cancelled subprocess terminate reconcile with locks warning	src/mindroom/tools/shell.py:655; src/mindroom/knowledge/refresh_runner.py:403
_reconcile_failed_refresh_subprocess	async_function	lines 403-417	related-only	failed subprocess reconcile state preserve last good	src/mindroom/knowledge/refresh_runner.py:807; src/mindroom/knowledge/registry.py:432
knowledge_binding_mutation_lock	async_function	lines 421-439	related-only	resolve refresh target acquire memory and file lock	src/mindroom/api/knowledge.py:586; src/mindroom/api/knowledge.py:633
refresh_knowledge_binding	async_function	lines 442-486	related-only	resolve published key mark active acquire locks save refreshing reconcile cancelled	src/mindroom/knowledge/refresh_scheduler.py:91; src/mindroom/api/knowledge.py:706
_save_refreshing_state	async_function	lines 489-501	related-only	shield to_thread mark running cancelled stale	src/mindroom/knowledge/registry.py:419; src/mindroom/knowledge/registry.py:401
_refresh_knowledge_binding_locked	async_function	lines 504-561	related-only	resolve binding manager reindex redact error publish result	src/mindroom/knowledge/manager.py:1668; src/mindroom/api/knowledge.py:137
_maybe_publish_unchanged_index	async_function	lines 564-604	related-only	git sync unchanged publish mark source changed manual reindex	src/mindroom/knowledge/manager.py:1668; src/mindroom/knowledge/registry.py:851
_refresh_result_from_persisted_state	async_function	lines 607-674	duplicate-found	published state complete compatible collection publish succeeded failed result	src/mindroom/knowledge/status.py:47; src/mindroom/knowledge/registry.py:563; src/mindroom/knowledge/registry.py:598; src/mindroom/knowledge/refresh_runner.py:807
_publish_unchanged_index	async_function	lines 677-751	duplicate-found	load published state complete settings collection source signature publish succeeded failed	src/mindroom/knowledge/status.py:47; src/mindroom/knowledge/registry.py:563; src/mindroom/knowledge/registry.py:598; src/mindroom/knowledge/refresh_runner.py:607
_published_state_fingerprint	function	lines 754-768	related-only	PublishedIndexState tuple fingerprint fields compare state	src/mindroom/knowledge/registry.py:323; src/mindroom/knowledge/registry.py:353
_refresh_running_fingerprint	function	lines 771-791	related-only	replace state refresh_job running reason refreshing fingerprint	src/mindroom/knowledge/registry.py:419; src/mindroom/knowledge/registry.py:373
_failed_subprocess_state_can_be_reconciled	function	lines 794-804	related-only	compare fingerprints running refreshing reconcile failed subprocess	src/mindroom/knowledge/refresh_runner.py:807; src/mindroom/knowledge/registry.py:419
_reconcile_cancelled_refresh	async_function	lines 807-835	duplicate-found	cancelled refresh complete compatible ready collection publish or stale	src/mindroom/knowledge/refresh_runner.py:607; src/mindroom/knowledge/refresh_runner.py:677; src/mindroom/knowledge/status.py:47
_load_subprocess_refresh_request	function	lines 838-871	related-only	json payload validate fields subprocess request object	src/mindroom/memory/auto_flush.py:113; src/mindroom/matrix/message_content.py:32
_optional_str_payload_field	function	lines 874-881	duplicate-found	optional string payload field validation helper	src/mindroom/memory/auto_flush.py:123; src/mindroom/knowledge/registry.py:276; src/mindroom/custom_tools/matrix_api.py:355
_execution_identity_from_payload	function	lines 884-905	duplicate-found	deserialize ToolExecutionIdentity optional fields channel agent_name	src/mindroom/memory/auto_flush.py:113; src/mindroom/api/sandbox_runner.py:843; src/mindroom/api/credentials.py:304
_run_subprocess_refresh_request	async_function	lines 908-923	related-only	resolve_runtime_paths Config validate subprocess request execution_identity refresh	src/mindroom/config/main.py:1764; src/mindroom/custom_tools/config_manager.py:85; src/mindroom/api/sandbox_runner.py:153
_parse_refresh_runner_args	function	lines 926-928	related-only	argparse parser internal CLI	src/mindroom/cli/main.py:1; src/mindroom/cli/config.py:1
main	function	lines 931-947	related-only	CLI stdin asyncio.run logger exception return code	src/mindroom/knowledge_refresh_runner.py:1; src/mindroom/cli/main.py:1
```

## Findings

### 1. Bounded per-key lock registries are repeated

`refresh_runner.py` keeps `_refresh_locks` as a keyed `asyncio.Lock` registry with borrower counts and pruning in `_RefreshLockEntry`, `_borrow_refresh_lock_for_key`, `_release_refresh_lock_for_key`, `_prune_refresh_locks_locked`, and `_acquire_refresh_lock` (`src/mindroom/knowledge/refresh_runner.py:99`, `src/mindroom/knowledge/refresh_runner.py:107`, `src/mindroom/knowledge/refresh_runner.py:118`, `src/mindroom/knowledge/refresh_runner.py:127`, `src/mindroom/knowledge/refresh_runner.py:142`).
The same behavior appears in smaller forms in OpenAI completion locks (`src/mindroom/api/openai_compat.py:210`), response lifecycle locks (`src/mindroom/response_lifecycle.py:128`), and Matrix cache room locks (`src/mindroom/matrix/cache/sqlite_event_cache.py:253`, `src/mindroom/matrix/cache/sqlite_event_cache.py:290`, `src/mindroom/matrix/cache/postgres_event_cache.py:646`).

The shared behavior is a map from a runtime key to an `asyncio.Lock`, bounded by opportunistically pruning entries whose locks are not active.
The differences to preserve are ownership scope, capacity, whether active users are counted before acquisition, whether ordering matters, and whether a separate global `threading.Lock` protects access from non-event-loop threads.

### 2. ToolExecutionIdentity JSON handling is duplicated

`_serialize_subprocess_refresh_request`, `_optional_str_payload_field`, and `_execution_identity_from_payload` serialize and validate `ToolExecutionIdentity` fields for the refresh subprocess (`src/mindroom/knowledge/refresh_runner.py:265`, `src/mindroom/knowledge/refresh_runner.py:874`, `src/mindroom/knowledge/refresh_runner.py:884`).
Memory auto-flush has a parallel typed payload and deserializer for the same fields in `_SerializedExecutionIdentity`, `_serialize_execution_identity`, and `_deserialize_execution_identity` (`src/mindroom/memory/auto_flush.py:69`, `src/mindroom/memory/auto_flush.py:96`, `src/mindroom/memory/auto_flush.py:113`).
Sandbox proxy payloads also use `asdict(execution_identity)` when crossing process boundaries (`src/mindroom/tool_system/sandbox_proxy.py:321`, `src/mindroom/tool_system/sandbox_proxy.py:379`).

The shared behavior is conversion between `ToolExecutionIdentity` and JSON-compatible dictionaries containing channel, agent, requester, room, thread, session, tenant, and account fields.
The differences to preserve are error policy: refresh subprocess parsing raises `TypeError` on malformed fields, while memory auto-flush treats malformed persisted identity as absent.

### 3. Published-index readiness and publish checks are repeated

`_refresh_result_from_persisted_state`, `_publish_unchanged_index`, and `_reconcile_cancelled_refresh` each load or receive a `PublishedIndexState`, check status/settings/availability/collection existence, call `publish_knowledge_index_from_state`, and then mark success or failure (`src/mindroom/knowledge/refresh_runner.py:607`, `src/mindroom/knowledge/refresh_runner.py:677`, `src/mindroom/knowledge/refresh_runner.py:807`).
Status and registry helpers already encode related readiness checks in `published_index_settings_compatible`, `published_index_availability_for_state`, and collection existence paths (`src/mindroom/knowledge/status.py:47`, `src/mindroom/knowledge/registry.py:563`, `src/mindroom/knowledge/registry.py:598`, `src/mindroom/knowledge/registry.py:644`).

The shared behavior is deciding whether a persisted complete state can be published for the current key, then publishing and marking the refresh state.
The differences to preserve are caller-specific error messages, whether stale state is returned as a non-error result, and whether unchanged refresh compares source signatures before publishing.

## Proposed Generalization

1. Add a small internal utility for bounded keyed async locks, likely under `src/mindroom/locking.py` or a narrower runtime utility module, only if touching at least two of the active lock registries in the same change.
2. Add `tool_execution_identity_to_payload()` and `tool_execution_identity_from_payload(..., strict: bool)` near `mindroom.tool_system.worker_routing.ToolExecutionIdentity`.
3. Extract a focused knowledge helper such as `_publish_complete_state_for_key(...)` inside `refresh_runner.py` first, before moving it to `registry.py`, because current behavior is tightly coupled to refresh result semantics.
4. Keep file-lock helpers as-is for now; similar `fcntl` usage exists, but lock modes, paths, and blocking behavior differ enough that a shared helper would risk obscuring call-site semantics.
5. Cover any future refactor with existing knowledge-manager refresh tests plus targeted unit tests for malformed execution identity payloads and lock pruning.

## Risk/tests

The lock-cache refactor risk is subtle concurrency behavior: borrower counts, active users, and pruning must not remove a lock while a waiter still depends on it.
Tests should include concurrent same-key refreshes, different-key refreshes, lock pruning at capacity, and cancellation during acquisition.

The identity-payload refactor risk is changing malformed persisted-data behavior.
Tests should assert that refresh subprocess payloads still raise clear `TypeError`s while memory auto-flush still ignores malformed persisted identities.

The published-index helper risk is changing refresh state transitions.
Tests should cover missing metadata, incomplete metadata, incompatible settings, missing collection, successful publish, unchanged source, source-changed stale marking, and cancelled refresh reconciliation.
