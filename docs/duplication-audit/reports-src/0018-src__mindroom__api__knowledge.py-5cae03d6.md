## Summary

Top duplication candidate: `src/mindroom/api/knowledge.py` has local path containment and path-overlap helpers that duplicate behavior already present in `src/mindroom/workspaces.py` and other path guards.
Related but not clearly duplicate: knowledge refresh scheduling/status payload assembly overlaps with `src/mindroom/knowledge/utils.py`, `src/mindroom/knowledge/watch.py`, and `src/mindroom/knowledge/refresh_scheduler.py`, but the API layer has different request/scheduler fallback semantics.
Most upload helpers are cohesive to this API route and did not show meaningful duplicated behavior elsewhere under `./src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_FileListInfo	class	lines 48-52	not-a-behavior-symbol	_FileListInfo file info dataclass degraded total_size	src/mindroom/knowledge/status.py:20; src/mindroom/knowledge/manager.py:1233
_ensure_base_exists	function	lines 55-57	related-only	knowledge_bases base_id not found get_knowledge_base_config HTTPException	src/mindroom/config/main.py:372; src/mindroom/runtime_resolution.py:269
_knowledge_root	function	lines 60-71	related-only	resolve_config_relative_path knowledge_bases path mkdir create	src/mindroom/runtime_resolution.py:283; src/mindroom/runtime_resolution.py:230; src/mindroom/knowledge/manager.py:1235
_resolve_within_root	function	lines 74-85	duplicate-found	resolve within root relative path absolute parent parts is_relative_to	src/mindroom/workspaces.py:37; src/mindroom/workspaces.py:61; src/mindroom/tools/file.py:39; src/mindroom/api/sandbox_exec.py:322
_list_file_info	async_function	lines 88-121	related-only	list files stat st_size modified suffix total_size degraded	src/mindroom/knowledge/manager.py:1233; src/mindroom/tools/file.py:212
_list_managed_file_paths	async_function	lines 124-140	related-only	list_knowledge_files list_git_tracked_knowledge_files git timeout redaction	src/mindroom/knowledge/manager.py:1233; src/mindroom/knowledge/manager.py:1639
_request_refresh_scheduler	function	lines 143-144	related-only	request knowledge_refresh_scheduler app_state	src/mindroom/api/openai_compat.py:824; src/mindroom/api/main.py:371
_schedule_refresh	function	lines 147-161	related-only	schedule_refresh config runtime_paths refresh_scheduler	src/mindroom/knowledge/utils.py:297; src/mindroom/knowledge/watch.py:222; src/mindroom/knowledge/watch.py:265
_schedule_refreshes	function	lines 164-172	related-only	deduplicate base_ids schedule_refresh dict.fromkeys	src/mindroom/knowledge/watch.py:250; src/mindroom/knowledge/watch.py:258
_same_source_base_ids	function	lines 175-187	related-only	same source base ids shared knowledge root resolve_config_relative_path	src/mindroom/knowledge/status.py:87; src/mindroom/knowledge/registry.py:440
_mark_source_changed_after_committed_mutation	async_function	lines 190-208	none-found	mark_knowledge_source_changed_async shield CancelledError committed mutation	none
_mark_committed_mutation_and_schedule_refresh	async_function	lines 211-230	related-only	mark source changed schedule refresh finally affected base ids	src/mindroom/knowledge/watch.py:243
_index_status_sync	function	lines 233-247	related-only	get_knowledge_index_status ValueError fallback KnowledgeIndexStatus	src/mindroom/knowledge/status.py:52; src/mindroom/knowledge/refresh_scheduler.py:71
_index_status	async_function	lines 250-255	related-only	asyncio.to_thread get_knowledge_index_status wrapper	src/mindroom/knowledge/manager.py:1639
_redacted_last_error	function	lines 258-261	related-only	redact_credentials_in_text last_error none	src/mindroom/knowledge/manager.py:1487; src/mindroom/knowledge/refresh_runner.py:540
_is_refreshing	function	lines 264-282	related-only	refresh_scheduler is_refreshing fallback is_refresh_active_for_binding	src/mindroom/knowledge/refresh_scheduler.py:62; src/mindroom/knowledge/utils.py:280
_git_status	async_function	lines 285-314	related-only	git status repo_url branch lfs initial_sync last_error repo_present	src/mindroom/knowledge/manager.py:1495; src/mindroom/knowledge/status.py:20
_path_overlaps	function	lines 317-318	duplicate-found	path overlap is_relative_to either direction	src/mindroom/workspaces.py:195; src/mindroom/workspaces.py:197
_git_backed_bases_for_target	function	lines 321-334	related-only	git backed bases target path overlaps resolve_config_relative_path	src/mindroom/runtime_resolution.py:221; src/mindroom/workspaces.py:179
_reject_git_file_mutation	function	lines 337-360	none-found	Git-backed mutation reject shared source path HTTPException 409	none
_validate_upload_size_hint	function	lines 363-373	related-only	seekable upload size hint MAX bytes	src/mindroom/api/sandbox_exec.py:360; src/mindroom/api/sandbox_exec.py:522
_upload_limit_error	function	lines 376-380	related-only	HTTPException 413 upload limit exceeds MiB	src/mindroom/api/sandbox_exec.py:360; src/mindroom/api/sandbox_exec.py:415
_ensure_within_upload_limit	function	lines 383-385	related-only	bytes_written upload limit max bytes	src/mindroom/api/sandbox_exec.py:522; src/mindroom/api/sandbox_exec.py:545
_stream_upload_to_destination	async_function	lines 388-394	none-found	UploadFile read chunk write destination bytes_written	none
_reject_non_file_upload_destination	function	lines 397-402	related-only	destination exists not regular file conflict	src/mindroom/workspaces.py:226; src/mindroom/tools/file.py:196
_reject_unmanaged_knowledge_file_path	function	lines 405-414	related-only	include_semantic_knowledge_relative_path managed file filters	src/mindroom/knowledge/manager.py:1227; src/mindroom/knowledge/manager.py:1247
_reject_duplicate_upload_destination	function	lines 417-421	none-found	duplicate upload destination batch HTTPException	none
_upload_temp_path	function	lines 424-425	related-only	temp upload uuid tmp path replace unlink	src/mindroom/api/config_lifecycle.py:415; src/mindroom/constants.py:1099
_UploadTarget	class	lines 429-433	not-a-behavior-symbol	UploadTarget dataclass upload destination filename relative_path	none
_StagedUpload	class	lines 437-440	not-a-behavior-symbol	StagedUpload dataclass temp_path destination relative_path	none
_stage_upload	async_function	lines 443-452	none-found	stage upload temp path cleanup CancelledError unlink	none
_write_uploads	async_function	lines 455-511	none-found	batch upload staging before_commit replace cleanup close UploadFile	none
list_knowledge_bases	async_function	lines 515-555	related-only	list knowledge bases status file_count indexed_count git refresh	src/mindroom/api/knowledge.py:658; src/mindroom/knowledge/status.py:52
list_knowledge_files	async_function	lines 559-572	related-only	list knowledge files route file_info total_size degraded	src/mindroom/api/knowledge.py:515; src/mindroom/knowledge/manager.py:1233
upload_knowledge_files	async_function	lines 576-621	none-found	upload knowledge files mutation lock write uploads source changed	none
upload_knowledge_files.<locals>._mark_uploaded_source_changed	nested_async_function	lines 589-596	none-found	nested mark uploaded source changed dashboard_upload	none
delete_knowledge_file	async_function	lines 625-654	related-only	delete file resolve root unmanaged mutation lock source changed	src/mindroom/tools/file.py:196
knowledge_status	async_function	lines 658-687	related-only	knowledge status route file_count indexed_count git refreshing last_error	src/mindroom/api/knowledge.py:515; src/mindroom/knowledge/status.py:52
reindex_knowledge	async_function	lines 691-745	related-only	reindex knowledge refresh_now refresh_knowledge_binding HTTPException detail	src/mindroom/knowledge/refresh_scheduler.py:83; src/mindroom/knowledge/refresh_runner.py:442
```

