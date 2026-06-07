# MindRoom Runtime Chart

This chart deploys only the MindRoom runtime and its own runtime support resources.
It is for clusters that already provide surrounding platform pieces such as Matrix, ingress, deployment-specific secrets, model gateways, and optional external backing services.

Use the instance chart in `cluster/k8s/instance` when you want a complete MindRoom instance with its own Matrix homeserver.
Use this chart when MindRoom should run inside an existing platform.

## Minimal Install

```bash
helm upgrade --install mindroom-runtime ./cluster/k8s/runtime \
  --namespace mindroom \
  --create-namespace
```

The default values render a self-contained Deployment, Service, ConfigMap, runtime PVC, and PostgreSQL event-cache StatefulSet.
A real deployment should provide a useful config and Matrix settings.

## Event Cache

The runtime chart defaults to PostgreSQL for MindRoom's Matrix event cache, because Kubernetes deployments need a restart-safe cache backend.
The chart can either create a small PostgreSQL StatefulSet for this cache or wire the runtime to an externally managed database.

Use the chart-managed database for a simple cluster deployment:

```yaml
eventCache:
  backend: postgres
  postgres:
    create: true
    persistence:
      size: 20Gi
```

For GitOps or `helm template` workflows, set `eventCache.postgres.auth.password` or provide existing Secrets so renders do not rotate generated credentials.
When adopting an existing PostgreSQL StatefulSet, keep the service name and password source stable:

```yaml
eventCache:
  backend: postgres
  postgres:
    create: true
    nameOverride: existing-event-cache-postgres
    selectorLabels:
      app: existing-event-cache-postgres
    auth:
      existingSecret: existing-event-cache-secrets
      passwordKey: POSTGRES_PASSWORD
    persistence:
      volumeName: existing-event-cache-postgres-data
  databaseUrl:
    existingSecret: existing-event-cache-secrets
    key: DATABASE_URL
```

Use an external database by providing a Secret with a full PostgreSQL connection URL:

```yaml
eventCache:
  backend: postgres
  postgres:
    create: false
  databaseUrl:
    existingSecret: event-cache-database-url
    key: DATABASE_URL
```

Use SQLite only for lightweight or local-style installs:

```yaml
eventCache:
  backend: sqlite
```

When `config.create` is enabled and `config.data` is empty, the chart renders a minimal config whose `cache` section follows `eventCache`.
When using `config.existingConfigMap` or custom `config.data`, keep that config's cache settings aligned with the chart values.

## Worker Egress Proxy

For dedicated Kubernetes workers, the chart can either deploy the approved egress proxy or point workers at an existing HTTP proxy Service.
In both modes, the worker egress NetworkPolicy keeps worker internet access on the proxy path instead of allowing direct public egress.

Use `approvedEgress` when you want the runtime chart to manage the proxy side too:

```yaml
workers:
  backend: kubernetes
  sandbox:
    proxyToken:
      existingSecret: mindroom-sandbox-proxy
      key: MINDROOM_SANDBOX_PROXY_TOKEN

approvedEgress:
  enabled: true
  image:
    tag: v0.1.0
  allowlist:
    domains:
      - example.com
      - .docs.example.com
```

This renders the proxy Deployment, Service, ServiceAccount, RBAC, allowlist ConfigMap, persistence PVC, and proxy ingress NetworkPolicy.
By default, chart-managed proxy resources use the release-derived `<fullname>-egress-proxy` name; set `approvedEgress.service.name` only when an explicit shared name is required.
The control-plane pod receives `MINDROOM_APPROVED_EGRESS_API_URL`, `MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH`, `MINDROOM_APPROVED_EGRESS_TOKEN`, and `MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS`.
The control-plane pod also mounts the same allowlist file so the approved egress plugin can avoid asking for domains that are already static-allowed.
The proxy pod reads `MINDROOM_APPROVED_EGRESS_TOKEN` from `approvedEgress.token.existingSecret` when set, otherwise it reuses `workers.sandbox.proxyToken`.
Pin `approvedEgress.image.tag` or `approvedEgress.image.digest` before enabling the feature.

Use `egressProxy` when another chart or platform layer already manages the proxy:

```yaml
workers:
  backend: kubernetes

egressProxy:
  enabled: true
  service:
    name: mindroom-egress-proxy
    namespace: mindroom
    port: 3128
  noProxy:
    - localhost
    - 127.0.0.1
    - ::1
    - internal-api.mindroom.svc.cluster.local
  networkPolicy:
    create: true
    proxyPodSelector:
      matchLabels:
        app.kubernetes.io/name: mindroom-egress-proxy
    extraEgress:
      - to:
          - podSelector:
              matchLabels:
                app.kubernetes.io/name: internal-api
        ports:
          - protocol: TCP
            port: 8080
```

