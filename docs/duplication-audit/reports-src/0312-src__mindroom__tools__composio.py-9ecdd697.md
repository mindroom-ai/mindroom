## Summary

No meaningful duplication found.
`composio_tools` shares the common MindRoom tool-registry wrapper shape used by many `src/mindroom/tools/*` modules, but its only extra behavior, calling `disable_vendor_telemetry()` after importing `ComposioToolSet`, appears specific to Composio and not duplicated in another toolkit wrapper.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
composio_tools	function	lines 195-200	related-only	composio_tools ComposioToolSet disable_vendor_telemetry vendor_telemetry_env_values def *_tools return *Tools	src/mindroom/tools/apify.py:46-50; src/mindroom/tools/firecrawl.py:105-109; src/mindroom/tools/openbb.py:15-26; src/mindroom/tools/__init__.py:272-276; src/mindroom/tools/__init__.py:326-330; src/mindroom/tools/shell.py:169; src/mindroom/vendor_telemetry.py:16-26
```

## Findings

No real duplication found for the required symbol.

`src/mindroom/tools/composio.py:195-200` lazily imports `ComposioToolSet`, disables known vendor telemetry, and returns the toolkit class.
Many wrappers in `src/mindroom/tools` perform the same lazy import and return pattern, such as `src/mindroom/tools/apify.py:46-50` and `src/mindroom/tools/firecrawl.py:105-109`, but that pattern is registry boilerplate rather than duplicated domain behavior.

The telemetry behavior is related to `src/mindroom/vendor_telemetry.py:16-26`, and `src/mindroom/tools/shell.py:169` uses `vendor_telemetry_env_values()` for subprocess environment setup.
Those call sites are not functionally duplicate with `composio_tools`: the Composio wrapper mutates process environment and disables already-loaded vendor modules before returning a toolkit, while shell subprocess setup copies opt-out environment values into a child process environment.

`src/mindroom/tools/openbb.py:15-26` is a related wrapper with import-time side-effect control, but it temporarily overrides `OPENBB_AUTO_BUILD` only for importing OpenBB tools and restores the previous value.
That differs materially from Composio's process-wide telemetry opt-out.

## Proposed Generalization

No refactor recommended.
The repeated lazy import and return pattern is widespread but intentionally simple, and extracting a generic helper would likely reduce readability while adding indirection around type checking and metadata registration.
The Composio telemetry call is unique enough to stay local.

## Risk/tests

No production changes were made.
If this area is refactored later, tests should verify that `composio_tools()` calls `disable_vendor_telemetry()` before the returned toolkit is used, and that generic tool registration still resolves `composio` from `src/mindroom/tools/__init__.py`.
