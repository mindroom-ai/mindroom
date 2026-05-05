## Summary

No meaningful duplication found.
`jina_tools` follows the same lightweight Agno toolkit factory pattern used by many `src/mindroom/tools/*.py` modules, but the shared behavior is already centralized in `register_tool_with_metadata` and `get_tool_by_name`.
The remaining repeated code is per-tool lazy import boilerplate plus tool-specific metadata, and a refactor would add indirection without clearly reducing active behavior duplication.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
jina_tools	function	lines 91-95	related-only	jina_tools JinaReaderTools agno.tools.jina def *_tools register_tool_with_metadata web scrape reader search	read src/mindroom/tools/jina.py:91; compared factory pattern in src/mindroom/tools/website.py:35, src/mindroom/tools/firecrawl.py:105, src/mindroom/tools/tavily.py:133, src/mindroom/tools/exa.py:196; checked shared registry behavior in src/mindroom/tool_system/metadata.py:577 and src/mindroom/tool_system/metadata.py:749
```

## Findings

No real duplication requiring a refactor was found for the required symbol.

`src/mindroom/tools/jina.py:91` returns the `JinaReaderTools` class through a lazy import.
This is structurally related to many other tool factories, including `src/mindroom/tools/website.py:35`, `src/mindroom/tools/firecrawl.py:105`, `src/mindroom/tools/tavily.py:133`, and `src/mindroom/tools/exa.py:196`.
Those factories all expose an Agno toolkit class through the metadata registry, but they differ in toolkit class, config fields, dependency list, docs URL, function names, status, and setup type.

The shared behavior of registering metadata and later building tool instances is already centralized in `src/mindroom/tool_system/metadata.py:749` and `src/mindroom/tool_system/metadata.py:577`.
The repeated lazy import body is minimal and appears intentional, because it avoids importing optional tool dependencies until a registered tool is requested.

## Proposed Generalization

No refactor recommended.

A helper that generated these one-line factories would need to encode dynamic imports and type-checking behavior, and would likely make optional dependency handling less explicit.
The current decorator and registry already provide the meaningful shared abstraction.

## Risk/tests

No production changes were made.
If this pattern were ever refactored, tests should cover tool metadata export, optional dependency pre-checking, auto-install fallback, and runtime loading for at least one no-config tool and one API-key-backed web/research tool such as Jina.
