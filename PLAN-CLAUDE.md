# ISSUE-237 — Embedder auth separation + loud failure surfacing

Implementation plan. No code changes in this commit.

## Root-cause summary (verified against the code)

The embedder key is the shared `openai` provider key in both embedding paths:

- `src/mindroom/embedding_factory.py:30` — `create_configured_embedder` calls `get_api_key_for_provider("openai", ...)` for the agno embedder used by knowledge indexes and semantic file-memory.
- `src/mindroom/memory/config.py:114` — `_get_memory_config` does the same for the mem0 embedder.
- `EmbedderConfig.api_key` (`src/mindroom/config/models.py:497`) exists in the schema but is never read anywhere — an explicit embedder key in `config.yaml` is silently ignored today.

The silence has four distinct mechanisms:

1. **Query-time swallow:** agno's `OpenAIEmbedder.get_embedding` (and every async variant, including our overrides in `src/mindroom/openai_embedder.py:59-110`) catches all exceptions and returns `[]`. A 401 becomes an empty query vector, which becomes empty search results, which becomes "No relevant memories found."
2. **Refresh-time genericization:** `KnowledgeManager._index_file_locked` (`src/mindroom/knowledge/manager.py:1042`) catches per-file indexing exceptions and returns `False`; `reindex_all` then records only `"Indexed 0 of N managed knowledge files"` as `_last_refresh_error`. The persisted `last_error` never contains the underlying 401, and nothing counts or escalates consecutive failures.
3. **Silent keyword fallback:** file-backed semantic memory search (`src/mindroom/memory/_file_backend.py:906-917`) catches `SemanticFileMemoryIndexUnavailableError` at `logger.debug` and any other exception at `logger.exception`, then silently falls back to keyword search. The tool output never distinguishes "semantic is broken" from "nothing matched."
4. **Notices lack the cause:** the existing per-turn knowledge availability notice (`format_knowledge_availability_notice`, `src/mindroom/knowledge/utils.py:488`) says "had a recent refresh failure" but never includes the persisted `last_error`, and `mindroom doctor` validates the embedder with the env var (`src/mindroom/cli/doctor.py:701`), not the credentials store the runtime actually uses.

## Part 1 — Dedicated embedder credential

### Resolution order

For the `openai`-provider embedder (the only embedder provider that uses an API key), resolve in this order:

1. `memory.embedder.config.api_key` — explicit key authored in `config.yaml` (field already exists; make it live).
2. Dedicated `embedder` shared credential service — `credentials/embedder_credentials.json`, field `api_key`, seedable from the `EMBEDDER_API_KEY` env var (or `EMBEDDER_API_KEY_FILE`).
3. Shared `openai` provider credential — today's behavior, kept as the backward-compat fallback.

`ollama` and `sentence_transformers` embedders are keyless and unchanged.

### Changes

**`src/mindroom/credentials_sync.py`**
- Add `EMBEDDER_CREDENTIAL_SERVICE = "embedder"`.
- Add `get_embedder_api_key(runtime_paths, *, explicit_api_key: str | None = None) -> str | None` implementing the order above (explicit → `embedder` service via `CredentialsManager.get_api_key` → `get_api_key_for_provider("openai")`). Taking the authored key as a plain argument avoids importing config types into this low-level module.
- Add `_sync_embedder_credentials(runtime_paths)` mirroring `_sync_github_private_credentials` (`credentials_sync.py:59`): seed/update the `embedder` service from `EMBEDDER_API_KEY` via `get_secret_from_env`, respecting the existing `_source=env` vs `_source=ui` rules. Call it from `sync_env_to_credentials`.
- Deliberately do **not** add `embedder` to `PROVIDER_ENV_KEYS` (`src/mindroom/constants.py:1064`): that map means "model provider", is consumed by `env_key_for_provider` and model loading, and `embedder` is not a model provider. The explicit sync function is the established pattern for non-provider services (GITHUB_TOKEN).

**`src/mindroom/embedding_factory.py`**
- In the `openai` branch, replace `get_api_key_for_provider("openai", ...)` with `get_embedder_api_key(runtime_paths, explicit_api_key=embedder_config.api_key)`.

**`src/mindroom/memory/config.py`**
- In `_get_memory_config`'s `openai` embedder branch (line 114), use the same `get_embedder_api_key(..., explicit_api_key=app_config.memory.embedder.config.api_key)`. The memory **LLM** key resolution (line 149) is intentionally untouched — it is a model provider, not the embedder.

