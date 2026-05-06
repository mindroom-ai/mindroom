# Duplication Audit: `src/mindroom/orchestration/plugin_watch.py`

## Summary

Top duplication candidates:

- `watch_plugins_task` repeats the same polling watcher skeleton and broad callback-continuation error handling as `src/mindroom/file_watcher.py:31` and `src/mindroom/api/main.py:298`, but it adds tree snapshots, configured-root rebinding, pending-change filtering, and debounce semantics.
- `sync_plugin_root_snapshots` and `replace_plugin_root_snapshots` share local root-pruning behavior with each other, but I did not find an equivalent active implementation elsewhere under `src`.
- `_path_is_under_any_root` is related to several path-containment checks in `src`, but those call sites enforce different security or plugin-origin semantics and are not direct duplicates.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_PluginWatcherRuntime	class	lines 20-37	related-only	Protocol watcher runtime reload_plugins_now sync_plugin_watch_roots hook registry	src/mindroom/orchestrator.py:759, src/mindroom/orchestrator.py:793, src/mindroom/knowledge/watch.py:133
_PluginWatcherRuntime._sync_plugin_watch_roots	method	lines 28-29	related-only	sync plugin watch roots configured roots snapshots	src/mindroom/orchestrator.py:759, src/mindroom/orchestration/plugin_watch.py:114, src/mindroom/knowledge/watch.py:141
_PluginWatcherRuntime.reload_plugins_now	async_method	lines 31-37	related-only	reload plugins now hook registry changed_paths	src/mindroom/orchestrator.py:793, src/mindroom/orchestrator.py:833, src/mindroom/commands/handler.py:91
watch_plugins_task	async_function	lines 40-87	duplicate-found	watch task polling sleep debounce pending changes exception continuing to watch	src/mindroom/file_watcher.py:31, src/mindroom/api/main.py:298, src/mindroom/knowledge/watch.py:184
_filter_pending_plugin_changes	function	lines 90-95	none-found	filter pending changes path under configured roots	src/mindroom/orchestration/plugin_watch.py:148, src/mindroom/knowledge/watch.py:109, src/mindroom/api/sandbox_worker_prep.py:227
collect_plugin_root_changes	function	lines 98-111	related-only	tree snapshot changed paths last snapshot by root	src/mindroom/file_watcher.py:66, src/mindroom/file_watcher.py:82, src/mindroom/knowledge/manager.py:1224
sync_plugin_root_snapshots	function	lines 114-126	related-only	drop removed roots seed baselines configured root set last snapshot	src/mindroom/orchestration/plugin_watch.py:134, src/mindroom/knowledge/watch.py:141, src/mindroom/orchestrator.py:759
capture_plugin_root_snapshots	function	lines 129-131	related-only	capture root snapshots tree_snapshot configured roots	src/mindroom/file_watcher.py:66, src/mindroom/orchestrator.py:781, src/mindroom/orchestrator.py:833
replace_plugin_root_snapshots	function	lines 134-145	related-only	replace watcher baselines root snapshots pop removed roots copy	src/mindroom/orchestration/plugin_watch.py:114, src/mindroom/orchestrator.py:768, src/mindroom/tool_system/registry_state.py:203
_path_is_under_any_root	function	lines 148-150	related-only	any path is_relative_to root path equals root	src/mindroom/api/sandbox_worker_prep.py:227, src/mindroom/tool_system/metadata.py:853, src/mindroom/workspaces.py:55
```

## Findings

### Polling watcher loop repeats existing watcher scaffolding

`watch_plugins_task` in `src/mindroom/orchestration/plugin_watch.py:40` uses a long-running polling loop with `asyncio.sleep(file_watcher._WATCH_SCAN_INTERVAL_SECONDS)`, catches unexpected exceptions, logs that the watcher will continue, and invokes a reload callback after detecting changes.
That behavior overlaps with the generic single-file watcher in `src/mindroom/file_watcher.py:31`, especially the sleep cadence, mtime comparison loop, and continuation after callback errors at `src/mindroom/file_watcher.py:47` and `src/mindroom/file_watcher.py:60`.
It also overlaps with the API config watcher in `src/mindroom/api/main.py:298`, which manually polls a runtime path, resets baseline state on path changes, and logs the same "continuing to watch" failure mode at `src/mindroom/api/main.py:324` and `src/mindroom/api/main.py:342`.

Differences to preserve:

- Plugin watching tracks multiple root snapshots instead of one file mtime.
- It debounces a set of pending paths using `_WATCH_TREE_DEBOUNCE_SECONDS`.
- It clears pending dirty state when `_plugin_watch_state_revision` changes.
- It waits for `orchestrator.running` before taking its initial baseline.
- API config watching uses `stop_event.wait()` rather than an orchestrator running flag.

### Snapshot root pruning is duplicated locally, not elsewhere

`sync_plugin_root_snapshots` in `src/mindroom/orchestration/plugin_watch.py:114` and `replace_plugin_root_snapshots` in `src/mindroom/orchestration/plugin_watch.py:134` both compute `configured_root_set`, iterate `tuple(last_snapshot_by_root)`, and remove roots no longer configured.
This is duplicated behavior inside the primary file.
I did not find another active implementation under `src` that keeps a `dict[Path, dict[Path, int]]` aligned to configured roots.

Differences to preserve:

- `sync_plugin_root_snapshots` only seeds missing roots by reading live tree snapshots.
- `replace_plugin_root_snapshots` overwrites every configured root from a prepared snapshot copy, including empty fallbacks for missing roots.

## Proposed Generalization

A small helper could be considered in `src/mindroom/orchestration/plugin_watch.py` only:

- `_drop_unconfigured_plugin_root_snapshots(configured_roots, last_snapshot_by_root) -> None`

That would remove the local pruning duplication without changing behavior.
No broader refactor is recommended right now.
The watcher loop overlap is real, but plugin watching has enough domain-specific state that pushing it into `file_watcher.watch_file` or a generic polling abstraction would likely add parameters for only two or three call sites.

## Risk/tests

If the local pruning helper is introduced later, focused tests should cover:

- removed roots are dropped from `last_snapshot_by_root`;
- `sync_plugin_root_snapshots` preserves existing configured-root snapshots and seeds only new roots;
- `replace_plugin_root_snapshots` replaces configured roots with copies from the supplied snapshot mapping;
- `watch_plugins_task` still clears pending changes when the plugin watch state revision changes.

No production code was changed for this audit.
