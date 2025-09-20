# MindRoom Local Development with Kind

Run MindRoom locally with Kubernetes using [kind](https://kind.sigs.k8s.io/).

## Quick Start (30 seconds)

```bash
# Start everything (creates cluster, builds images, deploys platform)
just cluster-kind-fresh

# Access the platform
just cluster-kind-port-frontend   # Opens http://localhost:3000
# In another terminal:
just cluster-kind-port-backend    # Backend API at http://localhost:8000

# Clean up
just cluster-kind-down
```

## Prerequisites

- Docker running
- kind (`brew install kind` or [install guide](https://kind.sigs.k8s.io/docs/user/quick-start/#installation))
- kubectl (`brew install kubectl`)
- helm (`brew install helm`)

## First Time Setup

```bash
# 1. (Optional) Copy and configure environment variables
cp .env.example .env
# Edit .env with your API keys for full functionality

# 2. Start the local cluster
make up

# 3. Access the platform
make frontend
```

## Access the Platform

### Option 1: Direct Port Forwarding (Easiest)

```bash
# Platform Frontend
kubectl port-forward -n mindroom-staging svc/platform-frontend 3000:3000
# Access at: http://localhost:3000

# Platform Backend API
kubectl port-forward -n mindroom-staging svc/platform-backend 8000:8000
# Access at: http://localhost:8000
```

### Option 2: Via Ingress with Local DNS

1. Add to `/etc/hosts`:
```
127.0.0.1 platform.local
127.0.0.1 instance1.local
```

2. Setup ingress and port-forward:
```bash
./setup-local-access.sh
kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80
```

3. Access:
- Platform: http://platform.local:8080
- Instance: http://instance1.local:8080

## Deploy a Test Instance

```bash
# Deploy instance using Helm
kubectl create namespace mindroom-instances
helm upgrade --install instance-1 ../instance \
  --namespace mindroom-instances \
  --set customer=1 \
  --set baseDomain=local \
  --set matrix.homeserver_url=https://matrix.org \
  --set matrix.admin_user="@test:matrix.org" \
  --set matrix.admin_password=test

# Access instance
kubectl port-forward -n mindroom-instances svc/mindroom-frontend-1 8081:8080
# Visit: http://localhost:8081
```

## Troubleshooting

### Images Won't Pull

If pods show `ImagePullBackOff`, the images need to be loaded into kind:

```bash
# Check images were loaded
docker exec -it mindroom-control-plane crictl images | grep platform

# If missing, reload:
./build_load_images.sh

# Update deployments to use correct tag and pull policy
kubectl set image deployment/platform-backend app=git.nijho.lt/basnijholt/platform-backend:latest -n mindroom-staging
kubectl set image deployment/platform-frontend app=git.nijho.lt/basnijholt/platform-frontend:latest -n mindroom-staging

# Patch to use IfNotPresent
kubectl patch deployment platform-backend -n mindroom-staging \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"app","imagePullPolicy":"IfNotPresent"}]}}}}'
kubectl patch deployment platform-frontend -n mindroom-staging \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"app","imagePullPolicy":"IfNotPresent"}]}}}}'
```

### Check Status

```bash
# View all pods
kubectl get pods -A

# Platform logs
kubectl logs -n mindroom-staging -l app=platform-backend -f
kubectl logs -n mindroom-staging -l app=platform-frontend -f

# Test access
./test-access.sh
```

## Clean Up

```bash
# Delete the kind cluster
./down.sh
# or
kind delete cluster --name mindroom
```

## Scripts

- `up.sh` - Create kind cluster with ingress
- `build_load_images.sh` - Build and load Docker images
- `install_platform.sh` - Deploy platform via Helm
- `start-fresh.sh` - Complete setup from scratch
- `setup-local-access.sh` - Configure ingress for local domains
- `test-access.sh` - Test all access methods
- `down.sh` - Delete kind cluster

## Configuration

The kind cluster configuration is in `kind-config.yaml`:
- 1 control-plane node
- 2 worker nodes
- Ingress ports mapped to host (30080 for HTTP, 30443 for HTTPS)

## Environment Variables

The build script reads from `saas-platform/.env` for:
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `PLATFORM_DOMAIN`

Create this file from `.env.example` if needed.

## Notes

- Images are tagged as `:latest` by the build script
- The Helm chart defaults expect this tag
- Use `imagePullPolicy: IfNotPresent` to avoid registry pulls
- Platform namespace: `mindroom-staging`
- Instance namespace: `mindroom-instances`
