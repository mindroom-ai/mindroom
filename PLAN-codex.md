# ISSUE-191 Plan

## A. Diagnosis

Estimates below are inferred from the current code paths on `origin/main`, not from fresh benchmarks.

### Request-path entry points

- `(b) Matrix single-agent turn:` `src/mindroom/response_runner.py:1612-1651` `ResponseRunner._prepare_response_runtime_common()` awaits `_ensure_request_knowledge_managers()` inside both `prepare_non_streaming_runtime()` and `prepare_streaming_runtime()`.
- `(b) Matrix single-agent turn:` `src/mindroom/response_runner.py:1011-1029` `_ensure_request_knowledge_managers()` calls `mindroom.knowledge.utils.ensure_request_knowledge_managers()`.
- `(b) Matrix single-agent turn:` `src/mindroom/knowledge/utils.py:57-75` `ensure_request_knowledge_managers()` loops agent names and calls `mindroom.knowledge.shared_managers.ensure_agent_knowledge_managers()`.
- `(b) Matrix single-agent turn:` `src/mindroom/knowledge/shared_managers.py:353-395` `ensure_agent_knowledge_managers()` is misnamed for the hot path because it handles both request-scoped private KBs and shared KBs.
- `(b) Matrix single-agent turn:` `src/mindroom/knowledge/shared_managers.py:379-385` request-scoped bases go through `_create_request_knowledge_manager_for_target()`, which creates a fresh `KnowledgeManager` and initializes it immediately.
- `(b) Matrix single-agent turn:` `src/mindroom/knowledge/shared_managers.py:388-394` shared bases go through `_ensure_shared_knowledge_manager_for_target()`, which may create, reinitialize, resume, or sync the shared manager before the turn continues.
- `(b) Matrix team turn:` `src/mindroom/teams.py:1246-1267` `_ensure_request_team_knowledge_managers()` calls the same `ensure_request_knowledge_managers()` helper.
- `(b) Matrix team turn:` `src/mindroom/teams.py:1499-1503` `team_response()` awaits `_ensure_request_team_knowledge_managers()` before materializing team members.
- `(b) Matrix team turn:` `src/mindroom/teams.py:1852-1856` `team_response_stream()` does the same on the streaming team path.
- `(b) Delegation inside a live turn:` `src/mindroom/custom_tools/delegate.py:95-107` `DelegateTools.delegate_task()` awaits `ensure_request_knowledge_managers()` before calling the delegated agent.
- `(b) OpenAI-compatible live request:` `src/mindroom/api/openai_compat.py:641-649` `_ensure_knowledge_initialized()` awaits `initialize_shared_knowledge_managers(start_watchers=False, reindex_on_create=False)`.
- `(b) OpenAI-compatible live request:` `src/mindroom/api/openai_compat.py:783-789` every `/v1/chat/completions` request calls `_ensure_knowledge_initialized()` before knowledge lookup and before any response generation begins.

### Shared-manager lifecycle calls reached from those entry points

