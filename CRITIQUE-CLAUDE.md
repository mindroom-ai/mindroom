# ISSUE-237 critique — PLAN.md (mine) vs PLAN-OTHER.md

All factual claims below were re-verified against the worktree source and the installed agno package, not taken from either plan.

## 1. Steelman of PLAN-OTHER

**Passive health recording in the embedder is its best idea.**
PLAN-OTHER §2 (lines 61-63) has `MindRoomOpenAIEmbedder` record a failed snapshot before re-raising and a healthy snapshot after every non-empty vector.
This makes recovery self-clearing: the moment the operator fixes the credential, the first real memory search or knowledge query flips the health state back, with no restart and no probe scheduling.
My plan records health only at startup and in the refresh-scheduler failure branch, which produces a genuine stale-state bug (see §3).

**Hot-reload re-probe is a real gap it closes.**
PLAN-OTHER §3 (line 70) re-runs the probe when a hot reload changes `memory.embedder` and lets a later success clear the degraded snapshot.
My plan probes only at `_start_runtime`, so after a hot reload the recorded state can describe an embedder that no longer exists.

**Category discipline in operator advice.**
PLAN-OTHER line 58 explicitly separates auth failures from other 4xx, 5xx, transport, and empty-vector failures "so operators do not receive misleading credential advice."
This exposed a concrete flaw in my §2f: my tool message appends "Fix the embedder credential (…)" whenever `semantic_error` is set and results are empty, including when the cause was a timeout or a 5xx, which is exactly the misleading advice they warn about.

**Doctor reuses the probe.**
PLAN-OTHER §1 (lines 41-42) routes `mindroom doctor` through the same resolver *and* the same health probe, so doctor validates the exact endpoint, model, and credential the runtime uses.
My plan swaps only the key resolution and keeps the separate `_validate_openai_embeddings_endpoint`, leaving two live-check implementations where one would do.

**Sharper leak-prevention test assertions.**
Its test plan (lines 182-183) asserts that both construction paths (`_get_memory_config` and `create_configured_embedder`) receive the *same* resolved key for the same config, and that no credential value appears in collection names, indexing metadata, logs, or health payloads.
My test plan checks each path independently but never asserts cross-path agreement or non-leakage; both are cheap and worth having.

**Concrete recovery smoke test.**
The manual test (lines 212-214) — disposable local 401 server, observe degraded health, fix the credential, observe the state clear — validates the recovery path end to end, which my "doctor does a live POST" note does not.

## 2. Attack on PLAN-OTHER

**§6 is built on a false premise and is the plan's biggest over-engineering.**
Line 20 and §6 claim agno "catches vector-search exceptions internally and can still return `No documents found`", justifying wrapping every generated `search_knowledge_base` entrypoint (sync and async), inventing "lightweight MindRoom knowledge handles with no queryable vector databases", extending `KnowledgeWithSourceDescriptions` with typed diagnostics, and threading them through `agents.py::_initialize_agent_instance`.
Verified against the installed agno: all four generated variants in `agno/agent/_default_tools.py` wrap retrieval in `try/except Exception` and return `f"Error searching knowledge base: {type(e).__name__}"` — an explicit, model-visible error string, not `No documents found`.
`No documents found` only occurs when the search *succeeds with empty results*, which is precisely the empty-vector behavior both plans already eliminate by making the embedder raise.
Once raising is in place, single-base query failures are already loud through agno itself; the entire wrapping layer (files `knowledge_source_descriptions.py`, `agent_knowledge_descriptions.py`, `agents.py` in its file map) solves a problem that no longer exists.
The one real residual quiet spot is multi-base merging (see §3), which needs a six-line fix, not a wrapper framework.

**A parallel memory-search API violates repo policy.**
§5 (lines 100-102) keeps `search_agent_memories` returning a bare list "for compatibility" and adds a second diagnostic-returning facade beside it.
`search_agent_memories` has exactly two call sites (`custom_tools/memory.py:106`, `memory/functions.py:204`).
CLAUDE.md says to embrace change and never add a wrapper whose purpose is preserving an old expectation; two parallel search entry points doing the same search with different return types is standing drift risk for zero benefit.

**Its file-memory diagnostic reports index state, not what actually happened in this search.**
§5 (lines 104-107) derives the tool notice by reading the persisted published-index status at tool time.
Two failure modes follow directly:
(a) key rotated after the last successful refresh — the index status is healthy, the query-time 401 is still swallowed by `_file_backend.py`'s generic `except Exception` fallback (lines 912-917), and the tool reports nothing, i.e. the original silent-failure bug survives for the window between rotation and the next refresh;
(b) key fixed but refresh not yet re-run — the stale `last_error` produces a false "semantic unavailable" prefix on a search whose semantic leg actually succeeded.
Threading the actual outcome of the actual search call (my §2e) cannot desynchronize this way.

