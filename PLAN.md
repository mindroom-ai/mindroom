# ISSUE-191 — Synthesized plan

Background-only refresh + last-good-snapshot serving for shared knowledge bases.
Live agent turns must serve from whatever snapshot already exists and must NEVER
await `git fetch`, `reindex_all`, `sync_indexed_files`, `sync_git_repository`,
`finish_pending_background_git_startup`, or `initialize_manager_for_startup` on
the request execution path.

This plan synthesizes `PLAN-codex.md` and `PLAN-claude.md` (preserved alongside
in this commit). Both critiques are committed on the planner branches
(`issue-191-plan-codex` `6c9c3831e`, `issue-191-plan-claude` `2aecd4505`).

## Synthesis decisions (where the plans diverged)

| Question | Decision | Why |
|---|---|---|
| Snapshot abstraction: new on-disk manifest vs. ChromaDB collection-name swap | **Collection-name swap.** No new manifest format, no migration tax. Persist freshness metadata in the existing `_indexing_settings.json` sidecar. | Smallest-diff bias. Manifest + on-disk snapshot dirs are an entire parallel persistence layer for the same atomicity guarantee. |
| Owner of background refresh in `/v1`-only API mode | **Introduce a tiny `KnowledgeRefreshOwner` Protocol.** Orchestrator implements it (Matrix path). API-only mode gets a minimal `StandaloneKnowledgeRefreshOwner` instantiated in `src/mindroom/api/main.py` lifespan when no orchestrator is present. | Codex's critique of Claude is correct: the `/v1` lifespan in `api/main.py:438-465` does not construct an orchestrator. Cannot just delete `_ensure_knowledge_initialized()`. |
| Atomic publication on full `reindex_all` | **Yes — Chroma collection-name shadow swap.** Build into `f"{collection}_pending"`, atomically rebind `KnowledgeManager._knowledge.vector_db` on success, drop the old collection only after the swap. | Codex correctly flagged that current `reindex_all` empties the live collection then re-inserts → readers see torn results during reindex. This IS the symptom-relevant atomicity boundary. |
| Per-file `index_file` / `remove_file` torn reads | **OUT OF SCOPE for this PR.** Mention in follow-up. | These windows are bounded to milliseconds per file and don't cause the user-visible "Thinking…" stall this issue is about. Different issue class. |
| Private (request-scoped) KB lifecycle | **OUT OF SCOPE.** Keep `_create_request_knowledge_manager_for_target` as-is. | Issue is explicitly "shared KB lifecycle/freshness work is too coupled to request execution." |
| Admin upload/delete behavior change | **OUT OF SCOPE.** Stay synchronous on the admin endpoint. | Visibility-after-upload regression isn't justified by ISSUE-191's symptom. |
| Multi-replica K8s coordination | **OUT OF SCOPE.** Document as a prerequisite: deployment is single-pod or has leader election for KB writers. | The bug report says K8s "makes it worse" because of slower disk/control plane, not because of multi-replica races. Multi-replica coordination is a separate problem. |
| File-filter "text-only" allowlist | **Cheap extension allowlist with NO `.pdf/.docx/.doc/.pptx`.** No NUL-byte sniff. User override via include/exclude globs. | Codex correctly caught that those binary office formats contradict the goal. NUL-sniff is cost on the indexing path for marginal gain. |
| Pure-read API for live turns | **Extend the existing `on_missing_bases` callback** in `utils.py:102-122` with a `KnowledgeAvailability` enum (`READY` / `INITIALIZING` / `REFRESH_FAILED` / `CONFIG_MISMATCH`). | Adopt Codex's typed reason taxonomy on top of Claude's narrower API change. |
| Lock contention | **Pure-read path MUST NOT acquire `_shared_knowledge_manager_init_lock`.** Use atomic dict reads against `_shared_knowledge_managers`. | Claude's critique made this explicit; Codex's plan implied it but didn't state it. Will be enforced by a unit test (read-during-held-lock returns in <10ms). |

## A. Diagnosis (consolidated)

Both planners converged on the same six call sites. Restated as a single map:

