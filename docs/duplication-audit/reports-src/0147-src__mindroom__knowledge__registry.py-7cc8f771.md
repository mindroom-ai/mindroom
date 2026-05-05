## Summary

Top duplication candidates for `src/mindroom/knowledge/registry.py`:

1. Published index metadata parsing and atomic JSON persistence are duplicated with `KnowledgeManager`'s private persisted index state handling.
2. Chroma/Knowledge construction for published index handles duplicates the manager's vector DB and Knowledge construction pattern.
3. Same-source knowledge alias discovery overlaps with the API mutation scheduling fallback and watcher grouping, but each call site preserves different side effects.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
PublishedIndexKey	class	lines 43-49	related-only	PublishedIndexKey base_id storage_root knowledge_path indexing_settings; knowledge refresh target keys	src/mindroom/knowledge/refresh_scheduler.py:42; src/mindroom/knowledge/watch.py:33; src/mindroom/knowledge/manager.py:286
KnowledgeRefreshTarget	class	lines 53-58	related-only	KnowledgeRefreshTarget resolve_refresh_target refresh scheduler task keys	src/mindroom/knowledge/refresh_scheduler.py:42; src/mindroom/knowledge/watch.py:86
KnowledgeSourceRoot	class	lines 62-66	related-only	KnowledgeSourceRoot shared local watch targets source root	src/mindroom/knowledge/watch.py:58; src/mindroom/knowledge/watch.py:73
PublishedIndexState	class	lines 70-84	duplicate-found	PublishedIndexState persisted index state settings status collection indexed_count source_signature	src/mindroom/knowledge/manager.py:151; src/mindroom/knowledge/manager.py:892
PublishedIndexHandle	class	lines 88-94	related-only	published index handle knowledge state metadata_path cache	none
PublishedIndexResolution	class	lines 98-105	related-only	PublishedIndexResolution availability schedule_refresh_on_access	none
_PublishedIndexVectorDb	class	lines 108-114	related-only	vector db Protocol client collection_name exists ChromaDb	src/mindroom/knowledge/manager.py:260; src/mindroom/knowledge/manager.py:1327
_PublishedIndexVectorDb.exists	method	lines 112-114	related-only	vector_db exists collection exists	src/mindroom/knowledge/manager.py:268; src/mindroom/knowledge/manager.py:1020
_utc_now	function	lines 124-125	related-only	datetime.now tz UTC isoformat last_published_at created_at	src/mindroom/knowledge/manager.py:1425; src/mindroom/attachments.py:435; src/mindroom/knowledge/utils.py:143
_run_to_thread_to_completion_on_cancel	async_function	lines 128-138	duplicate-found	asyncio.create_task asyncio.to_thread asyncio.shield cancellation completion	src/mindroom/api/knowledge.py:190; src/mindroom/knowledge/manager.py:1390; src/mindroom/knowledge/manager.py:1413; src/mindroom/voice_handler.py:188
_published_index_key_from_binding	function	lines 141-159	related-only	resolve binding to storage root knowledge path indexing settings key	src/mindroom/knowledge/manager.py:852; src/mindroom/runtime_resolution.py:259
_resolve_published_index_key_and_binding	function	lines 162-178	related-only	resolve_knowledge_binding start_watchers false create false binding key	src/mindroom/knowledge/refresh_runner.py:515; src/mindroom/knowledge/refresh_scheduler.py:119
resolve_published_index_key	function	lines 181-197	related-only	resolve published index key public facade	src/mindroom/knowledge/status.py:95; src/mindroom/knowledge/watch.py:67
refresh_target_for_published_index_key	function	lines 200-206	related-only	map published key to refresh target base storage path	src/mindroom/knowledge/refresh_scheduler.py:42; src/mindroom/knowledge/watch.py:15
source_root_for_refresh_target	function	lines 209-211	related-only	map refresh target to source root	src/mindroom/knowledge/watch.py:73
source_root_for_published_index_key	function	lines 214-216	related-only	map published key to source root	src/mindroom/knowledge/watch.py:58
resolve_refresh_target	function	lines 219-236	related-only	resolve base id to refresh target	src/mindroom/knowledge/refresh_scheduler.py:61; src/mindroom/knowledge/watch.py:67; src/mindroom/knowledge/watch.py:95
published_index_storage_path	function	lines 239-244	duplicate-found	knowledge_db base_storage_key storage path indexing settings path	src/mindroom/knowledge/manager.py:276; src/mindroom/knowledge/manager.py:833
published_index_metadata_path	function	lines 247-249	duplicate-found	indexing_settings.json metadata path	base path	src/mindroom/knowledge/manager.py:836
_coerce_nonnegative_int	function	lines 252-261	duplicate-found	coerce int indexed_count nonnegative json metadata	src/mindroom/knowledge/manager.py:905; src/mindroom/knowledge/manager.py:914
_coerce_status	function	lines 264-267	duplicate-found	status resetting indexing complete failed persisted metadata	src/mindroom/knowledge/manager.py:58; src/mindroom/knowledge/manager.py:911
_coerce_refresh_job	function	lines 270-273	none-found	refresh_job idle pending running failed metadata coercion	none
_optional_str	function	lines 276-277	duplicate-found	optional non-empty string metadata fields last_published_at published_revision	src/mindroom/knowledge/manager.py:920; src/mindroom/knowledge/manager.py:924; src/mindroom/attachments.py:590
load_published_index_state	function	lines 280-315	duplicate-found	load indexing_settings json persisted state validate settings status collection indexed_count source_signature	src/mindroom/knowledge/manager.py:892; src/mindroom/attachments.py:590
save_published_index_state	function	lines 318-350	duplicate-found	atomic json write temp path uuid pid replace indexing_settings	src/mindroom/knowledge/manager.py:938; src/mindroom/attachments.py:442
published_index_refresh_state	function	lines 353-367	none-found	refresh state none stale refreshing refresh_failed derived metadata	none
_state_with_refresh_fields	function	lines 370-398	none-found	replace refresh fields last_error reason last_refresh_at	none
mark_published_index_stale	function	lines 401-416	related-only	mark stale pending reason save state refresh job	src/mindroom/knowledge/refresh_runner.py:500; src/mindroom/knowledge/refresh_runner.py:835
mark_published_index_refresh_running	function	lines 419-429	related-only	mark refresh running state reason	src/mindroom/knowledge/refresh_runner.py:490
mark_published_index_refresh_failed_preserving_last_good	function	lines 432-444	related-only	refresh failed preserving complete last good state	src/mindroom/knowledge/refresh_runner.py:415; src/mindroom/knowledge/refresh_runner.py:541; src/mindroom/knowledge/refresh_runner.py:617
mark_published_index_refresh_succeeded	function	lines 447-462	related-only	clear refresh state success idle last_refresh_at	src/mindroom/knowledge/refresh_runner.py:667; src/mindroom/knowledge/refresh_runner.py:744; src/mindroom/knowledge/refresh_runner.py:833
_state_collection_name	function	lines 465-469	none-found	require collection name value error published metadata	none
_build_published_index_vector_db	function	lines 472-487	duplicate-found	ChromaDb collection path persistent_client embedder create_embedder	src/mindroom/knowledge/manager.py:1327
_build_published_index_knowledge	function	lines 490-499	duplicate-found	Knowledge vector_db build_vector_db	src/mindroom/knowledge/manager.py:1335; src/mindroom/knowledge/utils.py:617
published_index_collection_exists_for_state	function	lines 502-515	duplicate-found	status complete collection exists chroma_collection_exists warning false	src/mindroom/knowledge/manager.py:877; src/mindroom/knowledge/manager.py:260
indexing_settings_query_compatible	function	lines 518-526	related-only	indexing settings query compatible prefix length constants	src/mindroom/knowledge/manager.py:128; src/mindroom/knowledge/refresh_runner.py:686
indexing_settings_corpus_compatible	function	lines 529-537	related-only	indexing settings corpus compatible indexes constants	src/mindroom/knowledge/manager.py:135; src/mindroom/knowledge/refresh_runner.py:814
indexing_settings_metadata_equal	function	lines 540-545	related-only	indexing settings exact equality metadata	src/mindroom/knowledge/manager.py:311
published_index_settings_compatible	function	lines 548-556	related-only	query compatible corpus compatible published settings	src/mindroom/knowledge/refresh_runner.py:686; src/mindroom/knowledge/refresh_runner.py:814
_published_index_state_queryable	function	lines 559-564	related-only	complete collection settings compatible queryable state	src/mindroom/knowledge/manager.py:877; src/mindroom/knowledge/refresh_runner.py:686
_published_index_availability	function	lines 567-595	none-found	KnowledgeAvailability READY STALE CONFIG_MISMATCH REFRESH_FAILED	none
published_index_availability_for_state	function	lines 598-609	related-only	public availability wrapper for state	src/mindroom/knowledge/status.py:1
_cached_index_still_queryable	function	lines 612-616	related-only	cached index vector db exists queryable handle	src/mindroom/knowledge/manager.py:1018
_cached_index_matches_persisted_state	function	lines 619-632	none-found	cache handle persisted state field comparison	none
_load_queryable_index_from_state	function	lines 635-646	related-only	load queryable index from metadata state collection exists build knowledge	src/mindroom/knowledge/manager.py:877; src/mindroom/knowledge/manager.py:1327
get_published_index	function	lines 649-735	none-found	published index cache resolution availability fallback schedule refresh on access	none
publish_knowledge_index	function	lines 738-754	none-found	publish read handle evict refresh target cache	none
publish_knowledge_index_from_state	function	lines 757-769	related-only	rebuild publish handle from persisted metadata	src/mindroom/knowledge/refresh_runner.py:686
published_indexed_count	function	lines 772-774	none-found	indexed_count default zero accessor	none
_same_physical_binding	function	lines 777-782	related-only	compare base_id storage_root knowledge_path refresh target	src/mindroom/knowledge/refresh_scheduler.py:42
_same_physical_source	function	lines 785-786	duplicate-found	compare same knowledge source root storage_root knowledge_path	src/mindroom/api/knowledge.py:175; src/mindroom/knowledge/watch.py:58
_published_index_key_is_private	function	lines 789-790	related-only	private knowledge base id prefix startswith	src/mindroom/config/main.py:670; src/mindroom/config/main.py:1220
prune_private_index_bookkeeping	function	lines 793-797	none-found	bound private published index handles max cache	none
_cache_published_index	function	lines 800-802	none-found	cache published index prune private bookkeeping	none
_evict_published_indexes_for_refresh_target	function	lines 805-808	related-only	evict cached handles for refresh target same physical binding	none
_published_index_keys_for_shared_source	function	lines 811-847	duplicate-found	find same-source knowledge aliases over config knowledge_bases	src/mindroom/api/knowledge.py:175; src/mindroom/knowledge/watch.py:58
_mark_published_index_key_stale_on_disk	function	lines 850-853	none-found	mark key stale on disk evict cache	none
mark_knowledge_source_changed	function	lines 856-873	related-only	mark same-source published indexes stale return affected base ids	src/mindroom/api/knowledge.py:190; src/mindroom/knowledge/watch.py:252
mark_knowledge_source_changed_async	async_function	lines 876-892	duplicate-found	async to_thread shield committed mutation completion	src/mindroom/api/knowledge.py:190; src/mindroom/knowledge/manager.py:1413
clear_published_indexes	function	lines 895-897	none-found	clear process local published indexes cache	none
```

## Findings

### 1. Published index metadata load/save logic is duplicated with `KnowledgeManager`

`src/mindroom/knowledge/registry.py:280` loads `indexing_settings.json`, validates the same core fields (`settings`, `status`, `collection`, `indexed_count`, `source_signature`), coerces optional string metadata, and returns a dataclass state.
`src/mindroom/knowledge/manager.py:892` performs the same persisted JSON read and validation for `_PersistedIndexState`.
The registry version additionally accepts `failed` status and refresh-only fields (`refresh_job`, `reason`, `last_error`, `updated_at`, `last_refresh_at`), while the manager version requires a complete published collection.

`src/mindroom/knowledge/registry.py:318` and `src/mindroom/knowledge/manager.py:938` also duplicate payload assembly and atomic JSON persistence via a hidden temp file with process id, UUID, `json.dumps(..., sort_keys=True)`, and `Path.replace`.
`src/mindroom/attachments.py:442` has a similar atomic JSON metadata write, but it is a repository-wide persistence idiom rather than knowledge-index-specific duplication.

### 2. Published index storage path and Chroma/Knowledge construction mirror manager internals

`src/mindroom/knowledge/registry.py:239` computes the knowledge DB directory as `storage_root / "knowledge_db" / _base_storage_key(...)`.
`src/mindroom/knowledge/manager.py:833` computes the same storage directory for `KnowledgeManager`.

`src/mindroom/knowledge/registry.py:472` builds a `ChromaDb` with the published collection, storage path, persistent client, and `_create_embedder`.
`src/mindroom/knowledge/manager.py:1327` builds a manager `ChromaDb` with the same construction pattern.
`src/mindroom/knowledge/registry.py:490` and `src/mindroom/knowledge/manager.py:1335` then both wrap the vector DB in `Knowledge`.
This duplication is real, but it is currently small and bound to different owners: registry reopens a published read handle, while manager owns indexing and candidate publication.

### 3. Same-source alias fanout is repeated across registry, API, and watcher grouping

`src/mindroom/knowledge/registry.py:811` resolves each configured knowledge base to a `PublishedIndexKey`, compares physical source identity via `storage_root` and `knowledge_path`, and returns all matching keys to mark them stale.
`src/mindroom/api/knowledge.py:175` has a fallback same-source scan over `config.knowledge_bases`, comparing resolved knowledge roots before scheduling refreshes around committed mutations.
`src/mindroom/knowledge/watch.py:58` groups local watch targets by `KnowledgeSourceRoot`, which is the same source-root concept used by the registry.

The functional overlap is "same physical source alias fanout", but the call sites preserve different behavior.
The registry needs published-index keys and cache eviction, the API needs a conservative pre-mark fallback for scheduling in `finally`, and the watcher excludes private bases and Git-backed bases.

### 4. Cancellation-safe offloading appears in several knowledge paths

`src/mindroom/knowledge/registry.py:128` creates a `to_thread` task, shields it, and waits for completion even if the caller is cancelled.
`src/mindroom/api/knowledge.py:190` uses the same shield-and-complete behavior for committed source mutations.
`src/mindroom/knowledge/manager.py:1413` uses the same pattern when saving candidate publish metadata, returning a cancellation marker after the save completes.
These are related cancellation semantics with small return-value differences.

## Proposed Generalization

1. Extract a small knowledge metadata codec in `src/mindroom/knowledge/index_metadata.py` that owns the shared `indexing_settings.json` read/write primitives, non-empty optional string parsing, nonnegative integer coercion, and atomic JSON write.
2. Keep `PublishedIndexState` and `_PersistedIndexState` separate initially, but have both callers use shared field readers and payload writers.
3. Add a focused helper in `src/mindroom/knowledge/manager.py` or the new metadata module for `knowledge_index_storage_path(storage_root, base_id, knowledge_path)` so registry and manager do not independently reconstruct the same path.
4. Do not generalize same-source fanout yet unless another caller needs the exact registry semantics; the current differences are behaviorally important.
5. Consider a small async helper for "shield a task and complete it on cancellation" only if another cancellation-safe `to_thread` write path is added.

## Risk/tests

The metadata codec is the safest refactor target, but it has compatibility risk because `indexing_settings.json` is the handoff between indexing, refresh state, and query-time read handles.
Tests should cover malformed JSON, missing files, invalid status values, nonnegative integer coercion, complete-state required fields, refresh-only fields, and atomic save output for both registry and manager callers.

Chroma/Knowledge construction refactoring would risk changing embedder selection or collection path resolution, so it should be covered by tests that verify manager indexing and registry read-handle reopening use the same storage path and collection.

Same-source fanout should not be merged without tests for duplicate aliases, private knowledge bases, Git-backed bases, and API mutation scheduling after cancellation.
