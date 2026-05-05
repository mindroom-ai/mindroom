Summary: `effective_knowledge_embedder_signature` and `effective_mem0_embedder_signature` duplicate the same provider/host/dimension normalization behavior for two collection-key call sites.
`MindRoomOpenAIEmbedder` intentionally mirrors Agno's `OpenAIEmbedder` request and async embedding flow while changing the dimensions rule for OpenAI-compatible hosts; this is related duplication against dependency code, not duplication elsewhere in `./src`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_default_dimensions	function	lines 29-31	related-only	"_OPENAI_EMBEDDING_DIMENSIONS text-embedding-3-small text-embedding-3-large dimensions default"	src/mindroom/embeddings.py:44; src/mindroom/embeddings.py:66; src/mindroom/embeddings.py:107; tests/test_embeddings.py:128; tests/test_memory_config.py:251
effective_knowledge_embedder_signature	function	lines 34-53	duplicate-found	"effective_knowledge_embedder_signature effective_mem0_embedder_signature provider host dimensions indexing settings"	src/mindroom/embeddings.py:56; src/mindroom/knowledge/manager.py:286; src/mindroom/memory/config.py:20
effective_mem0_embedder_signature	function	lines 56-75	duplicate-found	"effective_mem0_embedder_signature effective_knowledge_embedder_signature provider host dimensions collection name"	src/mindroom/embeddings.py:34; src/mindroom/memory/config.py:20; src/mindroom/knowledge/manager.py:286
ensure_sentence_transformers_dependencies	function	lines 78-80	related-only	"ensure_optional_deps sentence_transformers sentence-transformers optional deps"	src/mindroom/memory/config.py:172; src/mindroom/embeddings.py:90; src/mindroom/runtime_support.py:93; src/mindroom/tool_system/dependencies.py:236
create_sentence_transformers_embedder	function	lines 83-95	related-only	"create_sentence_transformers_embedder SentenceTransformerEmbedder import_module dimensions get_embedding"	src/mindroom/knowledge/manager.py:333; src/mindroom/cli/doctor.py:584; tests/test_embeddings.py:95
MindRoomOpenAIEmbedder	class	lines 99-186	related-only	"MindRoomOpenAIEmbedder OpenAIEmbedder dimensions embeddings.create async_get_embedding batch usage"	src/mindroom/knowledge/manager.py:321; tests/test_embeddings.py:31; agno.knowledge.embedder.openai.OpenAIEmbedder
MindRoomOpenAIEmbedder.__post_init__	method	lines 104-108	related-only	"__post_init__ dimensions text-embedding-3-large default OpenAIEmbedder"	src/mindroom/embeddings.py:29; agno.knowledge.embedder.openai.OpenAIEmbedder.__post_init__; tests/test_embeddings.py:47
MindRoomOpenAIEmbedder._should_send_dimensions	method	lines 110-111	related-only	"should send dimensions base_url text-embedding-3 explicit dimensions OpenAIEmbedder"	src/mindroom/embeddings.py:121; agno.knowledge.embedder.openai.OpenAIEmbedder.response; agno.knowledge.embedder.openai.OpenAIEmbedder.async_get_embedding
MindRoomOpenAIEmbedder._request_params	method	lines 113-125	related-only	"input model encoding_format user dimensions request_params embeddings.create"	src/mindroom/embeddings.py:130; src/mindroom/embeddings.py:137; src/mindroom/embeddings.py:146; src/mindroom/embeddings.py:166; agno.knowledge.embedder.openai.OpenAIEmbedder.response
MindRoomOpenAIEmbedder.response	method	lines 130-132	related-only	"response embeddings.create _request_params OpenAIEmbedder response"	src/mindroom/embeddings.py:113; agno.knowledge.embedder.openai.OpenAIEmbedder.response; tests/test_embeddings.py:31
MindRoomOpenAIEmbedder.async_get_embedding	async_method	lines 134-141	related-only	"async_get_embedding embeddings.create response.data[0].embedding log_warning"	src/mindroom/embeddings.py:143; agno.knowledge.embedder.openai.OpenAIEmbedder.async_get_embedding; src/mindroom/knowledge/manager.py:180
MindRoomOpenAIEmbedder.async_get_embedding_and_usage	async_method	lines 143-152	related-only	"async_get_embedding_and_usage usage model_dump log_warning embedding"	src/mindroom/embeddings.py:134; src/mindroom/embeddings.py:178; agno.knowledge.embedder.openai.OpenAIEmbedder.async_get_embedding_and_usage; src/mindroom/knowledge/manager.py:185
MindRoomOpenAIEmbedder.async_get_embeddings_batch_and_usage	async_method	lines 154-186	related-only	"async_get_embeddings_batch_and_usage batch_size usage fallback async_get_embedding_and_usage"	src/mindroom/embeddings.py:143; agno.knowledge.embedder.openai.OpenAIEmbedder.async_get_embeddings_batch_and_usage
```

Findings:

1. Duplicate embedder signature normalization in `src/mindroom/embeddings.py:34` and `src/mindroom/embeddings.py:56`.
Both functions compute the same tuple of provider, model, effective host, and effective dimensions.
Both keep host only for `openai` and `ollama`, infer known OpenAI embedding dimensions when omitted, clear dimensions for `ollama` and `sentence_transformers`, and stringify the final dimension field.
The only observed difference is semantic naming for callers: knowledge indexing settings in `src/mindroom/knowledge/manager.py:286` and Mem0 collection naming in `src/mindroom/memory/config.py:20`.

2. `MindRoomOpenAIEmbedder` duplicates upstream Agno request construction and async flow by design, not another MindRoom source module.
The local `_request_params` helper at `src/mindroom/embeddings.py:113` removes the repeated request dictionary that upstream Agno currently inlines in `response`, `async_get_embedding`, `async_get_embedding_and_usage`, and `async_get_embeddings_batch_and_usage`.
The remaining method bodies at `src/mindroom/embeddings.py:130`, `src/mindroom/embeddings.py:134`, `src/mindroom/embeddings.py:143`, and `src/mindroom/embeddings.py:154` still mirror Agno's method behavior so the subclass can preserve response parsing, usage propagation, logging, and batch fallback semantics while changing only when `dimensions` is sent.
This should be treated as dependency override maintenance, not a source dedupe opportunity.

Proposed generalization:

1. Replace the two public signature functions with one private helper such as `_effective_embedder_signature(provider, model, *, host=None, dimensions=None)` in `src/mindroom/embeddings.py`.
2. Keep `effective_knowledge_embedder_signature` and `effective_mem0_embedder_signature` as thin semantic wrappers if their distinct names help call-site readability.
3. Do not refactor `MindRoomOpenAIEmbedder` further unless Agno exposes a shared request-construction hook upstream.

Risk/tests:

The signature helper refactor is low risk if wrapper names and tuple shape stay unchanged.
Tests to keep focused are `tests/test_embeddings.py` for default/custom dimension behavior and `tests/test_memory_config.py::TestMemoryConfig::test_memory_collection_name_ignores_equivalent_mem0_openai_default_dimensions` for collection-name compatibility.
Changing `MindRoomOpenAIEmbedder` carries higher regression risk because it intentionally shadows dependency behavior; batch fallback and usage propagation should be tested if touched.
