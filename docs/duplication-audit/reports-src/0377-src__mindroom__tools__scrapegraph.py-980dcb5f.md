Summary: No meaningful duplication found for the primary behavior in `src/mindroom/tools/scrapegraph.py`.
The `scrapegraph_tools` factory follows the same lazy Agno toolkit-class registration pattern used by many tool modules, but that pattern is intentional registry boilerplate rather than duplicated business behavior worth extracting for this file alone.
The closest related duplication is repeated web-scrape tool metadata field construction across neighboring modules, especially API-key fields, `enable_*` booleans, and `all` toggles, but the field names, defaults, and exposed function names differ enough that a ScrapeGraph-specific refactor is not recommended from this audit.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
scrapegraph_tools	function	lines 91-95	related-only	scrapegraph_tools; ScrapeGraphTools; def *_tools return toolkit class; web_scrape ConfigField enable_* all api_key	src/mindroom/tools/scrapegraph.py:91; src/mindroom/tools/firecrawl.py:105; src/mindroom/tools/agentql.py:103; src/mindroom/tools/browserbase.py:107; src/mindroom/tools/brightdata.py:98; src/mindroom/tools/crawl4ai.py:98; src/mindroom/tools/jina.py:91; src/mindroom/tools/spider.py:78; src/mindroom/tools/website.py:35
```

## Findings

No real duplication requiring consolidation was found for `scrapegraph_tools`.
The symbol's behavior is limited to lazily importing and returning `agno.tools.scrapegraph.ScrapeGraphTools` at `src/mindroom/tools/scrapegraph.py:91`.
The same lightweight factory pattern appears in neighboring web-scrape modules such as `src/mindroom/tools/firecrawl.py:105`, `src/mindroom/tools/browserbase.py:107`, `src/mindroom/tools/brightdata.py:98`, `src/mindroom/tools/crawl4ai.py:98`, `src/mindroom/tools/jina.py:91`, `src/mindroom/tools/spider.py:78`, and `src/mindroom/tools/website.py:35`.
This is related boilerplate for lazy dependency loading and registry integration, not duplicated domain logic.

Related-only metadata repetition exists across the web-scrape registration modules.
`src/mindroom/tools/scrapegraph.py:22` defines an optional `api_key` field and several `enable_*` boolean toggles, which resemble fields in `src/mindroom/tools/firecrawl.py:22`, `src/mindroom/tools/agentql.py:62`, `src/mindroom/tools/browserbase.py:22`, `src/mindroom/tools/brightdata.py:22`, `src/mindroom/tools/jina.py:22`, and `src/mindroom/tools/spider.py:22`.
The shared behavior is presenting toolkit constructor/config options as `ConfigField` metadata.
The differences to preserve are each provider's exact option names, default enabled functions, dependency list, docs URL, status/setup type, icon, and `function_names`.
Because the repeated code is declarative metadata and each module's values are provider-specific, extracting a shared helper would likely reduce readability more than it reduces maintenance.

## Proposed Generalization

No refactor recommended for this task.
If future audits find the same metadata-field boilerplate across a much larger active change, the smallest safe helper would be a local metadata helper for common optional fields such as `api_key`, `all`, and `enable_<name>` under `src/mindroom/tool_system/metadata.py` or a focused sibling module.
That helper should only build `ConfigField` instances and must not abstract provider-specific defaults or toolkit registration.

## Risk/Tests

No production code was edited.
If a future metadata helper is introduced, tests should verify generated tool metadata remains byte-for-byte equivalent for affected tools, especially `config_fields`, `function_names`, setup status, dependency declarations, and docs URLs.
The main behavioral risk would be accidentally changing which Agno functions are exposed or which toolkit constructor arguments are passed through user configuration.
