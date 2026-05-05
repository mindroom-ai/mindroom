## Summary

The primary file contains one behavior symbol: `google_drive_tools`, a metadata-registered lazy factory that returns the MindRoom Google Drive toolkit class.
This factory pattern is duplicated across adjacent Google integration registration modules, especially `google_sheets_tools`, `google_calendar_tools`, and `gmail_tools`.
No production refactor is recommended from this file alone because each call site carries tool-specific metadata, dependencies, OAuth provider names, and config fields.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
google_drive_tools	function	lines 82-86	duplicate-found	google_drive_tools; def *_tools; GoogleDriveTools; register_tool_with_metadata; Google OAuth tool factories	src/mindroom/tools/google_sheets.py:85; src/mindroom/tools/google_calendar.py:73; src/mindroom/tools/gmail.py:139; src/mindroom/tools/google_bigquery.py:89; src/mindroom/tools/google_maps.py:100
```

## Findings

### 1. Repeated lazy toolkit-class factory pattern

`src/mindroom/tools/google_drive.py:82` defines `google_drive_tools`, imports `GoogleDriveTools` inside the function, and returns that class.
The same behavior appears in `src/mindroom/tools/google_sheets.py:85`, `src/mindroom/tools/google_calendar.py:73`, and `src/mindroom/tools/gmail.py:139`, with each function lazily importing and returning the corresponding custom toolkit class.
`src/mindroom/tools/google_bigquery.py:89` and `src/mindroom/tools/google_maps.py:100` use the same factory shape for upstream Agno toolkit classes.

The shared behavior is the tool registry adapter: expose a zero-argument callable decorated by `register_tool_with_metadata`, defer the concrete toolkit import until the callable is invoked, and return the toolkit class object.
The differences to preserve are the decorator metadata, config fields, dependency lists, docs URLs, function-name lists, return type annotations, and the exact toolkit import path.

## Proposed Generalization

No refactor recommended for this module alone.
A possible future cleanup would be a small helper in `mindroom.tools` or `mindroom.tool_system.metadata` that builds lazy toolkit factories from an import path, but it would need to preserve readable per-tool functions and type annotations or it may make registry entries harder to inspect.
Given the duplication is mostly five lines per tool and tightly coupled to per-tool metadata, the current explicit pattern is acceptable.

## Risk/tests

Risk is low if left unchanged.
If a helper is introduced later, tests should cover registry discovery for each affected tool, lazy import behavior, metadata generation, and managed init argument wiring for custom Google OAuth tools.
