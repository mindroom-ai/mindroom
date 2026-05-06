## Summary

No meaningful duplication found.
The only required symbol, `google_sheets_tools`, follows the standard MindRoom tool registration factory pattern used by many modules, but its behavior is intentionally per-tool metadata binding plus lazy toolkit import rather than a duplicated business flow worth extracting.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
google_sheets_tools	function	lines 85-89	related-only	google_sheets_tools; def .*_tools; Google Sheets; register_tool_with_metadata; GoogleDriveTools; GoogleCalendarTools; GoogleBigQueryTools	src/mindroom/tools/google_drive.py:82; src/mindroom/tools/google_calendar.py:73; src/mindroom/tools/google_bigquery.py:89; src/mindroom/tools/gmail.py:139; src/mindroom/tools/__init__.py:75
```

## Findings

No real duplication requiring refactor was found for `google_sheets_tools`.

Related pattern:
`src/mindroom/tools/google_sheets.py:85` lazily imports `GoogleSheetsTools` and returns the class after `register_tool_with_metadata` attaches the Google Sheets metadata.
The same lightweight factory shape appears in `src/mindroom/tools/google_drive.py:82`, `src/mindroom/tools/google_calendar.py:73`, `src/mindroom/tools/google_bigquery.py:89`, and `src/mindroom/tools/gmail.py:139`.
These are functionally related because each function is a registered factory that returns a toolkit class, but the meaningful behavior lives in each module's decorator arguments and target toolkit class.
Extracting the four-line function body would not reduce meaningful duplication because each decorator still needs distinct metadata, dependencies, auth provider, config fields, and function names.

## Proposed Generalization

No refactor recommended.
A generic factory helper would obscure the existing registration convention while saving only a lazy import and return statement per tool module.

## Risk/Tests

No production code was changed.
If this convention is refactored in the future, tests should cover tool registry discovery, metadata generation, lazy imports for optional dependencies, and config loading for Google Sheets, Google Drive, Google Calendar, and Gmail.
