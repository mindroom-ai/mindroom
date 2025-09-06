# Platform Services

Core MindRoom platform services deployed as simple Kubernetes manifests.

## Services

- **customer-portal**: Next.js customer-facing web app (port 3000)
- **admin-dashboard**: React admin interface (port 80)
- **stripe-handler**: Node.js webhook handler (port 3005)
- **instance-provisioner**: Provisioning service (port 8002)

## Deployment

```bash
# From k8s/ directory
cd platform/

# Create secrets from .env file
./setup-secrets.sh

# Deploy all services
kubectl apply -f deploy.yml

# Check status
kubectl get pods -n mindroom
```

## Access Services

```bash
# Port-forward to access locally
kubectl port-forward -n mindroom svc/customer-portal 3000:3000
kubectl port-forward -n mindroom svc/admin-dashboard 8080:80
```

## Configuration

All configuration comes from environment variables in the `.env` file.
The `setup-secrets.sh` script creates Kubernetes secrets from this file.

## Why Simple Manifests?

- Platform services are deployed once per cluster
- No need for templating or multiple instances
- Easier to understand and debug
- Follows KISS principle
