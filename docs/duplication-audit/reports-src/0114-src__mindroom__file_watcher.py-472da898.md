Summary: One meaningful duplication candidate found.
`src/mindroom/api/main.py` repeats the same mtime polling and resilient callback loop as `watch_file`, with the added requirement that the watched config path can be rebound when runtime paths change.
Tree snapshot and changed-path helpers are reused by plugin watching, while skill and knowledge indexing code has related but intentionally different snapshot semantics.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_is_relevant_path	function	lines 21-28	related-only	relevant_path ignored suffixes __pycache__ .tmp .swp is_file include_semantic_relative_path	src/mindroom/knowledge/watch.py:109; src/mindroom/knowledge/manager.py:620; src/mindroom/knowledge/manager.py:632; src/mindroom/tool_system/skills.py:341
watch_file	async_function	lines 31-63	duplicate-found	watch_file st_mtime asyncio.sleep exists stat file watcher callback continuing to watch	src/mindroom/api/main.py:298; src/mindroom/orchestrator.py:1699; src/mindroom/api/config_lifecycle.py:795; src/mindroom/orchestration/plugin_watch.py:40
_tree_snapshot	function	lines 66-79	related-only	tree_snapshot rglob st_mtime_ns stat snapshot skill snapshot knowledge file signature	src/mindroom/orchestration/plugin_watch.py:98; src/mindroom/tool_system/skills.py:331; src/mindroom/tool_system/skills.py:341; src/mindroom/knowledge/manager.py:748; src/mindroom/knowledge/manager.py:1258
_tree_changed_paths	function	lines 82-88	none-found	changed_paths set previous current symmetric difference mtime comparison	src/mindroom/orchestration/plugin_watch.py:98; src/mindroom/knowledge/manager.py:1222; src/mindroom/knowledge/manager.py:1229
```

Findings:

1. `watch_file` duplicates the standalone API config watcher polling loop.
   `src/mindroom/file_watcher.py:31` converts a path, stores the last `st_mtime`, sleeps once per scan interval, detects a changed mtime, invokes an async callback, resets the baseline on `OSError` or `PermissionError`, and logs callback failures while continuing to watch.
   `src/mindroom/api/main.py:298` repeats the same behavior with local `last_mtime`, `config_path.exists()`, `config_path.stat().st_mtime`, periodic waiting, reset-on-filesystem-error, and the same "continuing to watch" exception behavior.
   The important difference is that API `_watch_config` must re-read `_app_runtime_paths(api_app).config_path` and rebaseline when the active runtime path changes, while `watch_file` watches one stable path.

2. `_is_relevant_path` has related filtering behavior, but no direct duplicate found.
   `src/mindroom/file_watcher.py:21` filters tree snapshot entries by filesystem kind, ignored cache directories, ignored editor/cache suffixes, and Emacs lockfile prefix.
   `src/mindroom/knowledge/watch.py:109` and `src/mindroom/knowledge/manager.py:620` also decide whether changed files are relevant, but they preserve knowledge-specific semantic-file rules and root containment checks.
   `src/mindroom/tool_system/skills.py:341` snapshots only `SKILL.md` files, which is much narrower than generic tree relevance.

3. `_tree_snapshot` has related snapshot behavior, but no active duplicate worth extracting.
   `src/mindroom/file_watcher.py:66` creates a generic `Path -> st_mtime_ns` snapshot for every relevant file under a tree.
   `src/mindroom/tool_system/skills.py:331` and `src/mindroom/tool_system/skills.py:341` build a deterministic snapshot of only `SKILL.md` files and include file size.
   `src/mindroom/knowledge/manager.py:748` and `src/mindroom/knowledge/manager.py:1258` capture richer semantic knowledge signatures, including size and content digest.
   These are related "file signature" operations, but they differ enough in scope and return shape that sharing `_tree_snapshot` would either lose required metadata or add parameters for unrelated domains.

4. `_tree_changed_paths` has no duplicate found.
   `src/mindroom/file_watcher.py:82` computes added, removed, and modified paths from two snapshots.
   `src/mindroom/orchestration/plugin_watch.py:98` is a consumer of this helper, not a duplicate.
   `src/mindroom/knowledge/manager.py:1222` computes changed and removed files from git output and tracked-file sets, which is related change detection but not a generic snapshot diff.

Proposed generalization:

Extract the rebinding-specific behavior in `src/mindroom/api/main.py:298` to use `watch_file` only if `watch_file` grows a minimal optional path supplier or a small companion helper such as `watch_resolved_file(get_path, callback, stop_event, poll_interval_seconds=...)` in `src/mindroom/file_watcher.py`.
The helper should preserve API `_watch_config` behavior by resetting the baseline when `get_path()` returns a different path and by stopping promptly when `stop_event` is set.
No refactor is recommended for `_is_relevant_path`, `_tree_snapshot`, or `_tree_changed_paths` because the related call sites are domain-specific or already reuse these helpers.

Risk/tests:

The main risk is changing shutdown latency or rebinding behavior in the API config watcher.
Tests should cover unchanged-file polling, changed-file callback invocation, filesystem-error baseline reset, callback exception logging/continuation, stop-event shutdown, and runtime config path rebinding before and after a change.
Plugin watcher tests should not need changes unless the shared tree snapshot helpers are modified.
