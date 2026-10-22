---
icon: lucide/log-in
---

# Trusted Upstream Browser Auth

Use trusted upstream auth when the MindRoom API and dashboard sit behind a deployment-owned identity gateway that has already authenticated the human and signs a JWT saying who they are.
Hosted multi-user deployments need it so the browser that opens an agent-issued OAuth link, such as `/api/oauth/google_drive/authorize?connect_token=...`, signs in as the Matrix requester that triggered it.
The standalone `MINDROOM_OWNER_USER_ID` setting maps every dashboard request to one Matrix user, so it suits only single-owner deployments.
The [usage export service](../usage.md#usage-export-service) and the [budget service](../budgets.md#budget-service) also build on this mode.
The [Connections portal](../connections-portal.md#trusted-upstream-sign-in) can use it, and users signed in to MindRoom Chat can open it without a gateway.

Trusted upstream auth is disabled by default and works with any gateway that publishes a JWKS endpoint and adds a short-lived signed JWT to each authenticated browser request, such as an identity-aware proxy or an OAuth2 proxy.
MindRoom takes the user only from the verified JWT claims and ignores identity headers, so a request that bypasses the gateway cannot pose as a user.
While trusted upstream auth is enabled, it replaces Supabase platform auth and `MINDROOM_API_KEY` auth for the dashboard and its API.

## Setup

```bash
MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED=true
MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER=X-Trusted-Jwt
MINDROOM_TRUSTED_UPSTREAM_JWKS_URL=https://gateway.example.com/.well-known/jwks.json
MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE=mindroom-dashboard
MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER=https://gateway.example.com
MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM=email
MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM=sub
MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM=matrix_user_id
```

| Variable | Meaning |
| --- | --- |
| `MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED` | Turns the mode on; default `false` |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER` | Required; header carrying the JWT |
| `MINDROOM_TRUSTED_UPSTREAM_JWKS_URL` | Required; the gateway's signing keys |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE` | Required; expected `aud` |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER` | Required; expected `iss` |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM` | Claim holding the verified email; default `email` |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM` | Optional; claim holding a stable user ID distinct from the email, which is the user ID otherwise |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM` | Optional; claim holding the user's Matrix ID |
| `MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE` | Optional; derives the Matrix ID from the email localpart and must contain exactly one `{localpart}` placeholder |
| `MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN` | The single email domain allowed for template derivation; required with the template |

MindRoom verifies the signature, expiry, issuer, audience, and every configured claim, and picks up gateway key rotation without a restart.

## Matrix Identity

Shared dashboard access needs only the signed user.
Personal `user` and `user_agent` OAuth connections, the Connections portal, and personal APIs also need a Matrix identity that matches the requester used in conversations.
With a Matrix user ID claim configured, that claim is the Matrix identity.
Otherwise the email-to-Matrix template, when set, derives it from the signed email: with the template `@{localpart}:example.org` and domain `example.com`, `alice@example.com` maps to `@alice:example.org`.
Domain matching is case-insensitive, emails from any other domain or a subdomain are rejected, and the result must be a valid Matrix user ID.

## Helm Charts

For the hosted instance chart, set the equivalent values:

```yaml
trustedUpstreamAuth:
  enabled: "true"
  jwtHeader: X-Trusted-Jwt
  jwksUrl: https://gateway.example.com/.well-known/jwks.json
  jwtAudience: mindroom-dashboard
  jwtIssuer: https://gateway.example.com
  jwtEmailClaim: email
  jwtUserIdClaim: sub
  jwtMatrixUserIdClaim: matrix_user_id
  emailToMatrixUserIdTemplate: "@{localpart}:example.org"
  emailDomain: example.com
```

The chart sets the matching `MINDROOM_TRUSTED_UPSTREAM_*` environment variables.
When instances come from the platform provisioner, put the same values under `provisioner.trustedUpstreamAuth` in the platform chart, which passes them to the provisioner as `INSTANCE_TRUSTED_UPSTREAM_*` variables.
Both charts fail to render when `enabled` is true without `jwtHeader`, `jwksUrl`, `jwtAudience`, and `jwtIssuer`, or when `emailToMatrixUserIdTemplate` is set without `emailDomain`.

## Dashboard On Its Own Host

When another app, such as MindRoom Chat, serves `/` on the public host, serve the dashboard on a separate host that sends every path to MindRoom with the browser's `Host` header unchanged, and set `MINDROOM_DASHBOARD_URL` to that host's origin, for example `https://mindroom-admin.example.com`.
Put that host behind the same access application as the public host, so its requests carry the JWT audience MindRoom already verifies.
The Connections header then links administrators there, and Connections pages opened on that host move to `MINDROOM_PUBLIC_URL`, where sign-in, account links, and MCP consent run.
Accounts connected from the dashboard on that host still return through the public host's OAuth callback.
A Connections sign-in from MindRoom Chat stays on the public host, so administrators reach the dashboard host through the access application or with `MINDROOM_API_KEY`.
A dashboard sign-in with `MINDROOM_API_KEY` also stays on its host, so the first account connected from the dashboard host asks for the key again on the public host before it finishes.
With `MINDROOM_CONNECTIONS_AGENT` set, users outside `administrators` who open that host are sent to the Connections portal, as on the public host.

## Security Boundary

Dashboard configuration is an operator capability.
Without `MINDROOM_CONNECTIONS_AGENT`, every user the upstream authenticates can read and change dashboard configuration regardless of the Matrix `administrators` list, so admit only trusted operators through the gateway in that mode.
With `MINDROOM_CONNECTIONS_AGENT` set, ordinary dashboard pages and APIs also require a Matrix identity listed in `administrators`, while the Connections portal and the OAuth callback, success, and reset pages keep their own access checks.
A user outside `administrators` who opens a dashboard page in a browser is redirected with 303 to `/connections?notice=admin-only`, where the Connections page says the dashboard is for administrators; API calls keep the 403 JSON.
A [Connections session from MindRoom Chat](../connections-portal.md#sessions-and-dashboard-access) reaches dashboard routes only for a user in `administrators` whose token the homeserver bound to this deployment.

A personal OAuth link opened by a browser signed in as anyone other than the requester who received it fails with `403`; see [Connect An Account](../oauth-framework.md#connect-an-account) for link rules.

## Browser Mutation Protection

Changing requests (anything other than `GET`, `HEAD`, `OPTIONS`, or `TRACE`) authenticated by trusted upstream identity, a dashboard cookie, or a Connections sign-in session must send an `Origin` matching `MINDROOM_PUBLIC_URL`, or the request origin when it is unset; on the `MINDROOM_DASHBOARD_URL` host they must send that origin instead.
Requests marked `Sec-Fetch-Site: cross-site` are rejected even when the `Origin` matches.
A validated bearer token skips these checks, but adding a bearer header to a trusted-upstream request does not.
`MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS` controls which origins may read credentialed responses and never authorizes cross-origin changes.
Host the dashboard on the app's public origin, or use its development proxy, which authenticates with a bearer token.

## Troubleshooting

| Status and message | Fix |
| --- | --- |
| `500 Trusted upstream auth is enabled but <variable> is not set` | Set the named variable |
| `500 Trusted upstream email mapping requires a valid template and MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN` | Use exactly one `{localpart}` in a valid Matrix ID template and set the email domain |
| `401 Missing trusted upstream JWT header: <header>` | The gateway did not send its assertion; check its routing and that it signs requests for this application |
| `401 Invalid trusted upstream JWT` | The JWT is expired, signed by an unknown key, for the wrong issuer or audience, or lacks a configured claim |
| `401 Invalid trusted upstream email identity` | The email is outside `MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN` |
| `401 Invalid trusted upstream Matrix user id` | The Matrix claim is not a valid Matrix user ID |
| `403 Administrator access required` | With `MINDROOM_CONNECTIONS_AGENT` set, only `administrators` reach ordinary dashboard routes |
| `403 Browser changes require a same-origin request` or `Browser changes require a valid public origin` | Open the dashboard from its `MINDROOM_PUBLIC_URL` origin |
| `403 OAuth link does not belong to the current user` | Open the conversation link while signed in as the requester who received it |
| Connections portal errors | See [Connections troubleshooting](../connections-portal.md#troubleshooting) |
