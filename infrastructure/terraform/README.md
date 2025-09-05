# MindRoom SaaS Infrastructure

Terraform configuration for deploying the MindRoom SaaS platform on Hetzner Cloud.

## Architecture Overview

The infrastructure consists of:
- **Dokku Server**: Hosts isolated customer MindRoom instances
- **Platform Server**: Runs management services (Stripe handler, provisioner, portals)
- **Private Network**: Internal communication between servers
- **Storage Volumes**: Persistent data for both servers
- **Firewall Rules**: Security configuration for each server

## Prerequisites

1. **Terraform** (>= 1.0)
   ```bash
   brew install terraform  # macOS
   # or download from https://www.terraform.io/downloads
   ```

2. **Hetzner Cloud Account**
   - Create account at https://www.hetzner.com/cloud
   - Generate API token: Cloud Console → Project → API tokens → Generate API Token

3. **Supabase Project**
   - Create project at https://app.supabase.com
   - Get credentials from Settings → API

4. **Stripe Account**
   - Create account at https://stripe.com
   - Get API keys from Dashboard → Developers → API keys
   - Create webhook endpoint pointing to `https://webhooks.mindroom.chat/stripe`
   - Create subscription products and get price IDs

5. **Domain Configuration**
   - Domain: mindroom.chat (or your domain)
   - Access to DNS management

## Setup Instructions

### 1. Generate SSH Keys

```bash
# Admin SSH key (if not exists)
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa

# Dokku provisioner key
mkdir -p ssh
ssh-keygen -t rsa -b 4096 -f ssh/dokku_provisioner -N ""
```

### 2. Configure Variables

```bash
# Copy example file
cp terraform.tfvars.example terraform.tfvars

# Edit with your values
nano terraform.tfvars
```

Required values:
```hcl
hcloud_token = "YOUR_HETZNER_API_TOKEN"
domain = "mindroom.chat"

# Get from Supabase project settings
supabase_url = "https://xxxxx.supabase.co"
supabase_anon_key = "eyJ..."
supabase_service_key = "eyJ..."

# Get from Stripe dashboard
stripe_secret_key = "sk_live_..."
stripe_publishable_key = "pk_live_..."
stripe_webhook_secret = "whsec_..."

# After creating products in Stripe
stripe_price_starter = "price_..."
stripe_price_professional = "price_..."
stripe_price_enterprise = "price_..."

# Your IP for SSH access (important for security!)
admin_ips = ["YOUR.IP.ADD.RESS/32"]
```

### 3. Deploy Infrastructure

```bash
# Initialize Terraform
terraform init

# Review planned changes
terraform plan

# Apply configuration
terraform apply

# Save important outputs
terraform output -json > outputs.json
```

### 4. Configure DNS

After deployment, configure these DNS records:

```
# Main domain
A     mindroom.chat              <platform_server_ip>
AAAA  mindroom.chat              <platform_server_ipv6>

# Platform services
A     app.mindroom.chat          <platform_server_ip>
A     admin.mindroom.chat        <platform_server_ip>
A     api.mindroom.chat          <platform_server_ip>
A     webhooks.mindroom.chat     <platform_server_ip>

# Customer instances (wildcard)
A     *.mindroom.chat            <dokku_server_ip>
AAAA  *.mindroom.chat            <dokku_server_ipv6>

# Matrix federation (optional)
A     *.m.mindroom.chat          <dokku_server_ip>
```

### 5. Verify Deployment

```bash
# Check Dokku server
ssh root@<dokku_server_ip>
dokku apps:list

# Check platform services
ssh root@<platform_server_ip>
docker ps

# Test endpoints
curl https://app.mindroom.chat
curl https://admin.mindroom.chat  # Requires auth: admin / MindRoom2024!
curl https://api.mindroom.chat/provision/health
curl https://webhooks.mindroom.chat/stripe/health
```

## Post-Deployment Setup

### 1. Complete SSL Setup

If SSL certificates weren't obtained during deployment (DNS not ready):

```bash
ssh root@<platform_server_ip>

# Retry SSL certificate generation
certbot certonly --nginx --non-interactive --agree-tos \
  --email admin@mindroom.chat \
  -d mindroom.chat \
  -d app.mindroom.chat \
  -d admin.mindroom.chat \
  -d api.mindroom.chat \
  -d webhooks.mindroom.chat

# Restart nginx
systemctl restart nginx
```

### 2. Deploy Platform Services

The platform services need their code deployed:

