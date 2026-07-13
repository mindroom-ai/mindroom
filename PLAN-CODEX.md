# ISSUE-237 implementation plan

## Outcome

Separate the configured embedder from the shared model-provider credential while retaining the current provider credential as a compatibility fallback.
Make authentication and reachability failures observable at startup, durable across repeated knowledge refresh failures, and explicit in the `search_memories` and `search_knowledge_base` tool results.
Keep keyword file-memory fallback and last-good knowledge indexes available, but label those results as degraded instead of presenting an embedding failure as a valid empty search.

## Current behavior and failure points

- `src/mindroom/config/models.py` already defines `EmbedderConfig.api_key`, but neither embedder construction path honors it.
- `src/mindroom/memory/config.py::_get_memory_config` always obtains an OpenAI-compatible Mem0 embedder key from `get_api_key_for_provider("openai")`.
- `src/mindroom/embedding_factory.py::create_configured_embedder` does the same for the Agno embedder used by semantic file memory and semantic knowledge bases.
- `src/mindroom/cli/doctor.py` checks the provider environment variable rather than the credential source used by the running embedder, so its result can disagree with runtime behavior.
- `src/mindroom/openai_embedder.py` inherits or implements methods that catch provider errors and return empty vectors, which converts a 401 into an apparently successful empty search or partial index.
- `src/mindroom/knowledge/refresh_runner.py` persists `last_error` and `refresh_failed`, but it can persist only a secondary error such as an empty or partial index and does not distinguish embedder authentication from unrelated indexing failures.
- `src/mindroom/knowledge/registry.py::PublishedIndexState` preserves the last error but not a consecutive failure count.
- `src/mindroom/memory/_file_backend.py` deliberately falls back from unavailable semantic search to keyword search without returning the degradation reason to its caller.
- `src/mindroom/custom_tools/memory.py::search_memories` turns an empty result list into `No relevant memories found.` without checking semantic index or embedder health.
- `src/mindroom/knowledge/utils.py` and `src/mindroom/agent_run_context.py` already expose a generic knowledge availability notice to the model, but the generated Agno knowledge tool can still return `No documents found` because Agno catches vector-search exceptions internally.

## Implementation

### 1. Centralize embedder credential resolution

Add a small resolver in a new leaf module named `src/mindroom/embedder_credentials.py`.
Use the same resolver from both `src/mindroom/memory/config.py` and `src/mindroom/embedding_factory.py` so Mem0, semantic file memory, and knowledge indexing cannot choose different credentials.

The resolution order will be:

1. A non-empty `config.memory.embedder.config.api_key` value.
2. The `api_key` stored in the shared CredentialsManager service named `embedder`, which lives in `credentials/embedder_credentials.json`.
3. The current provider credential returned by `get_api_key_for_provider(config.memory.embedder.provider, ...)`, including the existing `gemini` to `google` alias behavior.
4. `None` when no key exists, allowing keyless local OpenAI-compatible endpoints to continue working as they do today.

Do not add a new config field for ISSUE-237.
There is only one process-wide embedder configuration today, so a fixed dedicated `embedder` service provides separation without adding per-host naming or another configuration concept.
The existing `EmbedderConfig.api_key` is the explicit override required by the precedence rule and will finally become effective.
Treat blank strings as absent, never log the selected key, and do not include credential values or credential file metadata in collection signatures.

Update `src/mindroom/cli/doctor.py` to use this resolver and the shared health probe described below instead of reading `OPENAI_API_KEY` directly.
This makes `mindroom doctor` validate the exact endpoint, model, and credential combination used by the runtime.

Update `tach.toml` in the same implementation if the new resolver or health module changes a governed dependency, especially the existing `mindroom.embedding_factory` and `mindroom.openai_embedder` entries.

### 2. Add a strict, reusable embedder health probe

Add `src/mindroom/embedder_health.py` with typed dataclasses and small functional helpers rather than a long-lived service class.
The module will own a process-local, lock-protected latest health snapshot keyed by a secret-free embedder signature of provider, model, host, and dimensions.
The snapshot will contain status, failure category, optional HTTP status, a redacted operator detail, check time, and a consecutive probe or request failure count.

The probe will make one minimal embedding request using the configured provider implementation and will reject an empty vector as a failure.
For OpenAI-compatible embedders, it will use the resolved embedder key and the configured base URL and model.
For Ollama and sentence-transformers, it will exercise the configured embedder rather than merely checking a generic models endpoint.
Provider SDK imports needed only for structured error classification will remain deferred to the probe or provider implementation so slim import-graph tests do not acquire an OpenAI SDK dependency.

