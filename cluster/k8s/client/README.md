# MindRoom Client Chart

This chart deploys only the MindRoom web client (`ghcr.io/mindroom-ai/mindroom-cinny`) behind unprivileged nginx.
It is for clusters that already provide a Matrix homeserver, ingress, and TLS.

Use the instance chart in `cluster/k8s/instance` when you want a complete MindRoom instance with its own Matrix homeserver.
Use the runtime chart in `cluster/k8s/runtime` for the MindRoom agent runtime itself.
Use this chart when you only need the web client in front of an existing homeserver.

## Minimal Install

```bash
helm upgrade --install mindroom-client ./cluster/k8s/client \
  --namespace mindroom \
  --create-namespace \
  --set matrix.homeserverUrl=https://matrix.example.com
```

The default values render a Deployment, Service, a client `config.json` ConfigMap, and an nginx ConfigMap.
The Deployment carries checksum annotations for both chart-managed ConfigMaps, so config changes roll pods on upgrade.
For production, pin `image.tag` or `image.digest` instead of the default `latest`.

## Homeserver Configuration

The chart renders a minimal `config.json` from the `matrix` values:

```yaml
matrix:
  homeserverUrl: https://matrix.example.com
  defaultServerName: ""
  allowCustomHomeservers: false
```

When `defaultServerName` is set, the login form shows that server name instead of the URL, and the name must publish `/.well-known/matrix/client` pointing at `homeserverUrl`.
Set `config.data` to a full JSON document when you need client options beyond the homeserver entry, or point `config.existingConfigMap` at a ConfigMap you manage yourself.

## Base Path

Set `basePath` to serve the client under a URL prefix instead of the origin root:

```yaml
basePath: /mindroom
```

The chart-managed nginx config serves the app shell, `config.json`, and the service worker under the base path, redirects `/` and the bare base path to `basePath/`, and resolves hashed build assets referenced from any route depth.
The client always loads `/runtime-config.js` from the origin root, so route the full origin host to this Service even when `basePath` is not `/`.
The chart serves `/runtime-config.js` directly from nginx because the image entrypoint would otherwise write it into the app directory, which the unprivileged read-only container forbids.

## Service Worker Hygiene

The client registers its service worker at `basePath/sw.js`, scoped to `basePath/`.
Set `serviceWorker.enabled: false` to keep the client from registering it at all.

A Matrix client that previously controlled the origin root is a known footgun: its root-scoped service worker keeps serving the old app for every path on the origin, including the new base path.
Browsers replace a service worker by re-fetching its script URL, so the deterministic fix is to serve a worker at the old root `/sw.js` whose only job is to purge caches, release its clients into `basePath/`, and unregister itself.
Enable that cleanup worker when migrating such an origin:

```yaml
basePath: /mindroom

rootServiceWorkerCleanup:
  enabled: true
```

The cleanup worker requires a `basePath` other than `/`, because at the origin root `/sw.js` is the client's own service worker.
Once stale clients have cycled through the cleanup worker, the flag can be disabled again.

## Custom nginx Config

Point `nginx.existingConfigMap` at your own ConfigMap to take full control of the server config:

```yaml
nginx:
  existingConfigMap: my-client-nginx
  key: default.conf
```

The ConfigMap must provide the server config under `nginx.key`, listening on `nginx.port`.
When `rootServiceWorkerCleanup.enabled` is true, the same ConfigMap must also provide the cleanup worker script under `nginx.cleanupWorkerKey`.

## Notes

- The chart does not create ingress, TLS, or a Matrix homeserver.
- The container runs as the unprivileged nginx user with a read-only root filesystem, so `nginx.port` must stay at 1024 or higher unless you grant extra privileges through `securityContext` or lower `net.ipv4.ip_unprivileged_port_start`.
- Set `nginx.ipv6: false` on nodes without an IPv6 stack, where binding `[::]` makes nginx fail at startup.
- Use `nodeSelector`, `affinity`, `tolerations`, and `topologySpreadConstraints` for cluster-specific scheduling policy.
- Set `selectorLabels` when adopting an existing Deployment with an immutable selector.
- Override `probes.*.custom` when a deployment needs custom Kubernetes readiness or liveness probes.

## Render Locally

```bash
helm template mindroom-client ./cluster/k8s/client \
  --namespace mindroom \
  --set matrix.homeserverUrl=https://matrix.example.com \
  --set basePath=/mindroom \
  --set rootServiceWorkerCleanup.enabled=true
```
