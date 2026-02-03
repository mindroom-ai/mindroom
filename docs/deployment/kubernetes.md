---
icon: lucide/ship
---

# Kubernetes Deployment

Deploy MindRoom on Kubernetes for production multi-tenant deployments.

## Architecture

MindRoom uses two Helm charts:

- **Instance Chart** (`cluster/k8s/instance/`) - Individual MindRoom instance with bundled Matrix/Synapse
- **Platform Chart** (`cluster/k8s/platform/`) - SaaS control plane (API, frontend, provisioner)

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

## Secrets Management

API keys are mounted as files at `/etc/secrets/` (not environment variables). The backend reads paths from `*_API_KEY_FILE` environment variables:

```yaml
env:
  - name: ANTHROPIC_API_KEY_FILE
    value: "/etc/secrets/anthropic_key"
  - name: OPENROUTER_API_KEY_FILE
    value: "/etc/secrets/openrouter_key"
```

## Ingress

Each instance gets three hosts:

- `{customer}.{baseDomain}` - Frontend and API
- `{customer}.api.{baseDomain}` - Direct backend access
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
./redeploy-mindroom-backend.sh         # Redeploy all instance backends
./redeploy-mindroom-frontend.sh        # Redeploy all instance frontends
```

## Multi-Tenant Architecture

Each customer instance gets:

- Separate Kubernetes deployment in `mindroom-instances` namespace
- Isolated PersistentVolumeClaim for data
- Own Matrix/Synapse server (SQLite)
- Independent ConfigMap configuration
- Dedicated ingress routes

Platform services run in `mindroom-{environment}` namespace.