Classify a structured HTTP 401 as `auth_failed` and render it as `Semantic search unavailable: embedder authentication failed (HTTP 401).`.
Classify other 4xx responses, 5xx responses, transport failures, and invalid or empty vectors separately so operators do not receive misleading credential advice.
Redact provider response details with the repository's centralized redaction helpers before logging or retaining them, and expose only the category and status code in tool-facing text.

Change `src/mindroom/openai_embedder.py` so synchronous, asynchronous, usage-returning, and batch methods never translate provider exceptions into `[]` or `([], None)`.
Have those methods record a failed health snapshot before re-raising and record a successful snapshot after receiving a non-empty vector, using the lightweight health functions without importing the provider SDK into slim entry points.
Log with redaction if useful, then re-raise the original exception so index refresh code can persist the real 401 and vector search can mark the health snapshot failed.
Do not retry each batch item after a batch-wide authentication failure because that produces repeated requests with the same invalid credential and obscures the root cause.

### 3. Run the health check at startup and configuration reload

Wire the probe into the focused startup/config lifecycle from `src/mindroom/orchestrator.py` with only a small call at the lifecycle boundary.
Run it after configuration loading and environment-to-CredentialsManager synchronization, before the runtime is marked ready.
Run it again after a hot reload that changes `memory.embedder`, and allow a later successful probe or real embedding request to clear the degraded process snapshot.

Skip the probe when the active configuration cannot use embeddings, meaning there are no semantic knowledge bases and no effective Mem0 or semantic file-memory consumers.
Bound the probe with a short timeout and do not prevent Matrix bots from starting when it fails.
Instead, log a structured error immediately with provider, model, redacted host, failure category, and status code.

Extend `src/mindroom/api/main.py::health_check` with a redacted `embedder` diagnostic when the latest relevant probe failed.
Return HTTP 200 with `status: degraded` for an embedder-only failure so Kubernetes does not restart an otherwise usable Matrix runtime, while `/api/ready` remains tied to runtime startup readiness.
Keep the existing HTTP 503 behavior for stale Matrix sync loops.

### 4. Probe again on semantic refresh failure and persist consecutive failures

In `src/mindroom/knowledge/refresh_runner.py`, run the strict probe after a semantic refresh reports `_last_refresh_error` or raises during embedding or vector publication.
If the probe identifies an embedder failure, persist the canonical redacted embedder message and status instead of a secondary `Indexed 0 of N` or empty-vector message.
If the probe succeeds, retain the original refresh error because the failure belongs to file reading, chunking, Git, Chroma, or publication rather than the embedder.
Do not probe file-mode knowledge bases because they intentionally do not use embeddings.

Extend `src/mindroom/knowledge/registry.py::PublishedIndexState` with `consecutive_refresh_failures: int = 0`.
Parse a missing field as zero in `src/mindroom/knowledge/index_metadata.py`, increment it atomically whenever `mark_published_index_refresh_failed_preserving_last_good` writes a failure, and reset it in `mark_published_index_refresh_succeeded`.
Preserve the existing complete collection, indexed count, and last-published metadata while incrementing the count so the last-good-index guarantee remains unchanged.

Emit a structured `error` log when the count first reaches three consecutive failures and on subsequent failures, including the base ID, count, failure category, and redacted status but no credentials or raw response body.
This persisted per-index count survives restarts and also covers refreshes performed in the existing subprocess, unlike a process-only counter.
Expose the count through `src/mindroom/knowledge/status.py::KnowledgeIndexStatus` and the existing knowledge API payloads in `src/mindroom/api/knowledge.py` so the dashboard and support tooling can show that the failure is repeated rather than transient.

Update `frontend/src/components/Knowledge/Knowledge.tsx` and its existing tests to render the consecutive failure count beside `refresh_failed` and the already-redacted `last_error`.
Do not add a notification framework, email, Matrix alert bot, or separate alert database in this issue.

### 5. Surface degradation in `search_memories`

Add a diagnostic-returning facade in `src/mindroom/memory/functions.py`, exported from `src/mindroom/memory/__init__.py`, for the explicit memory tool to use without changing existing prompt-assembly callers.
A small dataclass such as `MemorySearchOutcome` will contain the normal list of `MemoryResult` values and an optional semantic degradation notice.
The existing `search_agent_memories` list-returning API will remain unchanged for compatibility and for internal prompt enrichment.

