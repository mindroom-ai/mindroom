# Agent 6: Orchestration and Integration

## Project Context

You are the final agent working on MindRoom's multi-tenant SaaS platform. The other 5 agents have completed their individual services:

1. **Supabase Setup** (`supabase/`) - Database, auth, and edge functions
2. **Stripe Handler** (`services/stripe-handler/`) - Webhook processing service
3. **Dokku Provisioner** (`services/dokku-provisioner/`) - Instance provisioning service
4. **Customer Portal** (`apps/customer-portal/`) - Customer-facing Next.js app
5. **Admin Dashboard** (`apps/admin-dashboard/`) - Internal React Admin panel

### Your Goal

Create the integration and deployment files that tie everything together. You will NOT modify any code in the service directories - only create orchestration and configuration files.

## Your Specific Tasks

### Task 1: Docker Compose Orchestration

Create `deploy/platform/docker-compose.local.yml` for local development:

```yaml
version: '3.8'

services:
  # PostgreSQL for platform data
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: mindroom_platform
      POSTGRES_USER: mindroom
      POSTGRES_PASSWORD: localdev123
    ports:
      - "5432:5432"
    volumes:
      - ./database/init.sql:/docker-entrypoint-initdb.d/init.sql
      - postgres_data:/var/lib/postgresql/data

  # Redis for caching and sessions
  redis:
    image: redis:7-alpine
    command: redis-server --requirepass localdev123
    ports:
      - "6379:6379"

  # Stripe webhook handler
  stripe-handler:
    build: ../../services/stripe-handler
    ports:
      - "3005:3005"
    environment:
      PORT: 3005
      STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY}
      STRIPE_WEBHOOK_SECRET: ${STRIPE_WEBHOOK_SECRET}
      SUPABASE_URL: ${SUPABASE_URL}
      SUPABASE_SERVICE_KEY: ${SUPABASE_SERVICE_KEY}
      DOKKU_PROVISIONER_URL: http://dokku-provisioner:8002
    depends_on:
      - postgres
      - redis
      - dokku-provisioner

  # Dokku provisioner
  dokku-provisioner:
    build: ../../services/dokku-provisioner
    ports:
      - "8002:8002"
    environment:
      DOKKU_HOST: ${DOKKU_HOST}
      DOKKU_USER: dokku
      DOKKU_SSH_KEY_PATH: /app/ssh/dokku_key
      BASE_DOMAIN: ${BASE_DOMAIN:-mindroom.local}
      SUPABASE_URL: ${SUPABASE_URL}
      SUPABASE_SERVICE_KEY: ${SUPABASE_SERVICE_KEY}
    volumes:
      - ./ssh/dokku_key:/app/ssh/dokku_key:ro
      - /var/run/docker.sock:/var/run/docker.sock
    depends_on:
      - postgres

  # Customer portal (development)
  customer-portal:
    build:
      context: ../../apps/customer-portal
      dockerfile: Dockerfile.dev
    ports:
      - "3000:3000"
    environment:
      NEXT_PUBLIC_SUPABASE_URL: ${SUPABASE_URL}
      NEXT_PUBLIC_SUPABASE_ANON_KEY: ${SUPABASE_ANON_KEY}
      NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY: ${STRIPE_PUBLISHABLE_KEY}
    volumes:
      - ../../apps/customer-portal:/app
      - /app/node_modules

  # Admin dashboard
  admin-dashboard:
    build: ../../apps/admin-dashboard
    ports:
      - "3001:3000"
    environment:
      REACT_APP_SUPABASE_URL: ${SUPABASE_URL}
      REACT_APP_SUPABASE_SERVICE_KEY: ${SUPABASE_SERVICE_KEY}
      REACT_APP_PROVISIONER_URL: http://dokku-provisioner:8002
      REACT_APP_STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY}
    depends_on:
      - postgres
      - dokku-provisioner

volumes:
  postgres_data:

networks:
  default:
    name: mindroom-platform
```

### Task 2: Production Docker Compose

