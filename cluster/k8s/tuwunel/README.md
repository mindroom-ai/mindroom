# MindRoom Tuwunel Chart

This chart deploys the [MindRoom Tuwunel fork](https://github.com/mindroom-ai/mindroom-tuwunel) as a standalone single-replica Matrix homeserver.
Use it together with the `mindroom-runtime` chart when MindRoom should talk to a chart-managed Tuwunel instead of an externally provided homeserver.
The chart renders `tuwunel.toml`, creates the Deployment, Service, and storage PVC, and leaves ingress, TLS, and apex well-known delegation to your cluster.

## Minimal Install

```bash
helm upgrade --install matrix ./cluster/k8s/tuwunel \
  --namespace mindroom \
  --create-namespace \
  --set tuwunel.serverName=example.com
```

`tuwunel.serverName` is the domain in Matrix user IDs and Tuwunel cannot change it after the first start without a database wipe.
`tuwunel.clientBaseUrl` is the public HTTPS URL where clients reach the homeserver and defaults to `https://<serverName>`.
Set it explicitly when the homeserver is served from a subdomain such as `https://matrix.example.com`.

## Config Rendering and Secrets

The chart renders the complete `tuwunel.toml` into a ConfigMap and never writes secret material into it.
Tuwunel natively reads `registration_token_file` and `client_secret_file`, so the rendered config references file paths backed by Secret volume mounts instead of inline values.
The alternative of an init container substituting secrets into a config template at pod start was rejected: it adds a moving part, hides the effective config from `helm template` and GitOps diffs, and is unnecessary because Tuwunel's native `*_file` options already keep secrets out of the config file.
A `checksum/config` pod annotation rolls the Deployment whenever the rendered config changes.

Secret files are mounted at fixed paths that the rendered config references:

| Value | Mounted file |
|-------|--------------|
| `tuwunel.registrationToken` | `/etc/tuwunel/secrets/registration-token/<key>` |
| `tuwunel.oidc.clientSecret` | `/etc/tuwunel/secrets/oidc/<key>` |

## Registration Token

Setting `tuwunel.registrationToken.existingSecret` enables token-gated registration and points Tuwunel at the mounted token file:

```yaml
tuwunel:
  serverName: example.com
  registrationToken:
    existingSecret: matrix-registration
    key: MATRIX_REGISTRATION_TOKEN
```

Without it, the rendered config leaves registration at Tuwunel's default of disabled.
MindRoom provisions agent accounts through registration, so a paired runtime deployment needs the token configured on both charts.

## OIDC Login

```yaml
tuwunel:
  serverName: example.com
  clientBaseUrl: https://matrix.example.com
  oidc:
    enabled: true
    brand: keycloak
    issuer: https://sso.example.com/realms/example
    clientId: tuwunel
    clientSecret:
      existingSecret: matrix-oidc
      key: OIDC_CLIENT_SECRET
    scope: [openid, profile, email]
```

The callback URL defaults to `<clientBaseUrl>/_matrix/client/unstable/login/sso/callback/<clientId>` and must be registered with the provider exactly as rendered.
`tuwunel.oidc.extraConfig` appends raw TOML inside the `[[global.identity_provider]]` block for less common options such as `userid_claims`, `trusted`, or `unique_id_fallbacks`.
For multiple identity providers, use a fully custom config through `config.existingConfigMap`.

## Custom Config

Operators with a fully custom `tuwunel.toml` can bypass chart rendering entirely:

```yaml
config:
  existingConfigMap: tuwunel-config
  key: tuwunel.toml
```

The Secret mounts from `tuwunel.registrationToken` and `tuwunel.oidc` still apply, so a custom config can reference the chart's standard secret file paths from the table above.
Config changes in an existing ConfigMap do not roll the pod automatically; restart the Deployment or annotate the pod template yourself.

## Pairing With mindroom-runtime

Point the runtime chart at this homeserver's Service and share the registration token Secret:

```yaml
matrix:
  homeserverUrl: http://matrix-mindroom-tuwunel.mindroom.svc.cluster.local:8008
  serverName: example.com
  registrationToken:
    existingSecret: matrix-registration
    key: MATRIX_REGISTRATION_TOKEN
```

`matrix.homeserverUrl` uses the in-cluster Service DNS name `<release>-mindroom-tuwunel.<namespace>.svc.cluster.local` and the `service.port` of this chart.
`matrix.serverName` must equal `tuwunel.serverName` here.
`matrix.registrationToken` must reference the same Secret as `tuwunel.registrationToken` so MindRoom can register its agent accounts.

## Well-Known Delegation

Tuwunel serves `/.well-known/matrix/client` and `/.well-known/matrix/server` from the rendered `[global.well_known]` section on its own listener.
Requests for those paths must reach the homeserver origin, which clients and federation resolve from the server name apex `https://<serverName>`.
When the apex is not routed to Tuwunel, the operator must serve the delegation documents at the apex:

- `https://example.com/.well-known/matrix/server` returning `{"m.server": "matrix.example.com:443"}`
- `https://example.com/.well-known/matrix/client` returning `{"m.homeserver": {"base_url": "https://matrix.example.com"}}`

The chart defaults `tuwunel.wellKnown.client` to the effective `clientBaseUrl` and `tuwunel.wellKnown.server` to `<serverName>:443`; override them when the public routing differs.

## Notes

- The Deployment is pinned to one replica with a `Recreate` strategy because Tuwunel does not support horizontal scaling against one database.
- The MindRoom fork's compact-edit collapsing for streaming responses is enabled by default; set `tuwunel.compactEdits: false` to disable it.
- The release image runs Tuwunel as root, so the chart sets no restrictive container security context by default; tighten `podSecurityContext` and `securityContext` to match your policy.
- Any Tuwunel option without a dedicated value can be set through `tuwunel.extraConfig` (raw TOML in `[global]`) or `TUWUNEL_*` environment overrides in `env.extra`.
- Set `selectorLabels` when adopting an existing Deployment with an immutable selector, and `storage.existingClaim` when adopting an existing data PVC.
