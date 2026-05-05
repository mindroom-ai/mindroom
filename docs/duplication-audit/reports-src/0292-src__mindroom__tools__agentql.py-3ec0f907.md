## Summary

No meaningful duplication found for the AgentQL-specific `playwright_stealth` compatibility shim.
The only related repeated behavior is the common `@register_tool_with_metadata` plus lazy Agno toolkit import pattern used across `src/mindroom/tools`, but that is already the local convention for per-tool metadata and does not justify a refactor from this module alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_ensure_agentql_playwright_stealth_compat	function	lines 18-24	none-found	playwright_stealth StealthConfig stealth_async stealth_sync importlib.import_module vars keys	src/mindroom/tools/agentql.py:18; src/mindroom/tools/openbb.py:15; src/mindroom/model_loading.py:79; src/mindroom/api/auth.py:315
_install_agentql_playwright_stealth_compat	function	lines 27-50	none-found	playwright_stealth StealthConfig BrowserType Properties enabled_scripts namespace add_init_script set_extra_http_headers	src/mindroom/tools/agentql.py:27; src/mindroom/custom_tools/browser.py:999; src/mindroom/custom_tools/browser.py:1003
_install_agentql_playwright_stealth_compat.<locals>._combine_scripts	nested_function	lines 32-35	none-found	enabled_scripts Properties BrowserType CHROME combine_scripts newline join stealth scripts	src/mindroom/tools/agentql.py:32; none outside primary file
_install_agentql_playwright_stealth_compat.<locals>._stealth_async	nested_async_function	lines 37-40	related-only	set_extra_http_headers add_init_script async playwright Page stealth_async	src/mindroom/tools/agentql.py:37; src/mindroom/custom_tools/browser.py:278; src/mindroom/custom_tools/browser.py:999; src/mindroom/custom_tools/browser.py:1003
_install_agentql_playwright_stealth_compat.<locals>._stealth_sync	nested_function	lines 42-45	related-only	set_extra_http_headers add_init_script sync playwright Page stealth_sync	src/mindroom/tools/agentql.py:42; src/mindroom/custom_tools/browser.py:278; src/mindroom/custom_tools/browser.py:999; src/mindroom/custom_tools/browser.py:1003
agentql_tools	function	lines 103-109	related-only	def *_tools register_tool_with_metadata AgentQLTools lazy import return Tools	src/mindroom/tools/agentql.py:103; src/mindroom/tools/crawl4ai.py:98; src/mindroom/tools/browserbase.py:107; src/mindroom/tools/firecrawl.py:105; src/mindroom/tools/scrapegraph.py:91; src/mindroom/tools/website.py:35; src/mindroom/tools/openbb.py:116
```

## Findings

No real duplicated implementation was found for the AgentQL compatibility shim.
`src/mindroom/tools/agentql.py:18` conditionally patches legacy exports into the imported `playwright_stealth` module, and searches for `StealthConfig`, `stealth_async`, `stealth_sync`, `enabled_scripts`, `add_init_script`, and `set_extra_http_headers` found no equivalent shim elsewhere under `src`.
`src/mindroom/custom_tools/browser.py:999` and `src/mindroom/custom_tools/browser.py:1003` use Playwright, but they launch and manage persistent browser contexts rather than patching stealth APIs or composing stealth scripts.

`agentql_tools` follows the repeated tool configuration pattern used by many wrappers.
Examples include `src/mindroom/tools/crawl4ai.py:98`, `src/mindroom/tools/browserbase.py:107`, `src/mindroom/tools/firecrawl.py:105`, `src/mindroom/tools/scrapegraph.py:91`, and `src/mindroom/tools/website.py:35`.
These functions all return an Agno toolkit class after local metadata registration, but the metadata fields differ enough that the current explicit modules remain clearer than introducing a generic factory.

`src/mindroom/tools/openbb.py:15` is related because it wraps a lazy import with package-specific setup/cleanup before returning the toolkit class at `src/mindroom/tools/openbb.py:116`.
That behavior is similar in shape to `agentql_tools` calling `_ensure_agentql_playwright_stealth_compat()` before importing `AgentQLTools`, but the package-specific side effects differ and should stay local unless more tools need dependency compatibility hooks.

## Proposed Generalization

No refactor recommended.
If another Agno tool later needs a package compatibility hook before lazy import, the smallest useful helper would be a local `src/mindroom/tools/import_hooks.py` function that runs a named pre-import callback before importing a toolkit class.
With only AgentQL and OpenBB having distinct package-specific import side effects, extracting that now would add indirection without removing meaningful duplication.

## Risk/Tests

Changing the AgentQL shim would risk breaking environments where `crawl4ai` installs `playwright-stealth` 2.x while AgentQL still imports legacy exports.
Any future refactor should include a focused unit test that supplies a fake `playwright_stealth` module without `StealthConfig`, `stealth_async`, or `stealth_sync`, calls `_ensure_agentql_playwright_stealth_compat()`, and verifies the three exports are installed.
No production code was edited for this audit.