When `egressProxy.injectWorkerProxyEnv` is true, worker pods receive `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and lowercase variants through `MINDROOM_KUBERNETES_WORKER_ENV_JSON`.
`workers.kubernetes.extraEnv` is still applied after those defaults, so platform values can override individual variables.
When `approvedEgress.enabled` is true, the chart automatically points worker proxy settings and the worker egress policy at the chart-managed proxy.
When `egressProxy.networkPolicy.create` is true, workers can egress only to DNS, the selected proxy pods, and `egressProxy.networkPolicy.extraEgress`.
For externally managed proxies, NetworkPolicy targets pods rather than Services, so `proxyPodSelector` must match the existing proxy Deployment labels.

## Existing Platform Example

```yaml
image:
  repository: ghcr.io/mindroom-ai/mindroom
  tag: latest

config:
  create: false
  existingConfigMap: mindroom-config
  key: config.yaml

storage:
  create: false
  existingClaim: mindroom-data
  mountPath: /app/agent_data

matrix:
  homeserverUrl: http://matrix.example.svc.cluster.local:8008
  serverName: example.com
  registrationToken:
    existingSecret: mindroom-secrets
    key: MATRIX_REGISTRATION_TOKEN

env:
  envFrom:
    - secretRef:
        name: mindroom-secrets
    - configMapRef:
        name: mindroom-env

eventCache:
  backend: postgres
  postgres:
    create: false
  databaseUrl:
    existingSecret: event-cache-database-url
    key: DATABASE_URL

workers:
  backend: kubernetes
  sandbox:
    proxyTools: shell,file,python,coding
    proxyToken:
      existingSecret: mindroom-sandbox-proxy
      key: MINDROOM_SANDBOX_PROXY_TOKEN
  kubernetes:
    serviceAccount:
      name: mindroom-worker
    port: 8766
    storageSubpathPrefix: workers
    readyTimeoutSeconds: 180
    idleTimeoutSeconds: 3600
```

## Notes

- The chart does not create ingress or a Matrix homeserver.
- The chart can create PostgreSQL for MindRoom's event cache, or use an external PostgreSQL URL from an existing Secret.
- Set `workers.sandbox.proxyToken.existingSecret` or `workers.sandbox.proxyToken.value` when sandbox proxying is enabled.
- Set `workers.sandbox.credentialsEncryptionKey.existingSecret` when encrypted credential storage is enabled so the primary runtime and static runner sidecar receive the same Secret-backed key.
- `workers.backend: static_runner` adds a sandbox-runner sidecar to the runtime pod.
- `workers.backend: kubernetes` lets the runtime create dedicated worker Deployments and Services on demand.
  In the release namespace, the chart stores derived worker tokens and optional credential-encryption keys as entries in one chart-created worker-auth Secret and grants only `get` and `patch` on that Secret.
  When `workers.kubernetes.namespace` points at a separate worker namespace, the chart uses per-worker auth Secrets and grants Secret CRUD only in that namespace.
  The chart can create the worker-manager RBAC and a worker NetworkPolicy.
- If workers run in a different namespace, provide storage, service accounts, and network policy behavior that are valid for that namespace.
  Kubernetes owner references are only set by default for same-namespace workers.
  The sandbox proxy token secret is only needed by the primary runtime; dedicated worker pods receive per-worker derived runner tokens.
- Mount arbitrary platform-specific files, projected secrets, ConfigMaps, init containers, and sidecars through `extraVolumes`, `extraVolumeMounts`, `initContainers`, and `extraContainers`.
- Use `nodeSelector`, `affinity`, `tolerations`, `topologySpreadConstraints`, and `podDisruptionBudget` for cluster-specific scheduling and availability policy.
- Set `selectorLabels` when adopting an existing Deployment with an immutable selector.
- Set `storage.volumeName`, `eventCache.postgres.selectorLabels`, `eventCache.postgres.persistence.volumeName`, or `workers.kubernetes.networkPolicy.name` when adopting existing resources with established names.
- Set `eventCache.postgres.persistence.includeChartLabels: false` when adopting an existing PostgreSQL StatefulSet whose volume claim template has no chart labels.
- Override `probes.*.custom` when a deployment needs custom Kubernetes startup, readiness, or liveness probes.

## Adopting Existing Resources

When replacing hand-written manifests for an existing runtime, keep immutable and externally referenced names stable in values:

```yaml
fullnameOverride: mindroom

selectorLabels:
  app: mindroom

config:
  create: false
  existingConfigMap: mindroom-config
  key: config.yaml

storage:
  create: false
  existingClaim: mindroom-data
  volumeName: data

eventCache:
  backend: postgres
  postgres:
    nameOverride: existing-event-cache-postgres
    selectorLabels:
      app: existing-event-cache-postgres
    auth:
      existingSecret: existing-event-cache-secrets
      passwordKey: POSTGRES_PASSWORD
    persistence:
      volumeName: existing-event-cache-postgres-data
      includeChartLabels: false
  databaseUrl:
    existingSecret: existing-event-cache-secrets
    key: DATABASE_URL

workers:
  backend: kubernetes
  kubernetes:
    networkPolicy:
      name: mindroom-workers

probes:
  liveness:
    custom:
      tcpSocket:
        port: api
      periodSeconds: 30
      timeoutSeconds: 10
      failureThreshold: 6
```

Render and diff the chart before applying it to existing objects:

```bash
helm template mindroom ./cluster/k8s/runtime \
  --namespace mindroom \
  -f runtime-values.yaml
```