- `(b) Lifecycle create path:` `src/mindroom/knowledge/shared_managers.py:213-263` `_create_knowledge_manager_for_target()` constructs `KnowledgeManager` and immediately calls `initialize_manager_for_startup()` unless `initialize_on_create=False`.
- `(b) Lifecycle startup dispatcher:` `src/mindroom/knowledge/startup.py:46-82` `initialize_manager_for_startup()` chooses `full_reindex`, `resume`, or `incremental`, and only defers work when Git startup mode is `background`.
- `(b) Lifecycle full init:` `src/mindroom/knowledge/manager.py:972-980` `KnowledgeManager.initialize()` runs `sync_git_repository(index_changes=False)` for Git-backed KBs and then `reindex_all()`.
- `(b) Lifecycle incremental sync:` `src/mindroom/knowledge/startup.py:25-31` `sync_manager_without_full_reindex()` calls `manager.sync_git_repository()` for Git-backed KBs or `manager.sync_indexed_files()` for non-Git KBs.
- `(b) Lifecycle resume:` `src/mindroom/knowledge/startup.py:34-43` `resume_manager_without_full_reindex()` calls `sync_git_repository(index_changes=False)` and then `sync_indexed_files()`.
- `(b) Lifecycle shared-manager reuse without runtime owner:` `src/mindroom/knowledge/shared_managers.py:312-319` `_ensure_shared_knowledge_manager_for_target()` will call `finish_pending_background_git_startup()` or `sync_manager_without_full_reindex()` when an existing shared manager has no active watcher or git-sync task.
- `(b) Lifecycle deferred-startup completion:` `src/mindroom/knowledge/manager.py:1083-1120` `finish_pending_background_git_startup()` runs `sync_git_repository()` and then either `reindex_all()` or `sync_indexed_files()`.
- `(b) Lifecycle git fetch and per-file index mutations:` `src/mindroom/knowledge/manager.py:1139-1184` `sync_git_repository()` fetches the repo and then calls `remove_file()` and `index_file()` for changed paths.
- `(b) Lifecycle filesystem scan and per-file index mutations:` `src/mindroom/knowledge/manager.py:996-1050` `sync_indexed_files()` loads existing vectors, walks the current file tree, stats every candidate file, and mutates the live collection for removed or changed paths.
- `(b) Lifecycle full rebuild:` `src/mindroom/knowledge/manager.py:922-980` `reindex_all()` deletes and recreates the live collection and then reinserts every candidate file.
- `(b) Lifecycle git checkout work:` `src/mindroom/knowledge/manager.py:606-811` `_run_git()`, `_ensure_git_repository()`, `_sync_git_repository_once()`, and LFS helpers can clone, fetch, checkout, reset, and pull LFS blobs while the caller awaits them.

### Pure-read and auth/visibility paths that are already fine

- `(a) Pure read:` `src/mindroom/knowledge/utils.py:78-122` `_get_knowledge_for_base()` and `get_agent_knowledge()` only read existing request managers or existing shared managers and return `Knowledge`.
- `(a) Pure read:` `src/mindroom/knowledge/utils.py:125-158` `KnowledgeAccessSupport.for_agent()` is a pure lookup wrapper around `get_agent_knowledge()`.
- `(a) Pure read:` `src/mindroom/teams.py:1164-1179` `materialize_exact_team_members()` resolves each member’s `Knowledge` after request managers already exist, and does not itself initialize or refresh KB state.
- `(a) Pure read:` `src/mindroom/api/openai_compat.py:820-856` agent knowledge resolution after `_ensure_knowledge_initialized()` is just `get_agent_knowledge()`.
- `(c) Auth and visibility only:` `src/mindroom/config/main.py:1209-1216` `Config.get_agent_knowledge_base_ids()` only returns the base IDs visible to one agent.
- `(c) Auth and visibility only:` `src/mindroom/runtime_resolution.py:267-327` `resolve_knowledge_binding()` resolves storage roots, workspace roots, and whether a base is request-scoped, but it does not itself sync or index.
- `(c) Auth and visibility only:` `src/mindroom/knowledge/shared_managers.py:463-470` `get_shared_knowledge_manager_for_config()` is only a config-aware cache lookup.

### Hooks and pre-placeholder timing

- No KB lifecycle happens in core hook emission for `message:enrich`, `message:before_response`, or `session:started`, because `src/mindroom/turn_policy.py:162-169`, `src/mindroom/delivery_gateway.py:59-84`, and `src/mindroom/response_runner.py:778-839` only emit hook contexts and never call knowledge code directly.
- The `agent:started` hook is startup-only, not per-turn, because `src/mindroom/bot.py:1095` emits `EVENT_AGENT_STARTED` during bot startup and not during chat execution.
- There are no knowledge-manager references in `src/mindroom/execution_preparation.py` or `src/mindroom/streaming.py` on `origin/main`, so those files are not the seam where KB freshness work enters the request path.
- `(a) Matrix placeholder timing today is already safe:` `src/mindroom/response_runner.py:2333-2349` decides whether to send `Thinking...`, and `src/mindroom/response_runner.py:1531-1545` sends the placeholder before spawning the real response task.
- `(b) Matrix KB work happens after placeholder send:` `src/mindroom/response_runner.py:2006-2010` marks `response_runtime_start`, then awaits `prepare_streaming_runtime()`, which is where `_ensure_request_knowledge_managers()` runs.
- `(b) Team KB work also happens after the team placeholder:` `src/mindroom/response_runner.py:1421-1428` prepares `🤝 Team Response: Thinking...`, and the real team execution then goes through `team_response()` or `team_response_stream()`, which await `_ensure_request_team_knowledge_managers()`.
- `(b) OpenAI-compatible requests are worse than Matrix turns here:` there is no placeholder at all, so `/v1/chat/completions` currently waits for `initialize_shared_knowledge_managers()` before streaming can even emit its first SSE chunk.

