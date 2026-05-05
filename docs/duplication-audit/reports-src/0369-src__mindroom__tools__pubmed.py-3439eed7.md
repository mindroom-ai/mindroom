Summary: `pubmed_tools` duplicates the standard MindRoom Agno toolkit registration/factory shape used by many `src/mindroom/tools/*` modules.
The duplication is structural rather than PubMed-specific: metadata is declared in `register_tool_with_metadata`, and the function lazily imports and returns the Agno toolkit class.
No duplicated PubMed search, parsing, validation, or API behavior was found elsewhere in `./src`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
pubmed_tools	function	lines 63-67	related-only	pubmed_tools; PubmedTools; agno.tools.pubmed; def *_tools() -> type; register_tool_with_metadata search toolkit factories	src/mindroom/tools/pubmed.py:63; src/mindroom/tools/arxiv.py:56; src/mindroom/tools/hackernews.py:49; src/mindroom/tools/serpapi.py:56; src/mindroom/tools/__init__.py:99; src/mindroom/tool_system/metadata.py:915
```

Findings:

1. Repeated toolkit factory wrapper pattern.
`src/mindroom/tools/pubmed.py:63` returns `PubmedTools` through the same lazy-import factory pattern used in `src/mindroom/tools/arxiv.py:56`, `src/mindroom/tools/hackernews.py:49`, and `src/mindroom/tools/serpapi.py:56`.
The shared behavior is registering a tool metadata record at import time while deferring the concrete Agno toolkit import until runtime factory invocation.
The per-tool differences are the Agno import path, return class, metadata values, dependencies, config fields, and function names.
This is related duplication, but it appears to be an intentional registry convention across many small tool modules.

2. No PubMed-specific duplicate implementation found.
Searches for `PubmedTools`, `pubmed`, `fetch_details`, `fetch_pubmed_ids`, `parse_details`, and `search_pubmed` found only the PubMed registration in `src/mindroom/tools/pubmed.py` plus the export in `src/mindroom/tools/__init__.py:99`.
There is no local PubMed API wrapper, parser, validation path, or result transformation duplicated elsewhere under `./src`.

Proposed generalization:

No refactor recommended for `pubmed_tools` alone.
A generic helper could reduce many one-line factories across `src/mindroom/tools/*`, but introducing it for this module would trade a clear established convention for indirection and would require broad coordinated edits outside the scope of this file.
If the project later chooses to address the repeated pattern globally, the minimal shape would be a small `tool_system` helper that builds lazy Agno toolkit factories from an import path and class name, while keeping each module's explicit metadata declaration local.

Risk/tests:

No production code was changed.
If the factory pattern is generalized later, tests should verify that built-in tool registration still occurs on `import mindroom.tools`, lazy imports still avoid importing optional dependencies until factory invocation, metadata remains unchanged for PubMed, and `src/mindroom/tools/__init__.py` continues exporting `pubmed_tools`.
