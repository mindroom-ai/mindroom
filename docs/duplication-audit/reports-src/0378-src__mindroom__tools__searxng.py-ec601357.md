## Summary

No meaningful duplication found.
`searxng_tools` follows the standard MindRoom tool-registration wrapper pattern used across `src/mindroom/tools`, but the duplicated behavior is intentionally declarative metadata plus a lazy Agno class import.
The nearest related modules are other search tool wrappers, especially DuckDuckGo, Google Search, SerpApi, Baidu Search, and Tavily, but their configuration fields and imported toolkits differ enough that no SearxNG-specific shared behavior is duplicated.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
searxng_tools	function	lines 56-60	related-only	searxng Searxng; def .*_tools; ToolCategory.RESEARCH; fixed_max_results; register_tool_with_metadata	src/mindroom/tools/searxng.py:56; src/mindroom/tools/duckduckgo.py:98; src/mindroom/tools/serpapi.py:56; src/mindroom/tools/googlesearch.py:91; src/mindroom/tools/baidusearch.py:84; src/mindroom/tools/tavily.py:133; src/mindroom/tools/__init__.py:108
```

## Findings

No real duplication requiring refactor was found for the required symbol.

`src/mindroom/tools/searxng.py:56` returns the `agno.tools.searxng.Searxng` class through the repository's normal lazy import wrapper pattern.
Related wrappers in `src/mindroom/tools/duckduckgo.py:98`, `src/mindroom/tools/serpapi.py:56`, `src/mindroom/tools/baidusearch.py:84`, and `src/mindroom/tools/tavily.py:133` perform the same category of behavior for their own Agno toolkit classes.
This is structural consistency rather than actionable duplicated domain behavior because each wrapper is coupled to different metadata, dependencies, docs URLs, config fields, function names, and toolkit class names.

`src/mindroom/tools/googlesearch.py:91` is related but not duplicate because it defines a local subclass to force the Agno WebSearch backend to Google.
That behavior does not overlap with SearxNG's direct class return.

The only other SearxNG references found were the generated/static metadata record in `src/mindroom/tools_metadata.json` and import/export plumbing in `src/mindroom/tools/__init__.py:108` and `src/mindroom/tools/__init__.py:229`.
Those are registry artifacts, not independent implementations of SearxNG behavior.

## Proposed Generalization

No refactor recommended.

A generic helper for "return this Agno toolkit class" could remove a few lines from many modules, but it would make imports and metadata registration less explicit while providing little behavior reuse.
For this file, keeping the direct wrapper is clearer and matches the existing tool module convention.

## Risk/Tests

No production code changes were made.
If a future broad refactor introduces a shared lazy toolkit loader, tests should cover metadata registration, dependency resolution, and tool instantiation for at least one direct wrapper such as SearxNG and one customized wrapper such as Google Search.
