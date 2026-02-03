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
# Set kubeconfig
export KUBECONFIG=./cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml

# Provision a new instance
./cluster/scripts/mindroom-cli.sh provision 1

# Check status
./cluster/scripts/mindroom-cli.sh status

# View logs
./cluster/scripts/mindroom-cli.sh logs 1

# Upgrade an instance
./cluster/scripts/mindroom-cli.sh upgrade 1
```

### Direct Helm Installation

For manual deployments or debugging:

```bash
helm upgrade --install instance-1 ./cluster/k8s/instance \
  --namespace mindroom-instances \
  --set customer=1 \
  --set baseDomain=mindroom.chat \
  --set anthropic_key="your-key" \
  --set openrouter_key="your-key"
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
# Create values file from example
cp cluster/k8s/platform/values-prod.example.yaml cluster/k8s/platform/values-prod.yaml
# Edit with your configuration

helm upgrade --install platform ./cluster/k8s/platform \
  -f ./cluster/k8s/platform/values-prod.yaml \
  --namespace mindroom-staging
```

### Platform values.yaml

```yaml
environment: staging
domain: mindroom.chat

registry: git.nijho.lt/basnijholt
imageTag: latest

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
```

Platform services use these ingress hosts:

- `app.{domain}` - Platform frontend
- `api.{domain}` - Platform backend API
- `webhooks.{domain}` - Stripe webhooks

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

The `mindroom-cli.sh` script provides common operations:

```bash
./cluster/scripts/mindroom-cli.sh list          # List instances
./cluster/scripts/mindroom-cli.sh pods          # Show pods
./cluster/scripts/mindroom-cli.sh urls          # Show instance URLs
./cluster/scripts/mindroom-cli.sh status        # Overall status
./cluster/scripts/mindroom-cli.sh logs <id>     # View logs
./cluster/scripts/mindroom-cli.sh provision <id>    # Create instance
./cluster/scripts/mindroom-cli.sh deprovision <id>  # Remove instance
./cluster/scripts/mindroom-cli.sh upgrade <id>      # Upgrade instance
```

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

Instances are deployed to the `mindroom-instances` namespace while platform services run in `mindroom-{environment}` (e.g., `mindroom-staging`).

MindRoom automatically creates Matrix user accounts for agents. The bundled Synapse server is configured to allow registration.