Create `deploy/platform/docker-compose.prod.yml`:

```yaml
version: '3.8'

services:
  # Use managed services in production:
  # - Supabase Cloud for database/auth
  # - Stripe webhooks point to public URL
  # - Redis from cloud provider

  # Stripe handler (production)
  stripe-handler:
    image: ${REGISTRY}/mindroom-stripe-handler:${VERSION:-latest}
    restart: always
    environment:
      PORT: 3005
      STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY}
      STRIPE_WEBHOOK_SECRET: ${STRIPE_WEBHOOK_SECRET}
      SUPABASE_URL: ${SUPABASE_URL}
      SUPABASE_SERVICE_KEY: ${SUPABASE_SERVICE_KEY}
      DOKKU_PROVISIONER_URL: http://dokku-provisioner:8002
    networks:
      - platform-network
      - traefik-public
    labels:
      - traefik.enable=true
      - traefik.http.routers.stripe-handler.rule=Host(`webhooks.${PLATFORM_DOMAIN}`)
      - traefik.http.routers.stripe-handler.entrypoints=websecure
      - traefik.http.routers.stripe-handler.tls=true

  # Dokku provisioner (production)
  dokku-provisioner:
    image: ${REGISTRY}/mindroom-dokku-provisioner:${VERSION:-latest}
    restart: always
    environment:
      DOKKU_HOST: ${DOKKU_HOST}
      DOKKU_USER: dokku
      DOKKU_SSH_KEY_PATH: /run/secrets/dokku_key
      BASE_DOMAIN: ${BASE_DOMAIN}
      SUPABASE_URL: ${SUPABASE_URL}
      SUPABASE_SERVICE_KEY: ${SUPABASE_SERVICE_KEY}
    secrets:
      - dokku_key
    networks:
      - platform-network

  # Customer portal (production)
  customer-portal:
    image: ${REGISTRY}/mindroom-customer-portal:${VERSION:-latest}
    restart: always
    environment:
      # Next.js needs build-time env vars
      NODE_ENV: production
    networks:
      - traefik-public
    labels:
      - traefik.enable=true
      - traefik.http.routers.customer-portal.rule=Host(`app.${PLATFORM_DOMAIN}`)
      - traefik.http.routers.customer-portal.entrypoints=websecure
      - traefik.http.routers.customer-portal.tls=true

  # Admin dashboard (production)
  admin-dashboard:
    image: ${REGISTRY}/mindroom-admin-dashboard:${VERSION:-latest}
    restart: always
    environment:
      NODE_ENV: production
    networks:
      - platform-network
      - traefik-public
    labels:
      - traefik.enable=true
      - traefik.http.routers.admin-dashboard.rule=Host(`admin.${PLATFORM_DOMAIN}`)
      - traefik.http.routers.admin-dashboard.entrypoints=websecure
      - traefik.http.routers.admin-dashboard.tls=true
      # Add IP whitelist for admin
      - traefik.http.middlewares.admin-ipwhitelist.ipwhitelist.sourcerange=10.0.0.0/8,192.168.0.0/16

secrets:
  dokku_key:
    external: true

networks:
  platform-network:
    driver: overlay
  traefik-public:
    external: true
```

### Task 3: Environment Configuration

Create `.env.example` in the root:

```bash
# Platform Configuration
PLATFORM_DOMAIN=mindroom.chat
BASE_DOMAIN=mindroom.chat
REGISTRY=registry.mindroom.chat

# Supabase
SUPABASE_URL=https://xxxxxxxxxxxxx.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# Stripe
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_STARTER=price_...
STRIPE_PRICE_PRO=price_...
STRIPE_PRICE_ENTERPRISE=price_...

# Dokku
DOKKU_HOST=dokku.mindroom.chat
DOKKU_USER=dokku
DOKKU_PORT=22

# Database (for local development)
PLATFORM_DB_USER=mindroom
PLATFORM_DB_PASSWORD=changeme
PLATFORM_DB_HOST=localhost
PLATFORM_DB_PORT=5432
PLATFORM_DB_NAME=mindroom_platform

# Redis (for local development)
PLATFORM_REDIS_PASSWORD=changeme
PLATFORM_REDIS_HOST=localhost
PLATFORM_REDIS_PORT=6379

# Email (optional)
RESEND_API_KEY=re_...
EMAIL_FROM=noreply@mindroom.chat

# Monitoring (optional)
SENTRY_DSN=https://...
DATADOG_API_KEY=...
```