For file memory in semantic mode, add a read-only status helper in `src/mindroom/memory/_semantic_file_search.py` that derives the same synthetic knowledge base ID, reads its published index status, and converts an embedder-auth `last_error` into the canonical notice.
Add a narrow adapter in `src/mindroom/memory/_file_backend.py` that resolves the agent's existing file-memory root and calls that status helper for the diagnostic facade.
This helper must not schedule a second refresh or make another embedding request.
The normal search will continue to return keyword fallback matches when the semantic index is unavailable.

For Mem0, combine the latest process health snapshot with any structured exception raised by `AsyncMemory.search`.
Record and classify a direct 401 before returning it to the tool layer, while leaving healthy empty result lists unchanged.

Change `src/mindroom/custom_tools/memory.py::search_memories` to format outcomes as follows:

- When semantic search is healthy and there are no matches, retain `No relevant memories found.`.
- When embedder authentication failed and there are no keyword fallback matches, return `Semantic search unavailable: embedder authentication failed (HTTP 401).` and do not append `No relevant memories found.`.
- When file memory produced keyword fallback matches, prefix the normal formatted matches with `Semantic search unavailable: embedder authentication failed (HTTP 401). Showing keyword matches only.`.
- When the embedder failed for a non-auth reason, use the matching safe category-specific notice rather than exposing the provider response body.

Keep `list_memories`, direct file reads, and CRUD behavior independent from the health snapshot so users can still inspect and repair stored memory while semantic search is down.

### 6. Surface degradation in knowledge search

Extend `src/mindroom/knowledge/utils.py::KnowledgeAvailabilityDetail` with a safe failure notice and the consecutive refresh count derived from the resolved `PublishedIndexState`.
Update `format_knowledge_availability_notice` so the model receives the specific embedder-auth message instead of only a generic recent-refresh-failure warning.
Continue to state whether a last-good index exists, but do not imply it is queryable when the query embedder itself cannot authenticate.

Extend `src/mindroom/knowledge_source_descriptions.py::KnowledgeWithSourceDescriptions` with typed search diagnostics for unavailable bases.
When every assigned semantic base is unavailable, return a lightweight MindRoom knowledge handle with no queryable vector databases so Agno still generates `search_knowledge_base` and the tool can explain the outage instead of disappearing.
When only some bases are unavailable, keep querying the available bases and prefix the returned documents with a notice identifying the omitted bases.

In `src/mindroom/agent_knowledge_descriptions.py`, wrap the generated `search_knowledge_base` function entrypoint after Agno creates its schema.
The wrapper will preserve the existing arguments, references, filtering behavior, and description, but it will return or prefix the safe diagnostic instead of allowing Agno's `No documents found` result to mask a known embedder failure.
It will support both sync and async generated entrypoints.
Pass the typed diagnostic through the existing `_initialize_agent_instance` seam in `src/mindroom/agents.py` rather than adding behavior to `bot.py` or `orchestrator.py`.

The all-down tool result will use the same canonical wording as memory search, optionally adding affected knowledge base IDs on a following sentence.
The mixed-source result will say that semantic search was unavailable for the named bases and that returned documents came only from the remaining sources.
Legitimate healthy searches with no documents will continue to return Agno's existing `No documents found` response.

### 7. Documentation and UI guidance

Update `docs/memory.md`, `docs/knowledge.md`, and the memory section of `docs/configuration/index.md` with the credential precedence and a Credentials tab example for service `embedder` containing `{"api_key": "..."}`.
State clearly that the provider credential is only a compatibility fallback and that rotating a model-provider key does not affect an explicitly configured dedicated embedder credential.
Document the startup degraded health signal, the three-failure escalation, and the exact tool-facing authentication message.
Follow the repository rule of one sentence per Markdown line.

Update the helper text in `frontend/src/components/MemoryConfig/MemoryConfig.tsx` and its test to point users to the dedicated `embedder` service instead of implying that the OpenAI model-provider credential is the only option.

## Exact file map

