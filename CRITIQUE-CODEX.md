# ISSUE-237 plan critique

## 1. Steelman of `PLAN-OTHER.md`

The other plan is better grounded in the existing credential infrastructure.
Its choice to put `get_embedder_api_key` and `EMBEDDER_API_KEY` synchronization in `credentials_sync.py` follows the established non-provider credential seeding pattern instead of adding the separate `embedder_credentials.py` module proposed in my plan (`PLAN-OTHER.md`, lines 34-38).
It also correctly keeps `embedder` out of `PROVIDER_ENV_KEYS`, whose meaning and downstream consumers are provider-specific (`PLAN-OTHER.md`, line 38).

Its credential order is precise and backward-compatible: authored `memory.embedder.config.api_key`, then the `embedder` credential service, then the existing `openai` credential (`PLAN-OTHER.md`, lines 22-30).
Restricting this logic to the keyed `openai` embedder branch is more accurate than my generic reference to `config.memory.embedder.provider`, because the implemented alternatives are keyless Ollama and sentence-transformers paths.
The explicit warning not to change the Mem0 LLM credential resolution is a useful guard against conflating two distinct clients (`PLAN-OTHER.md`, lines 43-44).
Updating the existing `EmbedderConfig.api_key` description is a small but important schema-documentation fix that my plan missed (`PLAN-OTHER.md`, lines 46-47).

The other plan is also more disciplined about scope in several places.
It reuses the current doctor embeddings request, existing credentials UI, existing knowledge availability enrichment, and existing published-index metadata instead of proposing new UI concepts (`PLAN-OTHER.md`, lines 49-55 and 102-106).
Its explicit `_published_state_fingerprint` update catches a concrete persistence/reconciliation detail that my plan mentioned only indirectly (`PLAN-OTHER.md`, line 93).
Its decision to capture the first indexing exception near `_index_file_locked` can preserve the originating failure more cheaply than probing merely to rediscover a cause already in hand (`PLAN-OTHER.md`, lines 95-97).

The typed `MemorySearchOutcome` is placed at the backend search seam where semantic fallback actually occurs (`PLAN-OTHER.md`, lines 108-132).
That is cleaner than my plan's separate file-memory status lookup after performing a list-returning search, because the backend already knows whether it used keyword fallback.
The proposed distinction between expected transient index states and a durable `REFRESH_FAILED` state is thoughtful and avoids alarming on normal first-index initialization (`PLAN-OTHER.md`, lines 118-124).

The test plan is more implementation-ready than mine in a few areas.
It names the real client injection fields, the existing credentials-sync test pattern, `_FILE` handling, UI-versus-env source precedence, the persisted-state fingerprint, and the exact backend contract tests (`PLAN-OTHER.md`, lines 148-164).
It also explicitly calls for reading the installed Mem0 source before relying on its exception behavior, which is the right instinct even though the proposed fallback is incomplete (`PLAN-OTHER.md`, lines 126-128).

Finally, the other plan avoids several speculative additions from mine.
It does not add dashboard rendering for the failure counter, multiple health signatures, a broad health taxonomy UI, or a diagnostic `Knowledge` subclass.
That narrower posture better matches the request to solve two concrete bugs with the smallest correct change.

## 2. Problems in `PLAN-OTHER.md`

### Knowledge query failures are still swallowed

The central claim in section 2a is false for the installed Agno version.
`PLAN-OTHER.md` says a raised Chroma query error reaches the generated `search_knowledge_base` tool as a visible error (`PLAN-OTHER.md`, lines 67-68 and 177).
In fact, `agno.knowledge.knowledge.Knowledge.search` catches every exception from `vector_db.search` and returns `[]`, after which Agno's generated tool returns `No documents found`.
For merged knowledge, MindRoom's `_MultiKnowledgeVectorDb.search` catches each database exception even earlier and returns the merged results from the remaining databases, or an empty list when all fail.
Re-raising from `MindRoomOpenAIEmbedder` is necessary for indexing correctness, but it is not sufficient to make knowledge tool output loud.
The plan therefore does not satisfy the requested knowledge-search failure surfacing.

The same swallow breaks part of the proposed file-memory design.
Section 2e expects a generic exception from the semantic query to reach `_file_backend.py` and become `semantic_error` (`PLAN-OTHER.md`, lines 121-124).
The semantic file-memory path calls `Knowledge.search`, so an embedding failure can instead arrive as a healthy-looking empty result without entering that `except` block.
Unless the memory outcome also consults a health failure recorded by the embedder request, the tool can still return `No relevant memories found.` during the exact outage in the issue.

