# MindRoom K8s Infrastructure

Simple Helm-based Kubernetes deployments for MindRoom SaaS.

## Structure

```
k8s/
├── platform/      # Platform services (admin, billing, provisioning)
├── instance/      # Customer instances (MindRoom + Synapse)
├── kind-setup.sh  # Local cluster setup
└── shell.nix      # Development tools
```

Both use Helm for consistency and environment management.

## Quick Start

### 1. Setup Local Cluster

```bash
nix-shell
./kind-setup.sh
```

### 2. Deploy Platform (Staging)

```bash
helm install platform-staging platform/ -f platform/values-staging.yaml
kubectl get pods -n mindroom-staging
```

### 3. Deploy Customer Instance

```bash
helm install demo instance/ \
  --set customer=demo \
  --set domain=demo.mindroom.chat \
  --set openai_key=$OPENAI_API_KEY
```

## Environments

### Platform Environments
- **Staging**: `values-staging.yaml` - Test environment with test Stripe/Supabase
- **Production**: `values-prod.yaml` - Live environment with real services

### Deploy to Production
```bash
helm install platform-prod platform/ -f platform/values-prod.yaml
kubectl get pods -n mindroom-production
```

## Why Helm Everywhere?

- **Consistency**: Single tool for all deployments
- **Environment Management**: Easy staging/prod separation
- **Templating**: Reuse configs across environments
- **Standard**: Industry standard for K8s packages

## Components

### Platform Services (`platform/`)
- customer-portal (port 3000)
- admin-dashboard (port 80)
- stripe-handler (port 3005)
- instance-provisioner (port 8002)

Configuration: `values-staging.yaml`, `values-prod.yaml`

### Customer Instances (`instance/`)
- mindroom (ports 3003, 8765)
- synapse (port 8008)

Each customer gets isolated instance with own namespace.