### Why the current code still blocks

- Cold shared KBs can block because the first live request can race the background orchestrator warmup and end up creating the shared manager itself through the same global registry and init lock.
- A request can also block behind the runtime owner because `_shared_knowledge_manager_init_lock()` serializes shared-manager creation by base ID, so a request that arrives while the background initializer is doing sync or reindex will await that lock.
- OpenAI-compatible requests intentionally disable background runtime ownership by passing `start_watchers=False`, and `tests/test_knowledge_manager.py:1838-1869` proves that the current reuse path performs an on-access refresh in that mode.
- Background Git startup helps only for shared Git-backed KBs with `startup_behavior="background"`, and `tests/test_knowledge_manager.py:2655-2698` shows that this only avoids synchronous work when the manager is created in that exact mode.

### Likely cost of the current `(b)` calls

- Warm local-folder syncs are at least one full vector metadata load plus one full semantic file-tree walk, so they are usually tens to hundreds of milliseconds for small KBs and can grow into multi-second scans on larger repos.
- Warm Git-backed syncs add at least `git fetch`, `git rev-parse`, tracked-file enumeration, and changed-file reindex work, so they are typically hundreds of milliseconds to seconds even when nothing changed.
- Cold Git-backed init can include `git clone`, `git checkout`, `git fetch`, optional `git lfs pull`, and then a full `reindex_all()`, so the real cost is seconds to minutes depending on repo size, embedding latency, and disk speed.
- The worst-case upper bound is currently dangerous because `KnowledgeGitConfig.sync_timeout_seconds` defaults to `3600` in `src/mindroom/config/knowledge.py:32-35`, so a stuck git command can hold a live request open for up to an hour per command.

## B. Proposed design

### Snapshot contract

- A shared KB snapshot is one immutable, fully-built semantic index for exactly one base ID and one config/indexing signature.
- Each snapshot lives in its own on-disk snapshot directory under the KB storage root and carries metadata including `snapshot_id`, `built_at`, `settings_signature`, `source_revision`, `indexed_count`, and `builder_version`.
- The active snapshot is published by an atomic manifest write such as `active_snapshot.json.tmp` followed by `os.replace()`, so readers see either the previous snapshot or the new one and never a partial build.
- Readers only ever read from the currently published snapshot, and builders never mutate that published snapshot in place.
- The last good snapshot remains readable while a newer snapshot is being built.

### Background refresh owner

- Use one process-local refresh owner per shared KB in `src/mindroom/knowledge/shared_managers.py`, because the current runtime already treats shared KB state as process-local global state.
- The owner keeps a tiny state record with the published snapshot pointer, `refresh_task`, `refresh_requested`, `refresh_in_progress`, `last_error`, and `last_snapshot_age`.
- The owner deduplicates refresh work so at most one refresh task runs per shared KB at a time, and additional triggers only mark the base dirty for one follow-up pass.
- No cross-process lease or distributed coordinator is needed in the first implementation, because that is not part of today’s runtime model and would be pure over-engineering for this issue.

### How refresh work runs

- Orchestrator startup and config reload should schedule shared KB refreshes in the background and never rely on a future chat turn to initialize them.
- Shared file watchers and Git pollers should only enqueue a refresh and never mutate the active semantic snapshot directly.
- Manual upload and delete endpoints should update the working tree and then enqueue refresh, while explicit reindex remains the only synchronous admin rebuild path.
- A refresh builds a new snapshot off to the side and publishes it only after the build completes successfully.