**`src/mindroom/config/models.py`**
- Update the `EmbedderConfig.api_key` description to state it is the highest-priority embedder key, above the `embedder` credential and the `openai` provider fallback. No schema shape changes.

**`src/mindroom/cli/doctor.py`**
- `_check_memory_embedder` currently reads the key from env only; change it to resolve via `get_embedder_api_key` (passing the authored `emb.config.api_key`) so doctor validates exactly the key the runtime will use. Keep the existing live `/embeddings` POST check (`_validate_openai_embeddings_endpoint`); adjust the "key not set" warning text to name the three sources.

**Docs (`docs/memory.md`, `docs/knowledge.md`)**
- Document the resolution order and `EMBEDDER_API_KEY`. One short subsection each.

No API/dashboard changes are needed: the credentials API and `CredentialsManager` handle arbitrary validated service names already, so `embedder` is manageable through the existing surfaces.

### Note on reindexing

The embedder signature that keys collections (`effective_knowledge_embedder_signature` / `effective_mem0_embedder_signature` in `src/mindroom/embeddings.py`) covers provider/model/host/dimensions and not the key, so changing where the key comes from forces no reindex. Correct: identical vectors regardless of auth source.

## Part 2 — Loud failure surfacing

### 2a. Stop swallowing embedding errors (`src/mindroom/openai_embedder.py`)