### Task 4: Setup Scripts

Create `scripts/setup.sh`:

```bash
#!/bin/bash
set -e

echo "🧠 MindRoom Platform Setup"
echo "========================="

# Check prerequisites
check_command() {
    if ! command -v $1 &> /dev/null; then
        echo "❌ $1 is not installed. Please install it first."
        exit 1
    fi
}

echo "Checking prerequisites..."
check_command docker
check_command npm
check_command node

# Create .env from example
if [ ! -f .env ]; then
    echo "Creating .env file from template..."
    cp .env.example .env
    echo "⚠️  Please edit .env with your actual values"
    exit 1
fi

# Load environment
source .env

# Initialize Supabase
echo "Setting up Supabase..."
cd supabase
npx supabase init || true
npx supabase link --project-ref $SUPABASE_PROJECT_ID
npx supabase db push
npx supabase functions deploy
cd ..

# Install dependencies for each service
echo "Installing dependencies..."

echo "  - Stripe Handler..."
cd services/stripe-handler
npm install
cd ../..

echo "  - Customer Portal..."
cd apps/customer-portal
npm install
cd ../..

echo "  - Admin Dashboard..."
cd apps/admin-dashboard
npm install
cd ../..

# Setup Dokku SSH key
echo "Setting up Dokku SSH access..."
mkdir -p deploy/platform/ssh
if [ ! -f deploy/platform/ssh/dokku_key ]; then
    ssh-keygen -t rsa -b 4096 -f deploy/platform/ssh/dokku_key -N ""
    echo "⚠️  Add this public key to your Dokku server:"
    cat deploy/platform/ssh/dokku_key.pub
fi

# Create Docker network
echo "Creating Docker network..."
docker network create mindroom-platform 2>/dev/null || true

echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "1. Configure your .env file with actual values"
echo "2. Add the SSH public key to your Dokku server"
echo "3. Run: ./scripts/start-local.sh"
```

Create `scripts/start-local.sh`:

```bash
#!/bin/bash
set -e

echo "🚀 Starting MindRoom Platform (Local Development)"

# Start Supabase
echo "Starting Supabase..."
cd supabase
npx supabase start
cd ..

# Start platform services
echo "Starting platform services..."
docker-compose -f deploy/platform/docker-compose.local.yml up -d

# Wait for services
echo "Waiting for services to be ready..."
sleep 10

# Show status
docker-compose -f deploy/platform/docker-compose.local.yml ps

echo "✅ Platform is running!"
echo ""
echo "Access points:"
echo "  - Customer Portal: http://localhost:3000"
echo "  - Admin Dashboard: http://localhost:3001"
echo "  - Stripe Webhooks: http://localhost:3005"
echo "  - Dokku Provisioner: http://localhost:8002"
echo "  - Supabase Studio: http://localhost:54323"
```

Create `scripts/deploy-production.sh`:

```bash
#!/bin/bash
set -e

echo "🚀 Deploying MindRoom Platform to Production"

# Check environment
if [ "$ENVIRONMENT" != "production" ]; then
    echo "❌ This script should only be run in production!"
    exit 1
fi

# Build and push Docker images
echo "Building Docker images..."

# Build each service
docker build -t ${REGISTRY}/mindroom-stripe-handler services/stripe-handler
docker build -t ${REGISTRY}/mindroom-dokku-provisioner services/dokku-provisioner
docker build -t ${REGISTRY}/mindroom-customer-portal apps/customer-portal
docker build -t ${REGISTRY}/mindroom-admin-dashboard apps/admin-dashboard

# Push to registry
echo "Pushing to registry..."
docker push ${REGISTRY}/mindroom-stripe-handler
docker push ${REGISTRY}/mindroom-dokku-provisioner
docker push ${REGISTRY}/mindroom-customer-portal
docker push ${REGISTRY}/mindroom-admin-dashboard

# Deploy with Docker Swarm
echo "Deploying to swarm..."
docker stack deploy -c deploy/platform/docker-compose.prod.yml mindroom-platform

# Run database migrations
echo "Running database migrations..."
cd supabase
npx supabase db push --db-url $DATABASE_URL
cd ..

echo "✅ Deployment complete!"
```

