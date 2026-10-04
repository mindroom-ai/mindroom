# Trusted Upstream Browser Auth

Use trusted upstream auth when the MindRoom API and dashboard sit behind a deployment-owned reverse proxy or identity gateway that has already authenticated the human.
Hosted multi-user deployments need it so the browser that opens an agent-issued OAuth link, such as `/api/oauth/google_drive/authorize?connect_token=...`, signs in as the Matrix requester that triggered it.
The standalone `MINDROOM_OWNER_USER_ID` setting maps every dashboard request to one Matrix user, so it suits only single-owner deployments.
The [Connections portal](#connections-portal), the [MCP Gateway](https://docs.mindroom.chat/deployment/mcp-gateway/), and the [usage export service](https://docs.mindroom.chat/usage/#usage-export-service) also build on this mode.

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

## Connections Portal

The Connections portal at `/connections` lets each signed-in user connect and disconnect their OAuth accounts without administrator dashboard access.
Enable it by naming a private agent:

```bash
MINDROOM_CONNECTIONS_AGENT=personal
MINDROOM_PUBLIC_URL=https://assistant.example.org
```

The named agent must use `private.per: user` or `private.per: user_agent`:

```yaml
agents:
  personal:
    display_name: Personal Mind
    role: Personal assistant
    private:
      per: user_agent
    access:
      users: ["@*:example.org"]
    tools:
      - google_drive
      - name: google_calendar
        defer: true
```

The portal requires [strict JWT mode](#strict-jwt-mode) with a Matrix identity from a signed Matrix claim or the email-to-Matrix template; header-only, API-key, and `MINDROOM_OWNER_USER_ID` authentication are rejected.
Connect and disconnect requests also require `MINDROOM_PUBLIC_URL`, or the request URL when unset, to be an HTTPS origin, and the browser's `Origin` header to match it.

The portal groups OAuth services by agent, covering the named private agent and the shared agents the user may use or manage credentials for.
Services come from each agent's tools, including deferred tools and plugin or MCP OAuth providers; tools that need no browser sign-in are listed too, and room-dependent tools are marked **MindRoom only**.
Each service's status loads independently, so one failing or unconnected service does not block the others.
Using an agent requires a matching `access.users` grant, administrator authority, or membership in a configured grant room; `access.current_room_members` does not apply because browser requests have no current room.
Users listed in `agents.<name>.credential_managers` see that shared agent even without use access, but credential management alone grants no tool access.
Who may manage each connection follows [Who Can Connect An Account](https://docs.mindroom.chat/oauth-framework/#who-can-connect-an-account), and connections made here are the same ones tools use.
Disconnecting a shared connection affects every agent using its [credential scope](https://docs.mindroom.chat/oauth-framework/#where-connections-are-stored).
The portal never shows tokens, model configuration, generic credential editing, or OAuth client settings; operators still configure OAuth clients.
When a service uses a shared service account, such as `GOOGLE_SERVICE_ACCOUNT_FILE`, the portal does not show it as a personal connection and disables **Connect** for that service.
Users choose the agents and tools the optional [MCP Gateway](https://docs.mindroom.chat/deployment/mcp-gateway/#choose-exposed-agents-and-tools) exposes on the same page.

To share a hostname with another frontend, forward `/connections`, `/connections/*`, `/api/connections`, `/api/connections/*`, and `/api/oauth/*` to the MindRoom API behind the authenticated upstream.
Exclude `/connections` from the other application's service-worker navigation fallback.
Portal assets are served under `/connections/assets/`, so root `/assets/` can keep serving the other application.

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
With the Connections portal enabled, ordinary dashboard pages and APIs also require a Matrix identity listed in `administrators`, while the portal and the OAuth callback, success, and reset pages keep their own access checks.

Strict JWT mode adds signature checks but does not replace header stripping, which every network path still needs.
A personal OAuth link opened by a browser signed in as anyone other than the requester who received it fails with `403`; see [Connect An Account](https://docs.mindroom.chat/oauth-framework/#connect-an-account) for link rules.

## Browser Mutation Protection

Changing requests (anything other than `GET`, `HEAD`, `OPTIONS`, or `TRACE`) authenticated by trusted upstream identity or a dashboard cookie must send an `Origin` matching `MINDROOM_PUBLIC_URL`, or the request origin when it is unset.
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
| `403 Administrator access required` | With the portal enabled, only `administrators` reach ordinary dashboard routes |
| `403 Browser changes require a same-origin request` or `Browser changes require a valid public origin` | Open the dashboard from its `MINDROOM_PUBLIC_URL` origin |
| `403 OAuth link does not belong to the current user` | Open the conversation link while signed in as the requester who received it |
| `403 Connections require trusted signed authentication` or `Connections require a verified Matrix identity` | Enable strict JWT mode with a Matrix claim or email-to-Matrix template |
| `403 Connections require a configured private agent` | Point `MINDROOM_CONNECTIONS_AGENT` at an agent with `private.per: user` or `user_agent` |
| `403 No connections are available for this account` | Give the user agent access or list them in `credential_managers` |
| `403 Connections require an HTTPS public origin` or `Connection changes require a same-origin request` | Set an HTTPS `MINDROOM_PUBLIC_URL` and open the portal from that origin |
