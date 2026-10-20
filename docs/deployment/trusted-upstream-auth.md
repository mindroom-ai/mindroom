---
icon: lucide/log-in
---

# Trusted Upstream Browser Auth

Use trusted upstream auth when the MindRoom API and dashboard sit behind a deployment-owned reverse proxy or identity gateway that has already authenticated the human.
Hosted multi-user deployments need it so the browser that opens an agent-issued OAuth link, such as `/api/oauth/google_drive/authorize?connect_token=...`, signs in as the Matrix requester that triggered it.
The standalone `MINDROOM_OWNER_USER_ID` setting maps every dashboard request to one Matrix user, so it suits only single-owner deployments.
The [MCP Gateway](mcp-gateway.md), the [usage export service](../usage.md#usage-export-service), and the [budget service](../budgets.md#budget-service) also build on this mode.
The [Connections portal](../connections-portal.md#trusted-upstream-sign-in) can use strict JWT mode, and users signed in to MindRoom Chat can open it without a gateway.

Trusted upstream auth is disabled by default and works with any gateway, such as an ingress controller, OAuth2 proxy, or identity-aware proxy.
Enable it only when every network path to MindRoom removes client-supplied copies of the trusted headers and injects verified values itself; never expose such an instance directly to browsers or the public internet.
Header-only mode trusts those headers alone and suits deployments reachable only through the gateway.
Prefer [strict JWT mode](#strict-jwt-mode) whenever the gateway can sign an assertion.
While trusted upstream auth is enabled, it replaces Supabase platform auth and `MINDROOM_API_KEY` auth for the dashboard and its API.

## Header Setup

Set the header names that your access layer owns:

```bash
MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED=true
MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER=X-MindRoom-User-Id
MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER=X-MindRoom-User-Email
MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER=X-MindRoom-Matrix-User-Id
MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE='@{localpart}:example.org'
MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN=example.com
```

| Variable | Meaning |
| --- | --- |
| `MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED` | Turns the mode on; default `false` |
| `MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER` | Required; a header carrying a stable ID for the authenticated browser user |
| `MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER` | Optional; header carrying the user's email, required in header-only mode when an email-to-Matrix template is set |
| `MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER` | Optional; header carrying the user's Matrix ID |
| `MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE` | Optional; derives the Matrix ID from the email localpart and must contain exactly one `{localpart}` placeholder |
| `MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN` | The single email domain allowed for template derivation; required with the template |

Shared dashboard access needs only the user ID header.
Personal `user` and `user_agent` OAuth connections, the Connections portal, and personal APIs also need a Matrix identity that matches the requester used in conversations.
Prefer the Matrix user ID header when the access layer can supply a real Matrix ID; header-only mode uses it when present and otherwise derives the ID from the email.
With the template `@{localpart}:example.org` and domain `example.com`, `alice@example.com` maps to `@alice:example.org`.
Domain matching is case-insensitive, emails from any other domain or a subdomain are rejected, and the result must be a valid Matrix user ID.

## Strict JWT Mode

Strict mode requires every request to carry a signed JWT from the gateway alongside the identity headers, so a spoofed identity header alone is rejected.
Use it when the gateway publishes a JWKS endpoint and issues short-lived assertions for authenticated browser requests.

```bash
MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT=true
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
| `MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT` | Turns strict mode on; default `false` |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER` | Required; header carrying the JWT |
| `MINDROOM_TRUSTED_UPSTREAM_JWKS_URL` | Required; the gateway's signing keys |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE` | Required; expected `aud` |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER` | Required; expected `iss` |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM` | Claim holding the verified email; default `email` |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM` | Optional; claim holding a stable user ID distinct from the email |
| `MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM` | Optional; claim holding a signed Matrix user ID |

MindRoom verifies the signature, expiry, issuer, audience, and every configured claim, and picks up gateway key rotation without a restart.
Headers must agree with the verified claims:

- The user ID header must equal the user ID claim when configured, and otherwise the email claim.
- A configured email header must equal the email claim; without an email header, MindRoom uses the email claim.
- With a Matrix user ID claim, that claim is the Matrix identity, and a Matrix user ID header must equal it.
- Without one, the Matrix identity can only come from the email-to-Matrix template applied to the verified email, which works without an email header, and a Matrix user ID header must equal the derived ID.
- With neither a Matrix claim nor a template, any Matrix user ID header is rejected because no signature backs it.

## Helm Charts

For the hosted instance chart, set the equivalent values:

```yaml
trustedUpstreamAuth:
  enabled: "true"
  userIdHeader: X-MindRoom-User-Id
  emailHeader: X-MindRoom-User-Email
  matrixUserIdHeader: X-MindRoom-Matrix-User-Id
  emailToMatrixUserIdTemplate: "@{localpart}:example.org"
  emailDomain: example.com
  requireJwt: "true"
  jwtHeader: X-Trusted-Jwt
  jwksUrl: https://gateway.example.com/.well-known/jwks.json
  jwtAudience: mindroom-dashboard
  jwtIssuer: https://gateway.example.com
  jwtEmailClaim: email
  jwtUserIdClaim: sub
  jwtMatrixUserIdClaim: matrix_user_id
```

The chart sets the matching `MINDROOM_TRUSTED_UPSTREAM_*` environment variables.
When instances come from the platform provisioner, put the same values under `provisioner.trustedUpstreamAuth` in the platform chart, which passes them to the provisioner as `INSTANCE_TRUSTED_UPSTREAM_*` variables.
Both charts fail to render when `enabled` is true without `userIdHeader`, when `emailToMatrixUserIdTemplate` is set without both `emailHeader` and `emailDomain`, or when `requireJwt` is true without `jwtHeader`, `jwksUrl`, `jwtAudience`, and `jwtIssuer`.

## Security Boundary

Dashboard configuration is an operator capability.
Without `MINDROOM_CONNECTIONS_AGENT`, every user the upstream authenticates can read and change dashboard configuration regardless of the Matrix `administrators` list, so admit only trusted operators through the gateway in that mode.
With `MINDROOM_CONNECTIONS_AGENT` set, ordinary dashboard pages and APIs also require a Matrix identity listed in `administrators`, while the Connections portal and the OAuth callback, success, and reset pages keep their own access checks.
A user outside `administrators` who opens a dashboard page in a browser is redirected with 303 to `/connections?notice=admin-only`, where the Connections page says the dashboard is for administrators; API calls keep the 403 JSON.
A [Connections session from MindRoom Chat](../connections-portal.md#sessions-and-dashboard-access) reaches dashboard routes only for a user in `administrators` whose token the homeserver bound to this deployment.

Strict JWT mode adds signature checks but does not replace header stripping, which every network path still needs.
A personal OAuth link opened by a browser signed in as anyone other than the requester who received it fails with `403`; see [Connect An Account](../oauth-framework.md#connect-an-account) for link rules.

## Browser Mutation Protection

Changing requests (anything other than `GET`, `HEAD`, `OPTIONS`, or `TRACE`) authenticated by trusted upstream identity, a dashboard cookie, or a Connections sign-in session must send an `Origin` matching `MINDROOM_PUBLIC_URL`, or the request origin when it is unset.
Requests marked `Sec-Fetch-Site: cross-site` are rejected even when the `Origin` matches.
A validated bearer token skips these checks, but adding a bearer header to a trusted-upstream request does not.
`MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS` controls which origins may read credentialed responses and never authorizes cross-origin changes.
Host the dashboard on the app's public origin, or use its development proxy, which authenticates with a bearer token.

## Troubleshooting

| Status and message | Fix |
| --- | --- |
| `500 Trusted upstream auth is enabled but MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER is not set` | Set the user ID header variable |
| `500 Trusted upstream strict JWT auth is enabled but <variable> is not set` | Set the named strict-mode variable |
| `500 Trusted upstream email-to-Matrix template is set but MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER is not set` | In header-only mode, set the email header or use the Matrix user ID header instead |
| `500 Trusted upstream email mapping requires a valid template and MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN` | Use exactly one `{localpart}` in a valid Matrix ID template and set the email domain |
| `401 Missing trusted upstream identity header: <header>`, `Missing trusted upstream email header: <header>`, or `Missing trusted upstream JWT header: <header>` | The gateway did not send the header; check its routing and header injection |
| `401 Invalid trusted upstream JWT` | The JWT is expired, signed by an unknown key, for the wrong issuer or audience, or lacks a configured claim |
| `401 Trusted upstream identity does not match JWT claim` | Make the user ID and email headers carry the same values as the JWT claims |
| `401 Trusted upstream Matrix identity does not match JWT claim`, `does not match verified email`, or `is not signed` | Make the Matrix user ID header match the signed or derived ID, configure a Matrix claim or template, or stop sending the header |
| `401 Invalid trusted upstream email identity` | The email is outside `MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN` |
| `401 Invalid trusted upstream Matrix user id` | The Matrix header or claim is not a valid Matrix user ID |
| `403 Administrator access required` | With `MINDROOM_CONNECTIONS_AGENT` set, only `administrators` reach ordinary dashboard routes |
| `403 Browser changes require a same-origin request` or `Browser changes require a valid public origin` | Open the dashboard from its `MINDROOM_PUBLIC_URL` origin |
| `403 OAuth link does not belong to the current user` | Open the conversation link while signed in as the requester who received it |
| Connections portal errors | See [Connections troubleshooting](../connections-portal.md#troubleshooting) |
