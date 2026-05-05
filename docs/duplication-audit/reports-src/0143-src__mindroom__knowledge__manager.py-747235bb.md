## Summary

Top duplication candidates for `src/mindroom/knowledge/manager.py`:

1. Published index metadata parsing and atomic persistence are duplicated between `KnowledgeManager._load_persisted_index_state` / `_save_persisted_index_state` and `knowledge.registry.load_published_index_state` / `save_published_index_state`.
2. Published Chroma/Knowledge construction is duplicated between manager construction helpers and `knowledge.registry` lookup construction, though registry intentionally builds read-only published objects from resolved metadata.
3. Knowledge root/path containment and cancellation cleanup patterns are related in the API, workspace, and refresh subprocess code, but they differ enough in error surface and lifecycle ownership that I would not generalize them as part of this module.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_CollectionListingClient	class	lines 152-157	none-found	list_collections protocol collection name cleanup	src/mindroom/knowledge/registry.py:502
_CollectionListingClient.list_collections	method	lines 155-157	none-found	list_collections collection objects Chroma client	src/mindroom/knowledge/manager.py:1377; src/mindroom/knowledge/registry.py:502
_NamedCollection	class	lines 161-164	none-found	NamedCollection protocol name attribute collection object	none
_CollectionExistenceEmbedder	class	lines 167-188	none-found	collection existence embedder must not embed ChromaDb exists	src/mindroom/knowledge/registry.py:502
_CollectionExistenceEmbedder.get_embedding	method	lines 170-173	none-found	get_embedding NotImplemented collection existence	none
_CollectionExistenceEmbedder.get_embedding_and_usage	method	lines 175-178	none-found	get_embedding_and_usage NotImplemented collection existence	none
_CollectionExistenceEmbedder.async_get_embedding	async_method	lines 180-183	none-found	async_get_embedding NotImplemented collection existence	none
_CollectionExistenceEmbedder.async_get_embedding_and_usage	async_method	lines 185-188	none-found	async_get_embedding_and_usage NotImplemented collection existence	none
_PersistedIndexState	class	lines 192-199	duplicate-found	persisted index state metadata settings status collection indexed_count source_signature	src/mindroom/knowledge/registry.py:280; src/mindroom/knowledge/registry.py:318
_CandidatePublishState	class	lines 203-204	none-found	candidate publish state index_published unpublished cleanup	none
_raise_cancelled	function	lines 207-208	related-only	raise CancelledError after shielded publish cancellation	src/mindroom/api/knowledge.py:614; src/mindroom/api/knowledge.py:647
_resolve_knowledge_path	function	lines 211-215	related-only	resolve_config_relative_path knowledge path runtime_paths	src/mindroom/api/knowledge.py:60; src/mindroom/runtime_resolution.py:232; src/mindroom/runtime_resolution.py:283
_ensure_knowledge_directory_ready	function	lines 218-222	related-only	mkdir parents exist_ok directory must be directory knowledge root	src/mindroom/api/knowledge.py:60; src/mindroom/workspaces.py:249
git_checkout_present	function	lines 225-252	related-only	git rev-parse is-inside-work-tree show-toplevel timeout	src/mindroom/knowledge/manager.py:664; src/mindroom/knowledge/manager.py:1017
_git_metadata_present	function	lines 255-257	none-found	.git exists cheap metadata root is_dir	none
chroma_collection_exists	function	lines 260-268	related-only	ChromaDb exists collection embedder published collection exists	src/mindroom/knowledge/registry.py:472; src/mindroom/knowledge/registry.py:502
_safe_identifier	function	lines 271-273	none-found	safe identifier alnum underscore hyphen default	none
_base_storage_key	function	lines 276-279	duplicate-found	knowledge_db base storage key sha256 base_id knowledge_path	src/mindroom/knowledge/registry.py:239
_collection_name	function	lines 282-283	none-found	mindroom_knowledge collection prefix base storage key	none
_indexing_settings_key	function	lines 286-314	related-only	indexing settings key embedder signature git config include exclude extensions	src/mindroom/knowledge/registry.py:291; src/mindroom/knowledge/utils.py:200
_create_embedder	function	lines 317-344	duplicate-found	create embedder openai ollama sentence_transformers ChromaDb	src/mindroom/knowledge/registry.py:472
_coerce_int	function	lines 347-358	duplicate-found	coerce int bool float is_integer string digit metadata indexed_count	src/mindroom/knowledge/registry.py:252; src/mindroom/tool_system/metadata.py:341; src/mindroom/custom_tools/google_drive.py:68
_credential_free_repo_url	function	lines 361-388	related-only	credential free repo url urlparse userinfo strip credentials	src/mindroom/knowledge/redaction.py:73
_authenticated_repo_url	function	lines 391-428	related-only	credentials service username token password repo url userinfo	src/mindroom/knowledge/manager.py:431; src/mindroom/credentials.py:1
_credentials_service_http_userinfo	function	lines 431-453	duplicate-found	credentials service username token password x-access-token	src/mindroom/knowledge/manager.py:391
_git_http_basic_auth_env	function	lines 456-462	none-found	GIT_CONFIG_COUNT extraHeader Authorization Basic base64	none
_git_auth_env	function	lines 465-498	related-only	git auth env embedded userinfo insteadOf credentials	src/mindroom/knowledge/utils.py:225; src/mindroom/knowledge/redaction.py:98
_merge_git_env	function	lines 501-506	none-found	merge optional env dicts	none
_split_posix_parts	function	lines 509-515	none-found	split posix parts strip root glob	none
_matches_root_glob	function	lines 518-552	none-found	root anchored glob double star fnmatchcase include exclude	none
_matches_root_glob.<locals>._match	nested_function	lines 527-550	not-a-behavior-symbol	nested memoized glob matcher covered by _matches_root_glob	none
_is_hidden_relative_path	function	lines 555-556	none-found	hidden relative path parts startswith dot	none
include_knowledge_relative_path	function	lines 559-578	related-only	include exclude patterns skip_hidden relative path validation	src/mindroom/api/knowledge.py:74; src/mindroom/workspaces.py:61
include_semantic_knowledge_relative_path	function	lines 581-594	related-only	include extensions exclude extensions text like suffix	src/mindroom/api/knowledge.py:476; src/mindroom/knowledge/manager.py:664
_path_is_symlink_or_under_symlink	function	lines 597-608	related-only	symlink under root relative_to current is_symlink	src/mindroom/workspaces.py:37; src/mindroom/workspaces.py:61
include_knowledge_file	function	lines 611-629	related-only	resolve root relative_to symlink file semantic include	src/mindroom/api/knowledge.py:74; src/mindroom/workspaces.py:37
list_knowledge_files	function	lines 632-646	related-only	os.walk followlinks false symlink prune sorted managed files	src/mindroom/workspaces.py:123
_semantic_file_paths_from_relative_paths	function	lines 649-661	related-only	relative paths to semantic files include_knowledge_file sorted set	src/mindroom/api/knowledge.py:103
_git_tracked_relative_paths_from_checkout	function	lines 664-704	related-only	git ls-files -z subprocess timeout redacted errors	src/mindroom/knowledge/manager.py:1138
list_git_tracked_knowledge_files	function	lines 707-723	related-only	git checkout present tracked semantic files public lister	src/mindroom/api/knowledge.py:1
_file_content_digest	function	lines 726-731	none-found	sha256 file chunks read 1MiB	none
knowledge_source_signature	function	lines 734-769	related-only	source signature files relative mtime size digest tracked paths	src/mindroom/knowledge/manager.py:772
_source_signature_from_file_signatures	function	lines 772-784	related-only	same source signature from file signatures mapping	src/mindroom/knowledge/manager.py:734
KnowledgeManager	class	lines 788-1705	related-only	knowledge manager lifecycle git sync indexing Chroma published metadata	src/mindroom/knowledge/registry.py:280; src/mindroom/knowledge/refresh_runner.py:523
KnowledgeManager.__post_init__	method	lines 818-850	related-only	resolve paths ensure dir storage key load state build knowledge	src/mindroom/knowledge/registry.py:239; src/mindroom/knowledge/registry.py:472
KnowledgeManager._set_settings	method	lines 852-868	related-only	assign config runtime storage path indexing settings	none
KnowledgeManager._knowledge_source_path	method	lines 870-875	none-found	initialized knowledge path guard	none
KnowledgeManager._persisted_collection_missing	method	lines 877-890	related-only	collection exists complete state warning fallback	src/mindroom/knowledge/registry.py:502
KnowledgeManager._load_persisted_index_state	method	lines 892-936	duplicate-found	load metadata json validate settings status collection count signature	src/mindroom/knowledge/registry.py:280
KnowledgeManager._save_persisted_index_state	method	lines 938-972	duplicate-found	atomic metadata json tmp replace unlink missing_ok	src/mindroom/knowledge/registry.py:318
KnowledgeManager._load_git_lfs_hydrated_head	method	lines 974-979	none-found	read hydrated head text strip OSError	none
KnowledgeManager._save_git_lfs_hydrated_head	method	lines 981-982	none-found	write hydrated head text	none
KnowledgeManager._clear_git_lfs_hydrated_head	method	lines 984-985	none-found	unlink hydrated head missing_ok	none
KnowledgeManager._has_existing_index	method	lines 987-989	related-only	ChromaDb exists current vector_db	src/mindroom/knowledge/registry.py:502
KnowledgeManager._needs_full_reindex_on_create	method	lines 991-999	related-only	persisted metadata mismatch resetting existing index	src/mindroom/knowledge/registry.py:353
KnowledgeManager._git_config	method	lines 1001-1002	none-found	get knowledge base git config	none
KnowledgeManager._git_uses_lfs	method	lines 1004-1006	none-found	bool git config lfs	none
KnowledgeManager._mark_git_initial_sync_complete	method	lines 1008-1009	none-found	mark initial sync complete refresh runner calls	none
KnowledgeManager._git_sync_timeout_seconds	method	lines 1011-1015	related-only	git sync timeout seconds float	src/mindroom/knowledge/manager.py:664
KnowledgeManager._git_checkout_present	async_method	lines 1017-1022	related-only	to_thread git_checkout_present timeout	src/mindroom/knowledge/manager.py:225
KnowledgeManager._include_semantic_relative_path	method	lines 1024-1028	related-only	wrapper include semantic relative path	src/mindroom/knowledge/manager.py:581
KnowledgeManager._include_relative_path	method	lines 1030-1031	related-only	wrapper include knowledge relative path	src/mindroom/knowledge/manager.py:559
KnowledgeManager._run_git	async_method	lines 1033-1080	related-only	async subprocess timeout cancellation kill redact errors	src/mindroom/knowledge/refresh_runner.py:230; src/mindroom/tools/shell.py:406
KnowledgeManager._ensure_git_lfs_available	async_method	lines 1082-1090	none-found	git lfs version checked runtime image	none
KnowledgeManager._ensure_git_lfs_repository_ready	async_method	lines 1092-1097	none-found	git lfs install local repository ready	none
KnowledgeManager._git_lfs_skip_smudge_env	method	lines 1099-1102	none-found	GIT_LFS_SKIP_SMUDGE env	none
KnowledgeManager._git_lfs_pull_args	method	lines 1104-1105	none-found	git lfs pull origin branch args	none
KnowledgeManager._hydrate_git_lfs_worktree	async_method	lines 1107-1129	none-found	hydrate git lfs worktree cached head	none
KnowledgeManager._git_rev_parse	async_method	lines 1131-1136	related-only	git rev-parse wrapper returning None on RuntimeError	src/mindroom/knowledge/manager.py:225
KnowledgeManager._git_list_tracked_files	async_method	lines 1138-1143	related-only	git ls-files -z filter semantic tracked paths	src/mindroom/knowledge/manager.py:664
KnowledgeManager._ensure_git_repository	async_method	lines 1145-1188	related-only	git clone existing checkout remote set-url lfs hydrate	src/mindroom/knowledge/manager.py:225; src/mindroom/knowledge/manager.py:465
KnowledgeManager._sync_git_source_once	async_method	lines 1190-1231	none-found	git fetch checkout reset diff changed removed files	none
KnowledgeManager.list_files	method	lines 1233-1253	related-only	list files git tracked cache or filesystem managed files	src/mindroom/api/knowledge.py:88
KnowledgeManager._relative_path	method	lines 1255-1256	related-only	file relative to knowledge source as_posix	src/mindroom/api/knowledge.py:106; src/mindroom/api/knowledge.py:637
KnowledgeManager._file_signature	method	lines 1258-1260	related-only	stat mtime size digest tuple	src/mindroom/knowledge/manager.py:734
KnowledgeManager._has_vectors_for_source_path	method	lines 1262-1282	none-found	Chroma get where source_path ids limit include none	none
KnowledgeManager._wait_for_source_vectors	async_method	lines 1284-1301	none-found	post insert vector visibility retry delays to_thread	none
KnowledgeManager._build_reader	method	lines 1303-1319	none-found	ReaderFactory text markdown SafeFixedSizeChunking chunk size overlap	none
KnowledgeManager._default_collection_name	method	lines 1321-1322	related-only	default collection name helper	src/mindroom/knowledge/manager.py:282
KnowledgeManager._candidate_collection_name	method	lines 1324-1325	none-found	candidate collection time uuid suffix	none
KnowledgeManager._build_vector_db	method	lines 1327-1333	duplicate-found	ChromaDb collection path persistent_client embedder	src/mindroom/knowledge/registry.py:472
KnowledgeManager._build_knowledge	method	lines 1335-1336	duplicate-found	Knowledge vector_db wrapper construction	src/mindroom/knowledge/registry.py:490; src/mindroom/knowledge/utils.py:617
KnowledgeManager._cleanup_superseded_collections	method	lines 1338-1375	none-found	cleanup superseded default candidate collections delete warning	none
KnowledgeManager._listed_collection_names	method	lines 1377-1384	none-found	list collection names strings objects dedupe	none
KnowledgeManager._reset_vector_db	method	lines 1386-1388	none-found	vector db delete create reset	none
KnowledgeManager._delete_unpublished_candidate_vector_db	async_method	lines 1390-1411	related-only	shield cleanup task cancellation delete vector db warning	src/mindroom/knowledge/refresh_runner.py:242
KnowledgeManager._save_candidate_publish_metadata	async_method	lines 1413-1436	related-only	shield metadata save publish cancellation	src/mindroom/knowledge/refresh_runner.py:242
KnowledgeManager._adopt_candidate_vector_db	async_method	lines 1438-1448	none-found	adopt candidate vector db state lock indexed files signatures	none
KnowledgeManager._publish_candidate_after_metadata_save	async_method	lines 1450-1472	related-only	save metadata adopt candidate raise cancelled after publish	src/mindroom/api/knowledge.py:614
KnowledgeManager.sync_git_source	async_method	lines 1474-1511	related-only	git sync status updated changed removed error redaction	src/mindroom/knowledge/refresh_runner.py:538
KnowledgeManager._index_file_locked	async_method	lines 1513-1591	none-found	index one file metadata remove vectors insert wait empty file state	none
KnowledgeManager._reindex_files_locked	async_method	lines 1593-1633	none-found	bounded concurrency semaphore gather index files	none
KnowledgeManager._reindex_files_locked.<locals>._index_one	nested_async_function	lines 1622-1630	not-a-behavior-symbol	nested semaphore wrapper covered by _reindex_files_locked	none
KnowledgeManager.reindex_all	async_method	lines 1635-1705	related-only	candidate collection reset reindex validate signatures publish cleanup	src/mindroom/knowledge/refresh_runner.py:523
```

## Findings

### 1. Published index metadata parsing and atomic persistence are duplicated

`KnowledgeManager._load_persisted_index_state` reads `indexing_settings.json`, parses JSON, validates `settings`, `status`, `collection`, `indexed_count`, and `source_signature`, coerces optional string fields, and returns a dataclass.
`knowledge.registry.load_published_index_state` performs the same metadata read/validation flow for the same file and fields at `src/mindroom/knowledge/registry.py:280`.

`KnowledgeManager._save_persisted_index_state` serializes the same metadata shape with optional fields and persists atomically with a dot-prefixed temp file, PID, UUID, `replace`, and cleanup on failure.
`knowledge.registry.save_published_index_state` duplicates that atomic write flow at `src/mindroom/knowledge/registry.py:318`.

Differences to preserve:

- Registry supports additional refresh orchestration fields: `refresh_job`, `reason`, `last_error`, `updated_at`, and `last_refresh_at`.
- Manager currently requires complete-state fields unconditionally when loading, while registry only requires those fields when status is `complete`.
- Manager uses `_PersistedIndexState`; registry uses `PublishedIndexState`.

### 2. Integer coercion for persisted counts is duplicated

`_coerce_int` in manager accepts non-bool ints, integral floats, and signed digit strings.
`knowledge.registry._coerce_nonnegative_int` at `src/mindroom/knowledge/registry.py:252` is the same persisted metadata count coercion with a nonnegative constraint.
Similar generic numeric config coercion exists in `src/mindroom/tool_system/metadata.py:341` and `src/mindroom/custom_tools/google_drive.py:68`, but those operate on tool config values and should stay separate.

Differences to preserve:

- Persisted `indexed_count` should reject negative values in both manager and registry callers.
- Generic tool config coercion intentionally returns floats in some cases and is not the same behavior.

### 3. Published Chroma/Knowledge construction is duplicated

`KnowledgeManager._build_vector_db` constructs a persistent `ChromaDb` with collection name, base storage path, and `_create_embedder`.
`knowledge.registry._build_published_index_vector_db` does the same Chroma construction at `src/mindroom/knowledge/registry.py:472`, using the published key and state.

`KnowledgeManager._build_knowledge` wraps that vector DB in `Knowledge`.
`knowledge.registry._build_published_index_knowledge` mirrors the wrapper at `src/mindroom/knowledge/registry.py:490`.

Differences to preserve:

- Manager writes to the active manager storage path and can create candidate collections.
- Registry resolves a previously published collection from `PublishedIndexKey`/`PublishedIndexState` for read access.
- Registry uses casts for a published-index vector DB protocol.

## Proposed Generalization

Minimal refactor recommended only for the metadata duplication.

Add a focused metadata codec in `src/mindroom/knowledge/registry.py` or a small sibling module such as `src/mindroom/knowledge/index_metadata.py` that provides:

- a shared atomic JSON write helper for index metadata files,
- a shared load function that parses common fields and accepts an option for manager-strict complete metadata,
- a nonnegative integer coercion helper for persisted index counts.

Do not generalize Git command execution, path containment, or cancellation cleanup from this audit.
Those areas are related but have different API error types, lifecycle ownership, and cleanup semantics.

For vector DB construction, a very small helper could be considered later, but the current duplication is low-risk and keeping registry construction explicit preserves readability.

## Risk/tests

Metadata deduplication risks:

- Accidentally accepting incomplete published metadata during manager startup.
- Accidentally dropping registry refresh fields when manager publishes a new complete index.
- Changing atomic write behavior or temp-file cleanup.

Tests to update or add:

- Manager persisted state load rejects invalid `indexed_count`, missing collection, missing source signature, and unsupported status.
- Registry load preserves refresh fields and still allows non-complete states without complete-only fields.
- Atomic save writes sorted JSON and removes temp files on write/replace failure.
- Existing knowledge refresh tests that cover `refresh_runner` and published index resolution should be run after any refactor.
