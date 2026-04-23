# ISSUE-191 — Plan (claude)

Decouple shared knowledge-base lifecycle work from the per-turn request path.
Live turns must serve from whatever Chroma snapshot already exists, never block
on `git fetch`, `reindex_all`, or settings-change reconciliation.

## A. Diagnosis — where shared KB lifecycle work enters the request path

### A.1 Per-turn entry points (request execution path)

| # | Call site | What it triggers | Class |
|---|---|---|---|
| 1 | `src/mindroom/response_runner.py:1648` (`_prepare_response_runtime_common` → `_ensure_request_knowledge_managers`) | per-agent → `ensure_request_knowledge_managers` → `ensure_agent_knowledge_managers` (`start_watchers=True`) | (b) lifecycle |
| 2 | `src/mindroom/teams.py:1499` and `:1852` (`_ensure_request_team_knowledge_managers` → `ensure_request_knowledge_managers`) | same as #1 for every team-member agent | (b) lifecycle |
| 3 | `src/mindroom/custom_tools/delegate.py:95` (`DelegateToolkit.delegate` → `ensure_request_knowledge_managers`) | same on every delegated invocation | (b) lifecycle |
| 4 | `src/mindroom/api/openai_compat.py:649, 787` (`_ensure_knowledge_initialized` → `initialize_shared_knowledge_managers(start_watchers=False, reindex_on_create=False)`) | every `/v1/chat/completions` request (re-)initializes ALL shared bases | (b) lifecycle |
| 5 | `src/mindroom/bot.py:808` and `response_runner.py:1716` (`KnowledgeAccessSupport.for_agent`) | pure dict lookup of `orchestrator.knowledge_managers` + `_get_knowledge_for_base` | (a) snapshot read |
| 6 | `src/mindroom/api/knowledge.py:112, 257` (admin upload / reindex routes) | admin endpoint, NOT a chat-turn path | (c) admin |

### A.2 What entry #1–#4 actually serialize on (the slow stuff)

`ensure_agent_knowledge_managers` → `_ensure_shared_knowledge_manager_for_target`
in `src/mindroom/knowledge/shared_managers.py:266-330`. Under
`_shared_knowledge_manager_init_lock(base_id)` (a process-global per-base lock):

1. `existing.needs_full_reindex(config, …)` (`shared_managers.py:282`,
   `manager.py:498-510`). If any indexing-affecting setting changed
   (`_indexing_settings_key` covers embedder, paths, chunk size/overlap, all
   git-config glob/branch fields), it tears down the old manager and runs
   `_create_knowledge_manager_for_target(reindex_on_create=True)` →
   `manager.initialize()` → `sync_git_repository(index_changes=False)` +
   `reindex_all()`. **Synchronous reclone + full reindex on the request
   thread.** (manager.py:972-986, 1327-1340)
2. `existing._refresh_settings(...)` (cheap).
3. `_shared_manager_has_background_runtime(existing)` is False (e.g. between
   bootstrap and the post-`set_runtime_ready` background refresh, or after a
   refresh task crash) → `await existing.finish_pending_background_git_startup()`
   (`shared_managers.py:316`, `manager.py:1083-1120`). This runs
   `sync_git_repository(index_changes=False)` + either `reindex_all()` or
   `sync_indexed_files()` under `_git_startup_lock`. **Synchronous git fetch +
   per-file index work on the request thread.**
4. `target.binding.incremental_sync_on_access` → `sync_manager_without_full_reindex`
   (`shared_managers.py:318`, `startup.py:25-31`). For git-backed bases this
   calls `sync_git_repository()` (full sync + per-changed-file index). For
   non-git bases it calls `sync_indexed_files()` (filesystem walk + per-file
   index). **Synchronous on the request thread.**
5. The `_shared_knowledge_manager_init_lock` ALSO serializes against
   `orchestrator._run_knowledge_refresh` (`orchestrator.py:474-504`), which is
   the post-startup background reconcile. So even when the per-request work is
   trivial, a request can park behind the background refresher holding the
   lock during a `reindex_all()`.

