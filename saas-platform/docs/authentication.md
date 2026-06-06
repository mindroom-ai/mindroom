Authentication Overview

This repo deploys two types of frontends/backends with two related but different auth paths:

- Platform (SaaS) app and API (namespace: mindroom-staging)
- Instance app and API (namespace: mindroom-instances, one per customer)

Components
- Supabase: identity provider for both platform and instances.
- Platform Frontend (Next.js): users log in here and obtain a Supabase session.
- Platform Backend (FastAPI): validates Supabase sessions and issues first-party Matrix OIDC codes for hosted Synapse login.
- Instance Backend (FastAPI): serves the bundled UI, serves instance-specific APIs, and trusts only explicitly configured upstream identity or standalone credentials.

How Auth Works (Platform Mode)
1) User signs in at the Platform app (e.g., https://app.<superdomain>).
2) Platform Frontend calls Platform Backend POST /my/sso-cookie with the Supabase access token.
   - Platform Backend sets an HttpOnly host-only cookie on the API host.
   - The cookie is not scoped to the superdomain and is not sent to tenant instance subdomains.
3) User navigates to an Instance domain (e.g., https://<id>.<superdomain>).
   - The tenant instance must not rely on raw platform JWT cookies.
   - Hosted Matrix login goes through the Platform Backend Matrix OIDC endpoints on the API host.
4) For tenant instance browser and API calls:
   - The instance accepts only the configured auth path for that deployment.
   - For hosted deployments, prefer trusted upstream auth with deployment-owned identity headers and strict JWT verification.
   - For standalone deployments, use the standalone dashboard API key flow.

Why raw platform JWT cookies are not shared with instances
- Tenant subdomains must not receive the platform Supabase access token.
- Instance auth fails closed unless an explicit trusted upstream or standalone auth path is configured.
- Matrix SSO still works through the Platform Backend because the API-host cookie is available to the Matrix OIDC authorize endpoint.

Key Settings
- Platform Backend
  - PLATFORM_DOMAIN identifies the platform domain used for links and allowed origins.
  - SUPABASE_URL/ANON_KEY/SERVICE_KEY used to validate tokens and perform server actions.
- Instance (Helm release instance-<id>)
  - trustedUpstreamAuth.enabled should be set when an access layer injects authenticated identity.
  - trustedUpstreamAuth.requireJwt should be true when the access layer can sign identity assertions.
  - values.yaml Supabase fields are not a substitute for tenant auth.

Notes and Gotchas
- A missing or invalid API-host SSO cookie redirects Matrix OIDC authorization back to platform login.
- A tenant instance request without the configured trusted upstream headers or standalone credential returns 401.
- WebSockets and SSE should terminate only behind the same authenticated instance access layer as normal API traffic.

Troubleshooting
- Matrix OIDC cookie missing: user is redirected to platform login.
- 401 on tenant /api: verify trusted upstream auth headers, JWT settings, or standalone API key configuration.
- 500 on UI: check backend logs and confirm the bundled frontend assets are present in the image.
- Inspect logs: backend logs show both UI and API request handling.
