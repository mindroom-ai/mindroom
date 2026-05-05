## Summary

Top duplication candidate: `GmailTools.__init__` repeats the same MindRoom-scoped OAuth wrapper initialization used by Google Calendar, Google Sheets, and Google Drive custom tools.
`GmailTools._should_fallback_to_original_auth` is an exact behavioral duplicate of the fallback override in those same Google wrappers.
The class-level wrapper structure is related to the other Google tool wrappers, but the meaningful shared behavior is the constructor sequence and service-account fallback predicate.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
GmailTools	class	lines 26-68	duplicate-found	GmailTools class ScopedOAuthClientMixin ThreadLocalGoogleServiceMixin Agno Google wrappers	src/mindroom/custom_tools/google_calendar.py:26; src/mindroom/custom_tools/google_sheets.py:32; src/mindroom/custom_tools/google_drive.py:30; src/mindroom/oauth/client.py:53; src/mindroom/custom_tools/google_service.py:14
GmailTools.__init__	method	lines 32-64	duplicate-found	provided_creds kwargs.pop creds credentials_manager _initialize_oauth_client _set_original_auth _wrap_oauth_function_entrypoints	src/mindroom/custom_tools/google_calendar.py:32; src/mindroom/custom_tools/google_sheets.py:38; src/mindroom/custom_tools/google_drive.py:36; src/mindroom/oauth/client.py:68; src/mindroom/oauth/client.py:80; src/mindroom/oauth/client.py:101; src/mindroom/oauth/client.py:105
GmailTools._should_fallback_to_original_auth	method	lines 66-68	duplicate-found	service_account_path GOOGLE_SERVICE_ACCOUNT_FILE _should_fallback_to_original_auth	src/mindroom/custom_tools/google_calendar.py:77; src/mindroom/custom_tools/google_sheets.py:73; src/mindroom/custom_tools/google_drive.py:95; src/mindroom/oauth/client.py:251
```

## Findings

### 1. Google OAuth toolkit wrapper constructor flow is duplicated

`src/mindroom/custom_tools/gmail.py:32` performs the same wrapper setup flow as `src/mindroom/custom_tools/google_sheets.py:38` and `src/mindroom/custom_tools/google_drive.py:36`, with a near-duplicate in `src/mindroom/custom_tools/google_calendar.py:32`.
The common behavior is: pop `creds`, require `credentials_manager`, assign `_runtime_paths` and `_creds_manager`, apply runtime Google original-auth kwargs, initialize the scoped OAuth client, call the Agno parent initializer, store the upstream `_auth` method, and wrap function entrypoints.

Differences to preserve:
Gmail, Sheets, and Drive pass `creds=creds` into `super().__init__`, while Calendar calls `super().__init__(**kwargs)` and then assigns `self.creds = creds`.
Calendar also normalizes update permissions before common auth setup.
Sheets normalizes dashboard field aliases before common auth setup.
Drive coerces `max_read_size` before common auth setup and applies model function aliases after wrapping.
Each wrapper has a distinct RuntimeError message, OAuth provider, tool name, Agno class, and logger.

### 2. Google service-account fallback predicate is duplicated

`src/mindroom/custom_tools/gmail.py:66` exactly repeats the predicate in `src/mindroom/custom_tools/google_calendar.py:77`, `src/mindroom/custom_tools/google_sheets.py:73`, and `src/mindroom/custom_tools/google_drive.py:95`.
All four overrides return true when either the upstream toolkit has `service_account_path` or the runtime env exposes `GOOGLE_SERVICE_ACCOUNT_FILE`.
This is a Google-specific refinement over the default `ScopedOAuthClientMixin._should_fallback_to_original_auth` behavior at `src/mindroom/oauth/client.py:251`.

Differences to preserve:
No per-tool differences were found in this predicate.
The helper must still run after `_runtime_paths` has been assigned and must continue to read `service_account_path` as provided by the Agno parent wrapper.

## Proposed Generalization

Add a small Google-specific mixin in `src/mindroom/custom_tools/google_service.py`, next to `ThreadLocalGoogleServiceMixin`, for the shared service-account fallback predicate.
That is the lowest-risk extraction because it removes an exact duplicate without changing constructor order or parent initialization semantics.

For the constructor duplication, consider a focused helper on a future cleanup only if more Google wrappers are added or existing wrappers continue to drift.
A possible helper would prepare common OAuth state and return `creds`, but it must leave tool-specific pre-normalization and parent initialization in each wrapper because those details differ enough to make a generic constructor easy to overfit.

## Risk/Tests

Refactoring the fallback predicate would need tests that instantiate Gmail, Calendar, Sheets, and Drive wrappers with `service_account_path` and with `GOOGLE_SERVICE_ACCOUNT_FILE` from `RuntimePaths`, then assert the original upstream auth path is selected.
Constructor extraction would have higher risk around Calendar's post-parent `self.creds` assignment, Sheets field alias validation, Drive size coercion, and Drive alias registration.
If constructor deduplication is attempted, targeted tests should cover each wrapper's constructor kwargs plus an OAuth-required tool call path.
