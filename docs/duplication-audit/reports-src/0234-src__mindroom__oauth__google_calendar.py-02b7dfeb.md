## Summary

The Google Calendar OAuth provider factory duplicates the same Google OAuth provider construction pattern used by the Google Drive, Gmail, and Google Sheets provider modules.
The duplication is real but small, and the repeated fields are already partially centralized through `mindroom.oauth.google`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
google_calendar_oauth_provider	function	lines 18-39	duplicate-found	google_calendar_oauth_provider, def .*oauth_provider, OAuthProvider, google_oauth_client, _google_domain_env_names, access_type offline	src/mindroom/oauth/google_drive.py:18-42; src/mindroom/oauth/google_sheets.py:18-39; src/mindroom/oauth/google_gmail.py:20-41; src/mindroom/oauth/google.py:27-104; src/mindroom/oauth/registry.py:45-51; src/mindroom/custom_tools/google_calendar.py:26-30; tests/test_google_calendar_oauth_tool.py:145-149; tests/api/test_oauth_api.py:1100-1122
```

## Findings

### Google OAuth provider factories repeat the same provider skeleton

- `src/mindroom/oauth/google_calendar.py:18-39` builds an `OAuthProvider` with the common Google authorization URL, token URL, identity scopes, domain allowlist env-name derivation, offline/consent auth params, shared Google client config service, and `_google_token_parser`.
- `src/mindroom/oauth/google_drive.py:18-42`, `src/mindroom/oauth/google_sheets.py:18-39`, and `src/mindroom/oauth/google_gmail.py:20-41` repeat the same construction pattern.
- The behavior is functionally the same: each factory declares a built-in Google OAuth provider and varies only product-specific metadata such as `id`, `display_name`, service names, OAuth scopes, tool config service, and status capabilities.
- Differences to preserve:
  - Calendar uses tool config service `google_calendar`, credential service `google_calendar_oauth`, client config service `google_calendar_oauth_client`, scope `https://www.googleapis.com/auth/calendar`, and status capability `Calendar event read/write`.
  - Drive, Sheets, and Gmail use their own ids, scopes, service names, and capability text.
  - Gmail's `tool_config_service` is `gmail`, unlike the provider id `google_gmail`.

## Proposed Generalization

A small helper in `src/mindroom/oauth/google.py` could centralize the repeated Google provider skeleton, for example:

- `google_oauth_provider(...) -> OAuthProvider`
- Required parameters: `provider_id`, `display_name`, `scopes`, `credential_service`, `tool_config_service`, `client_config_service`, and `status_capabilities`.
- The helper would own the common Google URLs, `shared_client_config_services=("google_oauth_client",)`, domain env-name construction, offline/consent auth params, and `_google_token_parser`.

No immediate refactor is required for Calendar alone because the duplicated factories are short and readable.
If another Google OAuth provider is added, the helper would reduce repeated security-sensitive OAuth defaults and make provider drift easier to spot.

## Risk/tests

Primary risks are accidental changes to provider ids, service names, callback redirect behavior, required scopes, and offline refresh-token auth params.
Tests to preserve or add around a refactor:

- `tests/test_google_calendar_oauth_tool.py:145-149` for Calendar's write scope.
- `tests/api/test_oauth_api.py:1100-1122` for shared Google client config using the Calendar provider callback URL.
- Existing Drive OAuth client config tests around provider-specific versus shared `google_oauth_client` precedence.
- A focused unit test for the helper output for each built-in Google provider would catch metadata drift without needing API-level coverage for every field.
