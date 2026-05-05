Summary: No meaningful duplication found.

The only behavior symbol in `src/mindroom/tools/google_calendar.py` is the registered factory function that lazily imports and returns `GoogleCalendarTools`.
Several other tool registration modules use the same lazy factory shape, especially Google OAuth-backed tools, but this is consistent registry boilerplate rather than duplicated calendar logic worth extracting.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
google_calendar_tools	function	lines 73-77	related-only	google_calendar_tools; def .*_tools; GoogleCalendarTools; Google Drive/Sheets/Gmail tool factory lazy imports	src/mindroom/tools/google_calendar.py:73; src/mindroom/tools/google_drive.py:82; src/mindroom/tools/google_sheets.py:85; src/mindroom/tools/gmail.py:139; src/mindroom/tools/__init__.py:72; src/mindroom/tools/__init__.py:192
```

Findings:

No real duplicate behavior found for `google_calendar_tools`.
The function is a minimal registry entrypoint: it imports `mindroom.custom_tools.google_calendar.GoogleCalendarTools` inside the function and returns that class.
Related functions in `src/mindroom/tools/google_drive.py:82`, `src/mindroom/tools/google_sheets.py:85`, and `src/mindroom/tools/gmail.py:139` use the same lazy import and return-class pattern for their own custom toolkits.
Those functions differ only by toolkit class and registry metadata, and the shared behavior is already implicit in `register_tool_with_metadata` plus the tools package export list.
Extracting a helper for a two-line lazy factory would likely obscure the simple registration pattern without reducing meaningful implementation duplication.

Proposed generalization:

No refactor recommended.
If the project later adds generated tool-registration modules or many more custom Google toolkit factories, a small metadata-driven registration helper could be considered, but the current file does not justify it.

Risk/tests:

No production change was made.
If this area is refactored later, tests should verify that `google_calendar_tools()` still returns `GoogleCalendarTools`, that metadata for `google_calendar` remains registered with the same auth provider and function names, and that imports remain lazy enough not to require Google optional dependencies during registry import.