| Class | Call site | What it triggers |
|---|---|---|
| (b) lifecycle | `src/mindroom/response_runner.py:1648` `_prepare_response_runtime_common` → `_ensure_request_knowledge_managers` | Per-agent → `ensure_agent_knowledge_managers` → `_ensure_shared_knowledge_manager_for_target`. Possibly: `finish_pending_background_git_startup`, `sync_manager_without_full_reindex`, `_create_knowledge_manager_for_target` (init=True). |
| (b) lifecycle | `src/mindroom/teams.py:1499` and `:1852` `_ensure_request_team_knowledge_managers` | Same as above, for every team-member agent. |
| (b) lifecycle | `src/mindroom/custom_tools/delegate.py:95` `DelegateTools.delegate_task` | Same as above, on every delegated invocation. |
| (b) lifecycle | `src/mindroom/api/openai_compat.py:649, 787` `_ensure_knowledge_initialized` | Awaits `initialize_shared_knowledge_managers(start_watchers=False, reindex_on_create=False)` per `/v1/chat/completions` request. Worse than Matrix because no placeholder. |
| (a) snapshot read | `src/mindroom/bot.py:808`, `response_runner.py:1716` `KnowledgeAccessSupport.for_agent` | Pure dict lookup — already fine. Will become the only request-path KB code. |
| (c) admin | `src/mindroom/api/knowledge.py:112, 257` upload/reindex | Operator-initiated. Out of scope. |

Serialization mechanism that makes a request wait: `_shared_knowledge_manager_init_lock(base_id)` (process-global per-base lock). It serializes against the orchestrator's background refresher (`orchestrator._run_knowledge_refresh`) — so even when the per-request lifecycle work is "incremental," a request can park behind the background refresher holding the lock during a `reindex_all`.

Worst-case upper bound today: `KnowledgeGitConfig.sync_timeout_seconds = 3600` (`src/mindroom/config/knowledge.py:32-35`). A stuck `git fetch` can hold a live request open for up to one hour.

Hooks (`src/mindroom/hooks/`), `execution_preparation.py`, and `streaming.py` are clean — no KB lifecycle work entered there. Confirmed by both planners; documented for future safety.

## B. Design

### B.1 Snapshot contract

A "snapshot" is the persistent ChromaDB collection backing one `KnowledgeManager.base_id`. Already atomic from a reader's perspective: `Knowledge.search` → `MultiKnowledgeVectorDb.search` → `ChromaDb.search` is a lock-free read against the on-disk collection.

Publication atomicity for full rebuild: shadow-collection swap (see Commit 4). Per-file `index_file` / `remove_file` retains today's brief torn-read window — out of scope for this PR.

The durable side of the contract (which collection is "live") is the existing `_indexing_settings.json` checkpoint at `manager.py:434-450`. We extend it with `last_published_at`, `published_revision`, `availability` (READY / INITIALIZING / REFRESH_FAILED / CONFIG_MISMATCH).

### B.2 Refresh owner

```python
# src/mindroom/knowledge/refresh_owner.py  (NEW, ~40 LOC)
class KnowledgeRefreshOwner(Protocol):
    """Owns background refresh of shared KBs. Request path NEVER calls this."""
    def schedule_refresh(self, base_id: str) -> None: ...
    def schedule_initial_load(self, base_id: str) -> None: ...
    def is_refreshing(self, base_id: str) -> bool: ...
```

Implementations:
- `OrchestratorKnowledgeRefreshOwner` — wraps existing `MultiAgentOrchestrator._schedule_knowledge_refresh` / `_run_knowledge_refresh` / `initialize_shared_knowledge_managers`. Already exists in spirit.
- `StandaloneKnowledgeRefreshOwner` — minimal scheduler used in `api/main.py` lifespan when no orchestrator is constructed. Wraps a single `asyncio.Task` per base running the same `initialize_shared_knowledge_managers(start_watchers=False, reindex_on_create=False)` flow that `_ensure_knowledge_initialized` runs today, but NOT on the request path.

### B.3 Pure-read API for live turns

Extend the existing `on_missing_bases` callback in `src/mindroom/knowledge/utils.py:102-122` to also receive availability state:

```python
class KnowledgeAvailability(Enum):
    READY = "ready"
    INITIALIZING = "initializing"
    REFRESH_FAILED = "refresh_failed"
    CONFIG_MISMATCH = "config_mismatch"

def get_agent_knowledge(
    ...,
    on_unavailable_bases: Callable[[Mapping[str, KnowledgeAvailability]], None] | None = None,
) -> Knowledge | None: ...
```

