---
icon: simple/cloudflare
---

# Cloudflare Access & Tunnel

This page puts MindRoom Chat, the Tuwunel homeserver, MatrixRTC calls, and the MindRoom runtime on one host behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/access-controls/), reached through a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/).
Examples use `chat.example.com` for that host, `admin.example.com` for the dashboard, and the Service names of the [Kubernetes charts](kubernetes.md#charts).

## Tunnel

Make the client chart the only origin of `chat.example.com` with its [single-origin `proxy` values](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md#single-origin), which forward the homeserver and the runtime's public routes, and send [the dashboard host](trusted-upstream-auth.md#dashboard-on-its-own-host) straight to the runtime:

```yaml
ingress:
  - hostname: chat.example.com
    path: '[/\\][.]{1,2}([/\\]|$)|[\x00-\x1F\x7F]'
    service: http_status:404
  - hostname: chat.example.com
    service: http://mindroom-client:80
  - hostname: admin.example.com
    path: '[/\\][.]{1,2}([/\\]|$)|[\x00-\x1F\x7F]'
    service: http_status:404
  - hostname: admin.example.com
    service: http://mindroom-runtime:8765
  - service: http_status:404
```

`cloudflared` uses the first rule that matches, so each host's first rule refuses paths with a `.` or `..` segment between `/` or `\` separators, or with a control character, which Access, the tunnel, and the origin can resolve differently.

Point the runtime's `MATRIX_HOMESERVER` (runtime chart `matrix.homeserverUrl`) at Tuwunel's in-cluster Service, as in [Pairing With mindroom-runtime](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#pairing-with-mindroom-runtime), because its Matrix, OpenID, and `/_matrix/client/versions` requests carry no Access session.

## Paths That Bypass Access

Protect both hosts with one Access application that covers each host as a whole, then add an application with a Bypass policy for each path below.
Access applies the most specific path application, so every other path keeps the host's policy.

| Path | Requested without an Access session by |
| --- | --- |
| `/runtime-config.js`, `/authentication-recovery.js` | A cached Chat page whose Access session expired, to [recover sign-in](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md#reverse-proxy-authentication-recovery) |
| `/sw.js`, under `basePath` when set | The browser's service worker update check, which after the session expires gets the Access login instead of the script, so an old worker keeps serving its cached app |
| `/livekit/jwt/*`, `/livekit/sfu/*` | MindRoom voice agents, which request call tokens and join LiveKit from the server, and the MatrixRTC authorization service, which calls LiveKit through its public `auth.livekitUrl` |
| `/.well-known/matrix/server`, `/_matrix/federation/v1/openid/userinfo` | The MatrixRTC authorization service, which checks each caller's Matrix OpenID token with the caller's homeserver |
| The MCP paths that the [route table](mcp-gateway.md#route-browser-and-machine-traffic) marks public or pass-through | MCP clients; with Cloudflare Managed OAuth, follow [External authorization](mcp-gateway.md#external-authorization) instead |
| `/_matrix/client/versions` (optional) | External uptime checks; it answers without a Matrix login, so a passing check shows that Cloudflare, the tunnel, and Tuwunel respond |

Machine callers of runtime routes need their own Access application with a service token, as the [usage export](../usage.md#usage-export-service) and [budget](../budgets.md#budget-service) services expect, or a bypass that leaves authentication to the route, such as the signed requests of [external triggers](../external-triggers.md).
Everything else stays behind Access, including `/authentication-recovery-probe` and `/connections`.
Published reports under `/reports/public/` need no MindRoom login, so behind Access only the people Access admits can open them.
An Access path application also covers every path below it, so the client chart [answers 404 below its public files](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md#reverse-proxy-authentication-recovery).

## Service Worker

Chat's service worker answers browser navigations on its origin with its cached app, so exclude the other browser pages on this host in the client chart:

```yaml
serviceWorker:
  navigationFallbackExcludePaths:
    - /cdn-cgi
    - /_tuwunel
```

`/cdn-cgi` holds Access's own pages on the host, such as `/cdn-cgi/access/logout`, and `/_tuwunel` holds Tuwunel's browser pages, such as its OIDC sign-in and account pages.

## Edge Settings

- **Managed WAF rules** can block Matrix media uploads, which then fail at Cloudflare although the homeserver would accept them.
  Add a WAF skip rule for `POST` requests to `/_matrix/media/v3/upload` and `/_matrix/media/r0/upload`; Tuwunel still requires a Matrix access token for them.
- **Browser Cache TTL** on the zone overrides the `max-age=0` that Chat sends for `/runtime-config.js`, `/authentication-recovery.js`, and `/sw.js` with its own longer lifetime, so browsers can keep stale copies.
  Set it to Respect Existing Headers, or add a response header rule that sets `Cache-Control: no-store, max-age=0` on those paths.
- **Health checks** from scripts can get a Cloudflare challenge instead of the page when they send no User-Agent or a non-standard one, such as a scripting library's default, while browsers pass.
  Send curl's or a browser's User-Agent, and point external checks at a bypassed path such as `/_matrix/client/versions`.

## MindRoom Settings

```bash
MINDROOM_PUBLIC_URL=https://chat.example.com
MINDROOM_DASHBOARD_URL=https://admin.example.com
MINDROOM_CLOUDFLARE_ACCESS_TEAM=<team>
MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED=true
MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE=<AUD tag>
```

`<team>` is the Zero Trust team name, the part before `.cloudflareaccess.com`, and `<AUD tag>` is the Application Audience (AUD) tag of the host-wide application.
Each feature behind its own Access application, such as MCP external authorization or the usage export and budget services, takes that application's AUD tag as its audience; [Trusted Upstream Browser Auth](trusted-upstream-auth.md) describes what the team name sets.

For voice agents, set `calls.livekit_service_url: https://chat.example.com/livekit/jwt` in `config.yaml`, because the runtime cannot read `/.well-known/matrix/client` through Access and agents otherwise ignore calls ([Calls configuration](../voice-calls.md#calls-configuration)).
Desktop bridge clients sign in through Access as described in [Homeservers Behind an Identity-Aware Proxy](../tools/desktop.md#homeservers-behind-an-identity-aware-proxy).
