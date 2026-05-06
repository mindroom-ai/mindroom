# Trusted Upstream Browser Auth

Use trusted upstream auth when MindRoom API and browser routes sit behind a deployment-owned access layer that has already authenticated the human.
This mode is disabled by default.
Do not enable it unless the reverse proxy or identity gateway strips client-supplied copies of the trusted headers and injects verified values itself.
Header-only mode is a compatibility option for deployments where MindRoom is only reachable through that trusted gateway.
Prefer strict JWT mode when the gateway can provide a signed upstream assertion.

## Why It Exists

Agent-issued OAuth links are normal browser links such as `/api/oauth/google_drive/authorize?connect_token=...`.
The connect token records the Matrix requester that triggered the missing-credentials tool result.
In a hosted multi-user private-agent deployment, the browser opening that link must authenticate as the same requester.
The standalone `MINDROOM_OWNER_USER_ID` setting maps every dashboard request to one Matrix user, so it is only appropriate for single-owner deployments.
It is not a hosted multi-user identity solution.

## Environment

Configure the header names that your access layer owns:

```bash
MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED=true
MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER=X-MindRoom-User-Id
MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER=X-MindRoom-User-Email
MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER=X-MindRoom-Matrix-User-Id
MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE='@{localpart}:example.org'
```

`MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER` is required when trusted upstream auth is enabled.
The user ID value must be stable for the authenticated browser user.
`MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER` is optional unless `MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE` is set.
When the email-to-Matrix template is set, `MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER` is required because MindRoom derives the Matrix localpart from that trusted email value.
When present, the email value is stored in `request.scope["auth_user"]["email"]`.
`MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER` is optional for shared dashboard access.
For private `user` and `user_agent` OAuth flows, the trusted identity must resolve to the requester identity used by Matrix-backed tool execution.
Prefer `MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER` when your access layer can supply a real Matrix ID.
When the access layer only supplies email, set `MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE` to derive the Matrix ID from the trusted email localpart.
For example, the template `@{localpart}:example.org` maps `alice@example.com` to `@alice:example.org`.
The template must contain exactly one `{localpart}` placeholder.
Derived Matrix IDs must pass MindRoom's Matrix user ID parser.

## Strict JWT Mode

Strict mode requires each trusted-upstream request to carry both the configured identity headers and a signed JWT from the upstream gateway.
Enable it when the gateway publishes a JWKS endpoint and issues short-lived assertions for authenticated browser requests.
In strict mode, spoofing the trusted identity header alone is not enough because MindRoom verifies the JWT signature, expiry, issuer, audience, and configured email claim before accepting the request.

```bash
MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT=true
MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER=X-Trusted-Jwt
MINDROOM_TRUSTED_UPSTREAM_JWKS_URL=https://gateway.example.com/.well-known/jwks.json
MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE=mindroom-dashboard
MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER=https://gateway.example.com
MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM=email
MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM=sub
```

`MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT` is disabled by default to preserve existing header-only deployments.
When it is set to true, `MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER`, `MINDROOM_TRUSTED_UPSTREAM_JWKS_URL`, `MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE`, and `MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER` are required.
`MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM` defaults to `email`.
Set `MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM` when the trusted user ID header contains a stable ID that is distinct from the email address.
MindRoom caches the JWKS response briefly and refreshes it automatically so key rotation can take effect without fetching keys on every request.
If the JWT is missing, expired, signed by an unknown key, issued by the wrong issuer, meant for the wrong audience, missing the configured email claim, or inconsistent with the configured trusted email header, MindRoom returns `401`.
When `MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM` is set, the trusted user ID header must match that verified JWT claim.
When `MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM` is not set, the trusted user ID header must match the verified email claim because no separate signed user ID is available.
When a trusted email header is configured, that email value must match `MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM`.

## Instance Chart

For the hosted instance chart, configure the equivalent values:

```yaml
trustedUpstreamAuth:
  enabled: "true"
  userIdHeader: X-MindRoom-User-Id
  emailHeader: X-MindRoom-User-Email
  matrixUserIdHeader: X-MindRoom-Matrix-User-Id
  emailToMatrixUserIdTemplate: "@{localpart}:example.org"
  requireJwt: "true"
  jwtHeader: X-Trusted-Jwt
  jwksUrl: https://gateway.example.com/.well-known/jwks.json
  jwtAudience: mindroom-dashboard
  jwtIssuer: https://gateway.example.com
  jwtEmailClaim: email
  jwtUserIdClaim: sub
```

The chart renders these values as the `MINDROOM_TRUSTED_UPSTREAM_*` runtime environment variables.
The instance chart fails rendering when `trustedUpstreamAuth.emailToMatrixUserIdTemplate` is set without `trustedUpstreamAuth.emailHeader`.
The instance chart also fails rendering when `trustedUpstreamAuth.requireJwt` is true without `jwtHeader`, `jwksUrl`, `jwtAudience`, or `jwtIssuer`.
The template value must contain exactly one `{localpart}` placeholder.
When using the platform provisioner, configure the platform chart with matching provisioner values:

```yaml
provisioner:
  trustedUpstreamAuth:
    enabled: "true"
    userIdHeader: X-MindRoom-User-Id
    emailHeader: X-MindRoom-User-Email
    matrixUserIdHeader: X-MindRoom-Matrix-User-Id
    emailToMatrixUserIdTemplate: "@{localpart}:example.org"
    requireJwt: "true"
    jwtHeader: X-Trusted-Jwt
    jwksUrl: https://gateway.example.com/.well-known/jwks.json
    jwtAudience: mindroom-dashboard
    jwtIssuer: https://gateway.example.com
    jwtEmailClaim: email
    jwtUserIdClaim: sub
```

The platform chart renders these as `INSTANCE_TRUSTED_UPSTREAM_*` variables on the provisioner deployment.
The platform chart fails rendering when `provisioner.trustedUpstreamAuth.emailToMatrixUserIdTemplate` is set without `provisioner.trustedUpstreamAuth.emailHeader`.
The platform chart also fails rendering when `provisioner.trustedUpstreamAuth.requireJwt` is true without `jwtHeader`, `jwksUrl`, `jwtAudience`, or `jwtIssuer`.

## Security Boundary

Trusted upstream auth is provider-neutral.
A reverse proxy, ingress controller, OAuth2 proxy, or another gateway can provide the headers as long as MindRoom only receives gateway-verified values.
Never expose a MindRoom instance with this mode enabled directly to browsers or the public internet.
In header-only mode, every network path to MindRoom must remove any client-provided copies of the trusted headers before adding authenticated values.
In strict JWT mode, the same header-stripping requirement still applies, and MindRoom additionally validates the signed assertion.
If the configured trusted user ID header is missing, MindRoom returns `401`.
If strict JWT mode is enabled and the configured JWT header is missing or invalid, MindRoom returns `401`.
If a trusted browser identity does not map to the Matrix requester stored in an OAuth connect token, MindRoom returns `403`.
Existing Supabase platform auth and standalone API-key auth remain available when trusted upstream auth is not enabled.
