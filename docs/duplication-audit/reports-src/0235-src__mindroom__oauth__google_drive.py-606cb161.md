# Duplication Audit: src/mindroom/oauth/google_drive.py

## Summary

Top duplication candidate: `google_drive_oauth_provider` is one of four near-identical built-in Google OAuth provider factory functions.
The duplicated behavior is Google OAuth provider construction: common Google auth/token endpoints, offline consent parameters, shared client-config service, domain-policy environment name derivation, and shared token parser.
The provider-specific values are meaningful and must remain distinct.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
google_drive_oauth_provider	function	lines 18-42	duplicate-found	google_drive_oauth_provider OAuthProvider authorization_url token_url extra_auth_params _google_domain_env_names _google_token_parser	src/mindroom/oauth/google_calendar.py:18; src/mindroom/oauth/google_gmail.py:20; src/mindroom/oauth/google_sheets.py:18; src/mindroom/oauth/google.py:92; src/mindroom/oauth/service.py:22
```

## Findings

### Repeated Google OAuth provider factory construction

`src/mindroom/oauth/google_drive.py:18` builds an `OAuthProvider` with the same Google OAuth mechanics as:

- `src/mindroom/oauth/google_calendar.py:18`
- `src/mindroom/oauth/google_gmail.py:20`
- `src/mindroom/oauth/google_sheets.py:18`

The duplicated behavior is the repeated factory shape:

- `authorization_url="https://accounts.google.com/o/oauth2/v2/auth"`
- `token_url="https://oauth2.googleapis.com/token"`
- `shared_client_config_services=("google_oauth_client",)`
- `allowed_email_domains_env=_google_domain_env_names(provider_id, "ALLOWED_EMAIL_DOMAINS")`
- `allowed_hosted_domains_env=_google_domain_env_names(provider_id, "ALLOWED_HOSTED_DOMAINS")`
- `extra_auth_params` requesting offline access, granted-scope inclusion, and consent
- `token_parser=_google_token_parser`

The differences to preserve are provider ID, display name, scopes, credential service, tool config service, provider-specific client config service, and status capabilities.
Gmail also intentionally maps `tool_config_service` to `gmail` instead of `google_gmail`, so any helper would need an explicit override rather than deriving every service name mechanically.

`src/mindroom/oauth/google.py:92` already centralizes shared token parsing and domain environment name construction, so the duplication left in `google_drive.py` is declarative provider assembly rather than repeated low-level OAuth validation.

`src/mindroom/oauth/service.py:22` separately repeats the Google provider ID set for service-account support, including `google_drive`.
That is related provider metadata, but it is not duplicate construction behavior inside the primary file.

## Proposed Generalization

A small helper in `src/mindroom/oauth/google.py` could reduce the repeated factory boilerplate:

`google_oauth_provider(provider_id, display_name, scopes, credential_service, tool_config_service, client_config_service, status_capabilities) -> OAuthProvider`

The helper should only fill the common Google endpoints, shared client config service, domain env names, offline consent parameters, and token parser.
It should not derive provider-specific service names unless explicitly passed, because Gmail already differs from the obvious provider ID convention.

This refactor is reasonable if another Google OAuth provider is added or if these definitions change often.
For the current four compact modules, no urgent refactor is required.

## Risk/Tests

Risk is mostly configuration drift: changing one Google provider factory can miss the matching endpoint, auth parameters, domain env names, or parser in the others.
A helper would reduce that drift but could also hide provider-specific service names if over-generalized.

Tests should compare loaded built-in provider definitions for Drive, Calendar, Gmail, and Sheets, including provider IDs, scopes, credential services, tool config services, client config service names, shared client config service, allowed-domain environment names, auth parameters, and token parser identity.
