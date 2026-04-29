---
icon: lucide/ship
---

# Kubernetes Deployment

Deploy MindRoom on Kubernetes for production multi-tenant deployments.

## Architecture

MindRoom uses three Helm charts:

- **Instance Chart** (`cluster/k8s/instance/`) - Individual MindRoom runtime with bundled dashboard/API plus Matrix/Synapse
- **Platform Chart** (`cluster/k8s/platform/`) - SaaS control plane (API, frontend, provisioner)
- **Runtime Chart** (`cluster/k8s/runtime/`) - MindRoom runtime only, for clusters that provide Matrix, storage, secrets, ingress, and platform services externally

## Prerequisites

- Kubernetes cluster (tested with k3s via kube-hetzner)
- kubectl and helm installed
- NGINX Ingress Controller
- cert-manager (for TLS certificates)

## Instance Deployment

### Via Provisioner API (Recommended)

```bash
export KUBECONFIG=./cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml

# Provision, check status, view logs
./cluster/scripts/mindroom-cli.sh provision 1
./cluster/scripts/mindroom-cli.sh status
./cluster/scripts/mindroom-cli.sh logs 1
```

### Direct Helm Installation

For debugging only:

```bash
helm upgrade --install instance-1 ./cluster/k8s/instance \
  --namespace mindroom-instances \
  --create-namespace \
  --set customer=1 \
  --set accountId="your-account-uuid" \
  --set baseDomain=mindroom.chat \
  --set anthropic_key="your-key" \
  --set openrouter_key="your-key" \
  --set supabaseUrl="https://your-project.supabase.co" \
  --set supabaseAnonKey="your-anon-key" \
  --set supabaseServiceKey="your-service-key"
```

## Runtime-Only Deployment

Use the runtime chart when you already operate the surrounding platform and only
want Kubernetes to run the MindRoom runtime. The chart intentionally does not
create Matrix, ingress, a model gateway, or platform services.

```bash
helm upgrade --install mindroom-runtime ./cluster/k8s/runtime \
  --namespace mindroom \
  --create-namespace \
  -f runtime-values.yaml
```

Typical production values point at existing resources:

```yaml
config:
  create: false
  existingConfigMap: mindroom-config
  key: config.yaml

storage:
  create: false
  existingClaim: mindroom-data

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

workers:
  backend: kubernetes
  sandbox:
    proxyToken:
      existingSecret: mindroom-sandbox-proxy
      key: MINDROOM_SANDBOX_PROXY_TOKEN
```

See `cluster/k8s/runtime/README.md` and `cluster/k8s/runtime/values.yaml` for
the full values surface.

## Worker Backends

The instance and runtime charts support two worker backend modes for worker-routed tools such as `shell`, `file`, and `python`.
The dedicated-worker provisioning flow is implemented today.
Both modes store agent data in the same per-agent directory structure.

| Helm value | Behavior | Best for |
|------------|----------|----------|
| `workerBackend: static_runner` | Runs one shared sandbox-runner sidecar inside the main MindRoom pod | Simpler deployments |
| `workerBackend: kubernetes` | Creates dedicated worker Deployments and Services on demand | Stronger runtime isolation per agent (filesystem isolation depends on `worker_scope`) |

### Shared Sidecar Mode

`workerBackend: static_runner` is the default.
The primary runtime talks to a shared sidecar over `localhost`.
This keeps the deployment simple, but all proxied tool calls share the same runner process.
The runner reads and writes the same agent storage directories as the main process.

### Dedicated Worker Mode

`workerBackend: kubernetes` enables the built-in Kubernetes worker backend.
The primary runtime creates worker Deployments and Services on demand and routes tool calls to the matching worker.
Each worker pod runs the sandbox-runner app and accesses the same agent storage directory as every other runtime for that agent.
Worker-local files (caches, virtualenvs, metadata) are kept separate per worker.
When a worker is idle, its Deployment scales to zero, but agent data and worker caches are preserved.

