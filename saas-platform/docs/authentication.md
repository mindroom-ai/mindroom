Authentication Overview

This repo deploys two types of frontends/backends with two related but different auth paths:

- Platform (SaaS) app and API (namespace: mindroom-staging)
- Instance app and API (namespace: mindroom-instances, one per customer)

Components
- Supabase: identity provider for both platform and instances.
- Platform Frontend (Next.js): users log in here and obtain a Supabase session.
- Platform Backend (FastAPI): sets a superdomain SSO cookie used by instances.
- Instance Backend (FastAPI): serves the bundled UI, serves instance-specific APIs, and verifies JWTs.

How Auth Works (Platform Mode)
1) User signs in at the Platform app (e.g., https://app.<superdomain>).
2) Platform Frontend calls Platform Backend POST /my/sso-cookie with the Supabase access token.
   - Platform Backend sets an HttpOnly cookie mindroom_jwt on the superdomain (e.g., .<superdomain>).
3) User navigates to an Instance domain (e.g., https://<id>.<superdomain>).
   - The instance backend checks for mindroom_jwt on UI routes and redirects to platform login if missing.
4) For /api calls on the Instance domain:
   - The browser sends mindroom_jwt automatically as an HttpOnly cookie.
   - Instance Backend reads the cookie directly, verifies the JWT against Supabase (using SUPABASE_URL + keys), and authorizes the request.

Why the instance no longer needs an auth sidecar
- The instance backend now reads mindroom_jwt directly.
- Ingress can route `/`, `/api`, and `/v1` to the same backend service.
- This removes the extra instance frontend deployment and the header-injection proxy hop.

Key Settings
- Platform Backend
  - PLATFORM_DOMAIN must be the superdomain (e.g., <superdomain>) so the cookie covers all subdomains.
  - SUPABASE_URL/ANON_KEY/SERVICE_KEY used to validate tokens and perform server actions.
- Instance (Helm release instance-<id>)
  - values.yaml: supabaseUrl, supabaseAnonKey, supabaseServiceKey must match the platform project.
  - deployment-backend:
    - UI: serves the bundled dashboard and redirects to the platform login if mindroom_jwt is missing.
    - API: reads mindroom_jwt directly and validates it against Supabase.

Notes and Gotchas
- If the cookie exists but is invalid/expired, UI can still load but API calls return 401 (by design).
- If instance Supabase vars don’t match platform’s project, backend will reject tokens (401).
- WebSockets and SSE now terminate directly on the backend service instead of a proxy sidecar.

Troubleshooting
- Cookie missing: user is redirected to platform login.
- 401 on /api: refresh SSO cookie via platform, verify instance Supabase vars match platform.
- 500 on UI: check backend logs and confirm the bundled frontend assets are present in the image.
- Inspect logs: backend logs show both UI and API request handling.
