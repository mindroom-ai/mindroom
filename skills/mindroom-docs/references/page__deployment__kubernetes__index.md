# Kubernetes Deployment

Deploy MindRoom on Kubernetes for production multi-tenant deployments.

## Architecture

MindRoom uses two Helm charts:

- **Instance Chart** (`cluster/k8s/instance/`) - Individual MindRoom runtime with bundled dashboard/API plus Matrix/Synapse
- **Platform Chart** (`cluster/k8s/platform/`) - SaaS control plane (API, frontend, provisioner)

## Prerequisites

- Kubernetes cluster (tested with k3s via kube-hetzner)
- kubectl and helm installed
- NGINX Ingress Controller
- cert-manager (for TLS certificates)

## Instance Deployment

### Via Provisioner API (Recommended)

```
export KUBECONFIG=./cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml

# Provision, check status, view logs
./cluster/scripts/mindroom-cli.sh provision 1
./cluster/scripts/mindroom-cli.sh status
./cluster/scripts/mindroom-cli.sh logs 1
```

### Direct Helm Installation

For debugging only:

```
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

## Worker Backends

The instance chart supports two worker backend modes for worker-routed tools such as `shell`, `file`, and `python`.

| Helm value                     | Behavior                                                                             | Best for                                                      |
| ------------------------------ | ------------------------------------------------------------------------------------ | ------------------------------------------------------------- |
| `workerBackend: static_runner` | Runs one shared sandbox-runner sidecar inside the main MindRoom pod                  | Simpler deployments and the current shared-worker model       |
| `workerBackend: kubernetes`    | Creates dedicated worker Deployments and Services on demand from the primary runtime | Stronger isolation and persistent worker state per worker key |

### Shared Sidecar Mode

`workerBackend: static_runner` is the default. The primary runtime talks to a shared sidecar over `localhost`. This keeps the deployment simple, but all worker-routed tool calls share the same runner process.

### Dedicated Worker Mode

`workerBackend: kubernetes` enables the built-in Kubernetes worker backend. The primary runtime creates worker Deployments and Services on demand and routes tool calls to the resolved worker handle. Each worker pod runs the sandbox-runner app and mounts worker-owned state from the shared PVC under a worker-specific subpath. Idle cleanup scales worker Deployments to zero while preserving that state.

Typical Helm values look like:

```
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

Important behavior and constraints:

- `kubernetesWorkerImage` and `kubernetesWorkerImagePullPolicy` default to the main MindRoom image settings when left empty.
- `workerCleanupIntervalSeconds` controls how often the primary runtime runs idle-worker cleanup.
- `kubernetesWorkerIdleTimeoutSeconds` controls when a worker is considered idle and eligible to scale down.
- `kubernetesWorkerReadyTimeoutSeconds` controls how long the primary runtime waits for a worker Deployment to become ready.
- `kubernetesWorkerPort` is the internal Service and container port used by dedicated workers.
- The worker state lives on the shared instance PVC under `kubernetesWorkerStorageSubpathPrefix/<worker-dir>/`.

### Storage Requirements

Dedicated workers need access to the same PVC as the primary runtime. For multi-node operation, set `storageAccessMode: ReadWriteMany`. If your storage class only supports `ReadWriteOnce`, set `controlPlaneNodeName` so the control plane and dedicated workers stay on the same node. The chart enforces this constraint during template rendering.

### RBAC And Network Policy

When `workerBackend: kubernetes` is enabled, the chart creates:

- A worker-manager ServiceAccount for the primary runtime.
- A Role and RoleBinding that allow managing worker Deployments and Services in the instance namespace.
- NetworkPolicy rules that allow the primary runtime to reach the internal worker port and allow worker traffic within the instance namespace.

### Operations

The authenticated dashboard API exposes `/api/workers` to list active or idle workers and `/api/workers/cleanup` to trigger cleanup manually. Dedicated workers are internal-only cluster Services and are authenticated with the shared `sandbox_proxy_token`. See [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md) for the execution model, credential leases, and non-Kubernetes deployment modes.

## Secrets Management

API keys are mounted as files at `/etc/secrets/` (not environment variables). MindRoom reads paths from `*_API_KEY_FILE` environment variables:

```
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

```
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

```
just cluster-kind-fresh              # Start cluster with everything
just cluster-kind-port-frontend      # http://localhost:3000
just cluster-kind-port-backend       # http://localhost:8000
just cluster-kind-down               # Clean up
```

See `cluster/k8s/kind/README.md` for details.

## CLI Helper

```
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

| Endpoint                           | Method | Description                        |
| ---------------------------------- | ------ | ---------------------------------- |
| `/system/provision`                | POST   | Create or re-provision an instance |
| `/system/instances/{id}/start`     | POST   | Start a stopped instance           |
| `/system/instances/{id}/stop`      | POST   | Stop a running instance            |
| `/system/instances/{id}/restart`   | POST   | Restart an instance                |
| `/system/instances/{id}/uninstall` | DELETE | Remove an instance                 |
| `/system/sync-instances`           | POST   | Sync states between DB and K8s     |

Example provision request:

```
curl -X POST "https://api.mindroom.chat/system/provision" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PROVISIONER_API_KEY" \
  -d '{"account_id": "uuid", "subscription_id": "sub-123", "tier": "starter"}'
```

The provisioner creates the namespace, generates URLs, deploys via Helm, and updates status in Supabase.

## Deployment Scripts

```
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