## Findings

### 1. Path containment checks are duplicated

`_resolve_within_root` in `src/mindroom/api/knowledge.py:74` validates a user-supplied relative path, rejects absolute and parent-traversal paths, resolves the joined path, and raises when the result escapes the root.
`resolve_relative_path_within_root` and `resolve_relative_path_within_root_preserving_leaf` in `src/mindroom/workspaces.py:37` and `src/mindroom/workspaces.py:61` implement the same core behavior: resolve a relative path under a root and reject escape through `..` or symlinks.
`_is_within_base_dir` in `src/mindroom/tools/file.py:39` and `resolve_workspace_env_hook_path` in `src/mindroom/api/sandbox_exec.py:322` are also related path containment guards, though they return booleans or domain-specific errors rather than API exceptions.

Differences to preserve:
`_resolve_within_root` raises `HTTPException` with route-specific messages.
`workspaces.py` raises `ValueError` and has two variants: one follows the leaf path, while the preserving-leaf variant is safer when the target may not exist yet.
The knowledge upload path currently resolves the full destination before writing; if a future helper is shared, the exact symlink behavior for new upload destinations should be tested before switching to the preserving-leaf variant.

### 2. Bidirectional path-overlap logic is duplicated in small local forms

`_path_overlaps` in `src/mindroom/api/knowledge.py:317` returns true when either path is relative to the other.
`_build_workspace_knowledge_links` in `src/mindroom/workspaces.py:195` and `src/mindroom/workspaces.py:197` performs the same bidirectional containment comparison inline while avoiding recursive knowledge symlinks.

Differences to preserve:
The workspace code has additional equality and desired-link filtering around the overlap checks.
The API helper is only used to reject mutations touching Git-backed roots.

## Proposed Generalization

1. Consider a small path helper in `src/mindroom/workspaces.py` or a focused `src/mindroom/path_safety.py` module for `path_overlaps(left: Path, right: Path) -> bool`.
2. If path resolution needs cleanup later, expose an exception-neutral helper that returns a resolved path or raises `ValueError`; keep FastAPI `HTTPException` translation inside `src/mindroom/api/knowledge.py`.
3. Do not refactor refresh scheduling/status or upload staging now; the similar code has different route, scheduler, and mutation-atomicity semantics.

## Risk/tests

Path containment is security-sensitive.
Tests should cover absolute paths, `..`, symlink escapes, nonexistent upload leaves, existing file replacement, existing directory rejection, and Git-backed shared-root rejection before any refactor.
Refresh/status code should remain unchanged unless route response snapshots and scheduler fallback behavior are covered.