`on_unavailable_bases` is called with the `{base_id: availability}` mapping for every shared base that is not READY. The existing `on_missing_bases` callback is preserved as a thin wrapper for backward compatibility within the codebase (only one or two call sites; not a public API).

The pure-read code path:
- Calls `get_published_shared_knowledge_manager(base_id)` — atomic dict read against `_shared_knowledge_managers`. NEVER takes `_shared_knowledge_manager_init_lock`.
- If absent → emits `INITIALIZING`, fires-and-forgets `refresh_owner.schedule_initial_load(base_id)`, returns no `Knowledge` for that base.
- If present but `_indexing_settings.json` says last attempt failed → `REFRESH_FAILED`.
- If present but `_indexing_settings_key` differs from current config → `CONFIG_MISMATCH` and fires-and-forgets a refresh.
- Otherwise → `READY`, returns the `Knowledge` handle.

### B.4 Where the degraded hint surfaces

- Matrix turns (Single agent + team + delegation): `response_runner.py` and `teams.py` collect the `{base_id: availability}` mapping and append a single line to `system_enrichment_items` like:
  > `Knowledge base \`engineering_docs\` is initializing; semantic search is unavailable for this turn. Do not claim to have searched it.`
- `/v1/chat/completions`: prepended as a `system` message before the user's first message. (No native enrichment channel; explicit system message is the cleanest equivalent.)

### B.5 Binding semantics

`agent.knowledge_bases = [...]` becomes pure visibility/auth. Enforced by:
- `Config.get_agent_knowledge_base_ids(agent_name)` continues as the only authority.
- `KnowledgeAccessSupport.for_agent` becomes the only request-path entry — it does NO lifecycle work.
- `_ensure_request_knowledge_managers` and `_ensure_request_team_knowledge_managers` are reduced to private-base resolution only (shared bases skipped). `_ensure_knowledge_initialized` is removed from the `/v1` request path.

## C. Implementation plan — minimal diff, ordered

Estimated total: **~200-350 net production LOC** + ~250-400 LOC of tests. ~5 files changed in production.

### Commit 1 — pure-read for shared bases on the request path (~80-120 LOC)

- `src/mindroom/knowledge/shared_managers.py`
  - Add `get_published_shared_knowledge_manager(base_id, *, config, runtime_paths) -> KnowledgeManager | None` — atomic dict read, no lock, no init.
  - In `ensure_agent_knowledge_managers`: skip shared bases (`if not target.binding.request_scoped: continue`). Private bases unchanged.
  - Mark `_ensure_shared_knowledge_manager_for_target` as private to `initialize_shared_knowledge_managers` / admin call sites only.
- `src/mindroom/knowledge/utils.py`
  - Add `KnowledgeAvailability` enum.
  - `_get_knowledge_for_base` calls `get_published_shared_knowledge_manager` for shared bases. Returns `None` immediately when not READY and reports availability via the new callback.
  - Extend `get_agent_knowledge` / `resolve_agent_knowledge` with `on_unavailable_bases`.
