## Summary

Top duplication candidate: `_google_token_parser` repeats the generic OAuth token-data assembly in `src/mindroom/oauth/providers.py::_default_token_parser`.
The duplication is real but partly intentional because Google requires verified ID-token claims while the default parser keeps ID-token claims unverified.
No meaningful duplication found for `_google_domain_env_names`; it is already the shared helper used by the Google provider modules.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_google_token_parser	function	lines 34-99	duplicate-found	token_parser OAuthTokenResult access_token refresh_token token_type expires_at scopes id_token claims	src/mindroom/oauth/providers.py:150; src/mindroom/api/oauth.py:280; src/mindroom/oauth/service.py:226
_google_domain_env_names	function	lines 102-104	related-only	domain_env_names allowed_email_domains_env allowed_hosted_domains_env MINDROOM_OAUTH	src/mindroom/oauth/google_calendar.py:30; src/mindroom/oauth/google_drive.py:30; src/mindroom/oauth/google_gmail.py:32; src/mindroom/oauth/google_sheets.py:30; src/mindroom/oauth/providers.py:110
```

## Findings

### 1. Google token parser duplicates generic OAuth token-data assembly

- Primary: `src/mindroom/oauth/google.py:34`
- Related implementation: `src/mindroom/oauth/providers.py:150`

Both parsers validate a non-empty `access_token`, resolve granted scopes from `token_response["scope"]` when present, build token storage with `token`, `token_uri`, `client_id`, `scopes`, `_source`, and `_oauth_provider`, then optionally copy `refresh_token`, `token_type`, and `expires_at` via `oauth_expires_at_from_response`.
This shared behavior is nearly identical between `src/mindroom/oauth/google.py:40` through `src/mindroom/oauth/google.py:97` and `src/mindroom/oauth/providers.py:157` through `src/mindroom/oauth/providers.py:183`.

The behavior differences to preserve are important:

- Google raises `OAuthClaimValidationError` with Google-specific messages, while the default parser raises `OAuthProviderError` for missing access tokens.
- Google requires verified identity claims, either from a verified `_oauth_claims` refresh response or from `google_id_token.verify_oauth2_token`.
- The default parser stores an unverified `_id_token` and returns `claims_verified=False`; Google intentionally does not store `_id_token` and returns `claims_verified=True`.

### 2. Google domain environment-name expansion is already centralized

- Primary: `src/mindroom/oauth/google.py:102`
- Call sites: `src/mindroom/oauth/google_calendar.py:30`, `src/mindroom/oauth/google_drive.py:30`, `src/mindroom/oauth/google_gmail.py:32`, `src/mindroom/oauth/google_sheets.py:30`

The helper expands a Google OAuth provider id plus suffix into provider-specific and `MINDROOM_OAUTH_` environment variable names.
The four built-in Google providers all call this helper rather than duplicating the string construction.
`src/mindroom/oauth/providers.py:110` has a related normalization helper for environment-name inputs, but it solves a different problem: coercing string or sequence values into a tuple.

## Proposed Generalization

For the token parser duplication, the smallest useful generalization would be a private helper in `src/mindroom/oauth/providers.py`, for example `_token_data_from_oauth_response(provider, token_response, client_config, *, missing_access_token_error)`, returning the shared token-data dictionary.
`_default_token_parser` and `_google_token_parser` could both call it, while keeping their claim handling separate.

No refactor recommended for `_google_domain_env_names`; it is already the local shared abstraction and has only one tiny responsibility.

## Risk/Tests

The token parser refactor would touch credential storage shape for every OAuth provider, so tests should assert:

- Default OAuth parsing still stores `_id_token` and returns `claims_verified=False`.
- Google parsing still requires verified claims and returns `claims_verified=True`.
- `refresh_token`, `token_type`, `expires_at`, `scope`, and fallback provider scopes are preserved exactly for both parser paths.
- Missing Google access token still raises `OAuthClaimValidationError`, not the generic `OAuthProviderError`.