### Live-turn read API

- Live turns should stop calling any helper that can create or refresh a shared KB.
- Introduce one pure read resolver such as `resolve_agent_knowledge_access()` that returns `KnowledgeAccessResult(knowledge, unavailable_bases)` for one agent.
- For shared KBs that resolver only consults the already-published shared snapshot state via `get_shared_knowledge_manager_for_config()` or its replacement and returns the current `Knowledge` handle.
- For request-scoped private KBs the resolver may keep using today’s create-on-access behavior, because ISSUE-191 is specifically about shared KB lifecycle work.
- Agno already expects a `Knowledge` object, so the live-turn API should stay `Knowledge`-based rather than forcing a new `search(snapshot=...)` interface through the agent stack.

### First-init degraded state

- If a shared KB has no published snapshot yet, the read resolver must return a typed unavailable result immediately without awaiting any lifecycle work.
- That unavailable result should include a machine-usable reason such as `initializing`, `refresh_failed`, or `config_mismatch`, plus the base IDs involved and any last-error summary.
- Matrix turns and `/v1/chat/completions` should surface that state to the LLM as a system enrichment item that explicitly says the KB is unavailable and must not be claimed as searched.
- The degraded path must complete in under 100 ms on the request path because it is only reading in-memory state and, at most, an already-written snapshot manifest.

### When requests may trigger refresh

- The ideal path is that live requests never trigger refresh at all.
- If a shared base is missing or stale on a request path, the request may call `schedule_shared_knowledge_refresh(base_id)` as fire-and-forget bookkeeping, but it must never await the refresh.
- A fresh shared snapshot should result in zero awaited calls to `initialize_manager_for_startup()`, `sync_git_repository()`, `sync_indexed_files()`, `reindex_all()`, or `finish_pending_background_git_startup()` on the request path.

### Binding semantics

- `agent.knowledge_bases` remains pure visibility and authorization metadata.
- `Config.get_agent_knowledge_base_ids()` continues to define what a given agent is allowed to see, and `resolve_knowledge_binding()` continues to define where that base lives and whether it is request-scoped.
- The code path that enforces “binding is visibility only” is the new read resolver, which must not call shared-manager ensure/init/refresh functions for shared KBs.

## C. Implementation plan

### File-by-file change list

