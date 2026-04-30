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
- `workers.backend: static_runner` adds a sandbox-runner sidecar to the runtime pod.
- `workers.backend: kubernetes` lets the runtime create dedicated worker Deployments, Services, and per-worker auth Secrets on demand.
  The chart can create the worker-manager RBAC and a worker NetworkPolicy for the same namespace.
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
