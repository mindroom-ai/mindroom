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
- Storage class (e.g., `hcloud-volumes` for Hetzner)

## Instance Deployment

### Via Provisioner API (Recommended)

Use the CLI helper for managed deployments:

```bash
# Navigate to cluster directory and set kubeconfig
cd cluster
export KUBECONFIG=./terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml

# Provision a new instance (instance ID used as subdomain)
./scripts/mindroom-cli.sh provision 1

# Check status
./scripts/mindroom-cli.sh status

# View logs
./scripts/mindroom-cli.sh logs 1

# Upgrade an instance
./scripts/mindroom-cli.sh upgrade 1
```

Alternatively, from the repository root:

```bash
export KUBECONFIG=./cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml
./cluster/scripts/mindroom-cli.sh provision 1
```

### Direct Helm Installation

For manual deployments or debugging:

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

### Instance values.yaml

```yaml
customer: demo
baseDomain: mindroom.chat
storage: 10Gi
storagePath: "/mindroom_data"

# Docker images
mindroom_image: git.nijho.lt/basnijholt/mindroom-frontend:latest
mindroom_backend_image: git.nijho.lt/basnijholt/mindroom-backend:latest
synapse_image: matrixdotorg/synapse:latest

# API keys (passed via --set or values file)
openai_key: ""
anthropic_key: ""
google_key: ""
openrouter_key: ""
deepseek_key: ""

# Supabase (shared auth)
supabaseUrl: ""
supabaseAnonKey: ""
supabaseServiceKey: ""

# Matrix admin password
matrix_admin_password: ""
```

Note: `accountId` is required when deploying but should be passed via `--set accountId=<uuid>` rather than stored in the values file.

## Secrets Management

API keys are mounted as files (not environment variables) for security:

```yaml
env:
  - name: ANTHROPIC_API_KEY_FILE
    value: "/etc/secrets/anthropic_key"
  - name: OPENAI_API_KEY_FILE
    value: "/etc/secrets/openai_key"
```

The instance chart automatically creates a Secret from values:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: mindroom-api-keys-{{ .Values.customer }}
  namespace: mindroom-instances
stringData:
  openai_key: "..."
  anthropic_key: "..."
  openrouter_key: "..."
  google_key: "..."
  deepseek_key: "..."
  supabase_service_key: "..."
```

## ConfigMap

Each instance has a ConfigMap with the MindRoom configuration. The default configuration is in `cluster/k8s/instance/default-config.yaml` and is automatically included:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: mindroom-config-{{ .Values.customer }}
data:
  config.yaml: |
    agents:
      general:
        display_name: GeneralAgent
        model: sonnet
        # ... agent configuration
```

## Ingress

Each instance gets three ingress hosts:

- `{customer}.{baseDomain}` - Frontend and API
- `{customer}.api.{baseDomain}` - Direct backend access
- `{customer}.matrix.{baseDomain}` - Matrix/Synapse server

## Platform Deployment

Deploy the SaaS platform control plane:

```bash
# Create values file from example (choose staging or prod)
cp cluster/k8s/platform/values-staging.example.yaml cluster/k8s/platform/values-staging.yaml
# or for production:
cp cluster/k8s/platform/values-prod.example.yaml cluster/k8s/platform/values-prod.yaml

# Edit with your configuration, then deploy

# For staging
helm upgrade --install platform ./cluster/k8s/platform \
  -f ./cluster/k8s/platform/values-staging.yaml \
  --namespace mindroom-staging \
  --create-namespace

# For production
helm upgrade --install platform ./cluster/k8s/platform \
  -f ./cluster/k8s/platform/values-prod.yaml \
  --namespace mindroom-production \
  --create-namespace
```

The namespace should match the `environment` value in your values file (`mindroom-{environment}`).

### Platform values.yaml

