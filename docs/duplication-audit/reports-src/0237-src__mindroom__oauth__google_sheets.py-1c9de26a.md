## Summary

The top duplication candidate is the built-in Google OAuth provider factory shape repeated by Sheets, Calendar, Drive, and Gmail.
Each factory constructs `OAuthProvider` with the same Google authorization/token endpoints, shared client config service, domain env-name derivation, offline consent auth params, and token parser, while varying only provider ids, display names, scopes, services, and status capabilities.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
google_sheets_oauth_provider	function	lines 18-39	duplicate-found	google_sheets_oauth_provider OAuthProvider authorization_url token_url _google_domain_env_names extra_auth_params token_parser Google Sheets	src/mindroom/oauth/google_calendar.py:18-39; src/mindroom/oauth/google_drive.py:18-42; src/mindroom/oauth/google_gmail.py:20-41; src/mindroom/oauth/google.py:102-104; src/mindroom/custom_tools/google_sheets.py:32-36; src/mindroom/oauth/registry.py:45-50
```

## Findings

### Repeated Google OAuth provider factory construction

- `src/mindroom/oauth/google_sheets.py:18-39` constructs the Google Sheets OAuth provider.
- `src/mindroom/oauth/google_calendar.py:18-39`, `src/mindroom/oauth/google_drive.py:18-42`, and `src/mindroom/oauth/google_gmail.py:20-41` use the same factory behavior for other Google services.

The duplicated behavior is the construction of a Google OAuth provider with:

- `authorization_url="https://accounts.google.com/o/oauth2/v2/auth"`
- `token_url="https://oauth2.googleapis.com/token"`
- service-specific OAuth scopes composed with `GOOGLE_IDENTITY_SCOPES`
- service-specific credential and tool config service names
- provider-specific client config service plus shared `google_oauth_client`
- allowed email and hosted-domain env names derived through `_google_domain_env_names`
- identical `extra_auth_params` for offline consent and incremental grants
- identical `_google_token_parser`

Differences to preserve:

- Sheets uses provider id `google_sheets`, display name `Google Sheets`, credential service `google_sheets_oauth`, tool config service `google_sheets`, client config service `google_sheets_oauth_client`, Sheets read/write scope, and status capability `Sheets read/write`.
- Calendar, Drive, and Gmail have different service ids, tool config service names, scope sets, and status capabilities.
- Gmail's tool config service is `gmail`, not `google_gmail`.
- Drive has multiple status capabilities.

The custom tool wrapper at `src/mindroom/custom_tools/google_sheets.py:32-36` only consumes this provider definition and is related rather than a duplicate provider factory.
The registry at `src/mindroom/oauth/registry.py:45-50` only loads the built-in provider factories and is also related rather than duplicated behavior.

## Proposed Generalization

A small helper in `src/mindroom/oauth/google.py` could remove the repeated construction while preserving all service-specific values:

1. Add a helper such as `google_oauth_provider(...) -> OAuthProvider` that accepts provider id, display name, scopes, credential service, tool config service, client config services, and status capabilities.
2. Keep each service module's public factory function and scope constant unchanged.
3. Replace only the repeated `OAuthProvider(...)` constructor body in each Google service module with the helper call.
4. Add or update focused OAuth provider tests asserting provider ids, scopes, service names, env names, auth params, and token parser remain unchanged.

No broader refactor is recommended.

## Risk/Tests

Risk is low if the helper is purely declarative, but mistakes in service names or env-name derivation could break existing credential lookup or domain restrictions.
Tests should cover all built-in Google provider definitions, especially Gmail's `tool_config_service="gmail"` and Drive's multi-entry `status_capabilities`.

No production code was edited for this audit.
