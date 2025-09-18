# MindRoom K8s Infrastructure

Complete Terraform configuration for deploying MindRoom on Kubernetes with a single `terraform apply`.

## Prerequisites

1. **Required Tools:**
   - [Terraform](https://www.terraform.io/downloads) >= 1.0
   - [kubectl](https://kubernetes.io/docs/tasks/tools/)
   - [Packer](https://developer.hashicorp.com/packer/downloads) (for MicroOS snapshots)
   - Docker (for building images)

2. **Required Accounts:**
   - [Hetzner Cloud](https://console.hetzner.cloud/) account
   - [Porkbun](https://porkbun.com/) account with API access enabled
   - [Supabase](https://supabase.com/) project
   - [Stripe](https://stripe.com/) account (test mode is fine)
   - [Gitea](https://gitea.io/) or similar Docker registry

3. **Domain Setup:**
   - Own a domain (e.g., mindroom.chat)
   - Domain must use Porkbun nameservers

4. **Docker Images:**
   The platform expects Docker images to be available in your Gitea registry.

   Platform images needed:
   - `platform-frontend:latest`
   - `platform-backend:latest`

   Customer instance images (from main MindRoom project):
   - `mindroom-backend:latest`
   - `mindroom-frontend:latest`

   These should be built and pushed to your registry before deployment.

## What It Deploys

1. **K3s Kubernetes Cluster** on Hetzner Cloud
   - Single-node setup (CPX31 by default)
   - nginx-ingress controller
   - cert-manager for SSL
   - Longhorn for storage

2. **DNS Records** via Porkbun
   - Platform subdomains (app, api, webhooks)
   - Wildcard for customer instances

3. **MindRoom Platform** via Helm
   - Customer portal (with admin interface)
   - Stripe webhook handler
   - Instance provisioner
4. **Monitoring Stack** (Prometheus + Alertmanager)
   - Deployed via `kube-prometheus-stack`
   - Scrapes platform backend metrics and ships built-in security alerts

## Quick Start

1. **Navigate to the terraform-k8s directory:**
   ```bash
   cd saas-platform/terraform-k8s
   ```

2. **Copy and configure terraform.tfvars:**
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   # Edit terraform.tfvars with your credentials
   ```

3. **Generate SSH keys for the cluster:**
   ```bash
   ssh-keygen -t ed25519 -f cluster_ssh_key -N ""
   ```

4. **Build MicroOS snapshots (first time only):**
   ```bash
   # Install Packer if not already installed
   # https://developer.hashicorp.com/packer/downloads

   # Export your Hetzner token for Packer
   export HCLOUD_TOKEN="your-hetzner-token-here"

   # Build the MicroOS snapshots (takes ~5-10 minutes)
   packer build hcloud-microos-snapshots.pkr.hcl
   ```
   Note: This creates OpenSUSE MicroOS snapshots in your Hetzner account.
   Only needed once per Hetzner account. The snapshots will be reused for all future deployments.

5. **Deploy everything:**
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

## Post-Deployment Setup

### Configure Authentication Providers

After deployment, configure OAuth providers in Supabase:

1. **Access Supabase Dashboard**: https://supabase.com/dashboard/project/[your-project-id]
2. **Enable Providers**: Authentication → Providers → Enable Google/GitHub
3. **Add Redirect URLs**: Authentication → URL Configuration
   ```
   https://app.<superdomain>/auth/callback
   http://localhost:3000/auth/callback
   ```

Terraform will output OAuth setup instructions after deployment.

## Environments

- **Staging/Test**: Uses `<environment>.<domain>` as the superdomain
- **Production**: Uses root `<domain>` as the superdomain

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