```yaml
environment: staging
domain: mindroom.chat

registry: git.nijho.lt/basnijholt
imageTag: latest
replicas: 1

supabase:
  url: "https://your-project.supabase.co"
  anonKey: "your-anon-key"
  serviceKey: "your-service-key"

stripe:
  publishableKey: "pk_test_..."
  secretKey: "sk_test_..."
  webhookSecret: "whsec_..."

gitea:
  user: "username"
  token: "your-token"

provisioner:
  apiKey: "your-provisioner-key"

# Optional AI API keys for platform services
apiKeys:
  openai: ""
  anthropic: ""
  openrouter: ""

# Cleanup scheduler (enable in production)
cleanupScheduler:
  enabled: "false"

# Ingress options
ingress:
  enableConfigurationSnippet: false

# Monitoring (optional)
monitoring:
  enabled: true
  releaseLabel: monitoring
  scrapeInterval: 30s
```

Platform services use these ingress hosts:

- `app.{domain}` - Platform frontend
- `api.{domain}` - Platform backend API
- `webhooks.{domain}/stripe` - Stripe webhooks endpoint

## Local Development with Kind

For local Kubernetes development:

```bash
# Start a local cluster with everything
just cluster-kind-fresh

# Access services
just cluster-kind-port-frontend   # http://localhost:3000
just cluster-kind-port-backend    # http://localhost:8000

# Clean up
just cluster-kind-down
```

See `cluster/k8s/kind/README.md` for detailed instructions.

## CLI Helper

The `mindroom-cli.sh` script provides common operations (run from `cluster/` directory):

```bash
./scripts/mindroom-cli.sh list          # List instances (alias: ls)
./scripts/mindroom-cli.sh pods          # Show pods
./scripts/mindroom-cli.sh urls          # Show instance URLs
./scripts/mindroom-cli.sh status        # Overall status
./scripts/mindroom-cli.sh logs <id>     # View logs
./scripts/mindroom-cli.sh provision <id>    # Create instance via provisioner API
./scripts/mindroom-cli.sh deprovision <id>  # Remove instance
./scripts/mindroom-cli.sh upgrade <id>      # Upgrade instance (optional: values file)
```

The CLI reads configuration from `saas-platform/.env` for the provisioner API key and platform domain.

## Provisioner API

The platform backend exposes a provisioner API for managing instances programmatically. All endpoints require a bearer token (`PROVISIONER_API_KEY`).

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/system/provision` | POST | Create or re-provision an instance |
| `/system/instances/{id}/start` | POST | Start a stopped instance |
| `/system/instances/{id}/stop` | POST | Stop a running instance |
| `/system/instances/{id}/restart` | POST | Restart an instance |
| `/system/instances/{id}/uninstall` | DELETE | Completely remove an instance |
| `/system/sync-instances` | POST | Sync instance states between DB and K8s |

### Provision Request

```bash
curl -X POST "https://api.mindroom.chat/system/provision" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PROVISIONER_API_KEY" \
  -d '{
    "account_id": "uuid-from-supabase",
    "subscription_id": "sub-123",
    "tier": "starter",
    "instance_id": "1"
  }'
```

Note: `instance_id` is optional and used for re-provisioning an existing instance.

The provisioner automatically:

- Creates the `mindroom-instances` namespace if needed
- Generates instance URLs (`{id}.{domain}`, `{id}.api.{domain}`, `{id}.matrix.{domain}`)
- Deploys via Helm with all required secrets
- Updates instance status in Supabase

## Deployment Scripts

Quick deployment of platform components:

```bash
cd saas-platform

# Deploy platform frontend
./deploy.sh platform-frontend

# Deploy platform backend
./deploy.sh platform-backend

# Redeploy all MindRoom backends
./redeploy-mindroom-backend.sh
```

## Multi-Tenant Architecture

Each customer instance gets:

- Separate Kubernetes deployment
- Isolated PersistentVolumeClaim for data
- Own Matrix/Synapse server (SQLite)
- Independent configuration via ConfigMap
- Dedicated ingress routes

Instances are deployed to the `mindroom-instances` namespace while platform services run in `mindroom-{environment}` (e.g., `mindroom-staging` for staging, `mindroom-production` for production).

MindRoom automatically creates Matrix user accounts for agents. The bundled Synapse server is configured to allow registration.