**The post-refresh-failure probe replaces the real error with an inferred one and costs an extra request.**
§4 (lines 82-85) fires a fresh embedding probe after every failed semantic refresh and overwrites the persisted error with the probe's verdict.
With raising embedders, the per-file exception *is* the real cause — auth, 429, timeout, or anything else — and capturing the first one (my manager change) records ground truth with zero extra network calls and no auth-only bias.
The probe approach also misclassifies transient causes: a refresh that failed on a 429 followed by a probe that happens to succeed persists "retain the original refresh error", but the original error under their own §2 is a per-file exception the plan never captures — the plan leaves `Indexed 0 of N` in place for every non-auth cause.

**Resolution-order step 3 contains dead generality.**
Line 33 resolves the fallback via `get_api_key_for_provider(config.memory.embedder.provider)` "including the existing gemini to google alias behavior."
Both construction paths key only the `openai` provider (`embedding_factory.py:23-33`, `memory/config.py:113-121`), and `create_configured_embedder` raises on any provider other than openai/ollama/sentence_transformers (`embedding_factory.py:48-52`).
The gemini alias can never be exercised by an embedder; writing the resolver generically over providers is speculative surface.

**Process snapshot over-modeling.**
§2 (lines 49-51) keys the snapshot by an embedder signature and adds its own "consecutive probe or request failure count."
There is one process-wide embedder config; a reload can simply reset the snapshot.
The process-local counter duplicates the durable per-index `consecutive_refresh_failures` both plans add, giving two counters with different semantics for one alert.

**Startup sequencing is internally inconsistent.**
§3 says to run the probe "before the runtime is marked ready" (line 69) yet also that it must not prevent bots from starting and that `/api/ready` stays tied to startup readiness (lines 73, 77).
Running it inline before readiness delays boot by the probe timeout whenever the endpoint is down — the exact scenario being handled.
A fire-and-forget task gets the same signal without coupling boot latency to a failing third party.

**`status: degraded` is a third health-status value with monitor-compat risk.**
`/api/health` today emits `status: healthy` or `status: unhealthy` + 503 (`api/main.py:716-725`).
Line 77 introduces `degraded` at HTTP 200; any external monitor matching `status == "healthy"` now alarms while the HTTP code says fine.
An additive `embedder` block with `status` left untouched (my plan) conveys the same information without the mixed signal.

**Scope creep: frontend and API surface.**
Lines 93-95 and the file map add `knowledge/status.py`, `api/knowledge.py`, `Knowledge.tsx` + test, and `MemoryConfig.tsx` + test.
The issue asks for auth separation and loud failure surfacing; the ERROR log at threshold, the persisted `last_error`, the availability notice, and the tool text already surface it to both operator and model.
Dashboard rendering of a counter is a nice-to-have that doubles the touched-file count.

**Minor: misplaced doctor tests and an extra leaf module.**
Line 184 puts doctor-resolution tests in `tests/test_cli_config.py`; `tests/test_cli_doctor.py` exists and is the natural home.
A new `embedder_credentials.py` module (plus tach entry) for one ~30-line resolver is unnecessary when `credentials_sync.py` already owns `get_api_key_for_provider` and the established non-provider-service pattern (`_sync_github_private_credentials`).

## 3. Attack on my own plan (what PLAN-OTHER exposed)

**Stale failure state after recovery — a real bug in my design.**
My plan records health at startup and in the refresh-scheduler *failure* branch only (§2c).
Fix the credential, next refresh succeeds → no probe fires, nothing calls `record_embedder_health(None)`, and `/api/health` reports "failing" until the next restart or the next *failure*.
PLAN-OTHER's passive success-recording in the embedder closes this for free.

**No hot-reload handling.**
My startup probe runs once in `_start_runtime`; a hot reload that changes `memory.embedder` neither re-probes nor clears the old verdict.

**Misleading fix-it advice for non-auth failures.**
My §2f message hard-codes "Fix the embedder credential (…), then retry" for every empty-result degraded search, even when `semantic_error` describes a timeout or 5xx.
The advice sentence must be conditional on `is_embedder_auth_error`.

**Multi-KB query-time failure is still silent in my plan.**
I claimed raising makes knowledge-query auth errors a visible tool error (§2a) and that `_MultiKnowledgeVectorDb`'s per-DB catch plus the availability notice is sufficient.
Verified: a single base bypasses the wrapper (`_merge_knowledge` early-returns at `knowledge/utils.py:672`) and does surface through agno's error string, but with multiple bases the per-DB `except Exception` (`utils.py:605`, `utils.py:634`) swallows *every* failure, the merge returns `[]`, and agno formats `No documents found`.
If the key rotates while indexes are healthy (no persisted `last_error` yet), a multi-KB agent gets exactly the old silent behavior.
PLAN-OTHER's all-down/partial-down distinction (§6, line 129) identified the right requirement even though its mechanism is wrong.

