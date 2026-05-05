## Summary

Top duplication candidates for `src/mindroom/custom_tools/google_sheets.py` are the repeated Google OAuth-backed toolkit wrapper lifecycle and the repeated service-account fallback predicate.
`GoogleSheetsTools._normalize_dashboard_config_kwargs` is related to the shared tool-config init pipeline, but its behavior is Sheets-specific alias mapping from dashboard field names to Agno constructor names.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
GoogleSheetsTools	class	lines 32-85	duplicate-found	Google OAuth tool wrappers ScopedOAuthClientMixin ThreadLocalGoogleServiceMixin AgnoGoogle*Tools	src/mindroom/custom_tools/google_calendar.py:26; src/mindroom/custom_tools/google_drive.py:30; src/mindroom/custom_tools/gmail.py:26; src/mindroom/oauth/client.py:53; tests/test_google_tool_wrappers.py:55
GoogleSheetsTools.__init__	method	lines 38-71	duplicate-found	provided_creds credentials_manager _apply_runtime_original_auth_kwargs _initialize_oauth_client _set_original_auth _wrap_oauth_function_entrypoints	src/mindroom/custom_tools/google_calendar.py:32; src/mindroom/custom_tools/google_drive.py:36; src/mindroom/custom_tools/gmail.py:32; src/mindroom/oauth/client.py:68; src/mindroom/tool_system/metadata.py:509; tests/test_google_tool_wrappers.py:55
GoogleSheetsTools._should_fallback_to_original_auth	method	lines 73-75	duplicate-found	_should_fallback_to_original_auth service_account_path GOOGLE_SERVICE_ACCOUNT_FILE oauth_provider_service_account_configured	src/mindroom/custom_tools/google_calendar.py:77; src/mindroom/custom_tools/google_drive.py:95; src/mindroom/custom_tools/gmail.py:66; src/mindroom/oauth/client.py:251; src/mindroom/oauth/service.py:174; tests/test_google_sheets_oauth_tool.py:146; tests/test_google_tool_wrappers.py:256
GoogleSheetsTools._normalize_dashboard_config_kwargs	method	lines 77-85	related-only	dashboard config kwargs read create update read_sheet create_sheet update_sheet config_fields init_kwargs	src/mindroom/tools/google_sheets.py:30; src/mindroom/tool_system/metadata.py:388; src/mindroom/tool_system/metadata.py:412; src/mindroom/tool_system/metadata.py:528; tests/test_google_sheets_oauth_tool.py:111
```

## Findings

### 1. Google OAuth toolkit wrapper initialization is repeated

`GoogleSheetsTools.__init__` repeats the same lifecycle used by `GmailTools`, `GoogleCalendarTools`, and `GoogleDriveTools`: pop explicit `creds`, require an explicit `credentials_manager`, store `_runtime_paths` and `_creds_manager`, apply runtime service-account/delegated-user kwargs, initialize scoped OAuth credentials, call the Agno parent constructor, store the original `_auth`, and wrap public function entrypoints.
The shared behavior appears at `src/mindroom/custom_tools/google_sheets.py:51`, `src/mindroom/custom_tools/gmail.py:45`, `src/mindroom/custom_tools/google_calendar.py:45`, and `src/mindroom/custom_tools/google_drive.py:44`.

Differences to preserve:
Google Calendar maps `allow_update` to several event write flags before initialization and assigns `self.creds` after `super().__init__`.
Google Drive coerces `max_read_size` and applies toolkit function aliases.
Google Sheets maps dashboard config aliases before validating the credential manager.

### 2. Service-account fallback predicate is duplicated across Google wrappers

`GoogleSheetsTools._should_fallback_to_original_auth` is identical in behavior to Gmail, Google Calendar, and Google Drive.
Each checks whether the upstream toolkit has a `service_account_path` or whether `RuntimePaths` provides `GOOGLE_SERVICE_ACCOUNT_FILE`.
The shared mixin already has a default `_should_fallback_to_original_auth` at `src/mindroom/oauth/client.py:251`, but the Google wrappers override it with identical service-account-aware logic at `src/mindroom/custom_tools/google_sheets.py:73`, `src/mindroom/custom_tools/gmail.py:66`, `src/mindroom/custom_tools/google_calendar.py:77`, and `src/mindroom/custom_tools/google_drive.py:95`.

`src/mindroom/oauth/service.py:174` contains related dashboard status logic for service-account configuration, but it checks provider eligibility plus the env value only and does not know about an instance-level `service_account_path`.

### 3. Sheets dashboard alias normalization is local but related to shared config init

`GoogleSheetsTools._normalize_dashboard_config_kwargs` maps dashboard field names from `src/mindroom/tools/google_sheets.py:48` (`read`, `create`, `update`) to Agno constructor arguments (`read_sheet`, `create_sheet`, `update_sheet`) and rejects ambiguous duplicate inputs.
The shared runtime config path in `src/mindroom/tool_system/metadata.py:412` collects dashboard config values into constructor kwargs by field name.
That pipeline is related, but it does not support per-field runtime aliases, so this helper is currently a Sheets-specific adapter rather than duplicated behavior.

## Proposed Generalization

1. Add a small helper on `ScopedOAuthClientMixin` for Google service-account fallback, or change its default `_should_fallback_to_original_auth` to return `self._defer_to_original_auth or bool(self.service_account_path or self._runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE"))` if all current mixin consumers are Google-backed.
2. Optionally add a focused initialization helper that accepts the parent auth descriptor, parent initializer callback, logger, kwargs, and a small pre/post hook to preserve Calendar, Drive, and Sheets-specific config handling.
3. Keep the Sheets dashboard alias mapper local unless more tools need field-name-to-init-arg aliases; if that happens, add an `init_arg` or alias field to `ConfigField` and resolve it in `_build_tool_config_init_kwargs`.

## Risk/tests

The service-account fallback dedupe is low risk if covered by existing Google wrapper tests in `tests/test_google_tool_wrappers.py` and tool-specific OAuth tests.
The initializer dedupe has higher risk because Agno parent constructors differ in how they accept and store credentials, especially Calendar assigning `self.creds` after parent initialization.
If implemented later, run `uv run pytest tests/test_google_tool_wrappers.py tests/test_google_sheets_oauth_tool.py tests/test_google_calendar_oauth_tool.py tests/test_google_drive_oauth_tool.py tests/test_gmail_tools.py -n 0 --no-cov`.
