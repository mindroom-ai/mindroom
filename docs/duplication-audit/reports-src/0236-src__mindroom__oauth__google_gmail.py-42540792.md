## Summary

Top duplication candidate: `google_gmail_oauth_provider` repeats the same built-in Google OAuth provider construction pattern used by Calendar, Drive, and Sheets.
The duplicated behavior is the provider factory boilerplate for Google authorization/token endpoints, shared OAuth client config service, domain allowlist environment names, offline/consent auth parameters, and shared Google token parsing.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
google_gmail_oauth_provider	function	lines 20-41	duplicate-found	google_gmail_oauth_provider OAuthProvider GOOGLE_GMAIL_OAUTH_SCOPES google OAuthProvider authorization_url token_url extra_auth_params token_parser	src/mindroom/oauth/google_gmail.py:20; src/mindroom/oauth/google_calendar.py:18; src/mindroom/oauth/google_drive.py:18; src/mindroom/oauth/google_sheets.py:18; src/mindroom/oauth/google.py:27; src/mindroom/oauth/registry.py:45; src/mindroom/custom_tools/gmail.py:29
```

## Findings

### 1. Repeated Google OAuth provider factory boilerplate

- Primary: `src/mindroom/oauth/google_gmail.py:20`
- Related duplicates:
  - `src/mindroom/oauth/google_calendar.py:18`
  - `src/mindroom/oauth/google_drive.py:18`
  - `src/mindroom/oauth/google_sheets.py:18`
- Shared behavior:
  - Each factory returns an `OAuthProvider` with the same Google OAuth authorization URL and token URL.
  - Each uses `shared_client_config_services=("google_oauth_client",)`.
  - Each derives email-domain and hosted-domain environment variable names through `_google_domain_env_names(provider_id, suffix)`.
  - Each supplies the same offline consent `extra_auth_params`.
  - Each delegates token normalization and identity validation to `_google_token_parser`.
- Differences to preserve:
  - `id`, `display_name`, `scopes`, `credential_service`, `tool_config_service`, `client_config_services`, and `status_capabilities` differ by service.
  - Gmail uses `tool_config_service="gmail"` while the provider id and credential service remain `google_gmail` and `google_gmail_oauth`.

This is functional duplication rather than merely similar formatting because these factories define the same Google OAuth handshake behavior for four provider registrations.
Only the service-specific metadata varies.

## Proposed generalization

Add a small helper in `src/mindroom/oauth/google.py`, for example `_google_oauth_provider(...) -> OAuthProvider`, that accepts the service-specific fields and fills the common Google OAuth defaults.
The helper should set:

- `authorization_url="https://accounts.google.com/o/oauth2/v2/auth"`
- `token_url="https://oauth2.googleapis.com/token"`
- `shared_client_config_services=("google_oauth_client",)`
- domain env names via `_google_domain_env_names`
- the shared offline/consent `extra_auth_params`
- `token_parser=_google_token_parser`

Each service module can keep its public factory and scope constant, then call the helper with explicit service metadata.
No broad registry or plugin changes are needed.

## Risk/tests

Risk is low if the helper preserves exact `OAuthProvider` field values.
The main regression risk is accidentally changing Gmail's `tool_config_service="gmail"` or any provider-specific client config service names.
Tests should compare the generated provider objects for Gmail, Calendar, Drive, and Sheets against their current public fields, especially URLs, scopes, credential/tool services, shared client config services, domain env names, auth params, status capabilities, and token parser identity.
