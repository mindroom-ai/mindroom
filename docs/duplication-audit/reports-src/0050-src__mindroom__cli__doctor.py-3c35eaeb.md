## Summary

Top duplication candidates in `src/mindroom/cli/doctor.py` are provider credential/host resolution, memory embedder construction/validation rules, and Vertex AI Claude environment/request setup.
The duplication is mostly between doctor diagnostics and runtime setup paths, which makes drift likely because doctor must mirror what runtime actually uses.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
doctor	function	lines 33-100	related-only	CLI orchestration doctor steps config providers memory matrix storage	src/mindroom/cli/main.py:130, src/mindroom/api/main.py:461
_run_doctor_step	function	lines 103-106	related-only	console.status spinner CLI step wrapper	src/mindroom/cli/local_stack.py:244, src/mindroom/cli/config.py:534
_check_config_exists	function	lines 109-115	related-only	config_path.exists config file missing CLI	src/mindroom/cli/config.py:410, src/mindroom/constants.py:1112
_check_config_valid	function	lines 118-137	related-only	_load_config_quiet iter_config_validation_messages get_all_configured_rooms	src/mindroom/cli/config.py:326, src/mindroom/cli/config.py:534, src/mindroom/config/main.py:120, src/mindroom/config/main.py:1582
_get_custom_base_url	function	lines 151-158	related-only	model extra_kwargs base_url provider	src/mindroom/model_loading.py:151, src/mindroom/model_loading.py:43
_http_check	function	lines 161-174	related-only	httpx get timeout is_success HTTP status helper	src/mindroom/cli/local_stack.py:244, src/mindroom/tool_system/sandbox_proxy.py:673
_is_local_network_host	function	lines 177-186	none-found	ipaddress local private loopback link local host hint	none
_with_local_network_hint	function	lines 189-212	none-found	local network host no route connection refused hint	none
_validate_openai_embeddings_endpoint	function	lines 215-254	duplicate-found	OpenAI embeddings request validate embedding vector data	src/mindroom/embeddings.py:113, src/mindroom/embeddings.py:130, src/mindroom/knowledge/manager.py:321
_validate_provider_key	function	lines 257-285	duplicate-found	provider API key env key models endpoint google gemini anthropic headers	src/mindroom/constants.py:1005, src/mindroom/constants.py:1025, src/mindroom/credentials_sync.py:362, src/mindroom/model_loading.py:54
_validate_vertexai_claude_connection	function	lines 288-328	duplicate-found	vertexai_claude project_id region env request params Claude	src/mindroom/model_loading.py:62, src/mindroom/model_loading.py:75, src/mindroom/vertex_claude_compat.py:47
_get_ollama_host	function	lines 331-336	duplicate-found	ollama host config env credentials default localhost 11434	src/mindroom/credentials_sync.py:389, src/mindroom/model_loading.py:93, src/mindroom/memory/config.py:77, src/mindroom/knowledge/manager.py:329
_check_providers	function	lines 339-366	related-only	group models by provider validate once	env_key duplicate checked in _check_single_provider candidates
_print_validation	function	lines 369-384	related-only	tri-state validation print green red yellow counts	src/mindroom/avatar_generation.py:303, src/mindroom/avatar_generation.py:367
_check_single_provider	function	lines 387-448	duplicate-found	provider validation ollama api key vertexai validated_keys	src/mindroom/model_loading.py:43, src/mindroom/credentials_sync.py:362, src/mindroom/credentials_sync.py:389
_check_memory_config	function	lines 451-474	related-only	memory backend per-agent mem0 none file mixed	get_agent_memory_backend in src/mindroom/config/main.py:1582 adjacent; no duplicate checker found
_check_memory_llm	function	lines 477-528	duplicate-found	memory llm provider host ollama fallback api key base_url	src/mindroom/memory/config.py:88, src/mindroom/memory/config.py:121
_check_memory_embedder	function	lines 531-581	duplicate-found	memory embedder provider openai ollama sentence_transformers host api key	src/mindroom/memory/config.py:56, src/mindroom/knowledge/manager.py:317
_validate_sentence_transformers_embedder	function	lines 584-594	related-only	create sentence_transformers embedder get_embedding empty vector	src/mindroom/embeddings.py:83, src/mindroom/knowledge/manager.py:333
_check_matrix_homeserver	function	lines 597-611	related-only	matrix versions URL response_has_matrix_versions httpx get verify	src/mindroom/matrix/health.py:53, src/mindroom/matrix/health.py:58, src/mindroom/cli/local_stack.py:253, src/mindroom/orchestration/runtime.py:357
_check_storage_writable	function	lines 614-626	related-only	storage mkdir mkstemp writable check tempfile unlink	src/mindroom/orchestrator.py:1945, src/mindroom/interactive.py:188
```

## Findings

### 1. Provider credential and host resolution is mirrored in doctor and runtime

`_validate_provider_key`, `_check_single_provider`, and `_get_ollama_host` in `src/mindroom/cli/doctor.py:257`, `src/mindroom/cli/doctor.py:387`, and `src/mindroom/cli/doctor.py:331` duplicate runtime provider resolution behavior in `src/mindroom/credentials_sync.py:362`, `src/mindroom/credentials_sync.py:389`, and `src/mindroom/model_loading.py:43`.
Both paths normalize Google/Gemini identity, map providers to credentials, special-case Ollama as host-only, and supply default Ollama host behavior.
The important behavior difference is that doctor reads API keys directly from `RuntimePaths.env_value`, while runtime reads shared credentials via `CredentialsManager` after env sync.
This means doctor can warn about missing credentials even when runtime would find credentials persisted in the shared credentials store.

### 2. Memory LLM/embedder doctor checks duplicate Mem0 and knowledge embedder setup

`_check_memory_llm` and `_check_memory_embedder` in `src/mindroom/cli/doctor.py:477` and `src/mindroom/cli/doctor.py:531` mirror provider-specific memory setup in `src/mindroom/memory/config.py:56`, `src/mindroom/memory/config.py:88`, and `src/mindroom/memory/config.py:121`.
The embedder provider cases also overlap with knowledge embedder construction in `src/mindroom/knowledge/manager.py:317`.
All three paths decide between OpenAI, Ollama, and sentence-transformers, resolve hosts, pass dimensions/model names, and handle local sentence-transformers creation.
Differences to preserve are that doctor performs reachability/probe checks and prints tri-state diagnostics, while runtime constructs concrete Mem0 or knowledge embedder objects.

### 3. Vertex AI Claude setup is duplicated between validation and model loading

`_validate_vertexai_claude_connection` in `src/mindroom/cli/doctor.py:288` repeats environment fallback and request-parameter setup from `src/mindroom/model_loading.py:62`.
Both paths resolve `project_id` from `ANTHROPIC_VERTEX_PROJECT_ID`, `region` from `CLOUD_ML_REGION`, merge them into `extra_kwargs`, and instantiate a Vertex Claude model.
Runtime additionally handles `ANTHROPIC_VERTEX_BASE_URL`, ADC credentials, prompt cache defaults, and the compatibility subclass in `src/mindroom/vertex_claude_compat.py:47`.
The doctor currently validates with Agno's base `VertexAIClaude`, so a diagnostic result can diverge from the actual runtime client.

### 4. OpenAI-compatible embedding request shape is duplicated at a lower level

`_validate_openai_embeddings_endpoint` in `src/mindroom/cli/doctor.py:215` hand-builds a raw `/embeddings` request and validates the returned embedding vector.
Runtime request construction lives in `MindRoomOpenAIEmbedder._request_params` and `response` in `src/mindroom/embeddings.py:113` and `src/mindroom/embeddings.py:130`.
Knowledge embedder construction uses the same embedder class in `src/mindroom/knowledge/manager.py:321`.
The difference to preserve is that doctor intentionally sends a tiny probe and accepts arbitrary OpenAI-compatible JSON, while runtime uses the OpenAI client and typed response objects.

## Proposed Generalization

1. Add a small provider diagnostics helper module, for example `src/mindroom/provider_diagnostics.py`, that returns provider credentials/hosts from the same sources used by runtime.
2. Move provider validation endpoint metadata and auth-header construction out of `cli/doctor.py` into that helper, while keeping console output in doctor.
3. Add a shared `resolve_ollama_host(config, runtime_paths, *, configured_host=None)` helper that first respects explicit config where needed, then shared credentials, then the current default.
4. Add a narrow Vertex Claude factory/request-preparation helper used by both `model_loading.py` and doctor, so doctor validates the same compatibility subclass and env fallbacks as runtime.
5. Keep `_print_validation` and step orchestration local to doctor because those are CLI presentation details, not shared behavior.

## Risk/tests

Provider resolution changes risk altering credential precedence, especially env values versus persisted shared credentials.
Tests should cover Google/Gemini alias handling, missing API key warnings, custom OpenAI base URLs, Ollama host precedence, and memory embedder host/model/dimensions behavior.
Vertex AI Claude tests should assert doctor and runtime use the same `project_id`, `region`, optional `base_url`, and client class.
No refactor should change CLI exit counts or message severity without explicit product intent.
