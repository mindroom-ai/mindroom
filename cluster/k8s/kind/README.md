# MindRoom Local Development with Kind

Run MindRoom locally with Kubernetes using [kind](https://kind.sigs.k8s.io/).

Run all commands below from the repository root.

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
- `just` and Nix (`nix-shell`) for the `just cluster-kind-*` recipes
- kind (`brew install kind` or [install guide](https://kind.sigs.k8s.io/docs/user/quick-start/#installation))
- kubectl (`brew install kubectl`)
- helm (`brew install helm`)

## First Time Setup

```bash
# 1. (Optional) Configure Supabase for platform authentication and data storage
cp cluster/k8s/kind/.env.example cluster/k8s/kind/.env
# Edit cluster/k8s/kind/.env, then export its values into this shell:
set -a
source cluster/k8s/kind/.env
set +a

# 2. Create the cluster, build/load images, and install the platform
just cluster-kind-fresh

# 3. Access the platform
just cluster-kind-port-frontend
```

`just cluster-kind-up` creates the cluster and ingress only; it does not build images or install the platform.

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
./cluster/k8s/kind/setup-local-access.sh
kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80
```

3. Access:
- Platform: http://platform.local:8080
- Instance: http://instance1.local:8080

## Deploy a Test Instance

The instance chart deploys a bundled Synapse homeserver; MindRoom connects to `http://synapse-1:8008` for this example.
The storage and image pull policies below match the kind smoke helper and use the images loaded during fresh setup.

Create a private values file for the shared sandbox token required by the default file and shell tools:

```bash
umask 077
cat > instance-secrets.yaml <<EOF
sandbox_proxy_token: "$(openssl rand -hex 32)"
EOF
```

Keep this file out of version control.
The chart passes this token to both MindRoom and the bundled sandbox runner.

```bash
# Deploy instance using Helm
kubectl create namespace mindroom-instances
helm upgrade --install instance-1 cluster/k8s/instance \
  --namespace mindroom-instances \
  -f instance-secrets.yaml \
  --set customer=1 \
  --set baseDomain=local \
  --set storageClassName=standard \
  --set mindroom_image_pull_policy=IfNotPresent \
  --set synapse_image_pull_policy=IfNotPresent

# Access instance
kubectl port-forward -n mindroom-instances svc/mindroom-1 8765:8765
# Visit: http://localhost:8765
```

## Troubleshooting

### Images Won't Pull

If pods show `ImagePullBackOff`, the images need to be loaded into kind:

```bash
# Check images were loaded
docker exec -it mindroom-control-plane crictl images | grep platform

# If missing, rebuild and reload all platform + MindRoom images:
./cluster/k8s/kind/build_load_images.sh

# Update deployments to use correct tag and pull policy
kubectl set image deployment/platform-backend app=ghcr.io/mindroom-ai/platform-backend:latest -n mindroom-staging
kubectl set image deployment/platform-frontend app=ghcr.io/mindroom-ai/platform-frontend:latest -n mindroom-staging

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
./cluster/k8s/kind/test-access.sh

# Deploy and smoke-test a real MindRoom instance
python -m cluster.k8s.kind.smoke_instance
```

## Clean Up

```bash
# Delete the kind cluster
./cluster/k8s/kind/down.sh
# or
kind delete cluster --name mindroom
```

## Scripts

- `up.sh` - Create kind cluster with ingress
- `build_load_images.sh` - Build and load platform + MindRoom Docker images
- `install_platform.sh` - Deploy platform via Helm
- `smoke_instance.py` - Deploy and verify a MindRoom instance
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

The kind scripts do not automatically load `cluster/k8s/kind/.env` or `saas-platform/.env`.
Export variables in the shell running `just cluster-kind-fresh` or `just cluster-kind-install-platform`, using the `set -a` / `source` / `set +a` sequence in First Time Setup.
The installer forwards these variables to the platform chart:

| Variable | Chart value | Default when unset or empty |
| --- | --- | --- |
| `SUPABASE_URL` | `supabase.url` | Chart default |
| `SUPABASE_ANON_KEY` | `supabase.anonKey` | Chart default |
| `SUPABASE_SERVICE_ROLE_KEY` | `supabase.serviceKey` | Chart default |
| `PROVISIONER_API_KEY` | `provisioner.apiKey` | `kind-provisioner-key` |
| `INSTANCE_BASE_DOMAIN` | `provisioner.instanceBaseDomain` | `local` |
| `INSTANCE_STORAGE_CLASS_NAME` | `provisioner.instanceStorageClassName` | `standard` |
| `INSTANCE_MINDROOM_IMAGE` | `provisioner.instanceMindroomImage` | `ghcr.io/mindroom-ai/mindroom:latest` |
| `INSTANCE_MINDROOM_IMAGE_PULL_POLICY` | `provisioner.instanceMindroomImagePullPolicy` | `IfNotPresent` |
| `INSTANCE_SYNAPSE_IMAGE` | `provisioner.instanceSynapseImage` | `matrixdotorg/synapse:latest` |
| `INSTANCE_SYNAPSE_IMAGE_PULL_POLICY` | `provisioner.instanceSynapseImagePullPolicy` | `IfNotPresent` |

Use `SUPABASE_SERVICE_ROLE_KEY`; the kind installer does not read `SUPABASE_SERVICE_KEY`.
These instance overrides configure future provisioner deployments, not the platform domain or the manual Helm example above.
The image helper separately accepts `SYNAPSE_IMAGE`; set it to the same image as `INSTANCE_SYNAPSE_IMAGE` when overriding both loading and provisioning.
Provider keys, Matrix settings, and platform domain/environment values are not forwarded from this environment file; configure those through the relevant Helm chart values in `cluster/k8s/platform/values.yaml` or `cluster/k8s/instance/values.yaml`.

## Notes

- Images are tagged as `:latest` by the build script
- The Helm chart defaults expect this tag
- Use `imagePullPolicy: IfNotPresent` to avoid registry pulls
- Platform namespace: `mindroom-staging`
- Instance namespace: `mindroom-instances`