For private (per-agent / per-user) bases, `target.binding.request_scoped` is
True and `_create_request_knowledge_manager_for_target` constructs a new
`KnowledgeManager` per request and runs `initialize_manager_for_startup` on it.
On a cold pod this is the full clone + index path.

### A.3 Tangential entries that are NOT on the live turn path

- `src/mindroom/api/knowledge.py:112, 257` — admin REST routes for upload /
  reindex. Already explicit, document only.
- `src/mindroom/orchestrator.py:352, 485, 1091` — startup + background refresh
  scheduler. Owns lifecycle by design, fine.
- Hooks (`src/mindroom/hooks/*.py`, `agent:started`, `message:enrich`,
  `message:before_response`, `command:execute`): no KB calls reachable. (`grep`
  confirmed no `knowledge` imports in `src/mindroom/hooks/`.)
- Routing / agent-policy / streaming: no KB calls.

### A.4 Observed timing relative to `Thinking…`

`response_runner.run_cancellable_response` (`response_runner.py:1531-1539`)
sends the `Thinking…` placeholder BEFORE `response_function` runs. So the
visible-placeholder timing is generally fine for fresh turns. BUT:

- For `existing_event_id` paths (interactive selections, edits, regenerations),
  `thinking_msg = None` (`response_runner.py:2334`) and the user sees no
  "working" signal at all — the existing event acts as the placeholder and any
  blocking on `_ensure_request_knowledge_managers` is invisible to them.
- For team coordination (`generate_team_response_helper_locked`), runtime prep
  including `_ensure_request_team_knowledge_managers` happens after the team
  "Thinking…" placeholder is sent, but stretches "Thinking…" arbitrarily.
