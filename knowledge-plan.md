# Knowledge Refactor Plan

## Objective

Simplify the knowledge lifecycle by separating shared manager reuse from request-scoped private manager construction.
Keep `src/mindroom/runtime_resolution.py` as the only place that resolves `storage_root`, `knowledge_path`, watcher policy, and whether a binding is request-scoped.
Remove the current design where `src/mindroom/knowledge/manager.py` tries to handle shared reuse, private reuse, eviction, and config replacement through one global cache.

## Non-Goals

Do not change `resolve_knowledge_binding(...)` in `src/mindroom/runtime_resolution.py` unless a bug is found during the refactor.
Do not change the indexing behavior inside `KnowledgeManager.initialize()`, `sync_indexed_files()`, or `sync_git_repository()`.
Do not change the external API shape of `get_agent_knowledge(...)`.
Do not preserve internal backward compatibility.
There are no library users that justify keeping old internal knowledge-manager entry points alive during this refactor.
Do not add backward-compatibility branches whose only purpose is to preserve the old private-manager cache behavior.

## End State

Shared managers are the only process-global knowledge objects.
Private or request-scoped managers are created fresh for the current request and live only in the caller-owned `request_knowledge_managers` map.
`initialize_knowledge_managers(...)` initializes only configured shared bases from `config.knowledge_bases`.
Shared lookup APIs return only shared managers that already exist in the shared registry.
Request-scoped code paths never depend on hidden global fallback or global cleanup.
Shared-manager reuse still refreshes runtime-bound state such as `runtime_paths` and the embedder before returning the reused manager.
Shared-manager reuse still performs incremental sync when the caller disables watchers and the binding requests on-access refresh.

## API Shape To Land

Rename `initialize_knowledge_managers(...)` to `initialize_shared_knowledge_managers(...)`.
Delete `initialize_knowledge_managers(...)` immediately after the callers move.
Make the shared initialization entry point skip any binding where `binding.request_scoped` is true.
Add `get_shared_knowledge_manager(base_id: str) -> KnowledgeManager | None` in `src/mindroom/knowledge/manager.py`.
Add `get_or_create_shared_knowledge_manager(...)` in `src/mindroom/knowledge/manager.py`.
Add `create_request_knowledge_manager(...)` in `src/mindroom/knowledge/manager.py`.
Keep `ensure_agent_knowledge_managers(...)` as the per-agent request materialization helper, but change its implementation so it never registers private managers globally.
If a shared manager is reused rather than replaced, keep a narrow refresh step that updates runtime-bound fields and the embedder without reviving the old mixed-cache policy.

## Phase 1

Refactor `src/mindroom/knowledge/manager.py` first.
Replace `_knowledge_managers` and `_static_knowledge_manager_keys` with one shared registry keyed by `base_id`.
If concurrency control is still needed, keep one simple lock map keyed by `base_id` for shared initialization only.
Delete `_scoped_private_manager_lru`.
Delete `_knowledge_manager_replacement_locks`.
Delete `_stale_request_manager_keys(...)`.
Delete any request-scoped branch in shared-registry code.
Keep `KnowledgeManager` focused on one resolved binding and its own watcher and sync behavior.
Add a small helper that computes a stable shared-manager signature from the resolved binding plus any indexing settings that require replacement.
When the shared signature changes, stop the old watcher and replace the shared manager object.
When the shared signature does not change, reuse the existing manager and run a narrow runtime refresh that updates `config`, `runtime_paths`, `storage_path`, `knowledge_path`, and the embedder.
Keep the current incremental-sync-on-reuse behavior for shared managers when `start_watchers=False` leads to `incremental_sync_on_access=True`.
Do not keep the current broad `_refresh_settings(...)` shape if it remains coupled to the mixed-cache policy.
Make `shutdown_knowledge_managers()` tear down only the shared registry and its shared-init locks.

## Phase 2

Rewrite `ensure_agent_knowledge_managers(...)` in `src/mindroom/knowledge/manager.py`.
For each resolved base ID, call `resolve_knowledge_binding(...)` once.
If the binding is shared, reuse `get_or_create_shared_knowledge_manager(...)`.
If the binding is request-scoped, call `create_request_knowledge_manager(...)` and put the result in the returned map.
Never store a request-scoped manager in a process-global dictionary.
Never return a private manager from `get_shared_knowledge_manager(...)`.
Keep the returned `dict[str, KnowledgeManager]` shape so existing callers can keep passing `request_knowledge_managers`.

