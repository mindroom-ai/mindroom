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

## Config Sources

By default the chart keeps the existing ConfigMap behavior.
Omit `config.source` or set `config.source: configMap` to mount a chart-created or existing ConfigMap at `config.mountPath`.
Set `config.source: file` when another init container or content bundle places `agent-config.yaml` on the runtime filesystem before MindRoom starts.
In file mode, `config.path` must be an absolute container path.
In file mode, the chart does not render or mount the runtime config ConfigMap.
Dedicated Kubernetes workers receive the same config file path and do not receive worker ConfigMap settings.
Dedicated Kubernetes workers also mount the storage subtree containing the config file read-only so content-bundle files under that subtree are visible without broad worker state access.

Use a content bundle as the source of truth for the runtime config:

```yaml
contentBundles:
  - name: team-config
    image: registry.example.com/team/mindroom-config@sha256:1111111111111111111111111111111111111111111111111111111111111111
    targetPath: /app/agent_data/content-bundles/team-config

config:
  source: file
  path: /app/agent_data/content-bundles/team-config/content/environments/prod/agent-config.yaml
```

## Runtime State Storage

MindRoom stores Matrix encryption keys and sync-token checkpoints under `MINDROOM_STORAGE_PATH`.
Hosted installs can keep those restart-critical directories on a dedicated PVC while normal workspace data stays on `storage`.
The chart mounts the same state PVC at `stateStorage.mountPath` and overlays the configured subpaths where MindRoom already reads and writes those directories.
The optional init container creates the state directories and applies the configured ownership before the runtime starts.
`initPermissions.runAsUser` and `initPermissions.fsGroup` are the ownership targets used by the init container.
By default, the init container runs as UID 0 because it performs `chown` and `chmod`, while the main runtime container keeps its own security context.

Use an existing PVC when another platform layer owns durable storage:

```yaml
storage:
  create: false
  existingClaim: mindroom-workspace
  mountPath: /app/agent_data

stateStorage:
  enabled: true
  existingClaim: mindroom-state
  mountPath: /app/mindroom_state
  encryptionKeys:
    enabled: true
    mountPath: /app/agent_data/encryption_keys
    subPath: encryption_keys
  syncTokens:
    enabled: true
    mountPath: /app/agent_data/sync_tokens
    subPath: sync_tokens
  initPermissions:
    enabled: true
    runAsUser: 1000
    fsGroup: 1000
```

Let the chart create the state PVC for simpler hosted installs:

```yaml
stateStorage:
  enabled: true
  create: true
  size: 20Gi
```

## Content Bundles

Hosted deployments can copy immutable private content into MindRoom storage before the runtime starts.
Use `contentBundles` for plugins, skills, agent templates, workspace templates, policy files, and small seed scripts that are packaged into an OCI image.
The chart does not clone repositories or download release assets at pod startup.
Private registry access should use normal Kubernetes image pull credentials through `imagePullSecrets`.

Each bundle image must be pinned by digest and must contain a POSIX shell, `cp`, and `mkdir`, because the chart runs the bundle image as a copy init container.
Because `overwrite` defaults to true, bundle images also need `rm` unless every bundle sets `overwrite: false`.
Package content under `/bundle` by default:

```dockerfile
FROM busybox:1.36
COPY . /bundle
```

Configure one or more bundles:

```yaml
imagePullSecrets:
  - name: private-registry-pull

contentBundles:
  - name: team-config
    image: registry.example.com/team/mindroom-config@sha256:1111111111111111111111111111111111111111111111111111111111111111
    sourcePath: /bundle
    targetPath: /app/agent_data/content-bundles/team-config
    seed:
      enabled: true
      command:
        - /app/agent_data/content-bundles/team-config/scripts/seed-content.sh

  - name: policy-pack
    image: registry.example.com/team/policy-pack@sha256:2222222222222222222222222222222222222222222222222222222222222222
```

If `targetPath` is omitted, the chart copies to `/app/agent_data/content-bundles/<name>`.
The init container removes the target path before copying unless `overwrite: false` is set.
`seed.command` runs after the copy and should point at a short script or executable supplied by the bundle instead of embedding deployment-specific shell in Helm values.
The script runs in the same bundle image, with MindRoom storage mounted at `storage.mountPath`.
Raw `initContainers`, `extraVolumes`, and `extraVolumeMounts` still work for deployments that need lower-level Kubernetes wiring.

