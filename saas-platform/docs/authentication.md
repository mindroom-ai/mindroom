# Authentication Overview

The platform app and API run in `mindroom-{environment}`; hosted customer instances share the `mindroom-instances` namespace.
Each instance's MindRoom backend serves its bundled dashboard and API.

## Hosted Supabase Login

1. The user signs in to the platform app and obtains a Supabase session.
2. The frontend calls `POST /my/sso-cookie` on the platform API with the Supabase access token as a bearer token.
3. The platform backend validates the token and sets the `__Host-mindroom_jwt` cookie on the platform API host.
4. An unauthenticated instance dashboard request redirects to `GET /instance-sso/authorize` on the platform API with the dashboard URL as `redirect_to`.
5. The platform validates the `__Host-mindroom_jwt` cookie, verifies that the user owns the instance named by the dashboard host and that its subscription allows sign-in, and redirects to the instance's `/api/auth/platform-sso` with a login ticket.
6. The runtime verifies the ticket, checks the user against its configured `ACCOUNT_ID`, and sets its own `__Host-mindroom_platform_session` cookie.

The `__Host-` prefix makes browsers keep the cookie host-only, so they never send the platform's Supabase token to tenant MindRoom or Matrix hosts, and tenant hosts cannot overwrite it.
It has `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, and a one-hour lifetime.
Setting or clearing it also expires the `mindroom_jwt` cookie with `Domain=.PLATFORM_DOMAIN` written by older releases.

The login ticket is an HS256 JWT signed with a key derived for that one instance, and it expires after 60 seconds.
It names the instance dashboard origin as its audience, and the runtime accepts each ticket once.
The runtime signs its one-hour session cookie with the same instance key, so a ticket or session from one instance does not verify on another.
Neither artefact is a Supabase token, so the platform API and Matrix OIDC endpoints reject both.
An instance owner can read their own instance key, which only lets them sign in to their own instance.
Platform logout clears `__Host-mindroom_jwt` but cannot clear instance cookies, so an existing instance session stays valid until its one-hour expiry.

## Upgrading to Instance-Signed Login

Instances provisioned before dashboard SSO have no `platform_sso_secret`, and the new platform no longer sends its cookie to instance hosts.
Deploy the platform backend and then re-provision every instance through the provisioner so each one receives its instance key, `MINDROOM_PLATFORM_SSO_URL`, and `platformDomain`.
Re-provision right after the platform deploy: until then, browser dashboard login fails, because older runtimes still wait for the retired shared cookie and newer runtimes without the key return `401`.
Re-provisioning also rolls the instance pod onto the current runtime image.

## Instance Authentication Modes

The runtime evaluates these modes in order:

1. **Trusted upstream auth**, when enabled, takes precedence over Supabase and standalone auth.
   It requires the configured identity headers and, when configured, verifies the upstream JWT.
   Missing required headers are rejected without falling back to a Supabase cookie.
   Only enable this mode behind a verified access layer that strips client-supplied identity headers and injects authenticated values.
2. **Supabase auth** is enabled by the instance's Supabase URL and anon key.
   It accepts a Supabase bearer token or a platform session cookie and rejects a user whose ID differs from a configured `ACCOUNT_ID`.
   Browser sign-in also requires `MINDROOM_PLATFORM_SSO_URL` and the instance key in `MINDROOM_PLATFORM_SSO_SECRET`.
   Hosted provisioning sets the account ID and instance key for the customer instance.
3. **Standalone auth** applies without Supabase or trusted upstream auth.
   When `MINDROOM_API_KEY` is set, protected endpoints require that key as a bearer token or a dashboard login cookie.
   Without the key, standalone mode does not require authentication.

Cookie-authenticated mutations also require the expected browser origin.
See [Kubernetes Deployment](../../docs/deployment/kubernetes.md) for deployment and trusted upstream settings.

## Matrix Login

The platform's first-party Matrix OIDC endpoints on the platform API host are an additional, opt-in consumer of `__Host-mindroom_jwt`.
They apply the same user, instance ownership, and subscription check as dashboard login before authorizing hosted Matrix login.
A missing or invalid cookie redirects this flow to platform login.
This is separate from the instance dashboard/API Supabase authentication path.

## Key Settings

- Platform backend: `PLATFORM_DOMAIN` controls links, allowed origins, and the legacy shared-cookie expiry; `INSTANCE_BASE_DOMAIN` selects the dashboard hosts that may receive login tickets; `INSTANCE_CREDENTIALS_ENCRYPTION_SECRET`, or `PROVISIONER_API_KEY` when it is unset, is the root of each instance key; Supabase URL, anon key, and service key configure platform identity and server operations.
- Instance chart: `supabaseUrl`, `supabaseAnonKey`, and `accountId` configure hosted Supabase authentication and its account check; `platformDomain`, defaulting to `baseDomain`, selects the platform login and SSO hosts; the instance Secret's `platform_sso_secret` holds the instance key.
- Optional access layer: `trustedUpstreamAuth.enabled` selects upstream authentication; its headers and JWT settings must match that layer.
- Standalone runtime: `MINDROOM_API_KEY` enables the standalone credential requirement.

## Troubleshooting

- Dashboard `401` without a platform redirect: the instance lacks `platform_sso_secret`; re-provision the instance so the provisioner writes it.
- Dashboard login `401` from `/api/auth/platform-sso`: the ticket expired, was already used, or was signed for another instance.
- API `401`: check the credentials required by the active authentication mode.
- Supabase API `403`: check the configured account ID; cookie-authenticated mutations can also fail origin checks.
- With hosted Supabase authentication and an instance key, unauthenticated dashboard requests redirect to `MINDROOM_PLATFORM_SSO_URL`.
- Standalone API-key dashboards redirect unauthenticated users to `/login`; unauthenticated API requests return `401`.
- Trusted-upstream authentication failures return `401` without a login redirect, even when `MINDROOM_PLATFORM_SSO_URL` is configured.
- Other unauthenticated dashboard requests return `401` when the active mode provides no login redirect.
- Missing bundled frontend assets return `404`; inspect the runtime image and backend logs.
