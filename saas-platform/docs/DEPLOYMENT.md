# MindRoom SaaS Platform Deployment Guide

This guide covers the complete deployment process for the MindRoom SaaS platform on Kubernetes.

## Architecture Overview

The platform consists of:
- **Kubernetes Cluster**: K3s on Hetzner Cloud (managed by terraform-k8s/)
- **Platform Services**: Customer portal, admin dashboard, API services
- **Customer Instances**: Deployed as separate Helm releases in Kubernetes
- **Supabase**: Cloud PostgreSQL database
- **Stripe**: Payment processing

## Quick Start

### Prerequisites

1. **Required Tools**:
   - [Terraform](https://www.terraform.io/downloads) >= 1.0
   - [kubectl](https://kubernetes.io/docs/tasks/tools/)
   - [Packer](https://developer.hashicorp.com/packer/downloads)
   - Docker

2. **Required Accounts**:
   - [Hetzner Cloud](https://console.hetzner.cloud/)
   - [Porkbun](https://porkbun.com/) for DNS
   - [Supabase](https://supabase.com/)
   - [Stripe](https://stripe.com/)
   - Docker registry (e.g., Gitea)

### Deploy Everything

```bash
cd saas-platform/terraform-k8s

# 1. Configure credentials
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your credentials

# 2. Generate SSH keys
ssh-keygen -t ed25519 -f cluster_ssh_key -N ""

# 3. Build MicroOS snapshots (first time only)
export HCLOUD_TOKEN="your-hetzner-token"
packer build hcloud-microos-snapshots.pkr.hcl

# 4. Deploy Kubernetes cluster and platform
terraform init
terraform apply
```

This deploys:
- K3s Kubernetes cluster on Hetzner
- nginx-ingress controller with cert-manager
- Platform services (customer portal, admin dashboard, API)
- DNS records for platform and customer wildcards

### Access the Cluster

```bash
export KUBECONFIG=$(terraform output -raw kubeconfig_path)
kubectl get nodes
kubectl get pods -n mindroom-staging
```

## Service Endpoints

After deployment (staging environment):
- **Customer Portal**: https://app.staging.mindroom.chat
- **Admin Dashboard**: https://admin.staging.mindroom.chat
- **API**: https://api.staging.mindroom.chat
- **Webhooks**: https://webhooks.staging.mindroom.chat
- **Customer Instances**: https://*.staging.mindroom.chat

## Building and Deploying Updates

### Build Docker Images

```bash
# Authenticate with registry
docker login git.nijho.lt -u username

# Build services
docker build -f Dockerfile.customer-portal -t git.nijho.lt/username/customer-portal:latest .
docker build -f Dockerfile.admin-dashboard -t git.nijho.lt/username/admin-dashboard:latest .
docker build -f Dockerfile.stripe-handler -t git.nijho.lt/username/stripe-handler:latest .
docker build -f Dockerfile.dokku-provisioner -t git.nijho.lt/username/dokku-provisioner:latest .

# Push to registry
docker push git.nijho.lt/username/customer-portal:latest
docker push git.nijho.lt/username/admin-dashboard:latest
docker push git.nijho.lt/username/stripe-handler:latest
docker push git.nijho.lt/username/dokku-provisioner:latest
```

### Deploy Updates

```bash
# Restart deployments to pull new images
kubectl rollout restart deployment -n mindroom-staging customer-portal
kubectl rollout restart deployment -n mindroom-staging admin-dashboard
kubectl rollout restart deployment -n mindroom-staging stripe-handler
kubectl rollout restart deployment -n mindroom-staging instance-provisioner
```

## Local Development with Kind

For local testing without cloud costs:

```bash
cd saas-platform/k8s

# Start local cluster
./kind-setup.sh

# Deploy platform
helm install mindroom-staging platform/ -f platform/values-staging.yaml

# Access services via port-forward
kubectl port-forward -n mindroom-staging svc/customer-portal 3000:3000
```

## Database Setup

### Run Migrations

```bash
cd saas-platform/scripts/database
./run-migrations.sh
```

### Create Admin User

```bash
SUPABASE_URL=https://your-project.supabase.co \
SUPABASE_SERVICE_KEY=your-service-key \
ADMIN_EMAIL=admin@mindroom.test \
ADMIN_PASSWORD=AdminPass123! \
node create-admin-user.js
```

### Setup Stripe Products

```bash
STRIPE_SECRET_KEY=sk_test_... \
node setup-stripe-products.js
```

## Monitoring

### Check Service Status

```bash
# View all pods
kubectl get pods -n mindroom-staging

# Check specific service
kubectl describe pod -n mindroom-staging -l app=customer-portal

# View logs
kubectl logs -n mindroom-staging -l app=customer-portal

# Stream logs
kubectl logs -f -n mindroom-staging deployment/customer-portal
```

### Resource Usage

```bash
# Node resources
kubectl top nodes

# Pod resources
kubectl top pods -n mindroom-staging
```

## Customer Instance Management

Customer instances are deployed as separate Helm releases:

```bash
# Deploy a new customer instance
helm install customer-acme k8s/instance/ \
  --set customer.name=acme \
  --set customer.subdomain=acme \
  --set environment=staging

# List customer instances
helm list | grep customer-

# Upgrade a customer instance
helm upgrade customer-acme k8s/instance/ --reuse-values

# Delete a customer instance
helm uninstall customer-acme
```

## Troubleshooting

### Pod Issues

```bash
# Get pod events
kubectl describe pod -n mindroom-staging <pod-name>

# Check pod logs
kubectl logs -n mindroom-staging <pod-name> --previous

# Execute into pod
kubectl exec -it -n mindroom-staging <pod-name> -- /bin/sh
```

### DNS Issues

```bash
# Check ingress
kubectl get ingress -n mindroom-staging

# Check certificates
kubectl get certificates -n mindroom-staging

# DNS lookup
nslookup app.staging.mindroom.chat
```

### Registry Authentication

```bash
# Check image pull secrets
kubectl get secrets -n mindroom-staging gitea-registry -o yaml

# Verify registry access
docker pull git.nijho.lt/username/customer-portal:latest
```

## Security Notes

1. **Secrets Management**: All secrets stored in Kubernetes secrets
2. **TLS/SSL**: Automatically managed by cert-manager
3. **Network Policies**: Can be configured for pod isolation
4. **RBAC**: Kubernetes role-based access control
5. **Image Scanning**: Scan Docker images for vulnerabilities

## Destroying Infrastructure

To tear down everything:

```bash
cd saas-platform/terraform-k8s
terraform destroy
```

This removes:
- K8s cluster and server
- DNS records
- All deployed applications

## Next Steps

After deployment:
1. Configure Stripe webhook endpoint in Stripe Dashboard
2. Test customer signup flow
3. Set up monitoring (Prometheus/Grafana)
4. Configure backup strategy for persistent volumes
5. Set up CI/CD pipeline for automated deployments
