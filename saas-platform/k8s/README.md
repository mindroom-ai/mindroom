# MindRoom K8s Infrastructure

Kubernetes deployments for MindRoom SaaS platform.

## Architecture

```
k8s/
├── platform/     # Platform services (admin, billing, provisioning)
│   └── Simple K8s manifests - deployed once per cluster
│
└── instances/    # Customer instances (MindRoom + Synapse)
    └── Helm charts - templated for multiple customers
```

## Quick Start

### 1. Setup Cluster

```bash
# Enter nix shell for tools
nix-shell

# Create local kind cluster
./kind-setup.sh
```

### 2. Deploy Platform Services

```bash
cd platform/
./setup-secrets.sh  # Create secrets from .env
kubectl apply -f deploy.yml
```

### 3. Deploy Customer Instance

```bash
cd instances/
helm install demo . \
  --set customer=demo \
  --set domain=demo.mindroom.chat \
  --set openai_key=$OPENAI_API_KEY
```

## Why This Structure?

- **Platform services** need to be deployed once → Simple YAML manifests
- **Customer instances** need templating for multiple deployments → Helm charts
- Clear separation of infrastructure vs customer workloads
- Right tool for each job (KISS principle)

## Components

### Platform Services
- **customer-portal**: Customer-facing web app
- **admin-dashboard**: Internal admin interface
- **stripe-handler**: Payment processing
- **instance-provisioner**: Automated provisioning

### Customer Instances
- **mindroom**: AI agent framework (ports 3003, 8765)
- **synapse**: Matrix server for chat

## Development

All tools available via nix-shell:
- `kubectl` - Kubernetes CLI
- `helm` - Package manager
- `k9s` - Terminal UI
- `kind` - Local clusters
