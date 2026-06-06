# SaaS RLS and SSO Security Report

## Scope

Own write scope stayed in SaaS Supabase and backend auth/test files.
No frontend source change was needed because frontend code only calls the backend SSO endpoint and does not set cookie scope itself.

## Confirmed and Fixed

### SAASFE-SAASFE-1/2: account self-update of privileged columns

Confirmed.
`accounts` had an own-row update policy plus table-wide `GRANT SELECT, INSERT, UPDATE ON TABLE accounts TO authenticated`, so a direct authenticated Supabase client could update own `is_admin`, `tier`, or `status`.
Fix: removed table-wide account `UPDATE` for `authenticated` and granted `UPDATE` only on profile and consent columns.
Privileged columns now require service-role writes, which is what the admin backend route uses after `verify_admin`.

Files:
- `saas-platform/supabase/migrations/000_consolidated_complete_schema.sql`
- `saas-platform/platform-backend/tests/test_multitenancy_security.py`

### SAASFE-SAASFE-3 / SAASBE-SAASBE-3: wildcard raw JWT SSO cookie

Confirmed.
`POST /my/sso-cookie` stored the raw Supabase access token in `mindroom_jwt` with `Domain=.<platform domain>`, sending it to tenant subdomains.
Fix: new raw-token cookie is host-only on the platform API host.
The endpoint also clears the legacy superdomain cookie on set and clear responses.

Files:
- `saas-platform/platform-backend/src/backend/routes/sso.py`
- `saas-platform/platform-backend/tests/test_sso_cookie_attrs.py`

### SAASBE-SAASBE-2: auth cache keyed by raw token and beyond JWT expiry

Confirmed.
`verify_user` keyed `_auth_cache` by raw bearer token and relied only on fixed 5-minute TTL.
Fix: cache key is SHA-256(token), and cached user data is returned only before the JWT `exp`.
Tokens without a parseable future `exp` are not cached.

Files:
- `saas-platform/platform-backend/src/backend/deps.py`
- `saas-platform/platform-backend/tests/test_deps.py`

## Dropped or Cheap Checks

No direct `SAASFE-5` or `SAASFE-6` identifiers existed in `.claude`, `docs`, or `saas-platform`.
No cheap actionable frontend candidate was found beyond the SSO caller, which remains a bearer-authenticated API call.

## Tests

Initial direct test command was blocked because `uv` was not on PATH.
Repo `shell.nix` was also blocked on aarch64-darwin by unsupported Chromium.
Used narrow Nix shell instead.

Commands run:

```bash
nix-shell -p python313 uv --run 'uv sync --all-extras --dev'
nix-shell -p python313 uv --run 'uv run pytest tests/test_deps.py tests/test_sso_cookie_attrs.py tests/test_multitenancy_security.py -q'
nix-shell -p python313 uv --run 'uv run pytest tests/test_rate_limit_sso.py tests/test_matrix_oidc.py -q'
```

Result:

```text
26 passed, 1 warning in 1.60s
6 passed, 32 warnings in 4.51s
```

Warning:

```text
httpx DeprecationWarning: Use 'content=<...>' to upload raw bytes/text content.
starlette TestClient DeprecationWarning: per-request cookies=<...> is deprecated.
```

## Residual Risk

Existing browsers may keep the old superdomain `mindroom_jwt` until they hit the updated set or clear endpoint, which now sends a legacy-domain deletion cookie.
Direct instance-domain auth that depended on the wildcard cookie now fails closed because the raw platform JWT is no longer sent to tenant subdomains.
The consolidated SQL file is fixed for fresh installs, but already-deployed databases still need the equivalent revoke/grant SQL applied.
