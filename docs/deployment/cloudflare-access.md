---
icon: simple/cloudflare
---

# Cloudflare Access & Tunnel

This page puts MindRoom Chat, the Tuwunel homeserver, MatrixRTC calls, and the MindRoom runtime on one host behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/access-controls/), reached through a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/).
Examples use `chat.example.com` for that host, `admin.example.com` for the dashboard, and the Service names of the [Kubernetes charts](kubernetes.md#charts).

## Route Map

`cloudflared` uses the first ingress rule that matches, so order the rules for `chat.example.com` like this:

| Order | Paths | Service |
| --- | --- | --- |
| 1 | Paths with a `.` or `..` segment, a backslash, or a control character | `http_status:404` |
| 2 | `/_matrix/`, `/_synapse/`, `/_tuwunel/`, and exactly `/.well-known/matrix/server` | Tuwunel |
| 3 | The runtime paths listed below | MindRoom runtime |
| 4 | Everything else, including `/.well-known/matrix/client`, `/livekit/jwt/`, and `/livekit/sfu/` | MindRoom Chat |

The first rule keeps a request from passing Access as one path and reaching a service as another, because Access, the tunnel, and the origin can resolve such paths differently.
The client chart serves `/.well-known/matrix/client` only with MatrixRTC or bug reports enabled; otherwise send it to Tuwunel as well.

Runtime paths on `chat.example.com`, each needed only for its feature:

- `/connections`, `/connections/*`, `/api/connections`, `/api/connections/*`, and `/api/oauth/*` for the [Connections portal](../connections-portal.md) and account links
- `/mcp` and the OAuth and discovery paths in the [MCP gateway route table](mcp-gateway.md#route-browser-and-machine-traffic)
- `/api/computers/*`, with WebSocket upgrades, for the [worker computer](../tools/worker-computer.md#reverse-proxy-and-host-routing)
- `/api/triggers/*` for [external triggers](../external-triggers.md)
- `/api/usage/export` and `/api/budgets/service` for the [usage export](../usage.md#usage-export-service) and [budget](../budgets.md#budget-service) services, each under its own Access application
- `/reports/public/*` for [published reports](../tools/agent-orchestration.md)

`admin.example.com` sends every path to the runtime, as described in [Dashboard On Its Own Host](trusted-upstream-auth.md#dashboard-on-its-own-host), behind the same first rule.

```yaml
ingress:
  - hostname: chat.example.com
    path: '[/\\][.]{1,2}([/\\]|$)|[\x00-\x1F\x7F]'
    service: http_status:404
  - hostname: chat.example.com
    path: ^/(_matrix|_synapse|_tuwunel)/
    service: http://matrix-mindroom-tuwunel:8008
  - hostname: chat.example.com
    path: ^/\.well-known/matrix/server$
    service: http://matrix-mindroom-tuwunel:8008
  - hostname: chat.example.com
    path: ^/(connections|api/connections)(/.*)?$
    service: http://mindroom-runtime:8765
  - hostname: chat.example.com
    path: ^/api/oauth/
    service: http://mindroom-runtime:8765
  - hostname: chat.example.com
    service: http://mindroom-client:80
  - hostname: admin.example.com
    path: '[/\\][.]{1,2}([/\\]|$)|[\x00-\x1F\x7F]'
    service: http_status:404
  - hostname: admin.example.com
    service: http://mindroom-runtime:8765
  - service: http_status:404
```

MindRoom Chat's service worker answers browser navigations on its origin with its cached app, so list the other paths users open in the browser on this host in the client chart:

```yaml
serviceWorker:
  navigationFallbackExcludePaths:
    - /connections
    - /mcp
    - /reports
    - /_tuwunel
    - /cdn-cgi
```

`/cdn-cgi` holds Access's own pages on the host, such as `/cdn-cgi/access/logout`.

Point the runtime's `MATRIX_HOMESERVER` (runtime chart `matrix.homeserverUrl`) at Tuwunel's in-cluster Service, as in [Pairing With mindroom-runtime](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#pairing-with-mindroom-runtime), because its Matrix, OpenID, and `/_matrix/client/versions` requests carry no Access session.

## Paths That Bypass Access

Protect each host with one Access application for the whole host, then add an application with a Bypass policy for each path below.
Access applies the most specific path application, so every other path keeps the host's policy.

| Path | Requested without an Access session by |
| --- | --- |
| `/runtime-config.js`, `/authentication-recovery.js` | A cached Chat page whose Access session expired, to [recover sign-in](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md#reverse-proxy-authentication-recovery) |
| `/sw.js`, under `basePath` when set | The browser's service worker update check, which after the session expires gets the Access login instead of the script, so an old worker keeps serving its cached app |
| `/livekit/jwt/*`, `/livekit/sfu/*` | MindRoom voice agents, which request call tokens and join LiveKit from the server, and the MatrixRTC authorization service, which calls LiveKit through its public `auth.livekitUrl` |
| `/.well-known/matrix/server`, `/_matrix/federation/v1/openid/userinfo` | The MatrixRTC authorization service, which checks each caller's Matrix OpenID token with the caller's homeserver |
| The MCP paths that the [route table](mcp-gateway.md#route-browser-and-machine-traffic) marks public or pass-through | MCP clients; with Cloudflare Managed OAuth, follow [External authorization](mcp-gateway.md#external-authorization) instead |
| `/_matrix/client/versions` (optional) | External uptime checks; it answers without a Matrix login, so a passing check shows that Cloudflare, the tunnel, and Tuwunel respond |

Everything else stays behind Access, including `/authentication-recovery-probe`, `/connections`, and all of `/api/`.
Published reports under `/reports/public/` need no MindRoom login, so behind Access only the people Access admits can open them.

A path application also covers every path below it, so a bypass for `/sw.js` also admits `/sw.js/anything` without a session.
Before adding a bypass, make sure every path below it answers 404.
Chat answers unknown paths with its app, so add these guards to the client chart:

```yaml
nginx:
  serverSnippet: |
    location ^~ /runtime-config.js/ { return 404; }
    location ^~ /authentication-recovery.js/ { return 404; }
    location ^~ /sw.js/ { return 404; }
```

## Edge Settings

- **Managed WAF rules** can block Matrix media uploads, which then fail at Cloudflare although the homeserver would accept them.
  Add a WAF skip rule for `POST` requests to `/_matrix/media/v3/upload` and `/_matrix/media/r0/upload`; Tuwunel still requires a Matrix access token for them.
- **Browser Cache TTL** on the zone overrides the `max-age=0` that Chat sends for `/runtime-config.js`, `/authentication-recovery.js`, and `/sw.js` with its own longer lifetime, so browsers can keep stale copies.
  Set it to Respect Existing Headers, or add a response header rule that sets `Cache-Control: no-store, max-age=0` on those paths.
- **Health checks** from scripts can get a Cloudflare challenge instead of the page when they send no User-Agent or a non-standard one, such as a scripting library's default, while browsers pass.
  Send curl's or a browser's User-Agent, and point external checks at a bypassed path such as `/_matrix/client/versions`.

## MindRoom Settings

Configure [trusted upstream auth](trusted-upstream-auth.md#setup) with the Access assertion:

```bash
MINDROOM_PUBLIC_URL=https://chat.example.com
MINDROOM_DASHBOARD_URL=https://admin.example.com
MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED=true
MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER=Cf-Access-Jwt-Assertion
MINDROOM_TRUSTED_UPSTREAM_JWKS_URL=https://<team>.cloudflareaccess.com/cdn-cgi/access/certs
MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER=https://<team>.cloudflareaccess.com
MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE=<AUD tag>
```

`<team>` is the Zero Trust team name, and `<AUD tag>` is the Application Audience (AUD) tag of the host-wide application, which must cover both hosts.

For voice agents, set `calls.livekit_service_url: https://chat.example.com/livekit/jwt` in `config.yaml`, because the runtime cannot read `/.well-known/matrix/client` through Access and agents otherwise ignore calls ([Calls configuration](../voice-calls.md#calls-configuration)).
Desktop bridge clients sign in through Access as described in [Homeservers Behind an Identity-Aware Proxy](../tools/desktop.md#homeservers-behind-an-identity-aware-proxy).
