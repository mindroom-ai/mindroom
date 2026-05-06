## Summary

No meaningful duplication found.
`src/mindroom/hooks/enrichment.py` is the only source module that renders `EnrichmentItem` values into `<mindroom_message_context>` or `<mindroom_system_context>` blocks.
The closest related code is XML-like prompt/history serialization in `src/mindroom/history/compaction.py` and transient context removal in `src/mindroom/history/storage.py`, but those operate on different data models and do not duplicate enrichment item rendering or cache-policy ordering.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_render_items	function	lines 14-27	related-only	_render_items, EnrichmentItem, cache_policy, <item key=, html.escape, XML item rendering	src/mindroom/hooks/context.py:397, src/mindroom/hooks/context.py:417, src/mindroom/agent_run_context.py:16, src/mindroom/history/compaction.py:1418, src/mindroom/history/compaction.py:1430, src/mindroom/history/compaction.py:1439
render_enrichment_block	function	lines 30-35	none-found	render_enrichment_block, mindroom_message_context, message enrichment block, model_prompt enrichment	src/mindroom/turn_policy.py:133, src/mindroom/turn_policy.py:177
render_system_enrichment_block	function	lines 38-47	related-only	render_system_enrichment_block, mindroom_system_context, system enrichment, stable volatile ordering, transient_system_context	src/mindroom/ai.py:195, src/mindroom/ai.py:779, src/mindroom/teams.py:1471, src/mindroom/teams.py:1582, src/mindroom/response_runner.py:1360, src/mindroom/response_runner.py:1558, src/mindroom/history/storage.py:147, src/mindroom/history/storage.py:231
```

## Findings

No real duplication found.

Related behavior checked:

- `src/mindroom/hooks/context.py:397` and `src/mindroom/hooks/context.py:417` both collect `EnrichmentItem` instances, but they intentionally expose different hook APIs: message metadata via `add_metadata` and system instructions via `add_instruction`.
  They do not render or order items.
- `src/mindroom/agent_run_context.py:16` appends a knowledge-availability `EnrichmentItem`, but it delegates rendering to `render_system_enrichment_block` callers.
  This is producer-side behavior, not duplicate formatting.
- `src/mindroom/history/compaction.py:1418`, `src/mindroom/history/compaction.py:1430`, and `src/mindroom/history/compaction.py:1439` build XML-like prompt compaction records with escaped attributes/content.
  The similarity is limited to XML-like string assembly and HTML escaping.
  It serializes Agno run/message history, not hook enrichment items, and does not share the same tags, cache policy semantics, or stable/volatile ordering.
- `src/mindroom/history/storage.py:147` and `src/mindroom/history/storage.py:231` remove transient system context after a run.
  This consumes the rendered string produced by `render_system_enrichment_block`, but does not duplicate rendering logic.

## Proposed Generalization

No refactor recommended.
The enrichment renderer is already centralized and small.
Extracting a generic XML tag builder would add abstraction for only superficial similarity with history compaction and would risk changing prompt formatting.

## Risk/Tests

No production-code changes were made.
If this renderer is changed later, focused tests should cover:

- Empty message and system enrichment return `""`.
- Item keys and cache policies are escaped as quoted attributes.
- Item text is escaped as content.
- System enrichment sorts stable items by key before volatile items by key.
- Message enrichment preserves input item order.
