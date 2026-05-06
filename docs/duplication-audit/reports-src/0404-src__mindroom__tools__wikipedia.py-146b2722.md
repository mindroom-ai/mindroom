## Summary

No meaningful duplication found.

`src/mindroom/tools/wikipedia.py` only registers metadata for the Agno Wikipedia toolkit and exposes a lazy-import factory.
That factory shape is repeated across many `src/mindroom/tools/*` wrappers, but the repeated behavior is registry boilerplate rather than Wikipedia-specific logic worth extracting from this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
wikipedia_tools	function	lines 42-46	related-only	wikipedia_tools WikipediaTools search_wikipedia search_wikipedia_and_update_knowledge_base lazy import return Tools register_tool_with_metadata	src/mindroom/tools/wikipedia.py:42; src/mindroom/tools/arxiv.py:56; src/mindroom/tools/newspaper4k.py:56; src/mindroom/tools/duckduckgo.py:100; src/mindroom/tools/googlesearch.py:91; src/mindroom/tools/hackernews.py:49; src/mindroom/tools/pubmed.py:65; src/mindroom/tools/website.py:37
```

## Findings

No real duplication of Wikipedia-specific behavior was found under `./src`.

The closest related pattern is the common tool-wrapper structure:
`src/mindroom/tools/wikipedia.py:13` registers metadata, and `src/mindroom/tools/wikipedia.py:42` lazily imports and returns `WikipediaTools`.
The same wrapper pattern appears in sibling Agno toolkit modules such as `src/mindroom/tools/arxiv.py:56`, `src/mindroom/tools/newspaper4k.py:56`, `src/mindroom/tools/duckduckgo.py:100`, `src/mindroom/tools/hackernews.py:49`, `src/mindroom/tools/pubmed.py:65`, and `src/mindroom/tools/website.py:37`.
This is structurally repetitive, but each module carries distinct metadata, dependencies, docs URLs, config fields, and function names.

`src/mindroom/tools/googlesearch.py:91` uses the same registration/factory entry point shape but intentionally adds a subclass to force the WebSearch backend to Google.
That difference should be preserved and makes a generic extraction less attractive without a wider registry redesign.

## Proposed Generalization

No refactor recommended.

A future broad cleanup could introduce a small declarative helper for simple Agno toolkit wrappers, but this file alone does not justify it.
Any such helper would need to preserve per-tool metadata, optional dependency declarations, static type imports, and special cases like `googlesearch_tools`.

## Risk/Tests

The main risk of generalizing this pattern is changing tool registration metadata or delaying optional dependency import failures differently.
If a future refactor is attempted, tests should verify that the `wikipedia` tool metadata still exposes `knowledge`, `all`, dependency `wikipedia`, and function names `search_wikipedia` and `search_wikipedia_and_update_knowledge_base`.
Existing registry/tool-loading tests should also confirm that the lazy factory still returns `agno.tools.wikipedia.WikipediaTools`.