### The refresh-failure probe is attached to the wrong signal

Section 2c proposes probing in `KnowledgeRefreshScheduler._handle_done`'s failure branch (`PLAN-OTHER.md`, lines 99-100).
The ordinary failed-refresh path does not raise out of the scheduler task.
`refresh_knowledge_binding` persists `REFRESH_FAILED` and returns a `KnowledgeRefreshResult`, while the refresh subprocess exits successfully and `refresh_knowledge_binding_in_subprocess` returns `None`.
Consequently, `task.result()` succeeds and the proposed exception branch never runs for the common `Indexed 0 of N` failure.

The proposed callback also lacks the inputs its probe needs.
`_handle_done` currently receives only the refresh target and `Task[None]`, not the scheduled request's config and runtime paths.
The plan does not specify the return-type or callback-signature change needed to recover them.

The last-known health state can also remain stale indefinitely.
The startup probe records success or failure, and the proposed refresh callback only discusses recording after a failure (`PLAN-OTHER.md`, lines 78-86 and 99-100).
There is no clearing path after a successful real embedding request, a successful later refresh, credential rotation, or a relevant config reload.
An additive `/api/health` field that continues reporting an old failure after recovery is not a safe health signal.

### The Mem0 fallback does not produce the promised tool message

The plan admits that Mem0 may swallow the embedder error and suggests falling back to startup or refresh probes if so (`PLAN-OTHER.md`, lines 126-128 and 178).
That fallback is not connected to `MemorySearchOutcome` or `search_memories`.
The proposed Mem0 backend only sets `semantic_error` when `AsyncMemory.search` raises, while the tool formatter only reads that outcome (`PLAN-OTHER.md`, lines 126-139).
If Mem0 returns an empty result after swallowing the error, the global probe state is ignored and the tool still says `No relevant memories found.`.

### The public memory facade is changed unnecessarily

Section 2e changes `search_agent_memories` from `list[MemoryResult]` to `MemorySearchOutcome` and dismisses compatibility because it found two production call sites (`PLAN-OTHER.md`, lines 130-132 and 171).
The function is exported from `mindroom.memory.__init__`, is used broadly in tests, and is a natural library facade even if the repository currently has no external consumers.
Changing it is avoidable.
The backend seam can return the typed outcome while the existing public function continues returning `.results`, and a new diagnostic-returning function can be used only by the explicit tool.
That preserves existing callers without a wrapper layer in production behavior.

### Error text and health coverage need tighter boundaries

`describe_embedder_error` proposes embedding the configured model, host, and a redacted arbitrary exception string into persisted and tool-facing output (`PLAN-OTHER.md`, lines 74-75 and 136-139).
The auth-specific branch does not explicitly redact URL userinfo before formatting the host, and generic exception strings can contain filesystem paths or verbose upstream response bodies even after credential-pattern redaction.
Tool output should use a fixed category-and-status message such as `Semantic search unavailable: embedder authentication failed (HTTP 401).`, while richer redacted detail remains in operator logs.

The proposed classification groups OpenAI `PermissionDeniedError` with authentication without defining whether the user-visible status is 401 or 403 (`PLAN-OTHER.md`, lines 74-75 and 156).
Those statuses should remain distinct because a valid key lacking permission is not repaired in the same way as a rejected key.

The fire-and-forget startup task should use the repository's logged detached-task helper or another tracked lifecycle seam, not an unobserved bare `asyncio.create_task` (`PLAN-OTHER.md`, lines 82-83).
The probe can remain non-blocking, but its own unexpected failure must be logged and its lifecycle behavior must be testable.

### Consecutive failure handling is mostly right but underspecified

The central persisted counter is the right design, but `PLAN-OTHER.md` does not clearly say whether the error-level log fires only at exactly three or for every failure from three onward (`PLAN-OTHER.md`, lines 88-93).
Logging at the threshold and subsequent failures is the useful behavior because a five-week outage must remain visible.
The implementation should also use the existing per-source refresh/file locking around the load-increment-save operation rather than imply that generic atomic JSON writing alone makes the read-modify-write atomic.

## 3. Problems in my `PLAN.md`

