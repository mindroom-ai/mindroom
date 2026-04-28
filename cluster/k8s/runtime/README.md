# MindRoom Runtime Chart

This chart deploys only the MindRoom runtime. It is for clusters that already
provide the surrounding platform pieces such as Matrix, ingress, storage,
secrets, model gateways, and optional backing services.

Use the instance chart in `cluster/k8s/instance` when you want a complete
MindRoom instance with its own Matrix homeserver. Use this chart when MindRoom
should run inside an existing platform.

## Minimal Install

```bash
helm upgrade --install mindroom-runtime ./cluster/k8s/runtime \
  --namespace mindroom \
  --create-namespace
```

The default values render a self-contained Deployment, Service, ConfigMap, and
PVC. A real deployment should provide a useful config and Matrix settings.

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
- Set `workers.sandbox.proxyToken.existingSecret` or
  `workers.sandbox.proxyToken.value` when sandbox proxying is enabled.
- `workers.backend: static_runner` adds a sandbox-runner sidecar to the runtime
  pod.
- `workers.backend: kubernetes` lets the runtime create dedicated worker
  Deployments and Services on demand. The chart can create the worker-manager
  RBAC and a worker NetworkPolicy for the same namespace.
- If workers run in a different namespace, provide storage, service accounts,
  and network policy behavior that are valid for that namespace. Kubernetes
  owner references are only set by default for same-namespace workers. When
  using an existing sandbox proxy token secret, create it in both the runtime
  namespace and the worker namespace. When using
  `workers.sandbox.proxyToken.value`, the chart creates both copies.
- Mount arbitrary platform-specific files, projected secrets, ConfigMaps, init
  containers, and sidecars through `extraVolumes`, `extraVolumeMounts`,
  `initContainers`, and `extraContainers`.
- Use `nodeSelector`, `affinity`, `tolerations`, `topologySpreadConstraints`,
  and `podDisruptionBudget` for cluster-specific scheduling and availability
  policy.
- Override `probes.*.spec` when a deployment needs custom Kubernetes
  startup, readiness, or liveness probes.
