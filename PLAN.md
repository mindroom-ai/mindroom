# ISSUE-237 — Final Plan (synthesized from dual-planner debate)

**Issue:** Embedder auth rides on the shared `openai` provider credential; silent 401 froze semantic memory for 5+ weeks (P1).
**Inputs:** `PLAN-CODEX.md`, `PLAN-CLAUDE.md`, `CRITIQUE-CODEX.md`, `CRITIQUE-CLAUDE.md` (committed alongside for provenance; strip before merge).
**Synthesis rule applied:** both critiques converged on ~85% of substance. Where they disagreed, the minimal, *verified* option wins (Bas style: no over-engineering, no parallel facades, no speculative wrapping).

## Part 1 — Dedicated embedder credential (both plans agree)

- New resolver `get_embedder_api_key` in `credentials_sync.py` (no new leaf module).
- Resolution order:
  1. explicit `EmbedderConfig.api_key` in config,
  2. dedicated `embedder` credential service (seeded from `EMBEDDER_API_KEY` env / `_FILE`, following the existing github-sync seeding pattern),
  3. fallback to the `openai` provider key (backward compat — existing configs keep working unchanged),
  4. `None` (keyless local endpoints keep working).
- Fallback stays hard-coded to `openai` — the only keyed embedder provider in both construction paths today; no provider-generic resolver.
- Wire into BOTH construction paths: `embedding_factory.py` and `memory/config.py`. Assert same resolved key across both in tests.
- No reindex triggered: embedding signatures exclude the key.

## Part 2 — Raising embedders + passive health state

- All `MindRoomOpenAIEmbedder` sync/async/batch methods **raise** instead of returning `[]` (both plans agree — the silent empty-vector return is the root enabler of the 5-week freeze).
- Small `embedder_health.py`: lock-guarded `str | None` current-failure value; record failure before re-raise, record healthy on any non-empty vector (two lines per path). No signature keying, no counters, no per-provider probes.
- Structured 401/403/transport classification; fixed tool-facing messages; operator detail redacted (no raw bodies, no hosts, no keys).
- Startup probe: fire-and-forget after credential sync, never blocks readiness. Config reload of `memory.embedder` resets health and re-probes.
- Refresh-scheduler failure branch keeps a strict one-vector probe (subprocess refreshes don't touch the main-process embedder; passive recording alone can miss key rotation when there's no query traffic). Probe result may only *canonicalize* a confirmed auth failure — it never overwrites a distinct original error (reader/Git/chunking/publication errors are preserved verbatim).
- `/api/health` gains an additive redacted `embedder` block; status values and HTTP codes unchanged.

## Part 3 — Refresh failure durability + escalation

- Capture the FIRST real per-file indexing exception in `KnowledgeManager` (`describe_embedder_error`), redact, persist into `_last_refresh_error`.
- Add optional `consecutive_refresh_failures: int` to `PublishedIndexState`: increment in `mark_published_index_refresh_failed_preserving_last_good`, reset on every success/publish, missing legacy value parses as 0, included in `_published_state_fingerprint`, ERROR-level log at 3 and every later failure.
- Last-good collection + indexed metadata always preserved; existing refresh locks guard the read-modify-write.
- No status-API or frontend exposure of the counter (logs + persisted `last_error` + notices are the loud surface this issue needs).

## Part 4 — User-visible degradation in search tools

- `MemorySearchOutcome` replaces the return type at the backend seam AND `search_agent_memories`; both call sites updated. **No parallel facade** (resolved against CRITIQUE-CODEX's unwrap-facade: two call sites, just update them).
- Degradation is taken from the actual search call's outcome, never from re-read index status.
- Healthy-empty output unchanged.
- Confirmed auth failure, no fallback matches → exactly: `Semantic search unavailable: embedder authentication failed (HTTP 401).`
- Keyword-fallback matches exist → same failure line + `Showing keyword matches only.` prefixed to the results.
- Credential-fix advice ("config api_key / 'embedder' credential / EMBEDDER_API_KEY") appears ONLY when `is_embedder_auth_error`; other causes get `Semantic memory search unavailable: {safe detail}` with no credential advice.
- Mem0 backend: classify propagated provider errors; consult current health when Mem0 swallows one and returns empty.

## Part 5 — Knowledge query loudness (minimal, verified)

- **No wrapping of agno-generated tools** (resolved against CRITIQUE-CODEX §12: CRITIQUE-CLAUDE verified agno already surfaces raised errors as `Error searching knowledge base: {type}` once embedders raise).
- Fix the one verified quiet spot: `_MultiKnowledgeVectorDb.search`/`async_search` re-raise the first captured exception when ALL per-DB searches failed; partial failures keep warn-and-merge (availability notice carries `last_error` context).
- Add `last_error` to knowledge availability detail; append safe classified cause in `format_knowledge_availability_notice`.

## Part 6 — Doctor

- `cli doctor` resolves via `get_embedder_api_key` and reuses the probe; retire duplicate `_validate_openai_embeddings_endpoint` logic if strictly a simplification.

## Tests

- Credential precedence (explicit > embedder credential > openai fallback > None), blank values, env/`_FILE` seeding, same-key-across-both-paths.
- Embedder raise-not-empty on all sync/async/batch paths; 401/403/transport classification; redaction (no key/host leaks in signatures, metadata, logs, health payloads).
- Health: record-on-failure, clear-on-success (degrade-then-recover), startup probe, reload reset.
- Refresh: first-error preservation, auth canonicalization only when probe confirms, counter across reloads, reset on success, threshold logging, legacy metadata, last-good preservation, fingerprint inclusion.
- Memory tool: healthy empty, auth-fail no results, auth-fail + keyword matches, non-auth degraded text has no credential advice, Mem0 swallow/raise.
- Knowledge: multi-KB all-down re-raise, partial-failure merge, availability notice with last_error.
- Fakes throughout — no live provider needed. Manual smoke before merge: disposable-401-server degrade→recover walkthrough.
- Gate: focused tests while implementing → `uv run tach check --dependencies --interfaces` → full `uv run pytest` → `uv run pre-commit run --all-files`.

## Explicitly dropped (from both plans)

- Agno tool wrapping / diagnostic handles; parallel memory facade; post-refresh probe overwriting distinct errors; signature-keyed health snapshots; second failure counter; `status: degraded` in /api/health; frontend/knowledge-API counter exposure; new `embedder_credentials.py` leaf module; provider-generic resolver; dummy vector DBs; unconditional credential advice in tool text.

## Non-goals

- Ops work (LiteLLM master-key rotation, scoped virtual key) — separate ops task.
- Alerting/notification channels beyond ERROR logs and tool-text surfacing.