- `src/mindroom/knowledge/shared_managers.py` (`~220-320 LOC`): add per-base shared refresh owner state, fire-and-forget `schedule_shared_knowledge_refresh()`, a pure read lookup for published shared snapshots, and removal of on-access shared refresh behavior from request helpers.
- `src/mindroom/knowledge/manager.py` (`~260-380 LOC`): add staged snapshot build and publish helpers, persisted active-snapshot metadata, last-good-snapshot status reporting, and snapshot cleanup policy while keeping current file parsing and embedding logic reusable.
- `src/mindroom/knowledge/startup.py` (`~40-80 LOC`): split “build snapshot now” from “schedule refresh now” semantics so startup helpers no longer assume the caller is allowed to block the request path.
- `src/mindroom/knowledge/utils.py` (`~120-180 LOC`): replace `ensure_request_knowledge_managers()` usage for shared bases with one `KnowledgeAccessResult` resolver that separates shared pure-read access from request-scoped private-manager creation.
- `src/mindroom/response_runner.py` (`~50-90 LOC`): switch single-agent request prep to the new resolver, plumb degraded KB hints into `system_enrichment_items`, and keep the existing placeholder ordering untouched.
- `src/mindroom/teams.py` (`~70-120 LOC`): switch team member materialization and team request prep to the new resolver so team turns do not initialize or refresh shared KBs.
- `src/mindroom/custom_tools/delegate.py` (`~20-40 LOC`): stop delegation from initializing shared KBs on the parent agent’s request path and use the same degraded hint path.
- `src/mindroom/api/openai_compat.py` (`~80-130 LOC`): stop awaiting `initialize_shared_knowledge_managers()` per request, use pure snapshot lookup, pass degraded hints into `ai_response()` and `stream_agent_response()`, and optionally fire-and-forget schedule shared refresh if needed.
- `src/mindroom/orchestrator.py` (`~30-60 LOC`): make the runtime owner proactively schedule shared KB refreshes and reloads so Matrix turns do not become the initializer of record.
- `src/mindroom/api/knowledge.py` (`~40-80 LOC`): expose snapshot status fields such as `active_snapshot_id`, `snapshot_built_at`, `snapshot_age_seconds`, and `refresh_in_progress`, and change upload/delete to enqueue refresh instead of mutating the live snapshot.
- `src/mindroom/config/knowledge.py` (`~20-50 LOC`) and `src/mindroom/knowledge/manager.py` (`~40-80 LOC` more): add the semantic-index file filter default and any minimal user override surface.
- `tests/test_knowledge_manager.py` (`~180-260 LOC`): snapshot publication, stale-read, first-init degraded, and shared refresh scheduling tests.
- `tests/test_multi_agent_bot.py` (`~80-140 LOC`): full Matrix turn tests proving placeholder timing and zero request-path lifecycle on fresh snapshots.
- `tests/test_openai_compat.py` (`~60-120 LOC`): `/v1/chat/completions` no longer awaits shared-manager init and now emits degraded hints when no snapshot exists.
- `tests/api/test_knowledge_api.py` (`~40-80 LOC`): snapshot status payload and manual admin endpoint behavior after the snapshot refactor.

### Ordered commits

1. Knowledge access split.
- Add `KnowledgeAccessResult`, keep private request-scoped KB creation as-is, and switch pure shared reads to explicit lookup-only helpers without changing background refresh yet.
2. Shared snapshot runtime.
- Add the shared per-base refresh owner, staged snapshot build/publish, persisted active-snapshot metadata, and orchestrator scheduling hooks.
3. Switch live request paths.
- Move Matrix turns, team turns, delegation, and `/v1/chat/completions` onto the pure shared snapshot read path with explicit degraded hints and request-path zero-refresh assertions.
4. Tighten semantic indexing defaults.
- Add the text-like default file filter, status/API exposure for snapshot freshness, and the remaining tests.

### Migration path

- This can land safely in one PR without a feature flag because all affected call sites are internal, the new path is strictly less blocking, and MindRoom already tolerates missing knowledge by proceeding without it.
- The cleanest minimal-diff migration is to accept one background rebuild of existing shared KBs after deploy rather than carrying a long-lived compatibility shim for the legacy live collection layout.
- That one-time rebuild is acceptable here because Bas is the only user and the new first-init degraded path is explicit and fast instead of hanging the request.

### Default semantic indexing filter

- The default semantic indexing policy should live in `KnowledgeManager`, because that is where candidate files are currently selected, and the minimal config surface should live in `src/mindroom/config/knowledge.py`.
- Default allowlist should be text-like files only, meaning Markdown, plain text, JSON, YAML, TOML, XML, CSV/TSV, HTML, notebooks, and common source-code and config extensions.
- Default denylist should exclude images, audio, video, archives, PDFs, office binaries, model weights, databases, fonts, `.git/**`, and any file that fails a cheap binary sniff such as embedded NUL bytes.
- User override should be a small KB-level include/exclude glob surface that is evaluated after the default text-like filter so a user can explicitly opt a repo subtree or file pattern back in when they really want it indexed.

## D. Test plan

