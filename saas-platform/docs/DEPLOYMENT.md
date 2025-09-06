# MindRoom SaaS Platform Deployment Guide

This guide covers the complete deployment process for the MindRoom SaaS platform.

## Architecture Overview

The platform consists of:
- **Dokku Server**: Hosts customer MindRoom instances
- **Platform Server**: Runs management services
- **Supabase**: Cloud PostgreSQL database
- **Stripe**: Payment processing

## Prerequisites

1. **Hetzner Cloud Account**: For server infrastructure
2. **Porkbun Account**: For DNS management (or any DNS provider)
3. **Supabase Project**: For database
4. **Stripe Account**: For payments
5. **Gitea Registry**: For Docker images (or use Docker Hub)

## Environment Setup

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Fill in all required credentials in `.env`:

```bash
# Hetzner Cloud
HCLOUD_TOKEN=your_hetzner_token

# DNS (Porkbun)
PORKBUN_API_KEY=your_api_key
PORKBUN_SECRET_KEY=your_secret_key

# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your_anon_key
SUPABASE_SERVICE_KEY=your_service_key
SUPABASE_DB_PASSWORD=your_database_password

# Stripe
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...

# Docker Registry (Gitea)
REGISTRY=git.nijho.lt/username
GITEA_URL=git.nijho.lt
GITEA_USER=username
GITEA_TOKEN=your_gitea_token
DOCKER_ARCH=amd64

# Platform Configuration
PLATFORM_DOMAIN=mindroom.chat
```

## Quick Start

### Deploy Everything

Run the complete deployment script:

```bash
chmod +x saas-platform/scripts/deployment/deploy-all.sh
./saas-platform/scripts/deployment/deploy-all.sh
```

This script will:
1. Deploy infrastructure with Terraform (2 Hetzner servers)
2. Configure DNS records
3. Run database migrations
4. Build and push Docker images
5. Deploy all platform services
6. Configure Nginx reverse proxy
7. Set up Stripe products
8. Verify deployment

### Clean Up Everything

To destroy all infrastructure and services:

```bash
chmod +x saas-platform/scripts/deployment/cleanup-all.sh
./saas-platform/scripts/deployment/cleanup-all.sh
```

## Manual Deployment Steps

If you prefer to deploy step by step:

### 1. Deploy Infrastructure

```bash
cd saas-platform/infrastructure/terraform
terraform init
terraform plan
terraform apply
cd ../..
```

### 2. Run Database Migrations

```bash
./saas-platform/scripts/database/run-migrations.sh
```

This uses SSH to run migrations from the platform server (works around network restrictions).

### 3. Build and Push Docker Images

```bash
# Authenticate with registry
echo "$GITEA_TOKEN" | docker login $GITEA_URL -u $GITEA_USER --password-stdin

# Build and push each service
docker build -f apps/customer-portal/Dockerfile -t $REGISTRY/customer-portal:$DOCKER_ARCH .
docker push $REGISTRY/customer-portal:$DOCKER_ARCH

docker build -f apps/admin-dashboard/Dockerfile -t $REGISTRY/admin-dashboard:$DOCKER_ARCH .
docker push $REGISTRY/admin-dashboard:$DOCKER_ARCH

docker build -f apps/stripe-handler/Dockerfile -t $REGISTRY/stripe-handler:$DOCKER_ARCH .
docker push $REGISTRY/stripe-handler:$DOCKER_ARCH

docker build -f apps/dokku-provisioner/Dockerfile -t $REGISTRY/dokku-provisioner:$DOCKER_ARCH .
docker push $REGISTRY/dokku-provisioner:$DOCKER_ARCH
```

### 4. Deploy Services to Platform Server

SSH into the platform server and run the containers:

```bash
ssh root@<platform-ip>

# Pull and run services
docker pull $REGISTRY/customer-portal:$DOCKER_ARCH
docker run -d --name customer-portal -p 3000:3000 --env-file .env $REGISTRY/customer-portal:$DOCKER_ARCH

# Repeat for other services...
```

### 5. Configure Stripe Products

```bash
node saas-platform/scripts/database/setup-stripe-products.js
```

## Service Endpoints

After deployment, services are available at:

- **Customer Portal**: http://portal.mindroom.chat
- **Admin Dashboard**: http://admin.mindroom.chat
- **API Endpoint**: http://api.mindroom.chat
- **Stripe Webhook**: http://api.mindroom.chat/stripe/webhook

## Monitoring

Check service status:

```bash
# Platform services
ssh root@<platform-ip> docker ps

# Dokku apps
ssh dokku@<dokku-ip> apps:list
```

View logs:

```bash
# Platform service logs
ssh root@<platform-ip> docker logs customer-portal

# Dokku app logs
ssh dokku@<dokku-ip> logs <app-name>
```

## Troubleshooting

### Database Connection Issues

If you can't connect to Supabase from your local network:
- The migration script uses SSH tunnel through the platform server
- Alternatively, run migrations from Supabase Dashboard SQL editor

### Docker Registry Authentication

If you get "unauthorized" errors:
```bash
echo "$GITEA_TOKEN" | docker login $GITEA_URL -u $GITEA_USER --password-stdin
```

### DNS Propagation

DNS changes can take up to 48 hours to propagate. Use IP addresses directly if needed:
- http://<platform-ip>:3000 (Customer Portal)
- http://<platform-ip>:3001 (Admin Dashboard)

## Security Notes

1. **SSH Keys**: Make sure your SSH key is added to servers
2. **Firewall**: Servers have UFW configured to allow only necessary ports
3. **HTTPS**: Use Certbot to enable SSL certificates after DNS propagates
4. **Secrets**: Never commit `.env` or `terraform.tfvars` files

## Next Steps

After deployment:

1. Configure Stripe webhook endpoint in Stripe Dashboard
2. Test customer signup flow
3. Monitor first deployments
4. Set up backup strategy
5. Configure monitoring and alerts
