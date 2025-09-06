# MindRoom K8s Infrastructure

Complete Terraform configuration for deploying MindRoom on Kubernetes with a single `terraform apply`.

## What It Deploys

1. **K3s Kubernetes Cluster** on Hetzner Cloud
   - Single-node setup (CPX31 by default)
   - nginx-ingress controller
   - cert-manager for SSL
   - Longhorn for storage

2. **DNS Records** via Porkbun
   - Platform subdomains (app, admin, api, webhooks)
   - Wildcard for customer instances

3. **MindRoom Platform** via Helm
   - Customer portal
   - Admin dashboard
   - Stripe webhook handler
   - Instance provisioner

## Quick Start

1. **Copy and configure terraform.tfvars:**
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   # Edit terraform.tfvars with your credentials
   ```

2. **Generate SSH keys for the cluster:**
   ```bash
   ssh-keygen -t ed25519 -f cluster_ssh_key -N ""
   ```

3. **Deploy everything:**
   ```bash
   terraform init
   terraform apply
   ```

## Required Credentials

- **Hetzner Cloud API Token**: From https://console.hetzner.cloud/
- **Porkbun API Keys**: From https://porkbun.com/account/api
- **Supabase**: Project URL and keys
- **Stripe**: API keys and webhook secret
- **Gitea**: Registry token for Docker images

## Outputs

After deployment, you'll get:
- Cluster IP address
- Kubeconfig file path
- Platform URLs
- DNS records created

## Accessing the Cluster

```bash
export KUBECONFIG=$(terraform output -raw kubeconfig_path)
kubectl get nodes
kubectl get pods -A
```

## Environments

- **Staging**: Uses `staging.mindroom.chat` subdomain
- **Production**: Uses root `mindroom.chat` domain

Set via `environment` variable in terraform.tfvars.

## Destroying

To tear down everything:
```bash
terraform destroy
```

This will remove:
- K8s cluster and server
- DNS records
- All deployed applications