- `src/mindroom/runtime_resolution.py`
  - Force `incremental_sync_on_access=False` for shared bases at resolve time (it's now a hint to orchestrator startup wiring, not the request path).

Test gate: `tests/test_knowledge_manager.py` — new tests:
- `test_get_published_shared_knowledge_manager_returns_in_under_10ms_with_init_lock_held`
- `test_get_published_shared_knowledge_manager_returns_none_before_init`
- `test_ensure_agent_knowledge_managers_skips_shared_bases`

### Commit 2 — `/v1/chat/completions` standalone refresh owner (~40-70 LOC)

- `src/mindroom/knowledge/refresh_owner.py` (NEW) — `KnowledgeRefreshOwner` Protocol + `StandaloneKnowledgeRefreshOwner`.
- `src/mindroom/api/main.py`
  - In lifespan, if no orchestrator is present, construct a `StandaloneKnowledgeRefreshOwner`, kick off `schedule_initial_load(base_id)` for every configured shared base.
- `src/mindroom/api/openai_compat.py`
  - DELETE `_ensure_knowledge_initialized` and its two callers at `:649, :787`.
  - When `KnowledgeAvailability != READY`, prepend a system message to the OpenAI request describing the unavailable bases.
- `src/mindroom/orchestrator.py`
  - Wrap existing background refresh in `OrchestratorKnowledgeRefreshOwner` so `utils.py` can fire `schedule_initial_load` without knowing which owner is present.

Test gate:
- `tests/test_openai_compat.py::test_chat_completions_does_not_await_initialize_shared_knowledge_managers`
- `tests/test_openai_compat.py::test_unready_kb_emits_system_hint`

### Commit 3 — Matrix request path consumes degraded hint (~50-90 LOC)

- `src/mindroom/response_runner.py`
  - In `_prepare_response_runtime_common` / `_ensure_request_knowledge_managers`: skip shared bases (delegated to Commit 1's helper) and collect availability via the new callback.
  - When availability is non-READY, append a system enrichment item.
- `src/mindroom/teams.py`
  - Same in `_ensure_request_team_knowledge_managers` and along team-member materialization.
- `src/mindroom/custom_tools/delegate.py`
  - Same in `delegate_task` — non-blocking even for delegations.

Test gate (`tests/test_streaming_behavior.py` is the smaller seam per Claude's critique):
- `test_chat_turn_does_not_block_on_repo_change` — placeholder timing assertion vs. an artificially blocked sync.
- `test_fresh_turn_does_zero_freshness_work_on_request_path` — spies on `initialize_manager_for_startup`, `finish_pending_background_git_startup`, `sync_git_repository`, `sync_indexed_files`, `reindex_all` all return zero counts.

### Commit 4 — shadow-collection swap on `reindex_all` (~50-80 LOC)

- `src/mindroom/knowledge/manager.py`
  - In `reindex_all`: build into `f"{collection_name}_pending"`, atomically rebind `self._knowledge.vector_db` to the new collection on success, drop the old collection only after the swap.
  - On failure: leave the previous collection intact so live turns still see the last-good snapshot.
  - Verify `MultiKnowledgeVectorDb`'s captured `vector_db` references survive the swap (manager._knowledge is the same object — only its `vector_db` attribute is rebound). Document explicitly in code comment.
- Update `_indexing_settings.json` write to record `last_published_at`, the resolved `published_revision`, and `availability` after a successful swap.

Test gate:
- `test_search_during_reindex_returns_results_against_old_collection` — start `reindex_all` against a slow embedder, assert `Knowledge.search` returns pre-reindex documents during the in-flight reindex.
- `test_reindex_all_failure_preserves_previous_snapshot` — reindex with a partway-through embedder error; old collection still serves reads after failure.

### Commit 5 — default text-like file filter for non-git bases (~20-40 LOC)

- `src/mindroom/knowledge/manager.py`
  - Add `_TEXT_LIKE_EXTENSIONS = {".md", ".markdown", ".txt", ".text", ".rst", ".json", ".yaml", ".yml", ".toml", ".ini", ".csv", ".tsv", ".html", ".xml"} | {common source-code extensions}`. Note: NO `.pdf`, `.docx`, `.doc`, `.pptx`.
  - `_include_file` checks `include_extensions` / `exclude_extensions` on `KnowledgeBaseConfig` first, falls back to `_TEXT_LIKE_EXTENSIONS`.
- `src/mindroom/config/knowledge.py`
  - Add `include_extensions: list[str] | None`, `exclude_extensions: list[str] = []` to `KnowledgeBaseConfig`.

Test gate:
- `test_text_only_default_file_filter_excludes_binary` — config with image/audio/video/pdf and a markdown file; only markdown indexed.
- `test_user_override_can_re_enable_specific_extensions` — explicit `include_extensions=[".pdf"]` opts back in.

### Migration / flag day

No feature flag. Each commit is independently shippable. If the PR has to land in two passes, commits 1-3 alone close the P1 (the request path no longer awaits any KB lifecycle work). Commits 4 and 5 are hardening and the binary-pollution fix; nice-to-have.

The Chroma collection-swap approach requires NO data migration — existing collections are still valid. A settings-key change still triggers a full reindex via the orchestrator's background owner; that reindex will be the first to use the shadow-collection path.

## D. Test plan (consolidated)

Listed above per-commit. Adding the live-test recipe:

### Live test against `mindroom-lab.service`

1. Boot `mindroom-lab.service` with one git-backed shared KB pointing at a ~100-file repo. Wait for first sync. Confirm via admin API that the manager is published.
2. From a separate shell, `git commit` to the bound repo (or simulate by editing a tracked file and committing locally with `poll_interval_seconds=5`).
3. Within 1 second of the push, send a chat turn to an agent that uses that KB.
4. Assert via lab service logs:
   - `placeholder_sent` timing entry fires before `Knowledge Git repository synchronized` for that base_id.
   - The model run starts before the git sync completes.
5. Counter-test: `kill -STOP $(pgrep -f "git fetch")` to artificially block the sync. Send a turn. Assert the turn completes anyway in under 2× the no-KB baseline latency.
6. Save: `placeholder_sent_ts`, first-token-ts, `git_sync_completed_ts` for each scenario as evidence under `/tmp/ISSUE-191-evidence/`.

## E. Correctness risks

1. **Stale results window.** Bounded by `git_config.poll_interval_seconds` (default 300s) for git-backed bases. Already user-configurable. Observable via the new `availability` field in `_indexing_settings.json` and the existing log lines.
2. **Snapshot publication race during full reindex.** Addressed in Commit 4 (collection-name swap). Per-file `index_file` torn reads remain — out of scope.
3. **`MultiKnowledgeVectorDb.vector_dbs` mid-Agent-run.** When a snapshot is republished mid-run, the agent's already-resolved `Knowledge` handle still points to `manager._knowledge` (same object); only its `.vector_db` attribute is rebound. Next `Knowledge.search` call sees the new collection. Verified by `test_search_during_reindex_returns_results_against_old_collection`.
4. **Config drift during refresh.** Addressed by Commit 4's pre-swap settings-signature check. A snapshot built against an obsolete settings key is discarded; the orchestrator schedules another build with current settings.
5. **First-init UX.** Handled by `KnowledgeAvailability=INITIALIZING` + system enrichment hint. The agent learns "KB X is initializing" instead of hanging.
6. **`_v1` standalone owner.** Commit 2's `StandaloneKnowledgeRefreshOwner` ensures the API-only deployment has a real background owner. Without this, removing `_ensure_knowledge_initialized` would leave shared bases never initialized in API-only mode.
7. **Plugins/hooks that today depend on synchronous KB access.** Both planners verified none exist in `src/mindroom/hooks/`. Third-party plugins relying on undocumented behavior will need to call the admin API.
8. **Settings-change reindex no longer happens on the request thread.** Now exclusively via the refresh owner. Old snapshot keeps serving until the new shadow collection is published atomically.

## F. Out of scope (explicit)

- Multi-replica K8s coordination (deployment must be single-pod or have leader election for KB writers; documented as a prerequisite, NOT solved here).
- Embedding model / dimensionality / provider.
- Vector store engine choice (still ChromaDB).
- RAG retrieval ranking, top-k tuning, reranking, chunking strategy.
- Mem0 memory system.
- Per-file `index_file`/`remove_file` torn-read window (different bug class).
- Private (request-scoped) KB lifecycle (out of stated scope).
- Admin upload/delete endpoint behavior change (stays synchronous).
- Repo checkout for non-semantic tooling (shell/file/coding tools). The existing `ensure_git_checkout_ready` path stays as-is; only the indexing half is decoupled.
- General hook-performance hardening unrelated to shared KB lifecycle.
- New on-disk snapshot manifest layer (rejected — Chroma's existing persistence + `_indexing_settings.json` sidecar is sufficient).
- Binary-content sniffing during indexing (rejected — extension allowlist is sufficient).
- Distributed / cross-pod snapshot coordination protocols.

## Phase log

- **2026-04-23 09:22 PDT** Codex planner committed `PLAN.md` (`646e9297f`, 204 LOC).
- **2026-04-23 09:22 PDT** Claude planner committed `PLAN.md` (`8d2b1d705`, 449 LOC).
- **2026-04-23 09:47 PDT** Codex critique of Claude (`6c9c3831e`): REWORK. Key blockers: false snapshot premise, `/v1` no-orchestrator gap, binary-format allowlist contradiction, `MultiKnowledgeVectorDb` handle pinning.
- **2026-04-23 09:47 PDT** Claude critique of Codex (`2aecd4505`): APPROVE_WITH_NITS, scope 3-4× larger than needed, snapshot manifest is duplicate of Chroma persistence, lock-acquisition rule not explicit.
- **2026-04-23 ~10:00 PDT** Synthesized plan committed as first commit on `issue-191`.
