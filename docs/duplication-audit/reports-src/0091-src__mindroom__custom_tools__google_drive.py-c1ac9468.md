Summary: `GoogleDriveTools._coerce_max_read_size` duplicates the number coercion behavior already implemented for runtime tool config fields in `src/mindroom/tool_system/metadata.py`.
The Google Drive wrapper initialization and service-account fallback match the Gmail, Google Calendar, and Google Sheets OAuth wrappers, but most shared auth behavior is already centralized in `ScopedOAuthClientMixin`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
GoogleDriveTools	class	lines 30-97	related-only	GoogleDriveTools class ScopedOAuthClientMixin ThreadLocalGoogleServiceMixin Google tools wrappers	src/mindroom/custom_tools/gmail.py:26; src/mindroom/custom_tools/google_calendar.py:26; src/mindroom/custom_tools/google_sheets.py:32; src/mindroom/oauth/client.py:53
GoogleDriveTools.__init__	method	lines 36-66	related-only	provided_creds credentials_manager _initialize_oauth_client _set_original_auth _wrap_oauth_function_entrypoints apply_toolkit_function_aliases	src/mindroom/custom_tools/gmail.py:32; src/mindroom/custom_tools/google_calendar.py:32; src/mindroom/custom_tools/google_sheets.py:38; src/mindroom/oauth/client.py:68; src/mindroom/oauth/client.py:80; src/mindroom/tool_system/toolkit_aliases.py:14
GoogleDriveTools._coerce_max_read_size	method	lines 68-93	duplicate-found	max_read_size math.isfinite Stored config value number finite string float parsed	src/mindroom/tool_system/metadata.py:341; src/mindroom/tool_system/metadata.py:370; src/mindroom/tool_system/metadata.py:1194
GoogleDriveTools._should_fallback_to_original_auth	method	lines 95-97	related-only	_should_fallback_to_original_auth service_account_path GOOGLE_SERVICE_ACCOUNT_FILE defer_to_original_auth	src/mindroom/custom_tools/gmail.py:66; src/mindroom/custom_tools/google_calendar.py:77; src/mindroom/custom_tools/google_sheets.py:73; src/mindroom/oauth/client.py:251
```

## Findings

1. Duplicate numeric coercion for Google Drive `max_read_size`.
   `src/mindroom/custom_tools/google_drive.py:68` accepts `None`, finite `int`/`float`, numeric strings with whitespace, empty strings as omitted values, rejects booleans, rejects non-finite numbers, and returns integral parsed strings as `int`.
   `src/mindroom/tool_system/metadata.py:341` implements the same conversion for persisted dashboard number fields before constructor invocation.
   The main differences to preserve are exception type/message and omission representation: Drive returns `None` so `__init__` can pop the kwarg, while metadata returns `_OMIT_TOOL_CONFIG_ARG` and raises `ToolConfigOverrideError`.

2. Related repeated OAuth wrapper initialization across Google custom tools.
   `src/mindroom/custom_tools/google_drive.py:44`, `src/mindroom/custom_tools/gmail.py:45`, `src/mindroom/custom_tools/google_calendar.py:45`, and `src/mindroom/custom_tools/google_sheets.py:51` all pop provided credentials, require an explicit `credentials_manager`, store runtime paths and credentials manager, apply runtime service-account kwargs, initialize scoped OAuth credentials, call the Agno parent, store original auth, and wrap function entrypoints.
   This is functionally similar, but the meaningful auth mechanics already live in `src/mindroom/oauth/client.py:68`, `src/mindroom/oauth/client.py:80`, `src/mindroom/oauth/client.py:101`, and `src/mindroom/oauth/client.py:105`.
   Drive also applies model-visible function aliases at `src/mindroom/custom_tools/google_drive.py:66`, Calendar rewrites `allow_update` into multiple Agno flags at `src/mindroom/custom_tools/google_calendar.py:46`, and Sheets maps dashboard field aliases at `src/mindroom/custom_tools/google_sheets.py:77`, so a broad constructor extraction would need careful parameterization.

3. Related repeated service-account fallback predicate.
   `src/mindroom/custom_tools/google_drive.py:95`, `src/mindroom/custom_tools/gmail.py:66`, `src/mindroom/custom_tools/google_calendar.py:77`, and `src/mindroom/custom_tools/google_sheets.py:73` all prefer original upstream auth when either `service_account_path` or `GOOGLE_SERVICE_ACCOUNT_FILE` is configured.
   The base mixin has a default predicate at `src/mindroom/oauth/client.py:251`, but the Google wrappers intentionally override it to inspect the resolved runtime environment and the Agno service-account attribute.

## Proposed Generalization

For the real duplication, consider extracting a small public helper from the existing metadata coercion path, for example `coerce_optional_number(value, *, label, omitted)` in `src/mindroom/tool_system/metadata.py` or a narrower shared utility near tool config normalization.
Then `_coerce_number_tool_config_value` and `GoogleDriveTools._coerce_max_read_size` can share the same parse/finite/bool logic while preserving their different exception types and omit sentinels.

No constructor refactor is recommended from this audit alone.
The sibling Google wrappers are similar, but the current mixin already centralizes the risky OAuth behavior, and the remaining constructor differences are tool-specific enough that a generic initializer could obscure behavior.

No separate refactor is recommended for `_should_fallback_to_original_auth` unless more Google service-account wrappers are added.
If it grows, a helper like `_has_google_service_account_auth(runtime_paths, service_account_path)` in `src/mindroom/custom_tools/google_service.py` would be enough.

## Risk/Tests

Numeric coercion refactor risk is limited but should cover booleans, finite numeric inputs, `nan`/`inf`, numeric strings, blank strings, invalid strings, and `None`.
Relevant tests should assert both Drive constructor behavior and metadata runtime config behavior because the two call sites currently expose different exception types and omission handling.

OAuth wrapper refactors would carry higher risk because they affect credential loading, service-account fallback, function wrapping, and per-tool Agno constructor differences.
If attempted, tests should instantiate Gmail, Drive, Calendar, and Sheets wrappers with stored OAuth, provided creds, and service-account fallback cases.