- The `/v1/chat/completions` API path (#4) blocks BEFORE any HTTP response is
  written.
- Cold-pod first turn cost: a 100-MB-ish repo with several hundred files plus
  embeddings produces minute-scale `reindex_all` runs; even on a warm pod a
  `git fetch` + a few changed files is single-digit seconds — long enough that
  the user notices the `Thinking…` stall.

## B. Proposed design — background-only refresh + last-good-snapshot serving

### B.1 Snapshot contract

A "snapshot" is the persistent ChromaDB collection backing one
`KnowledgeManager.base_id`. It is already atomic from a reader's perspective:
`Knowledge.search` / `MultiKnowledgeVectorDb.search`
(`src/mindroom/knowledge/utils.py:183-235`) hits `ChromaDb.search`, which is a
read against the persistent collection and never takes any of MindRoom's
asyncio locks (`_lock`, `_state_lock`, `_git_sync_lock`, `_git_startup_lock`,
`_shared_knowledge_manager_init_lock`).

Publication atomicity for changes:
- Existing `_index_file_locked` does `remove_vectors_by_metadata` then
  `ainsert` (`manager.py:1266-1304`). For one file this is intentionally not
  atomic — readers can briefly see that file missing. Out of scope for #191.
- `reindex_all` already calls `_reset_collection()` + per-file insert under
  `_lock` — readers can see an empty collection mid-reindex. We will narrow
  this by writing to a **shadow collection** during full reindex and swapping
  the published collection name only on success. (See section C, step 5.)

The `_indexing_settings.json` checkpoint
(`manager.py:434-450`) already names the resetting / indexing / complete state
and is used to decide between full_reindex / resume / incremental on next
boot. This is the durable side of the contract.

### B.2 Background owner of refresh work

Single source of truth: `MultiAgentOrchestrator`. It already owns:
- `initialize_shared_knowledge_managers` at startup (`orchestrator.py:352`).
- `_schedule_knowledge_refresh` (post-`set_runtime_ready`, after config
  reloads) (`orchestrator.py:1091, 1115, 1334, 1359`).
- `_git_sync_loop` per shared manager (`manager.py:1186-1208`) and the
  `_watch_loop` per shared manager (`manager.py:1400-1407`).

Add: `_schedule_per_base_initial_refresh(base_id)`. After a snapshot for a
base is unavailable (no Chroma collection or initialization deferred), the
orchestrator owns the one-shot refresh that publishes the first snapshot. The
request path enqueues if needed but never awaits.

### B.3 Read API live turns will use

Replace the per-turn `_ensure_request_knowledge_managers` for SHARED bases
with a pure lookup. For PRIVATE (request-scoped) bases, keep request-scoped
creation but also make it non-blocking with a degraded fallback when the
snapshot doesn't exist yet.

Concretely:

```python
# in shared_managers.py — new public helper, replacing the per-request path
def get_published_shared_knowledge_manager(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> KnowledgeManager | None:
    """Return the currently published shared manager, or None if not yet ready.

    NEVER blocks. NEVER triggers init / sync / reindex. NEVER acquires the
    init lock — uses an atomic dict read against `_shared_knowledge_managers`.
    """
```

`KnowledgeAccessSupport.for_agent` already does the right thing at the read
side; we change `response_runner._ensure_request_knowledge_managers` and
`teams._ensure_request_team_knowledge_managers` to:

1. Return `{}` for any shared base — `for_agent` will then fall through to
   the orchestrator-published manager via `get_published_shared_knowledge_manager`.
2. Only resolve PRIVATE bases per-request (small bounded local work — these
   are per-agent/per-user `__agent_private__:` IDs from
   `Config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX`).

Even private-base creation gets a fast-fail variant: if a snapshot for the
private base does not exist yet, return `None` and enqueue a background
init via `orchestrator._schedule_private_init`.

### B.4 First-init degraded state

`_get_knowledge_for_base` already returns `None` for missing bases and
`resolve_agent_knowledge` invokes `on_missing_bases` (`utils.py:78-122,
274-298`). We tighten it:

- Add a `KnowledgeAvailability` sentinel return: `READY` (snapshot present),
  `INITIALIZING` (background task running), `UNAVAILABLE` (no manager and not
  yet scheduled).
- For non-`READY` we DO NOT pass a `Knowledge` object to the agent. The
  existing `on_missing_bases` log already fires; we additionally surface a
  one-line note in the system enrichment for that turn ("Knowledge base
  `engineering_docs` is still warming up; semantic search is unavailable for
  this turn."). The LLM sees a tool-availability hint instead of a hang.
- All three states resolve in O(1) — no I/O, no locks.

### B.5 When (if ever) the request path may trigger refresh

Never. The only exception is the admin REST path
(`src/mindroom/api/knowledge.py`), which is operator-initiated and may
explicitly await completion. Document that contract.

### B.6 `agent.knowledge_bases = [...]` semantics

After the change, the binding is purely:
- A visibility/auth filter: `Config.get_agent_knowledge_base_ids(agent_name)`
  (`config/main.py`) is the only authority.
- A snapshot-read selector: passed to `MultiKnowledgeVectorDb` via
  `_merge_knowledge` (`utils.py:258-272`).

No refresh, sync, or reindex work is bundled. Enforced by removing
shared-base creation from `ensure_agent_knowledge_managers` /
`ensure_request_knowledge_managers` and never calling `ensure_*` from
`KnowledgeAccessSupport.for_agent`.

## C. Implementation plan — minimal-diff, ordered

### Commit 1 — read-only request path for shared bases (~80 LOC net)

- `src/mindroom/knowledge/shared_managers.py`
  - Add `get_published_shared_knowledge_manager(base_id, *, config, runtime_paths)`:
    pure lookup against `_shared_knowledge_managers`, no lock, no init.
  - In `ensure_agent_knowledge_managers`, change the loop body to:
    ```python
    if not target.binding.request_scoped:
        continue  # snapshot reads use the orchestrator-published manager
    managers[base_id] = await _create_request_knowledge_manager_for_target(...)
    ```
  - Delete/inline `_ensure_shared_knowledge_manager_for_target`'s on-access
    refresh branches (`shared_managers.py:312-318`). Remaining call sites
    (`initialize_shared_knowledge_managers`, `ensure_shared_knowledge_manager`,
    admin API) still need it; mark it private to those call sites.
  - Delete `_reconcile_shared_manager_runtime`'s `incremental_sync_on_access`
    branch (`shared_managers.py:202-205`) — only orchestrator drives runtime.
- `src/mindroom/knowledge/utils.py`
  - Update `_get_knowledge_for_base` to call
    `get_published_shared_knowledge_manager` directly (drops the
    `shared_manager_lookup` indirection — single source of truth, fewer
    parameters).
- `src/mindroom/runtime_resolution.py`
  - In `resolve_knowledge_binding`, set `incremental_sync_on_access=False`
    unconditionally for shared bases. The field is now used only as a hint
    for orchestrator startup wiring; rename if any callers care (they don't —
    grep shows only `shared_managers.py`).
- `src/mindroom/api/openai_compat.py`
  - Delete `_ensure_knowledge_initialized` and the two callers
    (`:649, :787`). The orchestrator already owns shared-base init.

Commit message: `plan(issue-191): read-only shared-KB request path`

### Commit 2 — fast-fail for in-flight first init (~50 LOC net)

- `src/mindroom/knowledge/shared_managers.py`
  - Track `_shared_knowledge_initializing: set[str]` (process-global). Set on
    entry to `_create_knowledge_manager_for_target`, clear on exit.
  - Add `is_shared_knowledge_initializing(base_id) -> bool`.
- `src/mindroom/knowledge/utils.py`
  - In `_get_knowledge_for_base`, when no manager is published yet, return
    `None` immediately AND record availability so the caller can emit an
    enrichment hint.
  - Extend `KnowledgeAccessSupport.for_agent` signature to optionally return
    a `KnowledgeAvailability` summary (no behavior change for callers that
    ignore it).
- `src/mindroom/response_runner.py` and `src/mindroom/teams.py`
  - When `KnowledgeAvailability != READY` for any bound base, emit a
    one-line system enrichment item via the existing
    `system_enrichment_items` plumbing (`teams.py:1490`,
    response_runner equivalent).

Commit message: `plan(issue-191): degraded-state hint for warming KB`

### Commit 3 — background-only first init for private bases (~40 LOC net)

- `src/mindroom/orchestrator.py`
  - Add `_schedule_private_knowledge_init(base_id, execution_identity)` that
    creates a logged task running `_create_request_knowledge_manager_for_target`.
  - Cache running tasks in `_private_knowledge_init_tasks: dict[str, Task]`
    keyed by `(base_id, worker_key)`.
- `src/mindroom/knowledge/shared_managers.py`
  - In `ensure_agent_knowledge_managers` for private bases: if the
    request-scoped manager is already cached, return it; otherwise enqueue
    via the orchestrator and return `None` for that base on this turn.
  - Cache request-scoped managers in a process-global
    `_request_knowledge_managers: dict[_KnowledgeManagerKey, KnowledgeManager]`
    so subsequent turns from the same worker can reuse the published
    snapshot.
- Edit `KnowledgeAccessSupport.for_agent` to surface `INITIALIZING` for
  private bases too.

Commit message: `plan(issue-191): background-init private KB`

### Commit 4 — atomic shadow-swap on full reindex (~80 LOC net)

- `src/mindroom/knowledge/manager.py`
  - In `reindex_all`, write into a SHADOW collection
    (`f"{collection_name}_pending"`) and update vector-db wiring atomically
    on success. Old collection deleted only after the swap.
  - On failure, leave the previous collection intact so live turns still see
    the last-good snapshot.
- The change is local to `reindex_all` and `_reset_collection` — search
  callers continue to use `Knowledge.vector_db`.

Commit message: `plan(issue-191): shadow-swap reindex publication`

### Commit 5 — default text-only file filter for non-git bases (~30 LOC net)

- `src/mindroom/knowledge/manager.py`
  - Add `_TEXT_LIKE_EXTENSIONS` constant covering: `.md, .markdown, .txt,
    .text, .rst, .json, .yaml, .yml, .toml, .ini, .csv, .tsv, .pdf, .docx,
    .doc, .pptx`. Anything else → skipped at `_include_file`.
  - Honor `KnowledgeBaseConfig.include_extensions` /
    `exclude_extensions` overrides.
- `src/mindroom/config/knowledge.py`
  - Add to `KnowledgeBaseConfig`:
    ```python
    include_extensions: list[str] | None = None
    exclude_extensions: list[str] = Field(default_factory=list)
    ```
  - For git-backed bases, the existing `include_patterns` /
    `exclude_patterns` already let users be more precise; the extension
    filter is ANDed with them.
- For any user who already has a binary-polluted index, the next reindex
  will produce a clean snapshot (this is acceptable per
  CLAUDE.md "no backwards-compat shims").

Commit message: `plan(issue-191): default text-only KB file filter`

### Migration / flag day

No feature flag. Each commit is independently testable and shippable.
Order is:
1. Commits 1–3 land the request-path/snapshot-serving fix.
2. Commit 4 hardens the snapshot publication (no longer mandatory but very
   cheap and removes the empty-collection-during-reindex window).
3. Commit 5 fixes the binary-pollution complaint.

The series can be one PR (ship altogether) since Bas is the only user. If we
need to land in two passes, commits 1+2+3 alone are a complete fix for the
P1; 4+5 are improvements.

## D. Test plan

### D.1 Unit (target: `tests/test_knowledge_manager.py`)

- `test_get_published_shared_knowledge_manager_does_not_block_during_reindex`
  - Spawn a `KnowledgeManager`, hold its `_lock` (simulating in-flight
    `reindex_all`), call `get_published_shared_knowledge_manager` and assert
    it returns within 10ms.
- `test_get_published_shared_knowledge_manager_returns_none_before_init`
  - With no shared manager registered, lookup returns `None` in <1ms.
- `test_search_during_reindex_returns_results_against_old_collection`
  - After commit 4: write a snapshot, start a `reindex_all` against a slow
    embedder (asyncio.Event), assert `Knowledge.search` still returns the
    pre-reindex documents during the in-flight reindex.
- `test_ensure_agent_knowledge_managers_skips_shared_bases`
  - Bound agent with one shared + one private base. Returned dict contains
    only the private-base manager; shared base resolves through the
    orchestrator publication path.
- `test_text_only_default_file_filter_excludes_binary` (commit 5)
  - Non-git base with `image.png`, `audio.mp3`, `notes.md`. Only `notes.md`
    appears in `list_files()` and `reindex_all` count.

### D.2 Integration (`tests/test_streaming_behavior.py` /
`tests/test_ai_*.py` family)

- `test_chat_turn_does_not_block_on_repo_change`
  - Wire a stub `KnowledgeManager` that, while a `_git_sync_loop` is
    artificially paused mid-fetch, runs an end-to-end streaming turn through
    `process_and_respond_streaming`. Assert
    `pipeline_timing.placeholder_sent` precedes the first model token by
    less than the stub's pause. (Use `MatrixIDS` test fixtures already
    present in tests.)
