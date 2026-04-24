# ISSUE-199 final plan — knowledge snapshot refresh simplification

Base: `origin/main` at `3a56edc312cf3c6fdc15a5d1e308fa6ebb0a3180`.
Branch: `issue-199-knowledge-snapshot-refresh`.
Planner inputs: Codex plan, Claude plan, Codex critique of Claude, Claude critique of Codex.

## North star

Knowledge is advisory. Chat/runtime responsiveness is the invariant.

Request handling may only look up an already-published last-good knowledge snapshot and schedule best-effort background refresh. It must not sync Git, embed, reindex, repair or replace managers, start watchers, reconcile lifecycle state, initialize all configured bases, or wait on refresh/init locks. If no ready snapshot exists, the request continues without knowledge and emits the existing availability metadata.

Refresh is per resolved base binding, fire-and-forget, and best-effort. A refresh builds a candidate from current files/config, then publishes atomically only after the candidate is complete. Failed or slow refresh leaves the last-good snapshot serving.

Startup does zero knowledge indexing work by default. Unused globally configured knowledge bases are not initialized. Optional warmup must be non-awaited and only for active referenced shared bases; defer warmup unless tests/product need it.

## Final synthesis decisions

The two planners agreed on the direction but disagreed on deletion aggressiveness. The final plan uses Claude's clear vocabulary and deletion pressure, but adopts Codex's repo-aware call-site map and correctness constraints.

Adopt:

- Published snapshot/read-handle vocabulary.
- Registry plus per-binding refresher shape.
- Request-path call-site removal from `response_runner.py`, `teams.py`, and `custom_tools/delegate.py`.
- No eager all-KB startup/API lifespan initialization.
- No orchestrator whole-config `_knowledge_refresh_task`.
- No watchers, Git polling, or deferred startup classifier.
- Existing shadow/candidate swap behavior from `KnowledgeManager.reindex_all()` as the starting point for atomic refresh.
- `execution_identity`/resolved binding key threading for private/request-scoped knowledge.
- Schedule-time binding/config capture so config changes cannot corrupt an in-flight refresh.
- Existing availability notice pattern; do not invent broad new UX states unless tests force it.
- Existing metadata filename/schema compatibility where practical; atomic writes are the goal, not opportunistic format churn.

Constrain:

- Do not key registry entries only by `base_id`. Use a resolved binding/settings key that includes at least base id, storage root/path, index-affecting settings, and requester/execution identity where relevant.
- Do not change Matrix event-cache behavior.
- Do not preserve old `ensure_*manager*` APIs on chat/runtime paths.
- Do not add queues, external workers, cross-process locking, collection sweepers, watcher replacements, or rich frontend progress UI in this PR.
- Admin explicit reindex may remain blocking if that is simpler and clearer; changing it to `202` is not required by ISSUE-199.
- Keep retrieval semantics as close as possible to the existing Agno `Knowledge`/vector DB adapter. Replace lifecycle, not search behavior.

## Target concepts

### Published snapshot

A small immutable read handle for an already-published collection. It contains metadata loaded from the existing per-base state file and enough information to construct or hold the current Agno-compatible knowledge/vector DB read object.

It must not have any write, sync, watcher, or repair behavior.

### Snapshot registry

A process-local cache of published snapshot read handles, keyed by resolved binding/settings key rather than raw base id.

Lookup rules:

1. Check in-memory registry.
2. If absent, cheaply try to open an existing published snapshot from metadata/disk.
3. If ready, return the read handle.
4. If missing, stale, failed, or config-mismatched, return no ready knowledge or last-good knowledge as appropriate and expose availability metadata for the existing notice path.
5. Never build, embed, sync Git, start watchers, or wait on lifecycle locks.

First-touch disk metadata/vector-handle opening is acceptable if cheap, but tests should prove it does not invoke heavy indexing operations. If handle construction proves heavy, prefer returning no knowledge plus scheduled refresh rather than blocking chat.

### Per-binding refresher

A minimal background owner with a task map keyed by resolved binding/settings key.

Required behavior:

- Scheduling one base must not cancel, replace, or wait for another base.
- Duplicate schedules for the same binding should coalesce.
- Refresh task captures config/runtime binding at schedule time.
- Failure logs/status-records but preserves last-good snapshot metadata and in-memory pointer.
- Shutdown cancels owned tasks.
- No global whole-config refresh and no retry loop unless a later issue adds retry policy.

### Refresh runner

One explicit refresh path for a resolved binding.

It may perform heavy work because it is not on the request path:

- Git one-shot sync for Git-backed bases, using existing helpers/semantics where possible.
- File walk/filter/chunk/embed.
- Candidate/shadow collection build.
- Atomic metadata publish via temp file and `Path.replace()`/`os.replace()`.
- Registry publish only after metadata/candidate are complete.
- Best-effort candidate cleanup on failure.

Use the existing `reindex_all()` shadow-swap kernel rather than rewriting Git/vector details from scratch, but extract it away from chat lifecycle machinery.

## File-by-file implementation plan

