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
```

`MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER` is required when trusted upstream auth is enabled.
The user ID value must be stable for the authenticated browser user.
`MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER` is optional and is stored in `request.scope["auth_user"]["email"]` when present.
`MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER` is optional for shared dashboard access but required for private `user` and `user_agent` OAuth flows unless you configure an email mapping template.

Use an email mapping template only when your Matrix IDs are deterministically derived from email:

```bash
MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE=@{localpart}:matrix.example.com
```

The template can use `{user_id}`, `{email}`, `{localpart}`, and `{domain}`.
MindRoom only accepts the result when it looks like a Matrix user ID.

## Security Boundary

Trusted upstream auth is provider-neutral.
Cloudflare Access, an ingress controller, an OAuth2 proxy, or another gateway can provide the headers as long as MindRoom only receives gateway-verified values.
Never expose a MindRoom instance with this mode enabled directly to browsers or the public internet.
If the configured trusted user ID header is missing, MindRoom returns `401`.
If a trusted browser identity does not map to the Matrix requester stored in an OAuth connect token, MindRoom returns `403`.
Existing Supabase platform auth and standalone API-key auth remain available when trusted upstream auth is not enabled.
