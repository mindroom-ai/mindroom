Authentication Overview

This repo deploys two types of frontends/backends with two related but different auth paths:

- Platform (SaaS) app and API (namespace: mindroom-staging)
- Instance app and API (namespace: mindroom-instances, one per customer)

Components
- Supabase: identity provider for both platform and instances.
- Platform Frontend (Next.js): users log in here and obtain a Supabase session.
- Platform Backend (FastAPI): sets a superdomain SSO cookie used by instances.
- Instance Frontend (Vite app + nginx sidecar): serves the UI and enforces SSO presence.
- Instance Backend (FastAPI): serves instance-specific APIs and verifies JWTs.

How Auth Works (Platform Mode)
1) User signs in at the Platform app (e.g., https://app.<superdomain>).
2) Platform Frontend calls Platform Backend POST /my/sso-cookie with the Supabase access token.
   - Platform Backend sets an HttpOnly cookie mindroom_jwt on the superdomain (e.g., .<superdomain>).
3) User navigates to an Instance domain (e.g., https://<id>.<superdomain>).
   - Instance nginx sidecar checks for mindroom_jwt on UI routes and redirects to platform login if missing.
4) For /api calls on the Instance domain:
   - Ingress routes /api to the instance’s nginx sidecar (not directly to the backend).
   - nginx injects Authorization: Bearer <mindroom_jwt> and proxies to the instance backend service.
   - Instance Backend verifies the JWT against Supabase (using SUPABASE_URL + keys) and authorizes the request.

Why /api goes through nginx sidecar
- The instance backend expects Authorization headers; it does not read the cookie directly.
- Routing /api via nginx lets us inject Authorization from mindroom_jwt safely (HttpOnly cookie stays in browser).

Key Settings
- Platform Backend
  - PLATFORM_DOMAIN must be the superdomain (e.g., <superdomain>) so the cookie covers all subdomains.
  - SUPABASE_URL/ANON_KEY/SERVICE_KEY used to validate tokens and perform server actions.
- Instance (Helm release instance-<id>)
  - values.yaml: supabaseUrl, supabaseAnonKey, supabaseServiceKey must match the platform project.
  - nginx configmap (nginx-auth-config-<id>):
    - UI: requires mindroom_jwt for /, proxies to frontend container.
    - API: injects Authorization from mindroom_jwt and proxies to backend service.

Notes and Gotchas
- If the cookie exists but is invalid/expired, UI can still load but API calls return 401 (by design).
- If instance Supabase vars don’t match platform’s project, backend will reject tokens (401).
- Avoid auth_request loops in nginx unless carefully configured; backend already validates JWTs.
- WebSockets: nginx must forward Upgrade/Connection headers for / and /api.

Troubleshooting
- Cookie missing: user is redirected to platform login.
- 401 on /api: refresh SSO cookie via platform, verify instance Supabase vars match platform.
- 500 on UI: check nginx config; avoid calling protected auth subroutes from auth_request.
- Inspect logs: nginx logs to stdout/stderr; backend logs show per-request status lines.