### `src/mindroom/knowledge/registry.py` or equivalent new read module

Add the snapshot read API:

- `PublishedKnowledgeSnapshot` or similar.
- Resolved binding/settings key type/helper.
- `get_published_snapshot(...)` for chat-safe lookup.
- `publish_snapshot(...)` for successful refresh.
- `reset/clear` helpers for tests and shutdown if needed.

This replaces chat-path dependence on `shared_managers.py`.

### `src/mindroom/knowledge/refresher.py` or replacement for `refresh_owner.py`

Implement one per-binding refresher owner. Keep the public protocol names expected by `KnowledgeAccessSupport` if useful (`schedule_refresh`, `schedule_initial_load`, `is_refreshing`), but semantics are per resolved binding, not whole config.

Delete or retire:

- `OrchestratorKnowledgeRefreshOwner` global scheduling behavior.
- `StandaloneKnowledgeRefreshOwner` calls into `ensure_shared_knowledge_manager()`.
- Any whole-config refresh from a single missing/stale base.

### `src/mindroom/knowledge/manager.py`

Extract or preserve only low-level refresh helpers needed by the refresh runner:

- path resolution and file filtering;
- Git helpers and credential-safe behavior;
- chunking/embedding/indexing helpers;
- shadow/candidate publish behavior.

Delete lifecycle behavior from the request path:

- request/shared init locks;
- watcher runtime;
- Git polling runtime;
- deferred background startup modes;
- startup mode classifier interactions;
- manager repair/replacement semantics.

The final implementation may either delete the `KnowledgeManager` class outright or leave a sharply reduced non-chat refresh helper if that keeps the PR safer. Reviewers should reject any remaining path where chat/runtime can instantiate/initialize a manager or trigger heavy work.

### `src/mindroom/knowledge/shared_managers.py`

Preferred end state: delete this module and update imports to the new registry/refresher modules.

If a temporary compatibility module remains during implementation, it must not expose reachable chat/runtime `ensure_*manager*` behavior. It should be deleted before merge unless reviewers decide a two-PR split is necessary.

### `src/mindroom/knowledge/startup.py`

Delete once imports are removed. Startup indexing modes are not part of the target architecture.

### `src/mindroom/knowledge/utils.py`

Rewrite knowledge resolution around published snapshots:

- Remove `ensure_request_knowledge_managers()`.
- `_get_knowledge_for_base()` should consult only the snapshot registry/read API.
- Preserve `KnowledgeAvailability` and `format_knowledge_availability_notice()` behavior as much as possible.
- Schedule per-binding refresh fire-and-forget on missing/stale/failed/unavailable snapshots.
- Thread execution identity/resolved private binding data into lookup and refresh scheduling.
- Keep `MultiKnowledgeVectorDb` or an equivalent adapter if Agno still needs a single merged vector DB object.

### `src/mindroom/response_runner.py`

Delete the request-time await of knowledge manager creation:

- Remove `_ensure_request_knowledge_managers()` or make it disappear from prepared state.
- Remove `request_knowledge_managers` from runtime state.
- Pass execution identity into `KnowledgeAccessSupport.for_agent()` / `get_agent_knowledge()`.
- Preserve `_append_knowledge_availability_enrichment()` behavior.

### `src/mindroom/custom_tools/delegate.py`

Delete the await of `ensure_request_knowledge_managers()`.

Delegate knowledge resolution must use published snapshot lookup with the delegate execution identity, schedule refresh if unavailable, and continue with availability metadata.

### `src/mindroom/teams.py`

Delete team request-time knowledge manager initialization.

Team/member knowledge resolution must use snapshot lookup and preserve member availability notices.

### `src/mindroom/api/openai_compat.py`

Keep the already nonblocking single-agent shape. Ensure team paths do not indirectly create request-scoped managers. Pass execution identity through knowledge lookup where applicable. Preserve degraded/initializing system hints.

### `src/mindroom/bot.py` and `src/mindroom/ai.py`

Update knowledge injection to accept published snapshot read handles or the preserved Agno-compatible merged knowledge object.

The cached `agent` property has no request identity and should remain shared-snapshot-only; it must not initialize private/request-scoped bases.

### `src/mindroom/orchestrator.py`

Remove global knowledge lifecycle orchestration:

- Delete `_knowledge_refresh_task` and whole-config schedule/cancel logic.
- Do not call all-KB initialization during startup/configure.
- Do not block readiness on knowledge.
- On config reload, schedule per-binding refresh only for active referenced bases if needed, and do not await it.
- On stop, shut down the per-binding refresher.
- Leave Matrix event-cache sync/service code untouched.

### `src/mindroom/api/main.py`

Stop scheduling or initializing every configured knowledge base during API lifespan startup. If any warmup remains, it must be non-awaited and only for active referenced shared bases.

### `src/mindroom/api/knowledge.py`

Status/list endpoints must read disk/snapshot/refresh metadata without initializing managers.

Upload/delete may mutate files and schedule refresh rather than directly sharing chat lifecycle code.

