## Summary

Top duplication candidates:

- `src/mindroom/memory/config.py` and `src/mindroom/knowledge/manager.py` both derive Chroma collection identity from memory embedder settings.
- `src/mindroom/memory/config.py` and `src/mindroom/knowledge/manager.py` both translate `config.memory.embedder` into provider-specific OpenAI/Ollama/sentence-transformers runtime settings.
- `src/mindroom/memory/config.py` repeats direct credential lookup patterns that already have shared helpers in `src/mindroom/credentials_sync.py`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_memory_collection_name	function	lines 20-33	duplicate-found	memory_collection_name collection_name effective_mem0_embedder_signature effective_knowledge_embedder_signature sha256 collection prefix	src/mindroom/knowledge/manager.py:276; src/mindroom/knowledge/manager.py:282; src/mindroom/knowledge/manager.py:286; src/mindroom/embeddings.py:34; src/mindroom/embeddings.py:56
_get_memory_config	function	lines 36-148	duplicate-found	MemoryConfig embedder provider openai ollama sentence_transformers api_key ollama_base_url openai_base_url chroma vector_store get_ollama_host get_api_key_for_provider	src/mindroom/knowledge/manager.py:317; src/mindroom/credentials_sync.py:362; src/mindroom/credentials_sync.py:389; src/mindroom/model_loading.py:43; src/mindroom/cli/doctor.py:495; src/mindroom/cli/doctor.py:531
create_memory_instance	async_function	lines 152-180	related-only	create_memory_instance AsyncMemory.from_config ensure_sentence_transformers_dependencies timed memory factory	src/mindroom/memory/functions.py:68; src/mindroom/memory/_mem0_backend.py:63; src/mindroom/embeddings.py:78; src/mindroom/embeddings.py:83
```

## Findings

### 1. Chroma collection naming uses repeated embedder-signature hashing

`src/mindroom/memory/config.py:20` builds a stable Chroma collection name by reading `config.memory.embedder`, creating an effective Mem0 embedder signature, hashing it with SHA-256, and appending the digest to `mindroom_memories`.

`src/mindroom/knowledge/manager.py:286` performs the same kind of behavior for knowledge indexing state: it reads `config.memory.embedder`, expands an effective knowledge embedder signature at `src/mindroom/knowledge/manager.py:294`, and folds it into a persistent settings key that controls Chroma index compatibility.
`src/mindroom/knowledge/manager.py:276` and `src/mindroom/knowledge/manager.py:282` separately hash storage/source identity into Chroma collection names.

This is real functional duplication around "derive stable Chroma identity from embedder settings", though the exact identity inputs differ.
Memory collection names only depend on embedder compatibility.
Knowledge collection names depend on knowledge-base identity and path, while embedder compatibility lives in the indexing settings key rather than the collection name.

### 2. Memory and knowledge duplicate provider-specific embedder runtime mapping

`src/mindroom/memory/config.py:58` builds a Mem0 embedder config from `config.memory.embedder`.
It maps `sentence_transformers` to Mem0 provider `huggingface`, copies the model, fills OpenAI `api_key`, maps OpenAI host to `openai_base_url`, maps dimensions to `embedding_dims`, maps Ollama host to `ollama_base_url`, and handles sentence-transformers dimensions.

`src/mindroom/knowledge/manager.py:317` reads the same `config.memory.embedder` object and performs the equivalent provider split for Agno embedder instances.
For OpenAI it passes model, API key, base URL, and dimensions at `src/mindroom/knowledge/manager.py:321`.
For Ollama it resolves the credential-backed host at `src/mindroom/knowledge/manager.py:329`.
For sentence-transformers it delegates to `create_sentence_transformers_embedder` at `src/mindroom/knowledge/manager.py:333`.

The output types differ, so this should not become one large factory.
The duplicated behavior is the provider normalization and credential/host/dimensions resolution from the same config source.
The important difference to preserve is that Mem0 expects dictionary field names such as `openai_base_url`, `ollama_base_url`, and `embedding_dims`, while Agno embedder constructors use `base_url`, `host`, and `dimensions`.

### 3. Memory config repeats shared credential helper behavior

`src/mindroom/memory/config.py:69`, `src/mindroom/memory/config.py:108`, and `src/mindroom/memory/config.py:112` call `get_runtime_shared_credentials_manager(...).get_api_key(...)` directly.
`src/mindroom/credentials_sync.py:362` already centralizes provider API-key lookup and includes provider normalization for `gemini` to `google`.

`src/mindroom/memory/config.py:79`, `src/mindroom/memory/config.py:99`, and `src/mindroom/memory/config.py:125` directly load `ollama` credentials to resolve a host.
`src/mindroom/credentials_sync.py:389` already provides `get_ollama_host(runtime_paths)`.
`src/mindroom/knowledge/manager.py:330` and `src/mindroom/model_loading.py:94` already use that helper.

This duplication is small but active.
It makes memory config slightly inconsistent with model loading and knowledge indexing, especially if provider aliases or credential lookup behavior changes.

## Proposed Generalization

1. Add a tiny shared resolver near the existing embedder helpers, likely in `src/mindroom/embeddings.py`, that returns normalized embedder provider settings from `config.memory.embedder` plus `runtime_paths`.
2. Keep output-specific adapters separate: one adapter in memory config for Mem0 dict field names and one in knowledge manager for Agno constructor arguments.
3. Replace direct Ollama credential loading in memory config with `get_ollama_host(runtime_paths)` and direct provider API-key lookup with `get_api_key_for_provider(provider, runtime_paths)`.
4. If collection compatibility logic needs more centralization later, add a small helper that hashes an embedder signature with a caller-provided prefix.
5. Do not merge Mem0 memory creation with knowledge `KnowledgeManager` creation; their lifecycle and output objects are different.

## Risk/tests

The main behavior risk is changing provider-specific field names passed to Mem0.
Tests should pin OpenAI `openai_base_url`, OpenAI `embedding_dims`, Ollama `ollama_base_url`, and the `sentence_transformers` to `huggingface` provider mapping in `tests/test_memory_config.py`.

Collection-name tests should continue covering default OpenAI dimensions, custom OpenAI-compatible dimensions, and provider changes.
Knowledge tests around `_create_embedder`, `_indexing_settings_key`, and collection state should be run if shared embedder-signature or resolver code is touched.

No production code was edited for this audit.