- `src/mindroom/embedding_factory.py`: add or consume the single credential resolver and construct the knowledge embedder with the resolved key.
- `src/mindroom/embedder_credentials.py`: hold the leaf credential precedence helper shared by Mem0, Agno, doctor, and health checks.
- `src/mindroom/embedder_health.py`: define health results, safe classification and formatting, the strict probe, and the process-local latest snapshot.
- `src/mindroom/memory/config.py`: pass the same resolved key into Mem0.
- `src/mindroom/openai_embedder.py`: stop returning empty vectors on provider failures.
- `src/mindroom/cli/doctor.py`: validate the runtime's actual resolved embedder credential and endpoint.
- `src/mindroom/orchestrator.py`: invoke the focused probe during startup and relevant reloads.
- `src/mindroom/api/main.py`: expose embedder degradation without changing readiness or Matrix liveness semantics.
- `src/mindroom/knowledge/index_metadata.py`: parse the optional persisted consecutive-failure integer safely.
- `src/mindroom/knowledge/registry.py`: persist, increment, reset, and alert on consecutive refresh failures while preserving the last good index.
- `src/mindroom/knowledge/status.py`: expose consecutive failure state to callers.
- `src/mindroom/knowledge/refresh_runner.py`: health-check after semantic refresh failures and preserve the canonical cause.
- `src/mindroom/knowledge/utils.py`: carry specific failure diagnostics through knowledge resolution and availability notices.
- `src/mindroom/knowledge_source_descriptions.py`: carry typed per-source search diagnostics on MindRoom knowledge handles.
- `src/mindroom/agent_knowledge_descriptions.py`: wrap generated knowledge search tools so known failures cannot become `No documents found`.
- `src/mindroom/agents.py`: pass knowledge diagnostics through the existing agent construction seam.
- `src/mindroom/memory/_semantic_file_search.py`: read and classify the synthetic semantic index failure without scheduling work.
- `src/mindroom/memory/_file_backend.py`: resolve the canonical file-memory scope for the explicit-tool diagnostic without disturbing keyword fallback.
- `src/mindroom/memory/functions.py` and `src/mindroom/memory/__init__.py`: add the explicit-tool diagnostic outcome while preserving the existing list API.
- `src/mindroom/custom_tools/memory.py`: render the canonical outage and keyword-fallback messages.
- `src/mindroom/api/knowledge.py`: include the persisted failure count in status responses.
- `frontend/src/components/MemoryConfig/MemoryConfig.tsx` and `frontend/src/components/MemoryConfig/MemoryConfig.test.tsx`: explain dedicated embedder credentials.
- `frontend/src/components/Knowledge/Knowledge.tsx` and `frontend/src/components/Knowledge/Knowledge.test.tsx`: display repeated refresh failures.
- `docs/memory.md`, `docs/knowledge.md`, and `docs/configuration/index.md`: document configuration, compatibility fallback, and failure behavior.
- `tach.toml`: update enforced imports only where the implementation changes a governed boundary.

## Test plan

### Credential resolution and construction

- Extend `tests/test_memory_config.py` with precedence cases for explicit config key over dedicated credential, dedicated credential over provider credential, provider-only fallback, and a keyless endpoint.
- Assert that `_get_memory_config` and `create_configured_embedder` receive the same selected key for the same config and runtime paths.
- Assert that no credential value is present in collection names, indexing settings metadata, logs, or health payloads.
- Extend `tests/test_cli_config.py` so doctor uses CredentialsManager resolution and validates the configured embeddings endpoint rather than the provider environment variable.

### Health checks and provider failures

- Add `tests/test_embedder_health.py` for a successful non-empty vector, HTTP 401, other HTTP status, transport failure, empty vector, redaction, snapshot reset after success, and signature isolation after config changes.
- Use fake embedder objects or a mocked OpenAI embeddings client for unit tests, not a real endpoint.
- Extend `tests/test_embeddings.py` to prove every `MindRoomOpenAIEmbedder` sync, async, usage, and batch path re-raises failures and never returns empty vectors as an error sentinel.
- Extend `tests/test_orchestrator_runtime.py` to prove startup invokes the probe after config loading, a failed probe does not abort startup, and an embedder-changing reload rechecks it.
- Extend `tests/api/test_api.py` to verify the redacted degraded health payload stays HTTP 200 and does not alter `/api/ready`.

### Durable refresh failure state

- Extend `tests/test_knowledge_manager.py` with a refresh whose indexer reports a secondary empty-index error while the follow-up probe returns 401, then assert that persisted `last_error` identifies authentication.
- Assert that three failures across metadata reloads produce a count of three and an error-level alert, a successful publish resets the count to zero, and a failed refresh preserves the last-good collection and indexed count.
- Assert that a healthy follow-up probe leaves unrelated Git, reader, chunking, Chroma, and publication errors unchanged.
- Extend `tests/api/test_knowledge_api.py` to verify the failure count and redacted canonical error in list and detail responses.

### Memory and knowledge tool output

