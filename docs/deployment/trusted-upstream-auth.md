---
icon: lucide/shield-check
---

# Trusted Upstream Browser Auth

Use trusted upstream auth when MindRoom API and browser routes sit behind a deployment-owned access layer that has already authenticated the human.
This mode is disabled by default.
Do not enable it unless the reverse proxy or identity gateway strips client-supplied copies of the trusted headers and injects verified values itself.

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
Derived Matrix IDs must pass MindRoom's Matrix user ID parser.

## Instance Chart

For the hosted instance chart, configure the equivalent values:

```yaml
trustedUpstreamAuth:
  enabled: "true"
  userIdHeader: X-MindRoom-User-Id
  emailHeader: X-MindRoom-User-Email
  matrixUserIdHeader: X-MindRoom-Matrix-User-Id
  emailToMatrixUserIdTemplate: "@{localpart}:example.org"
```

The chart renders these values as the `MINDROOM_TRUSTED_UPSTREAM_*` runtime environment variables.
The instance chart fails rendering when `trustedUpstreamAuth.emailToMatrixUserIdTemplate` is set without `trustedUpstreamAuth.emailHeader`.
When using the platform provisioner, configure the platform chart with matching provisioner values:

```yaml
provisioner:
  trustedUpstreamAuth:
    enabled: "true"
    userIdHeader: X-MindRoom-User-Id
    emailHeader: X-MindRoom-User-Email
    matrixUserIdHeader: X-MindRoom-Matrix-User-Id
    emailToMatrixUserIdTemplate: "@{localpart}:example.org"
```

The platform chart renders these as `INSTANCE_TRUSTED_UPSTREAM_*` variables on the provisioner deployment.

## Security Boundary

Trusted upstream auth is provider-neutral.
Cloudflare Access, an ingress controller, an OAuth2 proxy, or another gateway can provide the headers as long as MindRoom only receives gateway-verified values.
Never expose a MindRoom instance with this mode enabled directly to browsers or the public internet.
If the configured trusted user ID header is missing, MindRoom returns `401`.
If a trusted browser identity does not map to the Matrix requester stored in an OAuth connect token, MindRoom returns `403`.
Existing Supabase platform auth and standalone API-key auth remain available when trusted upstream auth is not enabled.
