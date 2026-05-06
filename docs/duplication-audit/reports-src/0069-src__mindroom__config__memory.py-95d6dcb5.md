## Summary

No meaningful duplication found.
`src/mindroom/config/memory.py` is mostly declarative Pydantic schema for memory-specific knobs.
The only behavioral symbol, `MemoryConfig.normalize_shorthand`, handles a narrow `memory: none` shorthand and has no matching implementation elsewhere under `./src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_MemoryEmbedderConfig	class	lines 14-21	related-only	provider config EmbedderConfig provider config default_factory	src/mindroom/config/models.py:469; src/mindroom/config/models.py:482; src/mindroom/config/voice.py:8
_MemoryLLMConfig	class	lines 24-28	related-only	provider config dict[str, Any] LLM provider voice intelligence model	src/mindroom/config/models.py:482; src/mindroom/config/voice.py:17
_MemoryFileConfig	class	lines 31-46	related-only	file memory path max_entrypoint_lines path Field ge=1	src/mindroom/config/knowledge.py:47; src/mindroom/config/agent.py:56; src/mindroom/workspaces.py:26
_MemoryAutoFlushBatchConfig	class	lines 49-61	related-only	max_sessions_per_cycle max_sessions_per_agent_per_cycle batch ge=1	src/mindroom/config/models.py:27; src/mindroom/config/models.py:52; src/mindroom/mcp/config.py:47
_MemoryAutoFlushContextConfig	class	lines 64-76	related-only	memory_snippets snippet_max_chars include_memory_context ge=0 ge=1	src/mindroom/config/models.py:216; src/mindroom/config/models.py:254; src/mindroom/config/knowledge.py:47
_MemoryAutoFlushExtractorConfig	class	lines 79-104	related-only	no_reply_token max_messages_per_flush max_chars_per_flush timeout include_memory_context	src/mindroom/config/models.py:292; src/mindroom/memory/auto_flush.py:503; src/mindroom/memory/auto_flush.py:826
MemoryAutoFlushConfig	class	lines 107-153	related-only	enabled interval idle ttl cooldown batch extractor config	src/mindroom/config/models.py:27; src/mindroom/config/models.py:52; src/mindroom/mcp/config.py:47; src/mindroom/memory/auto_flush.py:243
MemoryConfig	class	lines 156-188	related-only	MemoryConfig backend embedder llm file auto_flush memory backend none	src/mindroom/config/main.py:377; src/mindroom/config/agent.py:200; src/mindroom/memory/_policy.py:29
MemoryConfig.normalize_shorthand	method	lines 161-165	none-found	normalize_shorthand memory none model_validator before backend none cache_backend none	src/mindroom/config/main.py:423; src/mindroom/config/models.py:130; src/mindroom/config/agent.py:314; src/mindroom/matrix/cache/thread_write_cache_ops.py:48
```

## Findings

No real duplication requiring refactor.

The provider/config wrapper shape in `_MemoryEmbedderConfig` and `_MemoryLLMConfig` is related to `ModelConfig` in `src/mindroom/config/models.py:482` and voice provider settings in `src/mindroom/config/voice.py:8`, but the fields are intentionally different.
Memory embedders use an `EmbedderConfig`, memory LLM settings use an untyped provider-specific dictionary, model configs use `id`, `host`, `api_key`, `extra_kwargs`, and context sizing, and voice STT includes direct model/API/host fields.
A shared provider wrapper would either lose type specificity or add indirection without removing active repeated behavior.

The auto-flush nested classes resemble other grouped operational config containers such as streaming/coalescing in `src/mindroom/config/models.py:27` and MCP server limits in `src/mindroom/mcp/config.py:47`.
This is ordinary Pydantic schema organization, not duplicated behavior.
The fields are memory-worker-specific and are consumed directly by `src/mindroom/memory/auto_flush.py:243`, `src/mindroom/memory/auto_flush.py:503`, and `src/mindroom/memory/auto_flush.py:826`.

`MemoryConfig.normalize_shorthand` is the only normalization behavior in the file.
Other `mode="before"` validators normalize different input forms: root config section defaults in `src/mindroom/config/main.py:423`, tool entries in `src/mindroom/config/models.py:130`, and removed legacy agent fields in `src/mindroom/config/agent.py:314`.
`src/mindroom/matrix/cache/thread_write_cache_ops.py:48` also maps a backend value to a small dictionary, but it is a Matrix cache write result payload rather than config input normalization.

## Proposed Generalization

No refactor recommended.

## Risk/Tests

No production code changes were made.
If future work adds more top-level scalar shorthands like `memory: none`, tests should cover each accepted scalar form at both the nested model level and root `Config.model_validate` level, similar to `tests/test_memory_config.py:463` and `tests/test_memory_config.py:470`.
