## Summary

The main duplication candidate is the constructor-forwarding wrapper around `agno.tools.websearch.WebSearchTools`.
`src/mindroom/tools/googlesearch.py` locally defines the same kind of backend-pinning convenience subclass that Agno already provides for DuckDuckGo and that MindRoom exposes through `src/mindroom/tools/duckduckgo.py`.
No exact production-code duplicate of the Google wrapper was found under `./src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
googlesearch_tools	function	lines 91-125	related-only	WebSearchTools backend google googlesearch_tools duckduckgo_tools search_news web_search	src/mindroom/tools/duckduckgo.py:98; src/mindroom/tools/baidusearch.py:84; src/mindroom/tools/serpapi.py:56; src/mindroom/tools/serper.py:98; src/mindroom/tools/searxng.py:56
googlesearch_tools.<locals>.__init__	nested_function	lines 98-123	duplicate-found	super().__init__ enable_search enable_news backend fixed_max_results timelimit region proxy timeout verify_ssl	.venv/lib/python3.13/site-packages/agno/tools/duckduckgo.py:26; src/mindroom/tools/duckduckgo.py:98
```

## Findings

### WebSearch backend wrapper constructor duplication

- Primary behavior: `src/mindroom/tools/googlesearch.py:91` returns a local `GoogleSearchTools` subclass whose `__init__` forwards `enable_search`, `enable_news`, `modifier`, `fixed_max_results`, `proxy`, `timeout`, `verify_ssl`, `timelimit`, `region`, and `**kwargs` to `WebSearchTools`, while hard-coding `backend="google"` at `src/mindroom/tools/googlesearch.py:111`.
- Related behavior: Agno's installed `DuckDuckGoTools` wrapper does the same constructor forwarding to `WebSearchTools`, with a default backend of `"duckduckgo"` and an optional backend override in `.venv/lib/python3.13/site-packages/agno/tools/duckduckgo.py:26`.
- MindRoom exposes that Agno wrapper directly from `src/mindroom/tools/duckduckgo.py:98`, and its metadata fields overlap heavily with Google Search: `enable_search`, `enable_news`, `modifier`, `fixed_max_results`, `timelimit`, `region`, `proxy`, `timeout`, and `verify_ssl` at `src/mindroom/tools/duckduckgo.py:23`.
- Difference to preserve: Google Search intentionally omits a configurable `backend` field and always passes `backend="google"`.
DuckDuckGo allows backend variants and adds old method aliases inside the Agno class, so a shared wrapper must not accidentally expose those semantics for Google.

### Related search-tool registration pattern

- `src/mindroom/tools/serpapi.py:56`, `src/mindroom/tools/serper.py:98`, `src/mindroom/tools/searxng.py:56`, and `src/mindroom/tools/baidusearch.py:84` all follow the same metadata-decorated "return the Agno toolkit class" registration shape.
- This is related but not meaningful duplication for this primary file because those modules do not duplicate the local WebSearch subclass behavior.
They register different upstream toolkits with different config surfaces and APIs.

## Proposed Generalization

A narrow helper could be introduced only if another `WebSearchTools` backend wrapper is added in MindRoom.
For example, a private helper in `src/mindroom/tools/websearch_backend.py` could build a subclass factory that forwards the common `WebSearchTools` constructor parameters and pins a backend string.

No immediate refactor is recommended for the current codebase.
There is only one MindRoom-owned backend-pinning subclass, and generalizing it now would add an abstraction for a single local call site.

## Risk/tests

If this is refactored later, tests should instantiate the generated Google toolkit with default and non-default config values and assert that `backend` remains `"google"` while all forwarded attributes match the existing behavior.
Tests should also verify the registered function names remain `web_search` and `search_news`.
The optional `ddgs` dependency is not installed in the current environment, so import-based runtime verification of `WebSearchTools` was not possible; the installed Agno source file was inspected directly instead.
