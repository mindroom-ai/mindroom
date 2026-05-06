Summary: No meaningful duplication found.

The primary file centralizes two small adapters around `format_knowledge_availability_notice`.
Other source locations either call these helpers, render the underlying notice text, or perform generic enrichment/prompt assembly that is related but not functionally duplicated.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
append_knowledge_availability_enrichment	function	lines 16-27	related-only	knowledge_availability, append_knowledge_availability_enrichment, EnrichmentItem, cache_policy="volatile", system_enrichment_items	src/mindroom/response_runner.py:1558, src/mindroom/response_runner.py:1651, src/mindroom/teams.py:1578, src/mindroom/teams.py:1974, src/mindroom/custom_tools/delegate.py:109, src/mindroom/hooks/context.py:417, src/mindroom/hooks/enrichment.py:38, src/mindroom/ai.py:779, src/mindroom/teams.py:1471
prepend_knowledge_availability_notice	function	lines 30-36	related-only	prepend_knowledge_availability_notice, knowledge availability, notice prompt, prompt notice, Do not claim to have searched, "\\n\\n{prompt}"	src/mindroom/api/openai_compat.py:1012, src/mindroom/api/openai_compat.py:1607, src/mindroom/api/openai_compat.py:1746, src/mindroom/api/openai_compat.py:715, src/mindroom/knowledge/utils.py:418
```

Findings: No real duplication found.

`append_knowledge_availability_enrichment` is the only implementation that renders knowledge availability into an `EnrichmentItem` with key `knowledge_availability` and volatile cache policy.
The response runner, team paths, and delegate tool call it directly rather than rebuilding the item.
`SystemEnrichContext.add_instruction` in `src/mindroom/hooks/context.py:417` is only a generic hook API for appending caller-supplied enrichment items.
`render_system_enrichment_block` in `src/mindroom/hooks/enrichment.py:38` and its callers in `src/mindroom/ai.py:779` and `src/mindroom/teams.py:1471` render already-materialized enrichment items, so they are adjacent plumbing rather than duplicated notice creation.

`prepend_knowledge_availability_notice` is the only implementation that prefixes a prompt with the formatted knowledge availability notice.
The OpenAI-compatible paths call it directly at `src/mindroom/api/openai_compat.py:1012`, `src/mindroom/api/openai_compat.py:1607`, and `src/mindroom/api/openai_compat.py:1746`.
The only similar prompt-prefix pattern found is `src/mindroom/api/openai_compat.py:715`, which prefixes an explicit system prompt and does not use knowledge availability state.
The actual knowledge notice wording is centralized in `src/mindroom/knowledge/utils.py:418`.

Proposed generalization: No refactor recommended.

The current module already provides the narrow shared adapters for the two required surfaces: system enrichment items and OpenAI-compatible prompt text.
Merging these into a broader abstraction would add indirection without removing duplicated behavior.

Risk/tests: No production changes were made.

If these helpers are later changed, focused tests should cover no-unavailable-bases passthrough, deterministic notice text from `format_knowledge_availability_notice`, enrichment item key/cache policy, and prompt prefix spacing.