## Phase 3

Refactor `src/mindroom/knowledge/utils.py` after the manager split lands.
Keep `ensure_request_knowledge_managers(...)` as the multi-agent request helper.
Make `ensure_request_knowledge_managers(...)` just merge the results of `ensure_agent_knowledge_managers(...)` for each agent.
Update `get_knowledge_for_base(...)` to check `request_knowledge_managers` first and the shared registry second.
Remove `get_knowledge_manager(base_id, config=..., runtime_paths=..., execution_identity=...)`.
Keep the current fail-closed behavior where a private base returns `None` if the caller passed a request map that does not contain that base.
Make the default shared lookup path use `get_shared_knowledge_manager(...)`.

## Phase 4

Migrate the runtime callers once the shared-only and request-scoped manager APIs exist.
Keep `AgentBot._ensure_request_knowledge_managers(...)` in `src/mindroom/bot.py` as the main request entry point for one agent run.
Keep `_ensure_request_team_knowledge_managers(...)` in `src/mindroom/teams.py` as the main request entry point for team runs.
Change `src/mindroom/custom_tools/delegate.py` to use `ensure_request_knowledge_managers([agent_name], ...)` so delegation goes through the same request materialization path as bot and team execution.
Keep `src/mindroom/api/openai_compat.py` shared-only and have it initialize knowledge through the shared initialization entry point and read it through shared lookup only.
Keep `src/mindroom/api/knowledge.py` shared-only and remove any call pattern that tries to resolve private managers dynamically from `(config, runtime_paths, execution_identity)`.
Do not add a private-manager path to `/v1` or `/api/knowledge`.

## Phase 5

Delete the old mixed-cache entry points after the callers are migrated.
Remove `get_knowledge_manager(...)` entirely once all call sites use shared-only or request-scoped APIs explicitly.
Do not keep compatibility aliases for removed knowledge APIs.
Remove dead helpers that only existed to support mixed shared and request-scoped caching.
Remove `KnowledgeManager.matches(...)` and `KnowledgeManager.needs_full_reindex(...)` if the new shared-signature path makes them redundant.
Remove any tests that exist only to validate private-manager global caching, stale-key replacement, or LRU eviction.

## File-Level Changes

`src/mindroom/runtime_resolution.py` should remain the source of truth for knowledge binding resolution.
`src/mindroom/knowledge/manager.py` should own the shared registry, shared manager creation, request manager creation, and `KnowledgeManager`.
`src/mindroom/knowledge/utils.py` should be the only adapter that merges request-local managers with the shared registry for callers.
`src/mindroom/bot.py` should keep explicit request manager creation before private agent execution.
`src/mindroom/teams.py` should keep explicit request manager creation before team execution.
`src/mindroom/custom_tools/delegate.py` should stop calling the low-level per-agent helper directly once the request helper is available.
`src/mindroom/api/openai_compat.py` should stay on shared-only knowledge.
`src/mindroom/api/knowledge.py` should stay on shared-only knowledge.

## Tests To Keep And Update

