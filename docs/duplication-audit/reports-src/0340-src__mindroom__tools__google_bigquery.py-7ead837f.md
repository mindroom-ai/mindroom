Summary: No meaningful duplication found for BigQuery-specific behavior.
The primary symbol is a small deferred-import adapter that follows the same local tool registration pattern used by many files under `src/mindroom/tools`, but extracting this two-line body would add indirection without reducing meaningful maintenance cost.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
google_bigquery_tools	function	lines 89-93	related-only	google_bigquery BigQuery GoogleBigQueryTools; def *_tools; agno toolkit deferred import; database toolkit wrappers	src/mindroom/tools/google_bigquery.py:89; src/mindroom/tools/google_sheets.py:85; src/mindroom/tools/google_drive.py:82; src/mindroom/tools/google_calendar.py:73; src/mindroom/tools/postgres.py:85; src/mindroom/tools/redshift.py:145
```

Findings:

- `google_bigquery_tools` in `src/mindroom/tools/google_bigquery.py:89` duplicates the common tool-module adapter shape used in `src/mindroom/tools/google_sheets.py:85`, `src/mindroom/tools/google_drive.py:82`, `src/mindroom/tools/google_calendar.py:73`, `src/mindroom/tools/postgres.py:85`, and `src/mindroom/tools/redshift.py:145`.
  Each function performs a local import of the toolkit class and returns the class object so metadata registration can expose the toolkit without importing optional dependencies at module import time.
  This is functionally related boilerplate rather than a BigQuery-specific duplicate behavior.
- The BigQuery metadata itself is specific to its Agno toolkit: dataset, project, location, optional credentials, BigQuery operation toggles, `google-cloud-bigquery` dependency, and BigQuery function names.
  I did not find another source module under `src` that independently implements BigQuery table listing, schema description, SQL execution, or BigQuery credential/config transformation.

Proposed generalization:

No refactor recommended.
The repeated adapter body is intentionally local and keeps optional dependency imports isolated.
A shared helper such as `return_toolkit("agno.tools.google.bigquery", "GoogleBigQueryTools")` would reduce two lines per module but would weaken type clarity and make the registration files harder to scan.

Risk/tests:

- No production code was changed.
- If this pattern were ever generalized, tests should cover metadata discovery/import behavior with optional tool dependencies absent, because the main behavior to preserve is deferred import failure isolation.
- No BigQuery-specific tests appear necessary from this audit alone because no duplicated BigQuery implementation was found.