- Extend `tests/test_memory_tools.py` so a healthy empty search still returns `No relevant memories found.`, while an auth-failed empty search returns exactly `Semantic search unavailable: embedder authentication failed (HTTP 401).`.
- Add a file-memory case where keyword fallback returns matches and assert that the outage prefix and `[keyword]` matches are both present.
- Add a direct Mem0 401 exception case and assert that the raw provider body and token are absent.
- Extend `tests/test_memory_file_backend.py` to verify semantic failure diagnostics read the correct synthetic index, do not schedule duplicate refreshes, and do not remove keyword fallback.
- Extend `tests/test_knowledge_manager.py` and `tests/test_agents.py` for all-down and partially-down knowledge assignments, including sync and async generated tools, exact outage text, available-source results, and the unchanged healthy `No documents found` case.
- Extend the existing availability-notice tests in `tests/test_knowledge_manager.py` for the specific auth failure notice and consecutive count.

### Live validation

No live embedder is required for unit or integration tests because SDK clients, HTTP responses, and published metadata can be faked deterministically.
One optional manual smoke test should point an OpenAI-compatible embedder at a disposable local HTTP server that returns 401, start MindRoom, confirm the startup degraded health payload, call both search tools, then switch the `embedder` credential to a valid key and confirm a successful probe or refresh clears the state.
Do not use or rotate production credentials for this test.

### Repository validation

- Run focused tests for embedder, memory tools, file memory, knowledge manager, knowledge API, agent tool generation, orchestrator startup, doctor, and API health while iterating.
- Run `uv run tach check --dependencies --interfaces` if imports or public interfaces change.
- Run the full `uv run pytest` suite.
- Run `uv run pre-commit run --all-files` after `uv sync --all-extras` in the implementation worktree.

## Backward compatibility

- Existing configurations remain valid because no new field is required and the existing provider credential remains the final credential fallback.
- Deployments that currently rely on `credentials/openai_credentials.json` or `OPENAI_API_KEY` continue to work unchanged until an `embedder` credential or explicit config key is supplied.
- Adding `credentials/embedder_credentials.json` changes only embedding authentication and cannot change chat-model authentication.
- Existing explicit `memory.embedder.config.api_key` values begin working as documented, which is an intentional bug fix.
- Keyless local endpoints continue receiving `None` rather than a fabricated key.
- Embedder credentials do not participate in collection signatures, so adding or rotating a key does not force a new collection or discard a compatible last-good index.
- Published index metadata without `consecutive_refresh_failures` loads with zero, and old complete indexes remain queryable.
- Healthy empty memory and knowledge searches retain their current user-visible messages.
- File-memory keyword fallback and last-good knowledge reads remain available, now with explicit degradation labels.

## Risks and mitigations

- Removing empty-vector fallbacks may expose provider outages in code paths that previously limped onward, which is intentional because storing or querying with empty vectors is corrupt behavior.
- A startup probe adds one embedding request and can delay startup by its timeout, so it must use a tiny input, run off the event loop, and have a strict timeout.
- An invalid credential must not make readiness fail or trigger a restart loop, so embedder degradation uses HTTP 200 health diagnostics and explicit tool output.
- A stale process health snapshot could mask recovery, so every successful probe or real embedding request resets it and knowledge refresh success clears durable per-index state.
- Credential rotation does not change indexing settings, so failed indexes must remain eligible for the existing refresh retry path and a successful retry must clear the error and count.
- Multiple refresh subprocesses could race metadata updates, so the implementation must use the existing per-source refresh/file locks and atomic metadata writer rather than a separate counter file.
- Agno catches knowledge search exceptions and synthesizes `No documents found`, so tests must cover the generated tool entrypoint rather than only the vector database layer.
- Failure strings can contain credentials or upstream bodies, so only classified status and centrally redacted operator detail may cross into metadata, logs, APIs, or tool output.
- The new imports may affect slim entry points, so provider SDK imports stay deferred and import-graph plus Tach tests are mandatory.

## Non-goals

- Do not change the completed ops-side key rotation or deploy secrets.
- Do not remove the provider credential fallback in this issue.
- Do not invent per-host credential naming, multiple simultaneous embedders, or a general credential-routing framework while the config supports one embedder.
- Do not add email, webhook, Matrix, PagerDuty, or other external alert delivery.
- Do not rebuild indexes merely because the credential source changes.
- Do not change embedding models, dimensions, chunking, similarity thresholds, memory storage formats, or knowledge source semantics.
- Do not redesign Mem0, Agno, Chroma, the orchestrator, or the dashboard health architecture.
