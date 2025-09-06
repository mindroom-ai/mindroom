# K8s Deployment (Experimental)

Simple Kubernetes deployment for local testing. **Not production ready.**

## Files

- `kind-config.yaml` - kind cluster configuration
- `kind-setup.sh` - Creates local K8s cluster with kind
- `setup-secrets.sh` - Creates secrets from .env file
- `deploy.yaml` - Service deployments
- `shell.nix` - Nix shell with kubectl/helm

## Setup

```bash
# Enter nix shell for tools
nix-shell

# Create cluster
./kind-setup.sh

# Create secrets from .env
./setup-secrets.sh

# Deploy services
kubectl apply -f deploy.yaml

# Check status
kubectl get pods -n mindroom
```

## Current Issues

- Instance provisioner crashes (missing env vars)
- No ingress configuration
- No production readiness
- Images must be manually pushed to registry

## Why K8s over Dokku?

- Better for multiple services
- Native scaling
- Standard tooling

But also more complex. This is experimental.
