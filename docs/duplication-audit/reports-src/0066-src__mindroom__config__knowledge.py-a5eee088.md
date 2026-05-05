Summary: The main duplication candidate is shared/private knowledge chunking configuration and validation between `KnowledgeBaseConfig` and `AgentPrivateKnowledgeConfig`.
`KnowledgeGitConfig` is already reused by private knowledge config, so no Git schema duplication was found.
`normalize_extensions` appears specific to shared knowledge bases and has no matching normalization helper elsewhere under `src/mindroom`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
KnowledgeGitConfig	class	lines 8-44	related-only	KnowledgeGitConfig git repo_url branch poll_interval_seconds sync_timeout_seconds include_patterns exclude_patterns credentials_service	src/mindroom/config/agent.py:81; src/mindroom/config/main.py:321; src/mindroom/knowledge/manager.py:289; src/mindroom/knowledge/watch.py:104
KnowledgeBaseConfig	class	lines 47-98	duplicate-found	KnowledgeBaseConfig AgentPrivateKnowledgeConfig path watch chunk_size chunk_overlap git include_extensions exclude_extensions	src/mindroom/config/agent.py:56; src/mindroom/config/main.py:1261; src/mindroom/knowledge/manager.py:300; src/mindroom/knowledge/manager.py:1314
KnowledgeBaseConfig.normalize_extensions	method	lines 80-90	none-found	normalize_extensions include_extensions exclude_extensions stripped lower startswith dot suffix lower	src/mindroom/knowledge/manager.py:587; src/mindroom/knowledge/manager.py:591; src/mindroom/knowledge/manager.py:1306
KnowledgeBaseConfig.validate_chunking	method	lines 93-98	duplicate-found	validate_chunking chunk_overlap chunk_size smaller than chunk size	src/mindroom/config/agent.py:98
```

## Findings

1. `KnowledgeBaseConfig` duplicates the private knowledge chunking subset in `AgentPrivateKnowledgeConfig`.
   `src/mindroom/config/knowledge.py:55` and `src/mindroom/config/knowledge.py:60` define `chunk_size` and `chunk_overlap` with the same defaults and bounds as `src/mindroom/config/agent.py:71` and `src/mindroom/config/agent.py:76`.
   Both models also carry `watch` and `git` fields with near-identical operational meaning.
   The behavior is active because `Config.get_knowledge_base_config()` converts private knowledge into a `KnowledgeBaseConfig` at `src/mindroom/config/main.py:1261`, and indexing consumes the unified fields in `src/mindroom/knowledge/manager.py:300` and `src/mindroom/knowledge/manager.py:1314`.
   Differences to preserve: shared knowledge bases have a required/default filesystem `path`, include/exclude extension filters, and a shared-folder watch description; private knowledge has `enabled`, optional private-root-relative `path`, and private path validation.

2. `KnowledgeBaseConfig.validate_chunking()` duplicates `AgentPrivateKnowledgeConfig.validate_chunking()`.
   `src/mindroom/config/knowledge.py:93` rejects `chunk_overlap >= chunk_size`; `src/mindroom/config/agent.py:98` performs the same comparison and raises a private-field-specific error.
   Differences to preserve: the shared config error mentions `chunk_overlap` and `chunk_size`, while the private config error includes `private.knowledge.*` field names.

No meaningful duplication was found for `KnowledgeGitConfig`.
The same model is imported and reused by `AgentPrivateKnowledgeConfig` at `src/mindroom/config/agent.py:81`, so the Git configuration is already centralized.

No meaningful duplication was found for `normalize_extensions`.
The only similar code found was extension consumption in `src/mindroom/knowledge/manager.py:587` and `src/mindroom/knowledge/manager.py:591`, which relies on already-normalized config values rather than repeating the same normalization.

## Proposed Generalization

A minimal refactor would introduce a tiny shared helper such as `validate_knowledge_chunking(chunk_size: int, chunk_overlap: int, *, field_prefix: str = "") -> None` in `src/mindroom/config/knowledge.py`.
Both model validators could call it while preserving their existing error wording by passing a prefix or full message.

If reducing schema duplication is desired later, consider a small mixin/base model for only `watch`, `chunk_size`, `chunk_overlap`, and `git`.
That should be weighed carefully because `AgentPrivateKnowledgeConfig` has private-root path behavior and `KnowledgeBaseConfig` has extension filters, so a validation helper is the safer first step.

## Risk/tests

Risk is low for extracting only the chunking validator, but Pydantic validator behavior and error text could change if the helper is too broad.
Tests should cover valid and invalid `KnowledgeBaseConfig` chunk settings and valid and invalid `AgentPrivateKnowledgeConfig` chunk settings, including preserving the private error message context.
No production code was edited.
