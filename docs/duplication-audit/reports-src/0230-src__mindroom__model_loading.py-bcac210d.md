# Summary

No meaningful duplication found.
`src/mindroom/model_loading.py` is the central runtime model factory, and other source modules call `get_model_instance` rather than recreating full model construction.
The closest related code is doctor-time provider validation and embedder construction, but those flows intentionally differ from runtime chat-model instantiation.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_canonical_provider	function	lines 38-40	none-found	"strip().lower().replace('-', '_')", canonical provider normalization, provider lower/replace	src/mindroom/model_loading.py:40
_create_model_for_provider	function	lines 43-130	related-only	get_api_key_for_provider, get_ollama_host, provider_map, OpenRouter/Ollama/Gemini/Claude/OpenAIChat construction, unsupported provider	src/mindroom/cli/doctor.py:288, src/mindroom/cli/doctor.py:331, src/mindroom/cli/doctor.py:414, src/mindroom/knowledge/manager.py:317, src/mindroom/credentials_sync.py:362, src/mindroom/credentials_sync.py:389
get_model_instance	function	lines 133-176	related-only	get_model_instance callers, Unknown model Available models, model credentials, install_llm_request_logging, install_vertex_claude_prompt_cache_hook	src/mindroom/agents.py:439, src/mindroom/routing.py:85, src/mindroom/teams.py:615, src/mindroom/teams.py:1330, src/mindroom/thread_summary.py:329, src/mindroom/memory/auto_flush.py:533, src/mindroom/voice_handler.py:458, src/mindroom/topic_generator.py:89, src/mindroom/history/runtime.py:106, src/mindroom/scheduling.py:688
```

# Findings

No real duplicated behavior found.

Related-only candidates checked:

- `src/mindroom/cli/doctor.py:288` validates Vertex AI Claude connectivity and repeats some provider-specific environment resolution for `project_id` and `region`.
  This is related to `model_loading._create_model_for_provider` lines 62-88, but it is not duplicated runtime construction because doctor builds a short-lived validation request, uses Agno's `VertexAIClaude` directly, sets a validation timeout, and handles diagnostic exceptions.
- `src/mindroom/cli/doctor.py:331`, `src/mindroom/cli/doctor.py:414`, `src/mindroom/cli/doctor.py:480`, `src/mindroom/cli/doctor.py:502`, and `src/mindroom/cli/doctor.py:535` resolve Ollama host values for reachability checks.
  This overlaps with `model_loading._create_model_for_provider` line 94 only at the policy level of "config/env/default host", but doctor intentionally scans configured models and memory settings for diagnostics rather than constructing a model instance.
- `src/mindroom/knowledge/manager.py:317` constructs embedding providers with `get_api_key_for_provider`, `get_ollama_host`, and unsupported-provider errors.
  This is the same provider-family concept as model loading, but it targets Agno embedder classes and a different config model, so a shared chat-model factory would not reduce duplication there.
- Callers in `agents.py`, `routing.py`, `teams.py`, `thread_summary.py`, `memory/auto_flush.py`, `voice_handler.py`, `topic_generator.py`, `history/runtime.py`, and `scheduling.py` consistently use `model_loading.get_model_instance`.
  I did not find another source implementation that checks model existence, merges model-scoped credentials, constructs the model, and installs LLM/Vertex hooks.

# Proposed Generalization

No refactor recommended.
The existing `model_loading.py` module already acts as the shared helper for runtime chat-model creation.
Extracting doctor's validation-only provider checks or knowledge embedder creation into this path would mix runtime construction with diagnostics or embedding concerns.

# Risk/Tests

No production code was changed.
If future work changes provider env resolution, tests should cover:

- Runtime model creation for OpenAI, Anthropic, Google/Gemini, OpenRouter, Ollama, Codex, and Vertex Claude.
- Doctor validation for Ollama and Vertex Claude, because those paths have related but intentionally separate resolution rules.
- Knowledge embedder creation for OpenAI and Ollama, because it shares credential helpers but not model classes.