The other plan exposes that my plan is far too large for this issue.
My exact file map spans credential resolution, a rich health registry, orchestrator reload behavior, two APIs, four frontend files, three documentation files, several knowledge-description layers, and multiple memory adapters.
The dashboard failure counter, frontend status rendering, config helper-text change, per-signature process snapshots, and broad category/status payload are not required to separate the key or make failures loud.

My new `embedder_credentials.py` is the wrong file choice.
Credential resolution and environment-to-store synchronization already live together in `credentials_sync.py`, and the other plan's reuse of `_sync_service_credentials` provides correct `_source=env` versus `_source=ui` behavior without duplicating policy.
I also omitted the useful `EMBEDDER_API_KEY` and `EMBEDDER_API_KEY_FILE` bootstrap path and the `EmbedderConfig.api_key` description update.

My generic provider fallback is imprecise.
Only the `openai` embedder branch consumes an API key today, so describing a provider-derived fallback with Gemini alias handling suggests support that `create_configured_embedder` does not have.
The compatibility fallback should simply remain the existing `openai` credential for the OpenAI-compatible branch.

The proposed health module is over-specified.
A signature-keyed, lock-protected snapshot with timestamps, two counters, several failure categories, and request instrumentation is more machinery than one process-wide embedder needs.
A small frozen failure value, a lock-protected current state, strict classification, and success/failure recording are sufficient.

My startup design is also heavier than necessary.
Holding readiness until a bounded probe completes adds startup latency even though the service is intentionally allowed to run without semantic search.
A logged, lifecycle-owned background check can surface the failure without changing readiness semantics.

The memory diagnostic facade in my plan duplicates work and can race the search it diagnoses.
Resolving the synthetic file-memory index again after the backend already made its fallback decision is inferior to returning a typed outcome from the backend seam.
The other plan's outcome placement should win, while my compatibility-preserving list facade should remain.

My knowledge-tool design handles Agno's swallow correctly, but it carries too much per-source state through `KnowledgeWithSourceDescriptions`, creates a dummy knowledge handle, and specifies mixed-source result annotation that is not needed for a single process-wide embedder outage.
A narrow wrapper at the already-existing `KnowledgeToolDescribingAgent.get_tools` and `aget_tools` seam can replace a swallowed empty result with the canonical failure and provide a diagnostic-only tool when no queryable semantic handle exists.

My plan also proposes probing Ollama and sentence-transformers even though ISSUE-237 is credential separation and 401 surfacing for an OpenAI-compatible embedder.
Those providers can keep their existing doctor checks in this change.

Finally, exposing the consecutive count through the API and dashboard is speculative.
Persisting it, resetting it on success, and emitting an error log from the existing failure choke point meet the alerting requirement without adding product surface.

## 4. Synthesis recommendation

I would ship the following smaller merged plan.

### Credential resolution

1. Add `get_embedder_api_key` and dedicated env synchronization to `src/mindroom/credentials_sync.py`.
The exact order is nonblank authored `memory.embedder.config.api_key`, nonblank `embedder` service `api_key`, existing `openai` service key, then `None` for keyless OpenAI-compatible endpoints.
Seed the dedicated service from `EMBEDDER_API_KEY` or `EMBEDDER_API_KEY_FILE` through `_sync_service_credentials`, and do not add `embedder` to `PROVIDER_ENV_KEYS`.

2. Use that resolver only in the OpenAI branches of `src/mindroom/embedding_factory.py` and `src/mindroom/memory/config.py`.
Leave the Mem0 LLM credential, Ollama, and sentence-transformers behavior unchanged.

3. Update the `EmbedderConfig.api_key` field description and make `src/mindroom/cli/doctor.py` use the same resolver with its existing embeddings endpoint request.
Document the three-source precedence and optional dedicated env variable in `docs/memory.md` and `docs/knowledge.md`.
Do not add dashboard work.

### Strict failures and health

4. Change every sync, async, usage, and batch path in `src/mindroom/openai_embedder.py` to reject empty vectors and re-raise provider errors.
Remove the per-item batch retry after a batch-wide auth failure.
Record a small process-wide health failure on an actual request error and clear it after an actual nonempty-vector success.

5. Add a small functional `src/mindroom/embedder_health.py` with one frozen safe failure value, structured 401 versus 403 versus transport/other classification, fixed tool-facing messages, redacted operator detail, a lock-protected current value, and a strict one-vector probe.
Do not add signature maps, a second counter, or provider probes outside the OpenAI-compatible path.