- `test_fresh_turn_does_zero_freshness_work_on_request_path`
  - Spy on `KnowledgeManager.sync_git_repository`,
    `sync_indexed_files`, `reindex_all`, `finish_pending_background_git_startup`,
    `_git_sync_loop`. Run a streaming turn against an already-published
    shared manager. Assert all four spies record zero calls.
- `test_first_init_returns_degraded_hint`
  - Boot orchestrator with a slow git clone (asyncio.Event blocking). Send
    a turn. Assert the system enrichment passed to the LLM contains the
    "warming up" hint and the run completes without ever awaiting the
    clone.

### D.3 Live test (against `mindroom-lab.service` or local stack)

Bespoke evidence the bug is gone:

1. Boot a local stack with one git-backed shared KB pointing at a
   ~100-file repo. Wait for the first sync to complete; confirm via
   `/api/knowledge/bases` that `indexed_count > 0`.
2. From a separate shell, push a commit to that repo's branch (or simulate
   by editing a tracked file and committing locally — `poll_interval_seconds`
   = 5 for the test).
3. Within 1 second of the push, send via `matty`:
   `matty send "Lobby" "@mindroom_research summarize the README"`.
4. Tail backend logs and assert:
   - `placeholder_sent` timing entry fires before
     `Knowledge Git repository synchronized` for that base_id.
   - The model run starts before the git sync completes.