Keep and update `test_initialize_knowledge_managers_maintains_registry` in `tests/test_knowledge_manager.py`.
Keep and update `test_initialize_knowledge_managers_full_reindex_on_settings_change` in `tests/test_knowledge_manager.py`.
Keep and update `test_initialize_knowledge_managers_non_index_setting_change_uses_incremental_sync` in `tests/test_knowledge_manager.py`.
Keep and update `test_initialize_knowledge_managers_refreshes_runtime_paths_on_reuse` in `tests/test_knowledge_manager.py`.
Keep and update `test_initialize_knowledge_managers_refreshes_shared_managers_on_reuse_without_watchers` in `tests/test_knowledge_manager.py`.
Keep and update `test_private_knowledge_managers_copy_template_and_isolate_private_instance_roots` in `tests/test_knowledge_manager.py`.
Keep and update `test_get_knowledge_for_base_reuses_shared_manager_created_by_agent_ensure` in `tests/test_knowledge_manager.py`.
Keep and update `test_get_knowledge_for_base_does_not_fall_back_to_stale_shared_manager` in `tests/test_knowledge_manager.py`.
Keep and update `test_ensure_agent_knowledge_managers_removes_stale_shared_manager_keys` in `tests/test_knowledge_manager.py`.
Keep and update `test_ensure_agent_knowledge_managers_replaces_stale_shared_key_under_concurrency` in `tests/test_knowledge_manager.py`.
Keep and update `test_request_bound_private_manager_survives_cache_eviction` in `tests/test_knowledge_manager.py`.
Keep and update `test_degraded_request_scoped_knowledge_does_not_fall_back_to_cached_private_manager` in `tests/test_knowledge_manager.py`.
Keep and update `test_degraded_request_scoped_knowledge_preserves_shared_manager_fallback` in `tests/test_knowledge_manager.py`.
Keep and update the shared-only API tests in `tests/api/test_knowledge_api.py`.
Keep and update the shared-only initialization tests in `tests/test_openai_compat.py`.
Keep and update the request-map wiring tests in `tests/test_delegate_tools.py`, `tests/test_multi_agent_bot.py`, and `tests/test_team_media_fallback.py`.

## Tests To Delete Or Rewrite

Delete or rewrite `test_initialize_knowledge_managers_keeps_private_scoped_managers` in `tests/test_knowledge_manager.py`.
Delete or rewrite `test_ensure_agent_knowledge_managers_removes_stale_private_key_for_same_requester` in `tests/test_knowledge_manager.py`.
Delete or rewrite `test_ensure_agent_knowledge_managers_initializes_private_scope_once_under_concurrency` in `tests/test_knowledge_manager.py`.
Delete or rewrite `test_initialize_knowledge_managers_removes_private_scoped_managers_when_private_knowledge_is_removed` in `tests/test_knowledge_manager.py`.
Delete `test_private_scoped_knowledge_manager_cache_is_bounded` in `tests/test_knowledge_manager.py`.
Delete any test that asserts `get_knowledge_manager(..., config=..., runtime_paths=..., execution_identity=...)` can return a private manager.

## Tests To Add

Add a test that two calls to `ensure_agent_knowledge_managers(...)` for the same private binding return different manager objects.
Add a test that two calls to `ensure_agent_knowledge_managers(...)` for the same shared binding return the same shared manager object.
Add a test that `get_shared_knowledge_manager(...)` never returns a private manager, even after a prior request created one.
Add a test that request-scoped git knowledge still performs on-access sync without starting a persistent watcher.
Add a test that changing a shared base path or indexing setting replaces the shared manager object instead of mutating the old one in place.

## Suggested Commit Order

Commit 1 should introduce the shared-only registry and request-manager constructor in `src/mindroom/knowledge/manager.py`.
Commit 2 should migrate `src/mindroom/knowledge/utils.py`, `src/mindroom/bot.py`, `src/mindroom/teams.py`, and `src/mindroom/custom_tools/delegate.py`.
Commit 3 should migrate `src/mindroom/api/openai_compat.py` and `src/mindroom/api/knowledge.py`.
Commit 4 should delete the mixed-cache compatibility code and obsolete tests.

## Validation

Run `pytest tests/test_knowledge_manager.py tests/api/test_knowledge_api.py tests/test_openai_compat.py tests/test_delegate_tools.py tests/test_multi_agent_bot.py tests/test_team_media_fallback.py -q`.
Run `just test-backend` after the targeted suites pass.
Run `pre-commit run --all-files` before merging.

## Acceptance Criteria

There is one process-global knowledge registry and it contains shared managers only.
There is no process-global LRU or stale-key replacement logic for private knowledge.
There is no dynamic shared lookup path that can reconstruct a private manager from `(config, runtime_paths, execution_identity)`.
Private knowledge is resolved explicitly during request setup and survives only as long as the caller-owned request map.
Shared knowledge still reuses manager instances across repeated calls and config reloads.
The resulting `src/mindroom/knowledge/manager.py` is materially smaller and easier to follow than the current mixed-cache version.