```bash
# Clone service repositories to platform server
ssh root@<platform_server_ip>

# Deploy each service
cd /mnt/platform-data/stripe-handler
git clone <stripe-handler-repo> .
docker-compose restart stripe-handler

cd /mnt/platform-data/dokku-provisioner
git clone <dokku-provisioner-repo> .
docker-compose restart dokku-provisioner

cd /mnt/platform-data/customer-portal
git clone <customer-portal-repo> .
docker-compose restart customer-portal

cd /mnt/platform-data/admin-dashboard
git clone <admin-dashboard-repo> .
docker-compose restart admin-dashboard
```

### 3. Configure Stripe Webhook

1. Go to Stripe Dashboard → Developers → Webhooks
2. Add endpoint: `https://webhooks.mindroom.chat/stripe`
3. Select events:
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_succeeded`
   - `invoice.payment_failed`
4. Copy webhook signing secret to terraform.tfvars

### 4. Initialize Supabase Database

Run migrations from the Supabase agent output:

```sql
-- In Supabase SQL editor
-- Copy and run the migrations from deploy/platform/prompts/01-supabase-agent.md
```

## Management Commands

### View Infrastructure State
```bash
terraform show
terraform output
```

### Update Infrastructure
```bash
terraform plan
terraform apply
```

### Destroy Infrastructure (CAUTION!)
```bash
terraform destroy
```

### SSH Access
```bash
# Dokku server
ssh root@$(terraform output -raw dokku_server_ip)

# Platform server
ssh root@$(terraform output -raw platform_server_ip)

# View Dokku admin password
terraform output dokku_admin_password
```

## Scaling Considerations

### Server Sizing

Current configuration:
- **Dokku Server**: CPX31 (4 vCPU, 8GB RAM) - Supports ~20 concurrent instances
- **Platform Server**: CPX21 (3 vCPU, 4GB RAM) - Handles management services

To scale:
1. Edit `terraform.tfvars`:
   ```hcl
   dokku_server_type = "cpx41"  # 8 vCPU, 16GB RAM
   ```
2. Run `terraform apply`

### Adding More Dokku Servers

For horizontal scaling, add additional Dokku servers:
1. Copy the `hcloud_server.dokku` resource in `main.tf`
2. Rename to `hcloud_server.dokku_2`
3. Update DNS wildcards for load balancing

## Backup Strategy

Automatic backups are enabled by default:
- Daily snapshots of both servers
- 7-day retention
- Stored in Hetzner Cloud

Manual backup:
```bash
# Backup Dokku data
ssh root@<dokku_server_ip>
dokku postgres:export mindroom > backup.sql

# Backup platform data
ssh root@<platform_server_ip>
docker exec postgres pg_dumpall > backup.sql
```

## Monitoring

### Server Metrics
- CPU, RAM, Network: Hetzner Cloud Console → Graphs
- Disk usage: `df -h` on servers
- Docker: `docker stats` on platform server

### Application Monitoring
- Stripe webhooks: Stripe Dashboard → Webhooks → Logs
- Supabase: Supabase Dashboard → Logs
- Dokku apps: `dokku ps:report` on Dokku server

## Troubleshooting

### Common Issues

1. **DNS not resolving**
   - Verify DNS records are configured
   - Wait for propagation (up to 48 hours)
   - Test with `dig mindroom.chat`

2. **SSL certificate errors**
   - Ensure DNS is configured first
   - Run SSL setup script manually
   - Check Let's Encrypt rate limits

3. **Services not starting**
   - Check logs: `docker logs <container>`
   - Verify environment variables in `/opt/platform/.env`
   - Ensure services code is deployed

4. **Dokku deployment fails**
   - Check SSH key: `dokku ssh-keys:list`
   - Verify git remote: `git remote -v`
   - Check Dokku logs: `dokku logs <app>`

## Security Notes

1. **Change default passwords**
   - Admin dashboard: Edit `/etc/nginx/.htpasswd`
   - Dokku admin: `passwd dokku`

2. **Restrict SSH access**
   - Update `admin_ips` in terraform.tfvars
   - Use specific IPs, not 0.0.0.0/0

3. **Keep secrets secure**
   - Never commit terraform.tfvars
   - Use Terraform Cloud for remote state
   - Rotate API keys regularly

4. **Regular updates**
   - `apt update && apt upgrade` monthly
   - Update Docker images regularly
   - Keep Terraform providers updated

## Support

For issues or questions:
- Infrastructure: Check Hetzner status page
- Dokku: https://dokku.com/docs
- Platform services: See individual service documentation