5. Confirm the response itself is correct (uses pre-push snapshot — a
   stale-but-bounded result, which is the explicit acceptance per the
   issue report).

Counter-test: pause the git sync loop with a `signal::SIGSTOP` on the
`git fetch` subprocess (or use `iptables -A OUTPUT -j DROP --dport 443`
against github.com). Send a turn. Assert the turn completes anyway in
under 2× the no-KB baseline latency.

## E. Correctness risks

1. **Stale results window.** Bound by `git_config.poll_interval_seconds`
   (default 300s) for git-backed bases and by the watcher latency for
   filesystem bases. Already user-configurable. Observable via
   `KnowledgeManager.get_status().git.last_successful_sync_at` and the
   existing `Knowledge Git repository synchronized` log line.
2. **Snapshot publication race.** Currently `reindex_all` wipes the
   collection then re-inserts under `_lock`. Searches (no lock) can hit an
   empty collection mid-reindex. Commit 4's shadow-swap eliminates this.
   Until commit 4 lands, searches during reindex return `[]` — degraded but
   not blocking.
3. **First-init UX.** Handled by `KnowledgeAvailability` + system
   enrichment hint (B.4). The agent learns "KB X warming up" and can answer
   without it instead of hanging.
4. **Concurrent shared KB access from multiple agents.** The published
   manager is a single instance per base in `_shared_knowledge_managers`.
   `Knowledge.search` and `MultiKnowledgeVectorDb.async_search` are reentrant
   (Chroma handles it). No new locking needed.
