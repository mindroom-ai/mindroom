## Summary

Top duplication candidates for `src/mindroom/knowledge/watch.py`:

- Knowledge source mutation fanout plus refresh scheduling is repeated between filesystem watcher scheduling and dashboard/API committed mutations.
- Knowledge semantic path filtering is intentionally shared through `include_semantic_knowledge_relative_path`, but `watch.py` repeats a lightweight path-to-relative wrapper also present in file inclusion and API mutation paths.
- Async task cancellation and stop-event polling follow common local patterns also used by the refresh scheduler, API lifespan workers, config watchers, and file watchers.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_WatchTarget	class	lines 32-35	not-a-behavior-symbol	WatchTarget KnowledgeSourceRoot base_ids dataclass	src/mindroom/knowledge/watch.py:58; src/mindroom/knowledge/registry.py:53; src/mindroom/knowledge/registry.py:62
_GitPollTarget	class	lines 39-42	not-a-behavior-symbol	GitPollTarget poll_interval_seconds KnowledgeRefreshTarget dataclass	src/mindroom/knowledge/watch.py:83; src/mindroom/knowledge/utils.py:146
_WatchTask	class	lines 46-48	related-only	stop_event task dataclass cancel shutdown	src/mindroom/knowledge/refresh_scheduler.py:110; src/mindroom/api/main.py:377; src/mindroom/orchestration/runtime.py:156
_ensure_watch_root	function	lines 51-55	related-only	Knowledge path must be directory mkdir parents exist_ok	src/mindroom/knowledge/manager.py:222; src/mindroom/api/knowledge.py:68; src/mindroom/workspaces.py:258
_shared_local_watch_targets	function	lines 58-80	related-only	knowledge_bases watch git private resolve_refresh_target source_root_for_refresh_target	src/mindroom/runtime_resolution.py:269; src/mindroom/knowledge/registry.py:219; src/mindroom/api/knowledge.py:175
_shared_git_poll_targets	function	lines 83-106	related-only	knowledge_bases git poll_interval_seconds private resolve_refresh_target	src/mindroom/knowledge/utils.py:146; src/mindroom/runtime_resolution.py:269; src/mindroom/knowledge/registry.py:219
_changed_path_is_indexable	function	lines 109-117	related-only	relative_to as_posix include_semantic_knowledge_relative_path	src/mindroom/knowledge/manager.py:611; src/mindroom/api/knowledge.py:405; src/mindroom/api/knowledge.py:637
_changes_include_indexable_path	function	lines 120-130	none-found	watchfiles Change added modified deleted include indexable path	none
KnowledgeSourceWatcher	class	lines 133-269	related-only	source watcher lifecycle sync shutdown poll schedule refresh	src/mindroom/api/main.py:289; src/mindroom/orchestrator.py:669; src/mindroom/knowledge/refresh_scheduler.py:42
KnowledgeSourceWatcher.__init__	method	lines 136-139	not-a-behavior-symbol	refresh_scheduler filesystem_tasks git_poll_tasks initialization	src/mindroom/knowledge/refresh_scheduler.py:42; src/mindroom/orchestrator.py:277
KnowledgeSourceWatcher.sync	async_method	lines 141-169	related-only	shutdown create_task stop_event watcher started poller started	src/mindroom/api/main.py:377; src/mindroom/orchestration/runtime.py:212; src/mindroom/mcp/manager.py:391
KnowledgeSourceWatcher.shutdown	async_method	lines 171-182	duplicate-found	cancel tasks clear dict suppress CancelledError shutdown	src/mindroom/knowledge/refresh_scheduler.py:110; src/mindroom/api/main.py:383; src/mindroom/orchestrator.py:2017
KnowledgeSourceWatcher._watch_source	async_method	lines 184-210	related-only	awatch changes callback exception continuing watcher stopped	src/mindroom/file_watcher.py:31; src/mindroom/api/main.py:298; src/mindroom/orchestration/plugin_watch.py:40
KnowledgeSourceWatcher._poll_git_source	async_method	lines 212-241	related-only	while not stop_event wait_for timeout schedule_refresh poll_interval_seconds	src/mindroom/api/main.py:166; src/mindroom/api/main.py:298; src/mindroom/memory/auto_flush.py:575
KnowledgeSourceWatcher._schedule_refresh_for_target	async_method	lines 243-269	duplicate-found	mark_knowledge_source_changed_async changed_base_ids schedule_refresh watch git dashboard_upload dashboard_delete	src/mindroom/api/knowledge.py:190; src/mindroom/api/knowledge.py:211; src/mindroom/api/knowledge.py:164
```

## Findings

### 1. Source-change fanout and refresh scheduling is repeated

`KnowledgeSourceWatcher._schedule_refresh_for_target` marks a knowledge source changed, iterates affected base IDs, deduplicates them, filters to watched non-Git bases, and schedules refreshes through `KnowledgeRefreshScheduler` at `src/mindroom/knowledge/watch.py:250`.
Dashboard/API mutations use the same underlying source-change fanout and refresh scheduling path in `_mark_committed_mutation_and_schedule_refresh` at `src/mindroom/api/knowledge.py:211`, with `_schedule_refreshes` deduplicating base IDs at `src/mindroom/api/knowledge.py:164`.

The behavior is functionally related because both paths convert one source mutation into refresh work for all affected aliases returned by `mark_knowledge_source_changed_async`.
The watcher path preserves an important difference: it schedules only shared local watched, non-Git bases, because Git bases are polled and private bases are not watcher-owned.
The API path schedules a broader fallback set from `_same_source_base_ids` so committed dashboard mutations are refreshed even if cancellation happens after the source changed.

### 2. Task shutdown pattern is duplicated

`KnowledgeSourceWatcher.shutdown` gathers owned tasks, clears owner dictionaries, cancels tasks, and awaits each task under `suppress(asyncio.CancelledError)` at `src/mindroom/knowledge/watch.py:171`.
`KnowledgeRefreshScheduler.shutdown` does the same owner-dict clear, cancel, and suppressed await for scheduler tasks at `src/mindroom/knowledge/refresh_scheduler.py:110`.
The API lifespan also manually sets a shared stop event, cancels two tasks, and awaits under `suppress(asyncio.CancelledError)` at `src/mindroom/api/main.py:383`.

The behavior is repeated lifecycle cleanup, but the exact state containers differ.
`KnowledgeSourceWatcher` must set per-task stop events before cancellation, while `KnowledgeRefreshScheduler` also clears pending refresh requests and suppresses any exception, not only cancellation.

### 3. Path-to-relative semantic filtering is repeated in adjacent knowledge code

`_changed_path_is_indexable` converts an absolute changed path into a relative POSIX path under a knowledge root, rejects paths outside the root, and delegates semantic file eligibility to `include_semantic_knowledge_relative_path` at `src/mindroom/knowledge/watch.py:109`.
`include_knowledge_file` performs a stricter variant for actual files, including symlink and `is_file` checks, before calling the same semantic predicate at `src/mindroom/knowledge/manager.py:611`.
The API delete path computes a relative POSIX path and applies the same semantic predicate through `_reject_unmanaged_knowledge_file_path` at `src/mindroom/api/knowledge.py:637` and `src/mindroom/api/knowledge.py:405`.

This is related rather than a direct duplicate because watcher changes include delete events, where `is_file` and strict resolution cannot be required.
Any shared helper would need to distinguish "event path under root" from "existing managed file".

## Proposed Generalization

For the source-change scheduling duplication, consider a small helper in `mindroom.knowledge.registry` or a focused new `mindroom.knowledge.refresh_requests` module:

`async def mark_source_changed_and_schedule_refreshes(base_id, *, config, runtime_paths, refresh_scheduler, reason, include_base_id=False, filter_base_id=None) -> tuple[str, ...]`

The helper should call `mark_knowledge_source_changed_async`, dedupe affected IDs, apply an optional predicate for watcher-specific `watch and git is None`, and call `refresh_scheduler.schedule_refresh`.
Keep the dashboard cancellation behavior outside the helper, because that path intentionally shields committed mutations and schedules fallback IDs in `finally`.

For task shutdown, no refactor recommended unless a third owner grows the same per-task stop-event structure.
A tiny helper would have to account for different exception suppression policies and stop-event handling, so the current duplication is low-risk.

For path filtering, no refactor recommended.
The shared semantic predicate already exists, and the remaining wrappers differ because watcher delete events cannot require the file to exist.

## Risk/tests

Risks to preserve if refactoring source-change scheduling:

- Filesystem watcher must not schedule Git-backed bases from filesystem events.
- Filesystem watcher must not schedule private knowledge bases.
- Alias fanout from `mark_knowledge_source_changed_async` must remain deduplicated.
- Dashboard mutations must keep scheduling fallback same-source base IDs even if cancellation occurs after source change is committed.

Focused tests would need to cover `_schedule_refresh_for_target` with duplicate alias fanout, Git aliases, unwatched aliases, and dashboard mutation cancellation behavior in `src/mindroom/api/knowledge.py`.
Existing watcher tests, if present, should assert that `Change.deleted` still schedules for semantically indexable relative paths without requiring the file to exist.
