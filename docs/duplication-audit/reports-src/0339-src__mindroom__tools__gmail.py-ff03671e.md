Summary: `gmail_tools` duplicates the repository-wide metadata-registered tool factory pattern, especially the Google OAuth tool factories for Calendar, Drive, and Sheets.
The duplication is intentional and low risk because each module carries service-specific metadata, config fields, function names, and toolkit imports.
No meaningful refactor is recommended for this single factory.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
gmail_tools	function	lines 139-143	related-only	gmail_tools, register_tool_with_metadata, Google OAuth tool factories, return GmailTools	src/mindroom/tools/google_calendar.py:20, src/mindroom/tools/google_calendar.py:73, src/mindroom/tools/google_drive.py:20, src/mindroom/tools/google_drive.py:82, src/mindroom/tools/google_sheets.py:20, src/mindroom/tools/google_sheets.py:85, src/mindroom/tools/email.py:13, src/mindroom/tools/email.py:70, src/mindroom/custom_tools/gmail.py:26
```

Findings:

1. Metadata-registered toolkit factories repeat the same wrapper shape.
   `src/mindroom/tools/gmail.py:20` registers service metadata, and `src/mindroom/tools/gmail.py:139` lazily imports and returns `GmailTools`.
   The same behavior appears in `src/mindroom/tools/google_calendar.py:20` plus `src/mindroom/tools/google_calendar.py:73`, `src/mindroom/tools/google_drive.py:20` plus `src/mindroom/tools/google_drive.py:82`, and `src/mindroom/tools/google_sheets.py:20` plus `src/mindroom/tools/google_sheets.py:85`.
   These modules all expose a decorated zero-argument factory whose runtime behavior is to make a toolkit class discoverable through metadata while deferring the heavy/custom toolkit import until factory call time.
   Differences to preserve are the tool name, display metadata, category, OAuth provider, icon, config fields, dependencies, docs URL, function name list, and returned toolkit class.

2. Gmail is related to, but not duplicated with, the generic SMTP email tool.
   `src/mindroom/tools/email.py:13` and `src/mindroom/tools/email.py:70` use the same metadata/factory convention, but the behavior differs because it configures Agno's SMTP `EmailTools` with API-key style fields instead of OAuth-scoped Gmail API access.
   This is pattern reuse rather than a shared Gmail behavior duplicate.

Proposed generalization:

No refactor recommended.
The repeated factory body is only a lazy import plus class return, and the metadata payload is intentionally service-specific.
Introducing a shared factory helper would save only a few lines while making type annotations, import locality, and metadata scanning less direct.

Risk/tests:

No code changes are proposed.
If this pattern is generalized in the future, tests should verify metadata registration for `gmail`, lazy import behavior, managed init args, function name exposure, and successful instantiation of `mindroom.custom_tools.gmail.GmailTools` with scoped OAuth credentials.