5. **Restart behavior.** Snapshots survive restart because Chroma persists
   to disk under `mindroom_data/knowledge_db/<base_storage_key>/`. The
   `_indexing_settings.json` checkpoint already drives the resume / full
   reindex decision (`manager.py:474-484`). Cleanup policy unchanged: a
   settings change forces full reindex; the new shadow-swap means the old
   snapshot stays live until the new one is ready.
6. **Plugins / hooks depending on synchronous KB access.** None found.
   `grep -rn "knowledge" src/mindroom/hooks/` is empty. Any third-party
   plugin that called `ensure_request_knowledge_managers` for a shared base
   to side-effect a sync was relying on undocumented behavior; it now needs
   to schedule its own refresh through the admin API.
7. **Settings-change reindex no longer happens on the request thread.**
   This now happens via `orchestrator._schedule_knowledge_refresh`
   exclusively. If a config reload pumps a new embedder, the next request
   sees the OLD published manager (correct snapshot) until the background
   reindex completes and the orchestrator publishes the new manager. With
   commit 4's shadow-swap, the swap is atomic.
8. **API endpoint behavior change.** `/v1/chat/completions` no longer
   self-initializes shared KBs. Acceptable because the orchestrator boots
   them at startup; callers hitting the API before orchestrator readiness
   already see other failures.

## F. Out of scope (explicit)

- Embedding model selection / configuration.
- Vector store engine choice (still ChromaDB).
- RAG retrieval ranking, top-k tuning, reranking.
- Document chunking strategy beyond the existing `SafeFixedSizeChunking`.
- Mem0 memory system.
- Knowledge UI / admin REST API behavior changes beyond noting they remain
  the explicit refresh path.
- Repo checkout for non-semantic tooling (shell/file/coding tools). The
  existing `ensure_git_checkout_ready` path stays as-is; only the indexing
  half is decoupled.
- Multi-process snapshot leasing (single-process orchestrator owns
  refresh; this is fine for current MindRoom topology).
