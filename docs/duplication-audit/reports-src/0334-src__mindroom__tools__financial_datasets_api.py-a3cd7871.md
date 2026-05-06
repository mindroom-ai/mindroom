## Summary

No meaningful duplication found for active financial behavior.
`financial_datasets_api_tools` follows the same small Agno toolkit factory pattern used across many modules under `src/mindroom/tools`, but this is registry boilerplate rather than a refactor-worthy behavior duplicate in this single file.
The nearest domain overlap is with `src/mindroom/tools/yfinance.py` and `src/mindroom/tools/openbb.py`, which also expose financial-data toolkits, but they wrap different Agno toolkit classes, dependencies, configuration fields, and provider capabilities.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
financial_datasets_api_tools	function	lines 50-54	related-only	financial_datasets_api_tools FinancialDatasetsTools financial datasets stock crypto SEC filings return Agno toolkit class	src/mindroom/tools/financial_datasets_api.py:50; src/mindroom/tools/yfinance.py:108; src/mindroom/tools/openbb.py:116; src/mindroom/tools/duckduckgo.py:98; src/mindroom/tools/linear.py:44
```

## Findings

No real duplication that should be generalized from this primary file.

Related pattern: many tool modules expose a zero-argument function that imports and returns an Agno toolkit class.
Examples include `src/mindroom/tools/financial_datasets_api.py:50`, `src/mindroom/tools/yfinance.py:108`, `src/mindroom/tools/duckduckgo.py:98`, and `src/mindroom/tools/linear.py:44`.
This is repeated behavior in the broad sense, but it is coupled to the decorator metadata on each module and keeps optional toolkit imports lazy.
Generalizing only this factory body would save two lines per tool while adding indirection to many metadata registration modules.

Related domain overlap: `src/mindroom/tools/yfinance.py:13` and `src/mindroom/tools/openbb.py:29` both register financial-data toolkits.
They overlap with `src/mindroom/tools/financial_datasets_api.py:13` on stock/company/financial-statement capabilities such as company info, income statements, stock prices, and news.
The behavior is not duplicated implementation because each module delegates to a different upstream Agno toolkit and preserves different setup semantics: Financial Datasets API requires an API key and `requests`, YFinance has no API key and uses `yfinance`, and OpenBB has its own provider/PAT options plus a custom import helper at `src/mindroom/tools/openbb.py:15`.

## Proposed Generalization

No refactor recommended.
The small factory repetition appears to be an intentional registry convention, and the financial tool overlap reflects different external data providers rather than duplicated local implementation.

## Risk/tests

Refactoring these modules into a shared factory helper would risk changing lazy import timing, type-checking clarity, and decorator registration readability across many tools.
If such a broad cleanup were attempted later, tests should verify tool metadata registration, optional dependency behavior, import failures for unavailable optional toolkits, and constructor/config mapping for `financial_datasets_api`, `yfinance`, and `openbb`.