> [!WARNING]
> **Filesystem isolation depends on `worker_scope`.**
> With `shared`, `user_agent`, or unscoped execution, each worker can only see its own agent's storage directory — this is the strongest isolation available.
> With `user`, the worker can see all agents' storage because it shares one runtime across multiple agents for a single user.
> Use `user_agent` for per-agent filesystem isolation.

Typical Helm values look like:

```yaml
workerBackend: kubernetes
workerCleanupIntervalSeconds: 30
storageAccessMode: ReadWriteMany
controlPlaneNodeName: ""
kubernetesWorkerImage: ""
kubernetesWorkerImagePullPolicy: ""
kubernetesWorkerServiceAccountName: ""
kubernetesWorkerNamePrefix: "mindroom-worker"
kubernetesWorkerStorageSubpathPrefix: "workers"
kubernetesWorkerPort: 8766
kubernetesWorkerReadyTimeoutSeconds: 60
kubernetesWorkerIdleTimeoutSeconds: 1800
sandbox_proxy_token: "replace-me"
```

The runtime chart exposes the same concepts under the nested `workers.*` values.

Important behavior and constraints:

- `kubernetesWorkerImage` and `kubernetesWorkerImagePullPolicy` default to the main MindRoom image settings when left empty.
- `workerCleanupIntervalSeconds` controls how often the primary runtime runs idle-worker cleanup.
- `kubernetesWorkerIdleTimeoutSeconds` controls when a worker is considered idle and eligible to scale down.
- `kubernetesWorkerReadyTimeoutSeconds` controls how long the primary runtime waits for a worker Deployment to become ready.
- `kubernetesWorkerPort` is the internal Service and container port used by dedicated workers.
- Dedicated workers need access to the shared instance PVC so they can reach agent storage directories.
- For `shared`, `user_agent`, and unscoped execution, mounts are narrowed to just the target agent's directory plus the worker's scratch space.
- Shared credentials are copied into each dedicated worker as needed instead of exposing the whole shared credentials directory inside agent-isolated pods.
- Dedicated workers start with no shared credentials by default.
- Only services listed in `defaults.worker_grantable_credentials` are available inside a dedicated worker.
- `google_vertex_adc` is intentionally unsupported for dedicated workers because workers do not receive ADC files or `GOOGLE_APPLICATION_CREDENTIALS`; keep Vertex ADC usage in the primary runtime.
- Dedicated worker runtime env stays deny-by-default for provider and arbitrary `.env` values, while basic runtime plumbing such as `PATH`, `VIRTUAL_ENV`, and linker vars is set separately.
- This matches the broader sandbox-proxy contract for `python` and `shell`: proxied execution is intentionally stricter than direct local execution and does not inherit ordinary runtime `.env` or provider env by default.
- For agent-editable per-workspace env (extra PATH entries, npm/pip cache dirs, etc.), use the request-time `.mindroom/worker-env.sh` overlay documented in [Sandbox Proxy Isolation](sandbox-proxy.md#workspace-env-hook-mindroomworker-envsh). The overlay is sourced inside the running worker per request, so it does not change the worker Deployment, the startup manifest, the pod-template hash, or any Helm value, and does not require a worker restart when edited.
- Worker-local caches may still live under `kubernetesWorkerStorageSubpathPrefix/<worker-dir>/`.

### Storage Requirements

Dedicated workers need access to the same PVC as the primary runtime.
Set `storageAccessMode: ReadWriteMany` so multiple workers can access agent storage concurrently.
If your storage class only supports `ReadWriteOnce`, set `controlPlaneNodeName` so the control plane and dedicated workers stay on the same node.
The chart enforces this constraint during template rendering.

### RBAC And Network Policy

When `workerBackend: kubernetes` is enabled, the chart creates:

- A worker-manager ServiceAccount for the primary runtime.
- A Role and RoleBinding that allow managing worker Deployments and Services in the instance namespace.
- NetworkPolicy rules that allow the primary runtime to reach the internal worker port and allow worker traffic within the instance namespace.

### Operations

The authenticated dashboard API exposes `/api/workers` to list active or idle workers and `/api/workers/cleanup` to trigger cleanup manually.
Dedicated workers are internal-only cluster Services and are authenticated with the shared `sandbox_proxy_token`.
See [Sandbox Proxy Isolation](sandbox-proxy.md) for the execution model, credential leases, and non-Kubernetes deployment modes.

## Secrets Management

API keys are mounted as files at `/etc/secrets/` (not environment variables). MindRoom reads paths from `*_API_KEY_FILE` environment variables:

```yaml
env:
  - name: ANTHROPIC_API_KEY_FILE
    value: "/etc/secrets/anthropic_key"
  - name: OPENROUTER_API_KEY_FILE
    value: "/etc/secrets/openrouter_key"
```

## Ingress

Each instance gets three hosts:

- `{customer}.{baseDomain}` - MindRoom dashboard and API
- `{customer}.api.{baseDomain}` - Direct API access
- `{customer}.matrix.{baseDomain}` - Matrix/Synapse server

## Platform Deployment

```bash
# Create values file from example
cp cluster/k8s/platform/values-staging.example.yaml cluster/k8s/platform/values-staging.yaml
# Edit with your configuration

helm upgrade --install platform ./cluster/k8s/platform \
  -f ./cluster/k8s/platform/values-staging.yaml \
  --namespace mindroom-staging
```

The namespace must match `mindroom-{environment}` where `environment` is set in values.

Platform ingress hosts:

- `app.{domain}` - Platform frontend
- `api.{domain}` - Platform backend API
- `webhooks.{domain}/stripe` - Stripe webhooks

## Local Development with Kind

```bash
just cluster-kind-fresh              # Start cluster with everything
just cluster-kind-port-frontend      # http://localhost:3000
just cluster-kind-port-backend       # http://localhost:8000
just cluster-kind-down               # Clean up
```

See `cluster/k8s/kind/README.md` for details.

## CLI Helper

```bash
./cluster/scripts/mindroom-cli.sh list              # List instances
./cluster/scripts/mindroom-cli.sh status            # Overall status
./cluster/scripts/mindroom-cli.sh logs <id>         # View logs
./cluster/scripts/mindroom-cli.sh provision <id>    # Create instance
./cluster/scripts/mindroom-cli.sh deprovision <id>  # Remove instance
./cluster/scripts/mindroom-cli.sh upgrade <id>      # Upgrade instance
```

Reads configuration from `saas-platform/.env`.

## Provisioner API

All endpoints require bearer token (`PROVISIONER_API_KEY`).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/system/provision` | POST | Create or re-provision an instance |
| `/system/instances/{id}/start` | POST | Start a stopped instance |
| `/system/instances/{id}/stop` | POST | Stop a running instance |
| `/system/instances/{id}/restart` | POST | Restart an instance |
| `/system/instances/{id}/uninstall` | DELETE | Remove an instance |
| `/system/sync-instances` | POST | Sync states between DB and K8s |

Example provision request:

```bash
curl -X POST "https://api.mindroom.chat/system/provision" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PROVISIONER_API_KEY" \
  -d '{"account_id": "uuid", "subscription_id": "sub-123", "tier": "starter"}'
```

The provisioner creates the namespace, generates URLs, deploys via Helm, and updates status in Supabase.

## Deployment Scripts

```bash
cd saas-platform
./deploy.sh platform-frontend          # Deploy platform frontend
./deploy.sh platform-backend           # Deploy platform backend
./redeploy-mindroom.sh         # Redeploy all customer MindRoom instances
```

## Multi-Tenant Architecture

Each customer instance gets:

- Separate Kubernetes deployment in `mindroom-instances` namespace
- Isolated PersistentVolumeClaim for data
- Own Matrix/Synapse server (SQLite)
- Independent ConfigMap configuration
- Dedicated ingress routes

Platform services run in `mindroom-{environment}` namespace.
