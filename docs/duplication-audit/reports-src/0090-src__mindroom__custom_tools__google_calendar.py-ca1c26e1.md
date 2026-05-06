## Summary

Top duplication candidates for `src/mindroom/custom_tools/google_calendar.py` are the repeated Google OAuth wrapper constructor flow shared with Gmail, Drive, and Sheets, and the exact service-account fallback predicate repeated in all four Google custom tool wrappers.
The Calendar-specific `allow_update` expansion is related to Agno's upstream Calendar behavior, but MindRoom intentionally extends it to additional write tools, so it should be preserved if refactored.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
GoogleCalendarTools	class	lines 26-79	duplicate-found	Google custom tool wrapper classes ScopedOAuthClientMixin ThreadLocalGoogleServiceMixin _oauth_provider _oauth_tool_name	src/mindroom/custom_tools/gmail.py:26; src/mindroom/custom_tools/google_drive.py:30; src/mindroom/custom_tools/google_sheets.py:32; src/mindroom/oauth/client.py:53; src/mindroom/custom_tools/google_service.py:14
GoogleCalendarTools.__init__	method	lines 32-75	duplicate-found	provided_creds credentials_manager runtime_paths _initialize_oauth_client _set_original_auth _wrap_oauth_function_entrypoints allow_update create_event quick_add_event	src/mindroom/custom_tools/gmail.py:32; src/mindroom/custom_tools/google_drive.py:36; src/mindroom/custom_tools/google_sheets.py:38; .venv/lib/python3.13/site-packages/agno/tools/google/calendar.py:51; tests/test_google_calendar_oauth_tool.py:104
GoogleCalendarTools._should_fallback_to_original_auth	method	lines 77-79	duplicate-found	_should_fallback_to_original_auth service_account_path GOOGLE_SERVICE_ACCOUNT_FILE _apply_runtime_original_auth_kwargs	src/mindroom/custom_tools/gmail.py:66; src/mindroom/custom_tools/google_drive.py:95; src/mindroom/custom_tools/google_sheets.py:73; src/mindroom/oauth/client.py:68; src/mindroom/oauth/client.py:251; tests/test_google_calendar_oauth_tool.py:152; tests/test_google_tool_wrappers.py:256
```

## Findings

1. Repeated Google OAuth wrapper initialization across custom Google tools.

`GoogleCalendarTools.__init__` performs the same MindRoom wrapper sequence as `GmailTools.__init__`, `GoogleDriveTools.__init__`, and `GoogleSheetsTools.__init__`: pop `creds`, require an explicit `credentials_manager`, store `runtime_paths` and `_creds_manager`, apply runtime service-account kwargs, initialize scoped OAuth, call the Agno parent constructor, store the upstream `_auth`, and wrap function entrypoints.
The shared behavior appears at `src/mindroom/custom_tools/google_calendar.py:45`, `src/mindroom/custom_tools/gmail.py:45`, `src/mindroom/custom_tools/google_drive.py:44`, and `src/mindroom/custom_tools/google_sheets.py:51`.
Differences to preserve are Calendar's `allow_update` fan-out at `src/mindroom/custom_tools/google_calendar.py:46`, Drive's `max_read_size` coercion and function aliases at `src/mindroom/custom_tools/google_drive.py:48` and `src/mindroom/custom_tools/google_drive.py:66`, Sheets' dashboard kwarg normalization at `src/mindroom/custom_tools/google_sheets.py:52`, and Calendar's post-construction `self.creds = creds` assignment at `src/mindroom/custom_tools/google_calendar.py:71`.

2. Exact service-account fallback predicate is duplicated in all Google wrappers.

`GoogleCalendarTools._should_fallback_to_original_auth` returns the same expression as Gmail, Drive, and Sheets: fallback when `service_account_path` is set or `GOOGLE_SERVICE_ACCOUNT_FILE` is available through `RuntimePaths`.
The duplicated lines are `src/mindroom/custom_tools/google_calendar.py:77`, `src/mindroom/custom_tools/gmail.py:66`, `src/mindroom/custom_tools/google_drive.py:95`, and `src/mindroom/custom_tools/google_sheets.py:73`.
The base mixin already has a default `_should_fallback_to_original_auth` at `src/mindroom/oauth/client.py:251`, but its default returns `_defer_to_original_auth`; because `_apply_runtime_original_auth_kwargs` already resolves `GOOGLE_SERVICE_ACCOUNT_FILE` into `service_account_path` at `src/mindroom/oauth/client.py:68`, the repeated overrides may be reducible to a single mixin implementation if tests confirm no timing difference.

3. Calendar `allow_update` partially duplicates upstream Agno behavior but intentionally broadens it.

Agno's `GoogleCalendarTools.__init__` maps `allow_update` only to `create_event`, `update_event`, and `delete_event` at `.venv/lib/python3.13/site-packages/agno/tools/google/calendar.py:96`.
MindRoom's wrapper maps the same `allow_update` setting to those three plus `quick_add_event`, `move_event`, and `respond_to_event` at `src/mindroom/custom_tools/google_calendar.py:46`.
This is related behavior rather than a refactor target by itself because `tests/test_google_calendar_oauth_tool.py:104` and `tests/test_google_calendar_oauth_tool.py:123` assert that the broader MindRoom gate disables or enables all six write methods.

## Proposed Generalization

A minimal refactor would keep service-specific preprocessing in each wrapper and move only the common OAuth constructor tail into `ScopedOAuthClientMixin`, for example a helper that accepts `runtime_paths`, `credentials_manager`, `worker_target`, `kwargs`, `logger`, and the upstream auth descriptor, then returns the credentials to pass or assign after the Agno constructor.
For the fallback predicate, prefer a single implementation in `ScopedOAuthClientMixin` if tests confirm `_apply_runtime_original_auth_kwargs` always runs before auth selection for these wrappers.
No broad architecture change is recommended; the current duplication is real but limited to four wrappers with small service-specific differences.

## Risk/tests

The main behavior risk is constructor ordering: Agno parent constructors register functions and may call or mutate `creds`, `service_account_path`, `delegated_user`, and toolkit function metadata differently per service.
Tests to run for any future refactor should include `tests/test_google_calendar_oauth_tool.py`, `tests/test_google_drive_oauth_tool.py`, `tests/test_google_sheets_oauth_tool.py`, `tests/test_gmail_tools.py`, and `tests/test_google_tool_wrappers.py`.
No production code was changed for this audit.