- Override sync `get_embedding` and `get_embedding_and_usage` on `MindRoomOpenAIEmbedder` to call `self.response(...)` and let exceptions propagate (agno's base swallows and returns `[]`).
- Change our async overrides (`async_get_embedding`, `async_get_embedding_and_usage`, `async_get_embeddings_batch_and_usage`) to re-raise instead of returning `[]`/logging warnings. The per-item batch fallback loop goes away; a failing batch fails the file, which the indexing path already handles per-file.
- Effect on indexing: a 401 now surfaces as a per-file exception → `_index_file_locked` marks the file failed → `reindex_all` reports a real error (see 2c) → `REFRESH_FAILED` with the actual cause persisted.
- Effect on query: `ChromaDb.search` raises → propagates to the seams handled in 2e/2f. For agno's built-in `search_knowledge_base` agent tool, the raised error becomes a visible tool error instead of fake-empty results — which is the loud behavior we want. `_MultiKnowledgeVectorDb` (`knowledge/utils.py:593`) still catches per-DB so one broken base cannot kill a multi-base merge; its warning log is sufficient there because the per-turn availability notice (2d) carries the cause.

### 2b. New module `src/mindroom/embedder_health.py`

Small functional module (no classes beyond one frozen dataclass), registered in `tach.toml`:

- `describe_embedder_error(exc: BaseException) -> str` — compact human description; classify auth failures via `from openai import AuthenticationError, PermissionDeniedError  # noqa: PLC0415` (function-level import keeps the SDK out of import time; if the exception came from an embedding call, the SDK is already loaded). Auth errors render as e.g. `embedder auth failed (HTTP 401) for gemini-embedding-2 at https://litellm.lab.nijho.lt/v1`; anything else falls back to `type(exc).__name__: str(exc)` run through `redact_credentials_in_text`.
- `is_embedder_auth_error(exc) -> bool` — same classification, used by callers that only branch.
- `embedder_in_use(config) -> bool` — true when `memory.embedder.provider == "openai"` AND (memory backend is `mem0`, OR memory backend is `file` with `memory.search.mode == "semantic"` at the default or any per-agent override, OR any knowledge base has `mode == "semantic"`).
- `probe_embedder(config, runtime_paths) -> str | None` — build the embedder via `create_configured_embedder`, call `get_embedding("mindroom embedder health check")`, return `None` on a non-empty vector, else the `describe_embedder_error` string. Sync; callers wrap in `asyncio.to_thread`.
- Module-level last-known state (`record_embedder_health(error: str | None)` / `get_embedder_failure() -> str | None`, guarded by a `threading.Lock`, same pattern as `runtime_state.py`). This is main-process, in-memory state for the health endpoint; durable per-index failure state stays in the published index metadata (2c).

### 2c. Startup probe + consecutive refresh-failure escalation

**Startup (`src/mindroom/orchestrator.py`)**
- In `_start_runtime`, after config is loaded and bots are starting, spawn one fire-and-forget task: `asyncio.create_task(check_embedder_health_at_startup(config, self.runtime_paths))` where the coroutine lives in `embedder_health.py` (composition-root rule: orchestrator only wires). It no-ops unless `embedder_in_use(config)`; runs `probe_embedder` in a thread; on failure logs `logger.error("embedder_health_check_failed", error=...)` and calls `record_embedder_health(error)`; on success records healthy. It must never block or fail startup.

**`/api/health` (`src/mindroom/api/main.py:706`)**
- When `get_embedder_failure()` is set, add `"embedder": {"status": "failing", "detail": <error>}` to the response. Do **not** flip the overall status to 503 — a broken embedder degrades memory search but must not make liveness probes restart the pod.

**Consecutive-failure tracking (`src/mindroom/knowledge/registry.py`)**
- Add `consecutive_failures: int = 0` to `PublishedIndexState` (`registry.py:79`).
- `mark_published_index_refresh_failed_preserving_last_good` (`registry.py:401`): set `consecutive_failures = (current.consecutive_failures if current else 0) + 1`; when the new count reaches `_CONSECUTIVE_REFRESH_FAILURE_ALERT_THRESHOLD = 3`, log at `logger.error("knowledge_refresh_failing_repeatedly", base_id=..., consecutive_failures=..., last_error=...)`. This is the single choke point used by in-process refreshes, the refresh subprocess, and failed-subprocess reconciliation, and it is file-backed so it survives restarts and works across the subprocess boundary.
- `mark_published_index_refresh_succeeded` and the fresh-complete state writes (`_publish_file_mode_source_metadata`, candidate publish): reset to 0 (dataclass default covers fresh constructions; add explicit `consecutive_failures=0` in the succeeded `replace`).
- Persistence: `save_published_index_state` passes it through `write_index_metadata_payload` (already generic kwargs); `load_published_index_state` parses it with the nonnegative-int coercion in `index_metadata.py` (expose `_coerce_nonnegative_metadata_int` as `coerce_nonnegative_metadata_int` or add a tiny local parse). Missing key on old files → default 0.
- `_published_state_fingerprint` in `refresh_runner.py:864` lists fields explicitly; add the new field so both snapshots stay consistent.

**Refresh error detail (`src/mindroom/knowledge/manager.py`)**
- `_index_file_locked`: in the `except` branch (line 1042), record the first failure's `describe_embedder_error(exc)` into a new `self._last_file_index_error: str | None` (first-wins, cleared at the start of `reindex_all`).
- `reindex_all`: when `indexed_count != len(files)`, compose `_last_refresh_error` as `"Indexed {n} of {m} managed knowledge files (first error: {detail})"` when a detail was captured. The existing plumbing (`refresh_runner.py:530-539` → `mark_published_index_refresh_failed_preserving_last_good`) then persists the real cause into `last_error`, already credential-redacted.

**On-refresh-failure probe (`src/mindroom/knowledge/refresh_scheduler.py`)**
- In `_handle_done`'s failure branch (`refresh_scheduler.py:196`), also schedule `probe_embedder` via `asyncio.to_thread` (guarded by `embedder_in_use`) and `record_embedder_health(...)` with the result, so the health endpoint reflects reality after background refresh failures without waiting for the next startup.

### 2d. Availability notices carry the real error (`src/mindroom/knowledge/utils.py`)

- Add `last_error: str | None = None` to `KnowledgeAvailabilityDetail`; fill it from `lookup.state.last_error` in `resolve_agent_knowledge_access`.
- Add `last_error: str | None = None` to `KnowledgeBaseAccessResolution` (used by semantic file-memory, 2f), filled the same way in `resolve_knowledge_base_access`.
- In `format_knowledge_availability_notice`, append `` Last refresh error: {last_error}. `` to both `REFRESH_FAILED` branches when present. This flows automatically into agent system enrichment via the existing `append_knowledge_availability_enrichment` call sites (response_runner, teams, openai_compat, delegate, call_tools) — no changes needed there.

### 2e. Memory search outcome seam

Change the backend `search` seam to return a typed outcome instead of a bare list, so degradation is explicit instead of implied by emptiness:

**`src/mindroom/memory/_shared.py`**
- Add `@dataclass(frozen=True) MemorySearchOutcome: results: list[MemoryResult]; semantic_error: str | None = None`.

**`src/mindroom/memory/_backend.py`**
- `ResolvedMemoryBackend.search` returns `MemorySearchOutcome`.

**`src/mindroom/memory/_semantic_file_search.py`**
- Give `SemanticFileMemoryIndexUnavailableError` two attributes: `availability: KnowledgeAvailability` and `last_error: str | None`, populated from the (extended) `KnowledgeBaseAccessResolution` when raising at line 232.

**`src/mindroom/memory/_file_backend.py` (`search`, lines 893-919)**
- `SemanticFileMemoryIndexUnavailableError` with `availability is REFRESH_FAILED` → keyword fallback still runs, and the outcome gets `semantic_error = f"semantic index refresh is failing ({exc.last_error or 'unknown error'})"`. `INITIALIZING`/`STALE`/`CONFIG_MISMATCH` remain a silent fallback (normal transient states), but bump the fallback log from `debug` to `info` with the availability value.
- Generic exception from the semantic query → keyword fallback still runs; `semantic_error = describe_embedder_error(exc)`; keep the `logger.exception`.
- Keyword-mode and merged-team paths return `MemorySearchOutcome(results)` with no error.

**`src/mindroom/memory/_mem0_backend.py` (`search`)**
- Wrap the mem0 search calls: on an exception classified by `is_embedder_auth_error` (or any openai `APIStatusError`), return `MemorySearchOutcome([], semantic_error=describe_embedder_error(exc))` — mem0 has no keyword fallback, so results are empty but the cause is explicit. Other exceptions keep propagating unchanged.
- Verify during implementation that mem0's `AsyncMemory.search` actually propagates embedder HTTP errors (read the installed mem0 source); if mem0 swallows internally, fall back to relying on the startup/refresh probes for the mem0 notice and say so in the PR.

**`src/mindroom/memory/functions.py`**
- `search_agent_memories` returns `MemorySearchOutcome` (internal API, no compat shim — repo policy).
- `build_memory_prompt_parts` (line 204) uses `.results`; when `.semantic_error` is set, log one `logger.warning("memory_semantic_search_degraded", ...)` and continue — prompt assembly must degrade, never crash the turn.

### 2f. Tool output (`src/mindroom/custom_tools/memory.py::search_memories`)

- `semantic_error` set and results empty → return `f"Semantic memory search unavailable: {outcome.semantic_error}. Keyword fallback found no matches. Fix the embedder credential (config api_key, the 'embedder' credential, or EMBEDDER_API_KEY), then retry."` (mem0 variant omits the keyword sentence). Never the plain "No relevant memories found." when semantic is known-broken.
- `semantic_error` set with results → prepend `f"Note: semantic search unavailable ({outcome.semantic_error}); showing keyword-only matches."` before the numbered list.
- No `semantic_error` → behavior unchanged.
- Keep the existing `except` branch, but when `is_embedder_auth_error(e)` classify the message as `f"Semantic memory search unavailable: {describe_embedder_error(e)}"` instead of the generic `Failed to search memories: {e}`.

### tach.toml

- Add a `mindroom.embedder_health` module (depends on `mindroom.embedding_factory`, `mindroom.knowledge.redaction` or wherever `redact_credentials_in_text` is importable from, `mindroom.logging_config`, config).
- Add new edges where consumers gained imports (`mindroom.memory.*` → `mindroom.embedder_health`, `mindroom.custom_tools.memory` → `mindroom.embedder_health`, orchestrator, api, refresh scheduler, doctor).
- Run `uv run tach check --dependencies --interfaces` and adjust in the same PR per repo policy.
- `tests/test_import_graph.py`: the new module must not pull the openai SDK at import time (classification/probe use function-level imports), so no allowlist change should be needed; verify.

## Test plan (all fake, no live embedder)

Fakes: inject stub clients via the existing `OpenAIEmbedder.openai_client`/`async_client` dataclass fields, or monkeypatch `create_configured_embedder`; build a fake `openai.AuthenticationError` (real class, constructed with a stub response) for classification tests. Credentials tests use a tmp storage root with real JSON files, as `tests/test_credentials_sync.py` already does.

- `tests/test_credentials_sync.py`: `get_embedder_api_key` order — explicit wins; `embedder` credential beats `openai`; falls back to `openai`; returns None when nothing exists. `EMBEDDER_API_KEY` seeding: creates `embedder` service with `_source=env`, respects `_source=ui`, supports `_FILE`.
- `tests/test_embeddings.py` (or a new `tests/test_embedding_factory.py`): factory passes the resolved key for the openai provider given each credential layout; ollama/sentence_transformers untouched.
- `tests/test_memory_config.py`: mem0 embedder config picks the dedicated key when present, provider key otherwise; explicit config key wins.
- New `tests/test_openai_embedder.py` (or extend existing embedder tests): `get_embedding` raises on client failure (no `[]`), async variants raise, batch raises; success paths still return vectors and respect the dimensions logic.
- New `tests/test_embedder_health.py`: `describe_embedder_error` classifies 401/403 vs generic; output is credential-redacted; `embedder_in_use` truth table over memory backend/mode/knowledge-base permutations; `probe_embedder` healthy vs failing via monkeypatched factory; record/get state round-trip.
- Knowledge (`tests/test_knowledge_index_metadata.py`, manager/registry tests): `consecutive_failures` increments across repeated `mark_..._refresh_failed...` calls, resets on success, round-trips through save/load, defaults to 0 for legacy metadata files; ERROR log emitted at threshold (caplog); `reindex_all` failure includes the first per-file error detail in `_last_refresh_error`.
- `tests/test_bot_knowledge.py` / utils tests: `format_knowledge_availability_notice` includes `Last refresh error: ...` for REFRESH_FAILED with detail; unchanged otherwise; `KnowledgeBaseAccessResolution.last_error` populated.
- `tests/test_memory_backend_contract.py`, `test_memory_file_backend.py`, `test_memory_mem0_backend.py`: `search` returns `MemorySearchOutcome`; file backend sets `semantic_error` on REFRESH_FAILED index and on query-time auth error while still returning keyword results; transient availabilities stay silent; mem0 backend maps auth errors to an outcome instead of raising.
- `tests/test_memory_tools.py`: exact tool strings for (broken + no keyword hits), (broken + keyword hits), healthy-empty, healthy-hits.
- `tests/test_cli_doctor.py`: embedder check resolves the key from the credentials store (not env-only).
- `/api/health`: response contains the `embedder` block when a failure is recorded and omits it when healthy.

Live validation stays manual: `mindroom doctor` performs a real `/embeddings` POST with the resolved key.

## Backward compatibility

- Existing configs keep working unchanged: with no explicit key and no `embedder` credential, resolution lands on the `openai` provider key exactly as today.
- `EmbedderConfig.api_key` switches from silently ignored to honored — an intentional behavior change; a config carrying a stale value there would start using it. Called out in the PR description.
- Published index metadata: `consecutive_failures` is optional on load (legacy files default to 0); older readers ignore the unknown JSON key.
- `search_agent_memories`' return-type change is internal; both call sites (`memory/functions.py:204`, `custom_tools/memory.py:106`) are updated in the same PR. No compat wrapper, per repo policy.
- No collection names, embedder signatures, or Chroma layouts change — no reindexing is triggered.
- `/api/health` gains an additive field; status-code semantics unchanged.

## Risks

- **Raising embedder errors changes flow in every consumer of `MindRoomOpenAIEmbedder`.** Audited consumers: knowledge indexing (per-file exception handling already in place), knowledge query via agno tool (error becomes a visible tool error — desired), multi-KB merge (per-DB catch retained), semantic file memory (mapped to outcome). Watch for any new agno upgrade re-introducing swallows; the existing "keep aligned with agno" note in `openai_embedder.py` covers this.
- **mem0 internals unverified**: if mem0 swallows embedder errors internally, the mem0-path notice weakens to probe-based surfacing only; verify while implementing.
- **Turn breakage risk in prompt assembly** is closed by the outcome mapping in `build_memory_prompt_parts`; non-embedder exceptions behave as today.
- **Fingerprint drift**: `_published_state_fingerprint` must include the new field on both comparison sides; it does because both snapshots are produced by the same code in one process generation.
- Health endpoint intentionally stays 200 on embedder failure to avoid liveness-probe restart loops.

## Non-goals

- No per-host credential map (`litellm.lab.nijho.lt`-keyed credentials); one `embedder` service is sufficient and simpler.
- No new alerting transports (Matrix DM, webhook, email); "alerting" is the threshold ERROR log plus the user-visible tool/notice text and the health endpoint.
- No probes for `ollama`/`sentence_transformers` embedders (keyless; different failure modes).
- No changes to refresh scheduling/backoff behavior.
- No ops work — the credentials file already holds the working LiteLLM master key.
- No dashboard/UI changes; the generic credentials API already manages the `embedder` service.
