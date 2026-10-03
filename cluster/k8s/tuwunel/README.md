# MindRoom Tuwunel Chart

This chart deploys the [MindRoom Tuwunel fork](https://github.com/mindroom-ai/mindroom-tuwunel) as a standalone single-replica Matrix homeserver.
Use it together with the `mindroom-runtime` chart when MindRoom should talk to a chart-managed Tuwunel instead of an externally provided homeserver.
The chart renders `tuwunel.toml`, creates the Deployment, Service, and storage PVC, and leaves ingress and TLS to your cluster.
If apex well-known requests do not route to Tuwunel, serve the delegation documents at the apex separately.

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
Tuwunel natively reads `registration_token_file`, `client_secret_file`, and application-service registration files from a directory, so the rendered config references paths backed by Secret volume mounts instead of inline values.
The alternative of an init container substituting secrets into a config template at pod start was rejected: it adds a moving part, hides the effective config from `helm template` and GitOps diffs, and is unnecessary because Tuwunel's native `*_file` options already keep secrets out of the config file.
A `checksum/config` pod annotation rolls the Deployment whenever the rendered config changes.

Secret files are mounted at fixed paths that the rendered config references:

| Value | Mounted file |
|-------|--------------|
| `tuwunel.registrationToken` | `/etc/tuwunel/secrets/registration-token/<key>` |
| `tuwunel.oidc.clientSecret` | `/etc/tuwunel/secrets/oidc/<key>` |
| `tuwunel.appserviceRegistration` | `/etc/tuwunel/appservices/<key>` |

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

## Passwordless MindRoom Accounts

For deployments that disable password login, store a complete Matrix application-service registration YAML file in a Secret and mount it through `tuwunel.appserviceRegistration`.
`appserviceRegistration.key` is also the mounted filename and must end in lowercase `.yaml` or `.yml` for Tuwunel to load it:

```yaml
tuwunel:
  serverName: example.com
  appserviceRegistration:
    existingSecret: matrix-appservice
    key: registration.yaml
  extraConfig: |
    login_with_password = false
```

The registration should use `url: null`, a dedicated `as_token`, and an exclusive anchored user namespace that matches only MindRoom-managed accounts.
When both releases share a namespace, the same Secret can expose the `as_token` under a separate key for the runtime chart's `matrix.appserviceToken` setting.
Otherwise, copy the `as_token` value into a Secret in the runtime release's namespace and reference its local name and key.
Application-service registration bypasses normal registration controls, so `tuwunel.registrationToken` is unnecessary in this mode.

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

The Secret mounts from `tuwunel.registrationToken`, `tuwunel.oidc`, and `tuwunel.appserviceRegistration` still apply, so a custom config can reference the chart's standard secret file paths from the table above.
Config changes in an existing ConfigMap do not roll the pod automatically; restart the Deployment or annotate the pod template yourself.

## Pairing With mindroom-runtime

Point the runtime chart at this homeserver's Service and configure the matching registration token for password-based managed accounts:

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
`matrix.registrationToken` must resolve to the same token value as `tuwunel.registrationToken` so MindRoom can register its agent accounts.
Each Secret reference resolves in its release's namespace.
When both releases share a namespace, they can reference the same Secret and key.
For different namespaces, copy the token value into a Secret in the runtime release's namespace and set `matrix.registrationToken.existingSecret` and `matrix.registrationToken.key` to that local Secret and key.

## Well-Known Delegation

Tuwunel serves `/.well-known/matrix/client` and `/.well-known/matrix/server` from the rendered `[global.well_known]` section on its own listener.
Requests for those paths must reach the homeserver origin, which clients and federation resolve from the server name apex `https://<serverName>`.
When the apex is not routed to Tuwunel, the operator must serve the delegation documents at the apex:

- `https://example.com/.well-known/matrix/server` returning `{"m.server": "matrix.example.com:443"}`
- `https://example.com/.well-known/matrix/client` returning `{"m.homeserver": {"base_url": "https://matrix.example.com"}}`

The chart defaults `tuwunel.wellKnown.client` to the effective `clientBaseUrl` and `tuwunel.wellKnown.server` to `<serverName>:443`; override them when the public routing differs.

## Upload Size

Tuwunel's `max_request_size` caps every request body, including media uploads, and Tuwunel advertises it to clients as `m.upload.size` from `/_matrix/client/v1/media/config`.
Tuwunel defaults it to 24 MiB and refuses to start with a value below 10,000,000 bytes.
The chart has no dedicated value, so set it through `tuwunel.extraConfig` or a `TUWUNEL_MAX_REQUEST_SIZE` entry in `env.extra`:

```yaml
tuwunel:
  extraConfig: |
    max_request_size = 104857600  # 100 MiB
```

Every proxy between clients and Tuwunel must accept a body of at least that size, or uploads that Tuwunel advertises as allowed fail with `413` at the proxy before they reach the homeserver.
Raise each hop together with `max_request_size`:

- ingress-nginx: the `nginx.ingress.kubernetes.io/proxy-body-size` annotation on the Matrix Ingress, whose controller default is 1m
- any nginx that proxies `/_matrix/`, such as a location added through the client chart's `nginx.serverSnippet`: `client_max_body_size` in that server or location, whose nginx default is 1m
- any load balancer, CDN, or tunnel in front of the ingress, which may enforce its own request size limit

MindRoom uploads attachments and long-message sidecars to the same homeserver, and when `matrix.homeserverUrl` points at the in-cluster Service as in [Pairing With mindroom-runtime](#pairing-with-mindroom-runtime), only `max_request_size` limits those uploads.
A long message whose sidecar upload Tuwunel rejects is delivered as a truncated preview.
Agents do not read incoming media larger than 64 MiB whatever the homeserver accepts, as described in [File & Video Attachments](../../../docs/attachments.md).

## Upgrading Tuwunel

A Tuwunel release can migrate the database on its first start, and Tuwunel runs those migrations before it opens its HTTP listener.
Current releases log `Database migration in progress` every 15 seconds while they work, honor a stop request between migration steps, and resume from the last finished step on the next start.
Killing the process mid-migration, for example after a failed probe, an eviction, or an expired termination grace period, can leave the database mid-write and require a restore.
The default `latest` tag with `pullPolicy: Always` lets any pod restart pull a newer release and start an unplanned migration, so pin `image.tag` or `image.digest` and change it only as part of this procedure.

1. Read the release notes of every Tuwunel release between the running and the target version, and note database migrations and supported upgrade paths.
2. Record the running image tag or digest and the values used to deploy it.
3. Rehearse when the database is large: restore a recent snapshot into a separate claim, start the target image against it without client or federation traffic, and measure the migration's duration, memory, and disk use.
4. Scale the paired `mindroom-runtime` Deployment to zero at a quiet time, because its liveness probe restarts it repeatedly once Matrix sync has been stale for a few minutes.
5. Stop Tuwunel and wait until its pod is gone:

   ```bash
   kubectl -n mindroom scale deployment/matrix-mindroom-tuwunel --replicas=0
   kubectl -n mindroom wait --for=delete pod \
     -l app.kubernetes.io/instance=matrix,app.kubernetes.io/component=homeserver --timeout=10m
   ```

6. Take an offline snapshot of the data PVC (`matrix-mindroom-tuwunel-data` here), for example a CSI `VolumeSnapshot`, and wait until it is ready.
   The claim holds both the RocksDB database and the `media/` directory under `storage.mountPath`, so one offline snapshot keeps them consistent.
   Tuwunel's online backups and checkpoints exclude media, and copying the files of a running database does not produce a consistent RocksDB copy; see the fork's [backup guide](https://github.com/mindroom-ai/mindroom-tuwunel/blob/main/docs/backups.md).
   Back up any external media storage providers in the same window.
7. Give the first start enough time.
   The default startup probe allows 60 failures at 5-second intervals, about 5 minutes, before the kubelet restarts the container, which would interrupt a longer migration.
   Raise `probes.startup.failureThreshold` above the rehearsed duration with margin, then deploy the target image:

   ```yaml
   image:
     tag: <target-tag>
   probes:
     startup:
       periodSeconds: 5
       failureThreshold: 720  # 1 hour
   ```

   Kubernetes defaults apply to the Deployment's rollout and shutdown timers unless the Deployment sets them.
   `progressDeadlineSeconds` (default 600 seconds) only marks a slow rollout as failed and does not stop the pod, and `helm upgrade --wait` gives up after its own `--timeout` (default 5 minutes) without stopping the pod either.
   Do not combine the upgrade with Helm's automatic rollback on failure (`--atomic` in Helm 3), which would replace the migrating pod with the old image when that timeout expires.
   `terminationGracePeriodSeconds` (default 30 seconds) bounds how long a stopping pod can finish its current migration step before it is killed, so avoid stopping the pod while a migration runs.
8. Follow the pod log until the migration finishes and `/_matrix/client/versions` answers, then scale the MindRoom runtime back up and confirm that agents sync, reply, and can read existing media.
   The startup probe can return to its default afterwards.

For a migration too long to supervise through the Deployment, keep the Deployment at zero replicas and run the target image once in a separate Pod with `restartPolicy: Never`, no probes, and the same config, Secret mounts, and data claim, passing the arguments `--maintenance --execute "server shutdown"`.
Maintenance mode keeps the listener closed, and the startup command shuts the server down only after the migrations have finished.
Check its log for completed migrations, delete the Pod, and then deploy the target image as above.

Tuwunel normally refuses to open a database whose schema version is newer than it supports, and a newer release can change records or RocksDB files in ways an older release does not expect even when the schema version is unchanged, so switching back to the old image is not a rollback.
To roll back, stop the pod, restore the pre-upgrade snapshot of the claim and any external media, and redeploy the recorded old image and values.
Writes accepted after the snapshot are lost.

## Notes

- The Deployment is pinned to one replica with a `Recreate` strategy because Tuwunel does not support horizontal scaling against one database.
- The image defaults to the fork's `latest` tag with `pullPolicy: Always`; pin `image.tag` or `image.digest` for reproducible production deployments.
- The MindRoom fork's compact-edit collapsing for streaming responses is enabled by default; set `tuwunel.compactEdits: false` to disable it.
- The release image runs Tuwunel as root, so the chart sets no restrictive container security context by default; tighten `podSecurityContext` and `securityContext` to match your policy.
- Any Tuwunel option without a dedicated value can be set through `tuwunel.extraConfig` (raw TOML in `[global]`) or `TUWUNEL_*` environment overrides in `env.extra`.
- Set `selectorLabels` when adopting an existing Deployment with an immutable selector, and `storage.existingClaim` when adopting an existing data PVC.
