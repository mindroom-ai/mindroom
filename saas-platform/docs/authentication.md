# Authentication Overview

The platform app and API run in `mindroom-{environment}`; hosted customer instances share the `mindroom-instances` namespace.
Each instance's MindRoom backend serves its bundled dashboard and API.

## Hosted Supabase Login

1. The user signs in to the platform app and obtains a Supabase session.
2. The frontend calls `POST /my/sso-cookie` on the platform API with the Supabase access token as a bearer token.
3. The platform backend validates the token and sets the `mindroom_jwt` cookie.
4. On an instance dashboard or API request, the runtime validates the Supabase bearer token or `mindroom_jwt` cookie and checks the user against its configured `ACCOUNT_ID`.

For ordinary DNS values, the cookie has `Domain=.PLATFORM_DOMAIN`, so browsers send it to matching tenant subdomains as well as the platform API.
An existing leading dot is retained.
Empty values, `localhost`, IP addresses, single-label hosts, and values containing a colon use a host-only cookie instead.
The cookie has `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, and a one-hour lifetime.
Setting or clearing the shared cookie also expires the older host-only cookie on the API host.

Shared-cookie authentication trusts every workload receiving requests on matching subdomains, including the hosted MindRoom and Matrix services.
A compromised receiving workload can read the cookie header and replay its Supabase token against platform APIs with that user's permissions; `HttpOnly` and the instance's `ACCOUNT_ID` check do not prevent this.
Use this mode only when those workloads are within the platform's trust boundary.
Deployments that must isolate platform sessions from tenant-controlled code need a separately designed authentication boundary; making this cookie host-only alone breaks the current tenant SSO flow.

## Instance Authentication Modes

The runtime evaluates these modes in order:

1. **Trusted upstream auth**, when enabled, takes precedence over Supabase and standalone auth.
   It requires the configured identity headers and, when configured, verifies the upstream JWT.
   Missing required headers are rejected without falling back to a Supabase cookie.
   Only enable this mode behind a verified access layer that strips client-supplied identity headers and injects authenticated values.
2. **Supabase auth** is enabled by the instance's Supabase URL and anon key.
   It accepts a bearer token or `mindroom_jwt` cookie and rejects a user whose ID differs from `ACCOUNT_ID`.
   Every user of the Supabase project holds a valid token, so `ACCOUNT_ID` is required: without it the runtime logs an error at startup and refuses every Supabase-authenticated request.
   Hosted provisioning sets that account ID for the customer instance and rejects provisioning requests without one.
3. **Standalone auth** applies without Supabase or trusted upstream auth.
   When `MINDROOM_API_KEY` is set, protected endpoints require that key as a bearer token or a dashboard login cookie.
   Without the key, standalone mode does not require authentication.

Cookie-authenticated mutations also require the expected browser origin.
See [Kubernetes Deployment](../../docs/deployment/kubernetes.md) for deployment and trusted upstream settings.

## Matrix Login

The platform's first-party Matrix OIDC endpoints are an additional, opt-in consumer of `mindroom_jwt`.
They validate the user, instance ownership, and subscription before authorizing hosted Matrix login.
A missing or invalid cookie redirects this flow to platform login.
This is separate from the instance dashboard/API Supabase authentication path.

## Key Settings

- Platform backend: `PLATFORM_DOMAIN` controls the shared cookie domain, links, and allowed origins; Supabase URL, anon key, and service key configure platform identity and server operations.
- Instance chart: `supabaseUrl`, `supabaseAnonKey`, and `accountId` configure hosted Supabase authentication and its account check; the chart fails to render when either Supabase value is set without `accountId`.
- Optional access layer: `trustedUpstreamAuth.enabled` selects upstream authentication; its headers and JWT settings must match that layer.
- Standalone runtime: `MINDROOM_API_KEY` enables the standalone credential requirement.

## Troubleshooting

- Missing tenant cookie: check the configured domain, HTTPS, and whether a host-only exception applies.
- API `401`: check the credentials required by the active authentication mode.
- Supabase API `403`: check the configured account ID; cookie-authenticated mutations can also fail origin checks.
- Supabase API `500` reporting that `ACCOUNT_ID` is not set: set it to the instance owner's Supabase user ID (chart value `accountId`).
- With hosted Supabase authentication, unauthenticated dashboard requests redirect to the configured platform login URL.
- Standalone API-key dashboards redirect unauthenticated users to `/login`; unauthenticated API requests return `401`.
- Trusted-upstream authentication failures return `401` without a login redirect, even when a platform login URL is configured.
- Other unauthenticated dashboard requests return `401` when the active mode provides no login redirect.
- Missing bundled frontend assets return `404`; inspect the runtime image and backend logs.