Explicit reindex should call the same refresh runner. It may remain blocking if that preserves current admin semantics; do not change response codes unless implementation makes it clearly necessary and tests are updated deliberately.

Avoid carrying both `manager_available` and `snapshot_available` long-term. Pick one API surface and keep semantics clear. If frontend compatibility forces `manager_available`, redefine it as snapshot availability rather than duplicating fields.

### `src/mindroom/knowledge/__init__.py`

Export the new snapshot/refresher public API and remove old ensure/init manager exports once call sites are migrated.

### `pyproject.toml`

Remove `watchfiles>=1` if knowledge watchers are fully deleted and nothing else imports it.

## Required deletions / simplifications

- Request-scoped knowledge manager creation from Matrix, team, delegate, and OpenAI-compatible paths.
- Shared/request init lock graph from chat/runtime.
- Eager all-configured-KB startup/API lifespan initialization.
- Orchestrator whole-config refresh task and cancellation/replacement semantics.
- Startup mode module/classifier.
- Knowledge file watchers and Git polling runtime.
- Manager replacement/reconcile paths.
- Tests whose only purpose is preserving the old lifecycle.

## Required tests

Map tests to the seven requested behaviors. Prefer updating existing tests and deleting old lifecycle-preservation tests over adding production compatibility complexity.

1. Chat/request path does not await shared or request-scoped KB initialization.
   - Matrix response path test: no call to `ensure_*manager*`, Git sync, embed, reindex, or refresh await.
   - Keep/update OpenAI-compatible no-await test.

2. Missing shared KB schedules refresh but returns no knowledge.
   - Existing availability hint tests should still pass.
   - Add Matrix/team/delegate equivalents if current coverage is absent.

3. Existing published snapshot is used while refresh is running.
   - Registry/read-handle test where a refresh task is blocked before publish and lookup still returns old snapshot.

4. Failed refresh preserves last-good snapshot.
   - Adapt existing `reindex_all` failure preservation tests to the refresh runner/registry.
   - Assert metadata bytes or loaded snapshot pointer remain old after failure.

5. Per-base refresh independence.
   - Scheduling A and B creates independent tasks.
   - Duplicate A schedules coalesce.
   - A failure does not cancel/replace B.

6. Startup does not initialize unused configured KBs.
   - Orchestrator startup with configured but unreferenced bases calls no knowledge init/index/Git/embed.
   - API lifespan does not schedule every configured KB.

7. On-access refresh is per-base, not global.
   - A miss/stale result for base A schedules only A's binding, not whole config and not unrelated base B.

Additional regression constraints:

- OpenAI-compatible knowledge availability notices still prepend correctly.
- Admin status/list endpoints are non-initializing.
- Matrix event-cache tests remain unchanged.
- If watchers are deleted, no remaining imports of `watchfiles`.

Avoid brittle wall-clock tests except where there is no better interaction assertion. Use patched functions/counters to prove no heavy calls happen.

## Validation commands

From the implementation worktree, preferably inside `nix-shell shell.nix` if `libstdc++.so.6` problems appear:

```bash
uv run ruff check src/mindroom/knowledge src/mindroom/api/knowledge.py src/mindroom/api/openai_compat.py src/mindroom/orchestrator.py src/mindroom/response_runner.py src/mindroom/teams.py src/mindroom/custom_tools/delegate.py tests/test_knowledge_manager.py tests/api/test_knowledge_api.py tests/test_multi_agent_bot.py tests/test_openai_compat.py tests/test_delegate_tools.py
```

```bash
uv run pytest tests/test_knowledge_manager.py tests/api/test_knowledge_api.py tests/test_multi_agent_bot.py tests/test_openai_compat.py tests/test_delegate_tools.py tests/test_streaming_behavior.py -n 0 --no-cov -v
```

Before review/merge:

```bash
uv run pytest -n 0 --no-cov
uv run pre-commit run --all-files
```

If full pre-commit or full pytest has unrelated existing failures, record them clearly in the issue report and preserve focused evidence for changed behavior.

## Explicitly deferred

- Separate indexing process/worker.
- Durable refresh queue, leases, progress reporting, or cross-process locking.
- Orphan Chroma collection sweeper/retention policy.
- Filesystem watcher replacement or Git polling daemon.
- Rich frontend refresh progress UI.
- Snapshot pinning/rollback/version selection.
- Multi-replica coordination.
- Opportunistic state-file renames or broad schema migrations.
- Unrelated Matrix event-cache changes.

## PR description design note draft

This PR simplifies knowledge access so chat reads only already-published last-good snapshots. Request handling no longer creates knowledge managers, syncs Git, embeds, reindexes, starts watchers, initializes globally configured bases, or waits on knowledge lifecycle locks. Missing or stale bases schedule a per-binding best-effort background refresh and continue with explicit availability metadata. Refresh builds a candidate collection and publishes atomically; failed or slow refreshes leave the previous snapshot serving. Startup no longer initializes every configured KB, and the old shared-manager/watch/Git-polling/whole-config-refresh coordination is removed in favor of lazy snapshot lookup plus explicit refresh/admin paths.