Reference copied plugins from `config.yaml` with absolute paths:

```yaml
plugins:
  - /app/agent_data/content-bundles/team-config/plugins/review-tools
  - /app/agent_data/content-bundles/policy-pack/plugins/policy-tools
```

## Provider API Keys from Kubernetes Secrets

The MindRoom runtime natively honors model-provider API key env vars such as `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`.
At startup it syncs each value into its credential service, which stores it as `<storage.mountPath>/credentials/<provider>_credentials.json`.
Use `providerCredentials` to bind those env vars to existing Kubernetes Secrets without writing any credential files yourself:

```yaml
providerCredentials:
  - provider: openai
    existingSecret: my-llm-secret
    key: api-key
  - provider: anthropic
    existingSecret: my-llm-secret
    key: anthropic-api-key
```

Supported provider names are `anthropic`, `azure`, `openai`, `google`, `openrouter`, `deepseek`, `cerebras`, `groq`, and `ollama` (which expects a host URL instead of an API key).
Each entry renders only a `secretKeyRef` env var on the runtime container, so no secret material passes through ConfigMaps or appears in rendered manifests.
The chart rejects entries whose env var is also set through `env.extra`, but variables arriving through `env.envFrom` cannot be collision-checked because Secret and ConfigMap keys are not visible at template time.
If an `env.envFrom` source contains the same variable name, the container-level `env` entry rendered by `providerCredentials` takes precedence in Kubernetes.
Because the runtime writes the credential files itself, this path also works with encrypted credential storage (`workers.sandbox.credentialsEncryptionKey`), where externally written plaintext JSON files would be refused.

Sync semantics follow the runtime's `_source` tracking:

- Env-sourced credentials are refreshed on every startup, so rotating the Secret takes effect after a Deployment restart (env values from `secretKeyRef` are fixed at pod start).
- Credentials saved through the dashboard (`_source: ui`) are never overwritten by env sync.
- A pre-existing credential file without `_source: env`, for example one written manually or by a custom init container, is also left untouched; delete that file once when migrating to `providerCredentials`.

Non-secret companion settings such as `OPENAI_BASE_URL` or `AZURE_OPENAI_ENDPOINT` are plain config and belong in `env.extra`.
For credential services beyond the supported providers, the runtime accepts declared credential seeds through the `MINDROOM_CREDENTIAL_SEEDS_JSON` env var, whose JSON contains only env-var references while the secret values still arrive via `secretKeyRef` entries in `env.extra`:

```yaml
env:
  extra:
    - name: MY_GATEWAY_API_KEY
      valueFrom:
        secretKeyRef:
          name: my-gateway-secret
          key: api-key
    - name: MINDROOM_CREDENTIAL_SEEDS_JSON
      value: '[{"service": "my_gateway", "credentials": {"api_key": {"env": "MY_GATEWAY_API_KEY"}}}]'
```

## Control-Plane NetworkPolicy

The chart can create an optional NetworkPolicy for the control-plane pod, so operators can restrict runtime API ingress to an edge proxy and known clients.
The policy targets the runtime pods through the chart selector labels and allows the API port only from the configured peers.
NetworkPolicy targets pods rather than Services, so each `apiIngressFrom` entry must match the actual client pods.
A bare `podSelector` entry only matches pods in the release namespace, so combine `namespaceSelector` and `podSelector` in one entry for cross-namespace clients such as an ingress controller.

```yaml
networkPolicy:
  create: true
  apiIngressFrom:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: my-ingress
      podSelector:
        matchLabels:
          app.kubernetes.io/name: my-ingress
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: my-api-client
```

