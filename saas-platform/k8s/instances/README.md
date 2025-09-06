# Customer Instance Deployments

Helm chart for deploying isolated MindRoom customer instances with Matrix.

## Components per Instance

- **MindRoom**: AI agent framework (frontend + backend)
- **Synapse**: Matrix server with SQLite (no PostgreSQL/Redis needed)
- **Storage**: Persistent volumes for data isolation

## Quick Install

```bash
# From k8s/instances/ directory
helm install demo . \
  --set customer=demo \
  --set domain=demo.mindroom.chat \
  --set openai_key=$OPENAI_API_KEY \
  --set matrix_admin_password=secure-password

# Or use the setup script
./setup.sh demo demo.mindroom.chat
```

## What You Get

- **MindRoom**: Frontend (port 3003) + Backend (port 8765)
- **Synapse**: Matrix server with SQLite database
- **Storage**: Persistent volumes for both services
- **Ingress**: Routes for both `demo.mindroom.chat` and `m-demo.mindroom.chat`

## URLs

After deployment:
- App: `https://demo.mindroom.chat`
- Matrix: `https://m-demo.mindroom.chat`

## Create Matrix Admin User

After deployment, create an admin user:

```bash
# Get synapse pod name
SYNAPSE_POD=$(kubectl get pod -l app=synapse-demo -o name | cut -d/ -f2)

# Create admin user
kubectl exec -it $SYNAPSE_POD -- register_new_matrix_user \
  -c /data/homeserver.yaml \
  -u admin \
  -p your-password \
  --admin \
  http://localhost:8008
```

## Simplifications vs Full Setup

This uses:
- **SQLite** instead of PostgreSQL (simpler, good for <100 users)
- **No Redis** (no caching, but simpler)
- **No Authelia** (use Matrix's built-in auth)
- **Basic config** (minimal agents and settings)

## Files

- Single template file: ~280 lines
- All resources in one file for simplicity
- SQLite = no external database needed