### Task 5: Deployment to Dokku

Create `scripts/deploy-to-dokku.sh` for deploying the platform itself to Dokku:

```bash
#!/bin/bash
# Deploy the MindRoom platform services to Dokku

# Create Dokku apps for platform services
dokku apps:create mindroom-stripe-handler
dokku apps:create mindroom-provisioner
dokku apps:create mindroom-customer-portal
dokku apps:create mindroom-admin-dashboard

# Set up databases
dokku postgres:create platform-db
dokku postgres:link platform-db mindroom-stripe-handler
dokku postgres:link platform-db mindroom-provisioner

# Redis for sessions
dokku redis:create platform-redis
dokku redis:link platform-redis mindroom-stripe-handler

# Deploy each service
cd services/stripe-handler
git push dokku main
cd ../..

cd services/dokku-provisioner
git push dokku main
cd ../..

cd apps/customer-portal
git push dokku main
cd ../..

cd apps/admin-dashboard
git push dokku main
cd ../..
```

### Task 6: Documentation

Create `docs/DEPLOYMENT.md`:

```markdown
# MindRoom Platform Deployment Guide

## Architecture Overview

The MindRoom platform consists of several microservices:

```
┌─────────────────┐     ┌──────────────────┐
│ Customer Portal │────▶│   Supabase       │
│   (Next.js)     │     │  (Auth + DB)     │
└─────────────────┘     └──────────────────┘
                               ▲
┌─────────────────┐            │
│ Admin Dashboard │────────────┘
│  (React Admin)  │
└─────────────────┘

┌─────────────────┐     ┌──────────────────┐
│ Stripe Webhooks │────▶│ Stripe Handler   │
└─────────────────┘     │   (Node.js)      │
                        └──────────────────┘
                               │
                               ▼
                        ┌──────────────────┐
                        │ Dokku Provisioner│
                        │   (FastAPI)      │
                        └──────────────────┘
                               │
                               ▼
                        ┌──────────────────┐
                        │   Dokku Server   │
                        │ (Customer Apps)  │
                        └──────────────────┘
```

## Local Development

1. **Prerequisites**
   - Docker & Docker Compose
   - Node.js 18+
   - Supabase CLI
   - A Dokku server (can be local VM)

2. **Setup**
   ```bash
   ./scripts/setup.sh
   ```

3. **Start Services**
   ```bash
   ./scripts/start-local.sh
   ```

4. **Test Webhook**
   Use Stripe CLI to forward webhooks:
   ```bash
   stripe listen --forward-to localhost:3005/webhooks/stripe
   ```

## Production Deployment

### Option 1: Docker Swarm

1. Initialize swarm on your server
2. Configure environment variables
3. Run deployment script:
   ```bash
   ./scripts/deploy-production.sh
   ```

### Option 2: Dokku

1. Set up Dokku server
2. Install required plugins:
   ```bash
   dokku plugin:install postgres
   dokku plugin:install redis
   dokku plugin:install letsencrypt
   ```
3. Deploy platform:
   ```bash
   ./scripts/deploy-to-dokku.sh
   ```

### Option 3: Kubernetes

See `deploy/platform/k8s/` for Kubernetes manifests (TODO).

## Service Configuration

### Supabase
- Create project at supabase.com
- Enable email auth
- Run migrations: `npx supabase db push`
- Deploy Edge Functions: `npx supabase functions deploy`

### Stripe
1. Create products and prices
2. Configure webhook endpoint
3. Set webhook secret in environment

### Dokku Server
1. Set up Ubuntu server
2. Install Dokku
3. Add platform's SSH key
4. Configure wildcard DNS

## Monitoring

### Health Checks
- Stripe Handler: `http://service:3005/health`
- Dokku Provisioner: `http://service:8002/health`
- Customer Portal: `http://service:3000/api/health`
- Admin Dashboard: `http://service:3001/health`

