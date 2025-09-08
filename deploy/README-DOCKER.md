# Docker Deployment Guide

## Overview

This directory contains Docker configurations for running the MindRoom platform services.

## Files

- `Dockerfile.frontend` - Example frontend Dockerfile (reference)
- `Dockerfile.platform-frontend` - Customer portal Next.js application
- `Dockerfile.stripe-handler` - Stripe webhook handler Node.js service
- `Dockerfile.dokku-provisioner` - Dokku provisioning FastAPI service
- `../docker-compose.platform.yml` - Orchestrates all platform services

## Running the Platform

### Prerequisites

1. Copy `.env.example` to `.env` and configure your environment variables:
```bash
cp .env.example .env
# Edit .env with your actual values
```

2. Ensure Docker and Docker Compose are installed:
```bash
docker --version
docker compose version
```

3. Install dependencies for admin user creation:
```bash
npm install @supabase/supabase-js dotenv
```

### Starting All Services

To run all platform services together:

```bash
# Build and start all services
docker compose -f docker-compose.platform.yml up --build

# Run in detached mode (background)
docker compose -f docker-compose.platform.yml up -d --build
```

Admin users are managed through Supabase. To create an admin user:
1. Sign up through the normal registration flow
2. Access the Supabase dashboard and navigate to the `accounts` table
3. Set `is_admin = true` for the user account

### Individual Service Management

```bash
# Start specific service
docker compose -f docker-compose.platform.yml up platform-frontend

# Rebuild specific service
docker compose -f docker-compose.platform.yml build stripe-handler
docker compose -f docker-compose.platform.yml up stripe-handler

# View logs for specific service
docker compose -f docker-compose.platform.yml logs -f platform-frontend

# Stop all services
docker compose -f docker-compose.platform.yml down

# Stop and remove volumes (clean slate)
docker compose -f docker-compose.platform.yml down -v
```

## Services

### Application Services

| Service | Port | Description | Health Check |
|---------|------|-------------|--------------|
| platform-frontend | 3000 | Customer-facing portal | /api/health |
| stripe-handler | 3007 | Stripe webhook processor | /health |
| dokku-provisioner | 8002 | Instance provisioning API | /health |

### Infrastructure Services

| Service | Port | Description |
|---------|------|-------------|
| platform-postgres | 5433 | PostgreSQL database |
| platform-redis | 6380 | Redis cache/sessions |

Note: Ports are offset from default to avoid conflicts with the existing Matrix infrastructure.

## Environment Variables

Key environment variables required:

- `SUPABASE_URL` - Supabase project URL
- `SUPABASE_ANON_KEY` - Supabase anonymous key
- `SUPABASE_SERVICE_KEY` - Supabase service key
- `STRIPE_SECRET_KEY` - Stripe API secret key
- `STRIPE_WEBHOOK_SECRET` - Stripe webhook signing secret
- `STRIPE_PUBLISHABLE_KEY` - Stripe publishable key
- `DOKKU_HOST` - Dokku server hostname
- `DOKKU_USER` - Dokku SSH user
- `PLATFORM_DB_USER` - Database username
- `PLATFORM_DB_PASSWORD` - Database password
- `PLATFORM_REDIS_PASSWORD` - Redis password

## Troubleshooting

### Build Issues

If you encounter TypeScript or build errors:
1. Ensure all dependencies are installed locally first
2. Check that TypeScript configurations are correct
3. Review the individual Dockerfile for build stage issues

### Network Issues

The platform services use a separate network (`mindroom-platform`) from the Matrix infrastructure to maintain isolation.

### Database Connection

If services can't connect to the database:
1. Ensure platform-postgres is running: `docker compose -f docker-compose.platform.yml ps`
2. Check that the database is initialized: `docker compose -f docker-compose.platform.yml logs platform-postgres`
3. Verify environment variables are set correctly

## Production Deployment

For production deployment:

1. Use proper secrets management (don't commit .env files)
2. Consider using Docker Swarm or Kubernetes for orchestration
3. Implement proper monitoring and logging
4. Use external managed databases (Supabase, managed PostgreSQL)
5. Set up proper SSL/TLS termination
6. Configure rate limiting and DDoS protection