- `Unit:` seed one active shared snapshot, start a background refresh that blocks before publish, and assert reads still return the old snapshot while `refresh_in_progress=true`.
- `Unit:` create a shared KB state with no published snapshot and assert the resolver returns `KnowledgeAccessResult.unavailable(reason="initializing")` quickly without awaiting any sync or reindex method.
- `Integration:` run a full Matrix response through `ResponseRunner.generate_response()` with a deliberately blocked shared KB refresh and assert `Thinking...` is sent before the blocked refresh is released.
- `Integration:` run a full Matrix response against a fresh published shared snapshot with spies on `initialize_manager_for_startup()`, `finish_pending_background_git_startup()`, `sync_git_repository()`, `sync_indexed_files()`, and `reindex_all()`, and assert every counter stays at zero on the request path.
- `Integration:` run `/v1/chat/completions` with no shared snapshot and assert the request no longer awaits `initialize_shared_knowledge_managers()` and instead passes an explicit degraded KB hint into `ai_response()` or `stream_agent_response()`.
- `Integration:` mutate a watched or git-backed shared KB and assert the refresh owner publishes a new snapshot only after the background build completes, while intermediate reads still use the previous snapshot ID.
- `API:` assert `/api/knowledge/bases/{base_id}/status` exposes `active_snapshot_id`, `snapshot_built_at`, `snapshot_age_seconds`, and `refresh_in_progress`.

### Live test idea

- Use an isolated instance or `mindroom-lab.service` with one shared git-backed KB bound to a known agent and `MINDROOM_TIMING=1`.
- Capture `/api/knowledge/bases/{base_id}/status` before the test to record the active snapshot ID and age.
- Push or stage a repo change that forces a non-trivial refresh and confirm the KB status endpoint flips to `refresh_in_progress=true` while the active snapshot ID stays unchanged.
- Send a Matrix turn with `matty` during that refresh and record the event timestamps showing `Thinking...` arrives quickly while the refresh is still running.
- Ask about content that only exists in the new commit and confirm the in-flight turn either uses the old snapshot or explicitly reports KB unavailability on first init, but never blocks waiting for refresh.
- After publish completes, capture the new snapshot ID from the status endpoint and rerun the same question to prove the new snapshot is now being served.
- Keep the correlation-ID-scoped logs as evidence that no request-path `sync_git_repository()` or `reindex_all()` ran for the live turn.

## E. Correctness risks

- `Stale results window:` live turns may serve an older snapshot until background refresh completes, so we must expose `snapshot_age_seconds`, `snapshot_built_at`, and `refresh_in_progress` to make the staleness window observable and bounded by poll interval plus build time.
- `Snapshot publication race:` if we ever mutate the published collection in place we lose the guarantee, so the implementation must build in a separate snapshot location and publish only by atomic pointer swap plus `os.replace()` manifest update.
- `First-init UX:` an empty `knowledge=None` is too implicit, so the first-init path must carry an explicit unavailable reason into the system prompt to stop the agent from pretending it searched the KB.
- `Concurrent shared KB access:` readers should not take a heavy lock, and the publication model should let each read capture one immutable snapshot handle so concurrent searches run lock-free against the chosen snapshot.
- `Restart behavior:` active snapshots must survive process restarts on disk under the KB storage root, and cleanup should keep the active snapshot plus one previous snapshot until a newer publish succeeds cleanly.
- `Config drift during refresh:` a refresh built against old config must not publish into a now-mismatched binding, so snapshot metadata should include the indexing/settings signature and publish should verify it still matches current config.
- `Plugins and hooks:` no built-in hook path currently does KB lifecycle work, but third-party hook code can still block request execution however it wants, and this issue does not attempt to sandbox arbitrary plugin behavior.
- `Admin endpoint visibility change:` upload and delete may no longer be immediately searchable once they become enqueue-only operations, so status reporting and docs must make the “refresh pending” state obvious.

## F. Out of scope

- Changing the embeddings provider, embeddings model, or embedding dimensionality policy.
- Replacing Chroma with a different vector store engine.
- Changing retrieval ranking, result interleaving, chunking strategy, or Agno’s RAG semantics beyond what is necessary to read a published snapshot.
- Redesigning private or request-scoped KB lifecycle beyond preserving today’s behavior for those private scopes.
- Adding distributed leases, cross-pod snapshot coordination, or any other multi-process ownership protocol.
- Improving OCR, PDF parsing, or richer media extraction beyond the default “do not semantically index binary/media files” filter requested here.
- General hook-performance hardening unrelated to shared KB lifecycle.
