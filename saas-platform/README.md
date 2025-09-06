# MindRoom SaaS Platform

This directory contains all components specific to running MindRoom as a hosted SaaS platform. If you're looking to self-host MindRoom, you can safely ignore this entire directory.

## Structure

```
saas-platform/
├── infrastructure/     # Terraform configurations for Hetzner Cloud
├── supabase/          # Database schema and migrations
├── apps/              # Platform web applications
│   ├── customer-portal/      # Customer signup and management
│   └── admin-dashboard/      # Administrative interface
├── services/          # Platform microservices
│   ├── stripe-handler/       # Stripe webhook processing
│   └── instance-provisioner/ # Kubernetes/Helm instance provisioner
├── scripts/           # Platform management scripts
│   ├── deployment/    # Infrastructure and service deployment
│   ├── database/      # Database migrations and setup
│   └── testing/       # Platform-specific tests
├── docs/              # Platform documentation
└── Dockerfile.*       # Docker images for platform services
```

## Components

### Infrastructure
- **Hetzner Cloud** servers for hosting
- **Terraform** for infrastructure as code
- **Dokku** for container orchestration
- **DNS management** via Porkbun (optional)

### Applications
- **Customer Portal**: Where customers sign up and manage their subscriptions
- **Admin Dashboard**: For platform administration and monitoring
- **Dokku Provisioner**: Automatically provisions MindRoom instances for customers

### Services
- **Stripe Handler**: Processes payment webhooks and manages subscriptions
- **Database**: Supabase (PostgreSQL) for customer and subscription management

## Deployment

### Prerequisites
1. Hetzner Cloud account and API token
2. Supabase project
3. Stripe account with products configured
4. Gitea registry for Docker images
5. Domain name (optional but recommended)

### Quick Deploy
```bash
# From the repository root
./saas-platform/scripts/deployment/deploy-all.sh
```

This will:
1. Deploy infrastructure with Terraform
2. Run database migrations
3. Build and push Docker images
4. Deploy all platform services
5. Configure Nginx and SSL

### Teardown
```bash
# Complete cleanup (WARNING: destroys everything)
./saas-platform/scripts/deployment/cleanup-all.sh
```

## Configuration

All platform configuration is managed through environment variables in `.env`:

```bash
# Infrastructure
HCLOUD_TOKEN=your_hetzner_token
DOMAIN=mindroom.chat

# Database
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=your_service_key
SUPABASE_DB_PASSWORD=your_db_password

# Payments
STRIPE_SECRET_KEY=sk_live_xxx
STRIPE_PUBLISHABLE_KEY=pk_live_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx

# Docker Registry
GITEA_URL=git.nijho.lt
GITEA_USER=your_username
GITEA_TOKEN=your_token
REGISTRY=git.nijho.lt/basnijholt

# DNS (optional)
PORKBUN_API_KEY=your_api_key
PORKBUN_SECRET_KEY=your_secret_key
```

## Subscription Tiers

The platform supports multiple subscription tiers:

- **Free**: 1 agent, 100 messages/day, 1GB storage
- **Starter** ($29/mo): 3 agents, 1000 messages/day, 5GB storage
- **Professional** ($99/mo): 10 agents, 10000 messages/day, 50GB storage
- **Enterprise** (custom): Unlimited everything

## Development

### Local Testing
The platform components can be tested locally using Docker Compose:

```bash
cd saas-platform/apps/customer-portal
npm run dev

cd saas-platform/services/stripe-handler
npm run dev
```

### Database Migrations
```bash
./saas-platform/scripts/database/run-migrations.sh
```

### Adding New Features
1. Update database schema in `supabase/migrations/`
2. Regenerate combined migration: `cat supabase/migrations/*.sql > supabase/all-migrations.sql`
3. Update services to use new schema
4. Deploy changes

## Architecture

The platform follows a microservices architecture:

```
Internet → Nginx → Platform Services → Supabase Database
                 ↓
           Dokku Server → Customer MindRoom Instances
```

Each customer gets an isolated MindRoom instance running on Dokku with:
- Dedicated subdomain
- Resource limits based on subscription tier
- Automatic backups
- Health monitoring

## Monitoring

- Instance health checks run every 5 minutes
- Usage metrics tracked daily
- Automatic alerting for critical issues
- Uptime tracking per instance

## Security

- All data encrypted at rest and in transit
- Row-level security in database
- API authentication via Supabase
- Stripe webhook signature verification
- SSH key authentication for server access

## Support

This platform infrastructure is specific to the hosted MindRoom service. For self-hosting MindRoom, see the main README in the repository root.