**Missing cross-path and leak tests.**
I never assert that the mem0 path and the agno path resolve the identical key for identical inputs, nor that keys stay out of signatures, metadata, and health payloads.

**No recovery-path validation.**
My live validation is "doctor does a real POST"; I have nothing that exercises degrade-then-recover.

## 4. Synthesis — the plan I would ship

Base is my PLAN.md; graft four things from PLAN-OTHER and add one fix neither plan had right; drop everything speculative from both.

**Part 1 — credential (mine, unchanged).**
Resolver `get_embedder_api_key` in `credentials_sync.py` (no new leaf module): explicit `EmbedderConfig.api_key` → `embedder` credential service seeded from `EMBEDDER_API_KEY` (github-sync pattern) → `openai` provider key → `None` (keyless local endpoints keep working).
Fallback stays hard-coded to `openai` — the only keyed embedder provider in both construction paths; no provider-generic resolver.
Wire into `embedding_factory.py` and `memory/config.py`; no reindex (signatures exclude the key).

**Part 2 — raising embedders plus passive health (merged).**
All `MindRoomOpenAIEmbedder` sync/async/batch methods raise instead of returning `[]` (both plans agree).
Adopt PLAN-OTHER's hook: record failure before re-raise, record healthy on any non-empty vector — two lines per path into `embedder_health.py`.
Health state stays my simple lock-guarded `str | None`; no signature keying, no process-local counter; config reload resets it and re-triggers the startup probe when `memory.embedder` changed (adopting PLAN-OTHER's reload handling).
Startup probe stays fire-and-forget (my sequencing), never blocking readiness.
Keep the refresh-scheduler failure-branch probe: subprocess refreshes don't touch the main-process embedder, so passive recording alone can miss rotation when there is no query traffic.
`/api/health` gains an additive `embedder` block; `status` values unchanged, HTTP 200 preserved.

**Refresh error detail (mine, not the probe-overwrite).**
Capture the first per-file exception via `describe_embedder_error` in `manager.py` and compose it into `_last_refresh_error`; no extra embedding request, correct for auth and non-auth causes alike.
Durable `consecutive_failures` on `PublishedIndexState` with the threshold-3 ERROR log at the single registry choke point (both plans agree on substance).
Skip the status-API and frontend exposure — log plus persisted `last_error` plus notices are the loud surface this issue needs.

**Memory seam (mine, amended).**
`MemorySearchOutcome` replaces the return type of the backend seam and `search_agent_memories`; both call sites updated, no parallel facade.
Degradation is taken from the actual search call's outcome, never from re-read index status.
Tool text amendment per PLAN-OTHER's category insight: the "Fix the embedder credential (config api_key, the 'embedder' credential, or EMBEDDER_API_KEY)" sentence appears only when `is_embedder_auth_error`; other causes get "Semantic memory search unavailable: {detail}" with no credential advice.

**Knowledge query loudness (new minimal fix, replacing PLAN-OTHER §6).**
No wrapping of agno-generated tools — agno already returns `Error searching knowledge base: {type}` on raise.
Instead, fix the one verified quiet spot: `_MultiKnowledgeVectorDb.search`/`async_search` re-raise the first captured exception when *all* per-DB searches failed with exceptions; partial failures keep the current warn-and-merge behavior, which the availability notice (my §2d, carrying `last_error`) already contextualizes.
About six lines plus tests.

**Doctor.**
Resolve via `get_embedder_api_key` and reuse `probe_embedder` for the live check, retiring the duplicate `_validate_openai_embeddings_endpoint` logic if the swap is a strict simplification (PLAN-OTHER's DRY point); tests in `tests/test_cli_doctor.py`.

**Tests.**
My test plan, plus from PLAN-OTHER: same-resolved-key assertion across both construction paths; no-credential-leak assertions over signatures, metadata, logs, and health payloads; a degrade-then-recover case (fail recorded → passive success clears it); multi-KB all-down re-raise; non-auth degraded tool text contains no credential advice.
Manual smoke: PLAN-OTHER's disposable-401-server recovery walkthrough, run once before merge.

**Dropped from both plans.**
PLAN-OTHER: tool wrapping and diagnostic handles (§6), parallel memory facade, post-refresh probe overwrite, signature-keyed snapshot and second counter, `status: degraded`, frontend and knowledge-API changes, `embedder_credentials.py` module.
Mine: unconditional credential-fix advice, the assumption that startup-plus-failure-probes alone keep health state fresh.
