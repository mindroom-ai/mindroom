## Summary

`brandfetch_tools` duplicates the standard MindRoom tool-registration factory pattern used across many `src/mindroom/tools/*.py` modules: a metadata-decorated function lazily imports one Agno toolkit class and returns the class object.
This is real structural duplication, but it is intentionally lightweight and keeps optional Agno dependencies import-safe.
No Brandfetch-specific behavior is duplicated elsewhere in `./src`; `src/mindroom/tools_metadata.json` is a generated metadata mirror, not an independent implementation.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
brandfetch_tools	function	lines 85-89	duplicate-found	brandfetch; BrandfetchTools; return *Tools; register_tool_with_metadata lazy toolkit factory	src/mindroom/tools/cartesia.py:77; src/mindroom/tools/cartesia.py:81; src/mindroom/tools/lumalabs.py:77; src/mindroom/tools/lumalabs.py:81; src/mindroom/tools/csv.py:91; src/mindroom/tools/csv.py:95; src/mindroom/tools/openweather.py:77; src/mindroom/tools/openweather.py:81; src/mindroom/tools_metadata.json:8645
```

## Findings

### 1. Repeated lazy Agno toolkit-class factory

- Primary: `src/mindroom/tools/brandfetch.py:85` to `src/mindroom/tools/brandfetch.py:89`
- Similar candidates: `src/mindroom/tools/cartesia.py:77` to `src/mindroom/tools/cartesia.py:81`, `src/mindroom/tools/lumalabs.py:77` to `src/mindroom/tools/lumalabs.py:81`, `src/mindroom/tools/csv.py:91` to `src/mindroom/tools/csv.py:95`, `src/mindroom/tools/openweather.py:77` to `src/mindroom/tools/openweather.py:81`

Each function is a metadata-decorated factory with the same behavioral shape: defer importing an optional Agno toolkit until the factory is called, then return the toolkit class.
The only differences are the imported module path, returned class, type annotation, docstring, and surrounding registration metadata.

The decorator in `src/mindroom/tool_system/metadata.py:800` to `src/mindroom/tool_system/metadata.py:839` stores these factories in metadata, so the behavior is active and central to registration.
However, the duplication is extremely small and keeps each tool module explicit.

### 2. Generated Brandfetch metadata mirror

- Primary metadata source: `src/mindroom/tools/brandfetch.py:13` to `src/mindroom/tools/brandfetch.py:84`
- Mirror: `src/mindroom/tools_metadata.json:8645` to `src/mindroom/tools_metadata.json:8755`

The JSON contains Brandfetch config fields, docs URL, helper text, and tool name that mirror the Python registration metadata.
This is related duplication only if the JSON is generated and refreshed from code, which appears consistent with its role as a registry artifact.
It should not be treated as a second source of truth without checking the metadata generation workflow.

## Proposed Generalization

No refactor recommended for `brandfetch_tools`.

A possible small helper such as `lazy_toolkit_factory("agno.tools.brandfetch", "BrandfetchTools")` would reduce two body lines per tool, but it would weaken static typing, obscure direct imports, and provide little maintenance value for this file.
The current explicit factory pattern is clearer and safer for optional dependencies.

If the wider tools package is later generated or templated, this module could participate in that broader cleanup, but Brandfetch alone does not justify a code change.

## Risk/tests

No production code changes were made.

If a future refactor introduces a shared lazy factory, tests should verify:

- tool registration still records `factory` through `register_tool_with_metadata`;
- optional dependencies remain lazy and do not import during `import mindroom.tools`;
- `get_tool_by_name` or the equivalent runtime path can instantiate Brandfetch with saved config;
- generated `tools_metadata.json` remains synchronized with Python metadata.