### Logging
All services log to stdout/stderr. Use your preferred log aggregator:
- ELK Stack
- Datadog
- CloudWatch
- Loki/Grafana

### Metrics
Key metrics to monitor:
- Provisioning success rate
- Average provisioning time
- Webhook processing time
- Instance health status
- MRR and churn

## Troubleshooting

### Instance Provisioning Fails
1. Check Dokku provisioner logs
2. Verify SSH connectivity to Dokku
3. Ensure sufficient resources
4. Check Supabase connection

### Stripe Webhooks Not Received
1. Verify webhook secret
2. Check Stripe dashboard for errors
3. Ensure service is publicly accessible
4. Check firewall rules

### Customer Can't Access Instance
1. Verify instance status in admin
2. Check Dokku app status
3. Verify DNS configuration
4. Check SSL certificates

## Security Checklist

- [ ] Change all default passwords
- [ ] Enable 2FA for admin accounts
- [ ] Restrict admin dashboard access by IP
- [ ] Use SSL/TLS everywhere
- [ ] Rotate API keys regularly
- [ ] Enable audit logging
- [ ] Regular backups
- [ ] Security scanning
```

### Task 7: GitHub Actions CI/CD

Create `.github/workflows/platform-deploy.yml`:

```yaml
name: Deploy Platform

on:
  push:
    branches: [main]
    paths:
      - 'services/**'
      - 'apps/**'
      - 'deploy/platform/**'

env:
  REGISTRY: ghcr.io
  IMAGE_PREFIX: ${{ github.repository }}

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        service:
          - name: stripe-handler
            context: services/stripe-handler
          - name: dokku-provisioner
            context: services/dokku-provisioner
          - name: customer-portal
            context: apps/customer-portal
          - name: admin-dashboard
            context: apps/admin-dashboard

    steps:
      - uses: actions/checkout@v3

      - name: Log in to registry
        uses: docker/login-action@v2
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v4
        with:
          context: ${{ matrix.service.context }}
          push: true
          tags: |
            ${{ env.REGISTRY }}/${{ env.IMAGE_PREFIX }}-${{ matrix.service.name }}:latest
            ${{ env.REGISTRY }}/${{ env.IMAGE_PREFIX }}-${{ matrix.service.name }}:${{ github.sha }}

  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'

    steps:
      - uses: actions/checkout@v3

      - name: Deploy to production
        run: |
          # SSH to production server and pull new images
          # Or trigger webhook to update services
          echo "Deploy to production"
```

## Summary

You've created the following orchestration files:

1. **Docker Compose Files**:
   - `docker-compose.local.yml` - Local development
   - `docker-compose.prod.yml` - Production deployment

2. **Scripts**:
   - `setup.sh` - Initial setup
   - `start-local.sh` - Start local development
   - `deploy-production.sh` - Deploy to production
   - `deploy-to-dokku.sh` - Deploy using Dokku

3. **Configuration**:
   - `.env.example` - Environment template

4. **Documentation**:
   - `DEPLOYMENT.md` - Complete deployment guide

5. **CI/CD**:
   - GitHub Actions workflow

## Important Notes

1. DO NOT modify code in service directories
2. Only create orchestration and glue files
3. Test the integration thoroughly
4. Document any assumptions made
5. Ensure all services can communicate
6. Handle secrets securely
7. Include health checks and monitoring

The platform is now ready for deployment. Each service is self-contained, and this orchestration layer ties them together into a cohesive multi-tenant SaaS platform.