6. Start the probe through a logged lifecycle-owned task after credential synchronization, and expose an additive redacted `embedder` block from `/api/health` without changing its status code or `/api/ready`.
A successful probe or real embedding request must clear the value.
Re-run the check only when the embedder configuration is reloaded, if that lifecycle already provides a narrow changed-config hook.

### Refresh failures and durable escalation

7. Preserve the first real per-file indexing exception in `KnowledgeManager` and redact it before persistence, as the other plan proposes.
When a semantic refresh fails, run the strict probe at the refresh-result seam and prefer the canonical embedder failure only when the probe confirms it; otherwise retain the original reader, Git, chunking, or publication error.
The refresh probe must be keyed off the persisted/result `REFRESH_FAILED` outcome, not merely a raised scheduler task.

8. Add one optional `consecutive_refresh_failures` integer to `PublishedIndexState`.
Increment it in `mark_published_index_refresh_failed_preserving_last_good`, reset it in every success/publish path, parse a missing legacy value as zero, include it in `_published_state_fingerprint`, and log at error level at three and on every later failure.
Keep the last-good collection and indexed metadata intact, use existing refresh locks for the read-modify-write, and do not add API or frontend rendering for the count.

9. Add `last_error` to the existing knowledge availability detail and base-access resolution values and append only a safe classified cause to `format_knowledge_availability_notice`.
This reuses the enrichment path already consumed by Matrix, teams, delegation, and OpenAI-compatible requests.

### User-visible search results

10. Add `MemorySearchOutcome` at the internal backend seam, but preserve `search_agent_memories(...) -> list[MemoryResult]` by unwrapping `.results`.
Add a separate diagnostic-returning facade used only by `custom_tools/memory.py`.
The file backend should attach the persisted refresh error when it falls back to keywords and consult the current request health after Agno turns a query exception into an empty list.
The Mem0 backend should classify a propagated provider error and consult current health when Mem0 returns an empty result after swallowing one.

11. Keep healthy-empty output unchanged.
For a confirmed 401 with no fallback matches, return exactly `Semantic search unavailable: embedder authentication failed (HTTP 401).`.
For keyword fallback matches, prefix the existing result list with the same failure plus `Showing keyword matches only.`.
Do not expose raw exception bodies, configured hosts, or credential-repair instructions in model-facing tool text.

12. Fix knowledge search at the existing `agent_knowledge_descriptions.py` seam because changing the embedder alone cannot cross Agno's catch.
Pass one safe knowledge failure notice into `KnowledgeToolDescribingAgent`, wrap the generated sync or async `search_knowledge_base` function so a swallowed empty result becomes the canonical failure, and add a diagnostic-only function with the same name when assigned semantic bases are unavailable and Agno generated no tool.
Avoid dummy vector databases, a new `Knowledge` subclass, partial-source accounting, or changes to `bot.py` and `orchestrator.py` beyond lifecycle wiring.

### Tests and validation

13. Test credential precedence, blank values, env and `_FILE` seeding, UI-source protection, both embedder constructors, doctor resolution, and the unchanged Mem0 LLM key.

14. Test all strict OpenAI embedder methods, empty vectors, safe 401/403 classification, redaction, startup recording, success clearing, health payload behavior, and a reload check only if implemented.

15. Test refresh error preservation, confirmed-auth canonicalization, the persisted counter across reloads, reset on success, threshold-and-later logs, legacy metadata, last-good preservation, and fingerprint inclusion.

16. Test healthy empty memory, auth failure with no results, auth failure with keyword matches, Mem0 swallow/raise behavior against the installed source seam, and preservation of the public list-returning memory API.

17. Test the actual generated sync and async knowledge tool entrypoints for a healthy empty search, Agno-swallowed 401, a failed base with no generated tool, and last-good availability notices.
No live provider is required because fake clients and persisted metadata cover the behavior deterministically.

18. Run focused tests while implementing, then `uv run tach check --dependencies --interfaces`, the full `uv run pytest` suite, and `uv run pre-commit run --all-files`.

This synthesis takes the other plan's existing-file credential integration, local error capture, typed backend outcome, and focused tests.
It keeps my plan's compatibility facade, safe canonical messages, recovery clearing, and explicit correction for Agno's knowledge-search swallow.
It drops both plans' unnecessary surface area: no dashboard counter, no broad provider work, no per-source diagnostic hierarchy, and no dummy knowledge objects.
