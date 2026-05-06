Summary: `pandas_tools` follows the same metadata-decorated lazy toolkit factory pattern used by most files in `src/mindroom/tools`, especially adjacent data/productivity tool modules.
No meaningful production duplication unique to Pandas was found beyond this repeated registration wrapper shape.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
pandas_tools	function	lines 49-53	related-only	pandas_tools; PandasTools; def *_tools() -> type; register_tool_with_metadata; data/productivity toolkit factories	src/mindroom/tools/csv.py:91; src/mindroom/tools/duckdb.py:78; src/mindroom/tools/sql.py:119; src/mindroom/tools/yfinance.py:108; src/mindroom/tools/__init__.py:97
```

## Findings

No real duplication requiring refactor was found.

`src/mindroom/tools/pandas.py:49` is a tiny factory that lazy-imports `agno.tools.pandas.PandasTools` and returns the class after `register_tool_with_metadata` has registered Pandas-specific metadata.
This is structurally related to `src/mindroom/tools/csv.py:91`, `src/mindroom/tools/duckdb.py:78`, `src/mindroom/tools/sql.py:119`, and `src/mindroom/tools/yfinance.py:108`, which each perform the same lazy-import and return-class wrapper for their Agno toolkit.
The duplicated behavior is limited to the conventional factory shape, while the metadata decorator payloads, dependencies, function names, and target toolkit classes are tool-specific.

## Proposed Generalization

No refactor recommended.

A generic factory helper could theoretically reduce three lines per tool module, but it would obscure the explicit lazy import and type annotations for little gain.
The repeated shape is an intentional registry convention and is clearer left local to each tool file.

## Risk/Tests

No production code was changed.
If this pattern were generalized in the future, tests should cover registry loading, optional dependency import timing, metadata export for `pandas`, and runtime construction of `PandasTools` through configured tool loading.