The API port follows `runtime.apiPort`, so the policy stays aligned with the Deployment without a separate port value.
When `apiIngressFrom` is empty, the policy allows the API port from all sources while still denying other ingress to the pod.
Use `networkPolicy.extraIngress` and `networkPolicy.extraEgress` for raw Kubernetes rules beyond the API rule.
Setting any `extraEgress` entry adds `Egress` to `policyTypes`, so those rules must then cover every egress flow the runtime needs, such as DNS, the Matrix homeserver, model APIs, the event cache, workers, and the Kubernetes API server when `workers.backend` is `kubernetes`.

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
The control-plane pod also mounts the same allowlist file so the built-in `approved_egress` tool can avoid asking for domains that are already static-allowed.
The control-plane pod also receives `MINDROOM_APPROVED_EGRESS_ENABLED=true`, so MindRoom adds `approved_egress` and the required Matrix approval rule at runtime even when you use `config.data` or `config.existingConfigMap`.
Set `approvedEgress.manageRuntimeConfig: false` to skip that flag when the authored config already assigns `approved_egress` deliberately, for example to specific agents instead of `defaults.tools`; the proxy and the other approved egress env vars are still wired.
The proxy pod reads `MINDROOM_APPROVED_EGRESS_TOKEN` from `approvedEgress.token.existingSecret` when set, otherwise it reuses `workers.sandbox.proxyToken`.
Pin `approvedEgress.image.tag` or `approvedEgress.image.digest` before enabling the feature.

When Agent Vault is also enabled, keep approved egress as the first network hop.
The proxy resolves dynamic `request_network_access` grants from the worker pod's source IP; if worker traffic goes to Agent Vault first, every request reaches Squid from the vault pod IP and worker identity cannot be resolved.
Enable `approvedEgress.parentProxy` to route only token-bearing Agent Vault tool traffic through the vault parent after the allowlist/grant check:

```yaml
workers:
  backend: kubernetes
  kubernetes:
    agentVault:
      enabled: true
      cliImage: infisical/agent-vault:<pinned>
      ownerEmail: owner@example.test
      server:
        enabled: true
      # Leave proxyUrl at the default; parentProxy makes workers use approved
      # egress first and Squid forwards Proxy-Authorization traffic to the vault.

approvedEgress:
  enabled: true
  image:
    tag: v0.1.0
  parentProxy:
    enabled: true
    host: agent-vault
    port: 14322
```

With this setup, workers mint Agent Vault tokens through the API port (`14321`), workers proxy tool egress to the approved egress Service, Squid enforces allowlists and dynamic grants using the real worker IP, and Squid forwards requests carrying `Proxy-Authorization` to Agent Vault (`login=PASSTHRU`) for credential injection.
Tokenless traffic egresses directly from Squid after the normal policy check.
The chart rejects the unsafe default combination of chart-managed approved egress plus Agent Vault without `approvedEgress.parentProxy`, because that would be vault-first and break dynamic grants.

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

stateStorage:
  enabled: true
  existingClaim: mindroom-state

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
- Set `networkPolicy.create: true` to restrict control-plane API ingress to known client pods.
- The chart can create PostgreSQL for MindRoom's event cache, or use an external PostgreSQL URL from an existing Secret.
- Set `workers.sandbox.proxyToken.existingSecret` or `workers.sandbox.proxyToken.value` when sandbox proxying is enabled.
- Use `providerCredentials` to feed model-provider API keys from existing Kubernetes Secrets into the runtime's credential service.
- Set `workers.sandbox.credentialsEncryptionKey.existingSecret` when encrypted credential storage is enabled so the primary runtime and static runner sidecar receive the same Secret-backed key.
- `workers.backend: static_runner` adds a sandbox-runner sidecar to the runtime pod.
- `workers.backend: kubernetes` lets the runtime create dedicated worker Deployments and Services on demand.
  In the release namespace, the chart stores derived worker tokens and optional credential-encryption keys as entries in one chart-created worker-auth Secret and grants only `get` and `patch` on that Secret.
  When `workers.kubernetes.namespace` points at a separate worker namespace, the chart uses per-worker auth Secrets and grants Secret CRUD only in that namespace.
  The chart can create the worker-manager RBAC and a worker NetworkPolicy.
- With `workers.kubernetes.reconcilePodTemplates` (default `true`), each cleanup pass recreates scaled-down worker Deployments whose pod template (image, env, resources) drifted from the configured spec, so existing workers do not need manual recycling after upgrades.
  Running workers are recreated on their next provisioning after they scale down.
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
