# Cloudflare Access & Tunnel

This page puts MindRoom Chat, the Tuwunel homeserver, MatrixRTC calls, and the MindRoom runtime on one host behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/access-controls/), reached through a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/).
Examples use `chat.example.com` for that host, `admin.example.com` for the dashboard, and the Service names of the [Kubernetes charts](https://docs.mindroom.chat/deployment/kubernetes/#charts).

## Tunnel

`cloudflared` uses the first ingress rule that matches, so order the rules for `chat.example.com` like this:

| Order | Paths | Service |
| --- | --- | --- |
| 1 | Paths with a `.` or `..` segment between `/` or `\` separators, or with a control character | `http_status:404` |
| 2 | `/_matrix/`, `/_synapse/`, `/_tuwunel/`, and `/.well-known/matrix/server` with everything below it | Tuwunel |
| 3 | The public paths of each runtime feature you use, as that feature's page lists them | MindRoom runtime |
| 4 | Everything else, including `/.well-known/matrix/client`, `/livekit/jwt/`, and `/livekit/sfu/` | MindRoom Chat |

The first rule keeps a request from passing Access as one path and reaching a service as another, because Access, the tunnel, and the origin can resolve such paths differently.
The client chart serves `/.well-known/matrix/client` only with MatrixRTC, bug reports, or a [server emoji room](https://docs.mindroom.chat/custom-emoji/) enabled; otherwise send it to Tuwunel as well.
Runtime features with public paths include the [OpenAI-compatible API](https://docs.mindroom.chat/openai-api/), the [Connections portal](https://docs.mindroom.chat/connections-portal/#trusted-upstream-sign-in), the [MCP gateway](https://docs.mindroom.chat/deployment/mcp-gateway/#route-browser-and-machine-traffic), the [worker computer](https://docs.mindroom.chat/tools/worker-computer/#reverse-proxy-and-host-routing), [external triggers](https://docs.mindroom.chat/external-triggers/), [Slack events](https://docs.mindroom.chat/tools/slack/#set-up-the-slack-app), the [usage export](https://docs.mindroom.chat/usage/#usage-export-service) and [budget](https://docs.mindroom.chat/budgets/#budget-service) services, and [published](https://docs.mindroom.chat/tools/agent-orchestration/#report_publishing) and [private](https://docs.mindroom.chat/tools/agent-orchestration/#dynamic_workflow) reports.
`admin.example.com` sends every path to the runtime, as described in [Dashboard On Its Own Host](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#dashboard-on-its-own-host), behind the same first rule.

```yaml
ingress:
  - hostname: chat.example.com
    path: '[/\\][.]{1,2}([/\\]|$)|[\x00-\x1F\x7F]'
    service: http_status:404
  - hostname: chat.example.com
    path: ^/(_matrix|_synapse|_tuwunel)/|^/\.well-known/matrix/server(/|$)
    service: http://matrix-mindroom-tuwunel:8008
  - hostname: chat.example.com
    path: ^/(connections|api/connections)(/.*)?$|^/api/oauth/
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

The example routes only the Connections paths to the runtime; add a rule like it for each other feature you use.

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
| The MCP paths that the [route table](https://docs.mindroom.chat/deployment/mcp-gateway/#route-browser-and-machine-traffic) marks public or pass-through | MCP clients; with Cloudflare Managed OAuth, follow [External authorization](https://docs.mindroom.chat/deployment/mcp-gateway/#external-authorization) instead |
| `POST /api/slack/events` | Slack, whose [event requests](https://docs.mindroom.chat/tools/slack/#set-up-the-slack-app) MindRoom accepts only with a valid Slack signature |
| `/_matrix/client/versions` (optional) | External uptime checks; it answers without a Matrix login, so a passing check shows that Cloudflare, the tunnel, and Tuwunel respond |

Machine callers of runtime routes need their own Access application with a service token, as the [usage export](https://docs.mindroom.chat/usage/#usage-export-service) and [budget](https://docs.mindroom.chat/budgets/#budget-service) services expect, or a bypass that leaves authentication to the route, such as the signed requests of [external triggers](https://docs.mindroom.chat/external-triggers/) or the API keys of the [OpenAI-compatible API](https://docs.mindroom.chat/openai-api/).
Everything else stays behind Access, including `/authentication-recovery-probe` and `/connections`.
Published reports under `/reports/public/` need no MindRoom login, so behind Access only the people Access admits can open them.
An Access path application also covers every path below it, so the client chart [answers 404 below its public files](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md#reverse-proxy-authentication-recovery), and the tunnel sends everything below `/.well-known/matrix/server` to Tuwunel, which answers 404.

## Service Worker

Chat's service worker answers browser navigations on its origin with its cached app.
Chat images built from [mindroom-chat#516](https://github.com/mindroom-ai/mindroom-chat/pull/516) or later leave `/connections`, `/mcp`, and `/reports` to the network, so list only the other browser pages on this host in the client chart:

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

Configure [trusted upstream auth](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#setup) with the Access assertion:

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
Each feature behind its own Access application, such as MCP external authorization or the usage export and budget services, takes that application's AUD tag as its audience.

For voice agents, set `calls.livekit_service_url: https://chat.example.com/livekit/jwt` in `config.yaml`, because the runtime cannot read `/.well-known/matrix/client` through Access and agents otherwise ignore calls ([Calls configuration](https://docs.mindroom.chat/voice-calls/#calls-configuration)).
Desktop bridge clients sign in through Access as described in [Homeservers Behind an Identity-Aware Proxy](https://docs.mindroom.chat/tools/desktop/#homeservers-behind-an-identity-aware-proxy).
