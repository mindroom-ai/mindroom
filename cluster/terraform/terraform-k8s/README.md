# MindRoom K8s Infrastructure

Terraform configuration for deploying MindRoom on Kubernetes, with separate cluster bootstrap and platform deployment steps.

## Prerequisites

1. **Required Tools:**
   - [Terraform](https://www.terraform.io/downloads) >= 1.5.0 (required by the pinned kube-hetzner module)
   - [kubectl](https://kubernetes.io/docs/tasks/tools/)
   - [Packer](https://developer.hashicorp.com/packer/downloads) (for MicroOS snapshots)
   - Docker (for building images)

2. **Required Accounts:**
   - [Hetzner Cloud](https://console.hetzner.cloud/) account
   - [Porkbun](https://porkbun.com/) account with API access enabled
   - [Supabase](https://supabase.com/) project
   - [Stripe](https://stripe.com/) account (test mode is fine)
   - Access to the published GHCR images (or your own registry override)

3. **Domain Setup:**
   - Own a domain (e.g., mindroom.chat)
   - Domain must use Porkbun nameservers

4. **Docker Images:**
   The platform expects Docker images to be available in GHCR by default.

   Platform images needed:
   - `ghcr.io/mindroom-ai/platform-frontend:latest`
   - `ghcr.io/mindroom-ai/platform-backend:latest`

   Customer instance images (from main MindRoom project):
   - `ghcr.io/mindroom-ai/mindroom:latest`

   Override `registry` if you want to deploy from a different registry.

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
   cd cluster/terraform/terraform-k8s
   ```

2. **Copy and configure terraform.tfvars:**
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   # Edit terraform.tfvars with your credentials
   ```
   The example enables the platform with `deploy_platform = true`; the variable defaults to `false` when omitted, which skips the platform Helm release.
   Replace the example `domain` and `root_domain` with your deployment hostname suffix and Porkbun DNS zone (see [Environments](#environments)).
   Set a strong, unique `provisioner_api_key` before deploying the platform (see [Required Credentials](#required-credentials)).

3. **Generate SSH keys for the cluster:**
   ```bash
   ssh-keygen -t ed25519 -f cluster_ssh_key -N ""
   ```

4. **Build MicroOS snapshots if the target Hetzner project lacks them:**
   ```bash
   # Install Packer if not already installed
   # https://developer.hashicorp.com/packer/downloads

   # Export your Hetzner token for Packer
   export HCLOUD_TOKEN="your-hetzner-token-here"

   # Install the plugins declared by the template
   packer init hcloud-microos-snapshots.pkr.hcl

   # Build the MicroOS snapshots (takes ~5-10 minutes)
   packer build hcloud-microos-snapshots.pkr.hcl
   ```
   This creates OpenSUSE MicroOS snapshots in the project selected by `HCLOUD_TOKEN`.
   Packer's `HCLOUD_TOKEN` and Terraform's `hcloud_token` must target the intended project because [Hetzner API tokens are scoped to projects](https://docs.hetzner.com/cloud/api/getting-started/using-api/).
   Suitable snapshots can be reused within that project.
   For another project, build the snapshots there or [move existing snapshots to that project](https://docs.hetzner.com/cloud/servers/backups-snapshots/faq/#are-backupssnapshots-moveable).

5. **Bootstrap the cluster, then deploy the platform and remaining resources:**
   ```bash
   terraform init
   terraform apply -target=module.kube-hetzner
   terraform apply
   ```
   The initial targeted apply follows `scripts/up.sh` and creates the kubeconfig used by the Kubernetes and Helm providers.
   Run the full apply afterward to include DNS, certificate issuers, monitoring, and the enabled platform.

## Required Credentials

- **Hetzner Cloud API Token**: From https://console.hetzner.cloud/
- **Porkbun API Keys**: From https://porkbun.com/account/api
- **Supabase**: Project URL and keys
- **Stripe**: API keys and webhook secret
- **Provisioner**: Set `provisioner_api_key` to a strong, unique secret and configure clients of the provisioning `/system/*` endpoints to send the same value as a bearer token.

The provisioner key also derives stable per-instance secrets when `INSTANCE_CREDENTIALS_ENCRYPTION_SECRET` is unset.
This Terraform configuration passes `provisioner_api_key` to Helm but does not configure a dedicated derivation root, so keep the key stable for existing tenants.

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

`environment` controls deployment naming; it does not prepend a hostname prefix.
`domain` is the full deployment domain used for hosts such as `app.<domain>`, while `root_domain` selects the Porkbun DNS zone.
Replace `example.test` below with your own domain.

For staging, set these values in `terraform.tfvars`:

```hcl
environment = "staging"
domain      = "staging.example.test"
root_domain = "example.test"
```

For production at the root domain:

```hcl
environment = "production"
domain      = "example.test"
root_domain = "example.test"
```

## Destroying

To tear down everything:
```bash
terraform destroy
```

This will remove:
- K8s cluster and server
- DNS records
- All deployed applications
